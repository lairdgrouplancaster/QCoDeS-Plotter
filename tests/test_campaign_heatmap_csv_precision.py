"""CSV without dtype metadata retains the exact acquired floating values."""

import csv
import io

import numpy as np
import pytest

from qplot.windows._plot_actions import PlotActionsMixin
from tests.test_worker_array_heatmaps import heatmap_dataset
from tests.windows.test_numeric_sample_preservation import array_plot
from tests.windows.test_plot_csv_precision import _export_from_dialog, _open_csv_dialog


def _samples(dtype):
    return np.array([
        dtype("0.1"), dtype("1.1"), dtype(1) + np.finfo(dtype).eps,
        np.finfo(dtype).tiny, np.nextafter(dtype(0), dtype(1)), dtype("-0"),
    ], dtype=dtype)


def _import_dtype(dtype):
    return np.longdouble if dtype is np.longdouble else np.float64


@pytest.mark.parametrize("dtype", [np.float16, np.float32, np.float64, np.longdouble])
@pytest.mark.parametrize("shaped", [False, True])
def test_actual_heatmap_export_dialog_preserves_float_samples(
    tmp_path, monkeypatch, dtype, shaped,
):
    signal = _samples(dtype)
    records = [
        {"x": np.arange(signal.size), "slow": np.full(signal.size, row),
         "signal": signal.copy()}
        for row in range(2)
    ]
    shape = (2, signal.size) if shaped else None
    with array_plot(tmp_path, monkeypatch, records, shape=shape, heatmap=True) as (window, plot):
        expected = np.tile(signal, (2, 1)).astype(_import_dtype(dtype))
        np.testing.assert_array_equal(plot.dataGrid, expected)
        dialog = _open_csv_dialog(plot)
        for suffix, separator, delimiter in (("csv", "comma", ","), ("tsv", "tab", "\t")):
            dialog.currentExporter.params["separator"] = separator
            rows = _export_from_dialog(monkeypatch, plot, dialog, tmp_path / f"heat.{suffix}", delimiter)
            recovered = np.asarray([row[2] for row in rows[1:]], dtype=_import_dtype(dtype)).reshape(expected.shape)
            # A normal reader imports untyped CSV as binary64. Recovering the
            # source value must not require knowing its original NumPy dtype.
            np.testing.assert_array_equal(recovered, expected)
            np.testing.assert_array_equal(np.signbit(recovered), np.signbit(plot.dataGrid))
            np.testing.assert_array_equal(plot.dataGrid, expected)

        destination = tmp_path / "raw.csv"
        monkeypatch.setattr(window, "_choose_csv_export_filename", lambda _default: str(destination))
        window._export_measurement_csv(plot._dataset_key, ["signal"])
        with destination.open(newline="", encoding="utf-8") as exported:
            raw_rows = list(csv.DictReader(exported))
        recovered = np.asarray([row["signal"] for row in raw_rows], dtype=_import_dtype(dtype))
        np.testing.assert_array_equal(recovered, expected.ravel())
        np.testing.assert_array_equal(np.signbit(recovered), np.signbit(expected.ravel()))


@pytest.mark.parametrize("dtype", [np.float16, np.float32, np.float64, np.longdouble])
def test_raw_measurement_csv_preserves_narrow_float_columns(tmp_path, dtype):
    signal = _samples(dtype)
    records = [(0, np.arange(signal.size), signal), (1, np.arange(signal.size), signal)]
    with heatmap_dataset(tmp_path, shaped=True, planned_shape=(2, signal.size), records=records) as dataset:
        params = {parameter.name: parameter for parameter in dataset.get_parameters()}
        frame = PlotActionsMixin._measurement_dataframe(None, dataset, [params["signal"]])
        rows = list(csv.DictReader(io.StringIO(frame.to_csv(index=False))))
        recovered = np.asarray([row["signal"] for row in rows], dtype=_import_dtype(dtype))
        expected = np.tile(signal, 2).astype(_import_dtype(dtype))
        np.testing.assert_array_equal(recovered, expected)
        np.testing.assert_array_equal(np.signbit(recovered), np.signbit(expected))
        recorded = dataset.get_parameter_data("signal")["signal"]["signal"]
        assert recorded.dtype == np.dtype(dtype)
        np.testing.assert_array_equal(recorded.ravel(), np.tile(signal, 2))
