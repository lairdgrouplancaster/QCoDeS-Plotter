import csv
import gc
import hashlib
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
import qcodes
from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_by_id,
    load_or_create_experiment,
)
from qcodes.dataset.sqlite.database import connect
from qcodes.parameters import ManualParameter

import qplot.tools.worker as worker_module
from qplot.configuration.config import config
from qplot.datahandling import database as database_module
from qplot.datahandling import readonly as readonly_module
from qplot.datahandling.file_identity import (
    canonical_database_path,
    database_instance,
    logical_database_path,
)
from qplot.datahandling.qcodes_cache import (
    cache_parameter_data,
    cache_parameter_is_synchronized,
)
from qplot.datahandling.readonly import (
    replacement_wal_is_quarantined,
    sqlite_read_only_connection,
)
from qplot.testdata import (
    CSV_COLUMNS,
    GenerationCancelled,
    RunSpecification,
    enable_generation_provenance_for_writer,
    generate_database,
)
from qplot.windows import _database_actions as database_actions_module
from qplot.windows import _plot_actions as plot_actions_module
from qplot.windows import main as main_window
from qplot.windows._dataset_handle import database_file_identity


def configure_temp_qplot(monkeypatch, tmp_path):
    qplot_home = tmp_path / ".qplot"
    monkeypatch.setattr(config, "default_path", str(qplot_home))
    monkeypatch.setattr(config, "default_file", str(qplot_home / config.config_file_name))


def wait_for(predicate, timeout=12):
    app = qtw.QApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            app.processEvents()
            return
        time.sleep(0.03)
    raise AssertionError("Timed out waiting for GUI integration state")


def build_synthetic_database(db_path):
    initialise_or_create_database_at(str(db_path), journal_mode="DELETE")
    experiment = load_or_create_experiment("qplot_integration", sample_name="synthetic")

    gate = ManualParameter("gate", label="Gate voltage", unit="V")
    bias = ManualParameter("bias", label="Bias voltage", unit="mV")
    current = ManualParameter("current", label="Current", unit="nA")
    conductance = ManualParameter("conductance", label="Conductance", unit="uS")

    line_meas = Measurement(exp=experiment, name="line_integration")
    line_meas.register_parameter(gate)
    line_meas.register_parameter(current, setpoints=(gate,))
    with line_meas.run() as datasaver:
        for gate_value in np.linspace(-1.5, 1.5, 11):
            datasaver.add_result(
                (gate, float(gate_value)),
                (current, float(np.sin(gate_value) * 20.0)),
            )
        line_run_id = datasaver.dataset.run_id

    heatmap_meas = Measurement(exp=experiment, name="heatmap_integration")
    heatmap_meas.register_parameter(gate)
    heatmap_meas.register_parameter(bias)
    heatmap_meas.register_parameter(conductance, setpoints=(gate, bias))
    with heatmap_meas.run() as datasaver:
        for gate_value in np.linspace(-1.0, 1.0, 7):
            for bias_value in np.linspace(-0.6, 0.6, 5):
                value = np.cos(gate_value * 2.0) + np.sin(bias_value * 3.0)
                datasaver.add_result(
                    (gate, float(gate_value)),
                    (bias, float(bias_value)),
                    (conductance, float(value)),
                )
        heatmap_run_id = datasaver.dataset.run_id

    return line_run_id, heatmap_run_id


def build_line_database(db_path, point_count, *, guid=None, journal_mode=None):
    selected_journal_mode = "DELETE" if journal_mode is None else journal_mode
    initialise_or_create_database_at(
        str(db_path),
        journal_mode=selected_journal_mode,
    )
    experiment = load_or_create_experiment(
        f"qplot_replace_{db_path.stem}",
        sample_name="replacement",
    )
    gate = ManualParameter("gate", label="Gate voltage", unit="V")
    signal = ManualParameter("signal", label="Signal", unit="nA")
    measurement = Measurement(exp=experiment, name="replaceable_run")
    measurement.register_parameter(gate)
    measurement.register_parameter(signal, setpoints=(gate,))
    with measurement.run(write_in_background=False) as datasaver:
        for index in range(point_count):
            datasaver.add_result((gate, float(index)), (signal, float(index + 10)))
        dataset = datasaver.dataset
        run_id = dataset.run_id
        generated_guid = dataset.guid
        table_name = dataset.table_name
    dataset.conn.close()

    if guid is not None and guid != generated_guid:
        connection = sqlite3.connect(db_path)
        try:
            connection.execute(
                "UPDATE runs SET guid = ? WHERE run_id = ?",
                (guid, run_id),
            )
            connection.commit()
        finally:
            connection.close()
        generated_guid = guid

    return run_id, generated_guid, table_name


def build_line_database_with_replayed_stale_wal(db_path):
    """Create a generated run with a proven, safely replayable old WAL.

    The generated main's token and advanced WAL epoch make the initial pairing
    safe to load. The main and sidecars are copied while a raw SQLite writer
    owns them and restored only after it closes. This gives Windows the same
    unpaired-WAL replacement scenario as POSIX without trying to rename a file
    that SQLite has open, while preserving qPlot's first-load trust policy.
    """

    generate_database(
        [
            RunSpecification(
                1,
                "signal",
                "Signal",
                "nA",
                0.0,
                0.0,
                1,
            )
        ],
        db_path,
    )
    connection = sqlite3.connect(db_path)
    try:
        database_run = connection.execute(
            "SELECT run_id, guid, result_table_name FROM runs"
        ).fetchone()
    finally:
        connection.close()

    parked = {}
    writer = sqlite3.connect(db_path)
    try:
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("UPDATE runs SET name = ?", ("stale_sidecar_run",))
        writer.commit()
        parked_main = Path(f"{db_path}.parked-main")
        shutil.copyfile(db_path, parked_main)
        for suffix in ("-wal", "-shm"):
            source = Path(f"{db_path}{suffix}")
            assert source.is_file()
            parked_path = Path(f"{db_path}.parked{suffix}")
            shutil.copyfile(source, parked_path)
            parked[suffix] = parked_path
    finally:
        writer.close()

    shutil.copyfile(parked_main, db_path)
    parked_main.unlink()
    for suffix, parked_path in parked.items():
        shutil.copyfile(parked_path, f"{db_path}{suffix}")
        parked_path.unlink()

    return database_run


def prepare_generated_database_for_live_writes(db_path):
    """Install qPlot lineage before a test starts a real live WAL writer."""
    generate_database(
        [
            RunSpecification(
                1,
                "lineage_seed",
                "Lineage seed",
                "V",
                0.0,
                0.0,
                1,
            )
        ],
        db_path,
    )


def dependent_parameter(dataset, dimensions):
    for param in dataset.get_parameters():
        if param.depends_on and len(param.depends_on_) == dimensions:
            return param
    raise AssertionError(f"No {dimensions}D dependent parameter in run {dataset.run_id}")


def close_main_window(window):
    """Stop workers and close owned datasets before deleting Qt state."""

    window.startupDatabaseTimer.stop()
    window.monitor.stop()
    window.infoBox.preview.shutdown()
    window.close_plot_windows(confirm=False, status=False)
    for worker_name in (
        "_database_load_worker",
        "_database_detail_worker",
        "_database_expensive_detail_worker",
        "_database_refresh_worker",
        "_test_database_generation_worker",
    ):
        worker = getattr(window, worker_name, None)
        cancel = getattr(worker, "cancel", None)
        if callable(cancel):
            cancel()
    window._cancel_plot_work()
    for pool_name in (
        "threadPool",
        "databaseLoadThreadPool",
        "databaseDetailThreadPool",
        "databaseExpensiveDetailThreadPool",
        "databaseRefreshThreadPool",
        "testDatabaseGenerationThreadPool",
    ):
        assert getattr(window, pool_name).waitForDone(12_000)
    qtw.QApplication.processEvents()
    window.close_database(status=False)
    assert window.ds is None
    assert window.dataset_holder == {}
    window.hide()
    window.deleteLater()
    qtw.QApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    qtw.QApplication.processEvents()


def database_artifact_state(database_path):
    """Record content and metadata for the database and SQLite sidecars."""

    state = {}
    for suffix in ("", "-wal", "-shm", "-journal"):
        path = Path(f"{database_path}{suffix}")
        if not path.exists():
            state[suffix] = None
            continue
        stat = path.stat()
        state[suffix] = (
            stat.st_size,
            stat.st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
            )
    return state


def database_artifact_bytes_and_mtimes(database_path):
    """Record exact source bytes and mtimes for the main and all sidecars."""

    state = {}
    for suffix in ("", "-wal", "-shm", "-journal"):
        path = Path(f"{database_path}{suffix}")
        if not path.exists():
            state[suffix] = None
            continue
        state[suffix] = (path.read_bytes(), path.stat().st_mtime_ns)
    return state


def write_small_generation_specification(csv_path, *, point_count=5):
    with Path(csv_path).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        writer.writerow(
            (
                "1",
                "replacement",
                "Replacement",
                "V",
                "-1",
                "1",
                str(point_count),
                "",
                "",
                "",
            )
        )


def table_contents(table):
    """Return stable visible text from one details table."""

    return tuple(
        tuple(
            table.item(row, column).text()
            if table.item(row, column) is not None
            else ""
            for column in range(table.columnCount())
        )
        for row in range(table.rowCount())
    )


def run_list_contents(run_list):
    """Return stable text and identities from the visible run list."""

    rows = []
    for index in range(run_list.topLevelItemCount()):
        item = run_list.topLevelItem(index)
        assert item is not None
        rows.append(
            (
                tuple(item.text(column) for column in range(run_list.columnCount())),
                getattr(item, "guid", None),
            )
        )
    return tuple(rows)


def generation_database_view(window):
    """Capture the user-visible state that tentative replacement must restore."""

    preview = window.infoBox.preview
    control_names = (
        "loadDatabaseButton",
        "refreshDatabaseButton",
        "databaseInfoButton",
        "openDatabaseFolderButton",
    )
    return {
        "run_list": run_list_contents(window.RunList),
        "watching_guids": tuple(
            getattr(item, "guid", None)
            for item in window.RunList.watching
        ),
        "selected_run_id": window.selected_run_id,
        "selected_guid": getattr(window.ds, "guid", None),
        "selected_dataset_name": getattr(window.ds, "name", None),
        "run_id_text": window.run_idBox.text(),
        "details_overview": table_contents(window.infoBox.overview),
        "details_parameters": table_contents(window.infoBox.parameters),
        "preview_database": preview.database_path,
        "preview_current_guid": preview.current_guid,
        "preview_cached_guids": tuple(sorted(preview.cache)),
        "generation_action_enabled": window.generateTestDatabaseAction.isEnabled(),
        "database_controls_enabled": tuple(
            getattr(window, name).isEnabled()
            for name in control_names
        ),
        "monitor_active": window.monitor.isActive(),
        "monitor_interval": window.monitor.interval(),
    }


def select_nondefault_run_and_finish_previews(window):
    """Select a non-default run so a fallback-to-first reload is observable."""

    run_ids = sorted(window.RunList.all_run_metadata())
    assert len(run_ids) >= 2
    selected_run_id = window.selected_run_id
    target_run_id = next(run_id for run_id in run_ids if run_id != selected_run_id)
    matches = window.RunList.findItems(
        str(target_run_id),
        QtCore.Qt.MatchFlag.MatchExactly,
        0,
    )
    assert len(matches) == 1
    window.RunList.setCurrentItem(matches[0])
    wait_for(
        lambda: (
            window.selected_run_id == target_run_id
            and window.ds is not None
            and window.infoBox.preview.current_guid == window.ds.guid
        )
    )
    wait_for(
        lambda: (
            not window.infoBox.preview._workers
            and not window.infoBox.preview.queue
            and not window.infoBox.preview.active
        )
    )
    return target_run_id


def qplot_run_name(database_path, run_id):
    connection = sqlite_read_only_connection(database_path)
    try:
        return connection.execute(
            "SELECT name FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0]
    finally:
        connection.close()


def immutable_run_name(database_path, run_id):
    connection = sqlite3.connect(
        f"{Path(database_path).resolve().as_uri()}?mode=ro&immutable=1",
        uri=True,
    )
    try:
        return connection.execute(
            "SELECT name FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0]
    finally:
        connection.close()


def configure_generation_dialogs(monkeypatch, csv_path, database_path):
    monkeypatch.setattr(
        qtw.QFileDialog,
        "getOpenFileName",
        lambda *_args, **_kwargs: (str(csv_path), "CSV Files (*.csv)"),
    )
    monkeypatch.setattr(
        qtw.QFileDialog,
        "getSaveFileName",
        lambda *_args, **_kwargs: (
            str(database_path),
            "QCoDeS Database (*.db)",
        ),
    )
    monkeypatch.setattr(
        qtw.QMessageBox,
        "question",
        lambda *_args, **_kwargs: qtw.QMessageBox.StandardButton.Yes,
    )


def symlink_source_artifact_state(view_path, *target_paths):
    """Record a symlink entry and every possible database target/sidecar."""

    link_stat = view_path.lstat()
    return {
        "link": (
            os.readlink(view_path),
            link_stat.st_mode,
            link_stat.st_size,
            link_stat.st_mtime_ns,
        ),
        "targets": {
            str(path): database_artifact_state(path)
            for path in target_paths
            if path.exists()
        },
    }


def release_windows_database_locks(window, database_path, *extra_datasets):
    """Release test-only SQLite handles before a Windows atomic replacement.

    POSIX lets a test rename an open database while Windows correctly rejects
    it. qPlot detects a replacement before reading these stale objects, so the
    test can close their backing connections without changing the state under
    test or marking their DatasetHandles closed ahead of the invalidation.
    """

    if os.name != "nt":
        return

    source_path = Path(database_path).resolve()
    datasets = list(extra_datasets)
    selected_key = getattr(window, "_selected_dataset_key", None)
    if (
            selected_key is not None
            and (
                Path(selected_key.database_path).resolve() == source_path
                or Path(selected_key.resolved_database_path) == source_path
            )
            ):
        datasets.append(getattr(window, "ds", None))
    for dataset_key, handle in getattr(window, "dataset_holder", {}).items():
        if (
                Path(dataset_key.database_path).resolve() == source_path
                or Path(dataset_key.resolved_database_path) == source_path
                ):
            datasets.append(handle.dataset)

    closed_connections = set()
    for dataset in datasets:
        connection = getattr(dataset, "conn", dataset)
        close = getattr(connection, "close", None)
        if not callable(close) or id(connection) in closed_connections:
            continue
        close()
        closed_connections.add(id(connection))

    qtw.QApplication.processEvents()
    gc.collect()


def assert_same_path_generation_consumers_released(window):
    """Assert that no database-reading GUI consumer survived the release."""
    assert window.databaseLoadThreadPool.activeThreadCount() == 0
    assert window.databaseDetailThreadPool.activeThreadCount() == 0
    assert window.databaseExpensiveDetailThreadPool.activeThreadCount() == 0
    assert window.databaseRefreshThreadPool.activeThreadCount() == 0
    assert window.threadPool.activeThreadCount() == 0
    assert window._database_load_worker is None
    assert window._database_detail_worker is None
    assert window._database_expensive_detail_worker is None
    assert window._database_refresh_worker is None
    assert not window._plot_workers
    assert window.dataset_holder == {}
    assert window.ds is None
    assert window._selected_dataset_key is None
    assert window.selected_run_id is None
    assert window.windows == []
    assert window.RunList.topLevelItemCount() == 0
    preview = window.infoBox.preview
    assert preview._workers == {}
    assert preview.queue == {}
    assert preview.active == set()
    assert not window.monitor.isActive()


def test_main_window_opens_real_1d_and_2d_plots(tmp_path, monkeypatch):
    configure_temp_qplot(monkeypatch, tmp_path)
    database_path = Path(tmp_path) / "qplot-integration.db"
    line_run_id, heatmap_run_id = build_synthetic_database(database_path)

    window = main_window.MainWindow()
    try:
        window.startupDatabaseTimer.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.close_database(status=False)

        assert window.load_file(str(database_path))
        wait_for(
            lambda: (
                not window._database_load_active
                and window.RunList.topLevelItemCount() >= 2
            )
        )

        line_dataset = load_by_id(line_run_id)
        line_param = dependent_parameter(line_dataset, 1)
        window.ds = line_dataset
        window.openPlot(params=[line_param], show=True)
        line_window = window.windows[-1]
        wait_for(
            lambda: (
                hasattr(line_window, "axis_data")
                and not getattr(line_window.worker, "running", False)
            )
        )

        assert line_window.axis_data["x"].size == 11
        assert line_window.axis_data["y"].size == 11
        assert np.isfinite(line_window.axis_data["y"]).all()
        assert isinstance(window.x(), int)
        assert isinstance(window.y(), int)
        assert line_window.layout() is not None
        assert line_window.width() > 0
        assert line_window.height() > 0

        heatmap_dataset = load_by_id(heatmap_run_id)
        heatmap_param = dependent_parameter(heatmap_dataset, 2)
        window.ds = heatmap_dataset
        window.openPlot(params=[heatmap_param], show=False)
        heatmap_window = window.windows[-1]
        wait_for(
            lambda: (
                hasattr(heatmap_window, "dataGrid")
                and not getattr(heatmap_window.worker, "running", False)
            )
        )

        assert heatmap_window.dataGrid.shape == (7, 5)
        assert np.isfinite(heatmap_window.dataGrid).all()

        source_x = np.asarray(heatmap_window.axis_data["x"]).copy()
        source_y = np.asarray(heatmap_window.axis_data["y"]).copy()
        source_grid = np.asarray(heatmap_window.dataGrid).copy()

        heatmap_window.z_index = [2, 1]
        heatmap_window.openSweep("h")
        horizontal_cut = window.windows[-1]
        wait_for(lambda: not getattr(horizontal_cut.worker, "running", False))

        np.testing.assert_array_equal(horizontal_cut.axis_data["x"], source_x)
        np.testing.assert_array_equal(horizontal_cut.axis_data["y"], source_grid[1, :])
        assert horizontal_cut.sweep_id in heatmap_window.sweep_lines

        heatmap_window.z_index = [2, 1]
        heatmap_window.openSweep("v")
        vertical_cut = window.windows[-1]
        wait_for(lambda: not getattr(vertical_cut.worker, "running", False))

        np.testing.assert_array_equal(vertical_cut.axis_data["x"], source_y)
        np.testing.assert_array_equal(vertical_cut.axis_data["y"], source_grid[:, 2])
        assert vertical_cut.sweep_id in heatmap_window.sweep_lines
    finally:
        close_main_window(window)


def test_main_window_close_releases_private_wal_snapshot_before_qt_cleanup(
    tmp_path,
    monkeypatch,
):
    configure_temp_qplot(monkeypatch, tmp_path)
    original_database_path = qcodes.config.core.db_location
    database_path = Path(tmp_path) / "close-private-snapshot.db"
    build_line_database_with_replayed_stale_wal(database_path)
    source_artifacts = database_artifact_state(database_path)
    window = main_window.MainWindow()
    closed = False

    try:
        window.startupDatabaseTimer.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False

        assert window.load_file(str(database_path))
        wait_for(
            lambda: (
                not window._database_load_active
                and not window._database_detail_active
                and not window._database_expensive_detail_active
                and window.ds is not None
            )
        )
        window.monitor.stop()

        dataset = window.ds
        connection = dataset.conn
        snapshot_path = Path(
            connection.execute("PRAGMA database_list").fetchone()[2]
        )
        snapshot_directory = snapshot_path.parent
        assert snapshot_directory.name.startswith("qplot-readonly-")
        assert snapshot_directory.is_dir()

        close_main_window(window)
        closed = True

        with pytest.raises((sqlite3.ProgrammingError, RuntimeError)):
            connection.cursor()
        assert not snapshot_directory.exists()
        assert database_artifact_state(database_path) == source_artifacts
    finally:
        if not closed:
            close_main_window(window)
        qcodes.config.core.db_location = original_database_path


def test_atomic_replacement_reloads_every_real_qcodes_runtime_object(
    tmp_path,
    monkeypatch,
):
    configure_temp_qplot(monkeypatch, tmp_path)
    original_database_path = qcodes.config.core.db_location
    database_path = Path(tmp_path) / "loaded.db"
    replacement_path = Path(tmp_path) / "replacement.db"
    _run_id, guid, table_name = build_line_database(database_path, 3)
    _replacement_run_id, replacement_guid, replacement_table = build_line_database(
        replacement_path,
        4,
        guid=guid,
    )
    assert replacement_guid == guid
    assert replacement_table == table_name

    window = main_window.MainWindow()
    refresh_sql_finished = threading.Event()
    release_refresh = threading.Event()
    status_messages = []
    original_show_status = window.show_status
    original_get_run_status = database_module.get_run_status
    replacement_loads = []
    original_load_file = window.load_file
    snapshot_connections = []
    original_attach_snapshot_cleanup = readonly_module._attach_snapshot_cleanup

    def record_snapshot_connection(connection, snapshot):
        snapshot_directory = Path(snapshot.name)
        original_attach_snapshot_cleanup(connection, snapshot)
        snapshot_connections.append((connection, snapshot_directory))

    def record_status(message, timeout=5000):
        status_messages.append((message, timeout))
        return original_show_status(message, timeout)

    def block_completed_status(*args, **kwargs):
        status = original_get_run_status(*args, **kwargs)
        refresh_sql_finished.set()
        if not release_refresh.wait(10):
            raise TimeoutError("Test did not release the database refresh worker")
        return status

    def record_load(*args, **kwargs):
        replacement_loads.append((args, kwargs))
        return original_load_file(*args, **kwargs)

    try:
        monkeypatch.setattr(
            readonly_module,
            "_attach_snapshot_cleanup",
            record_snapshot_connection,
        )
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.close_database(status=False)
        window.show_status = record_status

        assert window.load_file(str(database_path))
        wait_for(
            lambda: (
                not window._database_load_active
                and not window._database_detail_active
                and not window._database_expensive_detail_active
            )
        )
        window.monitor.stop()
        assert window.ds is not None
        assert window.ds.guid == guid
        assert window.ds.number_of_results == 3

        parameter = dependent_parameter(window.ds, 1)
        window.openPlot(params=[parameter], show=False)
        old_plot = window.windows[-1]
        wait_for(lambda: not getattr(old_plot.worker, "running", False))
        old_key = old_plot._dataset_key
        old_handle = window.dataset_holder[old_key]
        old_dataset = old_handle.dataset
        assert old_dataset.number_of_results == 3
        assert old_key.database_identity == database_file_identity(database_path)

        stale_preview = [{"stale": True}]
        window.infoBox.preview.cache[guid] = stale_preview
        window.infoBox._setpoint_summary_cache["stale"] = "old instance"
        window.RunList.watching = [SimpleNamespace(guid=guid)]
        window.load_file = record_load

        monkeypatch.setattr(
            database_module,
            "get_run_status",
            block_completed_status,
        )
        window.refreshMain()
        assert refresh_sql_finished.wait(10)
        window.refreshMain()
        assert window._database_refresh_pending

        release_windows_database_locks(window, database_path)
        os.replace(replacement_path, database_path)
        replacement_artifacts = database_artifact_state(database_path)
        release_refresh.set()

        wait_for(
            lambda: (
                not window._database_load_active
                and not window._database_refresh_active
                and not window._database_detail_active
                and not window._database_expensive_detail_active
                and window._loaded_database_identity
                == database_file_identity(database_path)
            )
        )
        window.monitor.stop()

        runs = window.RunList.all_run_metadata()
        replacement_metadata = next(
            metadata for metadata in runs.values() if metadata["guid"] == guid
        )
        assert replacement_metadata["result_count"] == 4
        assert window.infoBox.preview.run_metadata[guid]["result_count"] == 4
        assert window.infoBox.preview.cache.get(guid) != stale_preview
        assert "stale" not in window.infoBox._setpoint_summary_cache
        assert old_plot not in window.windows
        assert old_handle.closed
        assert old_key not in window.dataset_holder
        with pytest.raises((sqlite3.ProgrammingError, RuntimeError)):
            old_dataset.conn.cursor()

        new_key = window._current_dataset_key(guid)
        assert new_key != old_key
        with pytest.raises(RuntimeError, match="replaced"):
            window._dataset_for_key(old_key)

        assert window.ds is not None
        assert window.ds.guid == guid
        assert window.ds.number_of_results == 4
        new_parameter = dependent_parameter(window.ds, 1)
        window.openPlot(params=[new_parameter], show=False)
        new_plot = window.windows[-1]
        wait_for(lambda: not getattr(new_plot.worker, "running", False))
        new_handle = window.dataset_holder[new_plot._dataset_key]
        assert new_handle is not old_handle
        assert new_handle.database_identity == database_file_identity(database_path)
        assert new_handle.dataset.number_of_results == 4
        assert new_plot.axis_data["x"].size == 4
        assert new_plot.axis_data["y"].size == 4

        qtw.QApplication.processEvents()
        assert len(replacement_loads) == 1
        assert replacement_loads[0][1] == {"force": True, "replacement": True}
        assert any(
            "Database was replaced and reloaded" in message
            for message, _timeout in status_messages
        )
        assert database_artifact_state(database_path) == replacement_artifacts
    finally:
        release_refresh.set()
        window.load_file = original_load_file
        close_main_window(window)
        for connection, snapshot_directory in snapshot_connections:
            with pytest.raises((sqlite3.ProgrammingError, RuntimeError)):
                connection.cursor()
            assert not snapshot_directory.exists()
        qcodes.config.core.db_location = original_database_path


@pytest.mark.parametrize("replacement_kind", ["symlink_entry", "target_file"])
def test_symlinked_database_replacement_retires_the_accepted_instance(
    tmp_path,
    monkeypatch,
    replacement_kind,
):
    configure_temp_qplot(monkeypatch, tmp_path)
    original_database_path = qcodes.config.core.db_location
    target_a = Path(tmp_path) / "target-a.db"
    target_b = Path(tmp_path) / "target-b.db"
    view_path = Path(tmp_path) / "view.db"
    next_link = Path(tmp_path) / "next-view.db"
    _run_id, guid, _table_name = build_line_database(target_a, 3)
    _new_run_id, new_guid, _new_table_name = build_line_database(
        target_b,
        4,
        guid=guid,
    )
    assert new_guid == guid
    try:
        view_path.symlink_to(target_a)
    except OSError as error:
        pytest.skip(f"This platform cannot create the required symlink: {error}")

    window = main_window.MainWindow()
    original_load_file = window.load_file
    replacement_loads = []

    def record_load(*args, **kwargs):
        replacement_loads.append((args, kwargs))
        return original_load_file(*args, **kwargs)

    try:
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.close_database(status=False)
        assert window.load_file(str(view_path))
        wait_for(
            lambda: (
                not window._database_load_active
                and not window._database_detail_active
                and not window._database_expensive_detail_active
            )
        )
        window.monitor.stop()
        assert window.ds is not None
        assert window.ds.number_of_results == 3

        parameter = dependent_parameter(window.ds, 1)
        window.openPlot(params=[parameter], show=False)
        old_plot = window.windows[-1]
        wait_for(lambda: not getattr(old_plot.worker, "running", False))
        old_key = old_plot._dataset_key
        old_handle = window.dataset_holder[old_key]
        old_dataset = old_handle.dataset
        assert old_key.database_path == logical_database_path(view_path)
        assert old_key.resolved_database_path == canonical_database_path(target_a)
        assert old_plot.axis_data["y"].size == 3

        window.infoBox.preview.cache[guid] = [{"stale": True}]
        window.infoBox._setpoint_summary_cache["stale"] = "old instance"
        window.load_file = record_load

        if replacement_kind == "symlink_entry":
            next_link.symlink_to(target_b)
            os.replace(next_link, view_path)
            artifact_targets = (target_a, target_b)
        else:
            release_windows_database_locks(window, target_a)
            os.replace(target_b, target_a)
            artifact_targets = (target_a,)
        replacement_artifacts = symlink_source_artifact_state(
            view_path,
            *artifact_targets,
        )

        window.refreshMain()
        wait_for(
            lambda: (
                not window._database_load_active
                and not window._database_refresh_active
                and not window._database_detail_active
                and not window._database_expensive_detail_active
            )
        )
        window.monitor.stop()

        metadata = next(iter(window.RunList.all_run_metadata().values()))
        assert metadata["result_count"] == 4
        assert window.ds is not None
        assert window.ds.guid == guid
        assert window.ds.number_of_results == 4
        assert old_plot not in window.windows
        assert old_handle.closed
        assert old_key not in window.dataset_holder
        with pytest.raises((sqlite3.ProgrammingError, RuntimeError)):
            old_dataset.conn.cursor()
        assert window.infoBox.preview.cache.get(guid) != [{"stale": True}]
        assert "stale" not in window.infoBox._setpoint_summary_cache

        new_key = window._current_dataset_key(guid)
        assert new_key.database_path == logical_database_path(view_path)
        assert new_key.resolved_database_path == canonical_database_path(view_path)
        assert new_key != old_key
        new_parameter = dependent_parameter(window.ds, 1)
        window.openPlot(params=[new_parameter], show=False)
        new_plot = window.windows[-1]
        wait_for(lambda: not getattr(new_plot.worker, "running", False))
        assert new_plot.axis_data["x"].size == 4
        assert new_plot.axis_data["y"].size == 4

        qtw.QApplication.processEvents()
        assert len(replacement_loads) == 1
        assert replacement_loads[0][1] == {"force": True, "replacement": True}
        assert symlink_source_artifact_state(
            view_path,
            *artifact_targets,
        ) == replacement_artifacts
    finally:
        window.load_file = original_load_file
        close_main_window(window)
        qcodes.config.core.db_location = original_database_path


def test_atomic_replacement_of_live_wal_uses_new_main_without_source_writes(
    tmp_path,
    monkeypatch,
):
    configure_temp_qplot(monkeypatch, tmp_path)
    original_database_path = qcodes.config.core.db_location
    database_path = Path(tmp_path) / "live-replaced.db"
    replacement_path = Path(tmp_path) / "replacement.db"
    window = None

    try:
        build_line_database_with_replayed_stale_wal(database_path)
        assert Path(f"{database_path}-wal").is_file()
        assert Path(f"{database_path}-shm").is_file()

        window = main_window.MainWindow()
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.close_database(status=False)
        assert window.load_file(str(database_path))
        wait_for(
            lambda: (
                not window._database_load_active
                and not window._database_detail_active
                and not window._database_expensive_detail_active
            )
        )
        window.monitor.stop()
        assert window.ds is not None
        old_parameter = dependent_parameter(window.ds, 1)
        window.openPlot(params=[old_parameter], show=False)
        old_plot = window.windows[-1]
        wait_for(lambda: not getattr(old_plot.worker, "running", False))
        old_handle = window.dataset_holder[old_plot._dataset_key]
        assert old_plot.axis_data["y"].size == 1
        old_wal_identity = database_file_identity(f"{database_path}-wal")
        assert old_wal_identity is not None

        generate_database(
            [
                RunSpecification(
                    1,
                    "replacement_signal",
                    "Replacement signal",
                    "V",
                    -1.0,
                    1.0,
                    4,
                )
            ],
            replacement_path,
        )
        release_windows_database_locks(window, database_path)
        os.replace(replacement_path, database_path)
        replacement_artifacts = database_artifact_state(database_path)
        assert replacement_artifacts["-wal"] is not None
        assert replacement_artifacts["-shm"] is not None
        assert database_file_identity(f"{database_path}-wal") == old_wal_identity

        # Deliberately take a plot action before MainWindow's refresh timer
        # can observe the replacement. It must start the replacement reload
        # without opening the new main together with the old WAL.
        window.show_error = lambda *_args, **_kwargs: None
        direct_dataset_reads = []
        original_loader = plot_actions_module.load_by_guid_read_only

        def record_direct_dataset_read(*args, **kwargs):
            direct_dataset_reads.append((args, kwargs))
            return original_loader(*args, **kwargs)

        with monkeypatch.context() as scoped_monkeypatch:
            scoped_monkeypatch.setattr(
                plot_actions_module,
                "load_by_guid_read_only",
                record_direct_dataset_read,
            )
            window.openPlot(
                guid=old_plot._dataset_key,
                params=[old_parameter],
                show=False,
            )
            assert direct_dataset_reads == []

        wait_for(
            lambda: (
                not window._database_load_active
                and not window._database_refresh_active
                and not window._database_detail_active
                and not window._database_expensive_detail_active
            )
        )
        window.monitor.stop()

        metadata = next(iter(window.RunList.all_run_metadata().values()))
        assert metadata["result_count"] == 4
        assert old_plot not in window.windows
        assert old_handle.closed
        assert window.ds is not None
        new_parameter = dependent_parameter(window.ds, 1)
        window.openPlot(params=[new_parameter], show=False)
        new_plot = window.windows[-1]
        wait_for(lambda: not getattr(new_plot.worker, "running", False))
        assert new_plot.axis_data["x"].size == 4
        assert new_plot.axis_data["y"].size == 4
        assert database_artifact_state(database_path) == replacement_artifacts
    finally:
        if window is not None:
            close_main_window(window)
        qcodes.config.core.db_location = original_database_path


def test_existing_live_plot_refresh_quarantines_replaced_wal_before_worker_read(
    tmp_path,
    monkeypatch,
):
    configure_temp_qplot(monkeypatch, tmp_path)
    original_database_path = qcodes.config.core.db_location
    database_path = Path(tmp_path) / "live-refresh-replaced.db"
    replacement_path = Path(tmp_path) / "live-refresh-replacement.db"
    window = None

    try:
        build_line_database_with_replayed_stale_wal(database_path)
        assert Path(f"{database_path}-wal").is_file()

        window = main_window.MainWindow()
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.close_database(status=False)
        assert window.load_file(str(database_path))
        wait_for(
            lambda: (
                not window._database_load_active
                and not window._database_detail_active
                and not window._database_expensive_detail_active
            )
        )
        window.monitor.stop()
        old_parameter = dependent_parameter(window.ds, 1)
        window.openPlot(params=[old_parameter], show=False)
        old_plot = window.windows[-1]
        wait_for(lambda: not getattr(old_plot.worker, "running", False))
        old_worker = old_plot.worker
        old_handle = window.dataset_holder[old_plot._dataset_key]

        generate_database(
            [
                RunSpecification(
                    1,
                    "replacement_signal",
                    "Replacement signal",
                    "V",
                    -1.0,
                    1.0,
                    4,
                )
            ],
            replacement_path,
        )
        release_windows_database_locks(window, database_path)
        os.replace(replacement_path, database_path)
        replacement_artifacts = database_artifact_state(database_path)
        assert replacement_artifacts["-wal"] is not None

        worker_database_reads = []
        original_prep = worker_module.load_param_data_from_db_prep
        original_load = worker_module.load_param_data_from_db

        def record_prep(*args, **kwargs):
            worker_database_reads.append("prep")
            return original_prep(*args, **kwargs)

        def record_load(*args, **kwargs):
            worker_database_reads.append("load")
            return original_load(*args, **kwargs)

        monkeypatch.setattr(worker_module, "load_param_data_from_db_prep", record_prep)
        monkeypatch.setattr(worker_module, "load_param_data_from_db", record_load)
        old_plot.refreshWindow(force=True)
        wait_for(
            lambda: (
                not window._database_load_active
                and not window._database_refresh_active
                and not window._database_detail_active
                and not window._database_expensive_detail_active
            )
        )
        window.monitor.stop()

        assert old_plot.worker is old_worker
        assert worker_database_reads == []
        assert old_plot not in window.windows
        assert old_handle.closed
        metadata = next(iter(window.RunList.all_run_metadata().values()))
        assert metadata["result_count"] == 4
        assert database_artifact_state(database_path) == replacement_artifacts
    finally:
        if window is not None:
            close_main_window(window)
        qcodes.config.core.db_location = original_database_path


def test_replaced_background_plot_does_not_switch_current_database(
    tmp_path,
    monkeypatch,
):
    configure_temp_qplot(monkeypatch, tmp_path)
    original_database_path = qcodes.config.core.db_location
    database_a = Path(tmp_path) / "source-a.db"
    replacement_a = Path(tmp_path) / "source-a-replacement.db"
    source_view = Path(tmp_path) / "source-view.db"
    replacement_view = Path(tmp_path) / "source-view-replacement.db"
    database_b = Path(tmp_path) / "source-b.db"
    _run_a, guid_a, _table_a = build_line_database(database_a, 3)
    _run_b, guid_b, _table_b = build_line_database(database_b, 2)
    _replacement_run, replacement_guid, _replacement_table = build_line_database(
        replacement_a,
        4,
        guid=guid_a,
    )
    assert replacement_guid == guid_a
    try:
        source_view.symlink_to(database_a)
    except OSError as error:
        pytest.skip(f"This platform cannot create the required symlink: {error}")

    window = main_window.MainWindow()
    try:
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.config.config["runtime_settings"]["del_grace_period"] = 0
        window.close_database(status=False)
        assert window.load_file(str(source_view))
        wait_for(
            lambda: (
                not window._database_load_active
                and not window._database_detail_active
                and not window._database_expensive_detail_active
            )
        )
        window.monitor.stop()
        assert window.ds is not None
        assert window.ds.guid == guid_a
        source_parameter = dependent_parameter(window.ds, 1)
        window.openPlot(params=[source_parameter], show=False)
        source_plot = window.windows[-1]
        wait_for(lambda: not getattr(source_plot.worker, "running", False))

        assert window.load_file(str(database_b))
        wait_for(
            lambda: (
                not window._database_load_active
                and not window._database_detail_active
                and not window._database_expensive_detail_active
            )
        )
        window.monitor.stop()
        assert window.fileTextbox.text() == str(database_b)
        assert window.ds is not None
        assert window.ds.guid == guid_b
        assert source_plot in window.windows

        target_parameter = dependent_parameter(window.ds, 1)
        window.openPlot(params=[target_parameter], show=False)
        target_plot = window.windows[-1]
        wait_for(lambda: not getattr(target_plot.worker, "running", False))

        source_key = source_plot._dataset_key
        source_handle = window.dataset_holder[source_key]
        source_trace_key = source_plot._trace_key
        assert window.add_trace_to_plot(
            target_plot,
            source_key,
            source_parameter.name,
            param=source_parameter,
        )
        wait_for(lambda: source_trace_key in target_plot.lines)
        assert source_plot._closed
        assert source_plot not in window.windows
        assert source_plot._merged_trace_users == 1
        assert source_key in window.dataset_holder

        replacement_view.symlink_to(replacement_a)
        os.replace(replacement_view, source_view)
        replacement_artifacts = symlink_source_artifact_state(
            source_view,
            database_a,
            replacement_a,
            database_b,
        )
        source_plot.refreshWindow(force=True)
        wait_for(
            lambda: (
                source_trace_key not in target_plot.lines
                and source_key not in window.dataset_holder
            )
        )

        assert source_plot not in window.windows
        assert source_handle.closed
        assert source_plot._merged_trace_users == 0
        assert not source_plot.monitor.isActive()
        assert target_plot in window.windows
        np.testing.assert_array_equal(target_plot.line.getData()[1], [10.0, 11.0])
        assert window.fileTextbox.text() == str(database_b)
        assert window._loaded_database_identity == database_file_identity(database_b)
        assert window.ds is not None
        assert window.ds.guid == guid_b
        assert symlink_source_artifact_state(
            source_view,
            database_a,
            replacement_a,
            database_b,
        ) == replacement_artifacts
    finally:
        close_main_window(window)
        qcodes.config.core.db_location = original_database_path


def test_live_wal_update_keeps_real_qcodes_instance_and_cached_handle(
    tmp_path,
    monkeypatch,
):
    configure_temp_qplot(monkeypatch, tmp_path)
    original_database_path = qcodes.config.core.db_location
    database_path = Path(tmp_path) / "live-wal.db"
    prepare_generated_database_for_live_writes(database_path)
    initialise_or_create_database_at(str(database_path), journal_mode="WAL")
    writer_connection = connect(database_path)
    enable_generation_provenance_for_writer(writer_connection)
    experiment = load_or_create_experiment(
        "live_wal",
        sample_name="same_instance",
        conn=writer_connection,
    )
    gate = ManualParameter("gate")
    signal = ManualParameter("signal")
    measurement = Measurement(exp=experiment, name="live_wal")
    measurement.register_parameter(gate)
    measurement.register_parameter(signal, setpoints=(gate,))

    with measurement.run(write_in_background=False) as datasaver:
        for index in range(3):
            datasaver.add_result((gate, float(index)), (signal, float(index + 10)))
        datasaver.flush_data_to_database(block=True)
        guid = datasaver.dataset.guid

        window = main_window.MainWindow()
        original_load_file = window.load_file
        try:
            window.startupDatabaseTimer.stop()
            window.monitor.stop()
            window.config.config["user_preference"]["confirm_close"] = False
            window.config.config["user_preference"]["confirm_close_all"] = False
            window.close_database(status=False)
            assert window.load_file(str(database_path))
            wait_for(
                lambda: (
                    not window._database_load_active
                    and not window._database_detail_active
                    and not window._database_expensive_detail_active
                )
            )
            window.monitor.stop()
            assert window.RunList.watching
            assert window.ds is not None
            assert window.ds.number_of_results == 3

            parameter = dependent_parameter(window.ds, 1)
            window.openPlot(params=[parameter], show=False)
            plot = window.windows[-1]
            wait_for(lambda: not getattr(plot.worker, "running", False))
            dataset_key = plot._dataset_key
            handle = window.dataset_holder[dataset_key]
            loaded_identity = window._loaded_database_identity

            datasaver.add_result((gate, 3.0), (signal, 13.0))
            datasaver.flush_data_to_database(block=True)
            writer_artifacts = database_artifact_state(database_path)
            assert database_file_identity(database_path) == loaded_identity

            replacement_loads = []
            def record_load(*args, **kwargs):
                replacement_loads.append((args, kwargs))
                return original_load_file(*args, **kwargs)

            window.load_file = record_load
            window.refreshMain()
            wait_for(lambda: not window._database_refresh_active)
            window.monitor.stop()

            metadata = window.RunList.all_run_metadata()
            run_metadata = next(
                run for run in metadata.values() if run["guid"] == guid
            )
            assert run_metadata["result_count"] == 4
            assert window._loaded_database_identity == loaded_identity
            assert window._current_dataset_key(guid) == dataset_key
            assert window.dataset_holder[dataset_key] is handle
            assert not handle.closed
            assert replacement_loads == []
            assert database_artifact_state(database_path) == writer_artifacts
        finally:
            window.load_file = original_load_file
            close_main_window(window)
            qcodes.config.core.db_location = original_database_path


def test_live_wal_preview_exports_use_fresh_action_local_datasets(
    tmp_path,
    monkeypatch,
):
    configure_temp_qplot(monkeypatch, tmp_path)
    original_database_path = qcodes.config.core.db_location
    source_directory = tmp_path / "source"
    export_directory = tmp_path / "exports"
    source_directory.mkdir()
    export_directory.mkdir()
    database_path = source_directory / "live-preview.db"
    selected_export_path = export_directory / "selected-preview.csv"
    run_export_path = export_directory / "run-preview.csv"
    prepare_generated_database_for_live_writes(database_path)
    initialise_or_create_database_at(str(database_path), journal_mode="WAL")
    writer_connection = connect(database_path)
    enable_generation_provenance_for_writer(writer_connection)
    experiment = load_or_create_experiment(
        "live_preview_export",
        sample_name="same_instance",
        conn=writer_connection,
    )
    gate = ManualParameter("gate")
    signal = ManualParameter("signal")
    measurement = Measurement(exp=experiment, name="live_preview_export")
    measurement.register_parameter(gate)
    measurement.register_parameter(signal, setpoints=(gate,))
    window = None

    try:
        with measurement.run(write_in_background=False) as datasaver:
            datasaver.add_result((gate, 1.0), (signal, 11.0))
            datasaver.flush_data_to_database(block=True)
            guid = datasaver.dataset.guid

            window = main_window.MainWindow()
            window.startupDatabaseTimer.stop()
            window.monitor.stop()
            window.config.config["user_preference"]["confirm_close"] = False
            window.config.config["user_preference"]["confirm_close_all"] = False
            window.close_database(status=False)
            assert window.load_file(str(database_path))
            wait_for(
                lambda: (
                    not window._database_load_active
                    and not window._database_detail_active
                    and not window._database_expensive_detail_active
                )
            )
            window.monitor.stop()
            assert window.ds is not None
            assert window.ds.guid == guid

            parameter = dependent_parameter(window.ds, 1)
            window.openPlot(params=[parameter], show=False)
            plot = window.windows[-1]
            wait_for(lambda: not getattr(plot.worker, "running", False))
            dataset_key = plot._dataset_key
            held_handle = window.dataset_holder[dataset_key]
            held_dataset = held_handle.dataset
            assert len(
                held_dataset.get_parameter_data("signal")["signal"]["signal"]
            ) == 1

            datasaver.add_result((gate, 2.0), (signal, 12.0))
            datasaver.flush_data_to_database(block=True)
            source_artifacts = database_artifact_state(database_path)
            source_entries = set(source_directory.iterdir())

            action_datasets = []
            action_snapshot_directories = []
            load_calls = []
            real_loader = plot_actions_module.load_by_guid_read_only

            def record_action_dataset(*args, **kwargs):
                dataset = real_loader(*args, **kwargs)
                snapshot_path = Path(
                    dataset.conn.execute("PRAGMA database_list").fetchone()[2]
                )
                action_datasets.append(dataset)
                action_snapshot_directories.append(snapshot_path.parent)
                load_calls.append((args, kwargs))
                return dataset

            export_paths = iter((selected_export_path, run_export_path))

            def choose_export_path(*_args, **_kwargs):
                return str(next(export_paths)), "CSV files (*.csv)"

            with monkeypatch.context() as scoped_monkeypatch:
                scoped_monkeypatch.setattr(
                    plot_actions_module,
                    "load_by_guid_read_only",
                    record_action_dataset,
                )
                scoped_monkeypatch.setattr(
                    qtw.QFileDialog,
                    "getSaveFileName",
                    choose_export_path,
                )
                window.export_preview_csv("signal")
                window.export_run_preview_csv(guid, "signal")

            for export_path in (selected_export_path, run_export_path):
                frame = pd.read_csv(export_path)
                assert frame["gate"].tolist() == [1.0, 2.0]
                assert frame["signal"].tolist() == [11.0, 12.0]

            assert len(load_calls) == 2
            for args, kwargs in load_calls:
                assert args == (guid, dataset_key.database_path)
                assert kwargs == {
                    "expected_database_identity": dataset_key.database_identity,
                }
            assert all(dataset is not held_dataset for dataset in action_datasets)
            for dataset in action_datasets:
                with pytest.raises((sqlite3.ProgrammingError, RuntimeError)):
                    dataset.conn.cursor()
            assert all(
                not snapshot_directory.exists()
                for snapshot_directory in action_snapshot_directories
            )

            # Selection and plot ownership remain on their original frozen
            # snapshot; exporting must neither refresh nor evict that handle.
            assert window.dataset_holder[dataset_key] is held_handle
            assert not held_handle.closed
            assert len(
                held_dataset.get_parameter_data("signal")["signal"]["signal"]
            ) == 1
            assert database_artifact_state(database_path) == source_artifacts
            assert set(source_directory.iterdir()) == source_entries
    finally:
        if window is not None:
            close_main_window(window)
        experiment.conn.close()
        qcodes.config.core.db_location = original_database_path


def test_preview_export_dialog_replacement_precedes_fresh_dataset_acquisition(
    tmp_path,
    monkeypatch,
):
    configure_temp_qplot(monkeypatch, tmp_path)
    original_database_path = qcodes.config.core.db_location
    source_directory = tmp_path / "source"
    replacement_directory = tmp_path / "replacement"
    export_directory = tmp_path / "exports"
    source_directory.mkdir()
    replacement_directory.mkdir()
    export_directory.mkdir()
    database_path = source_directory / "dialog-race.db"
    replacement_path = replacement_directory / "dialog-race.db"
    export_path = export_directory / "obsolete.csv"
    _run_id, guid, _table_name = build_line_database(database_path, 1)
    _replacement_run_id, replacement_guid, _replacement_table = (
        build_line_database(replacement_path, 2, guid=guid)
    )
    assert replacement_guid == guid
    window = main_window.MainWindow()
    original_load_file = window.load_file

    try:
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.close_database(status=False)
        assert window.load_file(str(database_path))
        wait_for(
            lambda: (
                not window._database_load_active
                and not window._database_detail_active
                and not window._database_expensive_detail_active
            )
        )
        window.monitor.stop()
        assert window.ds is not None
        parameter = dependent_parameter(window.ds, 1)
        window.openPlot(params=[parameter], show=False)
        plot = window.windows[-1]
        wait_for(lambda: not getattr(plot.worker, "running", False))
        dataset_key = plot._dataset_key
        held_handle = window.dataset_holder[dataset_key]
        held_dataset = held_handle.dataset

        action_datasets = []
        replacement_loads = []
        errors = []
        replacement_state = None
        replacement_entries = None
        real_loader = plot_actions_module.load_by_guid_read_only

        def record_action_dataset(*args, **kwargs):
            dataset = real_loader(*args, **kwargs)
            action_datasets.append(dataset)
            return dataset

        def record_load(*args, **kwargs):
            replacement_loads.append((args, kwargs))
            return original_load_file(*args, **kwargs)

        def replace_from_dialog(*_args, **_kwargs):
            nonlocal replacement_entries
            nonlocal replacement_state
            # The regression: an action-owned dataset must not exist while
            # the modal dialog is allowing the source path to change.
            assert action_datasets == []
            release_windows_database_locks(window, database_path)
            os.replace(replacement_path, database_path)
            replacement_state = database_artifact_state(database_path)
            replacement_entries = set(source_directory.iterdir())
            return str(export_path), "CSV files (*.csv)"

        with monkeypatch.context() as scoped_monkeypatch:
            scoped_monkeypatch.setattr(
                plot_actions_module,
                "load_by_guid_read_only",
                record_action_dataset,
            )
            scoped_monkeypatch.setattr(window, "load_file", record_load)
            scoped_monkeypatch.setattr(
                window,
                "show_error",
                lambda *args: errors.append(args),
            )
            scoped_monkeypatch.setattr(
                qtw.QFileDialog,
                "getSaveFileName",
                replace_from_dialog,
            )
            window.export_run_preview_csv(guid, "signal")

        wait_for(
            lambda: (
                not window._database_load_active
                and not window._database_detail_active
                and not window._database_expensive_detail_active
                and window._loaded_database_identity
                == database_file_identity(database_path)
            )
        )
        window.monitor.stop()

        assert action_datasets == []
        assert not export_path.exists()
        assert errors
        assert errors[-1][0] == "Run Load Failed"
        assert "replaced" in errors[-1][2].lower()
        assert replacement_loads
        assert replacement_loads[0][1] == {"force": True, "replacement": True}
        assert held_handle.closed
        with pytest.raises((sqlite3.ProgrammingError, RuntimeError)):
            held_dataset.conn.cursor()
        assert window.ds is not None
        assert window.ds.guid == guid
        assert window.ds.number_of_results == 2
        assert database_artifact_state(database_path) == replacement_state
        assert set(source_directory.iterdir()) == replacement_entries
    finally:
        window.load_file = original_load_file
        close_main_window(window)
        qcodes.config.core.db_location = original_database_path


def test_preview_export_extraction_replacement_preserves_existing_csv(
    tmp_path,
    monkeypatch,
):
    configure_temp_qplot(monkeypatch, tmp_path)
    original_database_path = qcodes.config.core.db_location
    source_directory = tmp_path / "source"
    replacement_directory = tmp_path / "replacement"
    export_directory = tmp_path / "exports"
    source_directory.mkdir()
    replacement_directory.mkdir()
    export_directory.mkdir()
    database_path = source_directory / "extraction-race.db"
    replacement_path = replacement_directory / "extraction-race.db"
    export_path = export_directory / "existing.csv"
    original_export = b"existing,content\nkeep,this\n"
    export_path.write_bytes(original_export)
    _run_id, guid, _table_name = build_line_database(database_path, 1)
    _replacement_run_id, replacement_guid, _replacement_table = (
        build_line_database(replacement_path, 2, guid=guid)
    )
    assert replacement_guid == guid
    window = main_window.MainWindow()
    original_load_file = window.load_file

    try:
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.close_database(status=False)
        assert window.load_file(str(database_path))
        wait_for(
            lambda: (
                not window._database_load_active
                and not window._database_detail_active
                and not window._database_expensive_detail_active
            )
        )
        window.monitor.stop()
        assert window.ds is not None
        parameter = dependent_parameter(window.ds, 1)
        window.openPlot(params=[parameter], show=False)
        plot = window.windows[-1]
        wait_for(lambda: not getattr(plot.worker, "running", False))
        dataset_key = plot._dataset_key
        held_handle = window.dataset_holder[dataset_key]

        action_datasets = []
        replacement_loads = []
        errors = []
        replacement_state = None
        replacement_entries = None
        real_loader = plot_actions_module.load_by_guid_read_only
        real_extraction = window._measurement_dataframe

        def record_action_dataset(*args, **kwargs):
            dataset = real_loader(*args, **kwargs)
            action_datasets.append(dataset)
            return dataset

        def replace_during_extraction(dataset, params):
            nonlocal replacement_entries
            nonlocal replacement_state
            frame = real_extraction(dataset, params)
            assert frame["signal"].tolist() == [10.0]
            release_windows_database_locks(window, database_path, dataset)
            os.replace(replacement_path, database_path)
            replacement_state = database_artifact_state(database_path)
            replacement_entries = set(source_directory.iterdir())
            return frame

        def record_load(*args, **kwargs):
            replacement_loads.append((args, kwargs))
            return original_load_file(*args, **kwargs)

        with monkeypatch.context() as scoped_monkeypatch:
            scoped_monkeypatch.setattr(
                plot_actions_module,
                "load_by_guid_read_only",
                record_action_dataset,
            )
            scoped_monkeypatch.setattr(
                window,
                "_measurement_dataframe",
                replace_during_extraction,
            )
            scoped_monkeypatch.setattr(window, "load_file", record_load)
            scoped_monkeypatch.setattr(
                window,
                "show_error",
                lambda *args: errors.append(args),
            )
            scoped_monkeypatch.setattr(
                qtw.QFileDialog,
                "getSaveFileName",
                lambda *_args, **_kwargs: (
                    str(export_path),
                    "CSV files (*.csv)",
                ),
            )
            scoped_monkeypatch.setattr(
                qtw.QMessageBox,
                "question",
                lambda *_args, **_kwargs: qtw.QMessageBox.StandardButton.Yes,
            )
            window.export_run_preview_csv(guid, "signal")

        wait_for(
            lambda: (
                not window._database_load_active
                and not window._database_detail_active
                and not window._database_expensive_detail_active
                and window._loaded_database_identity
                == database_file_identity(database_path)
            )
        )
        window.monitor.stop()

        assert len(action_datasets) == 1
        with pytest.raises((sqlite3.ProgrammingError, RuntimeError)):
            action_datasets[0].conn.cursor()
        assert export_path.read_bytes() == original_export
        assert set(export_directory.iterdir()) == {export_path}
        assert errors
        assert errors[-1][0] == "CSV Export Failed"
        assert "replaced" in errors[-1][2].lower()
        assert replacement_loads
        assert replacement_loads[0][1] == {"force": True, "replacement": True}
        assert held_handle.closed
        assert window.ds is not None
        assert window.ds.guid == guid
        assert window.ds.number_of_results == 2
        assert database_artifact_state(database_path) == replacement_state
        assert set(source_directory.iterdir()) == replacement_entries
    finally:
        window.load_file = original_load_file
        close_main_window(window)
        qcodes.config.core.db_location = original_database_path


def test_preview_export_rejects_database_replacement_during_fresh_load(
    tmp_path,
    monkeypatch,
):
    configure_temp_qplot(monkeypatch, tmp_path)
    original_database_path = qcodes.config.core.db_location
    source_directory = tmp_path / "source"
    replacement_directory = tmp_path / "replacement"
    export_directory = tmp_path / "exports"
    source_directory.mkdir()
    replacement_directory.mkdir()
    export_directory.mkdir()
    database_path = source_directory / "racing-preview.db"
    replacement_path = replacement_directory / "racing-preview.db"
    export_path = export_directory / "wrong-instance.csv"
    _run_id, guid, _table_name = build_line_database_with_replayed_stale_wal(
        database_path
    )
    build_line_database(replacement_path, 2, guid=guid)
    window = main_window.MainWindow()

    try:
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.close_database(status=False)
        assert window.load_file(str(database_path))
        wait_for(
            lambda: (
                not window._database_load_active
                and not window._database_detail_active
                and not window._database_expensive_detail_active
            )
        )
        window.monitor.stop()
        assert window.ds is not None
        parameter = dependent_parameter(window.ds, 1)
        window.openPlot(params=[parameter], show=False)
        plot = window.windows[-1]
        wait_for(lambda: not getattr(plot.worker, "running", False))
        dataset_key = plot._dataset_key
        held_handle = window.dataset_holder[dataset_key]
        held_dataset = held_handle.dataset

        real_require = readonly_module._require_expected_database_instance
        real_connect = readonly_module.connect
        replacement_state = None
        replacement_entries = None
        replacement_happened = False
        opened_connections = []
        errors = []
        dialog_opened = False

        def replace_after_initial_identity_check(path, expected_identity):
            nonlocal replacement_entries
            nonlocal replacement_happened
            nonlocal replacement_state
            result = real_require(path, expected_identity)
            if (
                not replacement_happened
                and Path(path) == database_path.resolve()
                and expected_identity == dataset_key.database_identity
            ):
                os.replace(replacement_path, database_path)
                replacement_state = database_artifact_state(database_path)
                replacement_entries = set(source_directory.iterdir())
                replacement_happened = True
            return result

        def record_connection(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            opened_connections.append(connection)
            return connection

        def unexpected_save_dialog(*_args, **_kwargs):
            nonlocal dialog_opened
            dialog_opened = True
            return str(export_path), "CSV files (*.csv)"

        with monkeypatch.context() as scoped_monkeypatch:
            scoped_monkeypatch.setattr(
                readonly_module,
                "_require_expected_database_instance",
                replace_after_initial_identity_check,
            )
            scoped_monkeypatch.setattr(
                readonly_module,
                "connect",
                record_connection,
            )
            scoped_monkeypatch.setattr(
                window,
                "_reload_if_database_instance_changed",
                lambda _path: False,
            )
            scoped_monkeypatch.setattr(
                window,
                "show_error",
                lambda *args: errors.append(args),
            )
            scoped_monkeypatch.setattr(
                qtw.QFileDialog,
                "getSaveFileName",
                unexpected_save_dialog,
            )
            window.export_run_preview_csv(guid, "signal")

        assert replacement_happened
        assert dialog_opened
        assert not export_path.exists()
        assert errors
        assert errors[-1][0] == "Run Load Failed"
        assert "replaced" in errors[-1][2].lower()
        # The replacement may now be rejected before SQLite is opened. If it
        # happens after opening, every provisional connection must be closed.
        for connection in opened_connections:
            with pytest.raises((sqlite3.ProgrammingError, RuntimeError)):
                connection.cursor()

        # The failed action neither exported the replacement nor disposed of
        # the plot-owned snapshot from the original database instance.
        assert window.dataset_holder[dataset_key] is held_handle
        assert not held_handle.closed
        assert len(
            held_dataset.get_parameter_data("signal")["signal"]["signal"]
        ) == 1
        assert database_artifact_state(database_path) == replacement_state
        assert set(source_directory.iterdir()) == replacement_entries
    finally:
        close_main_window(window)
        qcodes.config.core.db_location = original_database_path


@pytest.mark.parametrize("outcome", ["success", "failure", "cancellation"])
def test_same_path_generation_gate_blocks_every_database_consumer(
    tmp_path,
    monkeypatch,
    outcome,
):
    """A paused publisher owns its path until terminal recovery completes."""
    configure_temp_qplot(monkeypatch, tmp_path)
    original_qcodes_database = qcodes.config.core.db_location
    database_path = Path(tmp_path) / f"blocked-generation-{outcome}.db"
    csv_path = Path(tmp_path) / f"blocked-generation-{outcome}.csv"
    _run_id, guid, _table_name = build_line_database(
        database_path,
        2,
        journal_mode="DELETE",
    )
    write_small_generation_specification(csv_path, point_count=5)
    original_generate_database = database_actions_module.generate_database
    worker_entered = threading.Event()
    release_worker = threading.Event()
    injected_error = RuntimeError("injected prepublication generation failure")
    window = None
    errors = []

    def blocked_generate_database(*args, **kwargs):
        worker_entered.set()
        assert release_worker.wait(12)
        if outcome == "failure":
            raise injected_error
        if outcome == "cancellation":
            assert kwargs["cancelled_callback"]()
            raise GenerationCancelled("Test-database generation was cancelled")
        return original_generate_database(*args, **kwargs)

    try:
        window = main_window.MainWindow()
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.close_database(status=False)
        window.show_error = (
            lambda title, message, details=None: errors.append(
                (title, message, details)
            )
        )
        assert window.load_file(str(database_path))
        wait_for(
            lambda: (
                not window._database_load_active
                and not window._database_detail_active
                and not window._database_expensive_detail_active
                and not window.infoBox.preview._workers
                and not window._plot_workers
            )
        )
        parameter = dependent_parameter(window.ds, 1)
        window.openPlot(params=[parameter], show=False)
        old_plot = window.windows[-1]
        wait_for(
            lambda: (
                not getattr(old_plot.worker, "running", False)
                and not window._plot_workers
            )
        )
        original_instance = database_instance(database_path)

        configure_generation_dialogs(monkeypatch, csv_path, database_path)
        monkeypatch.setattr(
            database_actions_module,
            "generate_database",
            blocked_generate_database,
        )
        assert window.generate_test_database_from_csv()
        wait_for(worker_entered.is_set)
        assert window._database_generation_transaction_blocks_path(database_path)
        assert window.testDatabaseGenerationThreadPool.activeThreadCount() == 1
        assert_same_path_generation_consumers_released(window)

        def database_info_must_not_run(_database_path):
            raise AssertionError("database information bypassed generation gate")

        monkeypatch.setattr(
            database_actions_module,
            "database_info_rows",
            database_info_must_not_run,
        )
        window.refreshMain()
        window.monitor.timeout.emit()
        window.show_database_info()
        assert not window.load_database_path(str(database_path))
        assert not window.load_file(str(database_path), force=True, replacement=True)
        window.updateSelected(guid)
        window.openRun()
        window.open_selected_run_all()
        window.openPlot(guid)
        window.open_param_by_index(0)
        window.open_preview_plot("signal")
        window.open_run_preview_plot(guid, "signal")
        window.exportRunCsv()
        window.export_preview_csv("signal")
        window.export_run_preview_csv(guid, "signal")
        window._start_database_detail_load(
            str(database_path),
            {1: {"guid": guid}},
        )
        window.database_refresh_finished(
            window._database_refresh_generation,
            str(database_path),
            {99: {"guid": "stale-refresh-guid"}},
            {},
            None,
        )
        window.database_detail_batch_ready(
            window._database_detail_generation,
            str(database_path),
            {99: {"guid": "stale-detail-guid"}},
        )
        window.database_expensive_detail_batch_ready(
            window._database_expensive_detail_generation,
            str(database_path),
            {99: {"guid": "stale-expensive-detail-guid"}},
        )
        preview = window.infoBox.preview
        preview._worker_finished(
            preview.generation - 1,
            "stale-preview-guid",
            ["stale preview"],
            None,
        )
        # The production control represents tenths of a second, so keep the
        # test input representable while checking the conversion explicitly.
        window.spinBox.setValue(4.1)
        expected_monitor_interval = 4100
        assert_same_path_generation_consumers_released(window)
        assert window.testDatabaseGenerationThreadPool.activeThreadCount() == 1
        assert window.loadDatabaseButton.isEnabled()
        assert window.spinBox.isEnabled()
        for control in (
            window.refreshDatabaseButton,
            window.databaseInfoButton,
            window.plotRunButton,
            window.exportCsvButton,
            window.RunList,
            window.infoBox,
        ):
            assert not control.isEnabled()

        if outcome == "cancellation":
            window._test_database_generation_worker.cancel()
        release_worker.set()
        wait_for(lambda: not window._test_database_generation_active)
        wait_for(
            lambda: (
                not window._database_load_active
                and not window._database_detail_active
                and not window._database_expensive_detail_active
            )
        )

        assert not window._database_view_released_for_generation
        assert window._test_database_replacement_state is None
        assert window.generateTestDatabaseAction.isEnabled()
        assert window.monitor.isActive()
        assert window.monitor.interval() == expected_monitor_interval
        assert window.ds is not None
        if outcome == "success":
            assert database_instance(database_path) != original_instance
            assert window.ds.number_of_results == 5
            assert replacement_wal_is_quarantined(database_path)
            assert errors == []
        else:
            assert database_instance(database_path) == original_instance
            assert window.ds.number_of_results == 2
            assert not replacement_wal_is_quarantined(database_path)
            if outcome == "failure":
                assert errors == [
                    (
                        "Test Database Generation Failed",
                        "Could not generate the test database.",
                        str(injected_error),
                    )
                ]
            else:
                assert errors == []
    finally:
        release_worker.set()
        if window is not None:
            close_main_window(window)
        qcodes.config.core.db_location = original_qcodes_database


def test_unrelated_database_load_is_not_overwritten_by_generation_callback(
    tmp_path,
    monkeypatch,
):
    configure_temp_qplot(monkeypatch, tmp_path)
    original_qcodes_database = qcodes.config.core.db_location
    owned_path = Path(tmp_path) / "owned-generation.db"
    unrelated_path = Path(tmp_path) / "unrelated-session.db"
    csv_path = Path(tmp_path) / "owned-generation.csv"
    build_line_database(owned_path, 2, journal_mode="DELETE")
    build_line_database(unrelated_path, 3, journal_mode="DELETE")
    write_small_generation_specification(csv_path, point_count=5)
    original_generate_database = database_actions_module.generate_database
    worker_entered = threading.Event()
    release_worker = threading.Event()
    load_pool_blocked = threading.Event()
    release_load_pool = threading.Event()
    window = None

    class LoadPoolBlocker(QtCore.QRunnable):
        def run(self):
            load_pool_blocked.set()
            assert release_load_pool.wait(12)

    def blocked_generate_database(*args, **kwargs):
        worker_entered.set()
        assert release_worker.wait(12)
        return original_generate_database(*args, **kwargs)

    try:
        window = main_window.MainWindow()
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.close_database(status=False)
        assert window.load_file(str(owned_path))
        wait_for(
            lambda: (
                not window._database_load_active
                and not window._database_detail_active
                and not window._database_expensive_detail_active
            )
        )

        configure_generation_dialogs(monkeypatch, csv_path, owned_path)
        monkeypatch.setattr(
            database_actions_module,
            "generate_database",
            blocked_generate_database,
        )
        assert window.generate_test_database_from_csv()
        wait_for(worker_entered.is_set)
        window.databaseLoadThreadPool.start(LoadPoolBlocker())
        wait_for(load_pool_blocked.is_set)
        assert window.load_file(str(unrelated_path))
        assert window._database_load_active
        release_worker.set()
        wait_for(lambda: not window._test_database_generation_active)
        assert window.fileTextbox.text() == str(owned_path)
        assert window._test_database_replacement_state.outcome == "replacement"
        assert window._database_view_released_for_generation
        assert window._database_generation_transaction_blocks_path(owned_path)
        assert not window._database_generation_transaction_blocks_path(
            unrelated_path
        )

        release_load_pool.set()
        wait_for(
            lambda: (
                window.fileTextbox.text() == str(unrelated_path)
                and not window._database_load_active
                and not window._database_detail_active
                and not window._database_expensive_detail_active
            )
        )
        unrelated_instance = database_instance(unrelated_path)
        assert window._loaded_database_instance == unrelated_instance
        assert window.ds.number_of_results == 3
        assert not window._database_generation_transaction_blocks_path(owned_path)
        assert not window._database_generation_transaction_blocks_path(unrelated_path)
        assert window.refreshDatabaseButton.isEnabled()
        assert window.databaseInfoButton.isEnabled()
        assert window.RunList.isEnabled()
        assert window.infoBox.isEnabled()

        qtw.QApplication.processEvents()
        assert window.fileTextbox.text() == str(unrelated_path)
        assert window._loaded_database_instance == unrelated_instance
        assert window.ds.number_of_results == 3
        assert window._test_database_replacement_state is None
        assert not window._database_view_released_for_generation
    finally:
        release_worker.set()
        release_load_pool.set()
        if window is not None:
            close_main_window(window)
        qcodes.config.core.db_location = original_qcodes_database


def test_same_path_generation_prepublication_failure_restores_static_view(
    tmp_path,
    monkeypatch,
):
    configure_temp_qplot(monkeypatch, tmp_path)
    original_database_path = qcodes.config.core.db_location
    database_path = Path(tmp_path) / "static-generation-failure.db"
    csv_path = Path(tmp_path) / "replacement.csv"
    build_line_database(database_path, 2, journal_mode="DELETE")
    build_line_database(database_path, 3, journal_mode="DELETE")
    write_small_generation_specification(csv_path)
    window = None
    errors = []
    injected_error = RuntimeError("injected failure before database publication")

    try:
        window = main_window.MainWindow()
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.close_database(status=False)
        window.show_error = (
            lambda title, message, details=None: errors.append(
                (title, message, details)
            )
        )
        assert window.load_file(str(database_path))
        wait_for(
            lambda: (
                not window._database_load_active
                and not window._database_detail_active
                and not window._database_expensive_detail_active
            )
        )
        window.spinBox.setValue(2.4)
        expected_monitor_interval = 2400
        selected_run_id = select_nondefault_run_and_finish_previews(window)
        assert window.monitor.isActive()
        assert window.monitor.interval() == expected_monitor_interval
        assert not replacement_wal_is_quarantined(database_path)

        original_instance = database_instance(database_path)
        original_artifacts = database_artifact_bytes_and_mtimes(database_path)
        assert original_artifacts[""] is not None
        assert original_artifacts["-wal"] is None
        assert original_artifacts["-shm"] is None
        assert original_artifacts["-journal"] is None
        original_view = generation_database_view(window)
        assert original_view["selected_run_id"] == selected_run_id

        configure_generation_dialogs(
            monkeypatch,
            csv_path,
            database_path,
        )

        def fail_before_publication(*_args, **_kwargs):
            raise injected_error

        monkeypatch.setattr(
            database_actions_module,
            "generate_database",
            fail_before_publication,
        )
        assert window.generate_test_database_from_csv()
        wait_for(lambda: not window._test_database_generation_active)
        wait_for(
            lambda: (
                not window._database_load_active
                and not window._database_detail_active
                and not window._database_expensive_detail_active
            )
        )
        wait_for(
            lambda: (
                not window.infoBox.preview._workers
                and not window.infoBox.preview.queue
                and not window.infoBox.preview.active
            )
        )

        assert errors == [
            (
                "Test Database Generation Failed",
                "Could not generate the test database.",
                str(injected_error),
            )
        ]
        assert database_instance(database_path) == original_instance
        assert window._loaded_database_instance == original_instance
        assert not replacement_wal_is_quarantined(database_path)
        assert generation_database_view(window) == original_view
        assert database_artifact_bytes_and_mtimes(database_path) == original_artifacts
    finally:
        if window is not None:
            close_main_window(window)
        qcodes.config.core.db_location = original_database_path


def test_same_path_generation_prepublication_failure_restores_live_wal_view(
    tmp_path,
    monkeypatch,
):
    configure_temp_qplot(monkeypatch, tmp_path)
    original_database_path = qcodes.config.core.db_location
    database_path = Path(tmp_path) / "live-wal-generation-failure.db"
    csv_path = Path(tmp_path) / "replacement.csv"
    generate_database(
        [
            RunSpecification(
                1,
                "first_signal",
                "First signal",
                "nA",
                0.0,
                1.0,
                2,
            ),
            RunSpecification(
                1,
                "second_signal",
                "Second signal",
                "nA",
                0.0,
                2.0,
                3,
            ),
        ],
        database_path,
    )
    first_run_id = 1
    write_small_generation_specification(csv_path)
    writer = sqlite3.connect(database_path)
    window = None
    errors = []
    injected_error = RuntimeError("injected failure before database publication")

    try:
        assert writer.execute("PRAGMA journal_mode = DELETE").fetchone()[0] == "delete"
        writer.execute("UPDATE runs SET name = 'OLD_MAIN'")
        writer.commit()
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        writer.execute("UPDATE runs SET name = 'NEW_WAL'")
        writer.commit()
        assert immutable_run_name(database_path, first_run_id) == "OLD_MAIN"
        assert qplot_run_name(database_path, first_run_id) == "NEW_WAL"

        window = main_window.MainWindow()
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.close_database(status=False)
        window.show_error = (
            lambda title, message, details=None: errors.append(
                (title, message, details)
            )
        )
        assert window.load_file(str(database_path))
        wait_for(
            lambda: (
                not window._database_load_active
                and not window._database_detail_active
                and not window._database_expensive_detail_active
            )
        )
        window.spinBox.setValue(2.4)
        expected_monitor_interval = 2400
        selected_run_id = select_nondefault_run_and_finish_previews(window)
        assert window.ds.name == "NEW_WAL"
        assert window.monitor.isActive()
        assert window.monitor.interval() == expected_monitor_interval
        assert not replacement_wal_is_quarantined(database_path)

        original_instance = database_instance(database_path)
        original_artifacts = database_artifact_bytes_and_mtimes(database_path)
        assert original_artifacts[""] is not None
        assert original_artifacts["-wal"] is not None
        assert original_artifacts["-shm"] is not None
        assert original_artifacts["-journal"] is None
        original_view = generation_database_view(window)
        assert original_view["selected_run_id"] == selected_run_id

        configure_generation_dialogs(
            monkeypatch,
            csv_path,
            database_path,
        )

        def fail_before_publication(*_args, **_kwargs):
            raise injected_error

        monkeypatch.setattr(
            database_actions_module,
            "generate_database",
            fail_before_publication,
        )
        assert window.generate_test_database_from_csv()
        wait_for(lambda: not window._test_database_generation_active)
        wait_for(
            lambda: (
                not window._database_load_active
                and not window._database_detail_active
                and not window._database_expensive_detail_active
            )
        )
        wait_for(
            lambda: (
                not window.infoBox.preview._workers
                and not window.infoBox.preview.queue
                and not window.infoBox.preview.active
            )
        )

        assert errors == [
            (
                "Test Database Generation Failed",
                "Could not generate the test database.",
                str(injected_error),
            )
        ]
        assert database_instance(database_path) == original_instance
        assert window._loaded_database_instance == original_instance
        assert not replacement_wal_is_quarantined(database_path)
        assert qplot_run_name(database_path, selected_run_id) == "NEW_WAL"
        assert generation_database_view(window) == original_view
        assert database_artifact_bytes_and_mtimes(database_path) == original_artifacts
    finally:
        if window is not None:
            close_main_window(window)
        writer.close()
        qcodes.config.core.db_location = original_database_path


def test_loaded_path_test_database_generation_uses_full_gui_worker_lifecycle(
    tmp_path,
    monkeypatch,
):
    configure_temp_qplot(monkeypatch, tmp_path)
    original_database_path = qcodes.config.core.db_location
    database_path = Path(tmp_path) / "successful-gui-replacement.db"
    csv_path = Path(tmp_path) / "replacement.csv"
    build_line_database(database_path, 2, journal_mode="DELETE")
    write_small_generation_specification(csv_path, point_count=5)
    window = None
    errors = []

    try:
        window = main_window.MainWindow()
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.close_database(status=False)
        window.show_error = (
            lambda title, message, details=None: errors.append(
                (title, message, details)
            )
        )
        assert window.load_file(str(database_path))
        wait_for(
            lambda: (
                not window._database_load_active
                and not window._database_detail_active
                and not window._database_expensive_detail_active
            )
        )
        window.spinBox.setValue(2.4)
        expected_monitor_interval = 2400
        parameter = dependent_parameter(window.ds, 1)
        window.openPlot(params=[parameter], show=False)
        old_plot = window.windows[-1]
        wait_for(lambda: not getattr(old_plot.worker, "running", False))
        old_handle = window.dataset_holder[old_plot._dataset_key]
        original_instance = database_instance(database_path)

        configure_generation_dialogs(
            monkeypatch,
            csv_path,
            database_path,
        )
        assert window.generate_test_database_from_csv()
        wait_for(lambda: not window._test_database_generation_active)
        wait_for(
            lambda: (
                not window._database_load_active
                and not window._database_detail_active
                and not window._database_expensive_detail_active
            )
        )

        replacement_instance = database_instance(database_path)
        assert replacement_instance != original_instance
        assert window._loaded_database_instance == replacement_instance
        assert replacement_wal_is_quarantined(database_path)
        assert old_handle.closed
        assert old_plot not in window.windows
        assert window.RunList.topLevelItemCount() == 1
        assert window.ds is not None
        assert window.ds.number_of_results == 5
        metadata = next(iter(window.RunList.all_run_metadata().values()))
        assert metadata["result_count"] == 5
        assert window.generateTestDatabaseAction.isEnabled()
        assert not window._test_database_generation_active
        assert window._test_database_generation_worker is None
        assert window.monitor.isActive()
        assert window.monitor.interval() == expected_monitor_interval
        assert errors == []
    finally:
        if window is not None:
            close_main_window(window)
        qcodes.config.core.db_location = original_database_path


def test_loaded_path_test_database_generation_uses_replacement_reload(
    tmp_path,
    monkeypatch,
):
    configure_temp_qplot(monkeypatch, tmp_path)
    original_database_path = qcodes.config.core.db_location
    output_directory = Path(tmp_path) / "loaded # %23 space 測定"
    output_directory.mkdir()
    database_path = output_directory / "generated#%3f 測定.db"
    initial_specification = RunSpecification(
        1,
        "current",
        "Current",
        "nA",
        -1.0,
        1.0,
        3,
    )
    replacement_specification = RunSpecification(
        1,
        "current",
        "Current",
        "nA",
        -1.0,
        1.0,
        4,
    )
    generate_database([initial_specification], database_path)

    window = main_window.MainWindow()
    status_messages = []
    original_show_status = window.show_status

    def record_status(message, timeout=5000):
        status_messages.append((message, timeout))
        return original_show_status(message, timeout)

    try:
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.close_database(status=False)
        window.show_status = record_status
        assert window.load_file(str(database_path))
        wait_for(
            lambda: (
                not window._database_load_active
                and not window._database_detail_active
                and not window._database_expensive_detail_active
            )
        )
        window.monitor.stop()
        assert window.ds is not None
        assert window.ds.number_of_results == 3
        parameter = dependent_parameter(window.ds, 1)
        window.openPlot(params=[parameter], show=False)
        old_plot = window.windows[-1]
        wait_for(lambda: not getattr(old_plot.worker, "running", False))
        old_handle = window.dataset_holder[old_plot._dataset_key]

        release_windows_database_locks(window, database_path)
        generate_database(
            [replacement_specification],
            database_path,
            overwrite=True,
        )
        generated_artifacts = database_artifact_state(database_path)
        window.test_database_generation_finished(
            str(database_path),
            [replacement_specification],
            None,
        )
        wait_for(
            lambda: (
                not window._database_load_active
                and not window._database_detail_active
                and not window._database_expensive_detail_active
            )
        )
        window.monitor.stop()

        assert old_handle.closed
        assert old_plot not in window.windows
        assert window.ds is not None
        assert window.ds.number_of_results == 4
        metadata = next(iter(window.RunList.all_run_metadata().values()))
        assert metadata["result_count"] == 4
        assert any(
            "Database was replaced and reloaded" in message
            for message, _timeout in status_messages
        )
        assert database_artifact_state(database_path) == generated_artifacts
    finally:
        close_main_window(window)
        qcodes.config.core.db_location = original_database_path


def test_threaded_multi_parameter_completion_retries_each_real_plot(
    tmp_path,
    monkeypatch,
):
    configure_temp_qplot(monkeypatch, tmp_path)
    database_path = Path(tmp_path) / "threaded-live-run.db"
    original_database_path = qcodes.config.core.db_location
    initial_row_written = threading.Event()
    finish_writer = threading.Event()
    writer_completed = threading.Event()
    release_writer_connection = threading.Event()
    writer_state = {}
    writer_errors = []
    window = None
    prepare_generated_database_for_live_writes(database_path)

    def write_measurement():
        writer_dataset = None
        writer_connection = None
        try:
            initialise_or_create_database_at(str(database_path), journal_mode="WAL")
            writer_connection = connect(database_path)
            enable_generation_provenance_for_writer(writer_connection)
            experiment = load_or_create_experiment(
                "threaded_live_run",
                sample_name="completion_race",
                conn=writer_connection,
                )
            gate = ManualParameter("gate")
            signal_a = ManualParameter("signal_a")
            signal_b = ManualParameter("signal_b")
            measurement = Measurement(exp=experiment, name="completion_race")
            measurement.register_parameter(gate)
            measurement.register_parameter(signal_a, setpoints=(gate,))
            measurement.register_parameter(signal_b, setpoints=(gate,))
            with measurement.run(write_in_background=False) as datasaver:
                writer_dataset = datasaver.dataset
                datasaver.add_result(
                    (gate, 0.0),
                    (signal_a, 10.0),
                    (signal_b, 20.0),
                    )
                datasaver.flush_data_to_database(block=True)
                writer_state["run_id"] = writer_dataset.run_id
                initial_row_written.set()
                if not finish_writer.wait(30):
                    raise TimeoutError("Test did not release the measurement writer")
                datasaver.add_result(
                    (gate, 1.0),
                    (signal_a, 11.0),
                    (signal_b, 21.0),
                    )
                datasaver.add_result(
                    (gate, 2.0),
                    (signal_a, 12.0),
                    (signal_b, 22.0),
                    )
            writer_completed.set()
            if not release_writer_connection.wait(30):
                raise TimeoutError("Test did not release the writer connection")
        except BaseException as error:
            writer_errors.append(error)
            initial_row_written.set()
            writer_completed.set()
        finally:
            if writer_connection is not None:
                writer_connection.close()

    writer_thread = threading.Thread(target=write_measurement)
    writer_thread.start()

    try:
        assert initial_row_written.wait(30)
        assert writer_errors == []

        window = main_window.MainWindow()
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.close_database(status=False)
        assert window.load_file(str(database_path))
        wait_for(
            lambda: (
                not window._database_load_active
                and not window._database_detail_active
                and not window._database_expensive_detail_active
            )
        )
        window.monitor.stop()
        viewer_dataset = window.ds
        assert viewer_dataset is not None
        assert viewer_dataset.run_id == writer_state["run_id"]
        assert viewer_dataset.running
        # QCoDeS stores one result-table row per dependent parameter tree.
        assert viewer_dataset.number_of_results == 2

        parameters = {
            candidate.name: candidate
            for candidate in viewer_dataset.get_parameters()
            if candidate.depends_on
            }
        window.openPlot(
            params=[parameters["signal_a"], parameters["signal_b"]],
            show=False,
            )
        plot_a, hidden_plot_b = window.windows[-2:]
        wait_for(
            lambda: all(
                not getattr(plot.worker, "running", False)
                for plot in (plot_a, hidden_plot_b)
            )
        )
        for plot in (plot_a, hidden_plot_b):
            plot.spinBox.setValue(60.0)
            plot.monitor.stop()

        def cache_values(parameter_name):
            return np.asarray(
                cache_parameter_data(
                    viewer_dataset.cache,
                    parameter_name,
                    )[parameter_name]
                )

        def line_values(plot):
            return np.asarray(plot.line.getData()[1])

        np.testing.assert_array_equal(cache_values("signal_a"), [10.0])
        np.testing.assert_array_equal(cache_values("signal_b"), [20.0])
        np.testing.assert_array_equal(line_values(plot_a), [10.0])
        np.testing.assert_array_equal(line_values(hidden_plot_b), [20.0])

        assert window.add_trace_to_plot(
            plot_a,
            hidden_plot_b._dataset_key,
            "signal_b",
            param=hidden_plot_b.param,
            )
        merged_b = plot_a.lines[hidden_plot_b._trace_key]
        assert hidden_plot_b._closed
        assert hidden_plot_b not in window.windows
        assert hidden_plot_b._merged_trace_users == 1
        np.testing.assert_array_equal(np.asarray(merged_b.getData()[1]), [20.0])

        # Keep two plots open during the transition while the original B is a
        # closed-and-retained hidden source for the merged secondary trace.
        window.openPlot(params=[parameters["signal_b"]], show=False)
        plot_b = window.windows[-1]
        wait_for(lambda: not getattr(plot_b.worker, "running", False))
        plot_b.spinBox.setValue(60.0)
        plot_b.monitor.stop()
        plot_b.monitorIntervalChanged(plot_b.spinBox.value())
        assert window.windows == [plot_a, plot_b]
        assert not plot_b._closed
        np.testing.assert_array_equal(line_values(plot_b), [20.0])
        plot_a.monitor.stop()
        hidden_plot_b.monitor.stop()

        finish_writer.set()
        assert writer_completed.wait(30)
        assert writer_thread.is_alive()
        assert writer_errors == []
        assert viewer_dataset.running
        # The source-preserving viewer connection is intentionally pinned to
        # the WAL snapshot from when the run was opened. Plot workers must use
        # fresh read-only snapshots to discover the committed final rows.
        assert viewer_dataset.number_of_results == 2

        writer_complete_artifacts = database_artifact_state(database_path)
        assert set(writer_complete_artifacts) == {"", "-wal", "-shm", "-journal"}
        assert writer_complete_artifacts[""] is not None
        assert writer_complete_artifacts["-wal"] is not None
        assert writer_complete_artifacts["-shm"] is not None

        plot_dataset_handle = window.dataset_holder[plot_a._dataset_key]
        window.refreshMain()
        wait_for(lambda: not window._database_refresh_active)
        live_overview = {
            window.infoBox.overview.item(row, 0).text():
            window.infoBox.overview.item(row, 1).text()
            for row in range(window.infoBox.overview.rowCount())
            }
        assert live_overview["Status"] == "Completed"
        assert live_overview["Data points"] == "6"
        assert live_overview["Completed"]
        assert viewer_dataset.running
        assert viewer_dataset.number_of_results == 2
        assert window.dataset_holder[plot_a._dataset_key] is plot_dataset_handle
        assert not plot_dataset_handle.closed
        plot_dataset_handle.dataset.conn.cursor().close()
        assert database_artifact_state(database_path) == writer_complete_artifacts

        original_load = worker_module.load_param_data_from_db
        final_loads = []
        failed_b_load = False

        def record_and_fail_first_b_load(*args, **kwargs):
            nonlocal failed_b_load
            parameter_name = (
                kwargs["meas_parameter"]
                if "meas_parameter" in kwargs
                else args[3]
                )
            final_loads.append(parameter_name)
            if parameter_name == "signal_b" and not failed_b_load:
                failed_b_load = True
                raise RuntimeError("injected signal_b final-load failure")
            return original_load(*args, **kwargs)

        monkeypatch.setattr(
            worker_module,
            "load_param_data_from_db",
            record_and_fail_first_b_load,
            )
        import qcodes.dataset.data_set as qcodes_data_set

        def reject_qcodes_completion_write(*_args, **_kwargs):
            raise AssertionError("qPlot must not call mark_run_complete")

        monkeypatch.setattr(
            qcodes_data_set,
            "mark_run_complete",
            reject_qcodes_completion_write,
            )

        plot_a.refreshWindow()
        wait_for(
            lambda: (
                line_values(plot_a).size == 3
                and not getattr(plot_a.worker, "running", False)
            )
        )

        np.testing.assert_array_equal(cache_values("signal_a"), [10.0, 11.0, 12.0])
        np.testing.assert_array_equal(plot_a.line.getData()[0], [0.0, 1.0, 2.0])
        np.testing.assert_array_equal(line_values(plot_a), [10.0, 11.0, 12.0])
        np.testing.assert_array_equal(cache_values("signal_b"), [20.0])
        np.testing.assert_array_equal(line_values(plot_b), [20.0])
        np.testing.assert_array_equal(line_values(hidden_plot_b), [20.0])
        np.testing.assert_array_equal(np.asarray(merged_b.getData()[1]), [20.0])
        assert final_loads == ["signal_a"]
        assert viewer_dataset.completed
        assert not viewer_dataset.running
        assert cache_parameter_is_synchronized(viewer_dataset.cache, "signal_a")
        assert not cache_parameter_is_synchronized(viewer_dataset.cache, "signal_b")
        assert window.windows == [plot_a, plot_b]
        assert plot_a._qplot_display_synchronized
        assert not plot_b._qplot_display_synchronized
        assert not hidden_plot_b._qplot_display_synchronized
        assert merged_b.running
        assert plot_b.monitor.isActive()
        assert hidden_plot_b.monitor.isActive()
        assert database_artifact_state(database_path) == writer_complete_artifacts

        plot_b.show_error = lambda *_args, **_kwargs: None
        plot_b.refreshWindow()
        wait_for(
            lambda: (
                final_loads.count("signal_b") == 1
                and not getattr(plot_b.worker, "running", False)
            )
        )

        np.testing.assert_array_equal(cache_values("signal_b"), [20.0])
        np.testing.assert_array_equal(line_values(plot_b), [20.0])
        np.testing.assert_array_equal(line_values(hidden_plot_b), [20.0])
        assert not cache_parameter_is_synchronized(viewer_dataset.cache, "signal_b")
        assert not plot_b._qplot_display_synchronized
        assert plot_b.monitor.isActive()
        assert hidden_plot_b.monitor.isActive()
        assert database_artifact_state(database_path) == writer_complete_artifacts

        original_set_data = plot_b.line.setData
        original_refresh_plot = plot_b.refreshPlot
        display_errors = []
        fail_display = True

        def fail_first_final_display(*args, **kwargs):
            nonlocal fail_display
            y_data = np.asarray(kwargs.get("y", []))
            if fail_display and y_data.size == 3:
                fail_display = False
                raise RuntimeError("injected signal_b display failure")
            return original_set_data(*args, **kwargs)

        def capture_display_failure(finished=True, worker=None):
            try:
                return original_refresh_plot(finished, worker=worker)
            except RuntimeError as error:
                display_errors.append(error)
                return None

        plot_b.line.setData = fail_first_final_display
        plot_b.refreshPlot = capture_display_failure
        plot_b.refreshWindow()
        wait_for(
            lambda: (
                cache_values("signal_b").size == 3
                and not getattr(plot_b.worker, "running", False)
                and bool(display_errors)
            )
        )

        np.testing.assert_array_equal(cache_values("signal_b"), [20.0, 21.0, 22.0])
        np.testing.assert_array_equal(line_values(plot_b), [20.0])
        np.testing.assert_array_equal(line_values(hidden_plot_b), [20.0])
        np.testing.assert_array_equal(np.asarray(merged_b.getData()[1]), [20.0])
        assert final_loads == ["signal_a", "signal_b", "signal_b"]
        assert cache_parameter_is_synchronized(viewer_dataset.cache, "signal_b")
        assert not plot_b._qplot_display_synchronized
        assert "injected signal_b display failure" in str(display_errors[0])
        assert plot_b.monitor.isActive()
        assert hidden_plot_b.monitor.isActive()
        assert database_artifact_state(database_path) == writer_complete_artifacts

        plot_b.refreshWindow()
        wait_for(
            lambda: (
                plot_b._qplot_display_synchronized
                and line_values(plot_b).size == 3
                and not getattr(plot_b.worker, "running", False)
            )
        )

        np.testing.assert_array_equal(line_values(plot_b), [20.0, 21.0, 22.0])
        np.testing.assert_array_equal(plot_b.line.getData()[0], [0.0, 1.0, 2.0])
        np.testing.assert_array_equal(line_values(hidden_plot_b), [20.0])
        np.testing.assert_array_equal(
            np.asarray(merged_b.getData()[1]),
            [20.0],
            )
        assert final_loads == ["signal_a", "signal_b", "signal_b"]
        assert not plot_b._refresh_monitor_required()

        hidden_plot_b.refreshWindow()
        wait_for(
            lambda: (
                hidden_plot_b._qplot_display_synchronized
                and line_values(hidden_plot_b).size == 3
                and not getattr(hidden_plot_b.worker, "running", False)
            )
        )

        np.testing.assert_array_equal(
            line_values(hidden_plot_b),
            [20.0, 21.0, 22.0],
            )
        np.testing.assert_array_equal(
            np.asarray(merged_b.getData()[1]),
            [20.0, 21.0, 22.0],
            )
        assert final_loads == ["signal_a", "signal_b", "signal_b"]
        assert not hidden_plot_b._refresh_monitor_required()

        terminal_worker = plot_b.worker
        hidden_terminal_worker = hidden_plot_b.worker
        plot_b.refreshWindow()
        hidden_plot_b.refreshWindow()
        plot_a.refreshWindow()
        assert plot_b.worker is terminal_worker
        assert hidden_plot_b.worker is hidden_terminal_worker
        assert not plot_b.monitor.isActive()
        assert not hidden_plot_b.monitor.isActive()
        assert not plot_a.monitor.isActive()
        assert not merged_b.running
        assert final_loads == ["signal_a", "signal_b", "signal_b"]
        assert database_artifact_state(database_path) == writer_complete_artifacts
        release_writer_connection.set()
        writer_thread.join(30)
        assert not writer_thread.is_alive()
        assert writer_errors == []
    finally:
        finish_writer.set()
        release_writer_connection.set()
        writer_thread.join(30)
        if window is not None:
            close_main_window(window)
        qcodes.config.core.db_location = original_database_path


def test_threaded_wal_direct_sql_heatmap_completes_without_source_writes(
    tmp_path,
    monkeypatch,
):
    configure_temp_qplot(monkeypatch, tmp_path)
    database_path = Path(tmp_path) / "threaded-live-heatmap.db"
    original_database_path = qcodes.config.core.db_location
    initial_row_written = threading.Event()
    finish_writer = threading.Event()
    writer_completed = threading.Event()
    release_writer_connection = threading.Event()
    writer_state = {}
    writer_errors = []
    window = None
    prepare_generated_database_for_live_writes(database_path)

    def write_measurement():
        writer_dataset = None
        writer_connection = None
        try:
            initialise_or_create_database_at(str(database_path), journal_mode="WAL")
            writer_connection = connect(database_path)
            enable_generation_provenance_for_writer(writer_connection)
            experiment = load_or_create_experiment(
                "threaded_live_heatmap",
                sample_name="direct_sql_completion",
                conn=writer_connection,
                )
            x_param = ManualParameter("x_param")
            y_param = ManualParameter("y_param")
            signal = ManualParameter("heat_signal")
            measurement = Measurement(exp=experiment, name="direct_sql_completion")
            measurement.register_parameter(x_param)
            measurement.register_parameter(y_param)
            measurement.register_parameter(signal, setpoints=(x_param, y_param))
            with measurement.run(write_in_background=False) as datasaver:
                writer_dataset = datasaver.dataset
                datasaver.add_result(
                    (x_param, 0.0),
                    (y_param, 0.0),
                    (signal, 10.0),
                    )
                datasaver.flush_data_to_database(block=True)
                writer_state["run_id"] = writer_dataset.run_id
                initial_row_written.set()
                if not finish_writer.wait(30):
                    raise TimeoutError("Test did not release the heatmap writer")
                for x_value, y_value, signal_value in (
                        (1.0, 0.0, 11.0),
                        (0.0, 1.0, 12.0),
                        (1.0, 1.0, 13.0),
                        ):
                    datasaver.add_result(
                        (x_param, x_value),
                        (y_param, y_value),
                        (signal, signal_value),
                        )
            writer_completed.set()
            if not release_writer_connection.wait(30):
                raise TimeoutError("Test did not release the heatmap connection")
        except BaseException as error:
            writer_errors.append(error)
            initial_row_written.set()
            writer_completed.set()
        finally:
            if writer_connection is not None:
                writer_connection.close()

    writer_thread = threading.Thread(target=write_measurement)
    writer_thread.start()

    try:
        assert initial_row_written.wait(30)
        assert writer_errors == []

        window = main_window.MainWindow()
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.config.config["runtime_settings"]["max_full_heatmap_points"] = 1
        window.close_database(status=False)
        assert window.load_file(str(database_path))
        wait_for(
            lambda: (
                not window._database_load_active
                and not window._database_detail_active
                and not window._database_expensive_detail_active
            )
        )
        window.monitor.stop()
        viewer_dataset = window.ds
        assert viewer_dataset is not None
        assert viewer_dataset.run_id == writer_state["run_id"]
        assert viewer_dataset.running
        assert viewer_dataset.number_of_results == 1

        parameter = next(
            candidate
            for candidate in viewer_dataset.get_parameters()
            if candidate.name == "heat_signal"
            )
        window.openPlot(params=[parameter], show=False)
        heatmap = window.windows[-1]
        wait_for(lambda: not getattr(heatmap.worker, "running", False))
        heatmap.spinBox.setValue(60.0)
        heatmap.monitor.stop()

        assert not heatmap.worker.loaded_from_sql_heatmap
        assert np.isfinite(np.asarray(heatmap.dataGrid)).sum() == 1
        assert np.asarray(
            cache_parameter_data(viewer_dataset.cache, "heat_signal")["heat_signal"]
            ).size == 1

        finish_writer.set()
        assert writer_completed.wait(30)
        assert writer_thread.is_alive()
        assert writer_errors == []
        artifacts_after_writer_completion = database_artifact_state(database_path)
        assert artifacts_after_writer_completion[""] is not None
        assert artifacts_after_writer_completion["-wal"] is not None
        assert artifacts_after_writer_completion["-shm"] is not None

        import qcodes.dataset.data_set as qcodes_data_set

        def reject_qcodes_completion_write(*_args, **_kwargs):
            raise AssertionError("qPlot must not call mark_run_complete")

        monkeypatch.setattr(
            qcodes_data_set,
            "mark_run_complete",
            reject_qcodes_completion_write,
            )

        heatmap.refreshWindow()
        wait_for(
            lambda: (
                getattr(heatmap.worker, "loaded_from_sql_heatmap", False)
                and heatmap._qplot_display_synchronized
                and not getattr(heatmap.worker, "running", False)
            )
        )

        assert heatmap.worker.dataset_completed is True
        assert heatmap.worker.loaded_point_count == 4
        assert viewer_dataset.completed
        assert not viewer_dataset.running
        assert heatmap._qplot_display_uses_direct_sql
        assert not cache_parameter_is_synchronized(
            viewer_dataset.cache,
            "heat_signal",
            )
        assert np.asarray(
            cache_parameter_data(viewer_dataset.cache, "heat_signal")["heat_signal"]
            ).size == 1
        np.testing.assert_array_equal(
            np.sort(np.asarray(heatmap.dataGrid)[np.isfinite(heatmap.dataGrid)]),
            [10.0, 11.0, 12.0, 13.0],
            )
        np.testing.assert_array_equal(
            np.sort(np.asarray(heatmap.image.image).ravel()),
            [10.0, 11.0, 12.0, 13.0],
            )
        assert database_artifact_state(database_path) == artifacts_after_writer_completion

        wait_for(
            lambda: (
                not heatmap._heatmap_view_reload_timer.isActive()
                and not getattr(heatmap.worker, "running", False)
            ),
            )
        terminal_worker = heatmap.worker
        heatmap.refreshWindow()
        assert heatmap.worker is terminal_worker
        assert not heatmap.monitor.isActive()
        assert database_artifact_state(database_path) == artifacts_after_writer_completion

        release_writer_connection.set()
        writer_thread.join(30)
        assert not writer_thread.is_alive()
        assert writer_errors == []
    finally:
        finish_writer.set()
        release_writer_connection.set()
        writer_thread.join(30)
        if window is not None:
            close_main_window(window)
        qcodes.config.core.db_location = original_database_path
