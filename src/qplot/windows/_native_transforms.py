"""qPlot's coordinate-safe mapping for PyQtGraph's native line controls."""

from fractions import Fraction

import numpy as np
import pyqtgraph as pg
from pyqtgraph.graphicsItems.PlotDataItem import PlotDataset

from qplot.datahandling.parameter_data import numeric_isfinite
from qplot.tools.plot_tools import _center_float_samples, _integer_differences
from qplot.tools.sample_statistics import finite_mean


def _safe_difference(values: np.ndarray) -> np.ndarray:
    """Subtract samples before narrowing arithmetic can overflow or wrap."""
    if values.dtype.kind == "b":
        # NumPy's Boolean diff reports changes, losing their direction.
        return np.diff(values.astype(np.int8)).astype(np.float64)
    if values.dtype.kind in "iu":
        # Narrow integers fit in int64 even after subtraction. For 64-bit
        # integers, Python integers cover the full signed/unsigned difference
        # range. Converting samples to float first would erase adjacent steps
        # above 2**53; casting uint64 to int64 would wrap at the signed boundary.
        dtype = np.int64 if values.dtype.itemsize < 8 else object
        return np.diff(values.astype(dtype)).astype(np.float64)
    if values.dtype.kind == "O":
        # Limit operations retain integers alongside fractional clipped cells.
        # Subtract adjacent integer cells before their float view is made.
        return _integer_differences(values)
    if values.dtype.kind == "f" and values.dtype.itemsize < 8:
        # np.diff retains float16/float32, so even finite operands can
        # overflow before the derivative divides or FFT validates the span.
        return np.diff(values.astype(np.float64))
    return np.diff(values)


def _center_integer_samples(values: np.ndarray) -> np.ndarray:
    """Center integer samples before float conversion can erase their steps."""
    if not len(values):
        return np.array([], dtype=np.float64)
    # Python integers avoid both uint64 wraparound and int64 overflow. Using
    # the exact numerator also retains fractional means above float's integer
    # precision (for example, four adjacent samples near 2**63).
    total = sum(map(int, values))
    count = len(values)
    return np.fromiter(
        ((count * int(value) - total) / count for value in values),
        dtype=np.float64, count=count,
    )


def _center_object_samples(values: np.ndarray) -> np.ndarray:
    """Keep integer steps when a limit leaves mixed integer/float cells."""
    numeric = values.astype(np.float64)
    if not np.all(np.isfinite(numeric)):
        return numeric - np.mean(numeric)
    exact = [Fraction(value) for value in values]
    mean = sum(exact) / len(exact)
    return np.fromiter(
        (float(value - mean) for value in exact),
        dtype=np.float64, count=len(exact),
    )


def _safe_secant(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Recover finite quotients when either finite difference overflows."""
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        numerator, denominator = _safe_difference(y), _safe_difference(x)
        result = numerator / denominator
    repair = ((~np.isfinite(numerator) | ~np.isfinite(denominator))
              & numeric_isfinite(x[:-1]) & numeric_isfinite(x[1:])
              & numeric_isfinite(y[:-1]) & numeric_isfinite(y[1:]))
    for index in np.flatnonzero(repair):
        # This is a rare fallback. Exact fractions avoid both difference
        # overflow and underflow while dividing by a huge coordinate span.
        cells = [value.item() if isinstance(value, np.generic) else value
                 for value in (x[index], x[index + 1], y[index], y[index + 1])]
        left_x, right_x, left_y, right_y = map(Fraction, cells)
        distance = right_x - left_x
        if distance:
            quotient = (right_y - left_y) / distance
            try:
                result[index] = float(quotient)
            except OverflowError:
                result[index] = np.inf if quotient > 0 else -np.inf
    return result


class _FFTCoordinatesError(ValueError):
    """Coordinates cannot define a native coordinate-domain FFT."""


def _prepare_fft_coordinates(x: np.ndarray) -> tuple[np.ndarray, np.ndarray, float, bool]:
    """Return increasing interpolation coordinates and the frequency step."""
    message = "FFT requires finite, strictly monotonic coordinates with a nonzero span."
    if x.dtype.kind == "b":
        x = x.astype(np.uint8)
    elif x.dtype.kind == "f" and x.dtype.itemsize < 8:
        # Widen the coordinates too: x[-1] - x[0], interpolation, and the
        # uniformity check must all operate beyond the source dtype's range.
        x = x.astype(np.float64)
    with np.errstate(over="ignore", invalid="ignore"):
        dx = _safe_difference(x)
    if not np.all(numeric_isfinite(x)) or len(x) == 0:
        raise _FFTCoordinatesError(message)
    # Retain PyQtGraph's supported one-sample DC spectrum.
    if len(x) == 1:
        return x, dx, 1., False
    reverse = bool(np.all(dx < 0))
    if reverse:
        x, dx = x[::-1], -dx[::-1]
    elif not np.all(dx > 0):
        raise _FFTCoordinatesError(message)
    if x.dtype.kind in "iuO":
        # Rebase before float conversion to retain nearby steps above 2**53
        # without unsigned/signed overflow.
        exact = x.astype(object)
        try:
            x = (exact - exact[0]).astype(np.float64)
        except TypeError:
            exact = np.frompyfunc(Fraction, 1, 1)(exact)
            x = (exact - exact[0]).astype(np.float64)
    with np.errstate(over="ignore", invalid="ignore"):
        span = float(x[-1] - x[0])
    coordinate_scale = 1.
    if not np.isfinite(span):
        # A finite sweep may cross zero over more than float64's range.
        # Interpolate on scaled coordinates and restore units in frequency.
        coordinate_scale = float(np.max(np.abs(x)))
        x = x / coordinate_scale
        dx = np.diff(x)
    with np.errstate(over="ignore", invalid="ignore"):
        spacing = float(x[-1] - x[0]) / (len(x) - 1)
    if not (np.isfinite(spacing) and spacing > 0 and np.all(np.diff(x) > 0)):
        raise _FFTCoordinatesError(message)
    period = spacing * len(x)
    frequency_step = ((1. / period) if np.isfinite(period)
                      else (1. / spacing) / len(x)) / coordinate_scale
    # Finite acquired coordinates can imply an unrepresentable reciprocal
    # axis. Reject before multiplying the bins (including 0 * infinity).
    if (not np.isfinite(frequency_step) or frequency_step <= 0
            or not np.isfinite(frequency_step * (len(x) // 2))):
        raise _FFTCoordinatesError("FFT requires finite, representable frequencies.")
    return x, dx, frequency_step, reverse


def native_fft_coordinate_error(x: np.ndarray | None) -> str | None:
    """Validate populated traces; pending or empty curves have no spectrum."""
    if x is None or len(x) == 0:
        return None
    try:
        _prepare_fft_coordinates(x)
    except _FFTCoordinatesError as error:
        return str(error)
    return None


class NativePlotDataItem(pg.PlotDataItem):
    """Keep acquisition order while preparing native derivatives and FFTs."""

    _datasetMapped: PlotDataset | None
    _qplot_fft_error: str | None = None

    def _fourierTransform(self, x, y):
        """Prepare increasing FFT coordinates without changing source data.

        PyQtGraph's resampling assumes increasing coordinates. Reverse paired
        samples only for strictly decreasing sweeps; sorting a reversing sweep
        would merge different acquisition branches into a fictitious signal.
        The window preflights all traces before enabling FFT. This defensive
        empty mapping also keeps setData safe until its change signal lets the
        window roll back FFT after a refresh introduces unsupported coordinates.
        Mean subtraction has already run in the mapped dataset before this hook.
        """
        self._qplot_fft_error = None
        try:
            x, dx, frequency_step, reverse = _prepare_fft_coordinates(x)
        except _FFTCoordinatesError as error:
            self._qplot_fft_error = str(error)
            return np.array([], dtype=float), np.array([], dtype=float)
        if len(x) == 1:
            # abs(minimum signed integer) wraps in the recorded dtype.
            # The FFT spectrum uses floating magnitudes, including its DC bin.
            return np.array([0.]), np.abs(y.astype(np.float64))
        if reverse:
            y = y[::-1]
        if y.dtype.kind == "f" and y.dtype.itemsize < 8:
            # NumPy's FFT retains float32 precision. Widen before its internal
            # unnormalized sum can overflow, not just before normalization.
            y = y.astype(np.float64)
        dc_offset = None
        if y.dtype.kind in "iu":
            dc_offset = Fraction(sum(map(int, y)), len(y))
            y = _center_integer_samples(y)
        elif y.dtype.kind == "O":
            numeric = y.astype(np.float64)
            if (np.all(np.isfinite(numeric))
                    and np.max(np.abs(numeric)) <= np.finfo(float).max / 2.):
                # FFT converts integer/object arrays to float. Remove their
                # exact offset first so adjacent recorded integers survive;
                # restore only DC after any nonuniform interpolation.
                exact = [Fraction(value.item() if isinstance(value, np.generic) else value)
                         for value in y]
                dc_offset = sum(exact, Fraction()) / len(exact)
                y = np.array([float(value - dc_offset) for value in exact])
            else:
                y = numeric
        # Keep the ordinary interpolation semantics: nonuniform sweeps have
        # the mean of the resampled signal, not the acquired sample mean.
        resampled = np.any(np.abs(dx - dx[0]) > abs(dx[0]) / 1000)
        float_dc = None
        if dc_offset is None and np.all(np.isfinite(y)) and not resampled:
            # FFT accumulation can erase a measured cancellation residual.
            # Compute DC before amplitude scaling can round that residual.
            float_dc = finite_mean(y)
        float_anchor = None
        if dc_offset is None and y.dtype.kind == "f" and np.all(np.isfinite(y)):
            # A large floating DC offset can leak into small AC bins during
            # FFT accumulation. Nearby same-sign values satisfy Sterbenz's
            # lemma: subtracting this recorded anchor retains every source
            # difference exactly, including subnormals. Wider signals keep
            # their existing scaling path so anchoring cannot erase samples.
            anchor = float(y[0])
            absolute = np.abs(y)
            if (anchor != 0. and np.all(np.signbit(y) == np.signbit(anchor))
                    and np.min(absolute) >= abs(anchor) / 2.
                    and np.max(absolute) / 2. <= abs(anchor)):
                float_anchor = Fraction(anchor)
                y = y - anchor
        amplitude_scale = 1.
        if np.all(np.isfinite(y)):
            largest = float(np.max(np.abs(y)))
            if largest > np.finfo(float).max / len(y):
                # Scale before interpolation too: interpolating opposite
                # extremes otherwise overflows its intermediate difference.
                amplitude_scale = largest
                y = y / amplitude_scale
        # Match PyQtGraph's uniformity tolerance and normalized real FFT.
        if resampled:
            uniform_x = np.linspace(x[0], x[-1], len(x))
            y = np.interp(uniform_x, x, y)
            if dc_offset is None and np.all(np.isfinite(y)):
                if float_anchor is None:
                    float_dc = finite_mean(y) * amplitude_scale
                else:
                    # Resampling changes the centered mean. Restore its sign
                    # and the exact anchor before rounding the resulting DC.
                    centered_mean = sum((Fraction(float(value)) for value in y), Fraction()) / len(y)
                    float_dc = float(float_anchor + centered_mean * Fraction(amplitude_scale))
        magnitudes = np.abs(np.fft.rfft(y) / len(y))
        if amplitude_scale != 1.:
            # Every normalized coefficient is bounded by the largest sample.
            # Clamp roundoff at that bound before restoring extreme units.
            magnitudes = np.minimum(magnitudes, 1.) * amplitude_scale
        if dc_offset is not None:
            # Resampling may change the centered mean. Restore its signed
            # value before taking the magnitude, retaining the exact offset.
            if resampled:
                resampled_mean = sum((Fraction(float(value)) for value in y), Fraction()) / len(y)
                dc_offset += resampled_mean * Fraction(amplitude_scale)
            magnitudes[0] = abs(float(dc_offset))
        elif float_dc is not None:
            magnitudes[0] = abs(float_dc)
        frequencies = np.arange(len(magnitudes), dtype=float) * frequency_step
        return frequencies, magnitudes

    def _getDisplayDataset(self) -> PlotDataset | None:
        # Isolate the private PyQtGraph mapping hook here. Seed only the mapped
        # cache; upstream still owns log scaling, clipping, downsampling, curve
        # updates and cache invalidation through its ordinary native setters.
        source = self._dataset
        if source is not None and self._datasetMapped is None:
            if self.opts["derivativeMode"] or self.opts["phasemapMode"]:
                # Native derivative/phase mapping uses original samples,
                # overriding mean subtraction and (in phase mode) FFT. Phase
                # mode replaces both coordinates, so FFT/log-X must not
                # discard its first sample.
                x = source.y[:-1] if self.opts["phasemapMode"] else source.x[:-1]
                if self.opts["phasemapMode"] and x.dtype.kind == "O":
                    x = x.astype(np.float64)
                y = _safe_secant(source.x, source.y)
            elif self.opts["subtractMeanMode"] and source.y.dtype.kind in "iu":
                x = source.x
                y = _center_integer_samples(source.y)
                if self.opts["fftMode"]:
                    x, y = self._fourierTransform(x, y)
                    if self.opts["logMode"][0]:
                        x, y = x[1:], y[1:]
            elif self.opts["subtractMeanMode"] and source.y.dtype.kind == "f":
                x = source.x
                y = _center_float_samples(source.y)
                if self.opts["fftMode"]:
                    x, y = self._fourierTransform(x, y)
                    if self.opts["logMode"][0]:
                        x, y = x[1:], y[1:]
            elif source.y.dtype.kind == "O" or source.x.dtype.kind == "O":
                # PyQtGraph's painter cannot call isfinite on object cells.
                # Keep the original dataset exact for CSV/cursor access and
                # map only the on-screen values to float.
                x = source.x
                y = source.y if self.opts["fftMode"] else source.y.astype(np.float64)
                if self.opts["subtractMeanMode"]:
                    y = (_center_object_samples(source.y) if source.y.dtype.kind == "O"
                         else _center_float_samples(y))
                if self.opts["fftMode"]:
                    x, y = self._fourierTransform(x, y)
                    if self.opts["logMode"][0]:
                        x, y = x[1:], y[1:]
            else:
                return super()._getDisplayDataset()
            if x.dtype.kind == "O":
                x = x.astype(np.float64)
            mapped = PlotDataset(x, y)
            if True in self.opts["logMode"]:
                mapped.applyLogMapping(self.opts["logMode"])
            self._datasetMapped = mapped
        return super()._getDisplayDataset()
