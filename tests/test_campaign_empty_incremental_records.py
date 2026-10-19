"""Empty acquisitions cannot append unpaired setpoints to an unshaped cache."""

from contextlib import contextmanager

import numpy as np
import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.datahandling import LoadFromDB
from qplot.datahandling.LoadFromDB import load_param_data_from_db
from qplot.datahandling.parameter_data import (
    flatten_record_columns,
    parameter_data_for_export,
)
from qplot.datahandling.qcodes_cache import (
    snapshot_cache_parameter_state,
    update_cache_parameter_data,
)
from qplot.datahandling.readonly import load_by_id_read_only
from qplot.tools.worker import loader
from tests.datahandling.test_trusted_plot import protected_state


@contextmanager
def _source(tmp_path, dimensions, shaped):
    path = tmp_path / "empty-incremental.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("Empty incremental arrays", "regression")
    measurement = Measurement(exp=experiment)
    names = ("slow", "x") if dimensions == 2 else ("x",)
    for name in names:
        measurement.register_custom_parameter(name, paramtype="array")
    measurement.register_custom_parameter("signal", paramtype="array", setpoints=names)
    if shaped:
        measurement.set_shapes({"signal": (2, 3) if dimensions == 2 else (6,)})
    try:
        with measurement.run(write_in_background=False, in_memory_cache=False) as saver:
            def append(indices):
                indices = np.asarray(indices, dtype=int)
                x = indices % 3 if dimensions == 2 else indices
                values = np.asarray(indices, dtype=np.uint64) + np.uint64(2**63 + 1)
                record = [("x", x if indices.size else 99.), ("signal", values)]
                if dimensions == 2:
                    record.append(("slow", indices // 3 if indices.size else 99.))
                saver.add_result(*record)
                saver.flush_data_to_database(block=True)
                return values

            yield path, saver, append
    finally:
        saver.dataset.conn.close()
        experiment.conn.close()


def _worker(dataset, dimensions, *, read_data=True):
    parameters = {parameter.name: parameter for parameter in dataset.get_parameters()}
    worker = loader(dataset.cache, parameters["signal"], parameters,
                    {"x": "x", "y": "slow" if dimensions == 2 else "signal"},
                    read_data=read_data)
    worker.setAutoDelete(False)
    errors, finished = [], []
    worker.emitter.errorOccurred.connect(errors.append)
    worker.emitter.finished.connect(finished.append)
    worker.run()
    assert errors == []
    assert finished == [True]
    assert worker._sql_connection is None
    return worker


@pytest.mark.parametrize("dimensions", [1, 2])
@pytest.mark.parametrize("shaped", [False, True])
@pytest.mark.parametrize("empty_first", [False, True])
def test_empty_refreshes_preserve_samples_and_independent_row_cursor(
    tmp_path, dimensions, shaped, empty_first,
):
    batches = ([[], [], [0], [], [1, 2], [3, 4, 5], []] if empty_first
               else [[0], [], [], [1, 2], [3, 4, 5], []])
    with _source(tmp_path, dimensions, shaped) as (path, saver, append):
        dataset, expected, first_result = None, [], None
        try:
            for row_count, batch in enumerate(batches, 1):
                expected.extend(append(batch).tolist())
                before = protected_state(path)
                if dataset is None:
                    dataset = load_by_id_read_only(saver.run_id, str(path))
                worker = _worker(dataset, dimensions)
                assert worker.loaded_point_count == len(expected)
                result = (worker.updated_read_status, worker.updated_write_status, worker.cache_data)
                assert result[0]["signal"] == row_count
                assert update_cache_parameter_data(dataset.cache, "signal", *result)
                if first_result is None:
                    first_result = result
                cached = _worker(dataset, dimensions, read_data=False)
                for view in (worker, cached):
                    if dimensions == 1:
                        np.testing.assert_array_equal(view.axis_data["x"], np.arange(len(expected)))
                        assert view.axis_data["y"].tolist() == expected
                    else:
                        assert 99. not in view.axis_data["x"]
                        assert 99. not in view.axis_data["y"]
                        acquired = [value for value in view.dataGrid.ravel()
                                    if not np.isnan(float(value))]
                        assert acquired == expected
                raw_dataset = load_by_id_read_only(saver.run_id, str(path))
                try:
                    raw = flatten_record_columns(parameter_data_for_export(raw_dataset, "signal"))
                    assert raw["signal"].tolist() == expected
                finally:
                    raw_dataset.conn.close()
                assert protected_state(path) == before
            assert not update_cache_parameter_data(dataset.cache, "signal", *first_result)
            assert dataset.cache._read_status["signal"] == len(batches)
        finally:
            if dataset is not None:
                dataset.conn.close()


def test_cancelled_empty_merge_leaves_cache_unchanged_and_retry_succeeds(tmp_path, monkeypatch):
    with _source(tmp_path, 1, False) as (path, saver, append):
        append([])
        dataset = load_by_id_read_only(saver.run_id, str(path))
        try:
            dataset.cache.prepare()
            write, read, data = snapshot_cache_parameter_state(dataset.cache, "signal")
            before = protected_state(path)
            previous = {name: (id(values), values.copy()) for name, values in data["signal"].items()}
            def cancelled_flatten(columns, check_cancelled):
                def cancel():
                    check_cancelled()
                    raise InterruptedError("empty load cancelled")

                return flatten_record_columns(columns, cancel)

            with monkeypatch.context() as patch:
                patch.setattr(LoadFromDB, "flatten_record_columns", cancelled_flatten)
                with pytest.raises(InterruptedError, match="empty load cancelled"):
                    load_param_data_from_db(dataset.conn, dataset.table_name, dataset.description,
                                            "signal", write, read, data)
            for name, (identity, values) in previous.items():
                assert id(dataset.cache._data["signal"][name]) == identity
                np.testing.assert_array_equal(dataset.cache._data["signal"][name], values)
            assert dataset.cache._read_status == read
            result = load_param_data_from_db(dataset.conn, dataset.table_name, dataset.description,
                                            "signal", write, read, data)
            assert result[0]["signal"] == 1
            assert all(values.size == 0 and values.ndim == 1
                       for values in result[2]["signal"].values())
            assert result[2]["signal"]["signal"].dtype == np.uint64
            assert update_cache_parameter_data(dataset.cache, "signal", *result)
            assert protected_state(path) == before
        finally:
            dataset.conn.close()
