import hashlib
import threading
import time
from pathlib import Path
from time import perf_counter

import numpy as np
import qcodes
from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_by_id,
    load_or_create_experiment,
)
from qcodes.parameters import ManualParameter

import qplot.tools.worker as worker_module
from qplot.configuration.config import config
from qplot.datahandling.qcodes_cache import cache_parameter_data
from qplot.datahandling.readonly import (
    load_by_id_read_only,
    set_qcodes_database_location,
)
from qplot.tools.worker import loader
from qplot.windows import main as main_window
from qplot.windows._dataset_handle import DatasetHandle, DatasetKey
from qplot.windows._plotWin import plotWidget


def configure_temp_qplot(monkeypatch, tmp_path):
    qplot_home = tmp_path / ".qplot"
    monkeypatch.setattr(config, "default_path", str(qplot_home))
    monkeypatch.setattr(config, "default_file", str(qplot_home / config.config_file_name))


def wait_for(predicate, timeout=12):
    app = qtw.QApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            app.processEvents()
            return
        time.sleep(0.03)
    raise AssertionError("Timed out waiting for GUI integration state")


def build_synthetic_database(db_path):
    initialise_or_create_database_at(str(db_path))
    experiment = load_or_create_experiment("qplot_integration", sample_name="synthetic")

    gate = ManualParameter("gate", label="Gate voltage", unit="V")
    bias = ManualParameter("bias", label="Bias voltage", unit="mV")
    current = ManualParameter("current", label="Current", unit="nA")
    conductance = ManualParameter("conductance", label="Conductance", unit="uS")

    line_meas = Measurement(exp=experiment, name="line_integration")
    line_meas.register_parameter(gate)
    line_meas.register_parameter(current, setpoints=(gate,))
    with line_meas.run() as datasaver:
        for gate_value in np.linspace(-1.5, 1.5, 11):
            datasaver.add_result(
                (gate, float(gate_value)),
                (current, float(np.sin(gate_value) * 20.0)),
            )
        line_run_id = datasaver.dataset.run_id

    heatmap_meas = Measurement(exp=experiment, name="heatmap_integration")
    heatmap_meas.register_parameter(gate)
    heatmap_meas.register_parameter(bias)
    heatmap_meas.register_parameter(conductance, setpoints=(gate, bias))
    with heatmap_meas.run() as datasaver:
        for gate_value in np.linspace(-1.0, 1.0, 7):
            for bias_value in np.linspace(-0.6, 0.6, 5):
                value = np.cos(gate_value * 2.0) + np.sin(bias_value * 3.0)
                datasaver.add_result(
                    (gate, float(gate_value)),
                    (bias, float(bias_value)),
                    (conductance, float(value)),
                )
        heatmap_run_id = datasaver.dataset.run_id

    return line_run_id, heatmap_run_id


def dependent_parameter(dataset, dimensions):
    for param in dataset.get_parameters():
        if param.depends_on and len(param.depends_on_) == dimensions:
            return param
    raise AssertionError(f"No {dimensions}D dependent parameter in run {dataset.run_id}")


def close_main_window(window):
    window.startupDatabaseTimer.stop()
    window.monitor.stop()
    window.close_plot_windows(confirm=False, status=False)
    window.threadPool.waitForDone(1000)
    window.databaseLoadThreadPool.waitForDone(1000)
    window.hide()
    window.deleteLater()
    qtw.QApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    qtw.QApplication.processEvents()


def database_artifact_state(database_path):
    """Record content and metadata for the database and SQLite sidecars."""

    state = {}
    for suffix in ("", "-wal", "-shm", "-journal"):
        path = Path(f"{database_path}{suffix}")
        if not path.exists():
            state[suffix] = None
            continue
        stat = path.stat()
        state[suffix] = (
            stat.st_size,
            stat.st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
            )
    return state


def run_plot_worker_on_thread(worker):
    errors = []
    worker.emitter.errorOccurred.connect(errors.append)
    thread = threading.Thread(target=worker.run)
    thread.start()
    thread.join(10)
    qtw.QApplication.processEvents()
    assert not thread.is_alive()
    return errors


def test_main_window_opens_real_1d_and_2d_plots(tmp_path, monkeypatch):
    configure_temp_qplot(monkeypatch, tmp_path)
    database_path = Path(tmp_path) / "qplot-integration.db"
    line_run_id, heatmap_run_id = build_synthetic_database(database_path)

    window = main_window.MainWindow()
    try:
        window.startupDatabaseTimer.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.close_database(status=False)

        assert window.load_file(str(database_path))
        wait_for(
            lambda: (
                not window._database_load_active
                and window.RunList.topLevelItemCount() >= 2
            )
        )

        line_dataset = load_by_id(line_run_id)
        line_param = dependent_parameter(line_dataset, 1)
        window.ds = line_dataset
        window.openPlot(params=[line_param], show=True)
        line_window = window.windows[-1]
        wait_for(
            lambda: (
                hasattr(line_window, "axis_data")
                and not getattr(line_window.worker, "running", False)
            )
        )

        assert line_window.axis_data["x"].size == 11
        assert line_window.axis_data["y"].size == 11
        assert np.isfinite(line_window.axis_data["y"]).all()
        assert isinstance(window.x(), int)
        assert isinstance(window.y(), int)
        assert line_window.layout() is not None
        assert line_window.width() > 0
        assert line_window.height() > 0

        heatmap_dataset = load_by_id(heatmap_run_id)
        heatmap_param = dependent_parameter(heatmap_dataset, 2)
        window.ds = heatmap_dataset
        window.openPlot(params=[heatmap_param], show=False)
        heatmap_window = window.windows[-1]
        wait_for(
            lambda: (
                hasattr(heatmap_window, "dataGrid")
                and not getattr(heatmap_window.worker, "running", False)
            )
        )

        assert heatmap_window.dataGrid.shape == (7, 5)
        assert np.isfinite(heatmap_window.dataGrid).all()

        source_x = np.asarray(heatmap_window.axis_data["x"]).copy()
        source_y = np.asarray(heatmap_window.axis_data["y"]).copy()
        source_grid = np.asarray(heatmap_window.dataGrid).copy()

        heatmap_window.z_index = [2, 1]
        heatmap_window.openSweep("h")
        horizontal_cut = window.windows[-1]
        wait_for(lambda: not getattr(horizontal_cut.worker, "running", False))

        np.testing.assert_array_equal(horizontal_cut.axis_data["x"], source_x)
        np.testing.assert_array_equal(horizontal_cut.axis_data["y"], source_grid[1, :])
        assert horizontal_cut.sweep_id in heatmap_window.sweep_lines

        heatmap_window.z_index = [2, 1]
        heatmap_window.openSweep("v")
        vertical_cut = window.windows[-1]
        wait_for(lambda: not getattr(vertical_cut.worker, "running", False))

        np.testing.assert_array_equal(vertical_cut.axis_data["x"], source_y)
        np.testing.assert_array_equal(vertical_cut.axis_data["y"], source_grid[:, 2])
        assert vertical_cut.sweep_id in heatmap_window.sweep_lines
    finally:
        close_main_window(window)


def test_threaded_live_run_completion_commits_final_rows_before_stopping_monitor(
    tmp_path,
    monkeypatch,
):
    database_path = Path(tmp_path) / "threaded-live-run.db"
    original_database_path = qcodes.config.core.db_location
    initial_row_written = threading.Event()
    finish_writer = threading.Event()
    writer_state = {}
    writer_errors = []
    viewer_dataset = None

    def write_measurement():
        try:
            initialise_or_create_database_at(str(database_path), journal_mode="WAL")
            experiment = load_or_create_experiment(
                "threaded_live_run",
                sample_name="completion_race",
                )
            gate = ManualParameter("gate")
            signal = ManualParameter("signal")
            measurement = Measurement(exp=experiment, name="completion_race")
            measurement.register_parameter(gate)
            measurement.register_parameter(signal, setpoints=(gate,))
            with measurement.run(write_in_background=False) as datasaver:
                datasaver.add_result((gate, 0.0), (signal, 10.0))
                datasaver.flush_data_to_database(block=True)
                writer_state["run_id"] = datasaver.dataset.run_id
                initial_row_written.set()
                if not finish_writer.wait(10):
                    raise TimeoutError("Test did not release the measurement writer")
                datasaver.add_result((gate, 1.0), (signal, 11.0))
                datasaver.add_result((gate, 2.0), (signal, 12.0))
            datasaver.dataset.conn.close()
        except BaseException as error:
            writer_errors.append(error)
            initial_row_written.set()

    writer_thread = threading.Thread(target=write_measurement)
    writer_thread.start()

    try:
        assert initial_row_written.wait(10)
        assert writer_errors == []

        set_qcodes_database_location(database_path)
        viewer_dataset = load_by_id_read_only(writer_state["run_id"])
        assert viewer_dataset.running
        assert viewer_dataset.number_of_results == 1

        parameter = dependent_parameter(viewer_dataset, 1)
        parameter._complete = False
        parameters = {
            candidate.name: candidate
            for candidate in viewer_dataset.get_parameters()
            }
        parameters[parameter.name] = parameter
        axes = {"x": parameter.depends_on_[0]}

        finish_writer.set()
        writer_thread.join(10)
        assert not writer_thread.is_alive()
        assert writer_errors == []
        assert viewer_dataset.running
        # The source-preserving viewer connection is intentionally pinned to
        # the WAL snapshot from when the run was opened. Plot workers must use
        # fresh read-only snapshots to discover the committed final rows.
        assert viewer_dataset.number_of_results == 1

        writer_complete_artifacts = database_artifact_state(database_path)
        assert set(writer_complete_artifacts) == {"", "-wal", "-shm", "-journal"}
        assert writer_complete_artifacts[""] is not None

        original_load = worker_module.load_param_data_from_db

        def fail_final_load(*_args, **_kwargs):
            raise RuntimeError("injected final-load failure")

        monkeypatch.setattr(
            worker_module,
            "load_param_data_from_db",
            fail_final_load,
            )
        failed_worker = loader(
            viewer_dataset.cache,
            parameter,
            parameters,
            axes,
            )
        failed_errors = run_plot_worker_on_thread(failed_worker)

        assert len(failed_errors) == 1
        assert "injected final-load failure" in str(failed_errors[0])
        assert failed_worker.dataset_completed is True
        assert viewer_dataset.running
        assert not viewer_dataset.completed
        assert not parameter._complete

        class Monitor:
            def __init__(self):
                self.active = True
                self.stop_count = 0

            def stop(self):
                self.active = False
                self.stop_count += 1

        class SpinBox:
            def value(self):
                return 0.1

        class EndWait:
            def __init__(self):
                self.count = 0

            def emit(self):
                self.count += 1

        dataset_key = DatasetKey(database_path, viewer_dataset.guid)
        window = plotWidget.__new__(plotWidget)
        window._dataset_key = dataset_key
        window._dataset_holder = {
            dataset_key: DatasetHandle(viewer_dataset),
            }
        window._guid = viewer_dataset.guid
        window.param = parameter
        window.worker = failed_worker
        window.monitor = Monitor()
        window.spinBox = SpinBox()
        window.end_wait = EndWait()
        window._last_error_text = None
        window.show_status = lambda *_args, **_kwargs: None
        window.show_plot_state = lambda *_args, **_kwargs: None
        window.hide_plot_state = lambda: None
        window._set_param_axis_labels = lambda: None
        monitor_restarts = []

        def restart_monitor(interval):
            monitor_restarts.append(interval)
            window.monitor.active = True

        window.monitorIntervalChanged = restart_monitor

        assert plotWidget.refreshPlot(window, False, worker=failed_worker) is False
        window.last_ds_len = viewer_dataset.number_of_results
        retry_requests = []
        window.load_data = lambda: retry_requests.append(True)

        plotWidget.refreshWindow(window)

        assert retry_requests == [True]
        assert monitor_restarts == [0.1]
        assert window.monitor.active

        monkeypatch.setattr(
            worker_module,
            "load_param_data_from_db",
            original_load,
            )
        successful_worker = loader(
            viewer_dataset.cache,
            parameter,
            parameters,
            axes,
            )
        successful_worker.started_at = perf_counter()
        successful_worker.dataset_length_at_start = viewer_dataset.number_of_results
        successful_errors = run_plot_worker_on_thread(successful_worker)

        assert successful_errors == []
        assert successful_worker.dataset_completed is True
        assert viewer_dataset.running

        window.worker = successful_worker
        assert plotWidget.refreshPlot(window, True, worker=successful_worker) is True

        np.testing.assert_array_equal(
            cache_parameter_data(viewer_dataset.cache, parameter.name)["gate"],
            [0.0, 1.0, 2.0],
            )
        np.testing.assert_array_equal(
            cache_parameter_data(viewer_dataset.cache, parameter.name)["signal"],
            [10.0, 11.0, 12.0],
            )
        np.testing.assert_array_equal(window.axis_data["x"], [0.0, 1.0, 2.0])
        np.testing.assert_array_equal(window.axis_data["y"], [10.0, 11.0, 12.0])
        assert viewer_dataset.completed
        assert not viewer_dataset.running
        assert parameter._complete

        previous_restart_count = len(monitor_restarts)
        plotWidget.refreshWindow(window)
        assert len(monitor_restarts) == previous_restart_count
        assert not window.monitor.active
        assert retry_requests == [True]
        assert database_artifact_state(database_path) == writer_complete_artifacts
    finally:
        finish_writer.set()
        writer_thread.join(10)
        if viewer_dataset is not None:
            viewer_dataset.conn.close()
        qcodes.config.core.db_location = original_database_path
