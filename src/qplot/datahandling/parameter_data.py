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


def _record_column(values, dtype, check_cancelled):
    """Keep equal shapes dense and unequal shapes in separate object cells."""
    shapes = set()
    for value in values:
        check_cancelled()
        shapes.add(np.shape(value))
    if len(shapes) <= 1:
        check_cancelled()
        return np.array(values, dtype=dtype)

    column = np.empty(len(values), dtype=object)
    for index, value in enumerate(values):
        check_cancelled()
        # Assign one cell at a time: slice assignment also tries broadcasting.
        column[index] = np.asarray(value, dtype=dtype)
    return column


def get_parameter_data_for_one_paramtree(
    conn, table_name, rundescriber, output_param, start=None, end=None,
    *, check_cancelled: Callable[[], None] = _check_noop,
):
    """Decode one tree on an existing read-only connection.

    start/end and the returned count refer to non-NULL storage records, not
    flattened samples. No connections, transactions or cache writes are added.
    Errors from reading, expansion or dtype conversion propagate unchanged.
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
        data[spec.name] = _record_column(values, dtype, check_cancelled)
    check_cancelled()
    return data, count


def parameter_data_for_export(dataset, parameter_name):
    """Read DB-backed trees locally; in-memory datasets provide their own data."""
    if isinstance(dataset, DataSet):
        data, _ = get_parameter_data_for_one_paramtree(
            dataset.conn, dataset.table_name, dataset.description, parameter_name,
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
        flattened[name] = np.concatenate(parts) if parts else np.array([])
    check_cancelled()
    return flattened
