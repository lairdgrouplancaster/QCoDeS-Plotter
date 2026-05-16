from qplot.datahandling.qcodes_cache import (
    cache_database_path,
    cache_dataset_completed,
    cache_dataset_connection,
    cache_dataset_run_id,
    cache_has_no_written_data,
    cache_is_live,
    cache_parameter_data,
    cache_table_name,
    parameter_is_complete,
    prepare_cache_if_empty,
    set_cache_dataset_completed,
    set_parameter_complete,
    update_cache_parameter_data,
)
from qplot.datahandling import LoadFromDB as load_from_db


class Dataset:
    table_name = "results_1"
    path_to_db = "experiment.db"
    conn = object()
    run_id = 7
    completed = False


class Cache:
    def __init__(self):
        self._dataset = Dataset()
        self._read_status = {"signal": 0}
        self._write_status = {"signal": 0}
        self._data = {"signal": {"signal": [1, 2]}}
        self.live = False
        self.prepare_called = False

    def prepare(self):
        self.prepare_called = True
        self._data = {"signal": {"signal": [5, 6]}}


class Param:
    def __init__(self):
        self._complete = False


def test_cache_accessors_read_dataset_and_parameter_data():
    cache = Cache()

    assert cache_table_name(cache) == "results_1"
    assert cache_database_path(cache) == "experiment.db"
    assert cache_dataset_connection(cache) is cache._dataset.conn
    assert cache_dataset_run_id(cache) == 7
    assert not cache_dataset_completed(cache)
    assert not cache_is_live(cache)
    assert cache_parameter_data(cache, "signal") == {"signal": [1, 2]}
    assert cache_has_no_written_data(cache)


def test_cache_prepare_helper_prepares_empty_cache():
    cache = Cache()
    cache._data = {}

    prepare_cache_if_empty(cache)

    assert cache.prepare_called
    assert cache._data == {"signal": {"signal": [5, 6]}}


def test_dataset_completion_helper_sets_private_flag():
    cache = Cache()

    set_cache_dataset_completed(cache, True)

    assert cache_dataset_completed(cache)


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


def test_load_param_data_from_db_prep_marks_completed_param(monkeypatch):
    cache = Cache()
    cache._data = {}
    param = Param()

    monkeypatch.setattr(load_from_db, "completed", lambda conn, run_id: True)

    result = load_from_db.load_param_data_from_db_prep(cache, param)

    assert result is False
    assert cache._dataset.completed
    assert param._complete
    assert cache.prepare_called


def test_load_param_data_from_db_prep_skips_completed_param(monkeypatch):
    cache = Cache()
    param = Param()
    param._complete = True

    def fail_if_called(conn, run_id):
        raise AssertionError("completed should not be queried for completed params")

    monkeypatch.setattr(load_from_db, "completed", fail_if_called)

    assert load_from_db.load_param_data_from_db_prep(cache, param) is True
    assert not cache.prepare_called


def test_load_param_data_from_db_prep_rejects_live_cache():
    cache = Cache()
    cache.live = True
    param = Param()

    try:
        load_from_db.load_param_data_from_db_prep(cache, param)
    except RuntimeError as err:
        assert "being built in-memory" in str(err)
    else:
        raise AssertionError("Expected live cache to be rejected")
