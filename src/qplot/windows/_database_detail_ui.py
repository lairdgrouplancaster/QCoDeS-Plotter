"""Qt and run-list presentation adapter for snapshot metadata loading."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from qplot.datahandling.database import (
    DatabaseDetailWorker,
    DatabaseExpensiveDetailWorker,
)
from qplot.datahandling.file_identity import DatabaseInstance
from qplot.datahandling.trusted_live_service import TRUSTED_LIVE_MODE

from ._database_detail_controller import RunMetadata
from ._database_detail_state import DetailWorker, set_run_metadata_loading

if TYPE_CHECKING:
    from .main import MainWindow

SnapshotWorker = DatabaseDetailWorker | DatabaseExpensiveDetailWorker


class DatabaseDetailUi:
    def __init__(self, window: MainWindow) -> None:
        self.window = window

    def create_worker(
        self, expensive: bool, generation: int, path: str, run_ids: list[Any],
        worker_kwargs: dict[str, Any],
    ) -> DetailWorker:
        worker_type = DatabaseExpensiveDetailWorker if expensive else DatabaseDetailWorker
        return worker_type(generation, path, run_ids, batch_size=100, **worker_kwargs)

    def connect_worker(self, worker: DetailWorker, expensive: bool) -> None:
        signals = cast(SnapshotWorker, worker).signals
        window = self.window
        # Keep QObject-bound receivers so deletion disconnects queued signals.
        if expensive:
            signals.status.connect(window.database_expensive_detail_status)
            signals.batch_ready.connect(window.database_expensive_detail_batch_ready)
            signals.finished.connect(window.database_expensive_detail_finished)
        else:
            signals.status.connect(window.database_detail_status)
            signals.batch_ready.connect(window.database_detail_batch_ready)
            signals.finished.connect(window.database_detail_finished)

    def submit_worker(self, worker: DetailWorker, expensive: bool) -> None:
        window = self.window
        pool = getattr(window, "databaseDetailThreadPool", window.databaseLoadThreadPool)
        if expensive:
            pool = getattr(window, "databaseExpensiveDetailThreadPool", pool)
        pool.start(cast(SnapshotWorker, worker))

    def priority_run_ids(self, run_ids: Any = None) -> list[Any]:
        priority_ids = []
        seen = set()

        def add(candidate):
            if candidate is None:
                return
            try:
                key = int(candidate)
            except (TypeError, ValueError):
                key = candidate
            if key in seen:
                return
            priority_ids.append(candidate)
            seen.add(key)

        if isinstance(run_ids, (list, tuple, set)):
            for run_id in run_ids:
                add(run_id)
        else:
            add(run_ids)

        run_list = getattr(self.window, "RunList", None)
        selected_run_ids = getattr(run_list, "selected_run_ids", None)
        if callable(selected_run_ids):
            for run_id in selected_run_ids():
                add(run_id)

        visible_run_ids = getattr(run_list, "visible_run_ids", None)
        if callable(visible_run_ids):
            for run_id in visible_run_ids():
                add(run_id)

        return priority_ids


    def set_loading(
        self, columns: tuple[str, ...], *, loading: bool,
        run_ids: list[Any] | None = None, guids: list[Any] | None = None,
    ) -> None:
        set_run_metadata_loading(self.window, guids, columns=columns, run_ids=run_ids, loading=loading)

    def can_publish(self, path: str) -> bool:
        from ._database_actions import DatabaseActionsMixin, _database_paths_equal

        window = self.window
        return bool(
            not getattr(window, "_shutdown_started", False)
            and not getattr(window, "_shutdown_ready", False)
            and getattr(window, "_database_access_mode", None) != TRUSTED_LIVE_MODE
            and _database_paths_equal(path, window.fileTextbox.text())
            and not DatabaseActionsMixin._database_generation_transaction_blocks_path(window, path)
        )

    def source_current(
        self, path: str, instance: DatabaseInstance | None, *, after_publication: bool = False,
    ) -> bool:
        from ._database_actions import DatabaseActionsMixin

        return not DatabaseActionsMixin._reload_if_worker_database_instance_changed(
            self.window, path, instance, discard_stale_publication=after_publication,
        )

    def publish(self, runs: RunMetadata) -> None:
        self.window._apply_database_detail_batch(runs)

    def show_status(self, message: str, timeout: int) -> None:
        self.window.show_status(message, timeout)

    def reload(self, path: str) -> None:
        self.window._reload_replaced_database(path)
