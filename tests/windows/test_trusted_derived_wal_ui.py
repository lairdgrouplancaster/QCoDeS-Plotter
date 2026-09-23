"""Production MainWindow acceptance for the Stage 5C real-WAL path."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest
from PyQt6 import QtCore, QtGui, QtTest, QtWidgets
from qcodes import Station
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_by_id,
    load_or_create_experiment,
)
from qcodes.dataset.sqlite.database import connect
from qcodes.parameters import ManualParameter

from qplot.datahandling import trusted_work_coordinator as coordinator_module
from qplot.datahandling.file_identity import logical_database_path
from qplot.datahandling.trusted_derived_cache import TrustedDerivedDiskCache
from qplot.testdata import (
    RunSpecification,
    enable_generation_provenance_for_writer,
    generate_database,
)
from qplot.windows import _database_actions as database_actions
from qplot.windows import _trusted_derived_qt as bridge_module
from qplot.windows import main as main_window
from qplot.windows._widgets import details_tables
from qplot.windows._widgets import preview as preview_module
from qplot.windows._widgets import treeWidgets as tree_widgets
from tests._window_lifecycle import close_main_window
from tests.datahandling.test_trusted_live import (
    _assert_protected_artifacts_unchanged,
    _stable_artifact_state,
)

pytestmark = pytest.mark.timeout(180)


def _process_until(predicate, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QtWidgets.QApplication.processEvents()
        if predicate():
            return
        QtTest.QTest.qWait(5)
    raise AssertionError("Stage 5C real-WAL UI condition was not reached")


def _bridge_complete(window, guid: str) -> bool:
    bridge = window._trusted_derived_bridge
    return bool(
        guid in bridge._metadata_by_guid
        and window.RunList.run_preview_is_ready(guid)
        and guid in window.infoBox.preview.cache
    )


def _prepare_live_database(path: Path, name: str):
    generate_database(
        [RunSpecification(1, f"{name}_seed", "Seed", "V", 0.0, 1.0, 3)],
        path,
    )
    initialise_or_create_database_at(str(path), journal_mode="WAL")
    writer = connect(path)
    writer.execute("PRAGMA wal_autocheckpoint = 0")
    enable_generation_provenance_for_writer(writer)
    return writer


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
    datasaver = context.__enter__()
    datasaver.add_result((setpoint, 0.0), (signal, 0.0))
    datasaver.flush_data_to_database(block=True)
    return experiment, setpoint, signal, context, datasaver, datasaver.dataset


def _start_partial_two_dependent_run(
    writer,
    name: str,
    *,
    slow_count: int,
    fast_count: int,
    acquired_count: int,
):
    experiment = load_or_create_experiment(
        f"{name}_experiment",
        sample_name=f"{name}_sample",
        conn=writer,
    )
    slow = ManualParameter(f"{name}_slow")
    fast = ManualParameter(f"{name}_fast")
    signal = ManualParameter(f"{name}_signal")
    signal_b = ManualParameter(f"{name}_signal_b")
    station = Station(slow, fast, signal, signal_b)
    measurement = Measurement(
        exp=experiment,
        name=f"{name}_run",
        station=station,
    )
    measurement.write_period = 3_600
    measurement.register_parameter(slow)
    measurement.register_parameter(fast)
    measurement.register_parameter(signal, setpoints=(slow, fast))
    measurement.register_parameter(signal_b, setpoints=(slow, fast))
    measurement.set_shapes(
        {
            signal.name: (slow_count, fast_count),
            signal_b.name: (slow_count, fast_count),
        }
    )
    context = measurement.run(write_in_background=False)
    datasaver = context.__enter__()
    for logical_index in range(acquired_count):
        slow_index, fast_index = divmod(logical_index, fast_count)
        datasaver.add_result(
            (slow, float(slow_index)),
            (fast, float(fast_index)),
            (signal, float(slow_index * 1_000 + fast_index)),
            (signal_b, float(1_000_000 + slow_index * 1_000 + fast_index)),
        )
    datasaver.flush_data_to_database(block=True)
    datasaver.dataset.add_metadata("stage5c_operator", "Ada")
    return (
        experiment,
        station,
        slow,
        fast,
        signal,
        signal_b,
        context,
        datasaver,
        datasaver.dataset,
    )


def _tree_keys(tree: QtWidgets.QTreeWidget) -> set[str]:
    keys: set[str] = set()
    pending = [tree.topLevelItem(index) for index in range(tree.topLevelItemCount())]
    while pending:
        item = pending.pop()
        if item is None:
            continue
        keys.add(item.text(0))
        pending.extend(item.child(index) for index in range(item.childCount()))
    return keys


def _tree_items(tree: QtWidgets.QTreeWidget):
    iterator = QtWidgets.QTreeWidgetItemIterator(tree)
    while iterator.value() is not None:
        yield iterator.value()
        iterator += 1


def _tree_item_for_path(tree: QtWidgets.QTreeWidget, path: str):
    return next(
        item
        for item in _tree_items(tree)
        if str(item.data(0, details_tables.FULL_VALUE_PATH_ROLE) or "") == path
    )


def _overview_value(window, key: str) -> str | None:
    table = window.infoBox.overview
    for row in range(table.rowCount()):
        field = table.item(row, 0)
        value = table.item(row, 1)
        if field is not None and field.text() == key:
            return None if value is None else value.text()
    return None


def _rgba(image: QtGui.QImage, x: int, y: int) -> tuple[int, int, int, int]:
    return image.pixelColor(x, y).getRgb()


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

            _process_until(
                lambda: (
                    bridge.coordinator is not None
                    and not bridge.coordinator.active
                    and bridge.coordinator.snapshot().pending_count == 0
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


def test_real_wal_partial_two_dependent_grid_populates_detail_before_images(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Exercise the reported current-QCoDeS live layout through MainWindow."""

    slow_count = 108
    fast_count = 861
    partial_fast_count = 437
    planned_count = slow_count * fast_count
    partial_count = (slow_count - 1) * fast_count + partial_fast_count
    physical_partial_count = partial_count * 2
    database_directory = tmp_path / "partial-two-dependent"
    database_directory.mkdir()
    database_path = database_directory / "live.db"
    writer = _prepare_live_database(database_path, "partial")
    live_run = _start_partial_two_dependent_run(
        writer,
        "stage5c_partial",
        slow_count=slow_count,
        fast_count=fast_count,
        acquired_count=partial_count,
    )
    slow, fast = live_run[2], live_run[3]
    signal, signal_b = live_run[4], live_run[5]
    context, datasaver, dataset = live_run[6], live_run[7], live_run[8]
    guid = str(dataset.guid)
    protected_before_reader = _stable_artifact_state(
        database_path,
        consecutive_observations=2,
        observation_interval=0.02,
    )

    qplot_home = tmp_path / ".qplot-partial"
    cache_root = tmp_path / "partial-derived-cache"
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

    events: list[tuple[str, object]] = []
    original_update_runs = tree_widgets.RunList.updateRuns
    original_detail = tree_widgets.moreInfo.set_snapshot_run_detail
    original_thumbnail = tree_widgets.RunList.set_run_previews
    original_preview = preview_module.PreviewTab.publish_trusted_previews

    def record_update_runs(widget, runs, *args, **kwargs):
        result = original_update_runs(widget, runs, *args, **kwargs)
        for run in runs.values():
            if str(run.get("guid") or "") != guid:
                continue
            acquired = run.get("read_setpoint_count")
            shape = tuple(run.get("setpoint_shape") or ())
            if acquired in {partial_count, planned_count} and shape == (
                slow_count,
                fast_count,
            ):
                events.append((f"exact-{acquired}", dict(run)))
        return result

    def record_detail(widget, detail):
        result = original_detail(widget, detail)
        if str(detail.run.as_dict().get("guid") or "") == guid:
            events.append(("detail", detail))
        return result

    def record_thumbnail(widget, published_guid, previews):
        result = original_thumbnail(widget, published_guid, previews)
        if str(published_guid or "") == guid:
            events.append(("thumbnail", tuple(previews or ())))
        return result

    def record_preview(widget, published_guid, previews, *, error=None):
        result = original_preview(
            widget,
            published_guid,
            previews,
            error=error,
        )
        if str(published_guid or "") == guid:
            events.append(("preview", tuple(previews or ())))
        return result

    monkeypatch.setattr(tree_widgets.RunList, "updateRuns", record_update_runs)
    monkeypatch.setattr(
        tree_widgets.moreInfo,
        "set_snapshot_run_detail",
        record_detail,
    )
    monkeypatch.setattr(
        tree_widgets.RunList,
        "set_run_previews",
        record_thumbnail,
    )
    monkeypatch.setattr(
        preview_module.PreviewTab,
        "publish_trusted_previews",
        record_preview,
    )

    errors = []
    logged_errors = []
    window = None
    context_open = True
    try:
        with (
            patch.object(
                main_window.MainWindow,
                "show_error",
                side_effect=lambda _owner, title, message, details=None: errors.append(
                    (title, message, details)
                ),
            ),
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
            assert window.load_database_path(str(database_path))
            _process_until(lambda: not window._database_load_active)
            assert window._database_access_mode == database_actions.TRUSTED_LIVE_MODE, (
                window._database_fallback_reason,
                errors,
            )
            assert window.RunList.topLevelItemCount() == 2
            item = window.RunList._item_for_guid(guid)
            assert item is not None
            # The committed basic table is visible before the 46-page exact
            # prefix verification has produced any derived image.
            assert not any(name in {"thumbnail", "preview"} for name, _ in events)
            assert item.run_metadata.get("read_setpoint_count") != partial_count

            window.RunList.clearSelection()
            window.RunList.setCurrentItem(item)
            item.setSelected(True)
            _process_until(lambda: window._selected_run_guid == guid)
            _process_until(lambda: any(name == "detail" for name, _ in events))
            _process_until(
                lambda: item.run_metadata.get("read_setpoint_count") == partial_count,
                timeout=120.0,
            )
            _process_until(
                lambda: (
                    window.RunList.run_preview_is_ready(guid)
                    and guid in window.infoBox.preview.cache
                    and len(window.infoBox.preview.cache[guid]) == 2
                ),
                timeout=120.0,
            )

            assert item.run_metadata["result_count"] == physical_partial_count
            assert item.run_metadata["setpoint_shape"] == [slow_count, fast_count]
            assert item.run_metadata["setpoint_shape_source"] == "planned"
            assert item.run_metadata["setpoint_count"] == planned_count
            assert item.run_metadata["read_setpoint_count"] == partial_count
            setpoints_column = window.RunList.cols.index("Setpoints")
            status_column = window.RunList.cols.index("Status")
            assert item.text(setpoints_column) == ("92,564 / 92,988 = 108 × 861")
            assert item.text(status_column) == "Running (99.5%)"
            assert _overview_value(window, "Data points") == (
                "92,564 / 92,988 = 108 × 861"
            )

            event_names = [name for name, _payload in events]
            detail_index = event_names.index("detail")
            exact_index = event_names.index(f"exact-{partial_count}")
            thumbnail_index = event_names.index("thumbnail")
            preview_index = event_names.index("preview")
            assert detail_index < min(thumbnail_index, preview_index)
            assert exact_index < min(thumbnail_index, preview_index)
            assert event_names.count("thumbnail") == 1
            assert event_names.count("preview") == 1

            raw_keys = _tree_keys(window.infoBox.raw)
            assert {
                "Run",
                "Metadata",
                "Snapshot",
                "Parameters",
                "Setpoint summaries",
                "Unavailable fields",
            } <= raw_keys
            assert "stage5c_operator" in _tree_keys(window.infoBox.metadata)
            snapshot_keys = _tree_keys(window.infoBox.snapshot)
            assert snapshot_keys
            assert "Snapshot unavailable" not in snapshot_keys
            assert window.infoBox.preview.current_guid == guid

            neutral = (230, 230, 230, 255)
            initial_previews = window.infoBox.preview.cache[guid]
            previews_by_parameter = {
                str(preview["parameter"]): preview for preview in initial_previews
            }
            assert set(previews_by_parameter) == {signal.name, signal_b.name}
            for preview in previews_by_parameter.values():
                image = preview["image"]
                assert isinstance(image, QtGui.QImage)
                assert _rgba(image, 0, 0) != neutral
                assert _rgba(image, image.width() - 1, 0) == neutral
                upper_left = _rgba(image, 0, 0)
                lower_right = _rgba(image, image.width() - 1, image.height() - 1)
                assert sum(upper_left[:3]) > sum(lower_right[:3])

            coordinator = window._trusted_derived_bridge.coordinator
            assert coordinator is not None
            _process_until(
                lambda: (
                    not coordinator.active and coordinator.snapshot().pending_count == 0
                ),
                timeout=120.0,
            )
            protected_after_reader = _stable_artifact_state(
                database_path,
                consecutive_observations=2,
                observation_interval=0.02,
            )
            _assert_protected_artifacts_unchanged(
                protected_before_reader,
                protected_after_reader,
            )

            append_event_index = len(events)
            for fast_index in range(partial_fast_count, fast_count):
                datasaver.add_result(
                    (slow, float(slow_count - 1)),
                    (fast, float(fast_index)),
                    (
                        signal,
                        float((slow_count - 1) * 1_000 + fast_index),
                    ),
                    (
                        signal_b,
                        float(1_000_000 + (slow_count - 1) * 1_000 + fast_index),
                    ),
                )
            datasaver.flush_data_to_database(block=True)
            protected_before_append_reader = _stable_artifact_state(
                database_path,
                consecutive_observations=2,
                observation_interval=0.02,
            )
            window.refreshMain()
            _process_until(
                lambda: item.run_metadata.get("read_setpoint_count") == planned_count,
                timeout=120.0,
            )
            _process_until(
                lambda: (
                    guid in window.infoBox.preview.cache
                    and window.infoBox.preview.cache[guid] is not initial_previews
                    and len(window.infoBox.preview.cache[guid]) == 2
                ),
                timeout=120.0,
            )

            assert item.run_metadata["result_count"] == planned_count * 2
            assert item.run_metadata["read_setpoint_count"] == planned_count
            assert item.text(setpoints_column) == "92,988 = 108 × 861"
            assert _overview_value(window, "Data points") == ("92,988 = 108 × 861")
            appended_names = [name for name, _payload in events[append_event_index:]]
            assert appended_names.index(f"exact-{planned_count}") < min(
                appended_names.index("thumbnail"),
                appended_names.index("preview"),
            )
            assert "detail" in appended_names
            assert appended_names.index("detail") < min(
                appended_names.index("thumbnail"),
                appended_names.index("preview"),
            )
            updated_previews = window.infoBox.preview.cache[guid]
            for preview in updated_previews:
                image = preview["image"]
                assert isinstance(image, QtGui.QImage)
                assert _rgba(image, 0, 0) != neutral
                assert _rgba(image, image.width() - 1, 0) != neutral
                assert sum(_rgba(image, 0, 0)[:3]) > sum(
                    _rgba(image, image.width() - 1, image.height() - 1)[:3]
                )

            _process_until(
                lambda: (
                    not coordinator.active and coordinator.snapshot().pending_count == 0
                ),
                timeout=120.0,
            )
            protected_after_append_reader = _stable_artifact_state(
                database_path,
                consecutive_observations=2,
                observation_interval=0.02,
            )
            _assert_protected_artifacts_unchanged(
                protected_before_append_reader,
                protected_after_append_reader,
            )
            assert writer.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()[0] == 0
            assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (
                0,
                0,
                0,
            )
            # A writer can continue immediately after the trusted reader and
            # checkpoint interaction.  This row is intentionally not refreshed
            # into qPlot because it lies beyond the declared acquisition plan.
            datasaver.add_result(
                (slow, float(slow_count)),
                (fast, 0.0),
                (signal, float(slow_count * 1_000)),
                (signal_b, float(1_000_000 + slow_count * 1_000)),
            )
            datasaver.flush_data_to_database(block=True)

            assert legacy_cheap.call_count == 0
            assert legacy_expensive.call_count == 0
            assert legacy_selected.call_count == 0
            assert legacy_preview.call_count == 0
            assert errors == []
            assert logged_errors == []

        assert not tuple(database_directory.glob("*.qdc"))
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
        if context_open:
            context.__exit__(None, None, None)
            context_open = False
        assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
        writer.close()


def test_real_wal_mixed_size_tiers_do_not_starve_selected_images(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Selected conclusive output must outrun background progressive pages."""

    slow_count = 48
    fast_count = 257
    visible_count = 5_000
    remaining_count = 11_000
    database_directory = tmp_path / "mixed-size-tiers"
    database_directory.mkdir()
    database_path = database_directory / "live.db"
    writer = _prepare_live_database(database_path, "mixed")
    small_dataset = load_by_id(1, conn=writer)
    visible_run = _start_partial_two_dependent_run(
        writer,
        "mixed_visible",
        slow_count=slow_count,
        fast_count=fast_count,
        acquired_count=visible_count,
    )
    remaining_run = _start_partial_two_dependent_run(
        writer,
        "mixed_remaining",
        slow_count=slow_count,
        fast_count=fast_count,
        acquired_count=remaining_count,
    )
    open_contexts = [visible_run[6], remaining_run[6]]
    small_guid = str(small_dataset.guid)
    visible_guid = str(visible_run[8].guid)
    remaining_guid = str(remaining_run[8].guid)
    protected_before_reader = _stable_artifact_state(
        database_path,
        consecutive_observations=2,
        observation_interval=0.02,
    )

    qplot_home = tmp_path / ".qplot-mixed"
    cache_root = tmp_path / "mixed-derived-cache"
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

    real_coordinator = bridge_module.TrustedWorkCoordinator

    class _GatedCoordinator(real_coordinator):
        """Delay only initial dispatch until selection and viewport are exact."""

        def __init__(self, *args, **kwargs) -> None:
            self._test_work_released = False
            super().__init__(*args, **kwargs)

        def _pump(self, *args, **kwargs) -> None:
            if self._test_work_released:
                super()._pump(*args, **kwargs)

        def release_test_work(self) -> None:
            self._test_work_released = True
            super()._pump()

    monkeypatch.setattr(bridge_module, "TrustedWorkCoordinator", _GatedCoordinator)

    events: list[tuple[str, str, object]] = []
    original_metadata = bridge_module.TrustedDerivedQtBridge._publish_metadata
    original_detail = tree_widgets.moreInfo.set_snapshot_run_detail
    original_thumbnail = tree_widgets.RunList.set_run_previews
    original_preview = preview_module.PreviewTab.publish_trusted_previews

    def record_metadata(bridge, publication, run_id, guid, payload):
        result = original_metadata(bridge, publication, run_id, guid, payload)
        marker = ("metadata", str(guid))
        if guid in bridge._metadata_by_guid and not any(
            (kind, published_guid) == marker
            for kind, published_guid, _payload in events
        ):
            events.append(("metadata", str(guid), dict(payload)))
        return result

    def record_detail(widget, detail):
        result = original_detail(widget, detail)
        guid = str(detail.run.as_dict().get("guid") or "")
        events.append(("detail", guid, detail))
        return result

    def record_thumbnail(widget, guid, previews):
        result = original_thumbnail(widget, guid, previews)
        events.append(("thumbnail", str(guid or ""), tuple(previews or ())))
        return result

    def record_preview(widget, guid, previews, *, error=None):
        result = original_preview(widget, guid, previews, error=error)
        events.append(("preview", str(guid or ""), tuple(previews or ())))
        return result

    monkeypatch.setattr(
        bridge_module.TrustedDerivedQtBridge,
        "_publish_metadata",
        record_metadata,
    )
    monkeypatch.setattr(
        tree_widgets.moreInfo,
        "set_snapshot_run_detail",
        record_detail,
    )
    monkeypatch.setattr(
        tree_widgets.RunList,
        "set_run_previews",
        record_thumbnail,
    )
    monkeypatch.setattr(
        preview_module.PreviewTab,
        "publish_trusted_previews",
        record_preview,
    )

    def event_index(kind: str, guid: str) -> int:
        return next(
            index
            for index, (event_kind, event_guid, _payload) in enumerate(events)
            if event_kind == kind and event_guid == guid
        )

    def nonempty_image_event(kind: str, guid: str) -> bool:
        for event_kind, event_guid, previews in events:
            if event_kind != kind or event_guid != guid or not previews:
                continue
            if all(
                isinstance(preview.get("image"), QtGui.QImage)
                and not preview["image"].isNull()
                and not preview.get("unsupported")
                for preview in previews
            ):
                return True
        return False

    errors = []
    logged_errors = []
    window = None
    active_service = None
    try:
        with (
            patch.object(
                main_window.MainWindow,
                "show_error",
                side_effect=lambda _owner, title, message, details=None: errors.append(
                    (title, message, details)
                ),
            ),
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
            window.resize(900, 500)
            window.show()

            window.close_database(status=False)
            assert window.load_database_path(str(database_path))
            _process_until(lambda: not window._database_load_active)
            assert window._database_access_mode == database_actions.TRUSTED_LIVE_MODE, (
                window._database_fallback_reason,
                errors,
            )
            assert window.RunList.topLevelItemCount() == 3
            assert events == []

            small_item = window.RunList._item_for_guid(small_guid)
            visible_item = window.RunList._item_for_guid(visible_guid)
            remaining_item = window.RunList._item_for_guid(remaining_guid)
            assert small_item is not None
            assert visible_item is not None
            assert remaining_item is not None
            id_column = window.RunList.cols.index("ID")
            window.RunList.sortItems(id_column, QtCore.Qt.SortOrder.AscendingOrder)
            row = window.RunList.indexOfTopLevelItem(visible_item)
            row_height = max(24, window.RunList.sizeHintForRow(row))
            header = window.RunList.header()
            assert header is not None
            # Leave only the upper half of one row in the viewport.  The
            # bridge deliberately treats partial rows as visible, while the
            # following large run remains unambiguously off-screen.
            window.RunList.setFixedHeight(header.height() + max(12, row_height // 2))
            window.RunList.clearSelection()
            window.RunList.setCurrentItem(small_item)
            small_item.setSelected(True)
            _process_until(lambda: window._selected_run_guid == small_guid)
            window.RunList.scrollToItem(
                visible_item,
                QtWidgets.QAbstractItemView.ScrollHint.PositionAtTop,
            )
            QtWidgets.QApplication.processEvents()

            bridge = window._trusted_derived_bridge
            bridge._apply_priority()
            visible_indices = bridge._visible_stable_indices()
            assert bridge._index_by_guid[visible_guid] in visible_indices
            assert bridge._index_by_guid[remaining_guid] not in visible_indices
            coordinator = bridge.coordinator
            assert isinstance(coordinator, _GatedCoordinator)
            assert not coordinator.active
            coordinator.release_test_work()

            _process_until(
                lambda: (
                    small_guid in bridge._metadata_by_guid
                    and any(
                        kind == "detail" and guid == small_guid
                        for kind, guid, _payload in events
                    )
                    and nonempty_image_event("thumbnail", small_guid)
                    and nonempty_image_event("preview", small_guid)
                ),
                timeout=60.0,
            )
            # The selected conclusive result must not wait for every background
            # layout page.  Neither large run has publishable metadata yet.
            background_metadata_ready = {
                guid
                for guid in (visible_guid, remaining_guid)
                if guid in bridge._metadata_by_guid
            }
            assert background_metadata_ready == set(), [
                (kind, guid) for kind, guid, _payload in events
            ]
            assert event_index("metadata", small_guid) < event_index(
                "thumbnail", small_guid
            )
            assert event_index("metadata", small_guid) < event_index(
                "preview", small_guid
            )
            assert event_index("detail", small_guid) < event_index(
                "thumbnail", small_guid
            )
            assert event_index("detail", small_guid) < event_index(
                "preview", small_guid
            )
            assert window.infoBox.preview.current_guid == small_guid

            _process_until(lambda: _bridge_complete(window, visible_guid), timeout=90.0)
            _process_until(
                lambda: _bridge_complete(window, remaining_guid),
                timeout=90.0,
            )
            assert event_index("metadata", visible_guid) < event_index(
                "metadata", remaining_guid
            )
            _process_until(
                lambda: (
                    not coordinator.active and coordinator.snapshot().pending_count == 0
                ),
                timeout=90.0,
            )
            protected_after_reader = _stable_artifact_state(
                database_path,
                consecutive_observations=2,
                observation_interval=0.02,
            )
            _assert_protected_artifacts_unchanged(
                protected_before_reader,
                protected_after_reader,
            )

            prior_visible_preview = window.infoBox.preview.cache[visible_guid]
            visible_slow, visible_fast = visible_run[2], visible_run[3]
            visible_signal, visible_signal_b = visible_run[4], visible_run[5]
            visible_datasaver = visible_run[7]
            for logical_index in range(visible_count, visible_count + fast_count):
                slow_index, fast_index = divmod(logical_index, fast_count)
                visible_datasaver.add_result(
                    (visible_slow, float(slow_index)),
                    (visible_fast, float(fast_index)),
                    (visible_signal, float(slow_index * 1_000 + fast_index)),
                    (
                        visible_signal_b,
                        float(1_000_000 + slow_index * 1_000 + fast_index),
                    ),
                )
            visible_datasaver.flush_data_to_database(block=True)
            window.refreshMain()
            _process_until(
                lambda: (
                    visible_item.run_metadata.get("read_setpoint_count")
                    == visible_count + fast_count
                ),
                timeout=90.0,
            )
            _process_until(
                lambda: (
                    window.infoBox.preview.cache.get(visible_guid)
                    is not prior_visible_preview
                ),
                timeout=90.0,
            )
            _process_until(
                lambda: (
                    not coordinator.active and coordinator.snapshot().pending_count == 0
                ),
                timeout=90.0,
            )

            assert writer.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()[0] == 0
            assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (
                0,
                0,
                0,
            )
            next_index = visible_count + fast_count
            slow_index, fast_index = divmod(next_index, fast_count)
            visible_datasaver.add_result(
                (visible_slow, float(slow_index)),
                (visible_fast, float(fast_index)),
                (visible_signal, float(slow_index * 1_000 + fast_index)),
                (
                    visible_signal_b,
                    float(1_000_000 + slow_index * 1_000 + fast_index),
                ),
            )
            visible_datasaver.flush_data_to_database(block=True)
            assert Path(f"{database_path}-wal").stat().st_size > 0

            assert legacy_cheap.call_count == 0
            assert legacy_expensive.call_count == 0
            assert legacy_selected.call_count == 0
            assert legacy_preview.call_count == 0
            assert errors == []
            assert logged_errors == []
            assert not tuple(database_directory.glob("*.qdc"))
            assert tuple(cache_root.glob("*.qdc"))

            active_service = window._trusted_read_service
            assert active_service is not None
            assert active_service.liveness().helper_alive
            window.close_database(status=False)
            _process_until(
                lambda: (
                    not window._trusted_derived_bridge.background_active()
                    and not window._retired_trusted_read_services
                ),
                timeout=30.0,
            )
            assert active_service.closed
            closed_liveness = active_service.liveness()
            assert not closed_liveness.dispatcher_alive
            assert not closed_liveness.control_alive
            assert not closed_liveness.helper_alive
            close_main_window(window, timeout_ms=12_000)
            window = None
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
        for context in reversed(open_contexts):
            context.__exit__(None, None, None)
        assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
        writer.close()


def test_real_qcodes_large_snapshot_is_paged_beyond_old_node_limit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Reach the final field of a genuine large QCoDeS Snapshot through Qt."""

    database_directory = tmp_path / "large-lazy-snapshot"
    database_directory.mkdir()
    database_path = database_directory / "live.db"
    writer = _prepare_live_database(database_path, "lazy_snapshot")
    experiment = load_or_create_experiment(
        "lazy_snapshot_experiment",
        sample_name="lazy_snapshot_sample",
        conn=writer,
    )
    parameter_names = [f"p{index:03d}" for index in range(579)]
    parameter_names.append("lazy_final_parameter")
    station_parameters = [ManualParameter(name) for name in parameter_names]
    station = Station(*station_parameters)
    measurement = Measurement(
        exp=experiment,
        name="large_lazy_snapshot_run",
        station=station,
    )
    measurement.write_period = 3_600
    measurement.register_parameter(station_parameters[0])
    context = measurement.run(write_in_background=False)
    datasaver = context.__enter__()
    datasaver.add_result((station_parameters[0], 0.0))
    datasaver.flush_data_to_database(block=True)
    dataset = datasaver.dataset
    guid = str(dataset.guid)

    snapshot_row = writer.execute(
        'SELECT "snapshot" FROM "runs" WHERE "run_id" = ?',
        (dataset.run_id,),
    ).fetchone()
    assert snapshot_row is not None
    stored_snapshot = snapshot_row[0]
    assert isinstance(stored_snapshot, str)
    assert 138 * 1024 <= len(stored_snapshot.encode("utf-8")) <= 150 * 1024
    assert '"lazy_final_parameter"' in stored_snapshot
    assert (
        stored_snapshot.count('"full_name"') + stored_snapshot.count('"raw_value"')
        >= 2 * len(parameter_names)
        > 1_024
    )
    protected_before_reader = _stable_artifact_state(
        database_path,
        consecutive_observations=2,
        observation_interval=0.02,
    )

    qplot_home = tmp_path / ".qplot-large-lazy-snapshot"
    cache_root = tmp_path / "large-lazy-snapshot-cache"
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

    errors: list[tuple[object, ...]] = []
    logged_errors: list[tuple[object, ...]] = []
    window = None
    try:
        with (
            patch.object(
                main_window.MainWindow,
                "show_error",
                side_effect=lambda _owner, title, message, details=None: errors.append(
                    (title, message, details)
                ),
            ),
            patch.object(
                database_actions,
                "log_exception",
                side_effect=lambda label, error, *_args, **_kwargs: (
                    logged_errors.append((label, error))
                ),
            ),
        ):
            window = main_window.MainWindow()
            window.startupDatabaseTimer.stop()
            window.monitor.stop()
            window.config.config["user_preference"]["confirm_close"] = False
            window.config.config["user_preference"]["confirm_close_all"] = False
            window.resize(900, 600)
            window.show()

            window.close_database(status=False)
            assert window.load_database_path(str(database_path))
            _process_until(lambda: not window._database_load_active)
            window.monitor.stop()
            assert window._database_access_mode == database_actions.TRUSTED_LIVE_MODE, (
                window._database_fallback_reason,
                errors,
            )
            item = window.RunList._item_for_guid(guid)
            assert item is not None
            window.RunList.clearSelection()
            window.RunList.setCurrentItem(item)
            item.setSelected(True)
            _process_until(lambda: window._selected_run_guid == guid)

            bridge = window._trusted_derived_bridge
            _process_until(
                lambda: (
                    bridge._detail_display_guid == guid
                    and bridge._selected_detail_publication is not None
                    and bridge._snapshot_source is not None
                ),
                timeout=60.0,
            )
            coordinator = bridge.coordinator
            assert coordinator is not None
            _process_until(
                lambda: (
                    not coordinator.active
                    and coordinator.snapshot().pending_count == 0
                    and not bridge.background_active()
                ),
                timeout=60.0,
            )
            publication = bridge._selected_detail_publication
            assert publication is not None
            snapshot = publication.detail.snapshot
            assert snapshot.status == "available"
            assert "loaded on demand" in snapshot.message
            assert snapshot.source is None

            snapshot_tree = window.infoBox.snapshot
            initial_items = tuple(_tree_items(snapshot_tree))
            assert 0 < len(initial_items) <= 128
            assert not any(
                tree_item.text(0) in {"[truncated]", "Snapshot unavailable"}
                for tree_item in initial_items
            )
            station_item = _tree_item_for_path(snapshot_tree, "/Snapshot/station")
            assert station_item.isExpanded()
            _process_until(lambda: station_item.childCount() > 0)
            parameters_path = "/Snapshot/station/parameters"
            parameters_item = _tree_item_for_path(snapshot_tree, parameters_path)
            assert parameters_item.childCount() == 0
            parameters_item.setExpanded(True)
            _process_until(lambda: parameters_item.childCount() > 0)
            assert parameters_item.childCount() <= 128

            protected_after_initial_reader = _stable_artifact_state(
                database_path,
                consecutive_observations=2,
                observation_interval=0.02,
            )
            _assert_protected_artifacts_unchanged(
                protected_before_reader,
                protected_after_initial_reader,
            )

            committed_value = 1
            while True:
                parameters_item = _tree_item_for_path(snapshot_tree, parameters_path)
                load_more = next(
                    (
                        parameters_item.child(index)
                        for index in range(parameters_item.childCount())
                        if parameters_item.child(index).text(0) == "Load more…"
                    ),
                    None,
                )
                if load_more is None:
                    break
                prior_count = parameters_item.childCount()
                before_page = _stable_artifact_state(
                    database_path,
                    consecutive_observations=2,
                    observation_interval=0.02,
                )
                snapshot_tree.itemActivated.emit(load_more, 0)
                _process_until(
                    lambda expected_count=prior_count: (
                        _tree_item_for_path(
                            snapshot_tree,
                            parameters_path,
                        ).childCount()
                        > expected_count
                    )
                )
                _process_until(lambda: not bridge.background_active(), timeout=30.0)
                parameters_item = _tree_item_for_path(snapshot_tree, parameters_path)
                after_page = _stable_artifact_state(
                    database_path,
                    consecutive_observations=2,
                    observation_interval=0.02,
                )
                _assert_protected_artifacts_unchanged(before_page, after_page)
                assert 0 < parameters_item.childCount() - prior_count <= 127

                # The QCoDeS owner remains able to commit and checkpoint between
                # independently bounded in-memory Snapshot page requests.
                datasaver.add_result((station_parameters[0], float(committed_value)))
                committed_value += 1
                datasaver.flush_data_to_database(block=True)
                assert (
                    writer.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()[0] == 0
                )
                assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (
                    0,
                    0,
                    0,
                )

            parameters_item = _tree_item_for_path(snapshot_tree, parameters_path)
            loaded_names = [
                parameters_item.child(index).text(0)
                for index in range(parameters_item.childCount())
            ]
            assert loaded_names == parameter_names
            assert len(loaded_names) == len(set(loaded_names)) == 580

            final_item = _tree_item_for_path(
                snapshot_tree,
                "/Snapshot/station/parameters/lazy_final_parameter",
            )
            before_final_page = _stable_artifact_state(
                database_path,
                consecutive_observations=2,
                observation_interval=0.02,
            )
            final_item.setExpanded(True)
            _process_until(lambda: final_item.childCount() > 0)
            _process_until(lambda: not bridge.background_active(), timeout=30.0)
            final_name = _tree_item_for_path(
                snapshot_tree,
                "/Snapshot/station/parameters/lazy_final_parameter/name",
            )
            assert final_name.text(1) == "lazy_final_parameter"
            protected_after_final_page = _stable_artifact_state(
                database_path,
                consecutive_observations=2,
                observation_interval=0.02,
            )
            _assert_protected_artifacts_unchanged(
                before_final_page,
                protected_after_final_page,
            )
            assert not any(
                tree_item.text(0) == "[truncated]"
                for tree_item in _tree_items(snapshot_tree)
            )
            assert errors == []
            assert logged_errors == []
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
        context.__exit__(None, None, None)
        assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
        writer.close()
