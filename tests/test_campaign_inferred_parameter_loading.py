"""Viewer cache trees include recorded inferred QCoDeS measurements."""

from contextlib import contextmanager

import numpy as np
import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.datahandling.LoadFromDB import load_param_data_from_db
from qplot.datahandling.parameter_data import (
    flatten_record_columns,
    parameter_data_for_export,
)
from qplot.datahandling.qcodes_cache import (
    cache_parameter_is_synchronized,
    snapshot_cache_parameter_state,
    update_cache_parameter_data,
)
from qplot.datahandling.readonly import load_by_id_read_only
from qplot.windows import MainWindow
from qplot.windows._plotWin import plotWidget
from tests._window_lifecycle import close_main_window
from tests.windows.test_complex_line_data import database_state
from tests.windows.test_differentiation_integration import (
    apply_operations,
    operation_option,
)
from tests.windows.test_native_transform_labels import (
    no_callback_errors as _no_callback_errors,
)
from tests.windows.test_plot_csv_precision import _export_from_dialog, _open_csv_dialog
from tests.windows.test_plot_integration import configure_temp_qplot, wait_for

no_callback_errors = _no_callback_errors


@contextmanager
def _measurement(tmp_path, *, arrays, heatmap, shaped):
    path = tmp_path / "inferred.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("Inferred measurements", "regression")
    measurement = Measurement(exp=experiment)
    paramtype = "array" if arrays else "numeric"
    measurement.register_custom_parameter("x", paramtype=paramtype)
    if heatmap:
        measurement.register_custom_parameter("slow", paramtype=paramtype)
    setpoints = ("slow", "x") if heatmap else ("x",)
    measurement.register_custom_parameter("signal", paramtype=paramtype, setpoints=setpoints)
    for name in ("converted", "sibling"):
        measurement.register_custom_parameter(
            name, paramtype=paramtype, setpoints=setpoints, basis=("signal",),
        )
    if shaped:
        measurement.set_shapes({
            name: (2, 3) if heatmap else (6,)
            for name in ("signal", "converted", "sibling")
        })
    try:
        with measurement.run(write_in_background=False, in_memory_cache=False) as saver:
            def append(start, stop):
                indexes = np.arange(start, stop)
                columns = {
                    "x": (indexes % 3 if heatmap else indexes).astype(float),
                    "signal": indexes.astype(float),
                    "converted": 10 * indexes.astype(float),
                    "sibling": -5 * indexes.astype(float),
                }
                if heatmap:
                    columns["slow"] = (indexes // 3).astype(float)
                if arrays:
                    saver.add_result(*columns.items())
                else:
                    for index in range(stop - start):
                        saver.add_result(*((name, float(values[index]))
                                           for name, values in columns.items()))
                saver.flush_data_to_database(block=True)
            yield path, saver, append
    finally:
        saver.dataset.conn.close()
        experiment.conn.close()


@contextmanager
def _window(tmp_path, monkeypatch, path, guid, *, order=None):
    configure_temp_qplot(monkeypatch, tmp_path)
    errors = []
    monkeypatch.setattr(MainWindow, "show_error", lambda self, *args: errors.append(args))
    monkeypatch.setattr(plotWidget, "show_error", lambda self, *args: errors.append(args))
    window = MainWindow()
    try:
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.update_many({
            "user_preference.confirm_close": False,
            "user_preference.confirm_close_all": False,
            "user_preference.default_refresh_rate": 0,
        })
        window.spinBox.setValue(0)
        window.close_database(status=False)
        assert window.load_file(str(path))
        wait_for(lambda: not window._database_load_active)
        if order is None:
            window.openPlot(guid=guid)
        else:
            dataset = load_by_id_read_only(1, str(path))
            try:
                parameters = {param.name: param for param in dataset.get_parameters()}
                window.openPlot(guid=guid, params=[parameters[name] for name in order])
            finally:
                dataset.conn.close()
        wait_for(lambda: len(window.windows) == 3)
        wait_for(lambda: all(not plot.worker.running for plot in window.windows))
        for plot in window.windows:
            plot.monitor.stop()
        yield window, {plot.param.name: plot for plot in window.windows}, errors
    finally:
        close_main_window(window)


def _assert_samples(plots, count, heatmap):
    for name, multiplier in (("signal", 1), ("converted", 10), ("sibling", -5)):
        plot = plots[name]
        expected = multiplier * np.arange(count, dtype=float)
        if heatmap:
            columns = min(count, 3)
            grid = np.full(((count + 2) // 3, columns), np.nan)
            grid.flat[:count] = expected
            np.testing.assert_array_equal(plot.dataGrid, grid)
        else:
            np.testing.assert_array_equal(plot.line.getOriginalDataset()[1], expected)


@pytest.mark.parametrize("arrays", [False, True], ids=["numeric", "array"])
@pytest.mark.parametrize("heatmap", [False, True], ids=["line", "heatmap"])
@pytest.mark.parametrize("shaped", [False, True], ids=["unshaped", "shaped"])
def test_inferred_measurements_plot_export_and_process(
    tmp_path, monkeypatch, no_callback_errors, arrays, heatmap, shaped,
):
    with _measurement(tmp_path, arrays=arrays, heatmap=heatmap, shaped=shaped) as (
        path, saver, append,
    ):
        append(0, 6)
        guid, run_id = saver.dataset.guid, saver.run_id
    protected = database_state(path)
    dataset = load_by_id_read_only(run_id, str(path))
    try:
        raw = flatten_record_columns(parameter_data_for_export(dataset, "converted"))
        np.testing.assert_array_equal(raw["converted"], 10 * np.arange(6.))
    finally:
        dataset.conn.close()
    with _window(tmp_path, monkeypatch, path, guid) as (_main, plots, errors):
        assert errors == []
        _assert_samples(plots, 6, heatmap)
        plot = plots["converted"]
        dialog = _open_csv_dialog(plot)
        rows = _export_from_dialog(monkeypatch, plot, dialog, tmp_path / "samples.csv", ",")
        assert [float(row[-1]) for row in rows[1:]] == (10 * np.arange(6.)).tolist()
        option = operation_option(plot, "dz/dx" if heatmap else "dy/dx")
        option.input.setChecked(True)
        assert apply_operations(plot)[1:] == ([True], [])
        values = plot.dataGrid if heatmap else plot.line.getOriginalDataset()[1]
        np.testing.assert_array_equal(values, np.full(values.shape, 10.))
        option.input.setChecked(False)
        assert apply_operations(plot)[1:] == ([True], [])
        _assert_samples(plots, 6, heatmap)
        assert errors == []
    assert database_state(path) == protected


@pytest.mark.parametrize("arrays", [False, True], ids=["numeric", "ragged_array"])
@pytest.mark.parametrize("heatmap", [False, True], ids=["line", "heatmap"])
@pytest.mark.parametrize("shaped", [False, True], ids=["unshaped", "planned_overflow"])
def test_inferred_live_refreshes_keep_sibling_cursors_independent(
    tmp_path, monkeypatch, no_callback_errors, arrays, heatmap, shaped,
):
    with _measurement(tmp_path, arrays=arrays, heatmap=heatmap, shaped=shaped) as (
        path, saver, append,
    ):
        append(0, 2)
        protected = database_state(path)
        with _window(tmp_path, monkeypatch, path, saver.dataset.guid,
                     order=("converted", "sibling", "signal")) as (_main, plots, errors):
            assert errors == []
            _assert_samples(plots, 2, heatmap)
            for start, stop in ((2, 6), (6, 9)):
                assert database_state(path) == protected
                append(start, stop)
                protected = database_state(path)
                # Queue each independent tree before processing any callbacks.
                for name in ("sibling", "signal", "converted"):
                    plots[name].refreshPlotButton.click()
                wait_for(lambda: all(not plot.worker.running for plot in plots.values()))
                assert errors == []
                _assert_samples(plots, stop, heatmap)
                assert database_state(path) == protected
            cache = plots["converted"].ds.cache
            assert len({id(cache._data[name]) for name in plots}) == 3
            assert not cache_parameter_is_synchronized(cache, "converted")
        assert database_state(path) == protected


@pytest.mark.parametrize("shaped", [False, True])
def test_cancelled_initial_inferred_read_is_private_and_retry_can_publish(tmp_path, shaped):
    with _measurement(tmp_path, arrays=True, heatmap=False, shaped=shaped) as (path, saver, append):
        append(0, 2)
        append(2, 6)
        run_id = saver.run_id
    protected = database_state(path)
    dataset = load_by_id_read_only(run_id, str(path))
    try:
        cache = dataset.cache
        cache.prepare()
        saved_data = dict(cache._data)
        saved_read, saved_write = dict(cache._read_status), dict(cache._write_status)
        write, read, existing = snapshot_cache_parameter_state(cache, "converted")
        calls = 0

        def cancel():
            nonlocal calls
            calls += 1
            if calls == 4:
                raise InterruptedError("cancelled initial inferred read")

        with pytest.raises(InterruptedError, match="cancelled initial"):
            load_param_data_from_db(
                dataset.conn, dataset.table_name, dataset.description,
                "converted", write, read, existing, check_cancelled=cancel,
            )
        assert cache._data == saved_data
        assert cache._read_status == saved_read and cache._write_status == saved_write
        assert not cache_parameter_is_synchronized(cache, "converted")
        old = load_param_data_from_db(
            dataset.conn, dataset.table_name, dataset.description,
            "converted", write, read, existing, end=1,
        )
        new = load_param_data_from_db(
            dataset.conn, dataset.table_name, dataset.description,
            "converted", write, read, existing,
        )
        assert update_cache_parameter_data(cache, "converted", *new, dataset_completed=True)
        assert not update_cache_parameter_data(cache, "converted", *old)
        values = flatten_record_columns(cache._data["converted"])["converted"]
        np.testing.assert_array_equal(values, 10 * np.arange(6.))
        assert cache_parameter_is_synchronized(cache, "converted")
        assert not cache_parameter_is_synchronized(cache, "signal")
        for name, original in saved_data.items():
            if name != "converted":
                assert cache._data[name] is original
        with pytest.raises(KeyError):
            snapshot_cache_parameter_state(cache, "not_a_parameter")
    finally:
        dataset.conn.close()
    assert database_state(path) == protected
