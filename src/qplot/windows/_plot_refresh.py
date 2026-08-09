from time import perf_counter
from typing import TYPE_CHECKING, Any

from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from qplot.datahandling.file_identity import database_file_identity
from qplot.datahandling.qcodes_cache import (
    cache_has_no_written_data,
    cache_is_live,
    cache_parameter_is_synchronized,
    set_cache_dataset_completed,
    set_cache_parameter_synchronized,
    update_cache_parameter_data,
)
from qplot.datahandling.readonly import quarantine_wal_for_replaced_database
from qplot.diagnostics import log_exception
from qplot.tools import loader
from qplot.tools.operation_registry import OperationValidationError

if TYPE_CHECKING:
    class _PlotRefreshBase(qtw.QMainWindow):
        _last_error_text: str | None
        _live: bool
        axis_data: dict[str, Any]
        axis_param: dict[str, Any]
        config: Any
        dataGrid: Any
        display_param: Any
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


def plot_refresh_required(source: Any) -> bool:
    """Return whether a source plot still needs live/final refresh work."""

    predicate = getattr(source, "_refresh_monitor_required", None)
    if callable(predicate):
        return bool(predicate())

    dataset = getattr(source, "ds", None)
    return bool(getattr(dataset, "running", False))


class PlotRefreshMixin(_PlotRefreshBase):
    """
    Worker-backed plot refresh orchestration shared by plot windows.

    """

    def _can_process_refresh(self) -> bool:
        """Return whether this window still has an active dataset owner."""

        if not self.__dict__.get("_closed", False):
            return True

        if self.__dict__.get("_merged_trace_users", 0) <= 0:
            return False

        dataset_holder = self.__dict__.get("_dataset_holder")
        dataset_key = self.__dict__.get("_dataset_key")
        if not isinstance(dataset_holder, dict) or dataset_key is None:
            return False

        handle = dataset_holder.get(dataset_key)
        return handle is not None and getattr(handle, "users", 0) > 0


    def _source_database_is_current(self) -> bool:
        """Stop this plot before a replaced main file can be read as its source."""

        dataset_key = self.__dict__.get("_dataset_key")
        expected_identity = getattr(dataset_key, "database_identity", None)
        database_path = getattr(dataset_key, "database_path", None)
        if expected_identity is None or not database_path:
            return True
        if database_file_identity(database_path) == expected_identity:
            return True

        quarantine_wal_for_replaced_database(database_path)
        worker = self.__dict__.get("worker")
        if worker is not None and getattr(worker, "running", False):
            cancel = getattr(worker, "cancel", None)
            if callable(cancel):
                cancel()
        monitor = self.__dict__.get("monitor")
        if monitor is not None:
            monitor.stop()
        self.show_status("Database was replaced; reloading source data.", 5000)
        self.show_plot_state(
            "Database replaced",
            "qPlot is reloading the replacement before this plot can refresh.",
            kind="info",
        )
        replacement_signal = getattr(self, "database_replaced", None)
        emit = getattr(replacement_signal, "emit", None)
        if callable(emit):
            emit(str(database_path))
        return False

    def _refresh_monitor_required(self, dataset: Any | None = None) -> bool:
        """Keep polling until this plot has committed its terminal display."""

        if dataset is None:
            dataset = self.ds
        if getattr(dataset, "running", False):
            return True

        state = self.__dict__
        if not state.get("_qplot_display_synchronized", True):
            return True

        # A direct-SQL heatmap deliberately bypasses the QCoDeS cache. Its
        # successful display commit is therefore the terminal per-plot state.
        if state.get("_qplot_display_uses_direct_sql", False):
            return False

        cache = getattr(dataset, "cache", None)
        param = state.get("param")
        if cache is None or param is None:
            return False
        return not cache_parameter_is_synchronized(cache, param.name)

    def _sync_dataset_completion(self, dataset: Any) -> None:
        """Schedule this plot's outstanding final worker read."""

        if not self._refresh_monitor_required(dataset):
            return

        worker = getattr(self, "worker", None)
        if worker is not None and getattr(worker, "running", False):
            self._queue_pending_refresh()
            return

        self.load_data()

    def _ensure_refresh_monitor(self) -> None:
        """Restart polling when a terminal worker/display remains pending."""

        if not self._can_process_refresh():
            return
        try:
            dataset = self.ds
        except (AttributeError, KeyError, RuntimeError):
            return
        if not self._refresh_monitor_required(dataset):
            return

        state = self.__dict__
        monitor = state.get("monitor")
        spin_box = state.get("spinBox")
        if monitor is None or spin_box is None:
            return
        is_active = getattr(monitor, "isActive", None)
        if callable(is_active) and is_active():
            return
        self.monitorIntervalChanged(spin_box.value())

    def _mark_display_synchronized(self, worker: Any) -> bool:
        """Publish terminal plot state after the concrete display commit."""

        if worker is None or worker is not self.__dict__.get("worker"):
            return False
        if getattr(worker, "is_cancelled", lambda: False)():
            return False
        if not getattr(worker, "_qplot_display_commit_ready", False):
            return False
        if getattr(worker, "dataset_completed", None) is not True:
            return False

        loaded_from_sql = bool(
            getattr(worker, "loaded_from_sql_heatmap", False)
            )
        if not loaded_from_sql and not cache_parameter_is_synchronized(
                self.ds.cache,
                self.param.name,
                ):
            return False

        self._qplot_display_synchronized = True
        self._qplot_display_uses_direct_sql = loaded_from_sql
        worker._qplot_display_commit_ready = False
        return True

    def load_data(
            self,
            wait_on_thread: bool = False,
            *,
            force_sql_heatmap: bool = False,
            heatmap_axis_ranges: dict[str, tuple[float, float]] | None = None,
            heatmap_full_axis_ranges: dict[str, tuple[float, float]] | None = None,
            status_message: str | None = None,
            ) -> bool:
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
        if not self._can_process_refresh():
            return False
        if not self._source_database_is_current():
            return False

        try:
            operations = self.oper_widget.get_data()
        except OperationValidationError as error:
            message = str(error)
            self.show_status(message, 10_000)
            self.show_plot_state(
                "Operations not applied",
                message,
                kind="error",
                )
            return False

        loader_kwargs = {
            "read_data": True,
            "operations": operations,
            "force_sql_heatmap": force_sql_heatmap,
            "max_full_heatmap_points": self.config.get(
                "runtime_settings.max_full_heatmap_points"
                ),
            "heatmap_axis_ranges": heatmap_axis_ranges,
            "heatmap_full_axis_ranges": heatmap_full_axis_ranges,
        }
        database_identity = getattr(
            getattr(self, "_dataset_key", None),
            "database_identity",
            None,
        )
        if database_identity is not None:
            loader_kwargs["database_identity"] = database_identity

        worker: Any = loader(
            self.ds.cache,
            self.param,
            self.param_dict,
            self.axis_options,
            **loader_kwargs,
            )
        worker.dataset_length_at_start = self.ds.number_of_results

        if status_message is not None:
            message = status_message
        else:
            message = f"Loading data for {self.param.name}..."
        self.show_status(message, 0)
        self.show_plot_state(message, kind="loading")
        worker.started_at = perf_counter()

        # Callback
        worker.emitter.finished.connect(
            lambda finished, worker=worker: self.refreshPlot(finished, worker=worker)
            )
        worker_registry = getattr(self.threadPool, "_qplot_workers", None)
        if worker_registry is not None:
            worker_registry.add(worker)
            worker.emitter.finished.connect(
                lambda _finished, worker=worker, registry=worker_registry: (
                    registry.discard(worker)
                    )
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

        if not getattr(self.ds, "running", False):
            # A terminal reload supersedes the previous displayed snapshot.
            # Keep it retryable until this worker's concrete display commit.
            self._qplot_display_synchronized = False

        # Run worker
        self.worker = worker
        self.threadPool.start(worker)

        if wait_on_thread:
            hold_up.exec()  # The actual place the code waits for self.end_wait.emit
            self.end_wait.disconnect(hold_up.quit)

        return True


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
        if not self._can_process_refresh():
            return
        if not self._source_database_is_current():
            return

        retry = False
        dataset = None

        try:
            dataset = self.ds
            current_ds_len = dataset.number_of_results

            # Plot has started, worker first defined in initFrame
            if not hasattr(self, "worker"):
                self.initFrame()  # defined in children classes
                retry = True
                return

            # Check if new data has been added to the dataset
            if current_ds_len != self.last_ds_len or force:
                if self.worker.running:  # No need to run if already updating
                    self._queue_pending_refresh(force=force)
                    if force:
                        self.show_status("Refresh queued after the current load.", 3000)
                    return

                # The actual refresh line
                self.load_data()

            elif self._refresh_monitor_required(dataset):
                # Source completion is global, but this parameter/cache/display
                # may still need its own successful terminal refresh.
                self._sync_dataset_completion(dataset)

        except Exception:
            retry = True
            raise

        finally:  # Ran after return or otherwise

            # restart monitor
            if retry or (
                    dataset is not None
                    and self._refresh_monitor_required(dataset)
                    ):
                self.monitorIntervalChanged(self.spinBox.value())

            # restard monitor if any subplots are live
            elif dataset is not None:
                lines = self.__dict__.get("lines")
                if isinstance(lines, dict) and lines:
                    main_line = self.__dict__.get("line")
                    for subplot in lines.values():
                        if subplot is main_line:
                            continue
                        source = getattr(subplot, "from_win", None)
                        running = (
                            plot_refresh_required(source)
                            if source is not None
                            else bool(getattr(subplot, "running", False))
                            )
                        subplot.running = bool(running)
                        if running:
                            self.monitorIntervalChanged(self.spinBox.value())
                            break


    def _schedule_pending_refresh(self) -> None:
        state = self.__dict__
        if not state.get("_refresh_pending", False):
            return
        if state.get("_refresh_pending_scheduled", False):
            return

        self._refresh_pending_scheduled = True
        QtCore.QTimer.singleShot(0, self._run_pending_refresh)


    def _queue_pending_refresh(self, *, force: bool = False) -> None:
        """Coalesce refreshes while retaining explicit-force provenance."""

        state = self.__dict__
        if not state.get("_refresh_pending", False):
            self._refresh_pending_force = False
        self._refresh_pending = True
        if force:
            self._refresh_pending_force = True


    def _run_pending_refresh(self) -> None:
        self._refresh_pending_scheduled = False
        if not self.__dict__.get("_refresh_pending", False):
            return
        if not self._can_process_refresh():
            self._refresh_pending = False
            self._refresh_pending_force = False
            return
        if getattr(getattr(self, "worker", None), "running", False):
            self._schedule_pending_refresh()
            return

        force = bool(self.__dict__.get("_refresh_pending_force", False))
        self._refresh_pending = False
        self._refresh_pending_force = False
        if not force and not self._refresh_monitor_required():
            return
        self.refreshWindow(force=force)


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
            worker = self.__dict__.get("worker")

        if worker is None:
            return False

        current_worker = self.__dict__.get("worker")
        if worker is not current_worker:
            worker.running = False
            return False
        worker = current_worker

        was_cancelled = bool(
            getattr(worker, "is_cancelled", lambda: False)()
            )

        if not self._can_process_refresh():
            worker.running = False
            self.end_wait.emit()
            return False
        if not self._source_database_is_current():
            # A worker can finish from a private snapshot after the source main
            # file was atomically replaced.  Do not let that stale result reach
            # the shared cache or the concrete display callback.
            worker.running = False
            self.end_wait.emit()
            return False

        try:
            worker._qplot_display_commit_ready = False
            if was_cancelled:
                worker.running = False
                return False

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

                cache_updated = update_cache_parameter_data(
                    cache,
                    name,
                    worker.updated_read_status,
                    worker.updated_write_status,
                    worker.cache_data,
                    dataset_completed=getattr(
                        worker,
                        "dataset_completed",
                        None,
                        ),
                    )
                if not cache_updated:
                    self._queue_pending_refresh()
                    self.show_status(
                        "A newer refresh finished first; synchronising this plot...",
                        5000,
                        )
                    return False

                if not cache_has_no_written_data(cache):
                    self._live = False

            if (
                    getattr(worker, "dataset_completed", None) is True
                    and cache_is_live(self.ds.cache)
                    ):
                # An in-memory live cache is already authoritative; no database
                # cache commit is needed before its terminal display refresh.
                set_cache_parameter_synchronized(
                    self.ds.cache,
                    self.param.name,
                    True,
                    )

            # set data to be called by plot<1/2>d.refreshPlot()
            self.axis_data = {
                "x": worker.axis_data["x"],
                "y": worker.axis_data["y"],
                }
            self.axis_param = {
                "x": worker.axis_param["x"],
                "y": worker.axis_param["y"],
                }
            self.display_param = getattr(worker, "display_param", self.param)

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
            dataset_length = getattr(worker, "dataset_length_at_start", None)
            if dataset_length is None:
                dataset_length = self.ds.number_of_results
            self.last_ds_len = dataset_length
            self.hide_plot_state()
            if (
                    getattr(worker, "loaded_from_sql_heatmap", False)
                    and getattr(worker, "dataset_completed", None) is True
                ):
                # Direct SQL heatmaps intentionally bypass the QCoDeS cache.
                # Publish the global source observation here; the concrete
                # heatmap publishes its per-plot state only after rendering.
                set_cache_dataset_completed(self.ds.cache, True)
            worker._qplot_display_commit_ready = bool(
                getattr(worker, "dataset_completed", None) is True
                and (
                    getattr(worker, "loaded_from_sql_heatmap", False)
                    or cache_parameter_is_synchronized(
                        self.ds.cache,
                        self.param.name,
                        )
                    )
                )
            return True

        except AttributeError as err:
            # If worker starts too quickly, overwrites data and spits out error.
            # This should no longer be possible so making error soft error.
            self.show_status(f"Refresh skipped: {err}", 10_000)
            self.show_plot_state("Refresh skipped", str(err), kind="error")
            return None

        finally:  # Allow code to move on from wait_on_thread
            if worker is self.worker:
                worker.running = False
                self.end_wait.emit()
                if not getattr(worker, "_qplot_display_commit_ready", False):
                    self._ensure_refresh_monitor()
                self._schedule_pending_refresh()


    @QtCore.pyqtSlot(Exception)
    def err_raiser(self, err: Exception, worker: Any | None = None) -> None:
        if (
                worker is not None
                and getattr(worker, "is_cancelled", lambda: False)()
                ):
            worker.running = False
            return

        if not self._can_process_refresh():
            if worker is not None:
                worker.running = False
            return

        if worker is not None and worker is not self.worker:
            return

        if worker is not None and getattr(worker, "database_replaced", False):
            self._source_database_is_current()
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
        if not self._can_process_refresh():
            return

        if worker is not None and worker is not self.worker:
            return

        # Worker print() often does not work, so done through event handlers
        self.show_status(fstr, 5000)
        print(fstr)
