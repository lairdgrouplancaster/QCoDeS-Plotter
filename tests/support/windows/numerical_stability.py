"""Shared fixtures and assertions for numerical stability."""

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
from tests.support.windows.plot_integration import configure_temp_qplot, wait_for

__all__ = [
    'measured_plots',
]


@pytest.fixture
def measured_plots(tmp_path, monkeypatch, request):
    configure_temp_qplot(monkeypatch, tmp_path)
    x, y, count = request.param
    heatmap = y.ndim == 2
    path = tmp_path / "numerical.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("numerical audit", sample_name="test")
    guids = []
    try:
        for _ in range(count):
            measurement = Measurement(exp=experiment)
            measurement.register_custom_parameter(
                "x", paramtype="array", label="Coordinate", unit="V"
            )
            if heatmap:
                measurement.register_custom_parameter("slow", paramtype="array")
                measurement.set_shapes({"signal": y.shape})
            measurement.register_custom_parameter(
                "signal",
                paramtype="array",
                setpoints=("slow", "x") if heatmap else ("x",),
                label="Signal",
                unit="A",
            )
            with measurement.run(write_in_background=False) as saver:
                if heatmap:
                    saver.add_result(
                        ("x", np.tile(x, (len(y), 1))),
                        ("slow", np.repeat(np.arange(len(y))[:, None], len(x), axis=1)),
                        ("signal", y),
                    )
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
            prior_plot_count = len(window.windows)
            window.openPlot(guid=guid, show=True)
            wait_for(lambda prior_plot_count=prior_plot_count: len(window.windows) > prior_plot_count)
            host = window.windows[-1]
            wait_for(
                lambda host=host: hasattr(host, "axis_data") and not host.worker.running
            )
            host.monitor.stop()
            if heatmap:
                np.testing.assert_array_equal(host.dataGrid, y)
            else:
                host.line.setDynamicRangeLimit(None)
                np.testing.assert_array_equal(host.axis_data["y"], y)
            plots.append(host)
        yield window, plots, x, y
    finally:
        close_main_window(window)
        assert database_state(path) == protected
