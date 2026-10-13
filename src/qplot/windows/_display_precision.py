"""Reject renderer conversions that merge distinct recorded values."""

import numpy as np

DISPLAY_PRECISION_MESSAGE = (
    "Distinct recorded values become identical at display precision. "
    "Apply a subtraction operation to view relative values. "
    "The original samples remain available for CSV export and processing."
)

HEATMAP_DISPLAY_RANGE_MESSAGE = (
    "The heatmap coordinate range cannot be displayed safely. "
    "Recorded samples remain available for CSV export and processing."
)


def heatmap_display_range_is_finite(geometry, pixel_size=(1., 1.)):
    """Require finite image and viewport scales before installing Qt transforms."""
    if not np.all(np.isfinite(geometry.rect)):
        return False
    spans = np.asarray(geometry.rect[2:])
    cells = np.asarray((geometry.x.count, geometry.y.count))
    with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
        return bool(np.all(np.isfinite(spans / cells))
                    and np.all(np.isfinite(np.maximum(pixel_size, cells) / spans)))


def float_display_loses_distinctions(values):
    """Check exact integer/object values before a renderer converts to float.

    Ordinary float64 and narrower samples are already representable. Only
    potentially inexact inputs need sorting; adjacent distinct exact values
    must remain distinct after conversion, including fractional object cells.
    """
    values = np.asarray(values)
    if values.dtype.kind not in "iuO" or values.size < 2:
        return False
    if values.dtype.kind in "iu" and (
        values.dtype.itemsize < 8
        or (np.min(values) >= -(2**53) and np.max(values) <= 2**53)
    ):
        return False
    rendered = values.astype(float)
    unique = np.unique(values[np.isfinite(rendered)])
    if unique.size < 2:
        return False
    rendered_unique = unique.astype(float)
    return bool(np.any(rendered_unique[1:] == rendered_unique[:-1]))
