"""Precision-preserving arithmetic for native line transforms.

Rendering, display-range checks and PyQtGraph hooks stay in
``windows._native_transforms``; these helpers operate on recorded samples.
"""

from fractions import Fraction
from math import lcm
from typing import cast

import numpy as np

from qplot.datahandling.numeric_samples import numeric_isfinite
from qplot.tools.plot_tools import (
    _fraction_as_extended_float,
    _fraction_as_float,
    _integer_differences,
    _integer_samples,
)
from qplot.tools.sample_statistics import sample_fraction


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
    repair &= finite_endpoints & (coordinates[segments] != coordinates[segments + 1])
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


def _fft_sample_ratio(value):
    if isinstance(value, (int, np.integer, bool, np.bool_)):
        return int(value), 1
    return value.as_integer_ratio()


def _resample_fft_samples(coordinates, samples, recorded_coordinates):
    """Recover interpolation whose signal is sensitive to grid rounding."""
    targets = np.linspace(coordinates[0], coordinates[-1], len(coordinates))
    result = _interpolate_fft_samples(targets, coordinates, samples)
    result[0], result[-1] = samples[0], samples[-1]
    if np.all(samples == samples[0]):
        return result
    first, last = map(sample_fraction, (recorded_coordinates[0], recorded_coordinates[-1]))
    step = (last - first) / (len(coordinates) - 1)
    projection_exact = False
    if coordinates[0] == 0:
        if recorded_coordinates.dtype.kind in "iu":
            exact = recorded_coordinates.astype(object) - int(recorded_coordinates[0])
            projection_exact = bool(np.all(np.equal(exact, coordinates)))
        elif recorded_coordinates.dtype.kind == "f" and recorded_coordinates.dtype.itemsize <= 8:
            numeric = recorded_coordinates.astype(float, copy=False)
            with np.errstate(over="ignore", invalid="ignore"):
                relative = numeric - numeric[0]
                virtual_anchor = numeric - relative
                residual = (numeric - (relative + virtual_anchor)) + (virtual_anchor - numeric[0])
            projection_exact = bool(np.array_equal(relative, coordinates) and np.all(residual == 0))
    if projection_exact:
        # The bit bound proves every binary grid-index product is exact.
        if (step.denominator & (step.denominator - 1) == 0
                and (abs(step.numerator) * (len(coordinates) - 1)).bit_length() <= 53
                and sample_fraction(float(step)) == step):
            return result
    indices = np.arange(len(targets))
    if projection_exact:
        # Only division to form the step and multiplication by the index can
        # move this grid. Full ULP bounds also cover subnormal steps/targets;
        # do not round a subnormal remainder away before multiplying it.
        step_ulp = 2 * np.spacing(float(step) / 2)
        uncertainty = indices * step_ulp + 2 * np.spacing(np.abs(targets) / 2)
        uncertainty = np.maximum(uncertainty, 8 * np.nextafter(0., 1.))
    else:
        # Include coordinate projection, spacing and target arithmetic.
        span = float(coordinates[-1] - coordinates[0])
        uncertainty = np.full(len(targets), max(
            8 * np.finfo(float).eps * max(
                abs(float(coordinates[0])), abs(float(coordinates[-1])), abs(span),
            ),
            8 * np.nextafter(0., 1.),
        ))
    segments = np.clip(np.searchsorted(coordinates, targets, side="right") - 1,
                       0, len(coordinates) - 2)

    def needs_exact(intervals, points):
        before = samples[intervals].astype(result.dtype, copy=False)
        after = samples[intervals + 1].astype(result.dtype, copy=False)
        width = coordinates[intervals + 1] - coordinates[intervals]
        scale = np.maximum(np.maximum(np.abs(before), np.abs(after)), np.abs(result[points]))
        scale = np.where(scale > 0, scale, 1.)
        with np.errstate(over="ignore", under="ignore", divide="ignore", invalid="ignore"):
            difference = np.abs(before / scale - after / scale) + 2 * np.finfo(result.dtype).eps
            error = (uncertainty[points] / width) * difference
            # Bound relative signal error, not just coordinate displacement.
            # Ordinary floating interpolation stays unchanged below sqrt(eps);
            # a zero or small residual therefore receives stricter recovery.
            allowance = np.sqrt(np.finfo(float).eps) * np.abs(result[points] / scale)
        # If uncertainty spans a whole interval, it may cross further source
        # cells too. Exact bracketing resolves those collapsed/narrow groups.
        return ((width <= uncertainty[points])
                | ((before != after)
                   & ((error > allowance) | ~np.isfinite(error)
                      | ~np.isfinite(allowance) | (error == 0))))

    selected = needs_exact(segments, indices)
    # A rounded target can land in a flat bracket while its true position
    # remains in a changing neighbour. Check both sides of uncertain edges.
    for offset, boundary in ((-1, segments), (1, segments + 1)):
        neighbours = segments + offset
        near = ((neighbours >= 0) & (neighbours < len(coordinates) - 1)
                & (np.abs(targets - coordinates[boundary]) <= uncertainty))
        points = indices[near]
        selected[near] |= needs_exact(neighbours[near], points)
    selected[[0, -1]] = False
    positions = np.flatnonzero(selected)
    if not positions.size:
        return result

    # Keep the target as integer numerator/denominator even when no float can
    # represent it. Binary64 recovery needs no per-target Fraction objects.
    common = lcm(first.denominator, last.denominator)
    first_numerator = first.numerator * (common // first.denominator)
    delta = last.numerator * (common // last.denominator) - first_numerator
    denominator = common * (len(coordinates) - 1)
    start = first_numerator * (len(coordinates) - 1)

    for position in positions:
        numerator = start + delta * int(position)
        segment = int(segments[position])
        left, left_d = _fft_sample_ratio(recorded_coordinates[segment])
        right, right_d = _fft_sample_ratio(recorded_coordinates[segment + 1])
        if numerator * left_d < left * denominator or numerator * right_d > right * denominator:
            low, high = 0, len(recorded_coordinates) - 1
            while high - low > 1:
                middle = (low + high) // 2
                value, divisor = _fft_sample_ratio(recorded_coordinates[middle])
                if value * denominator <= numerator * divisor:
                    low = middle
                else:
                    high = middle
            segment = low
            left, left_d = _fft_sample_ratio(recorded_coordinates[segment])
            right, right_d = _fft_sample_ratio(recorded_coordinates[segment + 1])
        before, after = samples[segment], samples[segment + 1]
        weight_n = (numerator * left_d - left * denominator) * right_d
        weight_d = denominator * (right * left_d - left * right_d)
        if weight_n == 0:
            result[position] = before
        elif weight_n == weight_d:
            result[position] = after
        elif np.isfinite(before) and np.isfinite(after):
            before_n, before_d = _fft_sample_ratio(before)
            after_n, after_d = _fft_sample_ratio(after)
            interpolated_n = (
                before_n * after_d * weight_d
                + (after_n * before_d - before_n * after_d) * weight_n
            )
            interpolated_d = before_d * after_d * weight_d
            if result.dtype.itemsize > 8:
                result[position] = _fraction_as_extended_float(
                    Fraction(interpolated_n, interpolated_d), result.dtype,
                )
            else:
                result[position] = interpolated_n / interpolated_d
        else:
            # Preserve missing/infinite interpolation for an interior target.
            result[position] = np.interp(.5, [0., 1.], [before, after])
    return result


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
    # The recorded coordinates were strictly monotonic above. Their float
    # projection can merge a narrow interval; exact resampling below retains
    # its original endpoints instead of rejecting the acquired sweep.
    if not (np.isfinite(spacing) and spacing > 0 and np.all(np.diff(x) >= 0)):
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
