"""Real-WAL MainWindow acceptance for paging large QCoDeS snapshots."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from qcodes import Station
from qcodes.dataset import (
    Measurement,
    load_or_create_experiment,
)
from qcodes.parameters import ManualParameter

from qplot.datahandling import trusted_work_coordinator as coordinator_module
from qplot.datahandling.trusted_derived_cache import TrustedDerivedDiskCache
from qplot.windows import _database_actions as database_actions
from qplot.windows import main as main_window
from tests._window_lifecycle import close_main_window
from tests.datahandling.test_trusted_live import (
    _assert_protected_artifacts_unchanged,
    _stable_artifact_state,
)
from tests.windows._trusted_derived_wal_ui import (
    _prepare_live_database,
    _process_until,
    _tree_item_for_path,
    _tree_items,
)

pytestmark = pytest.mark.timeout(180)


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
