from time import perf_counter
from typing import TYPE_CHECKING, Any

from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from qplot.datahandling import load_param_data_from_db_prep
from qplot.datahandling.qcodes_cache import (
    cache_has_no_written_data,
    cache_is_live,
    update_cache_parameter_data,
)
from qplot.diagnostics import log_exception
from qplot.tools import loader

if TYPE_CHECKING:
    class _PlotRefreshBase(qtw.QMainWindow):
        _last_error_text: str | None
        _live: bool
        axis_data: dict[str, Any]
        axis_param: dict[str, Any]
        config: Any
        dataGrid: Any
        ds: Any
        end_wait: Any
        last_ds_len: int
        lines: dict[Any, Any]
        monitor: QtCore.QTimer
        oper_widget: Any
        param: Any
        param_dict: dict[str, Any]
        spinBox: qtw.QDoubleSpinBox
        threadPool: QtCore.QThreadPool
        worker: Any

        @property
        def axis_options(self) -> dict[str, str]: ...

        def _set_param_axis_labels(self) -> None: ...
        def hide_plot_state(self) -> None: ...
        def initFrame(self) -> None: ...
        def monitorIntervalChanged(self, interval: float) -> None: ...
        def show_error(
                self,
                title: str,
                message: str,
                details: str | None = None,
                ) -> None: ...
        def show_plot_state(
                self,
                title: object,
                detail: object | None = None,
                kind: str = "info",
                ) -> None: ...
        def show_status(self, message: str, timeout: int = 5000) -> None: ...
else:
    class _PlotRefreshBase:
        pass


class PlotRefreshMixin(_PlotRefreshBase):
    """
    Worker-backed plot refresh orchestration shared by plot windows.

    """

    def load_data(
            self,
            wait_on_thread: bool = False,
            *,
            force_sql_heatmap: bool = False,
            heatmap_axis_ranges: dict[str, tuple[float, float]] | None = None,
            heatmap_full_axis_ranges: dict[str, tuple[float, float]] | None = None,
            status_message: str | None = None,
            ) -> None:
        """
        Produces a worker for loading/refreshing the dataset.
        Then adds the worker to the threadPool queue to work.

        Can use wait_on_thread=True to force main thread to wait for callback.
        Recommend to avoid where possible, as effects all windows.

        Parameters
        ----------
        wait_on_thread : bool, optional
            If true uses an QEventLoop to stop main code from running until
            worker has finished its task. The default is False.

        """
        worker: Any = loader(
            self.ds.cache,
            self.param,
            self.param_dict,
            self.axis_options,
            read_data=True,
            operations=self.oper_widget.get_data(),
            force_sql_heatmap=force_sql_heatmap,
            max_full_heatmap_points=self.config.get(
                "runtime_settings.max_full_heatmap_points"
                ),
            heatmap_axis_ranges=heatmap_axis_ranges,
            heatmap_full_axis_ranges=heatmap_full_axis_ranges,
            )

        use_sql_heatmap = force_sql_heatmap
        if not use_sql_heatmap and not cache_is_live(self.ds.cache):
            use_sql_heatmap = worker._should_use_sql_heatmap()

        if use_sql_heatmap:
            complete = False
        else:
            complete = load_param_data_from_db_prep(self.ds.cache, self.param)
            worker.read_data = not complete

        if status_message is not None:
            message = status_message
        elif complete:
            message = f"Processing cached data for {self.param.name}..."
        else:
            message = f"Loading data for {self.param.name}..."
        self.show_status(message, 0)
        self.show_plot_state(message, kind="loading")
        worker.started_at = perf_counter()

        # Callback
        worker.emitter.finished.connect(
            lambda finished, worker=worker: self.refreshPlot(finished, worker=worker)
            )
        # Error event handling
        worker.emitter.errorOccurred.connect(
            lambda err, worker=worker: self.err_raiser(err, worker=worker)
            )
        worker.emitter.printer.connect(
            lambda message, worker=worker: self.worker_printer(
                message,
                worker=worker,
                )
            )

        if wait_on_thread:  # Force freeze main thread
            hold_up = QtCore.QEventLoop()
            self.end_wait.connect(hold_up.quit)  # Release main thread event

        # Run worker
        self.worker = worker
        self.threadPool.start(worker)

        if wait_on_thread:
            hold_up.exec()  # The actual place the code waits for self.end_wait.emit
            self.end_wait.disconnect(hold_up.quit)


    @QtCore.pyqtSlot()
    def refreshWindow(self, force: bool = False) -> None:
        """
        Event handler for monitor timeout and other refresh sources.

        Check whether refresh should be done and attempts to refresh plot.

        Parameters
        ----------
        force : bool, optional
            Forces a refresh regarless of checks. The default is False.

        """
        self.monitor.stop()
        retry = False
        skipped_busy_worker = False
        current_ds_len = self.ds.number_of_results

        try:
            # Plot has started, worker first defined in initFrame
            if not hasattr(self, "worker"):
                self.initFrame()  # defined in children classes
                retry = True
                return

            # Check if new data has been added to the dataset
            if current_ds_len != self.last_ds_len or force:
                if self.worker.running:  # No need to run if already updating
                    if not force:
                        skipped_busy_worker = True
                        return

                # The actual refresh line
                self.load_data()

        finally:  # Ran after return or otherwise

            # number_of_results Uses SQL check so can be used regardless of loader progress
            if not skipped_busy_worker:
                self.last_ds_len = current_ds_len

            # restart monitor
            if self.ds.running or retry:
                self.monitorIntervalChanged(self.spinBox.value())

            # restard monitor if any subplots are live
            elif hasattr(self, "lines") and self.lines:
                for subplot in list(self.lines.values())[1:]:
                    if subplot.running:
                        self.monitorIntervalChanged(self.spinBox.value())
                        break


    @QtCore.pyqtSlot(bool)
    def refreshPlot(
            self,
            finished: bool = True,
            worker: Any | None = None,
            ) -> bool | None:
        """
        Produces a shallow copy of data produced by worker.
        This is inhertited by plot<1/2>d to actually use the loaded data.

        Parameters
        ----------
        finished : bool
            In the event the worker had to abort, finished is False and refresh
            is not ran.

        """
        if worker is None:
            worker = self.worker

        if worker is not self.worker:
            worker.running = False
            return False

        try:
            if not finished:  # error in worker
                worker.running = False
                self.show_plot_state(
                    "Plot load failed",
                    "Check the status bar or diagnostic log for details.",
                    kind="error",
                    )
                return False

            # Update qcodes dataset variables if db read happened
            if worker.read_data:
                cache = self.ds.cache
                name = self.param.name

                update_cache_parameter_data(
                    cache,
                    name,
                    worker.updated_read_status,
                    worker.updated_write_status,
                    worker.cache_data,
                    )

                if not cache_has_no_written_data(cache):
                    self._live = False

            # set data to be called by plot<1/2>d.refreshPlot()
            self.axis_data = {
                "x": worker.axis_data["x"],
                "y": worker.axis_data["y"],
                }
            self.axis_param = {
                "x": worker.axis_param["x"],
                "y": worker.axis_param["y"],
                }

            # For 2d plots
            if hasattr(worker, "dataGrid"):
                self.dataGrid = worker.dataGrid

            # I didnt want to make this a dedicated callback for the few times
            # it is used, as the performace hit is neglible
            # Update text
            self._set_param_axis_labels()
            elapsed = perf_counter() - worker.started_at

            if getattr(worker, "loaded_from_sql_heatmap", False):
                loaded_points = getattr(worker, "loaded_point_count", None)
                if getattr(worker, "aggregated_heatmap_source", False):
                    point_text = (
                        f"{loaded_points:,} aggregated cells"
                        if loaded_points is not None
                        else "aggregated cells"
                        )
                else:
                    point_text = (
                        f"{loaded_points:,} sampled points"
                        if loaded_points is not None
                        else "sampled points"
                        )
                self.show_status(
                    f"Loaded {point_text} for {self.param.name} "
                    f"in {elapsed:.2f} seconds",
                    5000,
                    )
            else:
                self.show_status(
                    f"Loaded {self.ds.number_of_results:,} points for {self.param.name} "
                    f"in {elapsed:.2f} seconds",
                    5000,
                    )
            self.hide_plot_state()
            return True

        except AttributeError as err:
            # If worker starts too quickly, overwrites data and spits out error.
            # This should no longer be possible so making error soft error.
            self.show_status(f"Refresh skipped: {err}", 10_000)
            self.show_plot_state("Refresh skipped", str(err), kind="error")
            return None

        finally:  # Allow code to move on from wait_on_thread
            if worker is self.worker:
                self.end_wait.emit()


    @QtCore.pyqtSlot(Exception)
    def err_raiser(self, err: Exception, worker: Any | None = None) -> None:
        if worker is not None and worker is not self.worker:
            return

        message = f"{type(err).__name__}: {err}"
        log_exception("Plot worker error", err, __name__)
        self.show_status(f"Worker error: {message}", 10_000)
        self.show_plot_state("Plot load failed", message, kind="error")

        if message == self._last_error_text:
            return

        self._last_error_text = message
        self.show_error("Plot Error", "A plot worker failed.", message)


    @QtCore.pyqtSlot(str)
    def worker_printer(self, fstr: str, worker: Any | None = None) -> None:
        if worker is not None and worker is not self.worker:
            return

        # Worker print() often does not work, so done through event handlers
        self.show_status(fstr, 5000)
        print(fstr)
