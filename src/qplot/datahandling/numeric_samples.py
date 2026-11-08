"""Precision-preserving sample primitives, independent of database readers."""

from decimal import Decimal
from fractions import Fraction

import numpy as np


def integer_preserving_dtype(dtypes):
    """Use object cells when common-dtype promotion would convert integers.

    In particular, NumPy combines uint64 and int64 as float64, which cannot
    represent every stored integer. Mixed integer/float or integer/complex
    records need the same protection. Homogeneous numeric arrays stay dense.
    """
    dtypes = set(dtypes)
    common = np.result_type(*dtypes) if dtypes else np.dtype(float)
    if common.kind not in "iuO" and any(dtype.kind in "iu" for dtype in dtypes):
        return np.dtype(object)
    return common


def concatenate_record_samples(parts):
    """Join flattened records without rounding any original integer samples."""
    dtype = integer_preserving_dtype(part.dtype for part in parts)
    return np.concatenate(parts, dtype=dtype) if parts else np.array([], dtype=dtype)


def numeric_isfinite(values):
    """Inspect numeric object cells without changing the stored sample array."""
    values = np.asarray(values)
    if values.dtype.kind != "O":
        return np.isfinite(values)
    if any(isinstance(value, (complex, np.complexfloating)) for value in values.flat):
        return np.asarray([
            bool(np.isfinite(value)) if isinstance(value, (complex, np.complexfloating))
            else value is not None and bool(np.isfinite(float(value)))
            for value in values.flat
        ], dtype=bool).reshape(values.shape)
    try:
        with np.errstate(over="ignore"):
            converted = values.astype(float)
        finite = np.asarray(np.isfinite(converted))
        retry = np.isinf(converted)
    except OverflowError:
        # Exact integer/Fraction cells can overflow the conversion itself.
        finite = np.zeros(values.shape, dtype=bool)
        retry = np.ones(values.shape, dtype=bool)
    # Mixed QCoDeS records retain extended floats in object cells. Overflow
    # during the fast binary64 check must not turn finite acquired samples
    # into missing heatmap cells or prevent a representable centered result.
    # NaNs (including absent None cells) need no retry; large missing regions
    # in partial scans must retain their vectorized classification.
    for index in np.flatnonzero(retry):
        value = values.flat[index]
        if isinstance(value, (int, np.integer, Fraction)):
            finite.flat[index] = True
        elif isinstance(value, Decimal):
            finite.flat[index] = value.is_finite()
        elif isinstance(value, np.floating):
            finite.flat[index] = np.isfinite(value)
        else:
            finite.flat[index] = value is not None and np.isfinite(float(value))
    return finite


def numeric_isnan(values):
    """Identify missing samples in dense arrays or exact numeric object cells."""
    values = np.asarray(values)
    if values.dtype.kind == "O" and any(
        isinstance(value, (complex, np.complexfloating)) for value in values.flat
    ):
        return np.asarray([
            bool(np.isnan(value)) if isinstance(value, (complex, np.complexfloating))
            else value is None or bool(np.isnan(float(value)))
            for value in values.flat
        ], dtype=bool).reshape(values.shape)
    return np.isnan(values.astype(float) if values.dtype.kind == "O" else values)
