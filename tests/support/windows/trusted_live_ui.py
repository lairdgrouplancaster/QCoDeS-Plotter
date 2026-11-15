"""Shared fixtures and assertions for trusted live ui."""

from __future__ import annotations

from types import SimpleNamespace

from _database_load_fakes import PreviewPublicationGate

from qplot.datahandling.file_identity import (
    DatabaseInstance,
    logical_database_path,
)
from qplot.windows import _database_actions as database_actions

__all__ = [
    '_FakeLoadWorker',
    '_FakeService',
    '_LifecycleHarness',
    '_instance',
]


class _Signal:
    def __init__(self) -> None:
        self.callbacks = []

    def connect(self, callback) -> None:
        self.callbacks.append(callback)


class _Field:
    def __init__(self, text: str = "") -> None:
        self.value = text
        self.enabled = True
        self.signals_blocked = False

    def text(self) -> str:
        return self.value

    def setText(self, value: object) -> None:
        self.value = str(value)

    def setEnabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)

    def blockSignals(self, blocked: bool) -> bool:
        previous = self.signals_blocked
        self.signals_blocked = bool(blocked)
        return previous


class _Button:
    def __init__(self) -> None:
        self.enabled = True

    def setEnabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)


class _Frame:
    def __init__(self) -> None:
        self.visible = False

    def setVisible(self, visible: bool) -> None:
        self.visible = bool(visible)


class _Label:
    def __init__(self) -> None:
        self.text_value = ""
        self.tooltip = ""

    def setText(self, value: object) -> None:
        self.text_value = str(value)

    def setToolTip(self, value: object) -> None:
        self.tooltip = str(value)


class _Timer:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class _ThreadPool:
    def __init__(self) -> None:
        self.started = []

    def start(self, worker) -> None:
        self.started.append(worker)


class _Preview:
    def __init__(self, database_path: str = "", runs=None) -> None:
        self.database_path = database_path
        self.database_runs = (database_path, dict(runs or {}))

    def has_database(self, database_path: str) -> bool:
        return self.database_runs[0] == database_path

    def set_database_runs(self, database_path: str, runs) -> None:
        self.database_path = database_path
        self.database_runs = (database_path, dict(runs or {}))


class _LifecycleInfoBox:
    def __init__(self, database_path: str, runs) -> None:
        self.preview = _Preview(database_path, runs)
        self.cleared = False
        self.cache_cleared = False
        self.scrolled = False
        self.enabled = True

    def clear(self) -> None:
        self.cleared = True

    def clear_database_cache(self) -> None:
        self.cache_cleared = True

    def scrollToTop(self) -> None:
        self.scrolled = True

    def setEnabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)


class _LifecycleRunList(PreviewPublicationGate):
    def __init__(self, runs=None) -> None:
        self.runs = {}
        self.watching = []
        self.maxRunId = 0
        self.signals_blocked = False
        self.enabled = True
        self.addRuns(runs)

    def blockSignals(self, blocked: bool) -> bool:
        previous = self.signals_blocked
        self.signals_blocked = bool(blocked)
        return previous

    def clearSelection(self) -> None:
        return None

    def clear(self) -> None:
        for watcher in self.watching:
            watcher.valid = False
        self.runs = {}

    def addRuns(self, runs, *, continue_loading=None) -> bool:
        self.runs = dict(runs or {})
        self.maxRunId = max(self.runs, default=0)
        for run_id, metadata in self.runs.items():
            if callable(continue_loading) and not continue_loading():
                return False
            if metadata.get("completed_timestamp") is None:
                self.watching.append(
                    SimpleNamespace(run_id=run_id, valid=True),
                )
        return True

    def scrollToTop(self) -> None:
        return None

    def topLevelItemCount(self) -> int:
        return len(self.runs)

    def all_run_metadata(self):
        return dict(self.runs)

    def setEnabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)


class _FakeService:
    def __init__(self, instance: DatabaseInstance, *, accepted: bool = True) -> None:
        self.database_instance = instance
        self.accepted = accepted
        self.closing = False
        self.closed = False
        self.close_async_calls = 0

    def close_async(self) -> None:
        self.close_async_calls += 1


class _FakeLoadWorker:
    def __init__(self, service: _FakeService) -> None:
        self.trusted_service = service
        self.access_mode = database_actions.TRUSTED_LIVE_MODE
        self.fallback_reason = None
        self.cancelled = False
        self.signals = SimpleNamespace(status=_Signal(), finished=_Signal())

    def cancel(self) -> None:
        self.cancelled = True


class _Config:
    @staticmethod
    def get(key: str):
        if key == "runtime_settings.cloud_sync_timeout":
            return 30
        raise KeyError(key)


class _LifecycleHarness(database_actions.DatabaseActionsMixin):
    def __init__(
        self,
        instance: DatabaseInstance,
        service: _FakeService,
    ) -> None:
        runs = {1: {"guid": "guid-a", "run_timestamp": 1.0}}
        self._database_load_generation = 4
        self._database_load_active = False
        self._database_load_state = None
        self._database_load_worker = None
        self._database_refresh_generation = 1
        self._database_refresh_active = False
        self._database_refresh_pending = False
        self._database_refresh_worker = None
        self._database_selected_run_generation = 2
        self._trusted_read_service = service
        self._pending_trusted_read_services = {}
        self._retired_trusted_read_services = set()
        self._database_access_mode = database_actions.TRUSTED_LIVE_MODE
        self._database_fallback_reason = None
        self._loaded_database_identity = instance.identity
        self._loaded_database_instance = instance
        self._test_database_replacement_state = None
        self._database_view_released_for_generation = False
        self._test_database_generation_active = False
        self._apply_refresh_interval = None

        self.fileTextbox = _Field(instance.logical_path)
        self.run_idBox = _Field("1")
        self.measurementBox = _Field("signal")
        self.RunList = _LifecycleRunList(runs)
        self.infoBox = _LifecycleInfoBox(instance.logical_path, runs)
        self.monitor = _Timer()
        self.loadDatabaseButton = _Button()
        self.refreshDatabaseButton = _Button()
        self.databaseInfoButton = _Button()
        self.openDatabaseFolderButton = _Button()
        self.databaseLoadFrame = _Frame()
        self.databaseLoadLabel = _Label()
        self.databaseLoadThreadPool = _ThreadPool()
        self.config = _Config()

        self.selected_run_id = 1
        self._selected_run_guid = "guid-a"
        self.ds = object()
        self._selected_dataset_key = object()
        self.dataset_holder = {}
        self.localLastFile = instance.logical_path
        self.status_messages = []
        self.error_messages = []
        self.remembered_databases = []
        self.detail_loads = []
        self.default_selections = 0
        self.empty_state_syncs = 0
        self.cancelled_detail_loads = 0

    def show_status(self, message: str, timeout: int = 5000) -> None:
        self.status_messages.append((message, timeout))

    def show_error(self, title: str, message: str, details=None) -> None:
        self.error_messages.append((title, message, details))

    def remember_loaded_database(self, database_path: str) -> None:
        self.remembered_databases.append(database_path)

    def select_default_run(self) -> None:
        self.default_selections += 1

    def _start_database_detail_load(self, database_path: str, runs) -> None:
        self.detail_loads.append((database_path, dict(runs or {})))

    def _cancel_database_detail_load(self) -> None:
        self.cancelled_detail_loads += 1

    def _sync_empty_state(self) -> None:
        self.empty_state_syncs += 1


def _instance(name: str, identity: tuple[int, int]) -> DatabaseInstance:
    path = logical_database_path(name)
    return DatabaseInstance(path, path, identity)
