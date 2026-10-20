"""Mixed QCoDeS records retain finite samples beyond binary64's range."""

from decimal import Decimal
from fractions import Fraction
from functools import partial

import numpy as np
import pytest

from qplot.datahandling.parameter_data import numeric_isfinite
from qplot.tools.operation_registry import OperationCall
from qplot.tools.plot_tools import subtract_mean
from tests.test_worker_array_heatmaps import heatmap_dataset, make_worker, run_worker
from tests.windows.test_differentiation_integration import operation_option
from tests.windows.test_numeric_sample_preservation import array_plot
from tests.windows.test_plot_integration import wait_for

_EXTENDED_RANGE = np.finfo(np.longdouble).maxexp > np.finfo(float).maxexp


def _samples(sign=1):
    base = sign * np.longdouble("1e310")
    step = abs(np.spacing(base))
    return base + np.arange(-1, 2, dtype=np.longdouble) * step, float(step)


@pytest.mark.skipif(not _EXTENDED_RANGE, reason="Platform has no extended floating range")
@pytest.mark.parametrize("shaped", [False, True])
@pytest.mark.parametrize("sign", [-1, 1])
def test_real_mixed_records_keep_finite_heatmap_cells_and_centering(
    tmp_path, shaped, sign,
):
    signal, step = _samples(sign)
    records = [(0, [0, 1, 2], signal), (1, [0, 1, 2], np.array([1, 2, 3], dtype=np.int64))]
    with heatmap_dataset(tmp_path, shaped=shaped, records=records) as dataset:
        worker = make_worker(dataset)
        run_worker(worker)
        np.testing.assert_array_equal(worker.axis_data["x"], [0, 1, 2])
        np.testing.assert_array_equal(worker.axis_data["y"], [0, 1])
        np.testing.assert_array_equal(worker.dataGrid[0], signal)
        np.testing.assert_array_equal(worker.dataGrid[1], [1, 2, 3])
        operation = OperationCall("Subtract Row Mean", partial(subtract_mean, "x"), cooperative=True)
        centered = make_worker(dataset, operations=[operation])
        run_worker(centered)
        np.testing.assert_array_equal(centered.dataGrid, [[-step, 0., step], [-1., 0., 1.]])
        # The operation works privately; the original loaded cells stay exact.
        np.testing.assert_array_equal(worker.dataGrid[0], signal)


@pytest.mark.skipif(not _EXTENDED_RANGE, reason="Platform has no extended floating range")
@pytest.mark.parametrize("mixed", [False, True])
def test_bounded_float64_mean_route_still_refuses_unrepresentable_result(tmp_path, mixed):
    signal, _step = _samples()
    records = [(0, [0, 1, 2], signal),
               (1, [0, 1, 2], np.array([1, 2, 3], dtype=np.int64 if mixed else np.longdouble))]
    with heatmap_dataset(tmp_path, shaped=False, records=records) as dataset:
        worker = make_worker(dataset, force_sql_heatmap=True)
        finished, errors = [], []
        worker.emitter.finished.connect(finished.append)
        worker.emitter.errorOccurred.connect(errors.append)
        worker.run()
        assert finished == [False]
        assert len(errors) == 1 and isinstance(errors[0], OverflowError)
        assert worker._sql_connection is None


@pytest.mark.skipif(not _EXTENDED_RANGE, reason="Platform has no extended floating range")
@pytest.mark.parametrize("shaped", [False, True])
def test_real_heatmap_control_recovers_visible_mixed_extended_residuals(tmp_path, monkeypatch, shaped):
    signal, step = _samples()
    records = [
        {"x": np.arange(3), "slow": np.zeros(3), "signal": signal},
        {"x": np.arange(3), "slow": np.ones(3), "signal": np.array([1, 2, 3], dtype=np.int64)},
    ]
    with array_plot(tmp_path, monkeypatch, records, shape=(2, 3) if shaped else None,
                    heatmap=True) as (_window, host):
        np.testing.assert_array_equal(host.dataGrid[0], signal)
        operation_option(host, "Subtract Row Mean").input.setChecked(True)
        previous = host.worker
        host.oper_widget.apply_but.click()
        wait_for(lambda: host.worker is not previous and not host.worker.running)
        host.monitor.stop()
        np.testing.assert_array_equal(host.dataGrid, [[-step, 0., step], [-1., 0., 1.]])
        assert not host._heatmap_color_range_rejected


@pytest.mark.parametrize("shape", [(9,), (3, 3)])
def test_object_finiteness_retains_exact_numbers_and_special_values(shape):
    values = np.array([Decimal("1e1000"), Decimal("-1e1000"), Fraction(10**1000),
                       10**1000, np.uint64(2**64 - 1), Decimal("Infinity"),
                       Decimal("-Infinity"), Decimal("NaN"), np.nan], dtype=object).reshape(shape)
    original = values.copy()
    np.testing.assert_array_equal(numeric_isfinite(values),
                                  np.array([True] * 5 + [False] * 4).reshape(shape))
    assert all(now is old for now, old in zip(values.flat, original.flat, strict=True))


@pytest.mark.skipif(not _EXTENDED_RANGE, reason="Platform has no extended floating range")
def test_object_finiteness_does_not_narrow_extended_exponents():
    values = np.array([np.longdouble("1e310"), np.longdouble("-1e310"),
                       np.nextafter(np.longdouble(0), np.longdouble(1)),
                       np.longdouble("inf"), np.longdouble("-inf"), np.longdouble("nan"),
                       0, 1.5, np.uint64(2**64 - 1)], dtype=object)
    np.testing.assert_array_equal(numeric_isfinite(values), [True] * 3 + [False] * 3 + [True] * 3)


@pytest.mark.parametrize("value", ["not numeric", object(), 1 + 2j])
def test_nonnumeric_or_complex_objects_are_not_silently_accepted(value):
    with pytest.raises((TypeError, ValueError)):
        numeric_isfinite(np.array([1, value], dtype=object))


@pytest.mark.parametrize("dtype", [float, np.int64, np.longdouble, object])
def test_ordinary_finiteness_keeps_scalar_empty_and_array_shapes(dtype):
    for values in (np.asarray(2, dtype=dtype), np.empty((2, 0), dtype=dtype),
                   np.array([[1, 2], [-3, 0]], dtype=dtype)):
        actual = numeric_isfinite(values)
        assert actual.shape == values.shape
        np.testing.assert_array_equal(actual, np.ones(values.shape, dtype=bool))
