"""qPlot's coordinate-safe mapping for PyQtGraph's native line controls."""

from fractions import Fraction
from typing import cast

import numpy as np
import pyqtgraph as pg
from PyQt6 import QtCore
from pyqtgraph.graphicsItems.PlotDataItem import PlotDataset

from qplot.datahandling.parameter_data import numeric_isfinite
from qplot.tools.plot_tools import (
    _center_float_samples,
    _fraction_as_extended_float,
    _fraction_as_float,
    _integer_differences,
    _integer_samples,
)
from qplot.tools.sample_statistics import finite_mean, sample_fraction

from ._display_precision import (
    DISPLAY_PRECISION_MESSAGE,
    float_display_loses_distinctions,
)

LINE_DISPLAY_RANGE_MESSAGE = (
    "The line coordinate range cannot be displayed safely. "
    "Recorded samples remain available for CSV export and processing."
)


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
    exact = [sample_fraction(value) for value in values]
    mean = sum(exact) / len(exact)
    return np.fromiter(
        (_fraction_as_float(value - mean) for value in exact),
        dtype=np.float64, count=len(exact),
    )


def _safe_secant(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Recover finite quotients when either finite difference overflows."""
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        numerator, denominator = _safe_difference(y), _safe_difference(x)
        result = numerator / denominator
    mixed_objects = ((x.dtype.kind == "O" and not _integer_samples(x))
                     or (y.dtype.kind == "O" and not _integer_samples(y)))
    repair = ((~np.isfinite(numerator) | ~np.isfinite(denominator) | mixed_objects)
              & numeric_isfinite(x[:-1]) & numeric_isfinite(x[1:])
              & numeric_isfinite(y[:-1]) & numeric_isfinite(y[1:]))
    for index in np.flatnonzero(repair):
        # This is a rare fallback. Exact fractions avoid both difference
        # overflow and underflow while dividing by a huge coordinate span.
        cells = [value.item() if isinstance(value, np.generic) else value
                 for value in (x[index], x[index + 1], y[index], y[index + 1])]
        left_x, right_x, left_y, right_y = map(sample_fraction, cells)
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


def _interpolate_fft_samples(targets, coordinates, samples):
    """Retain extended samples and repair overflowing interpolation segments."""
    segments = np.searchsorted(coordinates, targets, side="right") - 1
    segments = np.clip(segments, 0, len(coordinates) - 2)
    extended = any(values.dtype.kind == "f" and values.dtype.itemsize > 8
                   for values in (targets, coordinates, samples))
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        differences = np.diff(samples)
        if extended:
            # np.interp accepts only binary64 arrays. Retain extended recorded
            # residuals through resampling, including cancellation in FFT DC.
            dtype = np.result_type(targets, coordinates, samples, np.float64)
            targets = targets.astype(dtype, copy=False)
            coordinates = coordinates.astype(dtype, copy=False)
            samples = samples.astype(dtype, copy=False)
            differences = np.diff(samples)
            left, right = coordinates[segments], coordinates[segments + 1]
            before, after = samples[segments], samples[segments + 1]
            weights = (targets - left) / (right - left)
            result = before + differences[segments] * weights
            # Match interpolation of missing/infinite samples and preserve
            # exact endpoints, where zero times infinity must not erase data.
            alternate = after - differences[segments] * (1 - weights)
            result = np.where(np.isnan(result), alternate, result)
            result = np.where(before == after, before, result)
            result = np.where(targets == left, before, result)
            result = np.where(targets == right, after, result)
        else:
            result = np.interp(targets, coordinates, samples)
    finite_endpoints = np.isfinite(samples[segments]) & np.isfinite(samples[segments + 1])
    repair = ~np.isfinite(differences[segments])
    if not extended:
        # np.interp divides the sample difference by the coordinate distance
        # first. That slope can overflow or lose precision to underflow even
        # when the interpolated sample is finite and far from either limit.
        with np.errstate(over="ignore", under="ignore", invalid="ignore", divide="ignore"):
            slopes = differences / np.diff(coordinates)
        unsafe = (~np.isfinite(slopes)
                  | ((np.abs(slopes) < np.finfo(float).tiny) & (differences != 0)))
        positions = np.flatnonzero(unsafe[segments] & ~repair & finite_endpoints)
        if positions.size:
            # A whole large sweep can have unrepresentable slopes. Bounded
            # interpolation weights avoid a Python/Fraction loop per sample.
            selected = segments[positions]
            left, right = coordinates[selected], coordinates[selected + 1]
            target = targets[positions]
            before, after = samples[selected], samples[selected + 1]
            with np.errstate(over="ignore", under="ignore", invalid="ignore", divide="ignore"):
                span = right - left
                before_weight = (right - target) / span
                after_weight = (target - left) / span
                # Compute both weights independently: 1 - after_weight can
                # erase a small, representable contribution near an endpoint.
                recovered = before * before_weight + after * after_weight
            at_left, at_right = target == left, target == right
            recovered = np.where(at_left, before, np.where(at_right, after, recovered))
            result[positions] = recovered
            # A tiny weight can still contribute a representable sample when
            # multiplied by a large amplitude. Preserve that rare exact path;
            # actual endpoints need no interpolation and remain vectorized.
            repair[positions] = (~(at_left | at_right)
                                 & (~np.isfinite(recovered)
                                    | (np.abs(before_weight) < np.finfo(float).tiny)
                                    | (np.abs(after_weight) < np.finfo(float).tiny)))
    repair &= finite_endpoints
    for index in np.flatnonzero(repair):
        segment = segments[index]
        left, right, target = map(sample_fraction, (
            coordinates[segment], coordinates[segment + 1], targets[index],
        ))
        before, after = map(sample_fraction, (samples[segment], samples[segment + 1]))
        interpolated = before + (after - before) * (target - left) / (right - left)
        result[index] = (_fraction_as_extended_float(interpolated, result.dtype)
                         if extended else float(interpolated))
    return result


def _normalized_real_fft(samples):
    """Bound FFT sums with binary scaling without erasing small source cells."""
    count = len(samples)
    if not np.all(np.isfinite(samples)):
        return np.fft.rfft(samples) / count
    largest = float(np.max(np.abs(samples)))
    if largest <= np.finfo(float).max / count:
        return np.fft.rfft(samples) / count
    # Each butterfly is bounded by the sum of absolute source amplitudes.
    # A length-sized binary divisor bounds that sum and retains normal small
    # samples exactly; dividing by the largest sample can erase whole signals.
    shift = (count - 1).bit_length()
    scaled = np.ldexp(samples, -shift)
    # Binary scaling may still round subnormal source cells. Transform just
    # that tiny tail separately, with exact binary normalization in its range.
    tail = (samples != 0) & (np.abs(scaled) < np.finfo(float).tiny)
    if np.any(tail):
        scaled[tail] = 0.
    spectrum: np.ndarray = np.fft.rfft(scaled) / count
    # Roundoff at the largest representable source must not turn bounded
    # components into infinity while their binary units are restored.
    bound = np.ldexp(largest, -shift)
    spectrum = (np.ldexp(np.clip(spectrum.real, -bound, bound), shift)
                + 1j * np.ldexp(np.clip(spectrum.imag, -bound, bound), shift))
    if np.any(tail):
        small = np.where(tail, samples, 0.)
        tail_shift = -int(np.frexp(np.max(np.abs(small)))[1])
        tail_spectrum = np.fft.rfft(np.ldexp(small, tail_shift)) / count
        spectrum += (np.ldexp(tail_spectrum.real, -tail_shift)
                     + 1j * np.ldexp(tail_spectrum.imag, -tail_shift))
    return spectrum


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
    coordinate_scale = 1.
    if x.dtype.kind in "iuO":
        # Rebase before float conversion to retain nearby steps above 2**53
        # without unsigned/signed overflow.
        exact: np.ndarray = x.astype(object)
        if x.dtype.kind == "O" and not _integer_samples(x):
            exact = cast(np.ndarray, np.frompyfunc(sample_fraction, 1, 1)(exact))
        with np.errstate(over="ignore", invalid="ignore"):
            try:
                rebased = (exact - exact[0]).astype(np.float64)
            except (TypeError, OverflowError):
                exact = cast(np.ndarray, np.frompyfunc(sample_fraction, 1, 1)(exact))
                rebased = np.fromiter(
                    (_fraction_as_float(value - exact[0]) for value in exact),
                    dtype=np.float64, count=len(exact),
                )
        if np.all(np.isfinite(rebased)):
            x = rebased
        else:
            # Mixed float/integer coordinate records also use object cells.
            # Their finite span can exceed float64 before rebasing finishes.
            # Scale exact differences before converting them to float.
            coordinate_scale = float(np.max(np.abs(x.astype(np.float64))))
            anchor = sample_fraction(exact[0])
            scale = Fraction(coordinate_scale)
            x = np.fromiter(
                (float((sample_fraction(value) - anchor) / scale) for value in exact),
                dtype=np.float64, count=len(exact),
            )
            dx = np.diff(x)
    elif x.dtype.kind == "f":
        # Uniform targets near a large recorded offset can round to an
        # acquired coordinate before interpolation. Remove the offset while
        # retaining the recorded spacing and the original stored samples.
        with np.errstate(over="ignore", invalid="ignore"):
            rebased = x - x[0]
        if (np.all(np.isfinite(rebased))
                and rebased[-1] <= np.finfo(float).max
                and np.all(np.diff(rebased) > 0)):
            # A wide sweep across zero can instead merge nearby coordinates
            # when rebased. Keep its original interpolation coordinates.
            x = rebased
    with np.errstate(over="ignore", invalid="ignore"):
        span = float(x[-1] - x[0])
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

    sigDisplayChanged = QtCore.pyqtSignal(object)
    _datasetMapped: PlotDataset | None
    _qplot_fft_error: str | None = None
    _qplot_precision_error: str | None = None

    def clear(self):
        self._qplot_range_dataset = None
        self._qplot_clip_dataset = None
        super().clear()

    def axisDataBounds(self, ax, frac=1.0, orthoRange=None):
        """Keep rejected mapped bounds available to qPlot's axis preflight."""
        if self._qplot_precision_error != LINE_DISPLAY_RANGE_MESSAGE or self._datasetMapped is None:
            return self.dataBounds(ax, frac, orthoRange)
        # Hidden painting children report no bounds. Measure the processed
        # samples in an unattached item so autoscale can reject the actual
        # range without installing unsafe data on a scene renderer.
        mapped = self._datasetMapped
        bounds_item = pg.PlotCurveItem(
            x=mapped.x, y=mapped.y, pen=self.opts["pen"],
            shadowPen=self.opts["shadowPen"], fillLevel=self.opts["fillLevel"],
            stepMode=self.opts["stepMode"],
        )
        return bounds_item.dataBounds(ax, frac, orthoRange)

    def viewChanged(self, view, oldView):
        super().viewChanged(view, oldView)
        self._watch_display_range(view if isinstance(view, pg.ViewBox) else None)

    def _watch_display_range(self, viewbox):
        prior = getattr(self, "_qplot_range_viewbox", None)
        if prior is viewbox:
            return
        if prior is not None:
            try:
                prior.sigTransformChanged.disconnect(self._schedule_display_range_check)
            except (RuntimeError, TypeError):
                pass
        self._qplot_range_viewbox = viewbox
        timer = getattr(self, "_qplot_range_timer", None)
        if timer is not None:
            timer.stop()
        self._qplot_view_range_rejected = False
        if viewbox is not None:
            if not hasattr(self, "_qplot_range_timer"):
                self._qplot_range_timer = QtCore.QTimer(self)
                self._qplot_range_timer.setSingleShot(True)
                self._qplot_range_timer.timeout.connect(self._check_display_range)
            viewbox.sigTransformChanged.connect(self._schedule_display_range_check)
            self._schedule_display_range_check()

    def _schedule_display_range_check(self, *_args):
        if not self._qplot_range_timer.isActive():
            self._qplot_range_timer.start(0)

    def _check_display_range(self):
        # Mixed plots finish mirroring their axis owners after range signals.
        # Inspect the composed drawing transform on the next event-loop turn.
        if not isinstance(self.getViewBox(), pg.ViewBox):
            return
        transform = self.sceneTransform()
        previous = self._qplot_precision_error
        self._qplot_view_range_rejected = not bool(np.all(np.isfinite((
            transform.m11(), transform.m12(), transform.m13(),
            transform.m21(), transform.m22(), transform.m23(),
            transform.m31(), transform.m32(), transform.m33(),
        ))))
        self._getDisplayDataset()
        if previous != self._qplot_precision_error:
            self.updateItems(styleUpdate=False)
            self.sigDisplayChanged.emit(self)

    def _mapped_range_is_displayable(self, dataset, viewbox):
        # Cache extrema with the mapped dataset, not the viewport. Resizing
        # and mouse movement must not repeatedly scan a large acquired trace.
        if getattr(self, "_qplot_range_dataset", None) is not dataset:
            x, y = dataset.x, dataset.y
            if x.shape == y.shape:
                paired = numeric_isfinite(x) & numeric_isfinite(y)
                x, y = x[paired], y[paired]
            span_values = []
            for values in (x, y):
                numeric = np.asarray(values, dtype=float)
                finite = numeric[np.isfinite(numeric)]
                span_values.append(float(np.max(finite)) - float(np.min(finite)) if finite.size else 0.)
            self._qplot_range_spans = np.asarray(span_values)
            self._qplot_range_dataset = dataset
        spans = self._qplot_range_spans
        pixels = np.array([max(1., viewbox.width()), max(1., viewbox.height())])
        with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
            return bool(np.all(np.isfinite(spans))
                        and np.all(np.isfinite(pixels[spans > 0] / spans[spans > 0])))

    def viewRangeChanged(self, vb=None, ranges=None, changed=None):
        if not isinstance(self.getViewBox(), pg.ViewBox):
            # Axis reassignment briefly leaves the trace in the scene alone.
            # GraphicsItem then reports the GraphicsView, which lacks native
            # clipping's axis API. Reattachment rebuilds the display normally.
            self._datasetDisplay = None
            self._watch_display_range(None)
            return
        super().viewRangeChanged(vb, ranges, changed)
        self.sigDisplayChanged.emit(self)

    def _precision_checked_samples(self, x, y):
        self._qplot_precision_error = None
        if all(values.dtype.kind not in "iuO"
               and not (values.dtype.kind == "f" and values.dtype.itemsize > 8)
               for values in (x, y)):
            return x, y
        # Only paired finite samples can be drawn. Derivatives and phase maps
        # can leave an exact coordinate opposite a missing slope; its rounded
        # position must not reject unrelated finite plotted samples.
        visible_x, visible_y = x, y
        if x.shape == y.shape:
            paired = numeric_isfinite(x) & numeric_isfinite(y)
            for values, logarithmic in zip((x, y), self.opts["logMode"], strict=True):
                if logarithmic:
                    # Log mapping also hides nonpositive samples. Compare only
                    # finite partners: Decimal NaN comparisons can raise.
                    paired[paired] &= values[paired] > 0
            visible_x, visible_y = x[paired], y[paired]
        # Centre-step plots supply N+1 edges and N values. Keep their existing
        # conservative precision check without inventing sample pairings.
        if (float_display_loses_distinctions(visible_x)
                or float_display_loses_distinctions(visible_y)):
            self._qplot_precision_error = DISPLAY_PRECISION_MESSAGE
            return np.array([], dtype=float), np.array([], dtype=float)
        return x, y

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
                exact = [sample_fraction(value)
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
        # Match PyQtGraph's uniformity tolerance and normalized real FFT.
        if resampled:
            uniform_x = np.linspace(x[0], x[-1], len(x))
            y = _interpolate_fft_samples(uniform_x, x, y)
            if dc_offset is None and np.all(np.isfinite(y)):
                if float_anchor is None:
                    float_dc = finite_mean(y)
                else:
                    # Resampling changes the centered mean. Restore its sign
                    # and the exact anchor before rounding the resulting DC.
                    centered_mean = sum((Fraction(float(value)) for value in y), Fraction()) / len(y)
                    float_dc = float(float_anchor + centered_mean)
        magnitudes = np.abs(_normalized_real_fft(y))
        if np.all(np.isfinite(y)):
            # The normalized magnitude cannot exceed the largest source cell.
            # Retain that bound when hypot's final rounding reaches overflow.
            magnitudes = np.minimum(magnitudes, np.max(np.abs(y)))
        if dc_offset is not None:
            # Resampling may change the centered mean. Restore its signed
            # value before taking the magnitude, retaining the exact offset.
            if resampled:
                resampled_mean = sum((Fraction(float(value)) for value in y), Fraction()) / len(y)
                dc_offset += resampled_mean
            magnitudes[0] = abs(float(dc_offset))
        elif float_dc is not None:
            magnitudes[0] = abs(float_dc)
        frequencies = np.arange(len(magnitudes), dtype=float) * frequency_step
        return frequencies, magnitudes

    def _getDisplayDataset(self) -> PlotDataset | None:
        if self._datasetMapped is None:
            # New data or processing can restore a representable range. Let
            # native autorange consume those bounds before settled validation.
            self._qplot_view_range_rejected = False
            self._qplot_clip_dataset = None
        if self._qplot_precision_error == LINE_DISPLAY_RANGE_MESSAGE:
            self._qplot_precision_error = None
        dataset = self._get_mapped_display_dataset()
        if getattr(self, "_qplot_range_dataset", None) is not self._datasetMapped:
            self._qplot_range_dataset = None
        viewbox = self.getViewBox()
        if not isinstance(viewbox, pg.ViewBox):
            viewbox = None
        self._watch_display_range(viewbox)
        if dataset is None or viewbox is None or self._qplot_precision_error:
            return dataset
        mapped = self._datasetMapped
        if (getattr(self, "_qplot_view_range_rejected", False)
                or (mapped is not None and not self._mapped_range_is_displayable(mapped, viewbox))):
            self._qplot_precision_error = LINE_DISPLAY_RANGE_MESSAGE
            return None
        return dataset

    def _get_coordinate_safe_display_dataset(self) -> PlotDataset | None:
        """Use native viewport slicing only for ordered mapped coordinates."""
        clipping = self.opts["clipToView"]
        if self._datasetMapped is None:
            dataset = super()._getDisplayDataset()
            if not clipping or self._datasetMapped is None:
                return dataset
        mapped = self._datasetMapped
        if clipping:
            if getattr(self, "_qplot_clip_dataset", None) is not mapped:
                x = mapped.x
                self._qplot_clip_ordered = bool(
                    np.all(numeric_isfinite(x)) and np.all(x[1:] >= x[:-1])
                )
                self._qplot_clip_dataset = mapped
                if not self._qplot_clip_ordered:
                    # Upstream may have just mapped and sliced a new dataset.
                    # Discard that slice before it reaches the curve renderer.
                    self._datasetDisplay = None
            if not self._qplot_clip_ordered:
                # PyQtGraph bisects X assuming increasing order. Descending
                # sweeps and phase maps must retain their acquisition order,
                # including every branch. Keep the requested control intact
                # and still allow native downsampling and range limiting.
                self.opts["clipToView"] = False
                try:
                    return super()._getDisplayDataset()
                finally:
                    self.opts["clipToView"] = clipping
        return super()._getDisplayDataset()

    def _get_mapped_display_dataset(self) -> PlotDataset | None:
        # Isolate the private PyQtGraph mapping hook here. Seed only the mapped
        # cache; upstream still owns log scaling, clipping, downsampling, curve
        # updates and cache invalidation through its ordinary native setters.
        source = self._dataset
        if source is None:
            self._qplot_precision_error = None
        if source is not None and self._datasetMapped is None:
            if self.opts["derivativeMode"] or self.opts["phasemapMode"]:
                # Native derivative/phase mapping uses original samples,
                # overriding mean subtraction and (in phase mode) FFT. Phase
                # mode replaces both coordinates, so FFT/log-X must not
                # discard its first sample.
                x = source.y[:-1] if self.opts["phasemapMode"] else source.x[:-1]
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
                y = source.y
                if self.opts["subtractMeanMode"]:
                    y = (_center_object_samples(source.y) if source.y.dtype.kind == "O"
                         else _center_float_samples(y))
                if self.opts["fftMode"]:
                    x, y = self._fourierTransform(x, y)
                    if self.opts["logMode"][0]:
                        x, y = x[1:], y[1:]
            else:
                # Upstream retains dense integer samples until QPainter
                # converts them to doubles. Reject only conversions that
                # merge distinct values, keeping originals for processing.
                self._qplot_precision_error = None
                if not self.opts["fftMode"]:
                    x, y = self._precision_checked_samples(source.x, source.y)
                    if self._qplot_precision_error:
                        self._datasetMapped = PlotDataset(x, y)
                return self._get_coordinate_safe_display_dataset()
            x, y = self._precision_checked_samples(x, y)
            if x.dtype.kind == "O":
                x = x.astype(np.float64)
            if y.dtype.kind == "O":
                y = y.astype(np.float64)
            mapped = PlotDataset(x, y)
            if True in self.opts["logMode"]:
                mapped.applyLogMapping(self.opts["logMode"])
            self._datasetMapped = mapped
        return self._get_coordinate_safe_display_dataset()
