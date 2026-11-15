"""Shared fixtures and assertions for native integer transforms."""

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
    'integer_plots',
]


_UINT_MAX = 2**64 - 1


_INT_MIN, _INT_MAX = -(2**63), 2**63 - 1


_CASES = {
    "reported_uint_y": (
        np.arange(4), np.array([10, 8, 6, 4], dtype=np.uint64), [-2, -2, -2],
    ),
    "adjacent_uint_y": (
        np.arange(4), np.array([2**63 + n for n in (3, 2, 1, 0)], dtype=np.uint64),
        [-1, -1, -1],
    ),
    "adjacent_uint_max_y": (
        np.arange(4), np.array([_UINT_MAX - n for n in (0, 1, 2, 3)], dtype=np.uint64),
        [-1, -1, -1],
    ),
    "adjacent_signed_min_y": (
        np.arange(4), np.array([_INT_MIN + n for n in (3, 2, 1, 0)], dtype=np.int64),
        [-1, -1, -1],
    ),
    "adjacent_signed_max_y": (
        np.arange(4), np.array([_INT_MAX - n for n in (0, 1, 2, 3)], dtype=np.int64),
        [-1, -1, -1],
    ),
    "adjacent_descending_uint_x": (
        np.array([2**63 + n for n in (3, 2, 1, 0)], dtype=np.uint64),
        np.array([10., 8., 6., 4.]), [2, 2, 2],
    ),
    "uint_signed_boundary": (
        np.arange(4), np.array([2**63 + n for n in (-1, 0, 1, 2)], dtype=np.uint64),
        [1, 1, 1],
    ),
    "full_uint_range": (
        np.array([_UINT_MAX, 0, _UINT_MAX, 0], dtype=np.uint64),
        np.array([0, _UINT_MAX, 0, _UINT_MAX], dtype=np.uint64), [-1, -1, -1],
    ),
    "signed_y_overflow": (
        np.arange(4), np.array([_INT_MIN, _INT_MAX, _INT_MIN, _INT_MAX]),
        [float(_UINT_MAX), -float(_UINT_MAX), float(_UINT_MAX)],
    ),
    "signed_x_overflow": (
        np.array([_INT_MIN, _INT_MAX, _INT_MIN, _INT_MAX]),
        np.array([_INT_MAX, _INT_MIN, _INT_MAX, _INT_MIN]), [-1, -1, -1],
    ),
    "narrow_signed_overflow": (
        np.array([127, -128, 127, -128], dtype=np.int8),
        np.array([-128, 127, -128, 127], dtype=np.int8), [-1, -1, -1],
    ),
    "adjacent_signed_y_float_x": (
        np.arange(4, dtype=float), np.array([2**53 + n for n in (3, 2, 1, 0)]),
        [-1, -1, -1],
    ),
    "nonuniform_uint": (
        np.array([10, 8, 4, 3], dtype=np.uint64),
        np.array([10, 8, 6, 4], dtype=np.uint64), [1, .5, 2],
    ),
    "single_sample": (
        np.array([0], dtype=np.uint64), np.array([2**63], dtype=np.uint64), [],
    ),
}


@pytest.fixture
def integer_plots(tmp_path, monkeypatch, request):
    configure_temp_qplot(monkeypatch, tmp_path)
    case, heatmap = request.param
    x, y, slopes = _CASES[case]
    path = tmp_path / "integer-transforms.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("integer transforms", sample_name="test")
    guids = []
    try:
        for _ in range(2):
            measurement = Measurement(exp=experiment)
            measurement.register_custom_parameter("x", paramtype="array")
            if heatmap:
                measurement.register_custom_parameter("slow", paramtype="array")
            measurement.register_custom_parameter(
                "signal", paramtype="array", setpoints=("slow", "x") if heatmap else ("x",),
            )
            with measurement.run(write_in_background=False) as saver:
                if heatmap:
                    saver.add_result(
                        ("x", np.tile(x, (2, 1))),
                        ("slow", np.repeat(np.arange(2)[:, None], len(x), axis=1)),
                        ("signal", np.tile(y, (2, 1))),
                    )
                else:
                    saver.add_result(("x", x), ("signal", y))
            stored = saver.dataset.get_parameter_data("signal")["signal"]
            for name, original in (("x", x), ("signal", y)):
                assert stored[name].dtype == original.dtype
                np.testing.assert_array_equal(stored[name].ravel()[:len(x)], original)
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
            # Extreme-range cases test arithmetic, without rendering-only
            # clipping imposed by an unrelated viewbox's current range.
            plot.line.setDynamicRangeLimit(None)
            plots.append(plot)
        yield window, plots, x, y, slopes
    finally:
        close_main_window(window)
        assert database_state(path) == protected
