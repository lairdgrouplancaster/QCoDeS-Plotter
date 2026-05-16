import time
from pathlib import Path

import numpy as np
from PyQt5 import QtCore
from PyQt5 import QtWidgets as qtw
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_by_id,
    load_or_create_experiment,
)
from qcodes.parameters import ManualParameter

from qplot.configuration.config import config
from qplot.windows import main as main_window


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
    initialise_or_create_database_at(str(db_path))
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


def dependent_parameter(dataset, dimensions):
    for param in dataset.get_parameters():
        if param.depends_on and len(param.depends_on_) == dimensions:
            return param
    raise AssertionError(f"No {dimensions}D dependent parameter in run {dataset.run_id}")


def close_main_window(window):
    window.startupDatabaseTimer.stop()
    window.monitor.stop()
    window.close_plot_windows(confirm=False, status=False)
    window.threadPool.waitForDone(1000)
    window.databaseLoadThreadPool.waitForDone(1000)
    window.hide()
    window.deleteLater()
    qtw.QApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
    qtw.QApplication.processEvents()


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
        window.openPlot(params=[line_param], show=False)
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
    finally:
        close_main_window(window)
