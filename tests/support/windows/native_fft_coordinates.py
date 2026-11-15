"""Shared fixtures and assertions for native fft coordinates."""

import numpy as np
import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.windows import main as main_window
from tests._window_lifecycle import close_main_window
from tests.support.windows.complex_line_data import database_state
from tests.support.windows.native_transform_labels import assert_display
from tests.support.windows.plot_integration import configure_temp_qplot, wait_for

__all__ = [
    'assert_spectrum',
    'fft_plots',
]


@pytest.fixture
def fft_plots(tmp_path, monkeypatch, request):
    configure_temp_qplot(monkeypatch, tmp_path)
    options = request.param if isinstance(request.param, dict) else {"x": request.param}
    heatmap = options.get("heatmap", False)
    scalar = options.get("scalar", False)
    coordinates = np.asarray(options["x"])
    values = np.asarray(options.get("y", [1., 17., 50., 82.]))[:len(coordinates)]
    path = tmp_path / "fft.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("native FFT", sample_name="test")
    guids = []
    try:
        traces = (
            (coordinates, values),
            (np.asarray(options.get("other_x", coordinates[::-1])),
             np.asarray(options.get("other_y", values[::-1]))),
        )
        for x, y in traces:
            measurement = Measurement(exp=experiment)
            paramtype = "numeric" if scalar else "array"
            measurement.register_custom_parameter("x", paramtype=paramtype, label="Coordinate", unit="V")
            if heatmap:
                measurement.register_custom_parameter("slow", paramtype="array")
            measurement.register_custom_parameter(
                "signal", paramtype=paramtype, setpoints=("slow", "x") if heatmap else ("x",),
                label="Signal", unit="A",
            )
            with measurement.run(write_in_background=False) as saver:
                if heatmap:
                    saver.add_result(
                        ("x", np.tile(x, (2, 1))),
                        ("slow", np.repeat(np.arange(2)[:, None], len(x), axis=1)),
                        ("signal", np.tile(y, (2, 1))),
                    )
                elif scalar:
                    for coordinate, value in zip(x, y, strict=True):
                        saver.add_result(("x", coordinate), ("signal", value))
                else:
                    saver.add_result(("x", x), ("signal", y))
            guids.append(saver.dataset.guid)
    finally:
        experiment.conn.close()
    protected = database_state(path)
    window = main_window.MainWindow()
    try:
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.close_database(status=False)
        assert window.load_file(str(path))
        wait_for(lambda: not window._database_load_active)
        plots = []
        for guid in guids:
            prior_count = len(window.windows)
            window.openPlot(guid=guid, show=True)
            wait_for(lambda prior_count=prior_count: len(window.windows) > prior_count)
            plot = window.windows[-1]
            wait_for(lambda plot=plot: hasattr(plot, "axis_data") and not plot.worker.running)
            plot.monitor.stop()
            if heatmap:
                plot.z_index = [0, 0]
                plot.openSweep("h")
                plot = window.windows[-1]
                wait_for(lambda plot=plot: hasattr(plot, "axis_data") and not plot.worker.running)
                plot.monitor.stop()
            plots.append(plot)
        yield window, plots, coordinates, values
    finally:
        close_main_window(window)
        assert database_state(path) == protected


def assert_spectrum(line, frequencies, magnitudes):
    assert_display(line, frequencies, magnitudes)
    assert_display(line.curve, frequencies, magnitudes)
