"""Real-WAL MainWindow refresh, database switching and shutdown acceptance."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from PyQt6 import QtCore, QtWidgets
from qcodes.dataset import (
    Measurement,
    load_or_create_experiment,
)
from qcodes.parameters import ManualParameter

from qplot.datahandling import trusted_work_coordinator as coordinator_module
from qplot.datahandling.file_identity import logical_database_path
from qplot.datahandling.trusted_derived_cache import TrustedDerivedDiskCache
from qplot.windows import _database_actions as database_actions
from qplot.windows import main as main_window
from qplot.windows._widgets import details_tables
from qplot.windows._widgets import preview as preview_module
from tests._window_lifecycle import close_main_window
from tests.datahandling.test_trusted_live import (
    _assert_protected_artifacts_unchanged,
    _stable_artifact_state,
)
from tests.windows._trusted_derived_wal_ui import (
    _bridge_complete,
    _prepare_live_database,
    _process_until,
    _tree_item_for_path,
    _tree_items,
)

pytestmark = pytest.mark.timeout(180)


def _start_live_run(
    writer,
    name: str,
    *,
    setpoint_label: str | None = None,
    signal_label: str | None = None,
):
    experiment = load_or_create_experiment(
        f"{name}_experiment",
        sample_name=f"{name}_sample",
        conn=writer,
    )
    setpoint_kwargs = {} if setpoint_label is None else {"label": setpoint_label}
    signal_kwargs = {} if signal_label is None else {"label": signal_label}
    setpoint = ManualParameter(f"{name}_setpoint", **setpoint_kwargs)
    signal = ManualParameter(f"{name}_signal", **signal_kwargs)
    measurement = Measurement(exp=experiment, name=f"{name}_run")
    measurement.write_period = 0.001
    measurement.register_parameter(setpoint)
    measurement.register_parameter(signal, setpoints=(setpoint,))
    context = measurement.run(write_in_background=False)
    try:
        datasaver = context.__enter__()
    except RuntimeError as error:
        cause = error
        while cause.__cause__ is not None:
            cause = cause.__cause__
        error.add_note(
            f"Run creation SQLite result: {getattr(cause, 'sqlite_errorname', None)} "
            f"({getattr(cause, 'sqlite_errorcode', None)})"
        )
        raise
    datasaver.add_result((setpoint, 0.0), (signal, 0.0))
    datasaver.flush_data_to_database(block=True)
    return experiment, setpoint, signal, context, datasaver, datasaver.dataset


def test_real_wal_progressive_ui_refresh_switch_and_close(
    tmp_path: Path,
    monkeypatch,
) -> None:
    first_directory = tmp_path / "first-database"
    second_directory = tmp_path / "second-database"
    first_directory.mkdir()
    second_directory.mkdir()
    first_path = first_directory / "first.db"
    second_path = second_directory / "second.db"
    first_writer = _prepare_live_database(first_path, "first")
    second_writer = _prepare_live_database(second_path, "second")
    first_run = _start_live_run(
        first_writer,
        "first_live",
        setpoint_label="S" * 250,
        signal_label="D" * 250,
    )
    second_run = _start_live_run(second_writer, "second_live")
    first_context = first_run[3]
    second_context = second_run[3]
    latest_first_run = None
    window = None
    exception_prefix = "Traceback (most recent call last):\n"
    exception_suffix = "\nKeyboardInterrupt"
    measurement_exception = (
        exception_prefix
        + "x"
        * (
            1_255
            - len(exception_prefix.encode("utf-8"))
            - len(exception_suffix.encode("utf-8"))
        )
        + exception_suffix
    )
    assert len(measurement_exception.encode("utf-8")) == 1_255
    first_run[5].add_metadata("measurement_exception", measurement_exception)
    first_run[5].add_metadata("stage5c_operator", "Ada")
    source_values = first_writer.execute(
        'SELECT "run_description", "measurement_exception", '
        '"stage5c_operator" FROM "runs" WHERE "run_id" = ?',
        (first_run[5].run_id,),
    ).fetchone()
    assert source_values is not None
    expected_run_description, stored_exception, stored_operator = source_values
    assert isinstance(expected_run_description, str)
    assert len(expected_run_description.encode("utf-8")) > 1_024
    assert stored_exception == measurement_exception
    assert stored_operator == "Ada"
    for index in range(1, 25):
        first_run[4].add_result(
            (first_run[1], float(index)),
            (first_run[2], float(index * 2)),
        )
    first_run[4].flush_data_to_database(block=True)
    for index in range(1, 9):
        second_run[4].add_result(
            (second_run[1], float(index)),
            (second_run[2], float(index * 2)),
        )
    second_run[4].flush_data_to_database(block=True)

    qplot_home = tmp_path / ".qplot"
    cache_root = tmp_path / "derived-cache"
    monkeypatch.setattr(main_window.config, "default_path", str(qplot_home))
    monkeypatch.setattr(
        main_window.config,
        "default_file",
        str(qplot_home / main_window.config.config_file_name),
    )
    monkeypatch.setattr(
        coordinator_module,
        "TrustedDerivedDiskCache",
        lambda **_kwargs: TrustedDerivedDiskCache(cache_root),
    )

    errors = []
    logged_errors = []

    def record_error(_owner, title, message, details=None) -> None:
        errors.append((title, message, details))

    def assert_selected_value_presentation():
        bridge = window._trusted_derived_bridge
        # A progressive detail can precede terminal metadata, which may refine
        # the source revision and correctly invalidate its exact-value viewer.
        # Exercise dialog reuse only after that revision and its detail settle.
        _process_until(
            lambda: (
                bridge.coordinator is not None
                and not bridge.coordinator.active
                and bridge.coordinator.snapshot().pending_count == 0
                and not window._database_refresh_active
                and not bridge.background_active()
                and bridge._selected_detail_publication is not None
                and bridge._detail_display_guid == str(first_run[5].guid)
            )
        )
        publication = bridge._selected_detail_publication
        assert publication is not None
        assert publication.run_guid == str(first_run[5].guid)
        detail = publication.detail
        assert detail.unavailable_fields == ()
        assert not detail.presentation.parameters_truncated
        assert detail.presentation.metadata.status == "available"
        assert detail.presentation.raw.status == "available"
        assert detail.presentation.metadata.shortened_value_count == 1
        assert detail.presentation.raw.shortened_value_count >= 2
        assert not any(
            item.text(0) == "[truncated]"
            for tree in (window.infoBox.metadata, window.infoBox.raw)
            for item in _tree_items(tree)
        )

        metadata_summary = _tree_item_for_path(
            window.infoBox.metadata,
            "/Metadata/[display]",
        )
        raw_summary = _tree_item_for_path(
            window.infoBox.raw,
            "/Raw/[display]",
        )
        assert "1 value shortened for display" in metadata_summary.text(1)
        assert "values shortened for display" in raw_summary.text(1)
        operator_item = _tree_item_for_path(
            window.infoBox.metadata,
            "/Metadata/stage5c_operator",
        )
        assert operator_item.text(1) == "Ada"

        description_item = _tree_item_for_path(
            window.infoBox.raw,
            "/Raw/Run/run_description",
        )
        exception_item = _tree_item_for_path(
            window.infoBox.metadata,
            "/Metadata/measurement_exception",
        )
        assert "[view full]" in description_item.text(1)
        assert "KeyboardInterrupt" in exception_item.text(1)
        assert (
            f"{len(expected_run_description.encode('utf-8'))} UTF-8 bytes"
            in description_item.toolTip(1)
        )
        assert "1255 UTF-8 bytes" in exception_item.toolTip(1)

        description_identifier = str(
            description_item.data(1, details_tables.FULL_VALUE_ID_ROLE) or ""
        )
        exception_identifier = str(
            exception_item.data(1, details_tables.FULL_VALUE_ID_ROLE) or ""
        )
        assert description_identifier
        assert exception_identifier
        assert (
            window.infoBox._trusted_full_values[description_identifier].text
            == expected_run_description
        )
        assert (
            window.infoBox._trusted_full_values[exception_identifier].text
            == measurement_exception
        )

        window.infoBox.raw.itemActivated.emit(description_item, 1)
        QtWidgets.QApplication.processEvents()
        dialog = window.infoBox._full_value_dialog
        assert dialog is not None
        assert dialog.text_edit.isReadOnly()
        assert dialog._exact_text == expected_run_description
        assert dialog.text_edit.toPlainText() == expected_run_description
        window.infoBox.metadata.itemDoubleClicked.emit(exception_item, 1)
        QtWidgets.QApplication.processEvents()
        assert window.infoBox._full_value_dialog is dialog
        assert dialog._exact_text == measurement_exception
        assert dialog.text_edit.toPlainText() == measurement_exception
        assert dialog.text_edit.toPlainText().endswith("KeyboardInterrupt")
        return publication, dialog

    try:
        with (
            patch.object(main_window.MainWindow, "show_error", record_error),
            patch.object(
                database_actions,
                "log_exception",
                side_effect=lambda label, error, *_args, **_kwargs: (
                    logged_errors.append((label, error))
                ),
            ),
            patch.object(
                database_actions,
                "DatabaseDetailWorker",
                wraps=database_actions.DatabaseDetailWorker,
            ) as legacy_cheap,
            patch.object(
                database_actions,
                "DatabaseExpensiveDetailWorker",
                wraps=database_actions.DatabaseExpensiveDetailWorker,
            ) as legacy_expensive,
            patch.object(
                database_actions,
                "DatabaseSelectedRunWorker",
                wraps=database_actions.DatabaseSelectedRunWorker,
            ) as legacy_selected,
            patch.object(
                preview_module,
                "PreviewWorker",
                wraps=preview_module.PreviewWorker,
            ) as legacy_preview,
        ):
            window = main_window.MainWindow()
            window.startupDatabaseTimer.stop()
            window.monitor.stop()
            window.config.config["user_preference"]["confirm_close"] = False
            window.config.config["user_preference"]["confirm_close_all"] = False

            window.close_database(status=False)
            assert window.load_database_path(str(first_path))
            _process_until(lambda: not window._database_load_active)
            assert window._database_access_mode == database_actions.TRUSTED_LIVE_MODE, (
                window._database_fallback_reason,
                errors,
            )
            assert window.RunList.topLevelItemCount() == 2
            first_guid = str(first_run[5].guid)
            first_item = window.RunList._item_for_guid(first_guid)
            assert first_item is not None
            assert "result_count" not in first_item.run_metadata
            _process_until(lambda: _bridge_complete(window, first_guid))
            assert first_item.run_metadata["result_count"] == 25
            assert window.infoBox.preview._trusted_derived_mode
            assert not window.infoBox.preview._workers
            assert legacy_cheap.call_count == 0
            assert legacy_expensive.call_count == 0
            assert legacy_selected.call_count == 0
            assert legacy_preview.call_count == 0
            assert errors == []
            assert logged_errors == []

            bridge = window._trusted_derived_bridge
            coordinator_before_reselection = bridge.coordinator
            timers_before_reselection = tuple(bridge.findChildren(QtCore.QTimer))
            cached_preview_before_reselection = window.infoBox.preview.cache[first_guid]
            window.RunList.setCurrentItem(first_item)
            first_item.setSelected(True)
            _process_until(lambda: window.infoBox.preview.current_guid == first_guid)
            assert window.load_database_path(str(first_path))
            QtWidgets.QApplication.processEvents()
            assert window.infoBox.preview._trusted_derived_mode
            assert bridge.coordinator is coordinator_before_reselection
            assert (
                tuple(bridge.findChildren(QtCore.QTimer)) == timers_before_reselection
            )
            assert len(timers_before_reselection) == 2
            assert window.infoBox.preview.cache[first_guid] is (
                cached_preview_before_reselection
            )
            assert not window.infoBox.preview._workers
            assert legacy_cheap.call_count == 0
            assert legacy_expensive.call_count == 0
            assert legacy_selected.call_count == 0
            assert legacy_preview.call_count == 0

            protected_before = _stable_artifact_state(
                first_path,
                consecutive_observations=2,
                observation_interval=0.02,
            )
            _process_until(
                lambda: (
                    bridge._detail_display_guid == first_guid
                    and bridge._selected_detail_publication is not None
                )
            )
            selected_publication, selected_dialog = assert_selected_value_presentation()
            prior_preview = window.infoBox.preview.cache[first_guid]
            window._trusted_derived_bridge.update_preview_size(window.preview_size + 17)
            _process_until(
                lambda: (
                    window.infoBox.preview.cache.get(first_guid) is not prior_preview
                )
            )
            _process_until(
                lambda: (
                    bridge._selected_detail_publication is not None
                    and bridge._detail_display_guid == first_guid
                    and not bridge.background_active()
                )
            )
            after_preview_publication, after_preview_dialog = (
                assert_selected_value_presentation()
            )
            selected_publication = after_preview_publication
            selected_dialog = after_preview_dialog
            protected_after = _stable_artifact_state(
                first_path,
                consecutive_observations=2,
                observation_interval=0.02,
            )
            _assert_protected_artifacts_unchanged(protected_before, protected_after)

            first_run[4].add_result((first_run[1], 25.0), (first_run[2], 50.0))
            first_run[4].flush_data_to_database(block=True)
            window.refreshMain()
            _process_until(lambda: first_item.run_metadata.get("result_count") == 26)
            _process_until(
                lambda: (
                    window._trusted_derived_bridge._metadata_by_guid[first_guid][
                        "result_count"
                    ]
                    == 26
                )
            )
            _process_until(
                lambda: (
                    bridge._selected_detail_publication is not None
                    and bridge._selected_detail_publication is not selected_publication
                    and bridge._detail_display_guid == first_guid
                )
            )
            refreshed_publication, refreshed_dialog = (
                assert_selected_value_presentation()
            )
            assert refreshed_publication is not selected_publication
            assert refreshed_dialog is not selected_dialog
            selected_publication = refreshed_publication
            selected_dialog = refreshed_dialog
            assert (errors, logged_errors) == ([], [])

            first_context.__exit__(None, None, None)
            first_context = None
            window.refreshMain()
            _process_until(lambda: bool(first_item.run_metadata.get("is_completed")))
            _process_until(
                lambda: bool(
                    dict(
                        window._trusted_derived_bridge._metadata_by_guid[first_guid][
                            "run_fields"
                        ]
                    ).get("is_completed")
                )
            )
            assert (errors, logged_errors) == ([], [])

            # Completing derived work does not finish the independent refresh
            # broker. Drain both before this fixture's schema-changing phase;
            # live appends and checkpoint progress are exercised separately.
            service = window._trusted_read_service
            supervisor = service._required_supervisor()
            _process_until(
                lambda: (
                    bridge.coordinator is not None
                    and not bridge.coordinator.active
                    and bridge.coordinator.snapshot().pending_count == 0
                    and not window._database_refresh_active
                    and service.liveness().outstanding_requests == 0
                    and not supervisor.resource_liveness().active_job
                )
            )
            latest_first_run = _start_live_run(first_writer, "first_new")
            window.refreshMain()
            _process_until(lambda: window.RunList.topLevelItemCount() == 3)
            assert (errors, logged_errors) == ([], [])
            new_guid = str(latest_first_run[5].guid)
            _process_until(lambda: _bridge_complete(window, new_guid))

            coordinator = bridge.coordinator
            assert coordinator is not None
            _process_until(lambda: not window._database_refresh_active)
            _process_until(
                lambda: (
                    not coordinator.active and coordinator.snapshot().pending_count == 0
                )
            )
            generation = coordinator.snapshot().generation
            supervisor = window._trusted_read_service._required_supervisor()
            _process_until(lambda: not supervisor.resource_liveness().active_job)
            prior_incarnation = supervisor.incarnation
            prior_metadata = bridge._metadata_by_guid[first_guid]
            publication_before_restart = bridge._selected_detail_publication
            assert publication_before_restart is not None
            assert any(
                value.text == expected_run_description
                for value in window.infoBox._trusted_full_values.values()
            )
            supervisor.restart()
            assert supervisor.incarnation > prior_incarnation
            bridge.helper_restarted()
            assert not window.infoBox._trusted_full_values
            assert window.infoBox._full_value_dialog is None
            assert coordinator.snapshot().generation > generation
            _process_until(
                lambda: bridge._metadata_by_guid.get(first_guid) is not prior_metadata
            )
            _process_until(
                lambda: (
                    bridge._selected_detail_publication is not None
                    and bridge._selected_detail_publication
                    is not publication_before_restart
                    and bridge._detail_display_guid == first_guid
                )
            )
            assert_selected_value_presentation()
            _process_until(
                lambda: (
                    not coordinator.active and coordinator.snapshot().pending_count == 0
                )
            )

            bridge.source_changed((latest_first_run[5].run_id,))
            assert coordinator.active
            assert window.load_database_path(str(second_path))
            _process_until(
                lambda: (
                    not window._database_load_active
                    and window.fileTextbox.text() == str(second_path)
                    and window.RunList.topLevelItemCount() == 2
                )
            )
            assert window._database_access_mode == database_actions.TRUSTED_LIVE_MODE, (
                window._database_fallback_reason,
                errors,
            )
            second_guid = str(second_run[5].guid)
            _process_until(
                lambda: (
                    bridge._database_instance is not None
                    and bridge._database_instance.logical_path
                    == logical_database_path(second_path)
                )
            )
            assert first_guid not in bridge._metadata_by_guid
            assert all(
                value.text not in {expected_run_description, measurement_exception}
                for value in window.infoBox._trusted_full_values.values()
            )
            if window.infoBox._full_value_dialog is not None:
                assert window.infoBox._full_value_dialog._exact_text not in {
                    expected_run_description,
                    measurement_exception,
                }
            _process_until(lambda: _bridge_complete(window, second_guid))

            assert (
                second_writer.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()[0]
                == 0
            )
            _process_until(
                lambda: (
                    not bridge.coordinator.active
                    and bridge.coordinator.snapshot().pending_count == 0
                )
            )
            assert second_writer.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone() == (
                0,
                0,
                0,
            )
            second_run[4].add_result((second_run[1], 9.0), (second_run[2], 18.0))
            second_run[4].flush_data_to_database(block=True)

        assert not tuple(first_directory.glob("*.qdc"))
        assert not tuple(second_directory.glob("*.qdc"))
        assert tuple(cache_root.glob("*.qdc"))
    finally:
        if window is not None:
            window.close_database(status=False)
            _process_until(
                lambda: (
                    not window._trusted_derived_bridge.background_active()
                    and not window._retired_trusted_read_services
                ),
                timeout=30.0,
            )
            close_main_window(window, timeout_ms=12_000)
        if latest_first_run is not None:
            latest_first_run[4].add_result(
                (latest_first_run[1], 1.0),
                (latest_first_run[2], 2.0),
            )
            latest_first_run[4].flush_data_to_database(block=True)
        assert first_writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (
            0,
            0,
            0,
        )
        if first_context is not None:
            first_context.__exit__(None, None, None)
        if latest_first_run is not None and latest_first_run[3] is not first_context:
            latest_first_run[3].__exit__(None, None, None)
        if second_context is not None:
            second_context.__exit__(None, None, None)
        first_writer.close()
        second_writer.close()
