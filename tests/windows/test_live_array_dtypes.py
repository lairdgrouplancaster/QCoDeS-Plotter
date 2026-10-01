"""Planned array refreshes retain numeric values before plot validation."""

import sys
import warnings

import numpy as np
import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.dataset.sqlite.database import connect

from qplot.datahandling.LoadFromDB import load_param_data_from_db
from qplot.datahandling.qcodes_cache import (
    cache_parameter_is_synchronized,
    snapshot_cache_parameter_publication_state,
)
from qplot.datahandling.readonly import load_by_id_read_only
from qplot.testdata import enable_generation_provenance_for_writer
from qplot.tools.worker import loader
from qplot.windows import main as main_window
from tests._window_lifecycle import close_main_window
from tests.windows.test_complex_line_data import database_state
from tests.windows.test_plot_integration import (
    configure_temp_qplot,
    prepare_generated_database_for_live_writes,
    wait_for,
)


@pytest.mark.parametrize("journal_mode", ["DELETE", "WAL"])
@pytest.mark.parametrize("column", ["signal", "fast"])
@pytest.mark.parametrize("complex_update", [False, True], ids=["float", "complex"])
def test_live_planned_array_dtype_change(
    tmp_path, monkeypatch, journal_mode, column, complex_update,
):
    configure_temp_qplot(monkeypatch, tmp_path)
    path = tmp_path / "live-array.db"
    if journal_mode == "WAL":
        # Array plots use the snapshot reader. Give this synthetic writer
        # provenance so its live WAL can be read through the supported path.
        prepare_generated_database_for_live_writes(path)
    initialise_or_create_database_at(str(path), journal_mode=journal_mode)
    writer = connect(str(path))
    if journal_mode == "WAL":
        enable_generation_provenance_for_writer(writer)
    experiment = load_or_create_experiment("live_array_dtypes", sample_name="test", conn=writer)
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("fast", paramtype="array")
    measurement.register_custom_parameter("signal", paramtype="array", setpoints=("fast",))
    measurement.set_shapes({"signal": (4,)})
    measurement.write_period = 3600
    initial = {"fast": np.array([0., 1.]), "signal": np.array([10., 20.])}
    initial[column] = initial[column].astype(np.float64 if complex_update else np.int64)
    incoming = {"fast": np.array([2., 3.]), "signal": np.array([30.5, 40.5])}
    incoming[column] = np.array([30 + 5j, 40 + 6j] if complex_update else [30.5, 40.5])
    expected = {name: np.concatenate((initial[name], incoming[name])) for name in initial}
    uncaught = []
    monkeypatch.setattr(sys, "excepthook", lambda *args: uncaught.append(args))
    window = dataset = fresh = None
    try:
        # qPlot owns the reader cache under test; disable QCoDeS' independent
        # writer cache, whose shaped merge has the same upstream dtype issue.
        with measurement.run(write_in_background=False, in_memory_cache=False) as datasaver:
            dataset = datasaver.dataset
            datasaver.add_result(*initial.items())
            datasaver.flush_data_to_database(block=True)
            protected = database_state(path)
            params = {param.name: param for param in dataset.get_parameters()}
            window = main_window.MainWindow()
            window.startupDatabaseTimer.stop()
            window.monitor.stop()
            window.config.config["user_preference"]["confirm_close"] = False
            window.config.config["user_preference"]["confirm_close_all"] = False
            errors = []
            monkeypatch.setattr(window, "show_error", lambda *args: errors.append(args))
            window.close_database(status=False)
            assert window.load_file(str(path))
            wait_for(lambda: not window._database_load_active and not window._database_detail_active)
            window.monitor.stop()
            window.openPlot(guid=dataset.guid, params=[params["signal"]], show=False)
            assert not errors, errors
            plot = window.windows[-1]
            wait_for(lambda: not plot.worker.running)
            plot.monitor.stop()
            assert not plot._last_error_text
            cache = plot.ds.cache
            prior = snapshot_cache_parameter_publication_state(cache, "signal")
            assert prior.read_status == 1
            assert prior.write_status == 2
            assert prior.data[column].dtype == initial[column].dtype
            for values in prior.data.values():
                assert values.shape == (4,)
            saved_arrays = {name: values.copy() for name, values in prior.data.items()}
            for axis, name in enumerate(("fast", "signal")):
                np.testing.assert_array_equal(plot.line.getData()[axis], initial[name])
            assert database_state(path) == protected

            fields = {name: getattr(plot, name) for name in (
                "axis_data", "axis_param", "display_param", "last_ds_len",
            )}
            updates = []
            plot.trace_updated.connect(lambda: updates.append(True))
            datasaver.add_result(*incoming.items())
            datasaver.flush_data_to_database(block=True)
            protected = database_state(path)
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                plot.refreshWindow(force=True)
                wait_for(lambda: not plot.worker.running)
                plot.monitor.stop()
                assert not any(isinstance(w.message, np.exceptions.ComplexWarning) for w in caught)

                # A fresh reader starts with no cache or acquired offsets.
                fresh = load_by_id_read_only(dataset.run_id, str(path))
                read, write, full = load_param_data_from_db(
                    fresh.conn, fresh.table_name, fresh.description, "signal", {}, {},
                    {"signal": {name: np.array([]) for name in initial}},
                )
                assert read["signal"] == 2
                assert write["signal"] == 4
                for name, values in expected.items():
                    np.testing.assert_array_equal(full["signal"][name], values)
                full_worker = loader(fresh.cache, params["signal"], params, {"x": "fast"})
                finished, errors = [], []
                full_worker.emitter.finished.connect(finished.append)
                full_worker.emitter.errorOccurred.connect(errors.append)
                full_worker.run()

            current = snapshot_cache_parameter_publication_state(cache, "signal")
            if complex_update:
                assert "complex" in plot._last_error_text.lower()
                assert "not supported" in plot._last_error_text.lower()
                assert column in plot._last_error_text
                assert finished == [False]
                assert len(errors) == 1
                assert plot._last_error_text == f"ValueError: {errors[0]}"
                assert not updates
                assert current == prior
                assert current.data is prior.data
                assert all(getattr(plot, name) is value for name, value in fields.items())
                assert not plot._qplot_display_synchronized
                assert not cache_parameter_is_synchronized(cache, "signal")
                assert not isinstance(getattr(plot.worker, "_qplot_publication_snapshot", None), dict)
                # The worker's private merge must reach validation with all
                # imaginary components intact; failed data stays unpublished.
                np.testing.assert_array_equal(plot.worker.cache_data["signal"][column], expected[column])
                for axis, name in enumerate(("fast", "signal")):
                    np.testing.assert_array_equal(plot.line.getData()[axis], initial[name])
            else:
                assert not plot._last_error_text
                assert finished == [True]
                assert errors == []
                assert updates == [True]
                assert current.read_status == 2
                assert current.write_status == 4
                for name, values in full["signal"].items():
                    np.testing.assert_array_equal(current.data[name], values)
                    assert current.data[name].dtype == values.dtype
                    assert current.data[name].shape == (4,)
                for axis, name in enumerate(("fast", "signal")):
                    np.testing.assert_array_equal(plot.line.getData()[axis], expected[name])
                    np.testing.assert_array_equal(plot.axis_data["xy"[axis]], full_worker.axis_data["xy"[axis]])
            for name, values in saved_arrays.items():
                np.testing.assert_array_equal(prior.data[name], values)
            assert not any(isinstance(w.message, np.exceptions.ComplexWarning) for w in caught)
            assert not uncaught, [str(args[1]) for args in uncaught]
            if fresh is not None:
                fresh.conn.close()
                fresh = None
            close_main_window(window)
            window = None
            assert database_state(path) == protected
    finally:
        if fresh is not None:
            fresh.conn.close()
        if window is not None:
            close_main_window(window)
        if dataset is not None:
            dataset.conn.close()
        experiment.conn.close()
