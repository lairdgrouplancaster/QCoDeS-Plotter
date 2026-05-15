from qplot.datahandling.qcodes_cache import (
    cache_database_path,
    cache_has_no_written_data,
    cache_parameter_data,
    cache_table_name,
    parameter_is_complete,
    set_parameter_complete,
    update_cache_parameter_data,
)


class Dataset:
    table_name = "results_1"
    path_to_db = "experiment.db"


class Cache:
    def __init__(self):
        self._dataset = Dataset()
        self._read_status = {"signal": 0}
        self._write_status = {"signal": 0}
        self._data = {"signal": {"signal": [1, 2]}}


class Param:
    def __init__(self):
        self._complete = False


def test_cache_accessors_read_dataset_and_parameter_data():
    cache = Cache()

    assert cache_table_name(cache) == "results_1"
    assert cache_database_path(cache) == "experiment.db"
    assert cache_parameter_data(cache, "signal") == {"signal": [1, 2]}
    assert cache_has_no_written_data(cache)


def test_cache_update_writes_single_parameter_state():
    cache = Cache()

    update_cache_parameter_data(
        cache,
        "signal",
        {"signal": 4},
        {"signal": 4},
        {"signal": {"signal": [3, 4]}},
    )

    assert cache._read_status["signal"] == 4
    assert cache._write_status["signal"] == 4
    assert cache._data["signal"] == {"signal": [3, 4]}
    assert not cache_has_no_written_data(cache)


def test_parameter_completion_helpers_set_private_flag():
    param = Param()

    assert not parameter_is_complete(param)

    set_parameter_complete(param, True)

    assert parameter_is_complete(param)
