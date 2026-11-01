"""Large exact heatmap means honour cancellation during every Python phase."""

import builtins
from decimal import Decimal
from fractions import Fraction
from math import isnan

import numpy as np
import pytest

import qplot.tools.plot_tools as plot_tools
from qplot.tools.operation_registry import PLOT_OPERATION_SPECS, OperationCall
from tests.test_worker_array_heatmaps import heatmap_dataset, make_worker


@pytest.mark.parametrize("axis", ["x", "y"])
@pytest.mark.parametrize("objects", [False, True])
@pytest.mark.parametrize("phase", ["convert", "sum", "residual"])
def test_exact_mean_checks_cancellation_within_each_phase(monkeypatch, axis, objects, phase):
    values = np.arange(8192, dtype=np.uint64) + np.uint64(2**63)
    if objects:
        values = values.astype(object)
        values[1] = Decimal("0.25")
        values[-1] = np.nan
    grid = values[None, :] if axis == "x" else values[:, None]
    original = grid.copy()
    consumed = 0

    def observe(value):
        nonlocal consumed
        consumed += 1
        return value

    if phase == "convert":
        original_fraction = plot_tools.sample_fraction
        monkeypatch.setattr(plot_tools, "sample_fraction", lambda value: observe(original_fraction(value)))
    elif phase == "sum":
        monkeypatch.setattr(plot_tools, "sum", lambda values: builtins.sum(observe(value) for value in values),
                            raising=False)
    else:
        original_float = plot_tools._fraction_as_float
        monkeypatch.setattr(plot_tools, "_fraction_as_float", lambda value: observe(original_float(value)))

    with pytest.raises(InterruptedError, match="cancelled"):
        plot_tools.subtract_mean(axis, {"z": grid}, cancelled_callback=lambda: consumed >= 1024)
    assert consumed == 1024
    assert grid.tolist() == original.tolist()


@pytest.mark.parametrize("axis", ["x", "y"])
@pytest.mark.parametrize("objects", [False, True])
def test_chunked_exact_mean_preserves_results_and_holes(axis, objects):
    values = np.arange(3073, dtype=np.uint64) + np.uint64(2**63)
    if objects:
        values = values.astype(object)
        values[1] = Decimal("0.25")
        values[-1] = np.nan
    grid = values[None, :] if axis == "x" else values[:, None]
    finite = [Fraction(value.item() if isinstance(value, np.generic) else value)
              for value in values if not isnan(value)]
    mean = sum(finite) / len(finite)
    expected = [float(value - mean) for value in finite]
    result = plot_tools.subtract_mean(axis, {"z": grid})["z"].ravel()
    np.testing.assert_array_equal(result[~np.isnan(result)], expected)
    assert np.isnan(result[-1]) == objects


def test_real_worker_cancel_during_exact_mean_does_not_publish(tmp_path, monkeypatch, qapplication):
    values = np.arange(8192, dtype=np.int64)
    mean = next(spec for spec in PLOT_OPERATION_SPECS["plot2d"] if spec.name == "Subtract Row Mean")
    with heatmap_dataset(tmp_path, shaped=False, records=[(0, values, values)]) as dataset:
        worker = make_worker(dataset, operations=[OperationCall(mean.name, mean.func, cooperative=True)])
        converted = 0
        original_fraction = plot_tools.sample_fraction

        def convert(value):
            nonlocal converted
            converted += 1
            if converted == 1024:
                worker.cancel()
            return original_fraction(value)

        monkeypatch.setattr(plot_tools, "sample_fraction", convert)
        finished, errors = [], []
        worker.emitter.finished.connect(finished.append)
        worker.emitter.errorOccurred.connect(errors.append)
        worker.run()
        assert converted == 1024
        assert finished == [False] and errors == []
        assert not worker.running
        np.testing.assert_array_equal(worker.dataGrid[0], values)
