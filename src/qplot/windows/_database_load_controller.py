"""Own pending database loads independently of the main window's widgets.

Only this controller advances load generations, owns pending workers/readers and
decides whether callbacks or queued retries may publish. The host performs GUI
updates and the existing transactional publication of an accepted result.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

from qplot.datahandling.file_identity import DatabaseInstance
from qplot.datahandling.readonly import (
    DatabaseInstanceChangedError,
    UnverifiableDatabaseWalError,
)
from qplot.datahandling.trusted_live_service import (
    SNAPSHOT_FALLBACK_MODE,
    TRUSTED_LIVE_MODE,
)
from qplot.diagnostics import log_exception

from ._database_load_state import DatabaseLoadSession
from ._database_observation import database_instances_or_sidecars_differ

if TYPE_CHECKING:
    from qplot.datahandling.database import DatabaseLoadWorker
    from qplot.datahandling.trusted_live_service import TrustedLiveReadService


@dataclass(frozen=True)
class AcceptedDatabaseLoad:
    generation: int
    session: DatabaseLoadSession
    instance: DatabaseInstance
    runs: dict[int, dict[str, Any]]
    service: TrustedLiveReadService | None
    access_mode: str
    fallback_reason: str | None


class DatabaseLoadHost(Protocol):
    """The GUI and resource operations needed by a pending load."""

    def create_worker(self, generation: int, session: DatabaseLoadSession) -> DatabaseLoadWorker: ...
    def connect_worker(self, worker: DatabaseLoadWorker) -> None: ...
    def submit_worker(self, worker: DatabaseLoadWorker) -> None: ...
    def is_committed_service(self, service: TrustedLiveReadService) -> bool: ...
    def retire_service(self, service: TrustedLiveReadService, *, force: bool = False) -> None: ...
    def show_loading(self, message: str) -> None: ...
    def hide_loading(self) -> None: ...
    def show_progress(self, message: str) -> None: ...
    def show_status(self, message: str, timeout: int) -> None: ...
    def load_failed(self, title: str, message: str, details: str) -> None: ...
    def load_cancelled(self) -> None: ...
    def publish(self, result: AcceptedDatabaseLoad) -> None: ...
    def can_retry(self) -> bool: ...
    def reload(self, session: DatabaseLoadSession) -> bool: ...


class _SupersededLoad(Exception):
    """A host callback cancelled or replaced a load during startup."""


class DatabaseLoadController:
    def __init__(
        self,
        host: DatabaseLoadHost,
        *,
        observe_instance: Callable[[str], DatabaseInstance],
        schedule: Callable[[Callable[[], None]], None],
    ) -> None:
        self.host = host
        self.observe_instance = observe_instance
        self.schedule = schedule
        self._generation = 0
        self._retry_generation: int | None = None
        self._session: DatabaseLoadSession | None = None
        self._publication_active = False
        self._pending_services: dict[int, TrustedLiveReadService] = {}

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def session(self) -> DatabaseLoadSession | None:
        return self._session

    @property
    def active(self) -> bool:
        return self._session is not None

    @property
    def worker(self) -> DatabaseLoadWorker | None:
        return self._session.worker if self._session is not None else None

    @property
    def publication_active(self) -> bool:
        return self._publication_active

    @property
    def pending_services(self) -> Mapping[int, TrustedLiveReadService]:
        return MappingProxyType(self._pending_services)

    def take_pending_services(self) -> tuple[TrustedLiveReadService, ...]:
        """Transfer pending readers to the window's bounded retirement owner."""
        services = tuple(self._pending_services.values())
        self._pending_services.clear()
        return services

    def _retire_pending(self, generation: int, *, force: bool = False) -> None:
        service = self._pending_services.pop(generation, None)
        if service is not None:
            self.host.retire_service(service, force=force)

    def start(self, session: DatabaseLoadSession, message: str) -> bool:
        if self.active or self.publication_active:
            return False
        self._generation += 1
        self._retry_generation = None
        generation = self.generation
        self._session = session
        try:
            self.host.show_loading(message)
            self._require_current(generation, session)
            worker = self.host.create_worker(generation, session)
            session.worker = worker
            service = getattr(worker, "trusted_service", None)
            session.trusted_service = service
            session.owns_trusted_service = bool(
                service is not None and not self.host.is_committed_service(service)
            )
            if service is not None and session.owns_trusted_service:
                self._pending_services[generation] = service
            self._require_current(generation, session)
            self.host.connect_worker(worker)
            self._require_current(generation, session)
            self.host.submit_worker(worker)
        except Exception as error:
            # Fence even a callback queued before connect/start raised. Retain
            # the service through retirement if cancellation itself fails.
            current = generation == self.generation and session is self.session
            if current:
                self.invalidate()
            elif (session.trusted_service is None
                  or not self.host.is_committed_service(session.trusted_service)):
                self._cancel_worker(session.worker)
            self._retire_pending(generation, force=True)
            if not current or isinstance(error, _SupersededLoad):
                return False
            self.host.hide_loading()
            log_exception("Database load startup failed", error, __name__)
            self.host.load_failed(
                "Database Load Failed", f"Could not load database {session.abspath}.", str(error),
            )
            return False
        return True

    def _require_current(self, generation: int, session: DatabaseLoadSession) -> None:
        if generation != self.generation or session is not self.session:
            raise _SupersededLoad

    @staticmethod
    def _cancel_worker(worker: DatabaseLoadWorker | None) -> None:
        if worker is not None:
            try:
                worker.cancel()
            except Exception as error:
                log_exception("Database load cancellation failed", error, __name__)

    def cancel_worker(self) -> None:
        worker = self.worker
        if worker is not None:
            worker.cancel()

    def invalidate(self, *, cancel: bool = True) -> None:
        """Fence callbacks/retries, leaving pending readers reachable for teardown."""
        worker = self.worker
        self._generation += 1
        self._retry_generation = None
        self._session = None
        if cancel:
            self._cancel_worker(worker)

    def cancel(self) -> None:
        active = self.active
        if not active and not self.publication_active and self._retry_generation is None:
            self.host.hide_loading()
            return
        generation = self.generation
        # Cancellation also fences a scheduled retry when no worker is active.
        self.invalidate()
        self._retire_pending(generation)
        self.host.hide_loading()
        if active:
            self.host.load_cancelled()

    def progress(self, generation: int, message: str) -> None:
        if generation == self.generation and self.active:
            self.host.show_progress(message)

    def finish(
        self,
        generation: int,
        abspath: str,
        runs: dict[int, dict[str, Any]] | None,
        error: object,
        worker: DatabaseLoadWorker | None = None,
    ) -> None:
        session = self.session
        if generation != self.generation:
            self._retire_pending(generation)
            return
        if session is None:
            # A duplicate completion can arrive through a nested Qt event while
            # publication still owns this generation's reader.
            return
        # An unrelated callback must not consume the current load's reader.
        if session.abspath != abspath or (worker is not None and worker is not session.worker):
            return
        worker = session.worker
        service = self._pending_services.get(generation, session.trusted_service)
        if service is not None and session.owns_trusted_service:
            self._pending_services[generation] = service
        access_mode = getattr(worker, "access_mode", None) or SNAPSHOT_FALLBACK_MODE
        fallback_reason = getattr(worker, "fallback_reason", None)
        self._session = None
        try:
            self.host.hide_loading()
            if generation != self.generation:
                self._retire_pending(generation)
                return
            instance = self.observe_instance(abspath)
            if generation != self.generation:
                self._retire_pending(generation)
                return
            if not self._accept(generation, session, instance, service, access_mode, error):
                return
            self.host.publish(AcceptedDatabaseLoad(
                generation, session, instance, runs or {}, service, access_mode, fallback_reason,
            ))
        except Exception as publication_error:
            self._retire_pending(generation, force=True)
            log_exception("Database load completion failed", publication_error, __name__)
            self.host.load_failed(
                "Database Load Failed", f"Could not publish database {abspath}.",
                str(publication_error),
            )

    def _accept(
        self,
        generation: int,
        session: DatabaseLoadSession,
        instance: DatabaseInstance,
        service: TrustedLiveReadService | None,
        access_mode: str,
        error: object,
    ) -> bool:
        if (isinstance(error, DatabaseInstanceChangedError)
                or database_instances_or_sidecars_differ(session.load_instance, instance)):
            self._retire_pending(generation)
            self.queue_retry(session)
            return False

        failure = None
        if isinstance(error, UnverifiableDatabaseWalError):
            failure = (
                "Unverifiable Database WAL",
                "qPlot refused to load this database because its WAL "
                "cannot be verified. Close every owning QCoDeS/SQLite "
                "connection cleanly, or checkpoint with the owning writer, "
                "then retry loading the database.",
                str(error),
            )
        elif error is not None:
            failure = (
                "Database Load Failed", f"Could not load database {session.abspath}.", str(error),
            )
        elif access_mode == TRUSTED_LIVE_MODE:
            if (service is None or not service.accepted
                    or database_instances_or_sidecars_differ(service.database_instance, instance)):
                failure = (
                    "Trusted Database Load Failed",
                    "The trusted reader did not remain bound to the accepted database instance.",
                    "The pending session was retired without changing the currently displayed database.",
                )
        elif access_mode != SNAPSHOT_FALLBACK_MODE:
            failure = (
                "Database Load Failed", "The database worker returned an unknown access mode.",
                str(access_mode),
            )
        if failure is None:
            return True
        self._retire_pending(generation)
        if error is not None:
            log_exception("Database load failed", error, __name__)
        self.host.load_failed(*failure)
        return False

    def queue_retry(
        self, session: DatabaseLoadSession, *,
        message: str = "Database changed while loading; retrying...",
    ) -> None:
        self._generation += 1
        generation = self.generation
        self._retry_generation = generation
        self.host.show_status(message, 0)
        self.schedule(lambda: self.retry_if_current(generation, session))

    def retry_if_current(self, generation: int, session: DatabaseLoadSession) -> None:
        if generation == self._retry_generation:
            self._retry_generation = None
        if generation == self.generation and self.host.can_retry():
            self.host.reload(session)

    @contextmanager
    def publication(
        self, result: AcceptedDatabaseLoad, *, committed: Callable[[], bool],
    ) -> Iterator[bool]:
        """Keep the pending reader visible to shutdown until publication unwinds."""
        if result.generation != self.generation:
            self._retire_pending(result.generation, force=True)
            yield False
            return
        if result.service is not None and result.session.owns_trusted_service:
            self._pending_services[result.generation] = result.service
        self._publication_active = True
        try:
            yield True
        finally:
            service = self._pending_services.pop(result.generation, None)
            try:
                if not committed() and service is not None:
                    self.host.retire_service(service, force=True)
            finally:
                self._publication_active = False
