"""Recorded inferred arrays retain missing ancillary fields and every sample."""

import csv
from contextlib import contextmanager
from copy import deepcopy
from fractions import Fraction

import numpy as np
import pytest
from PyQt6 import QtWidgets
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.datahandling.LoadFromDB import load_param_data_from_db
from qplot.datahandling.parameter_data import (
    flatten_record_columns,
    get_parameter_data_for_one_paramtree,
    parameter_data_for_export,
)
from qplot.datahandling.qcodes_cache import (
    snapshot_cache_parameter_state,
    update_cache_parameter_data,
)
from qplot.datahandling.readonly import load_by_id_read_only
from qplot.datahandling.trusted_live_service import TrustedLiveReadService
from qplot.tools import worker as workers
from qplot.windows import MainWindow
from qplot.windows._plotWin import plotWidget
from tests._window_lifecycle import close_main_window
from tests.windows.test_complex_line_data import database_state
from tests.windows.test_native_transform_labels import (
    no_callback_errors as _no_callback_errors,
)
from tests.windows.test_plot_integration import configure_temp_qplot, wait_for

no_callback_errors = _no_callback_errors


@contextmanager
def _source(tmp_path, *, widths=(2,), heatmap=False, shaped=False,
            dtype=float, other_type="array", direct=False, output_type="array",
            nonfinite=False, missing_output=False, empty_dtype=None,
            text_value="literal None"):
    path = tmp_path / "inferred-arrays.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("Missing inferred basis cells", "regression")
    measurement = Measurement(exp=experiment)
    # Numeric output exercises an absent first declared array in its tree.
    measurement.register_custom_parameter("x", paramtype="numeric" if output_type == "numeric" else "array")
    if heatmap:
        measurement.register_custom_parameter("slow", paramtype="array")
    axes = ("slow", "x") if heatmap else ("x",)
    measurement.register_custom_parameter("signal", paramtype="array", setpoints=axes)
    measurement.register_custom_parameter("other", paramtype=other_type, setpoints=axes)
    measurement.register_custom_parameter("converted", paramtype=output_type,
                                          setpoints=axes, basis=("signal", "other"))
    if shaped:
        measurement.set_shapes({
            name: (2, 3) if heatmap else (6,)
            for name in ("signal", "other", "converted")
        })
    try:
        with measurement.run(write_in_background=False, in_memory_cache=False) as saver:
            for row, width in enumerate(widths):
                shape = () if width is None else (width,)
                size = 1 if width is None else width
                x = np.arange(size, dtype=float).reshape(shape)
                values = np.arange(size, dtype=dtype).reshape(shape)
                if not size and empty_dtype is not None:
                    x, values = x.astype(empty_dtype), values.astype(empty_dtype)
                if np.dtype(dtype).kind in "iu":
                    values += np.asarray(2**63 + 1 if np.dtype(dtype).kind == "u" else 2**60 + 1, dtype=dtype)
                elif dtype is np.longdouble:
                    values = np.longdouble(1) + values * np.finfo(np.longdouble).eps
                else:
                    values += 100 + 10 * row
                if nonfinite:
                    values.flat[:3] = [np.nan, np.inf, -np.inf][:min(size, 3)]
                columns = {"x": x, "signal": values.copy(), "converted": values.copy()}
                if heatmap:
                    columns["slow"] = np.full(shape, row, dtype=float)
                if output_type == "numeric":
                    columns["x"], columns["converted"] = float(row), float(100 + row)
                columns["other"] = {
                    "array": values.copy(), "numeric": 7.5,
                    "text": text_value, "complex": 2 + 3j,
                }[other_type]
                if direct:
                    # Public DataSet writes allow valid mixed storage without
                    # DataSaver flattening scalar-root trees into array fields.
                    saver.dataset.add_results([
                        {name: value for name, value in columns.items() if name != absent}
                        for absent in ("signal", "other")
                    ])
                else:
                    saver.add_result(*columns.items())
                if missing_output:
                    saver.dataset.add_results([
                        {name: value for name, value in columns.items() if name != "converted"},
                    ])
            saver.flush_data_to_database(block=True)
            run_id, guid = saver.run_id, saver.dataset.guid
        saver.dataset.conn.close()
        experiment.conn.close()
        protected = database_state(path)
        dataset = load_by_id_read_only(run_id, str(path))
        try:
            yield path, guid, dataset
        finally:
            dataset.conn.close()
            assert database_state(path) == protected
    finally:
        saver.dataset.conn.close()
        experiment.conn.close()


def _stored_columns(dataset, end=None):
    names = [parameter.name for parameter in dataset.get_parameters()]
    quoted = ",".join('"' + name + '"' for name in names)
    rows = dataset.conn.execute(
        f'SELECT {quoted} FROM "{dataset.table_name}" WHERE converted IS NOT NULL ORDER BY id'
        + (f" LIMIT {int(end)}" if end is not None else ""),
    ).fetchall()
    expected = {name: [] for name in names}
    for row in rows:
        present_arrays = [value for value in row if isinstance(value, np.ndarray)]
        size = max((value.size for value in present_arrays), default=1)
        for name, value in zip(names, row, strict=True):
            if value is None:
                expected[name].extend([None] * size)
            elif isinstance(value, np.ndarray):
                expected[name].extend(value.ravel() if value.size != 1 else [value.item()] * size)
            else:
                expected[name].extend([value] * size)
    return expected, len(rows)


def _assert_columns(actual, expected):
    assert actual.keys() == expected.keys()
    for name, recorded in expected.items():
        values = actual[name]
        assert values.size == len(recorded), name
        for value, original in zip(values, recorded, strict=True):
            if original is None:
                assert value is None or np.isnan(value), (name, value)
            elif isinstance(original, str):
                assert value == original
            elif isinstance(original, (complex, np.complexfloating)):
                assert value == original or (np.isnan(value) and np.isnan(original))
            elif np.isnan(original):
                assert np.isnan(value)
            elif not np.isfinite(original):
                assert value == original
            elif isinstance(original, (int, np.integer)):
                assert Fraction(value) == Fraction(int(original))
            else:
                assert Fraction(*value.as_integer_ratio()) == Fraction(*original.as_integer_ratio())


@pytest.mark.parametrize("dtype", [float, np.int64, np.uint64, np.longdouble])
@pytest.mark.parametrize("widths", [(None,), (0, 2, 0, 3), (0, 0)])
def test_public_datasaver_missing_basis_arrays_preserve_every_record(tmp_path, dtype, widths):
    with _source(tmp_path, widths=widths, dtype=dtype) as (_path, _guid, dataset):
        expected, count = _stored_columns(dataset)
        decoded, actual_count = get_parameter_data_for_one_paramtree(
            dataset.conn, dataset.table_name, dataset.description, "converted",
        )
        assert actual_count == count
        _assert_columns(flatten_record_columns(decoded), expected)


@pytest.mark.parametrize("other_type", ["numeric", "text", "complex"])
@pytest.mark.parametrize("output_type", ["numeric", "array"])
def test_mixed_storage_basis_fields_keep_missingness(tmp_path, other_type, output_type):
    with _source(tmp_path, widths=(0, 1, 3), other_type=other_type,
                 output_type=output_type, direct=True) as (_path, _guid, dataset):
        expected, _count = _stored_columns(dataset)
        actual = flatten_record_columns(parameter_data_for_export(dataset, "converted"))
        _assert_columns(actual, expected)


def test_recorded_nonfinite_samples_and_null_output_rows_are_not_fabricated(tmp_path):
    with _source(tmp_path, widths=(0, 4), nonfinite=True, missing_output=True) as (_path, _guid, dataset):
        expected, count = _stored_columns(dataset)
        actual, actual_count = get_parameter_data_for_one_paramtree(
            dataset.conn, dataset.table_name, dataset.description, "converted",
        )
        assert actual_count == count == 4
        _assert_columns(flatten_record_columns(actual), expected)


@pytest.mark.parametrize("shaped", [False, True])
def test_incremental_missing_basis_columns_keep_exact_cache_samples(tmp_path, shaped):
    with _source(tmp_path, widths=(0, 2, 3), dtype=np.uint64, shaped=shaped) as (_path, _guid, dataset):
        cache = dataset.cache
        cache.prepare()
        for end in (1, 3, 6):
            write, read, previous = snapshot_cache_parameter_state(cache, "converted")
            result = load_param_data_from_db(
                dataset.conn, dataset.table_name, dataset.description,
                "converted", write, read, previous, end=end,
            )
            assert update_cache_parameter_data(cache, "converted", *result)
            expected, _count = _stored_columns(dataset, end)
            acquired = len(expected["converted"])
            actual = {name: values[:acquired] for name, values in
                      flatten_record_columns(cache._data["converted"]).items()}
            _assert_columns(actual, expected)


@contextmanager
def _converted_window(tmp_path, monkeypatch, path, guid, parameter):
    configure_temp_qplot(monkeypatch, tmp_path)
    errors = []
    monkeypatch.setattr(MainWindow, "show_error", lambda self, *args: errors.append(args))
    monkeypatch.setattr(plotWidget, "show_error", lambda self, *args: errors.append(args))
    window = MainWindow()
    try:
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.spinBox.setValue(0)
        window.close_database(status=False)
        assert window.load_file(str(path))
        wait_for(lambda: not window._database_load_active)
        window.openPlot(guid=guid, params=[parameter])
        wait_for(lambda: len(window.windows) == 1)
        plot = window.windows[0]
        wait_for(lambda: not plot.worker.running)
        plot.monitor.stop()
        yield window, plot, errors
    finally:
        close_main_window(window)


@pytest.mark.parametrize("heatmap", [False, True], ids=["line", "heatmap"])
@pytest.mark.parametrize("shaped", [False, True])
@pytest.mark.parametrize("widths", [(3, 3), (2, 3)])
def test_inferred_arrays_plot_and_raw_csv_through_actual_controls(
    tmp_path, monkeypatch, no_callback_errors, heatmap, shaped, widths,
):
    with _source(tmp_path, widths=widths, heatmap=heatmap, shaped=shaped) as (path, guid, dataset):
        expected, _count = _stored_columns(dataset)
        parameter = next(param for param in dataset.get_parameters() if param.name == "converted")
        with _converted_window(tmp_path, monkeypatch, path, guid, parameter) as (window, plot, errors):
            assert errors == []
            if heatmap:
                grid = np.full((2, max(widths)), np.nan)
                for row, width in enumerate(widths):
                    grid[row, :width] = 100 + 10 * row + np.arange(width)
                np.testing.assert_array_equal(plot.dataGrid, grid)
            else:
                np.testing.assert_array_equal(plot.line.getOriginalDataset()[1], expected["converted"])
            target = tmp_path / "raw.csv"
            monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName", lambda *args, **kwargs: (str(target), ""))
            window.export_run_preview_csv(guid, "converted")
            assert errors == [] and target.exists()
            with target.open(newline="") as source:
                rows = list(csv.DictReader(source))
            for name, values in expected.items():
                assert [None if row[name] == "" else float(row[name]) for row in rows] == values


@pytest.mark.parametrize("other_type", ["numeric", "text", "complex"])
def test_mixed_basis_raw_csv_keeps_blank_fields_and_recorded_text(
    tmp_path, monkeypatch, no_callback_errors, other_type,
):
    with _source(tmp_path, widths=(0, 2, 3), other_type=other_type, direct=True) as (path, guid, dataset):
        expected, _count = _stored_columns(dataset)
        parameter = next(param for param in dataset.get_parameters() if param.name == "converted")
        with _converted_window(tmp_path, monkeypatch, path, guid, parameter) as (window, plot, errors):
            assert errors == []
            np.testing.assert_array_equal(plot.line.getOriginalDataset()[1], expected["converted"])
            target = tmp_path / "mixed.csv"
            monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName", lambda *args, **kwargs: (str(target), ""))
            window.export_run_preview_csv(guid, "converted")
            assert errors == [] and target.exists()
            with target.open(newline="") as source:
                rows = list(csv.DictReader(source))
            for name, values in expected.items():
                for row, value in zip(rows, values, strict=True):
                    if value is None:
                        assert row[name] == ""
                    elif isinstance(value, str):
                        assert row[name] == value
                    elif isinstance(value, complex):
                        assert complex(row[name]) == value
                    else:
                        assert float(row[name]) == value


def test_cancelled_null_expansion_leaves_source_and_later_read_usable(tmp_path):
    with _source(tmp_path, widths=(2, 4)) as (_path, _guid, dataset):
        expected, _count = _stored_columns(dataset)
        calls = 0

        def cancel():
            nonlocal calls
            calls += 1
            if calls == 5:
                raise InterruptedError("cancelled missing cell expansion")

        with pytest.raises(InterruptedError, match="cancelled missing"):
            get_parameter_data_for_one_paramtree(
                dataset.conn, dataset.table_name, dataset.description, "converted",
                check_cancelled=cancel,
            )
        _assert_columns(flatten_record_columns(parameter_data_for_export(dataset, "converted")), expected)


def test_trusted_missing_basis_budget_rejects_before_allocation_and_retries(tmp_path, monkeypatch):
    from qplot.datahandling import parameter_data

    path = tmp_path / "many-bases.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("Expanded tree budget", "regression")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("x", paramtype="array")
    bases = tuple(f"basis_{index}" for index in range(16))
    for name in bases:
        measurement.register_custom_parameter(name, paramtype="array", setpoints=("x",))
    measurement.register_custom_parameter("converted", paramtype="array",
                                          setpoints=("x",), basis=bases)
    samples = np.arange(8192, dtype=float)
    try:
        with measurement.run(write_in_background=False, in_memory_cache=False) as saver:
            saver.add_result(("x", samples), ("converted", 2 * samples),
                             *((name, samples + index) for index, name in enumerate(bases)))
            guid = saver.dataset.guid
    finally:
        saver.dataset.conn.close()
        experiment.conn.close()
    protected = database_state(path)
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
        constructed = []
        original_column = parameter_data._record_column

        def construct(*args, **kwargs):
            constructed.append(True)
            return original_column(*args, **kwargs)

        monkeypatch.setattr(parameter_data, "_record_column", construct)
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
        assert constructed == []
        assert (dataset.cache._read_status, dataset.cache._write_status) == previous_status
        assert {tree: {name: id(value) for name, value in columns.items()}
                for tree, columns in dataset.cache._data.items()} == previous_arrays
        monkeypatch.setattr(workers, "MAX_TRUSTED_FULL_PLOT_DECODE_BYTES", 24 * 1024**2)
        worker, errors, finished = run()
        assert errors == [] and finished == [True]
        assert update_cache_parameter_data(
            dataset.cache, "converted", worker.updated_read_status,
            worker.updated_write_status, worker.cache_data,
            dataset_completed=worker.dataset_completed,
        )
        columns = dataset.cache._data["converted"]
        assert sum(column.nbytes for column in columns.values()) == 18 * 1024**2
        np.testing.assert_array_equal(columns["converted"].ravel(), np.tile(2 * samples, 16))
        for index, name in enumerate(bases):
            actual = columns[name].ravel().reshape(16, -1)
            assert np.count_nonzero(np.isfinite(actual)) == samples.size
            np.testing.assert_array_equal(actual[np.isfinite(actual)], samples + index)
    finally:
        service.close(timeout=15)
    assert not service.liveness().resource_cleanup_pending
    assert database_state(path) == protected


@pytest.mark.parametrize("shaped", [False, True])
def test_expanded_budget_counts_retained_incremental_and_planned_extent(tmp_path, monkeypatch, shaped):
    from qplot.datahandling import parameter_data

    with _source(tmp_path, widths=(0, 2, 3), shaped=shaped) as (_path, _guid, dataset):
        cache = dataset.cache
        cache.prepare()
        write, read, previous = snapshot_cache_parameter_state(cache, "converted")
        result = load_param_data_from_db(
            dataset.conn, dataset.table_name, dataset.description, "converted",
            write, read, previous, end=3, max_decode_bytes=256,
        )
        assert update_cache_parameter_data(cache, "converted", *result)
        write, read, previous = snapshot_cache_parameter_state(cache, "converted")
        prior_arrays = {name: (id(values), deepcopy(values)) for name, values in
                        cache._data["converted"].items()}
        original_column = parameter_data._record_column

        def unexpected_allocation(*args, **kwargs):
            pytest.fail("Expanded budget must be checked before constructing columns")

        monkeypatch.setattr(parameter_data, "_record_column", unexpected_allocation)
        with pytest.raises(ValueError, match="decoding budget"):
            load_param_data_from_db(
                dataset.conn, dataset.table_name, dataset.description, "converted",
                write, read, previous, max_decode_bytes=256,
            )
        assert cache._read_status["converted"] == read["converted"]
        for name, (identity, values) in prior_arrays.items():
            assert id(cache._data["converted"][name]) == identity
            for actual, saved in zip(cache._data["converted"][name], values, strict=True):
                np.testing.assert_array_equal(actual, saved)
        monkeypatch.setattr(parameter_data, "_record_column", original_column)
        result = load_param_data_from_db(
            dataset.conn, dataset.table_name, dataset.description, "converted",
            write, read, previous, max_decode_bytes=320,
        )
        assert update_cache_parameter_data(cache, "converted", *result)
        expected, _count = _stored_columns(dataset)
        _assert_columns(flatten_record_columns(cache._data["converted"]), expected)


def test_budget_includes_shape_padding_before_column_allocation(tmp_path):
    with _source(tmp_path, shaped=True) as (_path, _guid, dataset):
        write, read, previous = snapshot_cache_parameter_state(dataset.cache, "converted")
        with pytest.raises(ValueError, match="decoding budget"):
            load_param_data_from_db(
                dataset.conn, dataset.table_name, dataset.description, "converted",
                write, read, previous, max_decode_bytes=160,
            )
        result = load_param_data_from_db(
            dataset.conn, dataset.table_name, dataset.description, "converted",
            write, read, previous, max_decode_bytes=192,
        )
        assert sum(column.nbytes for column in result[2]["converted"].values()) == 192


def test_empty_record_dtype_still_counts_in_expanded_budget(tmp_path):
    with _source(tmp_path, widths=(0, 32), shaped=True, empty_dtype=np.int64) as (_path, _guid, dataset):
        write, read, previous = snapshot_cache_parameter_state(dataset.cache, "converted")
        with pytest.raises(ValueError, match="decoding budget"):
            load_param_data_from_db(
                dataset.conn, dataset.table_name, dataset.description, "converted",
                write, read, previous, max_decode_bytes=16 * 1024,
            )
        result = load_param_data_from_db(
            dataset.conn, dataset.table_name, dataset.description, "converted",
            write, read, previous, max_decode_bytes=32 * 1024,
        )
        assert all(column.dtype.kind == "O" for column in result[2]["converted"].values())
        expected, _count = _stored_columns(dataset)
        _assert_columns(flatten_record_columns(result[2]["converted"]), expected)


def test_incremental_budget_retains_object_text_cost_and_cancels_projection(tmp_path, monkeypatch):
    from qplot.datahandling import parameter_data

    with _source(tmp_path, widths=(32, 32), other_type="text", direct=True,
                 text_value="Z" * 2048) as (_path, _guid, dataset):
        cache = dataset.cache
        write, read, previous = snapshot_cache_parameter_state(cache, "converted")
        result = load_param_data_from_db(
            dataset.conn, dataset.table_name, dataset.description, "converted",
            write, read, previous, end=3, max_decode_bytes=1024**2,
        )
        assert update_cache_parameter_data(cache, "converted", *result)
        assert cache._data["converted"]["other"].dtype.kind == "O"
        write, read, previous = snapshot_cache_parameter_state(cache, "converted")
        original_projection = parameter_data._check_materialization_budget
        projection_active = False

        def project(*args, **kwargs):
            nonlocal projection_active
            projection_active = True
            return original_projection(*args, **kwargs)

        def cancel():
            if projection_active:
                raise InterruptedError("cancelled expanded layout projection")

        def unexpected_allocation(*args, **kwargs):
            pytest.fail("Refusal and cancellation must precede column allocation")

        with monkeypatch.context() as patch:
            patch.setattr(parameter_data, "_record_column", unexpected_allocation)
            with pytest.raises(ValueError, match="decoding budget"):
                load_param_data_from_db(
                    dataset.conn, dataset.table_name, dataset.description, "converted",
                    write, read, previous, max_decode_bytes=800 * 1024,
                )
            patch.setattr(parameter_data, "_check_materialization_budget", project)
            with pytest.raises(InterruptedError, match="layout projection"):
                load_param_data_from_db(
                    dataset.conn, dataset.table_name, dataset.description, "converted",
                    write, read, previous, max_decode_bytes=2 * 1024**2,
                    check_cancelled=cancel,
                )
        assert cache._read_status["converted"] == read["converted"]
        assert cache._write_status["converted"] == write["converted"]
        expected, _count = _stored_columns(dataset, end=3)
        _assert_columns(flatten_record_columns(cache._data["converted"]), expected)
        result = load_param_data_from_db(
            dataset.conn, dataset.table_name, dataset.description, "converted",
            write, read, previous, max_decode_bytes=2 * 1024**2,
        )
        assert update_cache_parameter_data(cache, "converted", *result)
        expected, _count = _stored_columns(dataset)
        _assert_columns(flatten_record_columns(cache._data["converted"]), expected)
