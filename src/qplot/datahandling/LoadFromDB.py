"""
The functions in this file are adapted from QCoDeS to allow for thread-safe
and single parameter loading. See:
    qcodes.dataset.data_set_cache
    qcodes.dataset.sqlite.queries
for the original functions of similar names as well as typing.
"""
from collections.abc import Callable
from dataclasses import dataclass
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


@dataclass(frozen=True)
class _PreparedMerge:
    """Columns ready for QCoDeS insertion and any planned-overflow extent.

    A shaped merge owns private arrays because QCoDeS inserts in place. The
    frozen record keeps those arrays and their shape/status policy together;
    it does not make the arrays themselves immutable.
    """

    existing: dict[str, npt.NDArray]
    incoming: dict[str, npt.NDArray]
    shape: tuple[int, ...] | None
    acquired_count: int | None = None


def _prepare_unshaped_merge(
    existing: dict[str, npt.NDArray],
    incoming: dict[str, npt.NDArray],
    check_cancelled: Callable[[], None],
    *,
    acquired_count: int | None = None,
) -> _PreparedMerge:
    """Protect recorded integers before QCoDeS concatenates dense columns."""
    # QCoDeS appends dense record columns with NumPy's usual promotion.
    # Promote both sides first when signed/unsigned (or integer/float)
    # records need object cells to retain every acquired integer.
    existing = dict(existing)
    incoming = dict(incoming)
    for name, values in incoming.items():
        check_cancelled()
        previous = existing.get(name)
        if previous is None or not previous.size or not values.size:
            continue
        dtype = integer_preserving_dtype((previous.dtype, values.dtype))
        if dtype.kind == "O":
            existing[name] = previous.astype(dtype, copy=False)
            incoming[name] = values.astype(dtype, copy=False)
    return _PreparedMerge(existing, incoming, None, acquired_count)


def _prepare_shaped_merge(
    existing: dict[str, npt.NDArray],
    incoming: dict[str, npt.NDArray],
    shape: tuple[int, ...],
    written_count: int | None,
    meas_parameter: str,
    check_cancelled: Callable[[], None],
) -> _PreparedMerge:
    """Prepare private insertion buffers, or acquired-only overflow columns."""
    # Object cells count records; shaped cache offsets count samples.
    # Flatten only the new records so QCoDeS' usual padding and offsets
    # still apply, including the same missing-value handling.
    if (any(values.dtype.kind == "O" for values in incoming.values())
            or len({values.shape for values in incoming.values()}) > 1):
        incoming = flatten_record_columns(incoming, check_cancelled)
    private_data = {}
    for name, values in existing.items():
        check_cancelled()
        new_values = incoming.get(name)
        dtype = values.dtype
        if (
            values.size and new_values is not None and new_values.size
            and values.dtype.kind in "biufcO" and new_values.dtype.kind in "biufcO"
        ):
            # Promote every numeric column before insertion, including
            # coordinates and complex values that plot validation rejects.
            # Empty cache placeholders must not dictate the first dtype.
            dtype = integer_preserving_dtype((values.dtype, new_values.dtype))
        # Shaped insertion writes through ravel(), so preserve C-contiguous
        # working arrays and never let it mutate the shared cache directly.
        private_data[name] = values.astype(dtype, order="C", copy=True)
    check_cancelled()
    acquired_before = written_count or 0
    new_values = incoming.get(meas_parameter)
    acquired_after = acquired_before + (new_values.size if new_values is not None else 0)
    if acquired_after <= prod(shape):
        return _PreparedMerge(private_data, incoming, shape)

    # Scans exceeding their plan append to acquired samples, never to unused
    # cache padding. The unshaped merge must retain their acquired extent.
    acquired_data = {
        name: values.ravel()[:acquired_before]
        for name, values in private_data.items()
    }
    incoming = {name: values.reshape(-1) for name, values in incoming.items()}
    return _prepare_unshaped_merge(
        acquired_data, incoming, check_cancelled, acquired_count=acquired_after,
    )


def append_shaped_parameter_data_to_existing_arrays(
    rundescriber,
    meas_parameter: str,
    write_status,
    existing_data,
    new_data,
    check_cancelled=_check_noop,
):
    """Append one parameter tree without changing its shared cache arrays.

    ``existing_data`` contains the cached columns; ``new_data`` contains newly
    read columns. Both are indexed first by measured parameter, then by column.
    ``write_status`` tracks QCoDeS insertion offsets, counting acquired samples
    for planned shapes. Read status counts database rows and is managed by the
    caller independently of these sample offsets.

    Return a copied write-status mapping and the merged measured-parameter tree.
    Preparation preserves numeric precision and gives shaped insertion private
    arrays so a cancelled or superseded worker cannot modify the shared cache.
    """
    updated_write_status = dict(write_status)
    existing = existing_data.get(meas_parameter, {})
    incoming = new_data.get(meas_parameter, {})

    if incoming and not incoming[meas_parameter].size:
        # QCoDeS skips an empty column independently when merging unshaped
        # caches. Empty array outputs can retain singleton setpoints, which
        # would then be appended without their measurement and misalign the
        # next refresh. Validate those records and retain zero samples in
        # every field; the separate read status still counts acquired rows.
        incoming = flatten_record_columns(incoming, check_cancelled)

    shapes = rundescriber.shapes
    shape = shapes.get(meas_parameter) if shapes is not None else None
    written_count = write_status.get(meas_parameter)
    if shape is None:
        prepared = _prepare_unshaped_merge(existing, incoming, check_cancelled)
    else:
        prepared = _prepare_shaped_merge(
            existing, incoming, shape, written_count, meas_parameter, check_cancelled,
        )

    merged, updated_count = _merge_data(
        prepared.existing,
        prepared.incoming,
        prepared.shape,
        single_tree_write_status=written_count,
        meas_parameter=meas_parameter,
    )
    if prepared.acquired_count is not None:
        # Unshaped QCoDeS appends reset this status; planned trees still need
        # their acquired sample extent for subsequent reads and processing.
        updated_count = prepared.acquired_count
    updated_write_status[meas_parameter] = updated_count
    check_cancelled()
    return updated_write_status, {meas_parameter: merged}


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
    max_decode_bytes=None,
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
        max_decode_bytes=max_decode_bytes,
        existing_data=existing_data.get(meas_parameter, {}),
        written_count=write_status.get(meas_parameter),
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
