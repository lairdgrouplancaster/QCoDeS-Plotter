"""Time a trusted explicit plot through Qt painting, without source snapshots.

Run with the project venv: python scripts/benchmark_plot_launch.py DATABASE RUN_ID
Settings are private to this invocation. The database is
opened only by qPlot's trusted reader. No writer or checkpoint is started.
"""

import argparse
import json
import tempfile
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("run_id", type=int)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--preceding-run", type=int)
    parser.add_argument("--max-threads", type=int)
    parser.add_argument("--action", choices=("guid", "double-click", "run"), default="guid")
    args = parser.parse_args()

    from PyQt6 import QtCore, QtWidgets

    from qplot.configuration.config import config
    from qplot.datahandling import readonly, trusted_plot
    from qplot.windows._plot_feedback import PlotWindowFeedbackMixin
    from qplot.windows.main import MainWindow

    def source_state():
        return {
            suffix: (candidate.stat().st_size, candidate.stat().st_mtime_ns)
            for suffix in ("", "-wal", "-journal")
            if (candidate := Path(str(args.database) + suffix)).exists()
        }

    def no_copy(*_args, **_kwargs):
        raise AssertionError("Benchmark attempted whole-database snapshot copying")

    readonly._copy_file_cooperatively = no_copy
    timings = []
    capture_steps = {}
    original_prefix = trusted_plot.plot_prefix

    def timed_prefix(*values):
        started = time.perf_counter()
        run_id = values[1].run_id
        steps = original_prefix(*values)
        try:
            while True:
                try:
                    next(steps)
                except StopIteration as complete:
                    result = complete.value
                    timings.append({"run_id": run_id,
                                    "capture_ms": (time.perf_counter() - started) * 1000,
                                    "rows": result.row_count,
                                    "private_bytes": result.path.stat().st_size if hasattr(result, "path") else 0})
                    return result
                capture_steps[run_id] = capture_steps.get(run_id, 0) + 1
                if capture_steps[run_id] % 256 == 0:
                    print(f"Run {run_id}: {capture_steps[run_id]} bounded steps, "
                          f"{time.perf_counter() - started:.1f} s", flush=True)
                yield
        finally:
            steps.close()

    trusted_plot.plot_prefix = timed_prefix
    app = QtWidgets.QApplication([])
    app.setQuitOnLastWindowClosed(False)

    def wait_for(predicate, timeout=3660, *, check_errors=True):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            app.processEvents()
            if check_errors and errors:
                raise RuntimeError(str(errors))
            if predicate():
                return
            time.sleep(0.001)
        raise TimeoutError("Plot benchmark did not reach display completion")

    class PaintTimes(QtCore.QObject):
        def eventFilter(self, watched, event):
            if event.type() == QtCore.QEvent.Type.Paint:
                if (getattr(watched, "_qplot_display_synchronized", False)
                        and watched not in painted):
                    painted[watched] = time.perf_counter()
            return False

    before = source_state()
    results, errors = [], []
    PlotWindowFeedbackMixin.show_error = lambda self, *items: errors.append(items)
    with tempfile.TemporaryDirectory(prefix="qplot-benchmark-") as directory:
        config.default_path = directory
        config.default_file = str(Path(directory) / config.config_file_name)
        window = MainWindow()
        window.show_error = lambda *items: errors.append(items)
        observer = PaintTimes()
        try:
            window.startupDatabaseTimer.stop()
            window.config.config["user_preference"]["confirm_close"] = False
            window.config.config["user_preference"]["confirm_close_all"] = False
            window.config.config["runtime_settings"]["del_grace_period"] = 0
            if args.max_threads is not None:
                window.threadPool.setMaxThreadCount(args.max_threads)
            window.close_database(status=False)
            started = time.perf_counter()
            window.load_file(str(args.database))
            wait_for(lambda: not window._database_load_active)
            open_ms = (time.perf_counter() - started) * 1000
            if window._database_access_mode != "trusted_live":
                raise RuntimeError("Benchmark requires an accepted trusted session")
            window.monitor.stop()
            metadata = window._run_metadata_for_id(args.run_id)
            guid = metadata["guid"]
            def launch(run_guid):
                if args.action == "guid":
                    window.openPlot(run_guid, show=True)
                else:
                    item = next(window.RunList.topLevelItem(index)
                                for index in range(window.RunList.topLevelItemCount())
                                if window.RunList.topLevelItem(index).guid == run_guid)
                    window.RunList.setCurrentItem(item)
                    if args.action == "double-click":
                        window.RunList.itemDoubleClicked.emit(item, 0)
                    else:
                        window.measurementBox.setText("*")
                        window.plotRunButton.click()
            for iteration in range(args.repeats):
                painted = {}
                timings.clear()
                capture_steps.clear()
                if args.preceding_run is not None:
                    preceding_guid = window._run_metadata_for_id(args.preceding_run)["guid"]
                    launch(preceding_guid)
                    wait_for(lambda: capture_steps.get(args.preceding_run, 0) > 0)
                preceding_steps_start = capture_steps.get(args.preceding_run, 0)
                started = time.perf_counter()
                launch(guid)
                wait_for(lambda: any(plot.ds.guid == guid for plot in window.windows))
                plots = [plot for plot in window.windows if plot.ds.guid == guid]
                for plot in plots:
                    plot.installEventFilter(observer)
                    plot.update()
                wait_for(lambda plots=plots: all(getattr(plot, "_qplot_display_synchronized", False)
                                     for plot in plots))
                # Grab requests a synchronous Qt render of each populated plot;
                # no cached preview is involved in this display measurement.
                for plot in plots:
                    plot.grab()
                app.processEvents()
                results.append({
                    "iteration": iteration + 1,
                    "display_ms": (time.perf_counter() - started) * 1000,
                    "paint_ms": [(value - started) * 1000 for value in painted.values()],
                    "grids": [list(plot.dataGrid.shape) for plot in plots],
                    "capture": list(timings),
                    "preceding_run_pending": any(plot.worker.running for plot in window.windows
                                                 if plot.ds.run_id == args.preceding_run),
                    "preceding_capture_steps": [preceding_steps_start,
                                                capture_steps.get(args.preceding_run, 0)],
                })
                window.close_plot_windows(confirm=False, status=False)
                wait_for(lambda: not window._plot_workers)
        finally:
            window.close_plot_windows(confirm=False, status=False)
            window._cancel_plot_work()
            window.threadPool.waitForDone(10_000)
            window.close_database(status=False)
            window.hide()
            wait_for(lambda: not window._retired_trusted_read_services,
                     timeout=20, check_errors=False)
    after = source_state()
    print(json.dumps({"database_bytes": args.database.stat().st_size,
                      "database_open_ms": open_ms, "run_id": args.run_id,
                      "preceding_run": args.preceding_run,
                      "results": results, "protected_file_stats_unchanged": before == after}, indent=2))
    if before != after:
        raise RuntimeError("Source main/WAL/journal metadata changed during validation")


if __name__ == "__main__":
    main()
