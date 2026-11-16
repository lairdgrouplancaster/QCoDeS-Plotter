"""Adapt the database load controller to Qt and the existing view transaction."""

from __future__ import annotations

from typing import TYPE_CHECKING

from qplot.datahandling.database import DatabaseLoadWorker

from ._database_load_controller import AcceptedDatabaseLoad
from ._database_load_publication import DatabaseLoadPublication
from ._database_load_state import DatabaseLoadSession

if TYPE_CHECKING:
    from qplot.datahandling.trusted_live_service import TrustedLiveReadService

    from .main import MainWindow


class DatabaseLoadUi:
    """All widget access and Qt signal connections stay on the GUI side."""

    def __init__(self, window: MainWindow) -> None:
        self.window = window

    def create_worker(self, generation: int, session: DatabaseLoadSession) -> DatabaseLoadWorker:
        # Bootstrap needs an isolated pending reader, never the committed one.
        return DatabaseLoadWorker(
            generation, session.abspath,
            self.window.config.get("runtime_settings.cloud_sync_timeout"),
            expected_database_instance=session.load_instance,
        )

    def connect_worker(self, worker: DatabaseLoadWorker) -> None:
        # Connect on the GUI thread and retain the window's Qt slot receivers.
        # Do not run controller/publication callbacks directly in a worker.
        worker.signals.status.connect(self.window.database_load_status)
        worker.signals.finished.connect(
            lambda generation, path, runs, error: self.window.database_load_finished(
                generation, path, runs, error, worker,
            )
        )

    def submit_worker(self, worker: DatabaseLoadWorker) -> None:
        self.window.databaseLoadThreadPool.start(worker)

    def is_committed_service(self, service: TrustedLiveReadService) -> bool:
        return service is getattr(self.window, "_trusted_read_service", None)

    def retire_service(self, service: TrustedLiveReadService, *, force: bool = False) -> None:
        self.window._retire_trusted_read_service(service, force=force)

    def show_loading(self, message: str) -> None:
        self._apply_refresh_interval()
        self.window._set_database_load_controls_enabled(False)
        self.window._show_database_load_panel(message)

    def hide_loading(self) -> None:
        self.window._set_database_load_controls_enabled(True)
        self.window._hide_database_load_panel()

    def show_progress(self, message: str) -> None:
        self.window._show_database_load_panel(message)

    def show_status(self, message: str, timeout: int) -> None:
        self.window.show_status(message, timeout)

    def _resume(self) -> None:
        self.window._resume_test_database_generation_recovery()
        self._apply_refresh_interval()

    def _apply_refresh_interval(self) -> None:
        apply_interval = getattr(self.window, "_apply_refresh_interval", None)
        if callable(apply_interval):
            apply_interval(self.window._main_refresh_interval())

    def load_failed(self, title: str, message: str, details: str) -> None:
        self.window.show_error(title, message, details)
        self._resume()

    def load_cancelled(self) -> None:
        self.window.show_status("Database load cancelled.", 3000)
        self._resume()

    def publish(self, result: AcceptedDatabaseLoad) -> None:
        publication = DatabaseLoadPublication(
            self.window, result,
            observe_instance=self.window._database_loader.observe_instance,
        )
        publication.run()
        if self.window._recover_database_load_publication(publication):
            self.window._complete_database_load_publication(publication)

    def can_retry(self) -> bool:
        return not (
            getattr(self.window, "_shutdown_started", False)
            or getattr(self.window, "_shutdown_ready", False)
        )

    def reload(self, session: DatabaseLoadSession) -> bool:
        return self.window._reload_replaced_database(
            session.abspath, generation_recovery=session.generation_recovery,
            load_started_at=session.load_started_at,
        )
