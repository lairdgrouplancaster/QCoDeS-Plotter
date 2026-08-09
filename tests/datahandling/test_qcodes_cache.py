import numpy as np

from qplot.datahandling import LoadFromDB as load_from_db
from qplot.datahandling.qcodes_cache import (
    cache_database_path,
    cache_dataset_completed,
    cache_dataset_connection,
    cache_dataset_run_id,
    cache_has_no_written_data,
    cache_is_live,
    cache_parameter_data,
    cache_parameter_is_synchronized,
    cache_table_name,
    parameter_is_complete,
    prepare_cache_if_empty,
    set_cache_dataset_completed,
    set_cache_parameter_synchronized,
    set_parameter_complete,
    snapshot_cache_parameter_state,
    update_cache_parameter_data,
)


class Dataset:
    table_name = "results_1"
    path_to_db = "experiment.db"
    conn = object()
    run_id = 7
    _completed = False

    @property
    def completed(self):
        return self._completed


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
        self.name = "signal"
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


def test_cache_parameter_snapshot_copies_state_mappings():
    cache = Cache()

    write_status, read_status, data = snapshot_cache_parameter_state(cache, "signal")
    cache._write_status["signal"] = 9
    cache._read_status["signal"] = 9
    cache._data["signal"]["extra"] = [3]

    assert write_status == {"signal": 0}
    assert read_status == {"signal": 0}
    assert data == {"signal": {"signal": [1, 2]}}


def test_dataset_completion_helper_sets_private_flag_without_property_setter():
    class SetterRejectingDataset(Dataset):
        def __init__(self):
            self._completed = False

        @property
        def completed(self):
            return self._completed

        @completed.setter
        def completed(self, _value):
            raise AssertionError("QCoDeS completion setter must not be called")

    cache = Cache()
    cache._dataset = SetterRejectingDataset()

    set_cache_dataset_completed(cache, True)

    assert cache_dataset_completed(cache)


def test_cache_update_writes_single_parameter_state():
    cache = Cache()

    committed = update_cache_parameter_data(
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
    assert committed


def test_cache_update_publishes_completion_after_parameter_state():
    cache = Cache()

    committed = update_cache_parameter_data(
        cache,
        "signal",
        {"signal": 4},
        {"signal": 4},
        {"signal": {"signal": [3, 4]}},
        dataset_completed=True,
    )

    assert committed
    assert cache_dataset_completed(cache)
    assert cache_parameter_is_synchronized(cache, "signal")
    assert cache._data["signal"] == {"signal": [3, 4]}


def test_cache_update_does_not_revert_published_completion():
    cache = Cache()
    set_cache_dataset_completed(cache, True)

    committed = update_cache_parameter_data(
        cache,
        "signal",
        {"signal": 4},
        {"signal": 4},
        {"signal": {"signal": [3, 4]}},
        dataset_completed=False,
    )

    assert committed
    assert cache_dataset_completed(cache)


def test_cache_update_rejects_result_older_than_current_parameter_state():
    cache = Cache()
    cache._read_status["signal"] = 8
    cache._write_status["signal"] = 8

    committed = update_cache_parameter_data(
        cache,
        "signal",
        {"signal": 4},
        {"signal": 4},
        {"signal": {"signal": [3, 4]}},
        dataset_completed=True,
    )

    assert not committed
    assert cache._read_status["signal"] == 8
    assert cache._write_status["signal"] == 8
    assert cache._data["signal"] == {"signal": [1, 2]}
    assert not cache_dataset_completed(cache)
    assert not cache_parameter_is_synchronized(cache, "signal")


def test_cache_update_accepts_newer_read_with_reset_unshaped_write_status():
    cache = Cache()
    cache._read_status["signal"] = 1
    cache._write_status["signal"] = 1

    committed = update_cache_parameter_data(
        cache,
        "signal",
        {"signal": 2},
        {"signal": 0},
        {"signal": {"signal": [1, 2, 3]}},
    )

    assert committed
    assert cache._read_status["signal"] == 2
    assert cache._write_status["signal"] == 0
    assert cache._data["signal"] == {"signal": [1, 2, 3]}


def test_cache_update_accepts_first_result_after_none_shaped_status():
    cache = Cache()
    cache._read_status["signal"] = None
    cache._write_status["signal"] = None

    committed = update_cache_parameter_data(
        cache,
        "signal",
        {"signal": 0},
        {"signal": 0},
        {"signal": {"signal": []}},
    )

    assert committed
    assert cache._read_status["signal"] == 0
    assert cache._write_status["signal"] == 0


def test_parameter_completion_helpers_set_private_flag():
    param = Param()

    assert not parameter_is_complete(param)

    set_parameter_complete(param, True)

    assert parameter_is_complete(param)


def test_shaped_merge_does_not_mutate_shared_existing_array():
    class RunDescriber:
        shapes = {"signal": (2,)}

    existing = {"signal": {"signal": np.array([1.0, np.nan])}}

    write_status, merged = load_from_db.append_shaped_parameter_data_to_existing_arrays(
        RunDescriber(),
        "signal",
        {"signal": 1},
        existing,
        {"signal": {"signal": np.array([2.0])}},
        )

    np.testing.assert_array_equal(existing["signal"]["signal"], [1.0, np.nan])
    np.testing.assert_array_equal(merged["signal"]["signal"], [1.0, 2.0])
    assert write_status["signal"] == 2


def test_load_param_data_from_db_prep_defers_parameter_completion_until_commit(monkeypatch):
    cache = Cache()
    cache._data = {}
    param = Param()

    monkeypatch.setattr(load_from_db, "completed", lambda conn, run_id: True)

    parameter_complete, dataset_completed = (
        load_from_db.load_param_data_from_db_prep(cache, param)
        )

    assert parameter_complete is False
    assert dataset_completed is True
    assert not cache._dataset.completed
    assert not param._complete
    assert not cache_parameter_is_synchronized(cache, "signal")
    assert cache.prepare_called


def test_load_param_data_from_db_prep_skips_completed_param(monkeypatch):
    cache = Cache()
    param = Param()
    set_cache_parameter_synchronized(cache, "signal", True)

    def fail_if_called(conn, run_id):
        raise AssertionError("completed should not be queried for completed params")

    monkeypatch.setattr(load_from_db, "completed", fail_if_called)

    assert load_from_db.load_param_data_from_db_prep(cache, param) == (True, False)
    assert not cache.prepare_called


def test_parameter_synchronization_is_shared_across_reconstructed_params(monkeypatch):
    cache = Cache()
    first_param = Param()
    reconstructed_param = Param()
    set_cache_parameter_synchronized(cache, first_param.name, True)

    def fail_if_called(conn, run_id):
        raise AssertionError("completed should not be queried after cache sync")

    monkeypatch.setattr(load_from_db, "completed", fail_if_called)

    assert load_from_db.load_param_data_from_db_prep(
        cache,
        reconstructed_param,
        ) == (True, False)


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
