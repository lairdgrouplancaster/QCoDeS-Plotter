"""Check measurement labels through loaded plots and production draw specs."""

import numpy as np
import pytest
from PyQt6 import QtGui
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.parameters import ManualParameter

from qplot.windows import main as main_window
from tests._window_lifecycle import close_main_window
from tests.windows.test_plot_integration import configure_temp_qplot, wait_for


def rendered_labels(axis):
    image = QtGui.QImage(1200, 800, QtGui.QImage.Format.Format_ARGB32)
    painter = QtGui.QPainter(image)
    try:
        specs = axis.generateDrawSpecs(painter)
        assert specs is not None
        return [text for _rect, _flags, text in specs[2]]
    finally:
        painter.end()


@pytest.fixture
def measurement_plot(tmp_path, monkeypatch, request):
    configure_temp_qplot(monkeypatch, tmp_path)
    values = request.param
    database_path = tmp_path / "tick-labels.db"
    initialise_or_create_database_at(str(database_path), journal_mode="DELETE")
    experiment = load_or_create_experiment("tick_labels", sample_name="synthetic")
    x = ManualParameter("set_current", label="Set current", unit="A")
    y = ManualParameter("current", label="Current", unit="A")
    measurement = Measurement(exp=experiment)
    measurement.register_parameter(x)
    measurement.register_parameter(y, setpoints=(x,))
    with measurement.run(write_in_background=False) as datasaver:
        for value in values:
            datasaver.add_result((x, float(value)), (y, float(value)))
        dataset = datasaver.dataset
        guid = dataset.guid
    dataset.conn.close()
    experiment.conn.close()
    original = database_path.read_bytes(), database_path.stat().st_mtime_ns
    window = main_window.MainWindow()
    try:
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.close_database(status=False)
        assert window.load_file(str(database_path))
        wait_for(lambda: not window._database_load_active)
        window.openPlot(guid=guid, show=True)
        plot = window.windows[-1]
        wait_for(lambda: hasattr(plot, "axis_data") and not plot.worker.running)
        plot.monitor.stop()
        plot.resize(1000, 700)
        yield plot
    finally:
        close_main_window(window)
        assert (database_path.read_bytes(), database_path.stat().st_mtime_ns) == original
        assert not database_path.with_name(database_path.name + "-wal").exists()
        assert not database_path.with_name(database_path.name + "-journal").exists()


@pytest.mark.parametrize("measurement_plot, expected", [
    (np.arange(1, 10) * 1e-16, ["200", "400", "600", "800"]),
    (np.arange(-9, 0) * 1e-16, ["-800", "-600", "-400", "-200"]),
    (np.arange(-9, 10) * 1e-16, ["-500", "0", "500"]),
    (np.arange(-9, 10) * 1.0, ["-5", "0", "5"]),
], indirect=["measurement_plot"])
def test_default_measurement_tick_labels(measurement_plot, expected, qapplication):
    qapplication.processEvents()
    for side in ("bottom", "left"):
        axis = measurement_plot.plot.getAxis(side)
        if max(abs(value) for value in axis.range) < 1e-15:
            assert axis.autoSIPrefixScale == pytest.approx(1e18)
            assert "10<sup>-18</sup> A" in axis.labelString()
        assert rendered_labels(axis) == expected


@pytest.mark.parametrize("measurement_plot", [np.arange(-9, 10) * 1e-16],
                         indirect=True)
@pytest.mark.parametrize("zero", [-0.0, -4 * np.spacing(5e-16)])
def test_roundoff_at_zero_has_no_negative_zero_label(
        measurement_plot, zero, monkeypatch, qapplication):
    qapplication.processEvents()
    for side in ("bottom", "left"):
        axis = measurement_plot.plot.getAxis(side)
        original_tick_values = axis.tickValues

        def noisy_tick_values(*args, original=original_tick_values):
            # Model cancellation at the zero tick; keep the real tick lattice,
            # axis scaling, formatter, and drawing path for every other tick.
            return [
                (spacing, [zero if value == 0 else value for value in values])
                for spacing, values in original(*args)
            ]

        monkeypatch.setattr(axis, "tickValues", noisy_tick_values)
        assert rendered_labels(axis) == ["-500", "0", "500"]


@pytest.mark.parametrize("measurement_plot", [np.logspace(-18, -14, 17)],
                         indirect=True)
def test_logarithmic_measurement_tick_labels(measurement_plot, qapplication):
    for name in ("x", "y"):
        measurement_plot._axis_scale_log_toggled(name, True)
    measurement_plot.vb.setRange(xRange=[-18.5, -13.5],
                                 yRange=[-18.5, -13.5], padding=0)
    qapplication.processEvents()
    for side in ("bottom", "left"):
        axis = measurement_plot.plot.getAxis(side)
        assert axis.logMode
        assert axis.autoSIPrefixScale == pytest.approx(1e15)
        assert rendered_labels(axis) == [
            "0.001", "0.1", "10¹",
        ], side
