"""Shared fixtures and assertions for heatmap layer integration."""

import hashlib
import stat
import time
from pathlib import Path

import numpy as np
from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.parameters import ManualParameter

from qplot.configuration.config import config
from qplot.windows._dragdrop import make_run_preview_mime

__all__ = [
    '_PreviewDropEvent',
    '_assert_reader_artifact_invariant',
    '_build_two_heatmap_database',
    '_configure_temp_qplot',
    '_database_artifact_state',
    '_drop_heatmap',
    '_wait_for',
]


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


def _database_artifact_state(database_path):
    state = {}
    for suffix in ("", "-wal", "-journal"):
        path = Path(f"{database_path}{suffix}")
        if not path.exists():
            state[suffix] = None
            continue
        metadata = path.stat()
        state[suffix] = (
            metadata.st_size,
            metadata.st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    # SQLite may hold Windows byte-range locks on this permitted mutable file.
    # Its contents are not part of the invariant; observe type/identity only.
    try:
        state["-shm"] = Path(f"{database_path}-shm").lstat()
    except FileNotFoundError:
        state["-shm"] = None
    return state


def _assert_reader_artifact_invariant(database_path, original_artifacts):
    current_artifacts = _database_artifact_state(database_path)
    for suffix in ("", "-wal", "-journal"):
        assert current_artifacts[suffix] == original_artifacts[suffix]

    # The pinned trusted reader may use SQLite's exact colocated WAL shared-
    # memory file.  Its creation and contents are the sole viewing exception.
    shm = current_artifacts["-shm"]
    if shm is not None:
        assert stat.S_ISREG(shm.st_mode)
    original_shm = original_artifacts["-shm"]
    if original_shm is not None:
        assert shm is not None
        assert (shm.st_dev, shm.st_ino) == (original_shm.st_dev, original_shm.st_ino)


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


def _drop_heatmap(target, guid, parameter, *, axes_pending=False):
    event = _PreviewDropEvent(
        make_run_preview_mime(
            guid,
            parameter.name,
            None if axes_pending else parameter.depends_on_,
            axes_pending=axes_pending,
        )
    )
    assert target._handle_preview_drag_drop(event)
    assert event.accepted
    assert not event.ignored
    assert event.drop_action == QtCore.Qt.DropAction.CopyAction
