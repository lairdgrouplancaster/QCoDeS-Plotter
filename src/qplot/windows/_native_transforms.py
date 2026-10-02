"""qPlot's coordinate-safe mapping for PyQtGraph's native line controls."""

from fractions import Fraction

import numpy as np
import pyqtgraph as pg
from pyqtgraph.graphicsItems.PlotDataItem import PlotDataset

from qplot.datahandling.parameter_data import numeric_isfinite


def _safe_difference(values: np.ndarray) -> np.ndarray:
    """Subtract samples before narrowing arithmetic can overflow or wrap."""
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
        try:
            differences = np.diff(values)
        except TypeError:
            fractions = np.frompyfunc(Fraction, 1, 1)(values)
            differences = np.diff(fractions)
        return differences.astype(np.float64)
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


class _FFTCoordinatesError(ValueError):
    """Coordinates cannot define a native coordinate-domain FFT."""


def _prepare_fft_coordinates(x: np.ndarray) -> tuple[np.ndarray, np.ndarray, float, bool]:
    """Share control validation and FFT preparation, preserving source order."""
    message = "FFT requires finite, strictly monotonic coordinates with a nonzero span."
    if x.dtype.kind == "b":
        x = x.astype(np.uint8)
    elif x.dtype.kind == "f" and x.dtype.itemsize < 8:
        # Widen the coordinates too: x[-1] - x[0], interpolation, and the
        # uniformity check must all operate beyond the source dtype's range.
        x = x.astype(np.float64)
    with np.errstate(over="ignore", invalid="ignore"):
        dx = _safe_difference(x)
    if not (np.all(numeric_isfinite(x)) and np.all(np.isfinite(dx))) or len(x) == 0:
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
        spacing = float(x[-1] - x[0]) / (len(x) - 1)
    if not (np.isfinite(spacing) and spacing > 0 and np.all(np.diff(x) > 0)):
        raise _FFTCoordinatesError(message)
    return x, dx, spacing, reverse


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
            x, dx, spacing, reverse = _prepare_fft_coordinates(x)
        except _FFTCoordinatesError as error:
            self._qplot_fft_error = str(error)
            return np.array([], dtype=float), np.array([], dtype=float)
        if len(x) == 1:
            return np.array([0.]), np.abs(y)
        if reverse:
            y = y[::-1]
        # Match PyQtGraph's uniformity tolerance and normalized real FFT.
        if np.any(np.abs(dx - dx[0]) > abs(dx[0]) / 1000):
            uniform_x = np.linspace(x[0], x[-1], len(x))
            y = np.interp(uniform_x, x, y)
        return np.fft.rfftfreq(len(y), spacing), np.abs(np.fft.rfft(y) / len(y))

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
                y = _safe_difference(source.y) / _safe_difference(source.x)
            elif self.opts["subtractMeanMode"] and source.y.dtype.kind in "iu":
                x = source.x
                y = _center_integer_samples(source.y)
                if self.opts["fftMode"]:
                    x, y = self._fourierTransform(x, y)
                    if self.opts["logMode"][0]:
                        x, y = x[1:], y[1:]
            elif (self.opts["subtractMeanMode"] and source.y.dtype.kind == "f"
                  and source.y.dtype.itemsize < 8):
                x = source.x
                y = source.y.astype(np.float64)
                y = y - np.mean(y)
                if self.opts["fftMode"]:
                    x, y = self._fourierTransform(x, y)
                    if self.opts["logMode"][0]:
                        x, y = x[1:], y[1:]
            elif source.y.dtype.kind == "O" or source.x.dtype.kind == "O":
                # PyQtGraph's painter cannot call isfinite on object cells.
                # Keep the original dataset exact for CSV/cursor access and
                # map only the on-screen values to float.
                x = source.x
                y = source.y.astype(np.float64)
                if self.opts["subtractMeanMode"]:
                    y = (_center_object_samples(source.y) if source.y.dtype.kind == "O"
                         else y - np.mean(y))
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
