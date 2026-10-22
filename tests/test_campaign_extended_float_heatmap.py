"""Extended QCoDeS array heatmaps use exact cells instead of pandas narrowing."""

from decimal import Decimal, localcontext
from fractions import Fraction

import numpy as np
import pytest

from tests.test_worker_array_heatmaps import heatmap_dataset, make_worker, run_worker
from tests.windows.test_differentiation_integration import (
    apply_operations,
    operation_option,
)
from tests.windows.test_native_transform_labels import (
    no_callback_errors as _no_callback_errors,
)
from tests.windows.test_numeric_sample_preservation import array_plot
from tests.windows.test_plot_csv_precision import _export_from_dialog, _open_csv_dialog

no_callback_errors = _no_callback_errors


@pytest.mark.parametrize("dtype", [np.float32, np.float64, np.longdouble, np.int64])
@pytest.mark.parametrize("force_sql", [False, True])
@pytest.mark.parametrize("negative", [False, True])
def test_real_unshaped_array_heatmap_cell_means(tmp_path, dtype, force_sql, negative):
    sign = -1 if negative else 1
    records = [
        (0, [0, 0, 1, np.nan], np.array([5, 7, sign * 3, 99], dtype=dtype)),
        (1, [0, 1, np.nan], np.array([11, 13, 17], dtype=dtype)),
        (0, [1, 2], np.array([sign, 19], dtype=dtype)),
    ]
    with heatmap_dataset(tmp_path, shaped=False, records=records) as dataset:
        worker = make_worker(dataset, force_sql_heatmap=force_sql)
        run_worker(worker)
        np.testing.assert_array_equal(worker.axis_data["x"], [0, 1, 2])
        np.testing.assert_array_equal(worker.axis_data["y"], [0, 1])
        np.testing.assert_array_equal(
            np.asarray(worker.dataGrid, dtype=float), [[6, sign * 2, 19], [11, 13, np.nan]],
        )


@pytest.mark.parametrize("mixed", [False, True])
def test_real_unshaped_extended_cells_retain_single_samples_and_duplicate_residuals(tmp_path, mixed):
    epsilon = np.finfo(np.longdouble).eps
    first = np.longdouble(1) + np.array([1, 3, 4], dtype=np.longdouble) * epsilon
    second = np.array([2, 3], dtype=np.int64 if mixed else np.longdouble)
    records = [(0, [0, 0, 1], first), (1, [0, 1], second)]
    with heatmap_dataset(tmp_path, shaped=False, records=records) as dataset:
        worker = make_worker(dataset)
        run_worker(worker)
        exact = [Fraction(*value.as_integer_ratio()) for value in first]
        expected = [(exact[0] + exact[1]) / 2, exact[2]]
        with localcontext() as context:
            context.prec = 100
            expected_values = [np.longdouble(str(Decimal(value.numerator) / Decimal(value.denominator)))
                               for value in expected]
        np.testing.assert_array_equal(
            np.array([str(value) for value in worker.dataGrid[0]], dtype=np.longdouble),
            expected_values,
        )
        np.testing.assert_array_equal(np.asarray(worker.dataGrid[1], dtype=float), [2, 3])
        assert np.unique(worker.dataGrid).size == 4


def test_mixed_extended_heatmap_plot_export_and_processing(tmp_path, monkeypatch, no_callback_errors):
    epsilon = np.finfo(np.longdouble).eps
    first = np.longdouble(1) + np.array([1, 3, 4], dtype=np.longdouble) * epsilon
    records = [
        {"slow": np.zeros(3), "x": np.array([0., 0., 1.]), "signal": first},
        {"slow": np.ones(2), "x": np.array([0., 1.]), "signal": np.array([2, 3], dtype=np.int64)},
    ]
    with array_plot(tmp_path, monkeypatch, records, heatmap=True) as (_window, host):
        expected_first = np.longdouble(1) + np.array([2, 4], dtype=np.longdouble) * epsilon
        np.testing.assert_array_equal(
            np.array([str(value) for value in host.dataGrid[0]], dtype=np.longdouble), expected_first,
        )
        dialog = _open_csv_dialog(host)
        rows = _export_from_dialog(monkeypatch, host, dialog, tmp_path / "extended-grid.csv", ",")
        np.testing.assert_array_equal(
            np.array([row[2] for row in rows[1:3]], dtype=np.longdouble), expected_first,
        )
        operation_option(host, "Subtract Row Mean").input.setChecked(True)
        assert apply_operations(host)[1:] == ([True], [])
        np.testing.assert_allclose(
            np.asarray(host.dataGrid, dtype=float), [[-float(epsilon), float(epsilon)], [-.5, .5]],
            rtol=1e-15, atol=0,
        )
