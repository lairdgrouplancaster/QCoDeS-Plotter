"""QCoDeS singleton array storage has the same samples in every viewer route."""

from contextlib import contextmanager
from dataclasses import replace

import numpy as np
import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.datahandling import trusted_array_preview
from qplot.datahandling.file_identity import database_instance
from qplot.datahandling.parameter_data import (
    flatten_record_columns,
    parameter_data_for_export,
)
from qplot.datahandling.readonly import load_by_id_read_only
from qplot.datahandling.trusted_derived_rendering import render_trusted_derived_payload
from qplot.datahandling.trusted_live_queries import (
    TrustedMetadataQueryAdapter,
    TrustedSourceRevisionNamespace,
)
from qplot.datahandling.trusted_live_service import TrustedLiveReadService
from qplot.datahandling.trusted_work_scheduler import TrustedWorkKind
from qplot.tools import worker as workers
from tests.datahandling.test_stage5c_real_qcodes import (
    _load_basic_runs,
    _ReadOnlySqliteExecutor,
)
from tests.datahandling.test_trusted_array_preview import _blob
from tests.datahandling.test_trusted_derived_rendering import _observation
from tests.datahandling.test_trusted_plot import protected_state
from tests.test_bug_hunt_measurement_previews import _image


@contextmanager
def _source(tmp_path, records, *, numeric_slow=False, numeric_signal=False, saver=False, shaped=False):
    names = ("x", "signal") if len(records[0]) == 2 else ("slow", "x", "signal")
    path = tmp_path / "singletons.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("Singleton arrays", "regression")
    measurement = Measurement(exp=experiment)
    for name in names[:-1]:
        measurement.register_custom_parameter(
            name, paramtype="numeric" if name == "slow" and numeric_slow else "array",
        )
    measurement.register_custom_parameter(
        "signal", paramtype="numeric" if numeric_signal else "array", setpoints=names[:-1],
    )
    if shaped:
        measurement.set_shapes({"signal": (3,) if len(names) == 2 else (2, 3)})
    try:
        with measurement.run(write_in_background=False, in_memory_cache=False) as datasaver:
            for record in records:
                if saver:
                    datasaver.add_result(*zip(names, record, strict=True))
                else:
                    datasaver.dataset.add_results([dict(zip(names, record, strict=True))])
            datasaver.flush_data_to_database(block=True)
            # The package's public reader is an independent oracle for its
            # singleton storage convention, before any viewer connection.
            expected = datasaver.dataset.get_parameter_data("signal")["signal"]
            run_id, guid = datasaver.run_id, datasaver.dataset.guid
    finally:
        datasaver.dataset.conn.close()
        experiment.conn.close()
    before = protected_state(path)
    try:
        yield path, run_id, guid, expected
    finally:
        assert protected_state(path) == before


def _capture(path, run_id):
    executor = _ReadOnlySqliteExecutor(path)
    try:
        adapter = TrustedMetadataQueryAdapter(executor, path)
        _load_basic_runs(adapter)
        return adapter.derived_source_observation(
            run_id, database_instance=database_instance(path),
            namespace=TrustedSourceRevisionNamespace.create(),
        )
    finally:
        executor.close()


def _expected_preview(columns, kind):
    observation = _observation(dimensions=len(columns) - 1)
    observation = replace(
        observation,
        parameters=tuple(replace(parameter, paramtype="array")
                         for parameter in observation.parameters),
        sample_rows=((1, *(_blob(column) for column in columns)),),
        validated_2d_layouts=(),
    )
    return render_trusted_derived_payload(observation, kind)


@pytest.mark.parametrize("kind", [TrustedWorkKind.THUMBNAIL, TrustedWorkKind.PREVIEW])
@pytest.mark.parametrize("dimensions,column", [(1, 0), (1, 1), (2, 0), (2, 1), (2, 2)])
@pytest.mark.parametrize("rank", [0, 1, 3])
def test_real_singleton_arrays_match_expanded_raw_samples_and_previews(
    tmp_path, dimensions, column, rank, kind,
):
    columns = ([np.arange(3.), np.array([1., 4., 2.])] if dimensions == 1 else
               [np.array([0., 0., 1.]), np.arange(3.), np.array([1., 4., 2.])])
    expected_columns = [values.copy() for values in columns]
    expected_columns[column][:] = 2.
    columns[column] = np.array(2.).reshape((1,) * rank)
    with _source(tmp_path, [columns]) as (path, run_id, _guid, qcodes):
        dataset = load_by_id_read_only(run_id, str(path))
        try:
            raw = flatten_record_columns(parameter_data_for_export(dataset, "signal"))
            names = ("x", "signal") if dimensions == 1 else ("slow", "x", "signal")
            for name, expected in zip(names, expected_columns, strict=True):
                np.testing.assert_array_equal(raw[name], expected)
                np.testing.assert_array_equal(qcodes[name].ravel(), expected)
        finally:
            dataset.conn.close()
        actual = render_trusted_derived_payload(_capture(path, run_id), kind)
        assert _image(actual) == _image(_expected_preview(expected_columns, kind))
        assert _image(actual)[1] == 3


def test_datasaver_scalar_setpoint_registered_as_array_gets_preview(tmp_path):
    with _source(tmp_path, [[2., np.array([0., 1., 2.])]], saver=True) as (
        path, run_id, _guid, expected,
    ):
        np.testing.assert_array_equal(expected["x"], [[2., 2., 2.]])
        payload = render_trusted_derived_payload(_capture(path, run_id), TrustedWorkKind.THUMBNAIL)
        assert _image(payload) == _image(_expected_preview(
            [[2., 2., 2.], [0., 1., 2.]], TrustedWorkKind.THUMBNAIL,
        ))


def _run(dataset, **kwargs):
    params = {parameter.name: parameter for parameter in dataset.get_parameters()}
    worker = workers.loader(dataset.cache, params["signal"], params,
                            {"x": "x", "y": "slow"}, **kwargs)
    worker.setAutoDelete(False)
    errors, finished = [], []
    worker.emitter.errorOccurred.connect(errors.append)
    worker.emitter.finished.connect(finished.append)
    worker.run()
    assert errors == [], errors
    assert finished == [True]
    assert worker._sql_connection is None
    return worker


@pytest.mark.parametrize("route", ["cache", "buffered", "streamed"])
@pytest.mark.parametrize("rank", [0, 1, 3])
@pytest.mark.parametrize("shaped", [False, True])
def test_singleton_output_heatmap_all_read_routes(tmp_path, monkeypatch, route, rank, shaped):
    records = [[np.array(slow).reshape((1,) * rank), np.arange(3.).reshape(1, 3),
                np.array(2. + slow).reshape((1,) * rank)] for slow in (0., 1.)]
    if route == "streamed":
        monkeypatch.setattr(workers, "MAX_SQL_HEATMAP_SOURCE_ROWS", 1)
    with _source(tmp_path, records, shaped=shaped) as (path, run_id, _guid, expected):
        dataset = load_by_id_read_only(run_id, str(path))
        try:
            worker = _run(dataset, force_sql_heatmap=route != "cache")
            np.testing.assert_array_equal(worker.axis_data["x"], [0., 1., 2.])
            np.testing.assert_array_equal(worker.axis_data["y"], [0., 1.])
            np.testing.assert_array_equal(worker.dataGrid, [[2., 2., 2.], [3., 3., 3.]])
            np.testing.assert_array_equal(expected["signal"].reshape(2, 3), worker.dataGrid)
            assert worker.loaded_from_sql_heatmap == (route != "cache")
            assert worker.aggregated_heatmap_source == (route == "streamed")
        finally:
            dataset.conn.close()


@pytest.mark.parametrize("numeric_slow", [False, True])
def test_empty_records_do_not_add_broadcast_samples(tmp_path, numeric_slow):
    empty = [0. if numeric_slow else np.array([]), np.array([]), np.array([])]
    full = [1. if numeric_slow else np.ones(3), np.arange(3.), np.array([4., 5., 6.])]
    with _source(tmp_path, [empty, full], numeric_slow=numeric_slow, saver=True) as (
        path, run_id, _guid, _expected,
    ):
        payload = render_trusted_derived_payload(_capture(path, run_id), TrustedWorkKind.THUMBNAIL)
        assert _image(payload) == _image(_expected_preview(
            [np.ones(3), np.arange(3.), [4., 5., 6.]], TrustedWorkKind.THUMBNAIL,
        ))
        dataset = load_by_id_read_only(run_id, str(path))
        try:
            worker = _run(dataset, force_sql_heatmap=True)
            np.testing.assert_array_equal(worker.axis_data["y"], [1.])
            np.testing.assert_array_equal(worker.dataGrid, [[4., 5., 6.]])
            assert worker.loaded_point_count == 3
        finally:
            dataset.conn.close()


def test_singleton_preview_budget_and_cancellation_are_checked_before_expansion(tmp_path, monkeypatch):
    columns = [np.array([0.]), np.arange(6.), np.array([2.])]
    with _source(tmp_path, [columns, columns]) as (path, run_id, _guid, _expected):
        observation = _capture(path, run_id)
        monkeypatch.setattr(trusted_array_preview, "MAX_SAMPLES", 11)
        rejected = render_trusted_derived_payload(observation, TrustedWorkKind.THUMBNAIL)
        assert rejected["status"] == "unsupported"
        assert "sample budget" in rejected["description"]
        monkeypatch.setattr(trusted_array_preview, "MAX_SAMPLES", 12)
        calls = 0

        def cancel():
            nonlocal calls
            calls += 1
            if calls == 3:
                raise InterruptedError("singleton preview cancelled")

        with pytest.raises(InterruptedError, match="singleton preview cancelled"):
            render_trusted_derived_payload(observation, TrustedWorkKind.THUMBNAIL, cancel_check=cancel)
        assert calls == 3
        assert _image(render_trusted_derived_payload(observation, TrustedWorkKind.THUMBNAIL))[1] == 12


def test_trusted_singleton_output_heatmap_and_bounded_retry(tmp_path):
    records = [[np.array([0.]), np.arange(6.), np.array([2.])]]
    with _source(tmp_path, records) as (path, _run_id, guid, _expected):
        service = TrustedLiveReadService(path)
        try:
            dataset = service.submit_plot_dataset(guid).wait(15)
            worker = _run(dataset, max_full_heatmap_points=3, max_heatmap_grid_cells=3)
            assert worker.dataGrid.shape == (1, 3)
            np.testing.assert_array_equal(worker.dataGrid, [[2., 2., 2.]])
            assert worker.heatmap_downsample_info is not None
            worker = _run(dataset)
            np.testing.assert_array_equal(worker.dataGrid, [[2.] * 6])
        finally:
            service.close(timeout=15)
        assert not service.liveness().resource_cleanup_pending


@pytest.mark.parametrize("numeric_signal", [False, True])
@pytest.mark.parametrize("swapped", [False, True])
def test_empty_output_with_singleton_array_setpoint_keeps_later_record(
    tmp_path, numeric_signal, swapped,
):
    if numeric_signal:
        records = [[np.array([]), np.array([2.]), 7.], [np.array([1.]), np.arange(3.), 4.]]
    else:
        records = [[0., 2., np.array([])], [1., np.arange(3.), np.array([4., 5., 6.])]]
    with _source(tmp_path, records, numeric_signal=numeric_signal, saver=not numeric_signal) as (
        path, run_id, _guid, _expected,
    ):
        payload = render_trusted_derived_payload(_capture(path, run_id), TrustedWorkKind.THUMBNAIL)
        expected = [np.ones(3), np.arange(3.), [4., 4., 4.] if numeric_signal else [4., 5., 6.]]
        assert _image(payload) == _image(_expected_preview(expected, TrustedWorkKind.THUMBNAIL))
        dataset = load_by_id_read_only(run_id, str(path))
        try:
            params = {parameter.name: parameter for parameter in dataset.get_parameters()}
            worker = workers.loader(dataset.cache, params["signal"], params,
                                    {"x": "slow" if swapped else "x", "y": "x" if swapped else "slow"},
                                    force_sql_heatmap=True)
            errors, finished = [], []
            worker.emitter.errorOccurred.connect(errors.append)
            worker.emitter.finished.connect(finished.append)
            worker.run()
            assert errors == [] and finished == [True]
            grid = np.asarray(expected[-1]).reshape(1, 3)
            np.testing.assert_array_equal(worker.dataGrid, grid.T if swapped else grid)
            assert worker.loaded_point_count == 3
        finally:
            dataset.conn.close()


def test_singleton_heatmap_chunks_cancel_and_retry_without_materializing_broadcasts(tmp_path, monkeypatch):
    records = [[np.array([0.]), np.arange(64.), np.array([2.])]]
    monkeypatch.setattr(workers, "CANCELLATION_CHUNK_SIZE", 4)
    with _source(tmp_path, records) as (path, run_id, _guid, _expected):
        dataset = load_by_id_read_only(run_id, str(path))
        try:
            params = {parameter.name: parameter for parameter in dataset.get_parameters()}
            worker = workers.loader(dataset.cache, params["signal"], params,
                                    {"x": "x", "y": "slow"}, force_sql_heatmap=True)
            reads = []
            original = workers._HeatmapArrayReader.read

            def read(reader, start, stop, check):
                reads.append((reader.size, start, stop))
                assert stop - start <= 4
                if reader.size == 1:
                    assert (start, stop) == (0, 1)
                if len(reads) == 4:
                    worker.cancel()
                return original(reader, start, stop, check)

            errors, finished = [], []
            worker.emitter.errorOccurred.connect(errors.append)
            worker.emitter.finished.connect(finished.append)
            with monkeypatch.context() as patch:
                patch.setattr(workers._HeatmapArrayReader, "read", read)
                worker.run()
            assert errors == [] and finished == [False]
            assert len(reads) == 4 and worker._sql_connection is None
            retried = _run(dataset, force_sql_heatmap=True)
            np.testing.assert_array_equal(retried.dataGrid, [[2.] * 64])
        finally:
            dataset.conn.close()


def test_old_unsupported_singleton_cache_cannot_satisfy_current_renderer(tmp_path):
    from qplot.datahandling.trusted_derived_cache import TrustedDerivedDiskCache
    from qplot.datahandling.trusted_derived_rendering import (
        TRUSTED_DERIVED_RENDERER_VERSION,
    )
    from tests.datahandling.test_trusted_derived_cache import _key, _payload

    old_key = replace(_key(), renderer_version="trusted-derived-renderer-v8")
    current_key = replace(old_key, renderer_version=TRUSTED_DERIVED_RENDERER_VERSION)
    old_payload = {**_payload(), "status": "unsupported",
                   "description": "Array measurement and setpoint sizes differ."}
    cache = TrustedDerivedDiskCache(tmp_path / "cache")
    assert cache.put(old_key, old_payload)
    assert cache.get(old_key) == old_payload
    assert cache.get(current_key) is None


@pytest.mark.parametrize("shaped", [False, True])
@pytest.mark.parametrize("dtype", [float, np.uint64])
def test_empty_singleton_records_keep_planned_samples_and_raw_csv(tmp_path, shaped, dtype):
    from qplot.windows._plot_actions import PlotActionsMixin

    values = np.array([4, 5, 6], dtype=dtype)
    if dtype is np.uint64:
        values += np.uint64(2**63)
    records = [[2., np.array([], dtype=dtype)], [3., values]]
    with _source(tmp_path, records, saver=True, shaped=shaped) as (path, run_id, _guid, _expected):
        dataset = load_by_id_read_only(run_id, str(path))
        try:
            params = {parameter.name: parameter for parameter in dataset.get_parameters()}
            worker = workers.loader(dataset.cache, params["signal"], params, {"x": "x", "y": "signal"})
            errors, finished = [], []
            worker.emitter.errorOccurred.connect(errors.append)
            worker.emitter.finished.connect(finished.append)
            worker.run()
            assert errors == [] and finished == [True]
            np.testing.assert_array_equal(worker.axis_data["x"], [3., 3., 3.])
            np.testing.assert_array_equal(worker.axis_data["y"], values)
            raw = parameter_data_for_export(dataset, "signal")
            flat = flatten_record_columns(raw)
            np.testing.assert_array_equal(flat["signal"], values)
            np.testing.assert_array_equal(flat["x"], [3., 3., 3.])
            frame = PlotActionsMixin._measurement_dataframe(None, dataset, [params["signal"]])
            np.testing.assert_array_equal(frame["signal"], values)
            np.testing.assert_array_equal(frame["x"], [3., 3., 3.])
            import csv
            import io

            rows = list(csv.DictReader(io.StringIO(frame.to_csv(index=False))))
            convert = int if dtype is np.uint64 else float
            assert [convert(row["signal"]) for row in rows] == values.tolist()
            assert [float(row["x"]) for row in rows] == [3., 3., 3.]
        finally:
            dataset.conn.close()


def test_initial_empty_singleton_cache_read_does_not_advance_acquisition(tmp_path):
    from qplot.datahandling.LoadFromDB import load_param_data_from_db
    from qplot.datahandling.qcodes_cache import (
        snapshot_cache_parameter_state,
        update_cache_parameter_data,
    )

    records = [[2., np.array([])], [3., np.array([4., 5., 6.])]]
    with _source(tmp_path, records, saver=True, shaped=True) as (path, run_id, _guid, _expected):
        dataset = load_by_id_read_only(run_id, str(path))
        try:
            dataset.cache.prepare()
            write, read, data = snapshot_cache_parameter_state(dataset.cache, "signal")
            first = load_param_data_from_db(
                dataset.conn, dataset.table_name, dataset.description,
                "signal", write, read, data, end=1,
            )
            assert first[0]["signal"] == 1
            assert first[1]["signal"] == 0
            assert all(values.size == 0 for values in first[2]["signal"].values())
            assert update_cache_parameter_data(dataset.cache, "signal", *first)
            write, read, data = snapshot_cache_parameter_state(dataset.cache, "signal")
            previous = {name: (id(values), values.copy()) for name, values in data["signal"].items()}
            checks = 0

            def cancel():
                nonlocal checks
                checks += 1
                if checks == 10:
                    raise InterruptedError("cancelled after empty record")

            with pytest.raises(InterruptedError, match="after empty record"):
                load_param_data_from_db(
                    dataset.conn, dataset.table_name, dataset.description,
                    "signal", write, read, data, check_cancelled=cancel,
                )
            assert dataset.cache._read_status["signal"] == 1
            assert dataset.cache._write_status["signal"] == 0
            for name, (identity, values) in previous.items():
                assert id(dataset.cache._data["signal"][name]) == identity
                np.testing.assert_array_equal(dataset.cache._data["signal"][name], values)
            result = load_param_data_from_db(
                dataset.conn, dataset.table_name, dataset.description, "signal", write, read, data,
            )
            assert result[0]["signal"] == 2
            assert result[1]["signal"] == 3
            assert update_cache_parameter_data(dataset.cache, "signal", *result)
            # Publishing the older empty prefix must not erase the append.
            assert not update_cache_parameter_data(dataset.cache, "signal", *first)
            np.testing.assert_array_equal(dataset.cache._data["signal"]["signal"], [4., 5., 6.])
            np.testing.assert_array_equal(dataset.cache._data["signal"]["x"], [3., 3., 3.])
        finally:
            dataset.conn.close()


@pytest.mark.parametrize("output_size,coordinate_size", [(1, 0), (0, 2), (2, 0)])
def test_empty_alignment_refuses_incompatible_nonempty_samples(output_size, coordinate_size):
    from qplot.windows._plot_actions import _csv_scalar_columns

    data = {"signal": np.ones((1, output_size)), "x": np.ones((1, coordinate_size))}
    with pytest.raises(ValueError, match="shapes do not match"):
        flatten_record_columns(data)
    with pytest.raises(ValueError, match="Cannot align CSV"):
        _csv_scalar_columns({"x": data["x"], "signal": data["signal"]}, "signal")


def test_empty_alignment_preserves_dtype_and_cancellation():
    from qplot.windows._plot_actions import _csv_scalar_columns

    signal = np.empty(2, dtype=object)
    signal[0], signal[1] = np.array([], dtype=np.longdouble), np.array([2**63 + 1], dtype=np.uint64)
    data = {"signal": signal, "x": np.array([[2.], [3.]])}
    checks = 0

    def cancel():
        nonlocal checks
        checks += 1
        if checks == 4:
            raise InterruptedError("empty alignment cancelled")

    with pytest.raises(InterruptedError, match="empty alignment cancelled"):
        flatten_record_columns(data, cancel)
    for actual in (flatten_record_columns(data), _csv_scalar_columns(data, "signal")):
        assert actual["signal"].dtype == object
        assert actual["signal"].tolist() == [2**63 + 1]
        np.testing.assert_array_equal(actual["x"], [3.])
    assert signal[0].dtype == np.longdouble and signal[0].size == 0
    assert signal[1].dtype == np.uint64 and signal[1].item() == 2**63 + 1
