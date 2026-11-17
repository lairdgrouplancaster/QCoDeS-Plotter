"""Own plot jobs and queued refreshes independently of Qt and rendering."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from qplot.diagnostics import log_exception


@dataclass(frozen=True)
class PlotRefreshRequest:
    operations: Any
    force_sql_heatmap: bool = False
    heatmap_axis_ranges: dict[str, tuple[float, float]] | None = None
    heatmap_full_axis_ranges: dict[str, tuple[float, float]] | None = None
    status_message: str | None = None


@dataclass(eq=False)
class PlotRefreshSession:
    generation: int
    worker: Any = None
    active: bool = True
    starting: bool = True
    publishing: bool = False
    completion: bool | None = None


class PlotRefreshHost(Protocol):
    def create_worker(self, request: PlotRefreshRequest) -> Any: ...
    def prepare_worker(self, worker: Any) -> None: ...
    def connect_worker(self, session: PlotRefreshSession) -> None: ...
    def show_loading(self, session: PlotRefreshSession, request: PlotRefreshRequest) -> None: ...
    def submit_worker(self, worker: Any) -> None: ...
    def release_worker(self, worker: Any) -> None: ...
    def can_refresh(self) -> bool: ...
    def refresh_required(self) -> bool: ...
    def refresh(self, force: bool) -> None: ...
    def schedule(self, callback: Callable[[], None]) -> None: ...
    def publish(self, worker: Any, finished: bool) -> None: ...
    def failed(self, error: Exception, *, startup: bool) -> None: ...
    def settled(self, session: PlotRefreshSession) -> None: ...
    def cancelled(self) -> None: ...
    def status(self, worker: Any, message: str) -> None: ...
    def error(self, worker: Any, error: Exception) -> None: ...


class PlotRefreshController:
    def __init__(self, host: PlotRefreshHost) -> None:
        self.host = host
        self.generation = 0
        self.session: PlotRefreshSession | None = None
        # None: no request; False: automatic; True: explicitly forced.
        self.pending: bool | None = None
        self._scheduled: object | None = None

    @property
    def worker(self) -> Any:
        return self.session.worker if self.session is not None else None

    @property
    def busy(self) -> bool:
        session = self.session
        return bool(session is not None and session.active and (
            session.starting or session.publishing or getattr(session.worker, "running", True)
        ))

    def current_worker(self, worker: Any) -> bool:
        return self.session is not None and self.owns(self.session) and self.session.worker is worker

    @property
    def scheduled(self) -> bool:
        return self._scheduled is not None

    def owns(self, session: PlotRefreshSession) -> bool:
        return self.session is session and self.generation == session.generation

    def accepts(self, session: PlotRefreshSession) -> bool:
        return self.owns(session) and session.active

    def publishing(self, worker: Any) -> bool:
        session = self.session
        return bool(session is not None and session.worker is worker and session.publishing)

    @staticmethod
    def _cancel(worker: Any) -> None:
        cancel = getattr(worker, "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except Exception as error:
                log_exception("Plot worker cancellation failed", error, __name__)

    def _release(self, worker: Any) -> None:
        try:
            self.host.release_worker(worker)
        except Exception as error:
            log_exception("Plot worker registration cleanup failed", error, __name__)

    def _abandon(self, session: PlotRefreshSession) -> None:
        session.active = False
        session.starting = False
        self._cancel(session.worker)
        if session.worker is not None:
            session.worker.running = False
        self._release(session.worker)

    def _failed(self, error: Exception, *, startup: bool) -> None:
        try:
            self.host.failed(error, startup=startup)
        except Exception as feedback_error:
            log_exception("Plot failure feedback failed", feedback_error, __name__)

    def _settled(self, session: PlotRefreshSession) -> None:
        try:
            self.host.settled(session)
        except Exception as error:
            log_exception("Plot completion feedback failed", error, __name__)

    def start(self, request: PlotRefreshRequest) -> bool:
        previous = self.session
        self.generation += 1
        session = PlotRefreshSession(self.generation)
        self.session = session
        self._scheduled = None
        if previous is not None and previous.active:
            previous.active = False
            self._cancel(previous.worker)
        try:
            if not self.accepts(session):
                return False
            worker = self.host.create_worker(request)
            session.worker = worker
            if not self.accepts(session):
                self._abandon(session)
                return False
            self.host.prepare_worker(worker)
            if self.accepts(session):
                self.host.connect_worker(session)
            if self.accepts(session):
                self.host.show_loading(session, request)
            if not self.accepts(session):
                self._abandon(session)
                return False
            self.host.submit_worker(worker)
        except Exception as error:
            self._abandon(session)
            if self.owns(session):
                self._failed(error, startup=True)
                if self.owns(session):
                    self._settled(session)
                    if self.owns(session):
                        self.schedule_pending()
            return False
        session.starting = False
        if not self.accepts(session):
            return False
        if session.completion is not None:
            self.finish(session, session.completion)
        return True

    def cancel(self) -> None:
        session = self.session
        was_active = session is not None and session.active
        self.generation += 1
        generation = self.generation
        self.pending = None
        self._scheduled = None
        if session is not None and was_active:
            session.active = False
            self._cancel(session.worker)
            # Keep the last worker available to display/export observations.
            # Its eventual completion releases the pool registry entry.
            if self.generation == generation:
                self.host.cancelled()

    def forget_worker(self) -> None:
        session = self.session
        self.cancel()
        if self.session is session:
            self.session = None

    def request_cancel(self, worker: Any) -> None:
        """Stop a read while retaining its terminal callback and queued refresh."""
        self._cancel(worker)

    def finish(self, session: PlotRefreshSession, finished: bool) -> None:
        if not self.accepts(session):
            self._release(session.worker)
            return
        if session.starting:
            if session.completion is None:
                session.completion = finished
            return
        if session.publishing:
            return
        session.publishing = True
        try:
            if self.host.can_refresh() and self.accepts(session):
                self.host.publish(session.worker, finished)
        except Exception as error:
            if self.accepts(session):
                self._failed(error, startup=False)
        finally:
            session.publishing = False
            session.active = False
            session.worker.running = False
            self._release(session.worker)
            if self.owns(session):
                self._settled(session)
                if self.owns(session):
                    self.schedule_pending()

    def status(self, session: PlotRefreshSession, message: str) -> None:
        if self.accepts(session):
            self.host.status(session.worker, message)

    def error(self, session: PlotRefreshSession, error: Exception) -> None:
        if self.accepts(session):
            self.host.error(session.worker, error)

    def queue(self, *, force: bool = False) -> None:
        self.pending = bool(self.pending or force)

    def restore_pending(self, pending: bool, force: bool = False) -> None:
        self.pending = force if pending else None
        self._scheduled = None

    def discard_automatic(self) -> None:
        if self.pending is False:
            self.restore_pending(False)

    def schedule_pending(self) -> None:
        if self.pending is None or self._scheduled is not None:
            return
        token = object()
        self._scheduled = token
        generation = self.generation

        def run() -> None:
            if self._scheduled is token and generation == self.generation:
                self._scheduled = None
                self.run_pending()

        try:
            self.host.schedule(run)
        except Exception:
            if self._scheduled is token:
                self._scheduled = None
            raise

    def run_pending(self) -> None:
        if self.pending is None:
            return
        generation = self.generation
        allowed = self.host.can_refresh()
        if generation != self.generation:
            return
        if not allowed:
            self.pending = None
            return
        if self.busy:
            # Completion schedules the retained request; never spin a zero timer.
            return
        force = self.pending
        self.pending = None
        required = force or self.host.refresh_required()
        if required and generation == self.generation:
            self.host.refresh(force)
