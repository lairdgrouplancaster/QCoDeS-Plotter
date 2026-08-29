"""Production MainWindow acceptance for the Stage 5C real-WAL path."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest
from PyQt6 import QtCore, QtWidgets
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.dataset.sqlite.database import connect
from qcodes.parameters import ManualParameter

from qplot.datahandling import trusted_work_coordinator as coordinator_module
from qplot.datahandling.trusted_derived_cache import TrustedDerivedDiskCache
from qplot.testdata import (
    RunSpecification,
    enable_generation_provenance_for_writer,
    generate_database,
)
from qplot.windows import _database_actions as database_actions
from qplot.windows import main as main_window
from qplot.windows._widgets import preview as preview_module
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
        time.sleep(0.005)
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


def _start_live_run(writer, name: str):
    experiment = load_or_create_experiment(
        f"{name}_experiment",
        sample_name=f"{name}_sample",
        conn=writer,
    )
    setpoint = ManualParameter(f"{name}_setpoint")
    signal = ManualParameter(f"{name}_signal")
    measurement = Measurement(exp=experiment, name=f"{name}_run")
    measurement.write_period = 0.001
    measurement.register_parameter(setpoint)
    measurement.register_parameter(signal, setpoints=(setpoint,))
    context = measurement.run(write_in_background=False)
    datasaver = context.__enter__()
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
    first_run = _start_live_run(first_writer, "first_live")
    second_run = _start_live_run(second_writer, "second_live")
    first_context = first_run[3]
    second_context = second_run[3]
    latest_first_run = None
    window = None
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
            prior_preview = window.infoBox.preview.cache[first_guid]
            window._trusted_derived_bridge.update_preview_size(window.preview_size + 17)
            _process_until(
                lambda: (
                    window.infoBox.preview.cache.get(first_guid) is not prior_preview
                )
            )
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
            generation = coordinator.snapshot().generation
            supervisor = window._trusted_read_service._required_supervisor()
            prior_incarnation = supervisor.incarnation
            prior_metadata = bridge._metadata_by_guid[first_guid]
            supervisor.restart()
            assert supervisor.incarnation > prior_incarnation
            bridge.helper_restarted()
            assert coordinator.snapshot().generation > generation
            _process_until(
                lambda: bridge._metadata_by_guid.get(first_guid) is not prior_metadata
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
                    and bridge._database_instance.logical_path == str(second_path)
                )
            )
            assert first_guid not in bridge._metadata_by_guid
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
