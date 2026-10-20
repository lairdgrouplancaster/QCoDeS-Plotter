"""QCoDeS parameter-tree decoding shared by plotting and raw export.

Keep QCoDeS' row selection and scalar expansion, but collect unequal array
shapes explicitly. NumPy's ``array(records, dtype=object)`` still attempts to
broadcast nested dimensions and cannot represent all valid QCoDeS records.
"""

from collections.abc import Callable
from decimal import Decimal
from fractions import Fraction
from math import prod

import numpy as np
from qcodes.dataset.data_set import DataSet
from qcodes.dataset.sqlite.queries import (
    _expand_data_to_arrays,
    _get_data_for_one_param_tree,
)


def _check_noop():
    pass


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
    return np.isnan(values.astype(float) if values.dtype.kind == "O" else values)


def _record_column(values, dtype, check_cancelled, *, preserve_integers=True):
    """Keep equal shapes dense and unequal shapes in separate object cells."""
    shapes = set()
    dtypes = set()
    arrays = []
    for value in values:
        check_cancelled()
        array = np.asarray(value, dtype=dtype)
        shapes.add(array.shape)
        dtypes.add(array.dtype)
        arrays.append(array)
    if len(shapes) <= 1:
        check_cancelled()
        if preserve_integers:
            dtype = integer_preserving_dtype(dtypes)
        return np.array(arrays, dtype=dtype)

    column = np.empty(len(values), dtype=object)
    for index, array in enumerate(arrays):
        check_cancelled()
        # Assign one cell at a time: slice assignment also tries broadcasting.
        column[index] = array
    return column


def _restore_missing_record_fields(record, specs, expanded, check_cancelled):
    # The selected output is always first and non-NULL. Its expanded shape
    # includes the zero-sample case, so absent cells cannot create samples.
    shape = np.shape(expanded[0])
    values = iter(expanded)
    restored = []
    for value, spec in zip(record, specs, strict=True):
        check_cancelled()
        if value is not None:
            restored.append(next(values))
        else:
            missing = (None if spec.type == "text" else
                       complex(np.nan, np.nan) if spec.type == "complex" else np.nan)
            # Retain only a scalar until the final column is assembled. This
            # avoids allocating a second full buffer for every absent field.
            restored.append(np.broadcast_to(np.asarray(missing), shape))
    return tuple(restored)


def _project_record_expansion(record, specs, check_cancelled):
    """Apply QCoDeS' broadcast shapes and dtypes using allocation-free views."""
    present = [(value, spec) for value, spec in zip(record, specs, strict=True)
               if value is not None]
    array_shapes = [np.shape(value) for value, spec in present if spec.type == "array"]
    if not array_shapes:
        projected = [value for value, _spec in present]
    else:
        # QCoDeS first expands scalar storage to the first present array's
        # shape, then expands one-element arrays to the first largest shape.
        # A view has the same dtype/size without allocating its repeated cells.
        projected = []
        for value, spec in present:
            check_cancelled()
            if spec.type == "array":
                projected.append(np.asarray(value))
            else:
                dtype = (np.float64 if spec.type == "numeric" else
                         np.complex128 if spec.type == "complex" else f"U{len(value)}")
                projected.append(np.broadcast_to(np.asarray(value, dtype=dtype), array_shapes[0]))
        largest = max(projected, key=lambda value: value.size)
        if largest.size > 1:
            # np.full accepts a singleton of any rank as its fill value;
            # broadcast_to alone rejects extra leading singleton dimensions.
            projected = [np.broadcast_to(value.reshape(()), largest.shape) if value.size == 1 else value
                         for value in projected]
    return _restore_missing_record_fields(record, specs, projected, check_cancelled)


def _expand_record(record, specs, check_cancelled):
    """Expand acquired fields before restoring absent ancillary cells."""
    if all(value is not None for value in record):
        expanded = [record]
        _expand_data_to_arrays(expanded, specs)
        return expanded[0]

    # Inferred values can be stored with several basis parameters in separate
    # rows. Their other basis cells are SQL NULL, which QCoDeS' expansion does
    # not accept. Keep those fields out of expansion; retain every present
    # value and let QCoDeS apply its ordinary scalar/array rules unchanged.
    present = [(value, spec) for value, spec in zip(record, specs, strict=True)
               if value is not None]
    expanded = [tuple(value for value, _spec in present)]
    _expand_data_to_arrays(expanded, tuple(spec for _value, spec in present))
    return _restore_missing_record_fields(record, specs, expanded[0], check_cancelled)


def _column_layout(parts, check_cancelled):
    """Project scalar storage without materializing record-shaped columns."""
    count, text_bytes = 0, 0
    dtypes = set()
    for index, part in enumerate(parts):
        if index % 1024 == 0:
            check_cancelled()
        array = np.asarray(part)
        # Empty acquired records still participate in the decoder's dtype
        # promotion, even though they add no samples.
        dtypes.add(array.dtype)
        if not array.size:
            continue
        count += array.size
        if array.dtype.kind in "US":
            text_bytes = max(text_bytes, array.dtype.itemsize)
        elif array.dtype.kind == "O":
            for cell_index, value in enumerate(array.flat):
                if cell_index % 1024 == 0:
                    check_cancelled()
                if isinstance(value, (str, bytes)):
                    text_bytes = max(text_bytes, 4 * len(value))
    return count, dtypes, text_bytes


def _cached_column_parts(column):
    if column.dtype.kind == "O" and column.ndim == 1:
        # Ragged records retain whole arrays in object cells. Dense object
        # scalar cells must retain their object allocation cost instead.
        for value in column:
            yield value if isinstance(value, np.ndarray) else np.asarray(value, dtype=object)
    else:
        yield column


def _check_materialization_budget(
    records, specs, rundescriber, output_param, existing_data, written_count,
    max_decode_bytes, check_cancelled,
):
    """Bound the expanded tree and its eventual incremental cache allocation."""
    if max_decode_bytes is None:
        return
    existing_data = {} if existing_data is None else existing_data
    shape = (rundescriber.shapes or {}).get(output_param)
    planned_count = prod(shape) if shape is not None else 0
    projected_bytes = 0
    for index, spec in enumerate(specs):
        check_cancelled()
        count, dtypes, text_bytes = _column_layout(
            (record[index] for record in records), check_cancelled,
        )
        if spec.type == "numeric":
            # The decoder always converts SQL numeric columns to float64.
            dtypes, text_bytes = {np.dtype(np.float64)}, 0
        previous = existing_data.get(spec.name)
        prior_count = 0
        if previous is not None and previous.size:
            prior_count, prior_dtypes, prior_text = _column_layout(
                _cached_column_parts(previous), check_cancelled,
            )
            dtypes.update(prior_dtypes)
            text_bytes = max(text_bytes, prior_text)
        dtype = integer_preserving_dtype(dtypes)
        # Object cells retain Python numbers/strings as well as array slots.
        # Dense numeric columns use their actual itemsize, including extended
        # floats. Missing fields participate in the same promotion as data.
        item_bytes = max(128, text_bytes + 128) if dtype.kind == "O" else dtype.itemsize
        if shape is not None:
            count = max(planned_count, (written_count or 0) + count)
        else:
            count += prior_count
        projected_bytes += count * item_bytes
        if projected_bytes > max_decode_bytes:
            raise ValueError(
                "Expanded parameter data exceeds the plot decoding budget. "
                "No measurement fields have been omitted."
            )
    check_cancelled()


def get_parameter_data_for_one_paramtree(
    conn, table_name, rundescriber, output_param, start=None, end=None,
    *, check_cancelled: Callable[[], None] = _check_noop,
    preserve_integers=True,
    max_decode_bytes=None, existing_data=None, written_count=None,
):
    """Decode one tree on an existing read-only connection.

    start/end and the returned count refer to non-NULL storage records, not
    flattened samples. No connections, transactions or cache writes are added.
    Errors from reading, expansion or dtype conversion propagate unchanged.
    Plotting and CSV both retain the precision of stored array samples.
    """
    check_cancelled()
    records, specs, count = _get_data_for_one_param_tree(
        conn, table_name, rundescriber.interdeps, output_param, start, end, None,
    )
    check_cancelled()
    if specs[0].name != output_param:
        raise ValueError("The output parameter must be first in its parameter tree.")

    # Scalar text and one-element arrays can expand far beyond their stored
    # size. Check the complete projected tree before QCoDeS allocates any of
    # those broadcasts, including retained cache data and planned padding.
    if max_decode_bytes is not None:
        projected = []
        for record in records:
            check_cancelled()
            projected.append(_project_record_expansion(record, specs, check_cancelled))
        _check_materialization_budget(
            projected, specs, rundescriber, output_param, existing_data, written_count,
            max_decode_bytes, check_cancelled,
        )
        del projected

    # Expand within each acquisition record, never across record boundaries.
    for index, record in enumerate(records):
        check_cancelled()
        records[index] = _expand_record(record, specs, check_cancelled)

    data = {}
    for index, spec in enumerate(specs):
        check_cancelled()
        values = [record[index] for record in records]
        # Match QCoDeS' numeric SQL-column convention. Array dtypes retain
        # their precision, including integers and complex values for export.
        dtype = np.float64 if spec.type == "numeric" else None
        data[spec.name] = _record_column(
            values, dtype, check_cancelled, preserve_integers=preserve_integers,
        )
    check_cancelled()
    return data, count


def parameter_data_for_export(dataset, parameter_name):
    """Read DB-backed trees locally; in-memory datasets provide their own data."""
    if isinstance(dataset, DataSet):
        data, _ = get_parameter_data_for_one_paramtree(
            dataset.conn, dataset.table_name, dataset.description, parameter_name,
            preserve_integers=True,
        )
        return data
    return dataset.get_parameter_data(parameter_name).get(parameter_name, {})


def flatten_record_columns(data, check_cancelled=_check_noop):
    """Flatten aligned record cells before inserting into a planned scan shape."""
    empty_records = set()
    for index, record in enumerate(zip(*data.values(), strict=True)):
        check_cancelled()
        sizes = [np.size(value) for value in record]
        # Decoded trees are output-first. A zero-sample output may retain
        # singleton setpoints; they cannot contribute an acquired cache slot.
        # Keep empty slices below so their dtypes still take part in promotion.
        if sizes[0] == 0 and all(size in (0, 1) for size in sizes):
            empty_records.add(index)
        elif len(set(sizes)) > 1:
            raise ValueError("Setpoint and measurement shapes do not match.")
    flattened = {}
    for name, column in data.items():
        check_cancelled()
        if column.dtype.kind != "O" and not empty_records:
            flattened[name] = column.reshape(-1)
            continue
        parts = []
        for index, record in enumerate(column):
            check_cancelled()
            samples = np.asarray(record).ravel()
            parts.append(samples[:0] if index in empty_records else samples)
        flattened[name] = concatenate_record_samples(parts)
    check_cancelled()
    return flattened
