"""
The functions in this file are addapted from qcodes to allow for thread safe
and single parameter loading. See:
    qcodes.dataset.data_set_cache
    qcodes.dataset.sqlite.queries
for the original functions of similar names as well as typing.
"""
from math import prod
from typing import TYPE_CHECKING

import numpy.typing as npt
from qcodes.dataset.data_set_cache import _merge_data
from qcodes.dataset.sqlite.queries import completed

from qplot.datahandling.parameter_data import (
    _check_noop,
    flatten_record_columns,
    get_parameter_data_for_one_paramtree,
    integer_preserving_dtype,
)
from qplot.datahandling.qcodes_cache import (
    cache_data,
    cache_dataset_completed,
    cache_dataset_connection,
    cache_dataset_run_id,
    cache_is_live,
    cache_lock,
    cache_parameter_is_synchronized,
    prepare_cache_if_empty,
)

if TYPE_CHECKING:
    from qcodes.dataset.data_set_cache import DataSetCacheWithDBBackend
    from qcodes.dataset.descriptions.param_spec import ParamSpec


def append_shaped_parameter_data_to_existing_arrays(
    rundescriber,
    meas_parameter : str,
    write_status,
    existing_data,
    new_data,
    check_cancelled=_check_noop,
):
    """
    Append datadict to an already existing datadict and return the merged
    data.

    Args:
        rundescriber: The rundescriber that describes the run
        write_status: Mapping from dependent parameter name to number of rows
          written to the cache previously.
        new_data: Mapping from dependent parameter name to mapping
          from parameter name to numpy arrays that the data should be
          appended to.
        existing_data: Mapping from dependent parameter name to mapping
          from parameter name to numpy arrays of new data.

    Returns:
        Updated write and read status, and the updated ``data``

    """
    merged_data = {}

    updated_write_status = dict(write_status)

    existing_data_1_tree = existing_data.get(meas_parameter, {})

    new_data_1_tree = new_data.get(meas_parameter, {})

    shapes = rundescriber.shapes
    if shapes is not None:
        shape = shapes.get(meas_parameter, None)
    else:
        shape = None

    overflow_count = None
    # QCoDeS inserts shaped refreshes into existing arrays in place. Work on
    # private copies so a concurrent worker cannot mutate the shared cache
    # before qPlot's monotonic commit check.
    if shape is not None:
        # Object cells count records; shaped cache offsets count samples.
        # Flatten only the new records so QCoDeS' usual padding and offsets
        # still apply, including the same missing-value handling.
        if any(values.dtype.kind == "O" for values in new_data_1_tree.values()):
            new_data_1_tree = flatten_record_columns(new_data_1_tree, check_cancelled)
        private_data = {}
        for name, values in existing_data_1_tree.items():
            check_cancelled()
            incoming = new_data_1_tree.get(name)
            dtype = values.dtype
            if (
                values.size and incoming is not None and incoming.size
                and values.dtype.kind in "biufcO" and incoming.dtype.kind in "biufcO"
            ):
                # QCoDeS' shaped insertion assigns into the cached dtype.
                # Promote every numeric column before insertion, including
                # coordinates and complex values that plot validation rejects.
                # Empty cache placeholders must not dictate the first dtype.
                dtype = integer_preserving_dtype((values.dtype, incoming.dtype))
            # Shaped insertion writes through ravel(), so retain the old
            # copy() behavior of making C-contiguous working arrays.
            private_data[name] = values.astype(dtype, order="C", copy=True)
        existing_data_1_tree = private_data
        check_cancelled()
        acquired_before = write_status.get(meas_parameter) or 0
        incoming = new_data_1_tree.get(meas_parameter)
        acquired_after = acquired_before + (incoming.size if incoming is not None else 0)
        if acquired_after > prod(shape):
            # Retain the flattened fallback for scans exceeding their plan,
            # but append to acquired samples, never to unused cache padding.
            existing_data_1_tree = {
                name: values.ravel()[:acquired_before]
                for name, values in existing_data_1_tree.items()
            }
            new_data_1_tree = {
                name: values.reshape(-1) for name, values in new_data_1_tree.items()
            }
            shape = None
            overflow_count = acquired_after

    if shape is None:
        # QCoDeS appends dense record columns with NumPy's usual promotion.
        # Promote both sides first when signed/unsigned (or integer/float)
        # records need object cells to retain every acquired integer.
        existing_data_1_tree = dict(existing_data_1_tree)
        new_data_1_tree = dict(new_data_1_tree)
        for name, incoming in new_data_1_tree.items():
            check_cancelled()
            previous = existing_data_1_tree.get(name)
            if previous is None or not previous.size or not incoming.size:
                continue
            dtype = integer_preserving_dtype((previous.dtype, incoming.dtype))
            if dtype.kind == "O":
                existing_data_1_tree[name] = previous.astype(dtype, copy=False)
                new_data_1_tree[name] = incoming.astype(dtype, copy=False)

    (merged_data[meas_parameter], updated_write_status[meas_parameter]) = (
        _merge_data(
            existing_data_1_tree,
            new_data_1_tree,
            shape,
            single_tree_write_status=write_status.get(meas_parameter),
            meas_parameter=meas_parameter,
        )
    )
    if overflow_count is not None:
        # Unshaped QCoDeS appends reset this status; planned trees still need
        # their acquired sample extent for subsequent reads and processing.
        updated_write_status[meas_parameter] = overflow_count
    check_cancelled()
    return updated_write_status, merged_data


def load_param_data_from_db_prep(
        cache : "DataSetCacheWithDBBackend",
        param : "ParamSpec",
        connection=None,
        ):
    if cache_is_live(cache):
        raise RuntimeError(
            "Cannot load data into this cache from the "
            "database because this dataset is being built "
            "in-memory."
        )

    if cache_parameter_is_synchronized(cache, param.name):
        return True, cache_dataset_completed(cache)

    with cache_lock(cache):
        is_completed = completed(
            connection or cache_dataset_connection(cache),
            cache_dataset_run_id(cache),
            )
        if cache_data(cache) == {}:
            prepare_cache_if_empty(cache)

    # The worker must load and process the final rows before publishing this
    # database completion observation to the viewer dataset.
    return False, is_completed


def load_param_data_from_db(
    conn,
    table_name,
    rundescriber,
    meas_parameter : str,
    write_status,
    read_status,
    existing_data,
    end: int | None = None,
    *,
    check_cancelled=_check_noop,
):
    # Data fetch
    updated_read_status: dict[str, int] = dict(read_status)
    new_data_dict: dict[str, dict[str, npt.NDArray]] = {}
    
    start = read_status.get(meas_parameter, 0) + 1
    new_data, n_rows_read = get_parameter_data_for_one_paramtree(
        conn,
        table_name,
        rundescriber=rundescriber,
        output_param=meas_parameter,
        start=start,
        end=end,
        check_cancelled=check_cancelled,
    )
    new_data_dict[meas_parameter] = new_data
    updated_read_status[meas_parameter] = start + n_rows_read - 1

    # Data Update
    (updated_write_status, merged_data) = (
        append_shaped_parameter_data_to_existing_arrays(
            rundescriber, meas_parameter, write_status, existing_data, new_data_dict,
            check_cancelled=check_cancelled,
        )
    )
    
    return (
        updated_read_status,
        updated_write_status,
        merged_data
        )
