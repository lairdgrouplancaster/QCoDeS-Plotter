"""Duplicate heatmap cells stay cancellable while retaining exact measurements."""

from decimal import Decimal
from fractions import Fraction

import numpy as np
import pytest

import qplot.tools.general as general
from tests.test_worker_array_heatmaps import heatmap_dataset, make_worker


@pytest.mark.parametrize("kind", ["signed", "unsigned", "mixed"])
def test_duplicate_mean_cancellation_bounds_consumed_samples(monkeypatch, kind):
    values = np.arange(8192, dtype=np.int64) + 2**60
    if kind == "unsigned":
        values = values.astype(np.uint64) + np.uint64(2**63)
    elif kind == "mixed":
        values = values.astype(object)
        values[1] = Decimal("0.125")
    original = values.copy()
    consumed = 0
    original_fraction = general.sample_fraction

    def convert(value):
        nonlocal consumed
        consumed += 1
        return original_fraction(value)

    def cancel():
        if consumed >= 1024:
            raise InterruptedError("cancelled")

    monkeypatch.setattr(general, "sample_fraction", convert)
    with pytest.raises(InterruptedError, match="cancelled"):
        general.data2matrix(np.zeros(values.size), np.zeros(values.size), values,
                            check_cancelled=cancel)
    assert consumed == 1024
    np.testing.assert_array_equal(values, original)


@pytest.mark.parametrize("kind", ["signed", "unsigned", "mixed", "extended"])
def test_duplicate_mean_retains_exact_offset_fraction_and_missing_cells(kind):
    values = np.arange(3074, dtype=np.int64) + 2**60
    if kind == "unsigned":
        values = values.astype(np.uint64) + np.uint64(2**63)
    elif kind in {"mixed", "extended"}:
        values = values.astype(object)
        values[1] = Decimal("0.125") if kind == "mixed" else np.longdouble("0.125")
    original = values.copy()
    rows = np.zeros(values.size)
    columns = np.zeros(values.size)
    rows[-1] = columns[-1] = 1
    grid = general.data2matrix(rows, columns, values).to_numpy()
    exact = [Fraction(*value.as_integer_ratio()) if isinstance(value, np.floating)
             else Fraction(value.item() if isinstance(value, np.generic) else value)
             for value in values[:-1]]
    expected = sum(exact) / len(exact)
    # Decimal publication may round the rational's repeating tail, but must
    # retain the recorded large integer offset and fractional residual.
    assert abs(Fraction(grid[0, 0]) - expected) < Fraction(1, 10**16)
    assert grid[1, 1] == values[-1]
    assert np.isnan(grid[0, 1]) and np.isnan(grid[1, 0])
    np.testing.assert_array_equal(values, original)


def test_real_worker_duplicate_mean_cancel_does_not_publish(tmp_path, monkeypatch, qapplication):
    values = np.arange(8192, dtype=np.int64)
    with heatmap_dataset(tmp_path, shaped=False,
                         records=[(0, np.zeros(values.size), values)]) as dataset:
        worker = make_worker(dataset)
        consumed = 0
        original_fraction = general.sample_fraction

        def convert(value):
            nonlocal consumed
            consumed += 1
            if consumed == 1024:
                worker.cancel()
            return original_fraction(value)

        monkeypatch.setattr(general, "sample_fraction", convert)
        finished, errors = [], []
        worker.emitter.finished.connect(finished.append)
        worker.emitter.errorOccurred.connect(errors.append)
        worker.run()
        assert consumed == 1024
        assert finished == [False] and errors == []
        assert not worker.running
        assert not hasattr(worker, "dataGrid")
