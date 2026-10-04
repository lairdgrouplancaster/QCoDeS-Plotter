"""Independent spectra for finite source cells across float64's range."""

import math
from fractions import Fraction

import numpy as np
import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.windows import main as main_window
from qplot.windows._native_transforms import (
    NativePlotDataItem,
    _interpolate_fft_samples,
    _normalized_real_fft,
)
from tests._window_lifecycle import close_main_window
from tests.windows.test_complex_line_data import database_state
from tests.windows.test_native_transform_labels import click_control
from tests.windows.test_native_transform_snapping import assert_full_snap_data
from tests.windows.test_plot_integration import configure_temp_qplot, wait_for


def _quarter_spectrum(values):
    """Four-point DFT uses only exact rational coefficients and one hypot."""
    a, b, c, d = map(lambda v: Fraction(float(v)), values)
    return np.array([abs(float((a + b + c + d) / 4)),
                     math.hypot(float((a - c) / 4), float((d - b) / 4)),
                     abs(float((a - b + c - d) / 4))])


def _interpolation_oracle(targets, x, y):
    result = []
    for target in targets:
        index = min(max(int(np.searchsorted(x, target, side="right")) - 1, 0), len(x) - 2)
        left, right = Fraction(float(x[index])), Fraction(float(x[index + 1]))
        before, after = Fraction(float(y[index])), Fraction(float(y[index + 1]))
        fraction = (Fraction(float(target)) - left) / (right - left)
        result.append(float((1 - fraction) * before + fraction * after))
    return np.array(result)


_TINY = np.nextafter(0., 1.)
_MAX = np.finfo(float).max
_CASES = [
    (np.arange(4.), np.array([1e308, 1e-100, -1e308, 1e-100]), [2], 5e-101),
    (np.arange(8.), np.array([1e-100, 1e308, 0, 0, -1e-100, 1e308, 0, 0]), [1, 3], 2.5e-101),
    (np.arange(8.), np.array([8 * _TINY, _MAX, 0, 0, -8 * _TINY, _MAX, 0, 0]),
     [1, 3], 2 * _TINY),
    (np.array([0., 2., 3., 4.]), np.array([_MAX, -_MAX, 1e-100, 1e-100]), [], None),
]


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("coordinates,values,bins,expected", _CASES)
def test_stored_dynamic_range_fft_native_control(
    tmp_path, monkeypatch, reverse, coordinates, values, bins, expected,
):
    configure_temp_qplot(monkeypatch, tmp_path)
    path = tmp_path / "dynamic-fft.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("FFT dynamic range", sample_name="test")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("x", paramtype="array")
    measurement.register_custom_parameter("signal", setpoints=("x",), paramtype="array")
    x, y = (coordinates[::-1], values[::-1]) if reverse else (coordinates, values)
    try:
        with measurement.run() as saver:
            saver.add_result(("x", x), ("signal", y))
            guid = saver.dataset.guid
        saver.dataset.conn.close()
    finally:
        experiment.conn.close()
    protected = database_state(path)
    window = main_window.MainWindow(startup_database_path=str(path))
    try:
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        assert window.load_file(str(path))
        wait_for(lambda: not window._database_load_active)
        window.openPlot(guid=guid, show=True)
        plot = window.windows[-1]
        wait_for(lambda: hasattr(plot, "axis_data") and not plot.worker.running)
        plot.monitor.stop()
        plot.line.setDynamicRangeLimit(None)
        original = tuple(v.copy() for v in plot.line.getOriginalDataset())
        np.testing.assert_array_equal(original[0], x)
        np.testing.assert_array_equal(original[1], y)
        click_control(plot, "fftCheck")
        for refresh in (False, True):
            if refresh:
                plot.refreshWindow(force=True)
                wait_for(lambda: not plot.worker.running)
                plot.monitor.stop()
            spectrum_x, spectrum_y = plot.line.getData()
            assert np.all(np.isfinite(spectrum_y))
            if bins:
                np.testing.assert_array_equal(spectrum_y[bins], [expected] * len(bins))
            else:
                uniform_x = np.linspace(coordinates[0], coordinates[-1], len(coordinates))
                resampled = _interpolation_oracle(uniform_x, coordinates, values)
                np.testing.assert_allclose(spectrum_y, _quarter_spectrum(resampled), rtol=2e-14, atol=0)
            np.testing.assert_array_equal(plot.line.curve.getData()[1], spectrum_y)
            assert_full_snap_data(plot.line, spectrum_x, spectrum_y, spectrum_x, spectrum_y)
            for current, recorded in zip(plot.line.getOriginalDataset(), original, strict=True):
                np.testing.assert_array_equal(current, recorded)
            np.testing.assert_array_equal(plot.worker.axis_data["y"], y)
        click_control(plot, "fftCheck")
        np.testing.assert_array_equal(plot.line.getData()[1], y)
    finally:
        close_main_window(window)
    assert database_state(path) == protected


@pytest.mark.parametrize("count", [3, 4, 5, 6, 7, 8, 10, 15, 16])
@pytest.mark.parametrize("opposing", [False, True])
def test_constant_and_opposing_extreme_spectra_remain_finite(count, opposing):
    values = np.full(count, _MAX)
    if opposing:
        values[1::2] = -_MAX
    direct = np.abs(_normalized_real_fft(values))
    assert np.all(np.isfinite(direct))
    line = NativePlotDataItem(x=np.arange(count), y=values, fftMode=True)
    observed = line.getData()[1]
    assert np.all(np.isfinite(observed))
    exact = list(map(lambda v: Fraction(float(v)), values))
    assert observed[0] == abs(float(sum(exact) / count))
    if opposing and count % 2 == 0:
        assert observed[-1] == pytest.approx(_MAX, rel=2e-15)
        assert direct[-1] == pytest.approx(_MAX, rel=2e-15)
    if not opposing:
        assert direct[0] == pytest.approx(_MAX, rel=2e-15)
        np.testing.assert_allclose(observed[1:], 0., atol=_MAX * 2e-15)


@pytest.mark.parametrize("values", [
    [_MAX, -_MAX, 1e-100, 1e-100], [-_MAX, _MAX, 1e-100, 1e-100],
    [1e308, -1e308, 0., 1e-100], [0., 1., 4., 9.],
])
def test_resampling_matches_recorded_coordinate_oracle(values):
    x = np.array([0., 2., 3., 4.])
    y = np.array(values)
    targets = np.linspace(0., 4., 4)
    observed = _interpolate_fft_samples(targets, x, y)
    expected = _interpolation_oracle(targets, x, y)
    np.testing.assert_allclose(observed, expected, rtol=2e-15, atol=0)
    np.testing.assert_array_equal(y, values)
