"""Check parameter-tree storage before QCoDeS materializes scalar broadcasts."""

from copy import deepcopy

import numpy as np
import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.datahandling import parameter_data
from qplot.datahandling.LoadFromDB import load_param_data_from_db
from qplot.datahandling.parameter_data import flatten_record_columns
from qplot.datahandling.qcodes_cache import (
    snapshot_cache_parameter_state,
    update_cache_parameter_data,
)
from qplot.datahandling.readonly import load_by_id_read_only
from qplot.datahandling.trusted_live_service import TrustedLiveReadService
from qplot.tools import worker as workers
from tests.test_campaign_inferred_array_missing_cells import (
    _assert_columns,
    _source,
    _stored_columns,
)
from tests.windows.test_complex_line_data import database_state


def test_trusted_text_expansion_is_budgeted_before_allocation_and_can_retry(tmp_path, monkeypatch):
    with _source(tmp_path, widths=(8192,), other_type="text", direct=True,
                 text_value="t" * 1024) as (path, guid, source):
        expected, _count = _stored_columns(source)
        service = TrustedLiveReadService(path)
        try:
            service.submit_bootstrap().wait(15)
            dataset = service.submit_plot_dataset(guid).wait(15)
            prefix = service.submit_plot_prefix(dataset).wait(20)
            assert prefix.decode_bytes < 8 * 1024**2
            if not dataset.cache._data:
                dataset.cache.prepare()
            previous_status = dict(dataset.cache._read_status), dict(dataset.cache._write_status)
            previous_arrays = {tree: {name: id(value) for name, value in columns.items()}
                               for tree, columns in dataset.cache._data.items()}
            expansions = []
            original = parameter_data._expand_data_to_arrays

            def expand(records, specs):
                expansions.append(True)
                return original(records, specs)

            monkeypatch.setattr(parameter_data, "_expand_data_to_arrays", expand)
            monkeypatch.setattr(workers, "MAX_TRUSTED_FULL_PLOT_DECODE_BYTES", 8 * 1024**2)

            def run():
                worker = workers.loader(dataset.cache, dataset.paramspecs["converted"],
                                        dataset.paramspecs, {"x": "x", "y": "converted"})
                worker.setAutoDelete(False)
                errors, finished = [], []
                worker.emitter.errorOccurred.connect(errors.append)
                worker.emitter.finished.connect(finished.append)
                worker.run()
                return worker, errors, finished

            _worker, errors, finished = run()
            assert finished == [False]
            assert len(errors) == 1 and "decoding budget" in str(errors[0])
            assert expansions == [], "The budget must precede scalar text expansion"
            assert (dataset.cache._read_status, dataset.cache._write_status) == previous_status
            assert {tree: {name: id(value) for name, value in columns.items()}
                    for tree, columns in dataset.cache._data.items()} == previous_arrays

            # The same small source succeeds with enough room for every field.
            monkeypatch.setattr(workers, "MAX_TRUSTED_FULL_PLOT_DECODE_BYTES", 96 * 1024**2)
            worker, errors, finished = run()
            assert errors == [] and finished == [True]
            assert expansions
            _assert_columns(flatten_record_columns(worker.cache_data["converted"]), expected)
        finally:
            service.close(timeout=15)
        assert not service.liveness().resource_cleanup_pending


@pytest.mark.parametrize("shaped", [False, True])
def test_incremental_text_budget_precedes_expansion_and_keeps_cached_values(tmp_path, monkeypatch, shaped):
    with _source(tmp_path, widths=(1, 32), shaped=shaped, other_type="text", direct=True,
                 text_value="Z" * 256) as (_path, _guid, dataset):
        cache = dataset.cache
        write, read, previous = snapshot_cache_parameter_state(cache, "converted")
        result = load_param_data_from_db(
            dataset.conn, dataset.table_name, dataset.description, "converted",
            write, read, previous, end=2, max_decode_bytes=12 * 1024,
        )
        assert update_cache_parameter_data(cache, "converted", *result)
        write, read, previous = snapshot_cache_parameter_state(cache, "converted")
        original_arrays = {name: (id(value), deepcopy(value)) for name, value in
                           cache._data["converted"].items()}

        def unexpected_expansion(*args, **kwargs):
            pytest.fail("The cumulative budget must precede scalar expansion")

        with monkeypatch.context() as patch:
            patch.setattr(parameter_data, "_expand_data_to_arrays", unexpected_expansion)
            with pytest.raises(ValueError, match="decoding budget"):
                load_param_data_from_db(
                    dataset.conn, dataset.table_name, dataset.description, "converted",
                    write, read, previous, max_decode_bytes=12 * 1024,
                )
        assert cache._read_status["converted"] == read["converted"]
        assert cache._write_status["converted"] == write["converted"]
        for name, (identity, value) in original_arrays.items():
            assert id(cache._data["converted"][name]) == identity
            np.testing.assert_array_equal(cache._data["converted"][name], value)
        result = load_param_data_from_db(
            dataset.conn, dataset.table_name, dataset.description, "converted",
            write, read, previous, max_decode_bytes=128 * 1024,
        )
        expected, _count = _stored_columns(dataset)
        _assert_columns(flatten_record_columns(result[2]["converted"]), expected)


@pytest.mark.parametrize("rank", [0, 1, 2, 3])
@pytest.mark.parametrize("singleton_output", [False, True])
def test_singleton_array_ranks_keep_qcodes_broadcast_semantics(tmp_path, monkeypatch, rank, singleton_output):
    path = tmp_path / "singleton.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("Singleton storage", "regression")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("x", paramtype="array")
    measurement.register_custom_parameter("signal", paramtype="array", setpoints=("x",))
    singleton = np.array(2**63 + 1, dtype=np.uint64).reshape((1,) * rank)
    values = np.array([1, 2, 3], dtype=np.uint64)
    columns = {"x": values if singleton_output else singleton,
               "signal": singleton if singleton_output else values}
    try:
        with measurement.run(write_in_background=False, in_memory_cache=False) as saver:
            saver.dataset.add_results([columns])
            run_id = saver.run_id
    finally:
        saver.dataset.conn.close()
        experiment.conn.close()
    protected = database_state(path)
    dataset = load_by_id_read_only(run_id, str(path))
    try:
        def unexpected_expansion(*args, **kwargs):
            pytest.fail("Singleton expansion must also be counted before allocation")

        with monkeypatch.context() as patch:
            patch.setattr(parameter_data, "_expand_data_to_arrays", unexpected_expansion)
            with pytest.raises(ValueError, match="decoding budget"):
                parameter_data.get_parameter_data_for_one_paramtree(
                    dataset.conn, dataset.table_name, dataset.description, "signal",
                    max_decode_bytes=47,
                )
        decoded, count = parameter_data.get_parameter_data_for_one_paramtree(
            dataset.conn, dataset.table_name, dataset.description, "signal",
            max_decode_bytes=48,
        )
        assert count == 1
        for name, recorded in columns.items():
            expected = np.repeat(recorded.item(), 3) if recorded.size == 1 else recorded
            assert decoded[name].dtype == np.uint64
            np.testing.assert_array_equal(decoded[name], expected.reshape(1, 3))
    finally:
        dataset.conn.close()
    assert database_state(path) == protected


def test_cancellation_during_projection_precedes_expansion_and_allows_retry(tmp_path, monkeypatch):
    with _source(tmp_path, widths=(0, 3), other_type="text", direct=True,
                 text_value="Z" * 256) as (_path, _guid, dataset):
        original_projection = parameter_data._project_record_expansion
        projection_active = False
        checks = 0

        def project(*args, **kwargs):
            nonlocal projection_active
            projection_active = True
            return original_projection(*args, **kwargs)

        def cancel():
            nonlocal checks
            if projection_active:
                checks += 1
                if checks == 3:
                    raise InterruptedError("cancelled before array expansion")

        def unexpected_expansion(*args, **kwargs):
            pytest.fail("Cancelled projection must not expand any records")

        with monkeypatch.context() as patch:
            patch.setattr(parameter_data, "_project_record_expansion", project)
            patch.setattr(parameter_data, "_expand_data_to_arrays", unexpected_expansion)
            with pytest.raises(InterruptedError, match="before array expansion"):
                parameter_data.get_parameter_data_for_one_paramtree(
                    dataset.conn, dataset.table_name, dataset.description, "converted",
                    max_decode_bytes=8 * 1024**2, check_cancelled=cancel,
                )
        assert checks == 3
        expected, expected_count = _stored_columns(dataset)
        decoded, count = parameter_data.get_parameter_data_for_one_paramtree(
            dataset.conn, dataset.table_name, dataset.description, "converted",
            max_decode_bytes=8 * 1024**2,
        )
        assert count == expected_count
        _assert_columns(flatten_record_columns(decoded), expected)


@pytest.mark.parametrize("other_type", ["array", "numeric", "complex", "text"])
@pytest.mark.parametrize("output_type", ["numeric", "array"])
def test_projection_preserves_empty_missing_and_mixed_storage(tmp_path, other_type, output_type):
    with _source(tmp_path, widths=(0, 1, 3), other_type=other_type, output_type=output_type,
                 dtype=np.longdouble, direct=True, text_value="α🧪") as (_path, _guid, dataset):
        expected, expected_count = _stored_columns(dataset)
        decoded, count = parameter_data.get_parameter_data_for_one_paramtree(
            dataset.conn, dataset.table_name, dataset.description, "converted",
            max_decode_bytes=8 * 1024**2,
        )
        assert count == expected_count
        _assert_columns(flatten_record_columns(decoded), expected)
