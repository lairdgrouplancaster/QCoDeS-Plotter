from collections.abc import Callable, Iterator
from contextlib import contextmanager
from time import perf_counter
from typing import TYPE_CHECKING, Any

from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

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
from qplot.tools.operation_registry import OperationValidationError
from qplot.tools.plot_result import PlotLoadResult

from ._complex_plots import configured_complex_mode, sync_complex_axes
from ._dataset_handle import DatasetKey, dataset_key_matches_current_source
from ._plot_host import PlotHost
from ._plot_publication import PlotPublicationOwner, RefreshPublication
from ._plot_refresh_controller import PlotRefreshController, PlotRefreshRequest
from ._refresh_interval import refresh_interval_value

if TYPE_CHECKING:
    class _PlotRefreshBase(PlotHost, qtw.QMainWindow):
        _last_error_text: str | None
        _requested_complex_mode: Any
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

        @property
        def axis_options(self) -> dict[str, str]: ...

        def _set_param_axis_labels(self) -> None: ...
        def monitorIntervalChanged(self, interval: float) -> None: ...
else:
    class _PlotRefreshBase(PlotHost):
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

    def _publication_owner(self) -> PlotPublicationOwner:
        owner = self.__dict__.get("_plot_publication_owner")
        if owner is None:
            owner = PlotPublicationOwner()
            self._plot_publication_owner = owner
        return owner

    @property
    def _refresh_publication(self) -> RefreshPublication | None:
        return self._publication_owner().current

    @property
    def _qplot_publication_generation(self) -> int:
        return self._publication_owner().generation

    def _refresh_controller(self) -> PlotRefreshController:
        controller = self.__dict__.get("_plot_refresh_controller")
        if controller is None:
            from ._plot_refresh_ui import PlotRefreshUi

            controller = PlotRefreshController(PlotRefreshUi(self))
            self._plot_refresh_controller = controller
        return controller

    @property
    def worker(self) -> Any:
        worker = self._refresh_controller().worker
        if worker is None:
            raise AttributeError("This plot has no refresh worker.")
        return worker

    @property
    def _refresh_pending(self) -> bool:
        return self._refresh_controller().pending is not None

    @property
    def _refresh_pending_force(self) -> bool:
        return self._refresh_controller().pending is True

    @property
    def _refresh_pending_scheduled(self) -> bool:
        return self._refresh_controller().scheduled

    def _cancel_plot_refresh(self) -> None:
        monitor = self.__dict__.get("monitor")
        if monitor is not None:
            monitor.stop()
        worker = getattr(self, "worker", None)
        self._refresh_controller().cancel()
        if worker is not None:
            self._rollback_refresh_publication(worker)


    def _refresh_publication_for(self, worker: Any) -> RefreshPublication | None:
        return self._publication_owner().for_worker(worker)

    def _discard_refresh_publication(self, worker: Any) -> None:
        self._publication_owner().discard(worker)

    def _can_process_refresh(self) -> bool:
        """Return whether this window still has an active dataset owner."""

        if not self.__dict__.get("_plot_setup_complete", True):
            return False

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


    def _source_database_matches_key(self) -> bool:
        """Return whether the retained key still exactly names its source."""
        dataset_key = self.__dict__.get("_dataset_key")
        if not isinstance(dataset_key, DatasetKey):
            return True
        database_path = getattr(dataset_key, "database_path", None)
        if not database_path:
            return True
        return dataset_key_matches_current_source(dataset_key)


    def _source_database_is_current(self) -> bool:
        """Stop before a replaced main, WAL, or SHM can back this plot."""

        dataset_key = self.__dict__.get("_dataset_key")
        database_path = getattr(dataset_key, "database_path", None)
        if self._source_database_matches_key():
            return True

        quarantine_wal_for_replaced_database(database_path)
        self._cancel_plot_refresh()
        monitor = self.__dict__.get("monitor")
        if monitor is not None:
            monitor.stop()
        self.show_status(
            "Database or SQLite sidecar was replaced; reloading source data.",
            5000,
        )
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


    def _begin_refresh_publication(self, worker: Any) -> None:
        self._publication_owner().begin(
            worker, self.__dict__, cache=getattr(self.ds, "cache", None),
            parameter_name=self.param.name,
            extra_fields=tuple(getattr(self, "_extra_publication_fields", ())),
        )
        worker._qplot_source_rejected = False

    def _record_refresh_cache_publication(self, worker: Any) -> None:
        self._publication_owner().record_cache(worker)

    def _rollback_refresh_publication(self, worker: Any) -> None:
        if self._refresh_publication_for(worker) is None:
            return
        self._publication_owner().rollback(worker, getattr(self, "worker", None), self.__dict__)
        worker._qplot_display_commit_ready = False
        worker._qplot_source_rejected = True

    def _refresh_publication_source_is_current(
            self,
            worker: Any,
            *,
            clear_display=None,
            ) -> bool:
        """Guard one cache/display publication boundary and fail closed."""

        if (not self._refresh_controller().current_worker(worker)
                or getattr(worker, "is_cancelled", lambda: False)()):
            worker._qplot_display_commit_ready = False
            self._rollback_refresh_publication(worker)
            if worker is getattr(self, "worker", None) and callable(clear_display):
                clear_display()
            return False
        publication = self._refresh_publication_for(worker)
        if publication is not None:
            if not self._publication_owner().owns(publication, getattr(self, "worker", None)):
                # A nested same-source callback already owns the plot.  The
                # older callback must stop harmlessly: rollback or renderer
                # clearing here would corrupt the newer accepted publication.
                worker._qplot_display_commit_ready = False
                self._discard_refresh_publication(worker)
                return False
        elif worker is not getattr(self, "worker", None):
            worker._qplot_display_commit_ready = False
            return False

        if self._source_database_matches_key():
            return True
        self._rollback_refresh_publication(worker)
        if callable(clear_display):
            clear_display()
        self._source_database_is_current()
        return False


    def _commit_refresh_publication(
            self, worker: Any, *, preview_ready: bool = False,
            clear_display: Callable[[], None] | None = None,
            ) -> None:
        """Release rollback state after the concrete display committed."""

        publication = self._refresh_publication_for(worker)
        if publication is None or publication.completing:
            return
        if not self._refresh_publication_source_is_current(worker, clear_display=clear_display):
            return
        publication.completing = True
        self._complete_plot_publication(worker)
        if not self._refresh_publication_source_is_current(worker, clear_display=clear_display):
            return
        if not self._publication_owner().commit(publication, getattr(self, "worker", None)):
            return
        overlay = self.__dict__.get("plot_state_overlay")
        if overlay is not None:
            overlay.finish(worker)
        publish_preview = self.__dict__.get("_plot_preview_sink")
        result = getattr(worker, "result", None)
        if (
            preview_ready
            and self._publication_owner().generation == publication.generation
            and self._refresh_controller().current_worker(worker)
            and result is not None
            and callable(publish_preview)
            and not getattr(worker, "operations", None)
            and getattr(worker, "heatmap_axis_ranges", None) is None
            and not getattr(worker, "is_cancelled", lambda: False)()
            and self._source_database_matches_key()
        ):
            try:
                publish_preview(self, result)
            except Exception as error:
                # A supplementary image must never turn a successful plot
                # publication into a plot load failure.
                log_exception("Plot preview reuse failed", error, __name__)

    def _publication_step(self, worker: Any, callback: Callable[[], Any]) -> bool:
        """Fence a dependent-view callback that can replace the current job."""
        if not self._refresh_publication_source_is_current(worker):
            return False
        callback()
        return self._refresh_publication_source_is_current(worker)

    def _complete_plot_publication(self, worker: Any) -> None:
        """Renderer-specific dependent views finish before the publication commits."""

    @contextmanager
    def _render_publication(self, worker: Any, clear_display: Callable[[], None]) -> Iterator[None]:
        """Separate accepted display outcomes from interrupted rendering.

        A normal early return may display an empty or unsupported-data message.
        An exception must not commit that partial renderer. Source rejection
        keeps its stronger cache rollback and replacement-recovery behaviour.
        """
        try:
            yield
            if self._refresh_publication_for(worker) is not None:
                if self._refresh_publication_source_is_current(worker, clear_display=clear_display):
                    self._commit_refresh_publication(worker, clear_display=clear_display)
        except Exception:
            if self._refresh_publication_source_is_current(worker, clear_display=clear_display):
                if self._publication_owner().render_failed(worker, getattr(self, "worker", None), self.__dict__):
                    worker._qplot_display_commit_ready = False
                    clear_display()
            raise
        finally:
            overlay = self.__dict__.get("plot_state_overlay")
            if overlay is not None:
                overlay.finish(worker)
            worker.running = False
            if worker is getattr(self, "worker", None):
                self._ensure_refresh_monitor()

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

        if self._refresh_controller().busy:
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
        self.monitorIntervalChanged(refresh_interval_value(spin_box))

    def _mark_display_synchronized(self, worker: Any) -> bool:
        """Publish terminal plot state after the concrete display commit."""

        if worker is None or worker is not getattr(self, "worker", None):
            return False
        if getattr(worker, "is_cancelled", lambda: False)():
            return False
        if not self._refresh_publication_source_is_current(worker):
            return False
        if not getattr(worker, "_qplot_display_commit_ready", False):
            return False
        result = getattr(worker, "result", None)
        if result is None or result.dataset_completed is not True:
            return False

        loaded_from_sql = result.loaded_from_sql_heatmap
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

        controller = self._refresh_controller()
        request = PlotRefreshRequest(
            operations, force_sql_heatmap, heatmap_axis_ranges,
            heatmap_full_axis_ranges, status_message,
        )
        # Connect before submission, and do not enter a nested loop when a
        # synchronous completion or failed startup already settled the job.
        hold_up = QtCore.QEventLoop() if wait_on_thread else None
        if hold_up is not None:
            self.end_wait.connect(hold_up.quit)
        try:
            launched = controller.start(request)
            session = controller.session
            if (hold_up is not None and launched and session is not None
                    and controller.accepts(session)):
                hold_up.exec()
            return launched
        finally:
            if hold_up is not None:
                self.end_wait.disconnect(hold_up.quit)


    @QtCore.pyqtSlot()
    def refresh_plot_data(self) -> None:
        """Manually reload each source whose data is shown in this plot."""
        if not self._can_process_refresh():
            return
        sources = [self]
        seen = {id(self)}
        for registry_name in ("lines", "heatmaps"):
            for item in self.__dict__.get(registry_name, {}).values():
                source = getattr(item, "from_win", None)
                if source is not None and id(source) not in seen:
                    sources.append(source)
                    seen.add(id(source))
        # A source contributes only its own primary data to an overlay.
        # Refreshing its overlays recursively would do unrelated work and
        # could cycle when two visible plots contain each other's traces.
        for source in sources:
            source.refreshWindow(force=True)


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

            if self._refresh_controller().busy:
                self._queue_pending_refresh(force=force)
                if force:
                    self.show_status("Refresh queued after the current load.", 3000)
                return

            # Retry the initial read without rebuilding existing renderers.
            if not hasattr(self, "worker"):
                self.load_data()
                retry = True
                return

            # Check if new data has been added to the dataset
            if current_ds_len != self.last_ds_len or force:
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
                self.monitorIntervalChanged(refresh_interval_value(self.spinBox))

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
                            self.monitorIntervalChanged(refresh_interval_value(self.spinBox))
                            break


    def _schedule_pending_refresh(self) -> None:
        self._refresh_controller().schedule_pending()

    def _queue_pending_refresh(self, *, force: bool = False) -> None:
        self._refresh_controller().queue(force=force)

    def _run_pending_refresh(self) -> None:
        self._refresh_controller().run_pending()


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
            worker = getattr(self, "worker", None)

        if worker is None:
            return False

        current_worker = getattr(self, "worker", None)
        if worker is not current_worker:
            worker.running = False
            return False
        worker = current_worker

        was_cancelled = bool(
            getattr(worker, "is_cancelled", lambda: False)()
            )

        if not self._can_process_refresh():
            worker.running = False
            if not self._refresh_controller().publishing(worker):
                self.end_wait.emit()
            return False
        if not self._source_database_is_current():
            # A worker can finish from a private snapshot after the source main
            # file was atomically replaced.  Do not let that stale result reach
            # the shared cache or the concrete display callback.
            worker.running = False
            if not self._refresh_controller().publishing(worker):
                self.end_wait.emit()
            return False

        try:
            worker._qplot_display_commit_ready = False
            if was_cancelled:
                worker.running = False
                self.show_plot_state("Plot load cancelled")
                return False

            if not finished:  # error in worker
                worker.running = False
                self.show_plot_state(
                    "Plot load failed",
                    "Check the diagnostic log for details.",
                    kind="error",
                    )
                return False

            result: PlotLoadResult | None = worker.result
            if result is None:
                raise AttributeError("The completed plot worker has no result.")
            if (result.complex_samples is not None
                    and "_frozen_result" not in self.__dict__
                    and worker.complex_mode != configured_complex_mode(self)):
                self._queue_pending_refresh(force=True)
                return False
            self._begin_refresh_publication(worker)
            prepare_publication = getattr(self, "_prepare_plot_publication", None)
            if callable(prepare_publication):
                prepare_publication(worker, result)
                if not self._refresh_publication_source_is_current(worker):
                    return False
            # Update qcodes dataset variables if db read happened
            cache_update = result.cache_update
            if cache_update is not None:
                cache = self.ds.cache
                name = self.param.name

                cache_updated = update_cache_parameter_data(
                    cache,
                    name,
                    cache_update.read_status,
                    cache_update.write_status,
                    cache_update.data,
                    dataset_completed=result.dataset_completed,
                    )
                if not cache_updated:
                    self._discard_refresh_publication(worker)
                    self._queue_pending_refresh()
                    self.show_status(
                        "A newer refresh finished first; synchronising this plot...",
                        5000,
                        )
                    return False

                self._record_refresh_cache_publication(worker)
                if not self._refresh_publication_source_is_current(worker):
                    return False

                if not cache_has_no_written_data(cache):
                    self._live = False

            if (
                    result.dataset_completed is True
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
                "x": result.axis_data["x"],
                "y": result.axis_data["y"],
                }
            self.axis_param = {
                "x": result.axis_param["x"],
                "y": result.axis_param["y"],
                }
            self.display_param = result.display_param if result.display_param is not None else self.param
            self.complex_samples = result.complex_samples

            # For 2d plots
            if result.dataGrid is not None:
                self.dataGrid = result.dataGrid

            # I didnt want to make this a dedicated callback for the few times
            # it is used, as the performace hit is neglible
            # Update text
            self._set_param_axis_labels()
            if not self._refresh_publication_source_is_current(worker):
                return False
            update_coordinate_context = getattr(
                self,
                "_update_coordinate_context",
                None,
                )
            if callable(update_coordinate_context):
                update_coordinate_context()
                if not self._refresh_publication_source_is_current(worker):
                    return False
            sync_complex_axes(self)
            if not self._refresh_publication_source_is_current(worker):
                return False
            elapsed = perf_counter() - worker.started_at

            if result.loaded_from_sql_heatmap:
                loaded_points = result.loaded_point_count
                if result.aggregated_heatmap_source:
                    point_text = (
                        f"{loaded_points:,} aggregated cells"
                        if loaded_points is not None
                        else "aggregated cells"
                        )
                else:
                    sampled = result.sampled_heatmap_source or bool(
                        (result.heatmap_downsample_info or {}).get("source_sampled")
                    )
                    point_kind = "sampled points" if sampled else "points"
                    point_text = (
                        f"{loaded_points:,} {point_kind}"
                        if loaded_points is not None
                        else point_kind
                        )
                self.show_status(
                    f"Loaded {point_text} in {elapsed:.2f} s",
                    5000,
                    )
            else:
                loaded_points = result.loaded_point_count
                if loaded_points is None:
                    loaded_points = result.dataset_length_at_start
                if loaded_points is None:
                    loaded_points = self.ds.number_of_results
                self.show_status(
                    f"Loaded {loaded_points:,} points in {elapsed:.2f} s",
                    5000,
                    )
            if not self._refresh_publication_source_is_current(worker):
                return False
            dataset_length = result.dataset_length_at_start
            if dataset_length is None:
                dataset_length = self.ds.number_of_results
            self.last_ds_len = dataset_length
            from qplot.datahandling.trusted_plot import TrustedPlotDataset
            if isinstance(self.ds, TrustedPlotDataset):
                self.ds.number_of_results = max(self.ds.number_of_results, dataset_length)
            overlay = self.__dict__.get("plot_state_overlay")
            if overlay is not None:
                overlay.rendering(worker)
            else:
                self.hide_plot_state()
            if not self._refresh_publication_source_is_current(worker):
                return False
            if (
                    result.loaded_from_sql_heatmap
                    and result.dataset_completed is True
                ):
                # Direct SQL heatmaps intentionally bypass the QCoDeS cache.
                # Publish the global source observation here; the concrete
                # heatmap publishes its per-plot state only after rendering.
                set_cache_dataset_completed(self.ds.cache, True)
            worker._qplot_display_commit_ready = bool(
                result.dataset_completed is True
                and (
                    result.loaded_from_sql_heatmap
                    or cache_parameter_is_synchronized(
                        self.ds.cache,
                        self.param.name,
                        )
                    )
                )
            self._record_refresh_cache_publication(worker)
            if not self._refresh_publication_source_is_current(worker):
                return False
            return True

        except AttributeError as err:
            # If worker starts too quickly, overwrites data and spits out error.
            # This should no longer be possible so making error soft error.
            # Keep the publication snapshot until the accepted source and this
            # worker's ownership have both been revalidated.  In particular,
            # an AttributeError after cache/axis mutation must not turn a
            # simultaneous database replacement into a committed stale view.
            if not self._refresh_publication_source_is_current(worker):
                return False
            self.show_status(f"Refresh skipped: {err}", 10_000)
            self.show_plot_state("Refresh skipped", str(err), kind="error")
            if not self._refresh_publication_source_is_current(worker):
                return False
            self._discard_refresh_publication(worker)
            return None

        except Exception:
            # The concrete 1D/2D callback is not entered when the base
            # publication raises.  Roll back an owned partial publication here;
            # a source mismatch performs the same rollback and recovery inside
            # the guard, while lost ownership leaves the newer callback alone.
            if self._refresh_publication_for(worker) is not None:
                if self._refresh_publication_source_is_current(worker):
                    self._rollback_refresh_publication(worker)
            if worker is getattr(self, "worker", None):
                self.show_plot_state("Plot display failed", kind="error")
            raise

        finally:  # Allow code to move on from wait_on_thread
            if (worker is not None and worker is getattr(self, "worker", None)
                    and not self._refresh_controller().publishing(worker)):
                worker.running = False
                self.end_wait.emit()
                if (
                        not getattr(worker, "_qplot_display_commit_ready", False)
                        and not getattr(worker, "_qplot_source_rejected", False)
                        ):
                    self._ensure_refresh_monitor()
                if not getattr(worker, "_qplot_source_rejected", False):
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
