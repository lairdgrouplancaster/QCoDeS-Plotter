"""Shared fixtures and assertions for plot integration."""

import csv
import hashlib
import sqlite3
import time
from pathlib import Path

import numpy as np
from PyQt6 import QtWidgets as qtw
from pyqtgraph.exporters import CSVExporter
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.parameters import ManualParameter

from qplot.configuration.config import config
from qplot.datahandling import database as database_module
from qplot.datahandling.trusted_live import TrustedLiveReaderUnavailableError
from qplot.testdata import (
    AxisSpecification,
    MeasurementSpecification,
    RunSpecification,
    generate_database,
)

__all__ = [
    'append_nonuniform_heatmap_run',
    'assert_database_artifacts_unchanged_except_trusted_shm',
    'build_line_database',
    'build_missing_cell_array_database',
    'build_synthetic_database',
    'configure_temp_qplot',
    'csv_rows',
    'database_artifact_state',
    'export_real_plot_csv',
    'force_snapshot_fallback',
    'prepare_generated_database_for_live_writes',
    'trusted_database_artifact_state',
    'wait_for',
]


def configure_temp_qplot(monkeypatch, tmp_path):
    qplot_home = tmp_path / ".qplot"
    monkeypatch.setattr(config, "default_path", str(qplot_home))
    monkeypatch.setattr(
        config, "default_file", str(qplot_home / config.config_file_name)
    )


def wait_for(predicate, timeout=12):
    app = qtw.QApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            app.processEvents()
            # A queued refresh can replace the publication during this drain.
            # Return only while the state the caller will inspect still holds.
            if predicate():
                return
        time.sleep(0.03)
    raise AssertionError("Timed out waiting for GUI integration state")


class _UnavailableTrustedRequest:
    """Force snapshot-only regressions through the documented fallback seam."""

    @staticmethod
    def wait():
        raise TrustedLiveReaderUnavailableError(
            "native reader unavailable in snapshot-only integration test"
        )

    @staticmethod
    def cancel():
        return True


def force_snapshot_fallback(monkeypatch):
    """Make subsequently created load services take the exact fallback path."""

    monkeypatch.setattr(
        database_module.TrustedLiveReadService,
        "submit_bootstrap",
        lambda _service, *, deadline=None: _UnavailableTrustedRequest(),
    )


def build_synthetic_database(db_path):
    initialise_or_create_database_at(str(db_path), journal_mode="DELETE")
    experiment = load_or_create_experiment("qplot_integration", sample_name="synthetic")

    try:
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
    finally:
        experiment.conn.close()


def append_nonuniform_heatmap_run():
    experiment = load_or_create_experiment(
        "qplot_csv_export",
        sample_name="nonuniform",
    )
    try:
        gate = ManualParameter("offset_gate", label="Offset gate", unit="V")
        bias = ManualParameter("uneven_bias", label="Uneven bias", unit="mV")
        signal = ManualParameter("mapped_signal", label="Mapped signal", unit="uS")
        measurement = Measurement(exp=experiment, name="nonuniform_csv_export")
        measurement.register_parameter(gate)
        measurement.register_parameter(bias)
        measurement.register_parameter(signal, setpoints=(gate, bias))

        missing_coordinate = (-0.25, 13.0)
        with measurement.run() as datasaver:
            for gate_value in (-2.0, -0.25, 4.5):
                for bias_value in (7.0, 13.0, 28.0):
                    if (gate_value, bias_value) == missing_coordinate:
                        continue
                    datasaver.add_result(
                        (gate, gate_value),
                        (bias, bias_value),
                        (signal, gate_value * 1000.0 + bias_value),
                    )

            return datasaver.dataset.run_id, missing_coordinate
    finally:
        experiment.conn.close()


def build_missing_cell_array_database(db_path):
    initialise_or_create_database_at(str(db_path), journal_mode="DELETE")
    experiment = load_or_create_experiment(
        "qplot_missing_array_cells",
        sample_name="synthetic",
    )
    try:
        measurement = Measurement(exp=experiment, name="missing_array_cells")
        measurement.register_custom_parameter("slow")
        measurement.register_custom_parameter("fast", paramtype="array")
        measurement.register_custom_parameter(
            "signal",
            setpoints=("slow", "fast"),
            paramtype="array",
        )
        with measurement.run() as datasaver:
            datasaver.add_result(
                ("slow", 0.0),
                ("fast", np.zeros(5)),
                ("signal", np.full(5, 10.0)),
            )
            datasaver.add_result(
                ("slow", 1.0),
                ("fast", np.ones(5)),
                ("signal", np.full(5, 20.0)),
            )
            return datasaver.dataset.run_id
    finally:
        experiment.conn.close()


def export_real_plot_csv(monkeypatch, plot_window, target):
    """Run qPlot's real exporter entry point with only the dialogs automated."""
    monkeypatch.setattr(
        qtw.QFileDialog,
        "getSaveFileName",
        lambda *_args, **_kwargs: (str(target), ""),
    )
    monkeypatch.setattr(
        qtw.QMessageBox,
        "question",
        lambda *_args, **_kwargs: qtw.QMessageBox.StandardButton.Yes,
    )
    accepted = plot_window._export_pyqtgraph_exporter(CSVExporter(plot_window.plot))
    if accepted:
        tasks = tuple(getattr(plot_window, "_export_tasks", ()))
        wait_for(lambda: all(task.done for task in tasks))
        return all(task.error is None and not task.cancelled for task in tasks)
    return False


def csv_rows(csv_path):
    with csv_path.open(newline="", encoding="utf-8") as csv_file:
        return list(csv.reader(csv_file))


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


def prepare_generated_database_for_live_writes(db_path):
    """Install qPlot lineage before a test starts a real live WAL writer."""
    generate_database(
        [
            RunSpecification(
                (
                    MeasurementSpecification(
                        "lineage_seed",
                        "Lineage seed",
                        "V",
                        (
                            AxisSpecification(
                                "V_SD", "Source-drain voltage", "V", 0.0, 0.0, 1
                            ),
                        ),
                    ),
                )
            )
        ],
        db_path,
    )


def database_artifact_state(database_path, *, hash_shm=True):
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
            hashlib.sha256(path.read_bytes()).hexdigest()
            if suffix != "-shm" or hash_shm else None,
        )
    return state


def trusted_database_artifact_state(database_path):
    """Hash protected files without reading SQLite's live SHM lock bytes.

    Windows enforces the SHM byte-range locks against ordinary file reads.
    Trusted viewing permits SHM content changes; only its presence is checked.
    Snapshot-only tests keep their strict SHM hashes via database_artifact_state.
    """
    return database_artifact_state(database_path, hash_shm=False)


def assert_database_artifacts_unchanged_except_trusted_shm(
    database_path,
    expected,
    *,
    state_reader=trusted_database_artifact_state,
):
    """Keep every source artifact exact except SQLite's permitted WAL SHM."""

    current = state_reader(database_path)
    for suffix in ("", "-wal", "-journal"):
        assert current[suffix] == expected[suffix]

    if expected["-shm"] is not None:
        assert current["-shm"] is not None
    if current["-shm"] is not None:
        assert Path(f"{database_path}-shm").is_file()
