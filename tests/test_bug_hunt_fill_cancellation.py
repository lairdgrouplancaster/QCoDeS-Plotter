"""Heatmap gap filling checks cancellation while scanning measured samples."""

from decimal import Decimal

import numpy as np
import pytest

import qplot.tools.plot_tools as plot_tools
from qplot.tools.operation_registry import PLOT_OPERATION_SPECS, OperationCall
from tests.test_worker_array_heatmaps import heatmap_dataset, make_worker


@pytest.mark.parametrize("direction", ["right", "below"])
@pytest.mark.parametrize("kind", ["integer", "float", "object", "nan_run"])
def test_fill_cancels_within_a_long_line(monkeypatch, direction, kind):
    values = np.arange(8192, dtype=np.int64)
    if kind in {"float", "nan_run"}:
        values = values.astype(float)
    if kind == "nan_run":
        values[1:-1] = np.nan
    if kind == "object":
        values = values.astype(object)
        values[0] = Decimal("0.125")
    grid = values[None, :] if direction == "right" else values[:, None]
    original = grid.copy()
    scanned = 0
    original_isnan = plot_tools.numeric_isnan

    def observe(value):
        nonlocal scanned
        scanned += 1
        return original_isnan(value)

    monkeypatch.setattr(plot_tools, "numeric_isnan", observe)
    with pytest.raises(InterruptedError, match="cancelled"):
        plot_tools.fill_heatmap(direction, {"z": grid},
                                cancelled_callback=lambda: scanned >= 1024)
    # Gap detection repeats its initial predicate and evaluates the current
    # predicate before polling, adding at most two calls to the block.
    assert 1024 <= scanned <= 1026
    np.testing.assert_array_equal(grid, original)


@pytest.mark.parametrize("direction", ["right", "below"])
@pytest.mark.parametrize("objects", [False, True])
def test_fill_preserves_gap_limits_and_edges_across_poll_boundary(direction, objects):
    values = np.arange(3100, dtype=float)
    values[:2] = np.nan
    values[1023:1026] = np.nan
    values[2046:2050] = np.nan
    values[-2:] = np.nan
    values[1022] = np.inf
    if objects:
        values = values.astype(object)
        values[1022] = Decimal("0.125")
    expected = values.copy()
    expected[1023:1026] = expected[1022]
    grid = values[None, :] if direction == "right" else values[:, None]
    original = grid.copy()
    result = plot_tools.fill_heatmap(direction, {"z": grid}, max_depth=3)["z"]
    np.testing.assert_array_equal(np.asarray(result.ravel(), dtype=float),
                                  np.asarray(expected, dtype=float))
    if objects:
        assert result.ravel()[1024] == Decimal("0.125")
    missing = np.isnan(np.asarray(original, dtype=float))
    np.testing.assert_array_equal(np.isnan(np.asarray(grid, dtype=float)), missing)
    np.testing.assert_array_equal(grid[~missing], original[~missing])


def test_real_worker_fill_cancel_keeps_unmodified_grid(tmp_path, monkeypatch, qapplication):
    values = np.arange(8192, dtype=np.int64)
    specification = next(spec for spec in PLOT_OPERATION_SPECS["plot2d"] if spec.name == "Fill Right")
    operation = OperationCall(specification.name,
                              lambda data, cancelled_callback=None: specification.func(
                                  10, data, cancelled_callback=cancelled_callback), cooperative=True)
    with heatmap_dataset(tmp_path, shaped=False, records=[(0, values, values)]) as dataset:
        worker = make_worker(dataset, operations=[operation])
        scanned = 0
        original_isnan = plot_tools.numeric_isnan

        def observe(value):
            nonlocal scanned
            scanned += 1
            if scanned == 1024:
                worker.cancel()
            return original_isnan(value)

        monkeypatch.setattr(plot_tools, "numeric_isnan", observe)
        finished, errors = [], []
        worker.emitter.finished.connect(finished.append)
        worker.emitter.errorOccurred.connect(errors.append)
        worker.run()
        assert scanned == 1024
        assert finished == [False] and errors == []
        assert not worker.running
        np.testing.assert_array_equal(worker.dataGrid[0], values)
