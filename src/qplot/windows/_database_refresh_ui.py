"""Qt adapter for database refresh ownership and view publication."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from PyQt6 import QtCore

from qplot.datahandling.database import DatabaseRefreshWorker
from qplot.datahandling.trusted_live_service import TRUSTED_LIVE_MODE

from ._database_refresh_controller import RefreshSession, RunMetadata, RunStatuses

if TYPE_CHECKING:
    from .main import MainWindow


class DatabaseRefreshUi:
    def __init__(self, window: MainWindow) -> None:
        self.window = window

    def create_worker(self, session: RefreshSession) -> DatabaseRefreshWorker | None:
        from ._database_actions import DatabaseActionsMixin

        window = self.window
        watched_pairs = []
        watched_guids = []
        item_run_id = getattr(window.RunList, "_item_run_id", None)
        for run in list(window.RunList.watching):
            guid = getattr(run, "guid", None)
            if not guid:
                continue
            run_id = item_run_id(run) if callable(item_run_id) else None
            if run_id is None:
                continue
            try:
                run_id = int(run_id)
            except (TypeError, ValueError):
                continue
            watched_pairs.append((run_id, str(guid)))
            watched_guids.append(str(guid))

        watched_by_run_id = dict(watched_pairs)
        watched_runs = []
        scheduled_run_ids = set()

        def schedule_watched(run_id, guid, category):
            try:
                run_id = int(run_id)
            except (TypeError, ValueError):
                return
            if run_id in scheduled_run_ids or not guid:
                return
            scheduled_run_ids.add(run_id)
            watched_runs.append((run_id, str(guid), category))

        selected_guid = getattr(window, "_selected_run_guid", None)
        if selected_guid:
            run_id_for_guid = getattr(window.RunList, "run_id_for_guid", None)
            selected_run_id = (
                run_id_for_guid(selected_guid)
                if callable(run_id_for_guid)
                else None
            )
            if selected_run_id is not None:
                schedule_watched(selected_run_id, selected_guid, "selected")

        visible_run_ids = getattr(window.RunList, "visible_run_ids", None)
        if callable(visible_run_ids):
            for run_id in visible_run_ids():
                try:
                    numeric_run_id = int(run_id)
                except (TypeError, ValueError):
                    continue
                guid = watched_by_run_id.get(numeric_run_id)
                if guid is not None:
                    schedule_watched(numeric_run_id, guid, "visible")

        for run_id, guid in watched_pairs:
            schedule_watched(run_id, guid, "remaining")

        refresh_kwargs: dict[str, Any] = {
            "expected_database_instance": session.instance,
        }
        trusted_service = DatabaseActionsMixin._active_trusted_service_for_instance(
            window,
            session.instance,
        )
        if (
                getattr(window, "_database_access_mode", None) == TRUSTED_LIVE_MODE
                and trusted_service is None
                ):
            window.show_error(
                "Trusted Refresh Unavailable",
                "The accepted trusted live-reader session is no longer usable.",
                "Reload the database to start a new trusted session. qPlot did "
                "not fall back to a snapshot after the accepted-session failure.",
            )
            return None
        if trusted_service is not None:
            refresh_kwargs["trusted_service"] = trusted_service
            refresh_kwargs["derived_coordinator_owned"] = bool(
                getattr(window, "_trusted_derived_bridge", None)
            )
        worker = DatabaseRefreshWorker(
            session.generation,
            session.path,
            window.RunList.maxRunId,
            watched_runs if trusted_service is not None else watched_guids,
            require_publication_ack=trusted_service is not None,
            **refresh_kwargs,
        )

        return worker

    def connect_worker(self, worker: DatabaseRefreshWorker) -> None:
        window = self.window
        ready = getattr(worker.signals, "new_runs_ready", None)
        if ready is not None:
            ready.connect(lambda generation, path, runs:
                          window.database_refresh_new_runs_ready(generation, path, runs, worker))
        # Preserve the QObject receiver so Qt disconnects it on window deletion.
        worker.signals.finished.connect(window.database_refresh_finished)

    def submit_worker(self, worker: DatabaseRefreshWorker) -> None:
        self.window.databaseRefreshThreadPool.start(worker)

    def schedule(self, callback: Callable[[], None]) -> None:
        # A plain singleShot closure can outlive the QObject it captures.
        # Parenting the timer makes window destruction discard the callback.
        timer = QtCore.QTimer(self.window)
        timer.setSingleShot(True)
        timer.timeout.connect(timer.deleteLater)
        timer.timeout.connect(callback)
        timer.start(0)

    def show_status(self, message: str, timeout: int) -> None:
        self.window.show_status(message, timeout)

    def failed(self, error: Exception) -> None:
        self.window.show_error("Refresh Failed", "Could not refresh the run list.", str(error))

    def can_refresh(self, path: str) -> bool:
        from ._database_actions import _database_paths_equal

        window = self.window
        return bool(
            _database_paths_equal(path, window.fileTextbox.text())
            and not getattr(window, "_shutdown_started", False)
            and not getattr(window, "_shutdown_ready", False)
        )

    def refresh(self) -> None:
        self.window.refreshMain()

    def reload(self, path: str) -> None:
        self.window._reload_replaced_database(path)

    def begin_publication(self) -> str:
        from ._database_actions import DatabaseActionsMixin

        selected = str(getattr(self.window, "_selected_run_guid", "") or "")
        DatabaseActionsMixin._sync_database_generation_controls(self.window)
        return selected

    def end_publication(self, selected_guid: str) -> None:
        from ._database_actions import DatabaseActionsMixin

        DatabaseActionsMixin._sync_database_generation_controls(self.window)
        DatabaseActionsMixin._reconcile_refresh_selection(self.window, selected_guid)

    def publish_page(
        self, session: RefreshSession, runs: RunMetadata, is_current: Callable[[], bool],
    ) -> None:
        from ._database_actions import DatabaseActionsMixin

        DatabaseActionsMixin._publish_database_refresh_page(self.window, session, runs, is_current)

    def finish(
        self, session: RefreshSession, runs: RunMetadata, statuses: RunStatuses,
        error: object, is_current: Callable[[], bool],
    ) -> None:
        from ._database_actions import DatabaseActionsMixin

        DatabaseActionsMixin._complete_database_refresh(
            self.window, session, runs, statuses, error, is_current,
        )
