import hashlib
import time
from pathlib import Path

import numpy as np
import qcodes
from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.parameters import ManualParameter

from qplot.configuration.config import config
from qplot.windows import main as main_window
from qplot.windows._dataset_handle import TraceKey
from qplot.windows._dragdrop import make_run_preview_mime


class _PreviewDropEvent:
    """Small drop-event stand-in for the plot window's actual MIME path."""

    def __init__(self, mime_data):
        self._mime_data = mime_data
        self.drop_action = None
        self.accepted = False
        self.ignored = False

    def type(self):
        return QtCore.QEvent.Type.Drop

    def mimeData(self):
        return self._mime_data

    def setDropAction(self, action):
        self.drop_action = action

    def accept(self):
        self.accepted = True

    def ignore(self):
        self.ignored = True


def _configure_temp_qplot(monkeypatch, tmp_path):
    qplot_home = tmp_path / ".qplot"
    monkeypatch.setattr(config, "default_path", str(qplot_home))
    monkeypatch.setattr(
        config,
        "default_file",
        str(qplot_home / config.config_file_name),
    )


def _wait_for(predicate, timeout=12):
    app = qtw.QApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            app.processEvents()
            return
        time.sleep(0.03)
    raise AssertionError("Timed out waiting for GUI integration state")


def _close_main_window(window):
    window.startupDatabaseTimer.stop()
    window.monitor.stop()
    window.close_plot_windows(confirm=False, status=False)
    window.threadPool.waitForDone(1000)
    window.databaseLoadThreadPool.waitForDone(1000)
    window.hide()
    window.deleteLater()
    qtw.QApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    qtw.QApplication.processEvents()


def _database_artifact_state(database_path):
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


def _build_two_heatmap_database(database_path):
    initialise_or_create_database_at(str(database_path))
    experiment = load_or_create_experiment(
        "heatmap_layer_integration",
        sample_name="two_layers",
    )

    gate = ManualParameter("gate", label="Gate voltage", unit="V")
    bias = ManualParameter("bias", label="Bias voltage", unit="mV")
    primary = ManualParameter("signal_a", label="Signal A", unit="nA")
    secondary = ManualParameter("signal_b", label="Signal B", unit="nA")
    gate_values = np.array([-1.0, 0.0, 1.0])
    bias_values = np.array([-0.5, 0.5, 1.5, 2.5])

    measurement = Measurement(exp=experiment, name="two_heatmaps")
    measurement.register_parameter(gate)
    measurement.register_parameter(bias)
    measurement.register_parameter(primary, setpoints=(gate, bias))
    measurement.register_parameter(secondary, setpoints=(gate, bias))

    dataset = None
    with measurement.run(write_in_background=False) as datasaver:
        for gate_value in gate_values:
            for bias_value in bias_values:
                datasaver.add_result(
                    (gate, float(gate_value)),
                    (bias, float(bias_value)),
                    (primary, float(10 * gate_value + bias_value)),
                    (secondary, float(100 + 20 * gate_value - 2 * bias_value)),
                )
        dataset = datasaver.dataset
        run_id = dataset.run_id
        guid = dataset.guid

    connections = {}
    for owner in (dataset, experiment):
        connection = getattr(owner, "conn", None)
        if connection is not None:
            connections[id(connection)] = connection
    for connection in connections.values():
        connection.close()

    expected_primary = np.array([
        [10 * gate_value + bias_value for bias_value in bias_values]
        for gate_value in gate_values
    ])
    expected_secondary = np.array([
        [100 + 20 * gate_value - 2 * bias_value for bias_value in bias_values]
        for gate_value in gate_values
    ])
    return run_id, guid, expected_primary, expected_secondary


def _drop_heatmap(target, guid, parameter):
    event = _PreviewDropEvent(
        make_run_preview_mime(
            guid,
            parameter.name,
            parameter.depends_on_,
        )
    )
    assert target._handle_preview_drag_drop(event)
    assert event.accepted
    assert not event.ignored
    assert event.drop_action == QtCore.Qt.DropAction.CopyAction


def test_preview_drop_adds_and_removes_real_secondary_heatmap(
    tmp_path,
    monkeypatch,
):
    _configure_temp_qplot(monkeypatch, tmp_path)
    original_database_path = qcodes.config.core.db_location
    database_path = Path(tmp_path) / "heatmap-layers.db"
    _run_id, guid, expected_primary, expected_secondary = (
        _build_two_heatmap_database(database_path)
    )
    original_artifacts = _database_artifact_state(database_path)
    window = None

    try:
        window = main_window.MainWindow()
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.config.config["runtime_settings"]["del_grace_period"] = 0
        window.close_database(status=False)

        assert window.load_file(str(database_path))
        _wait_for(
            lambda: (
                not window._database_load_active
                and not window._database_detail_active
                and not window._database_expensive_detail_active
                and window.ds is not None
            )
        )
        window.monitor.stop()
        assert window.ds.guid == guid

        parameters = {
            parameter.name: parameter
            for parameter in window.ds.get_parameters()
            if parameter.depends_on
        }
        primary_parameter = parameters["signal_a"]
        secondary_parameter = parameters["signal_b"]
        window.openPlot(params=[primary_parameter], show=False)
        target = window.windows[-1]
        _wait_for(
            lambda: (
                hasattr(target, "dataGrid")
                and target._heatmap_geometry() is not None
                and not getattr(target.worker, "running", False)
            )
        )

        np.testing.assert_allclose(target.dataGrid, expected_primary)
        primary_grid_object = target.dataGrid
        primary_grid = np.asarray(target.dataGrid).copy()
        primary_geometry = target._heatmap_geometry()
        primary_key = target._trace_key
        dataset_key = target._dataset_key
        dataset_handle = window.dataset_holder[dataset_key]
        primary_owner_count = dataset_handle.users
        secondary_key = TraceKey(dataset_key, secondary_parameter.name)

        _drop_heatmap(target, guid, secondary_parameter)
        _wait_for(
            lambda: (
                secondary_key in target.heatmaps
                and target.heatmaps[secondary_key].geometry is not None
                and target.heatmaps[secondary_key].image.isVisible()
                and not getattr(
                    target.heatmaps[secondary_key].from_win.worker,
                    "running",
                    False,
                )
            )
        )

        assert set(target.heatmaps) == {primary_key, secondary_key}
        assert target.heatmaps[primary_key] is target
        layer = target.heatmaps[secondary_key]
        source = layer.from_win
        assert layer.image.isVisible()
        assert not layer.heatmap_mesh.isVisible()
        np.testing.assert_allclose(layer.data_grid, expected_secondary)
        np.testing.assert_allclose(layer.image.image, expected_secondary)
        assert np.min(layer.data_grid) > np.max(primary_grid)

        assert target.dataGrid is primary_grid_object
        np.testing.assert_array_equal(target.dataGrid, primary_grid)
        assert target._heatmap_geometry() is primary_geometry
        assert source._closed
        assert source not in window.windows
        assert window.windows == [target]
        assert source._merged_trace_users == 1
        assert dataset_handle.users == primary_owner_count + 1

        _drop_heatmap(target, guid, secondary_parameter)
        qtw.QApplication.processEvents()
        assert set(target.heatmaps) == {primary_key, secondary_key}
        assert target.heatmaps[secondary_key] is layer
        assert layer.from_win is source
        assert window.windows == [target]
        assert source._merged_trace_users == 1
        assert dataset_handle.users == primary_owner_count + 1

        assert target.remove_heatmap(trace_key=secondary_key)
        assert set(target.heatmaps) == {primary_key}
        assert source._merged_trace_users == 0
        assert not source.monitor.isActive()
        assert not layer.image.isVisible()
        assert not layer.heatmap_mesh.isVisible()
        assert dataset_handle.users == primary_owner_count
        np.testing.assert_array_equal(target.dataGrid, primary_grid)
        assert target._heatmap_geometry() is primary_geometry
        assert _database_artifact_state(database_path) == original_artifacts
    finally:
        if window is not None:
            _close_main_window(window)
        qcodes.config.core.db_location = original_database_path

    assert _database_artifact_state(database_path) == original_artifacts
