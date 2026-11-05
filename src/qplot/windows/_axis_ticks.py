"""Bounded tick placement, scoring and formatting for qPlot axes.

Candidate fields are ordered by preference: requested count first, then label
coverage and centring, with interval size as a deterministic tie-breaker.
"""

from math import ceil, floor, isclose, isfinite, log10, ulp
from types import MethodType
from typing import NamedTuple

from PyQt6 import QtGui
from PyQt6 import QtWidgets as qtw

_MAX_TICK_POSITIONS = 2048


class _FallbackTickCandidate(NamedTuple):
    count_error: int
    too_few_ticks: bool
    spacing_error: float
    spacing: float
    factor: float


class _VisibleTickCandidate(NamedTuple):
    count_error: int
    too_few_ticks: bool
    negative_coverage: float
    centre_error: float
    spacing_error: float
    spacing: float
    factor: float


def _tick_positions(min_val, max_val, spacing, offset):
    """Return the positions on one regular tick lattice inside a range."""

    min_val, max_val = sorted((min_val, max_val))
    if (
            not all(isfinite(value) for value in (min_val, max_val, spacing, offset))
            or spacing <= 0
            ):
        return []

    data_range = max_val - min_val
    span_tolerance = data_range * 1e-12 if isfinite(data_range) else 0.0
    coordinate_precision = max(ulp(min_val), ulp(max_val))
    if spacing < coordinate_precision:
        return []
    tolerance = max(
        span_tolerance,
        spacing * 1e-12,
        coordinate_precision,
        ulp(offset),
        )
    ratio_tolerance = tolerance / spacing
    if not isfinite(ratio_tolerance):
        return []

    if isfinite(data_range):
        estimated_count = (data_range / spacing) + (2.0 * ratio_tolerance) + 2.0
    else:
        estimated_count = (
            (max_val / spacing)
            - (min_val / spacing)
            + (2.0 * ratio_tolerance)
            + 2.0
            )
    if not isfinite(estimated_count) or estimated_count > _MAX_TICK_POSITIONS:
        return []

    lower_ratio = (min_val - offset) / spacing
    upper_ratio = (max_val - offset) / spacing
    if not isfinite(lower_ratio) or not isfinite(upper_ratio):
        return []
    first_index = ceil(lower_ratio - ratio_tolerance)
    last_index = floor(upper_ratio + ratio_tolerance)
    count = last_index - first_index + 1
    if count <= 0 or count > _MAX_TICK_POSITIONS:
        return []

    positions: list[float] = []
    for index in range(first_index, last_index + 1):
        try:
            position = (index * spacing) + offset
        except OverflowError:
            continue
        if not isfinite(position):
            continue
        if position < min_val and min_val - position > tolerance:
            continue
        if position > max_val and position - max_val > tolerance:
            continue
        if not positions or position != positions[-1]:
            positions.append(position)
    return positions


def _visible_horizontal_tick_positions(axis, positions, min_val, max_val, size,
                                       spacing):
    """Return ticks whose labels fit completely within a horizontal axis."""

    if getattr(axis, "orientation", None) not in ("bottom", "top") or size <= 0:
        return positions

    scale = axis.autoSIPrefixScale * axis.scale
    strings = _clean_tick_strings(axis, positions, scale, spacing)
    font = axis.style.get("tickFont") or qtw.QApplication.font()
    metrics = QtGui.QFontMetricsF(font)
    data_range = max_val - min_val
    visible = []
    for position, string in zip(positions, strings, strict=True):
        pixel = ((position - min_val) / data_range) * size
        half_width = metrics.boundingRect(string).width() / 2.0
        if half_width <= pixel <= size - half_width:
            visible.append(position)
    return visible


def _major_tick_spacing(axis, min_val, max_val, size):
    """Choose a zero-aligned 1-2-5 interval nearest the requested tick count."""

    min_val, max_val = sorted((min_val, max_val))
    if not isfinite(min_val) or not isfinite(max_val):
        return []
    data_range = max_val - min_val
    if data_range == 0:
        return []

    target = axis._qplot_major_tick_count
    divisor = max(1, target - 1)
    raw_spacing = (max_val / divisor) - (min_val / divisor)
    if not isfinite(raw_spacing):
        raw_spacing = max(abs(min_val), abs(max_val))
    if raw_spacing == 0.0 and isfinite(data_range):
        raw_spacing = data_range
    if not isfinite(raw_spacing) or raw_spacing <= 0.0:
        return []

    base_exponent = floor(log10(raw_spacing))
    visible_candidates = []
    fallback_candidates = []
    for exponent in range(base_exponent - 2, base_exponent + 3):
        try:
            decade = 10.0 ** exponent
        except OverflowError:
            continue
        if not isfinite(decade) or decade <= 0.0:
            continue
        for factor in (1.0, 2.0, 5.0):
            spacing = factor * decade
            if not isfinite(spacing) or spacing <= 0.0:
                continue
            positions = _tick_positions(min_val, max_val, spacing, 0.0)
            if not positions:
                continue
            fallback_candidates.append(_FallbackTickCandidate(
                count_error=abs(len(positions) - target),
                too_few_ticks=len(positions) < 2,
                spacing_error=abs(log10(spacing) - log10(raw_spacing)),
                spacing=spacing,
                factor=factor,
                ))
            scored_positions = _visible_horizontal_tick_positions(
                axis,
                positions,
                min_val,
                max_val,
                size,
                spacing,
                )
            count = len(scored_positions)
            if count == 0:
                continue
            if isfinite(data_range):
                coverage = (
                    (scored_positions[-1] - scored_positions[0]) / data_range
                    if count > 1
                    else 0.0
                    )
                centre_error = abs(
                    (
                        (scored_positions[0] - min_val)
                        + (scored_positions[-1] - max_val)
                    ) / 2.0
                    ) / data_range
            else:
                scale = max(abs(min_val), abs(max_val))
                scaled_range = (max_val / scale) - (min_val / scale)
                coverage = (
                    (
                        (scored_positions[-1] / scale)
                        - (scored_positions[0] / scale)
                    ) / scaled_range
                    if count > 1
                    else 0.0
                    )
                tick_centre = (
                    (scored_positions[0] / scale) / 2.0
                    + (scored_positions[-1] / scale) / 2.0
                    )
                range_centre = (
                    (min_val / scale) / 2.0
                    + (max_val / scale) / 2.0
                    )
                centre_error = abs(tick_centre - range_centre) / scaled_range
            visible_candidates.append(_VisibleTickCandidate(
                count_error=abs(count - target),
                too_few_ticks=count < 2,
                negative_coverage=-coverage,
                centre_error=centre_error,
                spacing_error=abs(log10(spacing) - log10(raw_spacing)),
                spacing=spacing,
                factor=factor,
                ))

    if visible_candidates:
        candidate = min(visible_candidates)
        major_spacing, factor = candidate.spacing, candidate.factor
    elif fallback_candidates:
        fallback = min(fallback_candidates)
        major_spacing, factor = fallback.spacing, fallback.factor
    else:
        major_spacing = raw_spacing
        factor = 1.0
    subdivisions = {
        1.0: 5,
        2.0: 4,
        5.0: 5,
        }[factor]
    levels = [(major_spacing, 0.0)]
    minor_spacing = major_spacing / subdivisions
    if isfinite(minor_spacing) and 0.0 < minor_spacing < major_spacing:
        levels.append((minor_spacing, 0.0))
    return levels


def _clean_tick_strings(axis, values, scale, spacing):
    """Format nice intervals accurately and round-off near zero as zero."""

    if axis.logMode and spacing is None:
        return axis._qplot_original_tick_strings(
            values,
            scale,
            spacing,
            )

    # Work in tick coordinates: an absolute floor erases small measurements
    # before SI scaling. Allow floating-point round-off, but never a meaningful
    # fraction of a tick interval, even if distant ticks have coarse precision.
    interval = abs(spacing)
    precision = max((ulp(value) for value in values), default=ulp(interval))
    tolerance = min(interval * 1e-6, max(interval * 1e-10, precision * 4))
    clean_values = [0.0 if abs(value) <= tolerance else value for value in values]
    if axis.logMode:
        return axis._qplot_original_tick_strings(
            clean_values,
            scale,
            spacing,
            )

    scaled_spacing = abs(spacing * scale)
    places = max(0, ceil(-log10(scaled_spacing)))
    while places < 12 and not isclose(
            scaled_spacing,
            round(scaled_spacing, places),
            rel_tol=1e-12,
            abs_tol=10.0 ** (-places - 12),
            ):
        places += 1

    strings = []
    for value in clean_values:
        scaled_value = value * scale
        if abs(scaled_value) < 0.001 or abs(scaled_value) >= 10_000:
            strings.append(f"{scaled_value:g}")
        else:
            strings.append(f"{scaled_value:.{places}f}")
    return strings


def _configure_major_ticks(axis, count):
    """Make an AxisItem use qPlot's sparse, range-aware major ticks."""

    axis._qplot_major_tick_count = count
    if not hasattr(axis, "_qplot_major_tick_spacing"):
        axis._qplot_major_tick_spacing = axis.tickSpacing
        axis.tickSpacing = MethodType(_major_tick_spacing, axis)
    current_tick_strings = axis.tickStrings
    if getattr(current_tick_strings, "__func__", None) is not _clean_tick_strings:
        axis._qplot_original_tick_strings = current_tick_strings
        axis.tickStrings = MethodType(_clean_tick_strings, axis)
    axis.setStyle(maxTickLevel=1, maxTextLevel=0)
    axis.picture = None
    axis.update()
