"""
Compatibility helpers for QCoDeS dataset cache internals.

qPlot needs per-parameter refreshes that QCoDeS does not expose as a stable
public API. Keep those private-attribute touches in this module so future
QCoDeS upgrades have one place to adapt.
"""


def cache_dataset(cache):
    return cache._dataset


def cache_table_name(cache):
    return cache_dataset(cache).table_name


def cache_database_path(cache):
    return cache_dataset(cache).path_to_db


def cache_rundescriber(cache):
    return cache.rundescriber


def cache_read_status(cache):
    return cache._read_status


def cache_write_status(cache):
    return cache._write_status


def cache_data(cache):
    return cache._data


def cache_parameter_data(cache, parameter_name):
    return cache_data(cache)[parameter_name]


def cache_is_live(cache):
    return cache.live


def cache_dataset_connection(cache):
    return cache_dataset(cache).conn


def cache_dataset_run_id(cache):
    return cache_dataset(cache).run_id


def cache_dataset_completed(cache):
    return cache_dataset(cache).completed


def set_cache_dataset_completed(cache, completed):
    cache_dataset(cache).completed = completed


def parameter_is_complete(param):
    return param._complete is True


def set_parameter_complete(param, complete=True):
    param._complete = complete


def prepare_cache_if_empty(cache):
    if cache_data(cache) == {}:
        cache.prepare()


def update_cache_parameter_data(
        cache,
        parameter_name,
        updated_read_status,
        updated_write_status,
        updated_data,
        ):
    cache_read_status(cache)[parameter_name] = updated_read_status[parameter_name]
    cache_write_status(cache)[parameter_name] = updated_write_status[parameter_name]
    cache_data(cache)[parameter_name] = updated_data[parameter_name]


def cache_has_no_written_data(cache):
    return all(
        status is None or status == 0
        for status in cache_write_status(cache).values()
        )
