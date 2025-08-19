"""
The functions in this file are addapted from qcodes to allow for thread safe
and single parameter loading. See:
    qcodes.dataset.data_set_cache
    qcodes.dataset.sqlite.queries
for the original functions of similar names as well as typing.
"""
from qcodes.dataset.data_set_cache import _merge_data
from qcodes.dataset.sqlite.queries import (
    completed,
    get_parameter_data_for_one_paramtree,
    )


def append_shaped_parameter_data_to_existing_arrays(
    rundescriber,
    meas_parameter : str,
    write_status,
    existing_data,
    new_data,
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
    parameters = tuple(ps.name for ps in rundescriber.interdeps.non_dependencies)
    merged_data = {}

    updated_write_status = dict(write_status)

    existing_data_1_tree = existing_data.get(meas_parameter, {})

    new_data_1_tree = new_data.get(meas_parameter, {})

    shapes = rundescriber.shapes
    if shapes is not None:
        shape = shapes.get(meas_parameter, None)
    else:
        shape = None

    (merged_data[meas_parameter], updated_write_status[meas_parameter]) = (
        _merge_data(
            existing_data_1_tree,
            new_data_1_tree,
            shape,
            single_tree_write_status=write_status.get(meas_parameter),
            meas_parameter=meas_parameter,
        )
    )
    return updated_write_status, merged_data


def load_param_data_from_db_prep(
        cache : "DataSetCacheWithDBBackend",
        param : "paramSpec"
        ):
    if cache.live:
        raise RuntimeError(
            "Cannot load data into this cache from the "
            "database because this dataset is being built "
            "in-memory."
        )

    if param._complete == True: # Altered to be per param
        return True

    is_completed = completed(cache._dataset.conn, cache._dataset.run_id)
    if cache._dataset.completed != is_completed:
        cache._dataset.completed = is_completed
    if cache._dataset.completed:
        param._complete = True
    if cache._data == {}:
        cache.prepare()
    
    return False


def load_param_data_from_db(
    conn,
    table_name,
    rundescriber,
    meas_parameter : str,
    write_status,
    read_status,
    existing_data,
    end : int=None,
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
        callback=None,
    )
    new_data_dict[meas_parameter] = new_data
    updated_read_status[meas_parameter] = start + n_rows_read - 1

    # Data Update
    (updated_write_status, merged_data) = (
        append_shaped_parameter_data_to_existing_arrays(
            rundescriber, meas_parameter, write_status, existing_data, new_data_dict
        )
    )
    
    return (
        updated_read_status,
        updated_write_status,
        merged_data
        )
