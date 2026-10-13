"""Registered differentiation remains cancellable while classifying objects."""

import builtins
from decimal import Decimal

import numpy as np
import pytest

import qplot.tools.plot_tools as plot_tools
from qplot.tools.operation_registry import PLOT_OPERATION_SPECS, OperationCall
from tests.test_worker_array_heatmaps import heatmap_dataset, make_worker


@pytest.mark.parametrize("numeric", [False, True])
def test_object_derivative_type_scan_checks_cancellation(monkeypatch, numeric):
    values = np.arange(8192).astype(object)
    if numeric:
        values[0] = Decimal("0.25")
    original = values.copy()
    consumed = 0
    observed_types = ((int, float, Decimal, plot_tools.Fraction, np.integer, np.floating)
                      if numeric else (int, np.integer))

    def instance(value, types):
        nonlocal consumed
        if types == observed_types:
            consumed += 1
        return builtins.isinstance(value, types)

    monkeypatch.setattr(plot_tools, "isinstance", instance, raising=False)
    with pytest.raises(InterruptedError, match="cancelled"):
        plot_tools.differentiate("x", {"x": np.arange(values.size), "y": values, "z": None},
                                 cancelled_callback=lambda: consumed >= 1024)
    assert consumed == 1024
    np.testing.assert_array_equal(values, original)


def test_real_mixed_worker_derivative_cancels_before_publication(tmp_path, monkeypatch, qapplication):
    values = np.arange(4096, dtype=np.int64)
    specification = next(spec for spec in PLOT_OPERATION_SPECS["plot2d"] if spec.name == "dz/dx")
    with heatmap_dataset(tmp_path, shaped=False, records=[
        (0, values, values), (1, values, values.astype(np.uint64)),
    ]) as dataset:
        worker = make_worker(dataset, operations=[OperationCall(
            specification.name, specification.func, cooperative=True)])
        consumed = 0

        def instance(value, types):
            nonlocal consumed
            if types == (int, np.integer):
                consumed += 1
                if consumed == 1024:
                    worker.cancel()
            return builtins.isinstance(value, types)

        monkeypatch.setattr(plot_tools, "isinstance", instance, raising=False)
        finished, errors = [], []
        worker.emitter.finished.connect(finished.append)
        worker.emitter.errorOccurred.connect(errors.append)
        worker.run()
        assert consumed == 1024
        assert finished == [False] and not errors
        assert not worker.running
        np.testing.assert_array_equal(worker.dataGrid, np.stack((values, values)))
