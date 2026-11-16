"""Own refresh workers, coalesced requests and bounded page publication."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

from qplot.datahandling.file_identity import DatabaseInstance, logical_database_path
from qplot.datahandling.readonly import DatabaseInstanceChangedError
from qplot.diagnostics import log_exception

if TYPE_CHECKING:
    from qplot.datahandling.database import DatabaseRefreshWorker

RunMetadata = dict[int, dict[str, Any]]
RunStatuses = dict[str, dict[str, Any]]


class RefreshPublicationAborted(RuntimeError):
    """A callback no longer owns the refresh being published."""


@dataclass
class RefreshSession:
    generation: int
    path: str
    instance: DatabaseInstance | None
    worker: DatabaseRefreshWorker | None = None
    pending: bool = False
    staged_runs: RunMetadata = field(default_factory=dict)
    deferred_completion: Callable[[], None] | None = None
    publication_error: Exception | None = None


class DatabaseRefreshHost(Protocol):
    def create_worker(self, session: RefreshSession) -> DatabaseRefreshWorker | None: ...
    def connect_worker(self, worker: DatabaseRefreshWorker) -> None: ...
    def submit_worker(self, worker: DatabaseRefreshWorker) -> None: ...
    def show_status(self, message: str, timeout: int) -> None: ...
    def failed(self, error: Exception) -> None: ...
    def can_refresh(self, path: str) -> bool: ...
    def refresh(self) -> None: ...
    def reload(self, path: str) -> None: ...
    def begin_publication(self) -> str: ...
    def end_publication(self, selected_guid: str) -> None: ...
    def publish_page(
        self, session: RefreshSession, runs: RunMetadata, is_current: Callable[[], bool],
    ) -> None: ...
    def finish(
        self, session: RefreshSession, runs: RunMetadata, statuses: RunStatuses,
        error: object, is_current: Callable[[], bool],
    ) -> None: ...


class DatabaseRefreshController:
    def __init__(
        self, host: DatabaseRefreshHost, *, schedule: Callable[[Callable[[], None]], None],
    ) -> None:
        self.host = host
        self.schedule = schedule
        self._generation = 0
        self._session: RefreshSession | None = None
        self._publication_active = False
        self._finishing_session: RefreshSession | None = None

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def session(self) -> RefreshSession | None:
        return self._session

    @property
    def active(self) -> bool:
        return self.session is not None

    @property
    def worker(self) -> DatabaseRefreshWorker | None:
        return self.session.worker if self.session else None

    @property
    def instance(self) -> DatabaseInstance | None:
        return self.session.instance if self.session else None

    @property
    def pending(self) -> bool:
        return self.session.pending if self.session else False

    @property
    def staged_runs(self) -> Mapping[int, dict[str, Any]]:
        return MappingProxyType(self.session.staged_runs if self.session else {})

    @property
    def publication_active(self) -> bool:
        return self._publication_active

    def _current(self, session: RefreshSession) -> bool:
        return self.session is session and self.generation == session.generation

    def _callback_session(
        self, generation: int, path: str, worker: DatabaseRefreshWorker | None,
    ) -> RefreshSession | None:
        session = self.session
        if session is None or generation != self.generation:
            return None
        if worker is not None and worker is not session.worker:
            return None
        if logical_database_path(path) != logical_database_path(session.path):
            return None
        return session

    @staticmethod
    def _cancel(worker: DatabaseRefreshWorker | None) -> None:
        if worker is not None:
            try:
                worker.cancel()
            except Exception as error:
                log_exception("Database refresh cancellation failed", error, __name__)

    def cancel_worker(self) -> None:
        if self.worker is not None:
            self.worker.cancel()

    def invalidate(self, *, cancel: bool = True) -> None:
        worker = self.worker
        self._generation += 1
        self._session = None
        if cancel:
            self._cancel(worker)

    def start(self, path: str, instance: DatabaseInstance) -> bool:
        if self.session is not None:
            self.session.pending = True
            self.host.show_status("Database refresh queued.", 3000)
            return False
        if self.publication_active:
            return False
        self._generation += 1
        session = RefreshSession(self.generation, path, instance)
        self._session = session
        try:
            self.host.show_status("Checking for new runs...", 0)
            if not self._current(session):
                return False
            worker = self.host.create_worker(session)
            session.worker = worker
            if not self._current(session):
                self._cancel(worker)
                return False
            if worker is None:
                self.invalidate()
                return False
            self.host.connect_worker(worker)
            if not self._current(session):
                self._cancel(worker)
                return False
            self.host.submit_worker(worker)
        except Exception as error:
            current = self._current(session)
            if current:
                self.invalidate()
            else:
                self._cancel(session.worker)
            if current:
                log_exception("Database refresh startup failed", error, __name__)
                self.host.failed(error)
            return False
        return True

    def publish_page(
        self, generation: int, path: str, runs: RunMetadata | None,
        worker: DatabaseRefreshWorker | None = None,
    ) -> None:
        session = self._callback_session(generation, path, worker)
        if session is None or self.publication_active or self._finishing_session is session:
            if worker is not None:
                worker.reject_new_runs_publication(RefreshPublicationAborted(
                    "The trusted refresh page no longer owns UI publication.",
                ))
            return
        self._publication_active = True
        previous = dict(session.staged_runs)
        selected_guid = ""
        error = None
        try:
            selected_guid = self.host.begin_publication()
            session.staged_runs.update(runs or {})
            self.host.publish_page(
                session, runs or {},
                lambda: self._current(session) and self.host.can_refresh(path),
            )
        except Exception as caught:
            error = caught
            session.publication_error = caught
            session.staged_runs.clear()
            session.staged_runs.update(previous)
            log_exception("Trusted refresh basic-row publication failed", caught, __name__)
        finally:
            self._publication_active = False
            try:
                self.host.end_publication(selected_guid)
            except Exception as repair_error:
                log_exception("Refresh selection reconciliation failed", repair_error, __name__)
            finally:
                if worker is not None:
                    if error is None:
                        worker.acknowledge_new_runs_published()
                    else:
                        worker.reject_new_runs_publication(error)
            if session.deferred_completion is not None:
                self.schedule(session.deferred_completion)
                session.deferred_completion = None
        if isinstance(error, DatabaseInstanceChangedError) and self._current(session):
            self.invalidate()
            self.host.reload(path)

    def finish(
        self, generation: int, path: str, runs: RunMetadata | None,
        statuses: RunStatuses | None, error: object,
        worker: DatabaseRefreshWorker | None = None,
    ) -> None:
        session = self._callback_session(generation, path, worker)
        if session is None or self._finishing_session is session:
            return
        if self.publication_active:
            # A nested Qt event must not finalise a partially published page.
            if session.deferred_completion is None:
                session.deferred_completion = lambda: self.finish(
                    generation, path, runs, statuses, error, worker,
                )
            return
        previous_finishing = self._finishing_session
        self._finishing_session = session
        try:
            effective_runs = {**session.staged_runs, **(runs or {})}
            self.host.finish(
                session, effective_runs, statuses or {}, session.publication_error or error,
                lambda: self._current(session),
            )
        except Exception as caught:
            log_exception("Database refresh completion failed", caught, __name__)
            self.host.failed(caught)
        finally:
            self._finishing_session = previous_finishing
            # Nested events may have cancelled this session or installed another.
            if self._current(session):
                self._session = None
                if session.pending:
                    self.schedule(lambda: self._retry_if_current(session))

    def _retry_if_current(self, session: RefreshSession) -> None:
        if (self.generation == session.generation and not self.active
                and not self.publication_active and self.host.can_refresh(session.path)):
            self.host.refresh()
