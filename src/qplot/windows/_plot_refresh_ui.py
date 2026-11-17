"""Qt connections and presentation for the plot-refresh controller."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from time import perf_counter
from typing import TYPE_CHECKING, Any

from PyQt6 import QtCore

from qplot.diagnostics import log_exception
from qplot.tools import loader

from ._complex_plots import configured_complex_mode
from ._plot_refresh_controller import PlotRefreshRequest, PlotRefreshSession

if TYPE_CHECKING:
    from ._plot_refresh import PlotRefreshMixin


class _WorkerCallbacks(QtCore.QObject):
    """Keep callbacks on the window's thread, including trusted-reader completion."""

    def __init__(self, ui: PlotRefreshUi, session: PlotRefreshSession) -> None:
        super().__init__(ui.window)
        self.ui = ui
        self.session = session

    @QtCore.pyqtSlot(bool)
    def finished(self, finished: bool) -> None:
        with self.ui.worker_callback():
            self.ui.window._refresh_controller().finish(self.session, finished)

    @QtCore.pyqtSlot(Exception)
    def error(self, error: Exception) -> None:
        with self.ui.worker_callback():
            self.ui.window._refresh_controller().error(self.session, error)

    @QtCore.pyqtSlot(str)
    def status(self, message: str) -> None:
        with self.ui.worker_callback():
            self.ui.window._refresh_controller().status(self.session, message)


class PlotRefreshUi:
    def __init__(self, window: PlotRefreshMixin) -> None:
        self.window = window
        self._callbacks: dict[int, _WorkerCallbacks] = {}
        self._callback_depth = 0

    @property
    def has_pending_workers(self) -> bool:
        return bool(self._callbacks or self._callback_depth)

    @contextmanager
    def worker_callback(self) -> Iterator[None]:
        # Completion releases the worker before emitting feedback. A nested
        # event loop in that feedback must not delete the window mid-callback.
        self._callback_depth += 1
        try:
            yield
        finally:
            self._callback_depth -= 1

    def create_worker(self, request: PlotRefreshRequest) -> Any:
        window = self.window
        kwargs = {
            "read_data": True,
            "operations": request.operations,
            "force_sql_heatmap": request.force_sql_heatmap,
            "max_full_heatmap_points": window.config.get("runtime_settings.max_full_heatmap_points"),
            "max_heatmap_grid_cells": window.config.get("runtime_settings.max_heatmap_grid_cells"),
            # Custom factories still receive the established cell-budget keyword.
            "max_heatmap_grid_side": window.config.get("runtime_settings.max_heatmap_grid_cells"),
            "heatmap_axis_ranges": request.heatmap_axis_ranges,
            "heatmap_full_axis_ranges": request.heatmap_full_axis_ranges,
            "complex_mode": configured_complex_mode(window),
        }
        window._requested_complex_mode = kwargs["complex_mode"]
        identity = getattr(getattr(window, "_dataset_key", None), "database_identity", None)
        if identity is not None:
            kwargs["database_identity"] = identity
        factory = getattr(window, "_create_plot_loader", loader)
        return factory(window.ds.cache, window.param, window.param_dict, window.axis_options, **kwargs)

    def prepare_worker(self, worker: Any) -> None:
        window = self.window
        worker._qplot_operation_apply_generation = int(getattr(window.oper_widget, "_apply_generation", 0))
        key = getattr(window, "_dataset_key", None)
        if key is not None:
            worker.expected_database_path = key.database_path
            worker.expected_resolved_database_path = key.resolved_database_path
            worker.expected_sidecar_identities = key.sidecar_identities
            worker.sidecar_identities = key.sidecar_identities
        worker.dataset_length_at_start = window.ds.number_of_results
        worker.started_at = perf_counter()

    def connect_worker(self, session: PlotRefreshSession) -> None:
        worker = session.worker
        callbacks = _WorkerCallbacks(self, session)
        self._callbacks[id(worker)] = callbacks
        worker.emitter.finished.connect(callbacks.finished)
        worker.emitter.errorOccurred.connect(callbacks.error)
        worker.emitter.printer.connect(callbacks.status)
        registry = getattr(getattr(self.window, "threadPool", None), "_qplot_workers", None)
        if registry is not None:
            registry.add(worker)
            # Pool ownership outlives the window. This cleanup deliberately
            # captures no widget, so QObject destruction cannot strand a job.
            worker.emitter.finished.connect(
                lambda _finished, worker=worker, registry=registry: registry.discard(worker)
            )

    def show_loading(self, session: PlotRefreshSession, request: PlotRefreshRequest) -> None:
        window = self.window
        controller = window._refresh_controller()
        worker = session.worker
        message = (request.status_message if request.status_message is not None
                   else f"Loading data for {window.param.name}...")
        window.show_status(message, 0)
        if not controller.accepts(session):
            return
        window.show_plot_state(message, kind="loading")
        if not controller.accepts(session):
            return
        if not getattr(window.ds, "running", False):
            window._qplot_display_synchronized = False
        overlay = window.__dict__.get("plot_state_overlay")
        if overlay is not None:
            overlay.track(worker)
        if not controller.accepts(session):
            return
        will_start = getattr(type(window), "_refresh_worker_will_start", None)
        if callable(will_start):
            will_start(window, worker)

    def submit_worker(self, worker: Any) -> None:
        worker.start(self.window.threadPool)

    def release_worker(self, worker: Any) -> None:
        registry = getattr(getattr(self.window, "threadPool", None), "_qplot_workers", None)
        if registry is not None:
            registry.discard(worker)
        callbacks = self._callbacks.pop(id(worker), None)
        if callbacks is not None:
            callbacks.deleteLater()

    def can_refresh(self) -> bool:
        return self.window._can_process_refresh() and self.window._source_database_is_current()

    def refresh_required(self) -> bool:
        return self.window._refresh_monitor_required()

    def refresh(self, force: bool) -> None:
        self.window.refreshWindow(force=force)

    def schedule(self, callback: Callable[[], None]) -> None:
        timer = QtCore.QTimer(self.window)
        timer.setSingleShot(True)

        def run() -> None:
            timer.deleteLater()
            callback()

        timer.timeout.connect(run)
        timer.start(0)

    def publish(self, worker: Any, finished: bool) -> None:
        self.window.refreshPlot(finished, worker=worker)

    def failed(self, error: Exception, *, startup: bool) -> None:
        message = "Plot could not start" if startup else "Plot display failed"
        log_exception(message, error, __name__)
        window = self.window
        generation = window._refresh_controller().generation
        window.show_status(f"{message}: {error}", 10_000)
        if generation == window._refresh_controller().generation:
            window.show_plot_state(message, str(error), kind="error")

    def settled(self, session: PlotRefreshSession) -> None:
        window = self.window
        controller = window._refresh_controller()
        generation = controller.generation
        window.end_wait.emit()
        if generation == controller.generation and not getattr(session.worker, "_qplot_source_rejected", False):
            window._ensure_refresh_monitor()

    def cancelled(self) -> None:
        signal = getattr(self.window, "end_wait", None)
        if signal is not None:
            signal.emit()

    def status(self, worker: Any, message: str) -> None:
        self.window.worker_printer(message, worker=worker)

    def error(self, worker: Any, error: Exception) -> None:
        self.window.err_raiser(error, worker=worker)
