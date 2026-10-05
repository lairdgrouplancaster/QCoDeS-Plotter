"""QCoDeS parameter-tree decoding shared by plotting and raw export.

Keep QCoDeS' row selection and scalar expansion, but collect unequal array
shapes explicitly. NumPy's ``array(records, dtype=object)`` still attempts to
broadcast nested dimensions and cannot represent all valid QCoDeS records.
"""

from collections.abc import Callable

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
    return np.isfinite(values.astype(float) if values.dtype.kind == "O" else values)


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


def get_parameter_data_for_one_paramtree(
    conn, table_name, rundescriber, output_param, start=None, end=None,
    *, check_cancelled: Callable[[], None] = _check_noop,
    preserve_integers=True,
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

    # Expand within each acquisition record, never across record boundaries.
    for index, record in enumerate(records):
        check_cancelled()
        expanded = [record]
        _expand_data_to_arrays(expanded, specs)
        records[index] = expanded[0]

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
    for record in zip(*data.values(), strict=True):
        check_cancelled()
        if len({np.size(value) for value in record}) > 1:
            raise ValueError("Setpoint and measurement shapes do not match.")
    flattened = {}
    for name, column in data.items():
        check_cancelled()
        if column.dtype.kind != "O":
            flattened[name] = column.reshape(-1)
            continue
        parts = []
        for record in column:
            check_cancelled()
            parts.append(np.asarray(record).ravel())
        flattened[name] = concatenate_record_samples(parts)
    check_cancelled()
    return flattened
