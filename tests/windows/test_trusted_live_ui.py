"""DB-free UI integration tests for the Stage 4 trusted read service."""

from __future__ import annotations

import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PyQt6 import QtCore, QtGui, QtTest
from PyQt6 import QtWidgets as qtw

from qplot.datahandling import database as database_module
from qplot.datahandling import readonly as readonly_module
from qplot.datahandling import readSQL as read_sql_module
from qplot.datahandling import trusted_live_service as trusted_service_module
from qplot.datahandling.file_identity import (
    DatabaseInstance,
    database_instance,
    logical_database_path,
)
from qplot.datahandling.trusted_live import (
    TrustedLiveCancelledError,
    TrustedLiveQueryError,
    TrustedQueryResult,
)
from qplot.datahandling.trusted_live_queries import (
    TrustedBootstrapResult,
    TrustedRunRecord,
    TrustedSelectedRunDetail,
)
from qplot.datahandling.trusted_live_service import TrustedLiveReadService
from qplot.datahandling.trusted_presentation import build_selected_run_presentation
from qplot.datahandling.trusted_snapshot import normalize_trusted_snapshot
from qplot.windows import _database_actions as database_actions
from qplot.windows import _plot_actions as plot_actions
from qplot.windows import _run_controls as run_controls
from qplot.windows._widgets import preview as preview_module
from qplot.windows._widgets import treeWidgets


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
        self.database_runs = (database_path, dict(runs or {}))

    def has_database(self, database_path: str) -> bool:
        return self.database_runs[0] == database_path

    def set_database_runs(self, database_path: str, runs) -> None:
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


class _LifecycleRunList:
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
        self._database_selected_run_worker = None
        self._database_selected_run_instance = None
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
        self._selected_run_detail_cache = {}
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


class _RefreshPublicationSource:
    def __init__(self) -> None:
        self.acknowledgements = 0
        self.rejections = []

    def acknowledge_new_runs_published(self) -> None:
        self.acknowledgements += 1

    def reject_new_runs_publication(self, error) -> None:
        self.rejections.append(error)


class _RefreshPublicationHarness(database_actions.DatabaseActionsMixin):
    def __init__(self) -> None:
        self._database_refresh_generation = 7
        self._database_refresh_active = True
        self._database_refresh_publication_active = False
        self._database_refresh_instance = None
        self._database_refresh_staged_new_runs = {}
        self._database_load_active = False
        self._database_load_publication_active = False
        self._test_database_replacement_state = None
        self._test_database_generation_active = False
        self._shutdown_started = False
        self._shutdown_ready = False
        self._database_access_mode = database_actions.TRUSTED_LIVE_MODE
        self._selected_run_guid = "guid-1"
        self.selected_run_id = 1
        self.fileTextbox = _Field("refresh.db")
        accepted_instance = database_instance(self.fileTextbox.text())
        self._database_refresh_instance = accepted_instance
        self._loaded_database_identity = accepted_instance.identity
        self._loaded_database_instance = accepted_instance
        self.RunList = treeWidgets.RunList()
        self.RunList.addRuns(
            {
                1: {
                    "guid": "guid-1",
                    "sweep_parameters": [],
                    "measure_parameters": [],
                    "is_completed": True,
                },
                2: {
                    "guid": "guid-2",
                    "sweep_parameters": [],
                    "measure_parameters": [],
                    "is_completed": True,
                },
            }
        )
        self.RunList.setCurrentItem(self.RunList._item_for_guid("guid-1"))
        self.selection_updates: list[str | None] = []
        self.empty_state_syncs = 0
        self.status_messages = []

    def _reload_if_worker_database_instance_changed(
        self,
        _database_path,
        _expected_instance,
    ) -> bool:
        return False

    def _sync_empty_state(self) -> None:
        self.empty_state_syncs += 1

    def show_status(self, message: str, timeout: int = 5000) -> None:
        self.status_messages.append((message, timeout))

    def updateSelected(self, guid: str) -> None:
        if not self._database_generation_read_allowed(notify=False):
            return
        self._selected_run_guid = str(guid)
        self.selection_updates.append(str(guid))

    def clear_non_single_run_selection(self) -> None:
        if not self._database_generation_read_allowed(notify=False):
            return
        self._selected_run_guid = None
        self.selection_updates.append(None)

    def dispose(self) -> None:
        self.RunList.deleteLater()
        qtw.QApplication.sendPostedEvents(
            None,
            QtCore.QEvent.Type.DeferredDelete,
        )
        qtw.QApplication.processEvents()


def test_refresh_publication_replays_selection_changed_during_qt_yield():
    harness = _RefreshPublicationHarness()
    source = _RefreshPublicationSource()
    first = harness.RunList._item_for_guid("guid-1")
    second = harness.RunList._item_for_guid("guid-2")
    assert first is not None
    assert second is not None
    harness.RunList.setCurrentItem(first)
    harness.RunList.selected.connect(harness.updateSelected)
    page = {
        run_id: {
            "guid": f"guid-{run_id}",
            "sweep_parameters": [],
            "measure_parameters": [],
            "is_completed": True,
        }
        for run_id in range(3, 304)
    }

    def change_selection(*_args) -> None:
        harness.RunList.clearSelection()
        second.setSelected(True)
        harness.RunList.setCurrentItem(second)

    try:
        with (
            patch.object(
                database_actions.DatabaseActionsMixin,
                "_reload_if_worker_database_instance_changed",
                return_value=False,
            ),
            patch.object(
                treeWidgets.QtCore.QCoreApplication,
                "processEvents",
                side_effect=change_selection,
            ),
        ):
            harness.database_refresh_new_runs_ready(
                harness._database_refresh_generation,
                "refresh.db",
                page,
                source,
            )

        assert harness._selected_run_guid == "guid-2"
        assert harness.selection_updates == ["guid-2"]
        assert source.acknowledgements == 1
        assert source.rejections == []
        assert harness.RunList.maxRunId == 303
        assert harness.RunList.topLevelItemCount() == 303
    finally:
        harness.dispose()


def test_refresh_publication_abort_rolls_back_partial_page_and_cursor():
    harness = _RefreshPublicationHarness()
    source = _RefreshPublicationSource()
    original_add_runs = harness.RunList.addRuns
    page = {
        run_id: {
            "guid": f"guid-{run_id}",
            "sweep_parameters": [],
            "measure_parameters": [],
            "is_completed": True,
        }
        for run_id in range(3, 304)
    }

    def select_staged_row_and_abort(*_args) -> None:
        staged_item = harness.RunList._item_for_guid("guid-252")
        assert staged_item is not None
        harness.RunList.clearSelection()
        staged_item.setSelected(True)
        harness.RunList.setCurrentItem(staged_item)
        harness._database_refresh_active = False

    def add_runs_then_clear_removed_selection(runs, **kwargs):
        result = original_add_runs(runs, **kwargs)
        if result is False:
            harness.RunList.clearSelection()
            harness.RunList.setCurrentItem(None)
        return result

    try:
        harness.RunList.addRuns = add_runs_then_clear_removed_selection
        harness.RunList.selected.connect(harness.updateSelected)
        with (
            patch.object(database_actions, "log_exception"),
            patch.object(
                database_actions.DatabaseActionsMixin,
                "_reload_if_worker_database_instance_changed",
                return_value=False,
            ),
            patch.object(
                treeWidgets.QtCore.QCoreApplication,
                "processEvents",
                side_effect=select_staged_row_and_abort,
            ),
        ):
            harness.database_refresh_new_runs_ready(
                harness._database_refresh_generation,
                "refresh.db",
                page,
                source,
            )

        assert source.acknowledgements == 0
        assert len(source.rejections) == 1
        assert isinstance(
            source.rejections[0],
            database_actions._DatabaseRefreshPublicationAborted,
        )
        assert harness.RunList.maxRunId == 2
        assert harness.RunList.topLevelItemCount() == 2
        assert harness.RunList._item_for_guid("guid-252") is None
        assert harness._database_refresh_staged_new_runs == {}
        assert harness.RunList.selectedItems() == []
        assert harness._selected_run_guid is None
        assert harness.selection_updates == [None]
    finally:
        harness.dispose()


@pytest.mark.parametrize(
    ("changed_identity", "changed_sidecars"),
    (
        ((1, 3), frozenset({(2, 11)})),
        ((1, 2), frozenset()),
        ((1, 2), frozenset({(2, 12)})),
    ),
    ids=("main-replaced", "sidecar-removed", "sidecar-replaced"),
)
def test_refresh_page_source_change_during_qt_yield_rolls_back_before_ack(
    changed_identity,
    changed_sidecars,
):
    harness = _RefreshPublicationHarness()
    source = _RefreshPublicationSource()
    path = logical_database_path("refresh-source-change.db")
    accepted = DatabaseInstance(
        path,
        path,
        (1, 2),
        sidecar_identities=frozenset({(2, 11)}),
    )
    changed = DatabaseInstance(
        path,
        path,
        changed_identity,
        sidecar_identities=changed_sidecars,
    )
    harness.fileTextbox.setText(path)
    harness._database_refresh_instance = accepted
    harness._loaded_database_identity = accepted.identity
    harness._loaded_database_instance = accepted
    observed = [accepted]
    page = {
        run_id: {
            "guid": f"guid-{run_id}",
            "sweep_parameters": [],
            "measure_parameters": [],
            "is_completed": True,
        }
        for run_id in range(3, 304)
    }

    def replace_source(*_args) -> None:
        observed[0] = changed

    try:
        with (
            patch.object(
                database_actions,
                "database_instance",
                side_effect=lambda _path: observed[0],
            ),
            patch.object(database_actions, "log_exception"),
            patch.object(
                database_actions.DatabaseActionsMixin,
                "_reload_if_worker_database_instance_changed",
                return_value=False,
            ),
            patch.object(harness, "_reload_replaced_database") as reload_replaced,
            patch.object(
                treeWidgets.QtCore.QCoreApplication,
                "processEvents",
                side_effect=replace_source,
            ),
        ):
            harness.database_refresh_new_runs_ready(
                harness._database_refresh_generation,
                path,
                page,
                source,
            )

        assert source.acknowledgements == 0
        assert len(source.rejections) == 1
        assert isinstance(
            source.rejections[0],
            database_actions.DatabaseInstanceChangedError,
        )
        assert harness.RunList.maxRunId == 2
        assert harness.RunList.topLevelItemCount() == 2
        assert harness.RunList._item_for_guid("guid-252") is None
        assert harness._database_refresh_staged_new_runs == {}
        reload_replaced.assert_called_once_with(path)
    finally:
        harness.dispose()


@pytest.mark.parametrize(
    "changed_sidecars",
    (frozenset(), frozenset({(2, 12)})),
    ids=("new-sidecar-removed", "new-sidecar-replaced"),
)
def test_refresh_commit_uses_sidecar_baseline_promoted_by_preflight(
    changed_sidecars,
):
    harness = _RefreshPublicationHarness()
    source = _RefreshPublicationSource()
    path = logical_database_path("refresh-promoted-sidecar.db")
    initially_empty = DatabaseInstance(path, path, (1, 2))
    appeared = DatabaseInstance(
        path,
        path,
        (1, 2),
        sidecar_identities=frozenset({(2, 11)}),
    )
    changed = DatabaseInstance(
        path,
        path,
        (1, 2),
        sidecar_identities=changed_sidecars,
    )
    harness.fileTextbox.setText(path)
    harness._database_refresh_instance = initially_empty
    harness._loaded_database_identity = initially_empty.identity
    harness._loaded_database_instance = initially_empty
    observed = [appeared]
    page = {
        run_id: {
            "guid": f"guid-{run_id}",
            "sweep_parameters": [],
            "measure_parameters": [],
            "is_completed": True,
        }
        for run_id in range(3, 304)
    }

    def accept_first_sidecar(_self, _database_path, _expected_instance):
        assert database_actions.DatabaseActionsMixin._advance_loaded_database_sidecar_baseline(
            harness,
            appeared,
        )
        return False

    def replace_source(*_args) -> None:
        observed[0] = changed

    try:
        with (
            patch.object(
                database_actions,
                "database_instance",
                side_effect=lambda _path: observed[0],
            ),
            patch.object(database_actions, "log_exception"),
            patch.object(
                _RefreshPublicationHarness,
                "_reload_if_worker_database_instance_changed",
                new=accept_first_sidecar,
            ),
            patch.object(harness, "_reload_replaced_database") as reload_replaced,
            patch.object(
                treeWidgets.QtCore.QCoreApplication,
                "processEvents",
                side_effect=replace_source,
            ),
        ):
            harness.database_refresh_new_runs_ready(
                harness._database_refresh_generation,
                path,
                page,
                source,
            )

        assert source.acknowledgements == 0
        assert len(source.rejections) == 1
        assert isinstance(
            source.rejections[0],
            database_actions.DatabaseInstanceChangedError,
        )
        assert harness.RunList.maxRunId == 2
        assert harness.RunList.topLevelItemCount() == 2
        assert harness._database_refresh_staged_new_runs == {}
        reload_replaced.assert_called_once_with(path)
    finally:
        harness.dispose()


def test_trusted_refresh_updates_selected_completed_row_and_reloads_detail():
    class Harness(database_actions.DatabaseActionsMixin):
        def __init__(self) -> None:
            self._database_load_publication_active = False
            self._database_refresh_publication_active = False
            self._test_database_replacement_state = None
            self._database_access_mode = database_actions.TRUSTED_LIVE_MODE
            self._selected_run_guid = "selected-guid"
            self._selected_run_detail_cache = {
                ("instance", 7, "selected-guid"): object(),
            }
            self.RunList = treeWidgets.RunList()
            self.RunList.addRuns(
                {
                    7: {
                        "guid": "selected-guid",
                        "sweep_parameters": [],
                        "measure_parameters": ["signal"],
                        "is_completed": True,
                        "result_count": 10,
                    }
                }
            )
            self.reselected = []
            self.empty_state_syncs = 0
            self.status_messages = []

        def updateSelected(self, guid: str) -> None:
            self.reselected.append(str(guid))

        def _sync_empty_state(self) -> None:
            self.empty_state_syncs += 1

        def show_status(self, message: str, timeout: int = 5000) -> None:
            self.status_messages.append((message, timeout))

        def _empty_database_refresh_status(self) -> str:
            return "empty"

    harness = Harness()
    status = {
        "guid": "selected-guid",
        "is_completed": True,
        "result_count": 15,
    }
    try:
        assert harness.RunList.watching == []

        harness._apply_database_refresh_result(
            {},
            {"selected-guid": status},
        )

        item = harness.RunList._item_for_guid("selected-guid")
        assert item is not None
        assert item.run_metadata["result_count"] == 15
        assert harness._selected_run_detail_cache == {}
        assert harness.reselected == ["selected-guid"]
    finally:
        harness.RunList.deleteLater()
        qtw.QApplication.sendPostedEvents(
            None,
            QtCore.QEvent.Type.DeferredDelete,
        )
        qtw.QApplication.processEvents()


class _SelectionRunList:
    def __init__(self, run_id: int, guid: str) -> None:
        self.run_id = run_id
        self.guid = guid
        self.item = SimpleNamespace(
            run_metadata={
                "run_id": run_id,
                "guid": guid,
                "name": "trusted run",
                "result_count": 12,
            }
        )
        self.updated = []
        self.cleared = False

    def _item_for_guid(self, guid: str):
        return self.item if guid == self.guid else None

    def run_id_for_guid(self, guid: str):
        return self.run_id if guid == self.guid else None

    def all_run_metadata(self):
        if self.cleared:
            return {}
        return {self.run_id: dict(self.item.run_metadata)}

    def updateRuns(self, runs) -> None:
        self.updated.append(dict(runs))
        metadata = runs.get(self.run_id)
        if metadata:
            self.item.run_metadata.update(metadata)

    def clear(self) -> None:
        self.cleared = True


class _RecordingInfoBox:
    def __init__(self) -> None:
        self.events = []
        self.visible = None

    def set_trusted_run_loading(self, run) -> None:
        self.events.append(("loading", dict(run)))
        self.visible = self.events[-1]

    def set_trusted_run_detail(self, detail) -> None:
        self.events.append(("detail", detail))
        self.visible = self.events[-1]

    def set_trusted_run_error(self, error, run) -> None:
        self.events.append(("error", str(error), dict(run)))
        self.visible = self.events[-1]

    def set_snapshot_run_loading(self, run) -> None:
        self.events.append(("snapshot-loading", dict(run)))
        self.visible = self.events[-1]

    def set_snapshot_run_detail(self, detail) -> None:
        self.events.append(("snapshot-detail", detail))
        self.visible = self.events[-1]

    def set_snapshot_run_unavailable(self, run) -> None:
        self.events.append(("snapshot-unavailable", dict(run)))
        self.visible = self.events[-1]

    def set_snapshot_run_error(self, error, run) -> None:
        self.events.append(("snapshot-error", str(error), dict(run)))
        self.visible = self.events[-1]

    def clear(self) -> None:
        self.visible = None


class _DiscardableRunList:
    def __init__(self) -> None:
        self.visible = {1: {"guid": "committed-guid"}}
        self.signals_blocked = False

    def blockSignals(self, blocked: bool) -> bool:
        previous = self.signals_blocked
        self.signals_blocked = bool(blocked)
        return previous

    def clear(self) -> None:
        self.visible = {}


class _FakeSelectedWorker:
    def __init__(self, args, kwargs) -> None:
        self.args = args
        self.kwargs = kwargs
        self.cancelled = False
        self.signals = SimpleNamespace(progress=_Signal(), finished=_Signal())

    def cancel(self) -> None:
        self.cancelled = True


class _SelectionHarness(database_actions.DatabaseActionsMixin):
    def __init__(self, instance: DatabaseInstance, service: _FakeService) -> None:
        self._loaded_database_instance = instance
        self._loaded_database_identity = instance.identity
        self._trusted_read_service = service
        self._database_access_mode = database_actions.TRUSTED_LIVE_MODE
        self._database_selected_run_generation = 8
        self._database_selected_run_worker = None
        self._database_selected_run_instance = None
        self._database_selected_run_mode = database_actions.TRUSTED_LIVE_MODE
        self._selected_run_guid = None
        self._selected_run_detail_cache = {}
        self._snapshot_setpoint_summary_cache = {"guid-7": object()}
        self._selected_dataset_key = object()
        self.selected_run_id = None
        self.ds = object()

        self.fileTextbox = _Field(instance.logical_path)
        self.run_idBox = _Field()
        self.RunList = _SelectionRunList(7, "guid-7")
        self.infoBox = _RecordingInfoBox()
        self.databaseDetailThreadPool = _ThreadPool()
        self.status_messages = []
        self.prioritized = []
        self.released_datasets = 0
        self.reloads = []

    def _release_selected_dataset(self) -> None:
        self.released_datasets += 1
        self.ds = None
        self._selected_dataset_key = None

    def _prioritize_database_detail_runs(self, run_ids) -> None:
        self.prioritized.append(list(run_ids))

    def _prioritize_preview_runs(self, run_ids) -> None:
        self.prioritized.append(list(run_ids))

    def _reload_if_worker_database_instance_changed(
        self,
        _database_path,
        _instance,
    ) -> bool:
        return False

    def _reload_replaced_database(self, database_path: str) -> None:
        self.reloads.append(database_path)

    def _run_metadata_for_guid(self, guid: str):
        item = self.RunList._item_for_guid(guid)
        return dict(item.run_metadata) if item is not None else {}

    @staticmethod
    def _run_point_count(metadata):
        return metadata.get("result_count")

    def show_status(self, message: str, timeout: int = 5000) -> None:
        self.status_messages.append((message, timeout))

    def _load_dataset(self, *_args, **_kwargs):
        raise AssertionError("trusted selection must not load a QCoDeS DataSet")


class _TrustedPreviewBoundaryHarness(
    _LifecycleHarness,
    plot_actions.PlotActionsMixin,
    run_controls.RunControlsMixin,
):
    """Use the real run-list/details widgets around fake Stage 4 workers."""

    def __init__(self, instance: DatabaseInstance, service: _FakeService) -> None:
        super().__init__(instance, service)
        runs = {1: {"guid": "guid-a", "run_timestamp": 1.0}}
        self.RunList = treeWidgets.RunList()
        self.RunList.addRuns(runs)
        self.infoBox = treeWidgets.moreInfo(preview_size=80)
        self.infoBox.preview.set_database_runs("", {})
        self.RunList.selected.connect(lambda guid: self.updateSelected(guid))
        self.RunList.nonSingleSelection.connect(
            lambda: self.clear_non_single_run_selection()
        )
        self.RunList.verticalScrollBar().valueChanged.connect(
            lambda _value: self._run_table_view_changed()
        )
        self.databaseDetailThreadPool = _ThreadPool()
        self.databaseSelectedRunThreadPool = self.databaseDetailThreadPool
        self._database_detail_generation = 0
        self._database_detail_active = False
        self._database_detail_worker = None
        self._database_detail_instance = None
        self._database_expensive_detail_generation = 0
        self._database_expensive_detail_active = False
        self._database_expensive_detail_worker = None
        self._database_expensive_detail_instance = None
        self.ds = None
        self._selected_dataset_key = None
        self.selected_run_id = None
        self._selected_run_guid = None

    def select_default_run(self) -> None:
        database_actions.DatabaseActionsMixin.select_default_run(self)

    def dispose(self) -> None:
        self.infoBox.preview.shutdown()
        self.RunList.deleteLater()
        self.infoBox.deleteLater()
        qtw.QApplication.sendPostedEvents(
            None,
            QtCore.QEvent.Type.DeferredDelete,
        )
        qtw.QApplication.processEvents()


class _ActionRunList:
    def __init__(self, run_id: int, guid: str) -> None:
        self.runs = {
            run_id: {
                "guid": guid,
                "measure_parameters": ("signal",),
            }
        }

    def all_run_metadata(self):
        return dict(self.runs)

    def run_id_for_guid(self, guid: str):
        for run_id, metadata in self.runs.items():
            if metadata["guid"] == guid:
                return run_id
        return None


class _ActionConnection:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class _ActionParameter:
    name = "signal"
    depends_on = "gate"
    depends_on_ = ("gate",)


class _ActionDataset:
    def __init__(self, guid: str, run_id: int) -> None:
        self.guid = guid
        self.run_id = run_id
        self.conn = _ActionConnection()
        self.parameter = _ActionParameter()

    def get_parameters(self):
        return [self.parameter]

    @staticmethod
    def get_parameter_data(parameter_name: str):
        assert parameter_name == "signal"
        return {
            "signal": {
                "gate": [0.0, 1.0],
                "signal": [10.0, 11.0],
            }
        }


class _TrustedActionHarness(plot_actions.PlotActionsMixin):
    def __init__(
        self,
        instance: DatabaseInstance,
        service: _FakeService,
        guid: str,
    ) -> None:
        self.fileTextbox = _Field(instance.logical_path)
        self.measurementBox = _Field("*")
        self._loaded_database_instance = instance
        self._loaded_database_identity = instance.identity
        self._database_access_mode = database_actions.TRUSTED_LIVE_MODE
        self._trusted_read_service = service
        self._selected_run_guid = guid
        self.selected_run_id = 7
        self.ds = None
        self._selected_dataset_key = None
        self.dataset_holder = {}
        self.RunList = _ActionRunList(7, guid)
        self.windows = []
        self.infoBox = SimpleNamespace(
            preview=SimpleNamespace(request_guids=lambda _guids: None)
        )
        self.spinBox = SimpleNamespace(value=lambda: 1.0)
        self.status_messages = []
        self.error_messages = []
        self.opened = []
        self.export_filename = "trusted-export.csv"

    def _reload_if_database_instance_changed(self, _database_path: str) -> bool:
        return False

    def openWin(
        self,
        widget,
        dataset,
        parameter,
        *,
        dataset_key,
        **kwargs,
    ) -> None:
        self.opened.append((widget, dataset, parameter, dataset_key, kwargs))

    def _choose_csv_export_filename(self, _default_name: str) -> str:
        return self.export_filename

    def post_admin(self) -> None:
        return None

    def show_status(self, message: str, timeout: int = 5000) -> None:
        self.status_messages.append((message, timeout))

    def show_error(self, title: str, message: str, details=None) -> None:
        self.error_messages.append((title, message, details))


class _DelayedJob:
    def __init__(self) -> None:
        self.release = threading.Event()
        self.cancelled = threading.Event()


class _DelayedSupervisor:
    """Supervisor fake whose query, cancellation, and close all pause."""

    def __init__(self, instance: DatabaseInstance) -> None:
        self.database_instance = instance
        self.incarnation = 1
        self.helper_pid = 41_041
        self.helper_alive = True
        self.query_entered = threading.Event()
        self.cancel_entered = threading.Event()
        self.cancel_release = threading.Event()
        self.close_entered = threading.Event()
        self.close_release = threading.Event()
        self.job = None

    def submit_query(self, *_args, **_kwargs) -> _DelayedJob:
        self.job = _DelayedJob()
        return self.job

    def wait(self, job: _DelayedJob, *, timeout=None):
        del timeout
        self.query_entered.set()
        if not job.release.wait(5):
            raise AssertionError("delayed trusted query was not released")
        if job.cancelled.is_set():
            raise TrustedLiveCancelledError("delayed trusted query cancelled")
        return TrustedQueryResult(("value",), ((1,),))

    def cancel(self, job: _DelayedJob) -> bool:
        self.cancel_entered.set()
        job.cancelled.set()
        job.release.set()
        if not self.cancel_release.wait(5):
            raise AssertionError("delayed trusted cancellation was not released")
        return True

    def close(self) -> None:
        self.close_entered.set()
        if not self.close_release.wait(5):
            raise AssertionError("delayed trusted close was not released")
        self.helper_alive = False


class _DelayedAdapter:
    def __init__(self, executor, _database_path: str) -> None:
        self.executor = executor

    def bind_executor(self, executor) -> None:
        self.executor = executor

    def bootstrap(self) -> TrustedBootstrapResult:
        self.executor.query("delayed-query")
        return TrustedBootstrapResult(1, 1, 1)


def _wait_for_gui_event(event: threading.Event, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not event.is_set() and time.monotonic() < deadline:
        QtTest.QTest.qWait(5)
    assert event.is_set()


def _assert_gui_timer_advances(ticks: list[float]) -> None:
    previous = len(ticks)
    QtTest.QTest.qWait(30)
    assert len(ticks) > previous


def _instance(name: str, identity: tuple[int, int]) -> DatabaseInstance:
    path = logical_database_path(name)
    return DatabaseInstance(path, path, identity)


def _sidecar_swap_fixture(tmp_path, name: str):
    database_path = tmp_path / f"{name}.db"
    wal_path = tmp_path / f"{name}.db-wal"
    replacement_wal = tmp_path / f"{name}.replacement-wal"
    database_path.write_bytes(b"main")
    wal_path.write_bytes(b"accepted wal")
    replacement_wal.write_bytes(b"replacement wal")
    return database_path, wal_path, replacement_wal, database_instance(database_path)


def _selected_detail() -> TrustedSelectedRunDetail:
    return TrustedSelectedRunDetail(
        run=TrustedRunRecord(
            7,
            (
                ("guid", "guid-7"),
                ("name", "trusted run"),
                ("result_count", 12),
            ),
        ),
        parameters=(),
        metadata=(),
        snapshot=normalize_trusted_snapshot(None),
        setpoint_summaries=(),
        presentation=build_selected_run_presentation(
            run_fields={
                "run_id": 7,
                "guid": "guid-7",
                "name": "trusted run",
                "result_count": 12,
            },
            metadata_fields={},
            parameters=(),
            snapshot_summary={"Status": "empty"},
            setpoint_summaries=(),
            unavailable_fields=(),
        ),
    )


def test_trusted_selection_publishes_loading_then_plain_detail_without_db_io():
    instance = _instance("selected.db", (1, 7))
    service = _FakeService(instance)
    harness = _SelectionHarness(instance, service)
    workers = []

    def worker_factory(*args, **kwargs):
        worker = _FakeSelectedWorker(args, kwargs)
        workers.append(worker)
        return worker

    with (
        patch.object(
            database_actions,
            "DatabaseSelectedRunWorker",
            side_effect=worker_factory,
        ),
        patch.object(
            database_actions,
            "database_instance",
            return_value=instance,
        ),
    ):
        assert harness._update_trusted_selected_run("guid-7")
        generation = harness._database_selected_run_generation
        detail = _selected_detail()
        active_worker = harness._database_selected_run_worker
        harness.database_selected_run_progress(
            generation,
            instance.logical_path,
            "guid-7",
            detail,
        )
        assert harness._database_selected_run_worker is active_worker
        assert harness._selected_run_detail_cache == {
            database_actions._selected_run_detail_cache_key(
                instance, 7, "guid-7"
            ): detail,
        }
        assert harness._selected_run_partial_detail_keys == {
            database_actions._selected_run_detail_cache_key(instance, 7, "guid-7"),
        }
        harness.database_selected_run_finished(
            generation,
            instance.logical_path,
            "guid-7",
            detail,
            None,
        )

    assert harness.infoBox.events[0][0] == "loading"
    assert harness.infoBox.events[1] == ("detail", detail)
    assert harness.infoBox.events[2] == ("detail", detail)
    assert harness.ds is None
    assert harness.released_datasets == 1
    assert harness.prioritized == [[7]]
    assert len(workers) == 1
    assert workers[0].args[2:5] == (7, "guid-7", service)
    assert harness.databaseDetailThreadPool.started == workers
    assert harness.RunList.updated == 2 * [
        {
            7: {
                "guid": "guid-7",
                "name": "trusted run",
                "result_count": 12,
            }
        }
    ]
    assert harness._selected_run_detail_cache == {
        database_actions._selected_run_detail_cache_key(instance, 7, "guid-7"): detail,
    }
    assert harness._selected_run_partial_detail_keys == set()
    assert harness.status_messages[-1][0] == "Selected run 7 with 12 points."


def test_snapshot_selection_is_basic_only_and_starts_no_detail_snapshot(tmp_path):
    database_path = tmp_path / "snapshot-selection.db"
    database_path.write_bytes(b"snapshot selection identity")
    instance = database_instance(database_path)
    harness = _SelectionHarness(instance, _FakeService(instance))
    harness._database_access_mode = database_actions.SNAPSHOT_FALLBACK_MODE
    harness._trusted_read_service = None
    detail = _selected_detail()
    harness._selected_run_detail_cache[
        database_actions._selected_run_detail_cache_key(
            instance,
            7,
            "guid-7",
        )
    ] = detail

    forbidden = AssertionError("selection dispatch performed eager database I/O")
    with (
        patch.object(
            database_actions,
            "DatabaseSelectedRunWorker",
            side_effect=forbidden,
        ) as selected_worker,
        patch.object(database_actions, "database_instance", return_value=instance),
        patch.object(
            harness.databaseDetailThreadPool,
            "start",
            side_effect=forbidden,
        ) as detail_pool_start,
        patch.object(
            plot_actions,
            "load_by_guid_read_only",
            side_effect=forbidden,
        ) as dataset_loader,
        patch.object(
            database_module,
            "get_snapshot_selected_run_detail",
            side_effect=forbidden,
        ) as detail_reader,
        patch.object(
            read_sql_module,
            "_read_only_connection",
            side_effect=forbidden,
        ) as retained_reader,
        patch.object(
            readonly_module,
            "_prepare_read_target",
            side_effect=forbidden,
        ) as snapshot_prep,
        patch.object(
            read_sql_module,
            "qcodes_read_only_connection",
            side_effect=forbidden,
        ) as qcodes_opener,
        patch.object(
            read_sql_module,
            "sqlite_read_only_connection",
            side_effect=forbidden,
        ) as sqlite_opener,
        patch.object(
            treeWidgets,
            "sqlite_read_only_connection",
            side_effect=forbidden,
            create=True,
        ) as widget_opener,
    ):
        plot_actions.PlotActionsMixin.updateSelected(harness, "guid-7")

        assert harness.infoBox.events == [
            (
                "snapshot-unavailable",
                {
                    "run_id": 7,
                    "guid": "guid-7",
                    "name": "trusted run",
                    "result_count": 12,
                },
            )
        ]
        assert harness.ds is None
        assert harness._selected_dataset_key is None
        assert harness._database_selected_run_worker is None
        assert harness._database_selected_run_instance is None
        assert harness._database_selected_run_mode is None
        assert harness.prioritized == [[7]]
        selected_worker.assert_not_called()
        detail_pool_start.assert_not_called()
        dataset_loader.assert_not_called()
        detail_reader.assert_not_called()
        retained_reader.assert_not_called()
        snapshot_prep.assert_not_called()
        qcodes_opener.assert_not_called()
        sqlite_opener.assert_not_called()
        widget_opener.assert_not_called()
    assert harness.status_messages[-1][0] == "Selected run 7 with 12 points."


def test_snapshot_basic_only_publication_restores_reentrant_selection():
    instance = _instance("reentrant-snapshot-basic.db", (4, 7))

    class TwoRunList:
        def __init__(self):
            self.items = {
                guid: SimpleNamespace(
                    run_metadata={
                        "run_id": run_id,
                        "guid": guid,
                        "result_count": run_id,
                    }
                )
                for run_id, guid in ((7, "guid-7"), (8, "guid-8"))
            }
            self.updated = []

        def _item_for_guid(self, guid):
            return self.items.get(guid)

        def run_id_for_guid(self, guid):
            item = self._item_for_guid(guid)
            return item.run_metadata["run_id"] if item is not None else None

        def updateRuns(self, runs):
            self.updated.append(dict(runs))

    class ReentrantInfoBox:
        def __init__(self):
            self.harness = None
            self.reentered = False
            self.visible = None

        def set_snapshot_run_unavailable(self, run):
            guid = str(run["guid"])
            if guid == "guid-7" and not self.reentered:
                self.reentered = True
                plot_actions.PlotActionsMixin.updateSelected(
                    self.harness,
                    "guid-8",
                )
            # Simulate A's setter resuming after B has finished rendering.
            self.visible = ("snapshot-unavailable", guid)

        def clear(self):
            self.visible = None

    harness = _SelectionHarness(instance, _FakeService(instance))
    harness._database_access_mode = database_actions.SNAPSHOT_FALLBACK_MODE
    harness._trusted_read_service = None
    harness.RunList = TwoRunList()
    harness.infoBox = ReentrantInfoBox()
    harness.infoBox.harness = harness

    with (
        patch.object(database_actions, "database_instance", return_value=instance),
        patch.object(
            database_actions,
            "DatabaseSelectedRunWorker",
            side_effect=AssertionError("fallback selection started detail work"),
        ) as selected_worker,
        patch.object(
            harness.databaseDetailThreadPool,
            "start",
            side_effect=AssertionError("fallback selection started a worker"),
        ) as pool_start,
    ):
        plot_actions.PlotActionsMixin.updateSelected(harness, "guid-7")

    selected_worker.assert_not_called()
    pool_start.assert_not_called()
    assert harness.infoBox.reentered
    assert harness._selected_run_guid == "guid-8"
    assert harness.selected_run_id == 8
    assert harness.infoBox.visible == ("snapshot-unavailable", "guid-8")


@pytest.mark.parametrize("publication_kind", ("cached", "async"))
@pytest.mark.parametrize("restore_reentry", (False, True))
def test_selected_detail_reentrancy_restores_newer_selection(
    publication_kind,
    restore_reentry,
):
    instance = _instance("reentrant-selected.db", (4, 7))

    class TwoRunList:
        def __init__(self):
            self.items = {
                7: SimpleNamespace(
                    run_metadata={
                        "run_id": 7,
                        "guid": "guid-7",
                        "result_count": 12,
                    }
                ),
                8: SimpleNamespace(
                    run_metadata={
                        "run_id": 8,
                        "guid": "guid-8",
                        "result_count": 3,
                    }
                ),
                9: SimpleNamespace(
                    run_metadata={
                        "run_id": 9,
                        "guid": "guid-9",
                        "result_count": 5,
                    }
                ),
            }
            self.updated = []

        def _item_for_guid(self, guid):
            return next(
                (
                    item
                    for item in self.items.values()
                    if item.run_metadata["guid"] == guid
                ),
                None,
            )

        def run_id_for_guid(self, guid):
            item = self._item_for_guid(guid)
            return item.run_metadata["run_id"] if item is not None else None

        def updateRuns(self, runs):
            self.updated.append(dict(runs))
            for run_id, metadata in runs.items():
                self.items[run_id].run_metadata.update(metadata)

    class ReentrantInfoBox:
        def __init__(self):
            self.events = []
            self.visible = None
            self.reentered = False
            self.restore_reentered = False
            self.loading_counts = {}
            self.harness = None

        def _loading(self, run):
            guid = str(run["guid"])
            event = ("loading", guid)
            self.events.append(event)
            self.loading_counts[guid] = self.loading_counts.get(guid, 0) + 1
            if (
                restore_reentry
                and guid == "guid-8"
                and self.loading_counts[guid] == 2
                and not self.restore_reentered
            ):
                self.restore_reentered = True
                plot_actions.PlotActionsMixin.updateSelected(
                    self.harness,
                    "guid-9",
                )
            # A restore setter can likewise resume after the nested selection.
            self.visible = event

        def _detail(self, detail):
            guid = str(detail.run.as_dict()["guid"])
            self.events.append(("detail", guid))
            if guid == "guid-7" and not self.reentered:
                self.reentered = True
                plot_actions.PlotActionsMixin.updateSelected(
                    self.harness,
                    "guid-8",
                )
            # Simulate a real setter that resumes and finishes rendering A
            # after nested selection B has already rendered its placeholder.
            self.visible = ("detail", guid)

        set_trusted_run_loading = _loading
        set_snapshot_run_loading = _loading
        set_trusted_run_detail = _detail
        set_snapshot_run_detail = _detail

        def clear(self):
            self.visible = None

    harness = _SelectionHarness(instance, _FakeService(instance))
    harness._database_access_mode = database_actions.TRUSTED_LIVE_MODE
    harness.RunList = TwoRunList()
    harness.infoBox = ReentrantInfoBox()
    harness.infoBox.harness = harness
    workers = []

    def worker_factory(*args, **kwargs):
        worker = _FakeSelectedWorker(args, kwargs)
        workers.append(worker)
        return worker

    detail = _selected_detail()
    stale_key = database_actions._selected_run_detail_cache_key(
        instance,
        7,
        "guid-7",
    )
    if publication_kind == "cached":
        harness._selected_run_detail_cache[stale_key] = detail

    with (
        patch.object(
            database_actions,
            "DatabaseSelectedRunWorker",
            side_effect=worker_factory,
        ),
        patch.object(database_actions, "database_instance", return_value=instance),
    ):
        plot_actions.PlotActionsMixin.updateSelected(harness, "guid-7")
        if publication_kind == "async":
            generation = harness._database_selected_run_generation
            harness.database_selected_run_finished(
                generation,
                instance.logical_path,
                "guid-7",
                detail,
                None,
            )

    assert harness.infoBox.reentered
    expected_run_id = 9 if restore_reentry else 8
    expected_guid = f"guid-{expected_run_id}"
    assert harness.infoBox.restore_reentered is restore_reentry
    assert harness._selected_run_guid == expected_guid
    assert harness.selected_run_id == expected_run_id
    assert harness.infoBox.visible == ("loading", expected_guid)
    assert stale_key not in harness._selected_run_detail_cache
    assert workers[-1] is harness._database_selected_run_worker
    assert workers[-1].args[3] == expected_guid


def test_typed_run_id_clears_guid_and_cancels_stale_selected_detail():
    class Worker:
        def __init__(self) -> None:
            self.cancelled = False

        def cancel(self) -> None:
            self.cancelled = True

    class RunList:
        def __init__(self) -> None:
            self.signals_blocked = False
            self.selection_cleared = False

        def blockSignals(self, blocked: bool) -> bool:
            previous = self.signals_blocked
            self.signals_blocked = bool(blocked)
            return previous

        def clearSelection(self) -> None:
            self.selection_cleared = True

    class InfoBox:
        def __init__(self) -> None:
            self.events = []

        def clear(self) -> None:
            self.events.append("clear")

        def set_trusted_run_error(self, *_args) -> None:
            self.events.append("stale error")

    class Harness:
        update_run_id = run_controls.RunControlsMixin.update_run_id
        _cancel_selected_run_detail = (
            database_actions.DatabaseActionsMixin._cancel_selected_run_detail
        )
        database_selected_run_finished = (
            database_actions.DatabaseActionsMixin.database_selected_run_finished
        )
        open_selected_run_all = plot_actions.PlotActionsMixin.open_selected_run_all

        def __init__(self) -> None:
            self.RunList = RunList()
            self.infoBox = InfoBox()
            self.ds = object()
            self._selected_dataset_key = object()
            self._selected_run_guid = "guid-a"
            self.selected_run_id = 1
            self._database_selected_run_generation = 8
            self._database_selected_run_worker = Worker()
            self._database_selected_run_instance = _instance("typed.db", (1, 7))
            self.fileTextbox = _Field("typed.db")
            self.opened = []
            self.status_messages = []

        def openPlot(self, guid) -> None:
            self.opened.append(guid)

        def show_status(self, message: str, timeout: int = 5000) -> None:
            self.status_messages.append((message, timeout))

    harness = Harness()
    stale_worker = harness._database_selected_run_worker
    stale_generation = harness._database_selected_run_generation

    harness.update_run_id("2")
    harness.open_selected_run_all()
    harness.database_selected_run_finished(
        stale_generation,
        "typed.db",
        "guid-a",
        None,
        RuntimeError("stale selected-run error"),
    )

    assert stale_worker.cancelled
    assert harness._database_selected_run_generation == stale_generation + 1
    assert harness._database_selected_run_worker is None
    assert harness._database_selected_run_instance is None
    assert harness._selected_run_guid is None
    assert harness.selected_run_id == 2
    assert harness.ds is None
    assert harness._selected_dataset_key is None
    assert harness.RunList.selection_cleared
    assert harness.infoBox.events == ["clear"]
    assert harness.opened == []
    assert "Select a run" in harness.status_messages[-1][0]


def test_reusable_selected_summary_error_retains_and_retries_cheap_detail():
    instance = _instance("selected.db", (1, 7))
    service = _FakeService(instance)
    harness = _SelectionHarness(instance, service)
    workers = []

    def worker_factory(*args, **kwargs):
        worker = _FakeSelectedWorker(args, kwargs)
        workers.append(worker)
        return worker

    with (
        patch.object(
            database_actions,
            "DatabaseSelectedRunWorker",
            side_effect=worker_factory,
        ),
        patch.object(
            database_actions,
            "database_instance",
            return_value=instance,
        ),
        patch.object(database_actions, "log_exception"),
    ):
        assert harness._update_trusted_selected_run("guid-7")
        generation = harness._database_selected_run_generation
        detail = _selected_detail()
        harness.database_selected_run_progress(
            generation,
            instance.logical_path,
            "guid-7",
            detail,
        )
        error = TrustedLiveQueryError("injected summary scan failure")
        harness.database_selected_run_finished(
            generation,
            instance.logical_path,
            "guid-7",
            None,
            error,
        )

        assert all(event[0] != "error" for event in harness.infoBox.events)
        assert harness.infoBox.events[-1] == ("detail", detail)
        assert harness._selected_run_detail_cache == {
            database_actions._selected_run_detail_cache_key(
                instance, 7, "guid-7"
            ): detail,
        }
        assert harness._selected_run_partial_detail_keys == {
            database_actions._selected_run_detail_cache_key(instance, 7, "guid-7"),
        }
        assert "basic details remain" in harness.status_messages[-1][0]

        assert harness._update_trusted_selected_run("guid-7")

    assert len(workers) == 2
    assert harness.databaseDetailThreadPool.started == workers
    assert harness.infoBox.events[-1] == ("detail", detail)


def test_trusted_selection_rejects_cached_detail_after_main_replacement():
    instance = _instance("selected.db", (1, 7))
    replacement = _instance("selected.db", (1, 8))
    harness = _SelectionHarness(instance, _FakeService(instance))
    detail = _selected_detail()
    harness._selected_run_detail_cache[
        database_actions._selected_run_detail_cache_key(instance, 7, "guid-7")
    ] = detail

    with (
        patch.object(
            database_actions,
            "database_instance",
            return_value=replacement,
        ),
        patch.object(
            database_actions,
            "DatabaseSelectedRunWorker",
            side_effect=AssertionError("stale selection started a worker"),
        ),
    ):
        assert harness._update_trusted_selected_run("guid-7")

    assert harness.reloads == [instance.logical_path]
    assert harness.infoBox.events == []
    assert harness.databaseDetailThreadPool.started == []
    assert harness._selected_run_guid is None
    assert harness.selected_run_id is None


def test_trusted_selection_rejects_cached_detail_after_sidecar_aba():
    path = logical_database_path("selected.db")
    instance = DatabaseInstance(
        path,
        path,
        (1, 7),
        sidecar_identities=frozenset({(2, 11)}),
    )
    sidecar_replacement = DatabaseInstance(
        path,
        path,
        (1, 7),
        sidecar_identities=frozenset({(2, 12)}),
    )
    harness = _SelectionHarness(instance, _FakeService(instance))
    detail = _selected_detail()
    harness._selected_run_detail_cache[
        database_actions._selected_run_detail_cache_key(instance, 7, "guid-7")
    ] = detail

    with (
        patch.object(
            database_actions,
            "database_instance",
            return_value=sidecar_replacement,
        ),
        patch.object(
            database_actions,
            "DatabaseSelectedRunWorker",
            side_effect=AssertionError("stale selection started a worker"),
        ),
    ):
        assert harness._update_trusted_selected_run("guid-7")

    assert harness.reloads == [instance.logical_path]
    assert harness.infoBox.events == []
    assert harness.databaseDetailThreadPool.started == []
    assert harness._selected_run_guid is None
    assert harness.selected_run_id is None


def test_trusted_selection_accepts_wal_and_shm_appearing_after_acceptance():
    path = logical_database_path("selected.db")
    accepted = DatabaseInstance(path, path, (1, 7))
    with_new_sidecars = DatabaseInstance(
        path,
        path,
        (1, 7),
        sidecar_identities=frozenset({(2, 11), (2, 12)}),
    )
    harness = _SelectionHarness(accepted, _FakeService(accepted))
    detail = _selected_detail()
    harness._selected_run_detail_cache[
        database_actions._selected_run_detail_cache_key(accepted, 7, "guid-7")
    ] = detail
    workers = []

    def worker_factory(*args, **kwargs):
        worker = _FakeSelectedWorker(args, kwargs)
        workers.append(worker)
        return worker

    with (
        patch.object(
            database_actions,
            "database_instance",
            return_value=with_new_sidecars,
        ),
        patch.object(
            database_actions,
            "DatabaseSelectedRunWorker",
            side_effect=worker_factory,
        ),
    ):
        assert harness._update_trusted_selected_run("guid-7")
        assert not (
            database_actions.DatabaseActionsMixin._reload_if_worker_database_instance_changed(
                harness,
                accepted.logical_path,
                accepted,
            )
        )

    assert harness.reloads == []
    assert harness._loaded_database_instance == with_new_sidecars
    assert harness.infoBox.events[-1][0] == "loading"
    assert harness.databaseDetailThreadPool.started == workers
    assert len(workers) == 1
    assert database_actions._selected_run_detail_cache_key(
        accepted, 7, "guid-7"
    ) != database_actions._selected_run_detail_cache_key(with_new_sidecars, 7, "guid-7")


@pytest.mark.parametrize(
    "later_sidecars",
    [frozenset({(2, 12)}), frozenset()],
    ids=["replacement", "removal"],
)
def test_new_sidecar_baseline_rejects_its_later_replacement_or_removal(
    later_sidecars,
):
    path = logical_database_path("sidecar-baseline.db")
    accepted = DatabaseInstance(path, path, (1, 7))
    first_wal = DatabaseInstance(
        path,
        path,
        (1, 7),
        sidecar_identities=frozenset({(2, 11)}),
    )
    later = DatabaseInstance(
        path,
        path,
        (1, 7),
        sidecar_identities=later_sidecars,
    )
    harness = _SelectionHarness(accepted, _FakeService(accepted))

    with patch.object(
        database_actions,
        "database_instance",
        return_value=first_wal,
    ):
        assert not harness._reload_if_database_instance_changed(path)

    assert harness._loaded_database_instance == first_wal

    with patch.object(
        database_actions,
        "database_instance",
        return_value=later,
    ):
        assert harness._reload_if_database_instance_changed(path)

    assert harness.reloads == [path]


@pytest.mark.parametrize("callback_phase", ["progress", "finished"])
@pytest.mark.parametrize("replacement_kind", ["main", "sidecar"])
def test_trusted_selected_callback_rejects_post_query_source_aba(
    callback_phase,
    replacement_kind,
):
    path = logical_database_path("selected-callback.db")
    accepted = DatabaseInstance(
        path,
        path,
        (1, 7),
        sidecar_identities=frozenset({(2, 11)}),
    )
    replacement = DatabaseInstance(
        path,
        path,
        (1, 8) if replacement_kind == "main" else (1, 7),
        sidecar_identities=(
            accepted.sidecar_identities
            if replacement_kind == "main"
            else frozenset({(2, 12)})
        ),
    )

    class CallbackHarness(_SelectionHarness):
        _reload_if_worker_database_instance_changed = database_actions.DatabaseActionsMixin._reload_if_worker_database_instance_changed

    harness = CallbackHarness(accepted, _FakeService(accepted))
    harness._selected_run_guid = "guid-7"
    harness.selected_run_id = 7
    harness._database_selected_run_instance = accepted

    with patch.object(
        database_actions,
        "database_instance",
        return_value=replacement,
    ):
        if callback_phase == "progress":
            harness.database_selected_run_progress(
                harness._database_selected_run_generation,
                accepted.logical_path,
                "guid-7",
                _selected_detail(),
            )
        else:
            harness.database_selected_run_finished(
                harness._database_selected_run_generation,
                accepted.logical_path,
                "guid-7",
                _selected_detail(),
                None,
            )

    assert harness.reloads == [accepted.logical_path]
    assert harness.infoBox.events == []
    assert harness.RunList.updated == []
    assert harness._selected_run_detail_cache == {}


def test_trusted_selected_callback_rechecks_after_widget_publication(tmp_path):
    database_path, wal_path, replacement_wal, accepted = _sidecar_swap_fixture(
        tmp_path,
        "selected-widget-callback",
    )
    harness = _SelectionHarness(accepted, _FakeService(accepted))
    harness._selected_run_guid = "guid-7"
    harness.selected_run_id = 7
    harness._database_selected_run_instance = accepted
    harness.fileTextbox.value = os.path.join(
        os.fspath(tmp_path),
        "equivalent-spelling",
        os.pardir,
        database_path.name,
    )

    original_set_detail = harness.infoBox.set_trusted_run_detail

    def set_detail_and_replace(detail):
        original_set_detail(detail)
        replacement_wal.replace(wal_path)

    harness.infoBox.set_trusted_run_detail = set_detail_and_replace

    harness.database_selected_run_progress(
        harness._database_selected_run_generation,
        accepted.logical_path,
        "guid-7",
        _selected_detail(),
    )

    assert harness.reloads == [accepted.logical_path]
    assert harness.infoBox.visible is None
    assert harness.RunList.updated
    assert harness.RunList.all_run_metadata() == {}
    assert harness.selected_run_id is None
    assert harness._selected_run_guid is None
    assert harness._selected_run_detail_cache == {}
    assert harness._selected_run_partial_detail_keys == set()
    assert harness._snapshot_setpoint_summary_cache == {}
    assert not any(
        message.startswith("Selected run") for message, _ in harness.status_messages
    )


@pytest.mark.parametrize(
    ("callback_name", "generation_name", "active_name", "instance_name"),
    [
        (
            "database_detail_batch_ready",
            "_database_detail_generation",
            "_database_detail_active",
            "_database_detail_instance",
        ),
        (
            "database_expensive_detail_batch_ready",
            "_database_expensive_detail_generation",
            "_database_expensive_detail_active",
            "_database_expensive_detail_instance",
        ),
    ],
)
@pytest.mark.parametrize("worker_instance_bound", [True, False])
def test_trusted_detail_callback_rechecks_after_row_publication(
    tmp_path,
    callback_name,
    generation_name,
    active_name,
    instance_name,
    worker_instance_bound,
):
    database_path, wal_path, replacement_wal, accepted = _sidecar_swap_fixture(
        tmp_path,
        callback_name,
    )

    class Harness(database_actions.DatabaseActionsMixin):
        def __init__(self):
            self.fileTextbox = _Field(str(database_path))
            self._loaded_database_instance = accepted
            self._loaded_database_identity = accepted.identity
            self._snapshot_setpoint_summary_cache = {"old": object()}
            self.RunList = _DiscardableRunList()
            self.applied = []
            self.reloads = []
            setattr(self, generation_name, 19)
            setattr(self, active_name, True)
            setattr(
                self,
                instance_name,
                accepted if worker_instance_bound else None,
            )

        def _apply_database_detail_batch(self, runs) -> None:
            self.applied.append(dict(runs))
            self.RunList.visible = dict(runs)
            replacement_wal.replace(wal_path)

        def _reload_replaced_database(self, path: str) -> None:
            self.reloads.append(path)

    harness = Harness()
    runs = {7: {"guid": "guid-7", "result_count": 12}}

    getattr(harness, callback_name)(19, str(database_path), runs)

    assert harness.applied == [runs]
    assert harness.RunList.visible == {}
    assert harness._snapshot_setpoint_summary_cache == {}
    assert not harness.RunList.signals_blocked
    assert harness.reloads == [str(database_path)]


def test_trusted_refresh_callback_rechecks_after_final_view_publication(tmp_path):
    database_path, wal_path, replacement_wal, accepted = _sidecar_swap_fixture(
        tmp_path,
        "refresh-final-callback",
    )

    class Harness(database_actions.DatabaseActionsMixin):
        def __init__(self):
            self.fileTextbox = _Field(str(database_path))
            self._loaded_database_instance = accepted
            self._loaded_database_identity = accepted.identity
            self._snapshot_setpoint_summary_cache = {"old": object()}
            self._database_refresh_generation = 23
            self._database_refresh_active = True
            self._database_refresh_pending = False
            self._database_refresh_worker = object()
            self._database_refresh_identity = accepted.identity
            self._database_refresh_instance = accepted
            self._database_refresh_staged_new_runs = {}
            self.RunList = _DiscardableRunList()
            self.applied = []
            self.reloads = []

        def _apply_database_refresh_result(self, new_runs, statuses) -> None:
            self.applied.append((dict(new_runs), dict(statuses)))
            self.RunList.visible = {
                **dict(new_runs),
                "statuses": dict(statuses),
            }
            replacement_wal.replace(wal_path)

        def _reload_replaced_database(self, path: str) -> None:
            self.reloads.append(path)

    harness = Harness()
    new_runs = {8: {"guid": "guid-8"}}
    statuses = {"guid-7": {"result_count": 13}}

    harness.database_refresh_finished(
        23,
        str(database_path),
        new_runs,
        statuses,
        None,
    )

    assert harness.applied == [(new_runs, statuses)]
    assert harness.RunList.visible == {}
    assert harness._snapshot_setpoint_summary_cache == {}
    assert not harness.RunList.signals_blocked
    assert harness.reloads == [str(database_path)]
    assert not harness._database_refresh_active


def test_trusted_selection_ignores_stale_detail_then_renders_current_error():
    instance = _instance("selected.db", (1, 7))
    service = _FakeService(instance)
    harness = _SelectionHarness(instance, service)

    with (
        patch.object(
            database_actions,
            "DatabaseSelectedRunWorker",
            side_effect=lambda *args, **kwargs: _FakeSelectedWorker(args, kwargs),
        ),
        patch.object(
            database_actions,
            "database_instance",
            return_value=instance,
        ),
        patch.object(database_actions, "log_exception"),
    ):
        assert harness._update_trusted_selected_run("guid-7")
        generation = harness._database_selected_run_generation
        active_worker = harness._database_selected_run_worker

        harness.database_selected_run_progress(
            generation - 1,
            instance.logical_path,
            "guid-7",
            _selected_detail(),
        )
        harness.database_selected_run_finished(
            generation - 1,
            instance.logical_path,
            "guid-7",
            _selected_detail(),
            None,
        )
        assert harness.infoBox.events[0][0] == "loading"
        assert len(harness.infoBox.events) == 1
        assert harness._database_selected_run_worker is active_worker

        error = RuntimeError("bounded trusted detail failure")
        harness.database_selected_run_finished(
            generation,
            instance.logical_path,
            "guid-7",
            None,
            error,
        )

    assert harness.infoBox.events[-1][0:2] == (
        "error",
        "bounded trusted detail failure",
    )
    assert harness.RunList.updated == []
    assert harness._selected_run_detail_cache == {}
    assert harness._database_selected_run_worker is None
    assert harness.status_messages[-1][0].startswith("Selected-run details failed:")


def test_trusted_selection_never_falls_through_for_missing_or_malformed_rows():
    instance = _instance("selected.db", (1, 7))
    harness = _SelectionHarness(instance, _FakeService(instance))

    with patch.object(
        harness,
        "_load_dataset",
        side_effect=AssertionError("trusted selection reached snapshot DataSet I/O"),
    ) as load_dataset:
        plot_actions.PlotActionsMixin.updateSelected(harness, "missing-guid")
        harness.RunList.run_id = "not-a-run-id"
        plot_actions.PlotActionsMixin.updateSelected(harness, "guid-7")

    load_dataset.assert_not_called()
    assert harness.ds is not None
    assert harness.status_messages == []


@pytest.mark.parametrize("unusable_state", ["closing", "closed"])
def test_lost_trusted_service_cannot_start_snapshot_detail_workers(unusable_state):
    instance = _instance("details.db", (1, 8))
    service = _FakeService(instance)
    setattr(service, unusable_state, True)

    class Harness(database_actions.DatabaseActionsMixin):
        def __init__(self) -> None:
            self._test_database_replacement_state = None
            self._database_access_mode = database_actions.TRUSTED_LIVE_MODE
            self._loaded_database_instance = instance
            self._trusted_read_service = service
            self._database_detail_generation = 4
            self._database_detail_active = False
            self._database_detail_worker = None
            self._database_detail_instance = None
            self._database_expensive_detail_generation = 5
            self._database_expensive_detail_active = False
            self._database_expensive_detail_worker = None
            self._database_expensive_detail_instance = None
            self.error_messages = []

        def show_error(self, title: str, message: str, details=None) -> None:
            self.error_messages.append((title, message, details))

    harness = Harness()
    worker_kwargs = {"expected_database_instance": instance}
    with (
        patch.object(database_actions, "DatabaseDetailWorker") as cheap_worker,
        patch.object(
            database_actions,
            "DatabaseExpensiveDetailWorker",
        ) as expensive_worker,
    ):
        harness._start_database_detail_load(
            instance.logical_path,
            {1: {"guid": "guid-1"}},
        )
        harness._start_database_cheap_detail_worker(
            instance.logical_path,
            [1],
            instance,
            worker_kwargs,
        )
        harness._start_database_expensive_detail_worker(
            instance.logical_path,
            [1],
            instance,
            worker_kwargs,
        )

    cheap_worker.assert_not_called()
    expensive_worker.assert_not_called()
    assert not harness._database_detail_active
    assert not harness._database_expensive_detail_active
    assert harness.error_messages == [
        (
            "Trusted Details Unavailable",
            "The accepted trusted live-reader session is no longer usable.",
            "Reload the database to start a new trusted session. qPlot did not "
            "fall back to snapshot metadata after the accepted-session failure.",
        )
    ]


def test_retired_service_is_owned_until_broker_threads_really_exit():
    instance = _instance("retiring.db", (1, 9))
    control_body_done = threading.Event()
    release_control_thread = threading.Event()
    original_control_loop = TrustedLiveReadService._control_loop

    def held_control_loop(service) -> None:
        original_control_loop(service)
        control_body_done.set()
        release_control_thread.wait(3)

    service = None
    try:
        with patch.object(
            TrustedLiveReadService,
            "_control_loop",
            held_control_loop,
        ):
            service = TrustedLiveReadService(
                instance.logical_path,
                expected_database_instance=instance,
            )
        owner = SimpleNamespace(_retired_trusted_read_services={service})
        service.close_async()
        assert control_body_done.wait(2)
        assert service.closed
        assert not service.wait_closed(0)

        database_actions.DatabaseActionsMixin._reap_retired_trusted_read_services(owner)
        assert owner._retired_trusted_read_services == {service}

        release_control_thread.set()
        assert service.wait_closed(2)
        database_actions.DatabaseActionsMixin._reap_retired_trusted_read_services(owner)
        assert owner._retired_trusted_read_services == set()
    finally:
        release_control_thread.set()
        if service is not None:
            service.close_async()
            assert service.wait_closed(3)


def test_runtime_reaper_releases_delayed_quarantine_without_database_switch():
    class DelayedService:
        def __init__(self) -> None:
            self.closed = False
            self.close_async_calls = 0
            self.wait_timeouts = []

        def close_async(self) -> None:
            self.close_async_calls += 1

        def wait_closed(self, timeout: float) -> bool:
            self.wait_timeouts.append(timeout)
            return self.closed

    service = DelayedService()
    owner = SimpleNamespace(
        _trusted_read_service=None,
        _retired_trusted_read_services=set(),
        _retired_service_reap_diagnostics={},
        _retired_service_reaper_timer=QtCore.QTimer(),
    )
    owner._retired_service_reaper_timer.setInterval(5)
    owner._retired_service_reaper_timer.timeout.connect(
        lambda: (
            database_actions.DatabaseActionsMixin._reap_retired_trusted_read_services(
                owner
            )
        )
    )

    database_actions.DatabaseActionsMixin._retire_trusted_read_service(
        owner,
        service,
    )
    assert owner._retired_service_reaper_timer.isActive()
    assert owner._retired_trusted_read_services == {service}

    QtCore.QTimer.singleShot(20, lambda: setattr(service, "closed", True))
    deadline = time.monotonic() + 1.0
    while owner._retired_trusted_read_services and time.monotonic() < deadline:
        qtw.QApplication.processEvents()
        QtTest.QTest.qWait(5)

    assert owner._retired_trusted_read_services == set()
    assert not owner._retired_service_reaper_timer.isActive()
    assert service.close_async_calls == 1
    assert service.wait_timeouts
    assert set(service.wait_timeouts) == {0}


def test_active_service_is_retained_until_pending_database_succeeds():
    instance_a = _instance("database-a.db", (1, 1))
    instance_b = _instance("database-b.db", (1, 2))
    service_a = _FakeService(instance_a)
    service_b = _FakeService(instance_b, accepted=False)
    worker_b = _FakeLoadWorker(service_b)
    harness = _LifecycleHarness(instance_a, service_a)
    configured_paths = []

    instances = {
        instance_a.logical_path: instance_a,
        instance_b.logical_path: instance_b,
    }

    def observed_instance(path):
        return instances[logical_database_path(path)]

    with (
        patch.object(
            database_actions, "get_DB_location", return_value=instance_a.logical_path
        ),
        patch.object(
            database_actions, "database_instance", side_effect=observed_instance
        ),
        patch.object(database_actions, "DatabaseLoadWorker", return_value=worker_b),
        patch.object(
            database_actions,
            "set_qcodes_database_location",
            side_effect=configured_paths.append,
        ),
        patch.object(database_actions, "log_event"),
    ):
        assert harness.load_file(instance_b.logical_path)
        generation = harness._database_load_generation

        assert harness._trusted_read_service is service_a
        assert service_a.close_async_calls == 0
        assert harness._pending_trusted_read_services == {generation: service_b}
        assert harness.fileTextbox.text() == instance_a.logical_path

        service_b.accepted = True
        new_runs = {9: {"guid": "guid-b", "run_timestamp": 9.0}}
        harness.database_load_finished(
            generation,
            instance_b.logical_path,
            new_runs,
            None,
            worker_b,
        )

    assert harness._trusted_read_service is service_b
    assert harness._pending_trusted_read_services == {}
    assert service_b.close_async_calls == 0
    assert service_a.close_async_calls == 1
    assert service_a in harness._retired_trusted_read_services
    assert harness.fileTextbox.text() == instance_b.logical_path
    assert harness.RunList.runs == new_runs
    assert harness.infoBox.preview.database_runs == ("", {})
    assert configured_paths == [instance_b.logical_path]
    assert harness.detail_loads == [(instance_b.logical_path, new_runs)]


def test_trusted_service_accepts_exact_shm_appearing_during_open():
    path = logical_database_path("database-shm.db")
    pre_open = DatabaseInstance(path, path, (1, 2))
    post_open = DatabaseInstance(
        path,
        path,
        (1, 2),
        sidecar_identities=frozenset({(2, 11)}),
    )
    service = _FakeService(pre_open)
    harness = _LifecycleHarness(post_open, service)

    assert harness._active_trusted_service_for_instance(post_open) is service


def test_pending_load_accepts_shm_then_wal_appearing_during_publication():
    instance_a = _instance("database-a.db", (1, 1))
    path_b = logical_database_path("database-b-shm.db")
    instance_b_pre_open = DatabaseInstance(path_b, path_b, (1, 2))
    instance_b_post_open = DatabaseInstance(
        path_b,
        path_b,
        (1, 2),
        sidecar_identities=frozenset({(2, 11)}),
    )
    instance_b_during_publication = DatabaseInstance(
        path_b,
        path_b,
        (1, 2),
        sidecar_identities=frozenset({(2, 11), (2, 12)}),
    )
    service_a = _FakeService(instance_a)
    service_b = _FakeService(instance_b_pre_open, accepted=False)
    worker_b = _FakeLoadWorker(service_b)
    harness = _LifecycleHarness(instance_a, service_a)
    b_observations = iter(
        (
            instance_b_pre_open,
            instance_b_post_open,
            instance_b_during_publication,
            instance_b_during_publication,
        )
    )

    def observed_instance(path):
        if logical_database_path(path) == instance_a.logical_path:
            return instance_a
        return next(b_observations)

    with (
        patch.object(
            database_actions,
            "get_DB_location",
            return_value=instance_a.logical_path,
        ),
        patch.object(
            database_actions,
            "database_instance",
            side_effect=observed_instance,
        ),
        patch.object(database_actions, "DatabaseLoadWorker", return_value=worker_b),
        patch.object(database_actions, "set_qcodes_database_location"),
        patch.object(database_actions, "log_event"),
    ):
        assert harness.load_file(path_b)
        generation = harness._database_load_generation
        service_b.accepted = True
        new_runs = {9: {"guid": "guid-b", "run_timestamp": 9.0}}
        harness.database_load_finished(
            generation,
            path_b,
            new_runs,
            None,
            worker_b,
        )

    assert harness._trusted_read_service is service_b
    assert harness._loaded_database_instance == instance_b_post_open
    assert service_b.close_async_calls == 0
    assert service_a.close_async_calls == 1
    assert harness.RunList.runs == new_runs


@pytest.mark.parametrize(
    "later_sidecars",
    (frozenset(), frozenset({(2, 12)})),
    ids=("removed", "replaced"),
)
def test_pending_load_rejects_accepted_sidecar_change_before_gui_publication(
    later_sidecars,
):
    instance_a = _instance("database-a.db", (1, 1))
    path_b = logical_database_path("database-b-callback-sidecar.db")
    instance_b_accepted = DatabaseInstance(
        path_b,
        path_b,
        (1, 2),
        sidecar_identities=frozenset({(2, 11)}),
    )
    instance_b_changed = DatabaseInstance(
        path_b,
        path_b,
        (1, 2),
        sidecar_identities=later_sidecars,
    )
    service_a = _FakeService(instance_a)
    service_b = _FakeService(instance_b_accepted, accepted=False)
    worker_b = _FakeLoadWorker(service_b)
    harness = _LifecycleHarness(instance_a, service_a)
    b_observations = iter((instance_b_accepted, instance_b_changed))
    retry_callbacks = []

    def observed_instance(path):
        if logical_database_path(path) == instance_a.logical_path:
            return instance_a
        return next(b_observations)

    with (
        patch.object(
            database_actions,
            "get_DB_location",
            return_value=instance_a.logical_path,
        ),
        patch.object(
            database_actions,
            "database_instance",
            side_effect=observed_instance,
        ),
        patch.object(database_actions, "DatabaseLoadWorker", return_value=worker_b),
        patch.object(
            database_actions.QtCore.QTimer,
            "singleShot",
            side_effect=lambda _delay, callback: retry_callbacks.append(callback),
        ),
        patch.object(database_actions, "set_qcodes_database_location") as configure,
        patch.object(database_actions, "log_event"),
    ):
        assert harness.load_file(path_b)
        generation = harness._database_load_generation
        service_b.accepted = True
        harness.database_load_finished(
            generation,
            path_b,
            {9: {"guid": "stale-guid-b", "run_timestamp": 9.0}},
            None,
            worker_b,
        )

    configure.assert_not_called()
    assert harness._trusted_read_service is service_a
    assert harness._loaded_database_instance == instance_a
    assert harness.fileTextbox.text() == instance_a.logical_path
    assert harness.RunList.all_run_metadata() == {
        1: {"guid": "guid-a", "run_timestamp": 1.0},
    }
    assert service_a.close_async_calls == 0
    assert service_b.close_async_calls == 1
    assert service_b in harness._retired_trusted_read_services
    assert len(retry_callbacks) == 1


def test_pending_load_rejects_sidecar_swap_after_staging():
    instance_a = _instance("database-a.db", (1, 1))
    path_b = logical_database_path("database-b-sidecar-swap.db")
    instance_b_accepted = DatabaseInstance(path_b, path_b, (1, 2))
    instance_b_staged = DatabaseInstance(
        path_b,
        path_b,
        (1, 2),
        sidecar_identities=frozenset({(2, 11)}),
    )
    instance_b_swapped = DatabaseInstance(
        path_b,
        path_b,
        (1, 2),
        sidecar_identities=frozenset({(2, 12)}),
    )
    service_a = _FakeService(instance_a)
    service_b = _FakeService(instance_b_accepted, accepted=False)
    worker_b = _FakeLoadWorker(service_b)
    harness = _LifecycleHarness(instance_a, service_a)
    b_observations = iter(
        (
            instance_b_accepted,
            instance_b_accepted,
            instance_b_staged,
            instance_b_swapped,
        )
    )

    def observed_instance(path):
        if logical_database_path(path) == instance_a.logical_path:
            return instance_a
        return next(b_observations)

    with (
        patch.object(
            database_actions,
            "get_DB_location",
            return_value=instance_a.logical_path,
        ),
        patch.object(
            database_actions,
            "database_instance",
            side_effect=observed_instance,
        ),
        patch.object(database_actions, "DatabaseLoadWorker", return_value=worker_b),
        patch.object(database_actions, "set_qcodes_database_location"),
        patch.object(database_actions, "log_event"),
        patch.object(harness, "_reload_replaced_database") as reload_replaced,
    ):
        assert harness.load_file(path_b)
        generation = harness._database_load_generation
        service_b.accepted = True
        harness.database_load_finished(
            generation,
            path_b,
            {9: {"guid": "guid-b", "run_timestamp": 9.0}},
            None,
            worker_b,
        )

    reload_replaced.assert_called_once_with(
        path_b,
        generation_recovery=False,
    )
    assert harness._loaded_database_instance == instance_b_staged


def test_trusted_same_instance_shortcut_keeps_preview_source_disabled():
    instance = _instance("database-a.db", (1, 1))
    service = _FakeService(instance)
    harness = _LifecycleHarness(instance, service)
    harness.infoBox.preview.set_database_runs("", {})

    with (
        patch.object(
            database_actions,
            "get_DB_location",
            return_value=instance.logical_path,
        ),
        patch.object(
            database_actions,
            "database_instance",
            return_value=instance,
        ),
        patch.object(database_actions, "log_event"),
    ):
        assert harness.load_file(instance.logical_path)

    assert harness.databaseLoadThreadPool.started == []
    assert harness.infoBox.preview.database_runs == ("", {})
    assert service.close_async_calls == 0


def test_cancelled_forced_same_instance_load_does_not_mutate_active_adapter():
    instance = _instance("database-a.db", (1, 1))
    active_service = _FakeService(instance)
    active_service.adapter_cache = {1: "committed selected-run detail"}
    active_service.adapter_cursor = 17
    pending_service = _FakeService(instance, accepted=False)
    pending_service.adapter_cache = {2: "tentative detail"}
    pending_service.adapter_cursor = 23
    harness = _LifecycleHarness(instance, active_service)

    def make_worker(*_args, trusted_service=None, **_kwargs):
        return _FakeLoadWorker(trusted_service or pending_service)

    with (
        patch.object(
            database_actions,
            "get_DB_location",
            return_value=instance.logical_path,
        ),
        patch.object(
            database_actions,
            "database_instance",
            return_value=instance,
        ),
        patch.object(database_actions, "DatabaseLoadWorker", side_effect=make_worker),
        patch.object(database_actions, "log_event"),
    ):
        assert harness.load_file(instance.logical_path, force=True)
        generation = harness._database_load_generation
        worker = harness.databaseLoadThreadPool.started[0]
        assert worker.trusted_service is pending_service
        assert harness._pending_trusted_read_services == {
            generation: pending_service,
        }
        assert harness._database_load_state["owns_trusted_service"] is True

        # Model the destructive reset performed by submit_bootstrap().  It is
        # safe only because the worker owns an isolated pending adapter.
        worker.trusted_service.adapter_cache.clear()
        worker.trusted_service.adapter_cursor = 0

        harness.cancel_database_load()

    assert worker.cancelled
    assert harness._trusted_read_service is active_service
    assert active_service.adapter_cache == {1: "committed selected-run detail"}
    assert active_service.adapter_cursor == 17
    assert active_service.close_async_calls == 0
    assert pending_service.close_async_calls == 1
    assert pending_service in harness._retired_trusted_read_services


def test_successful_forced_same_instance_load_commits_isolated_service():
    instance = _instance("database-a.db", (1, 1))
    active_service = _FakeService(instance)
    pending_service = _FakeService(instance, accepted=False)
    worker = _FakeLoadWorker(pending_service)
    harness = _LifecycleHarness(instance, active_service)
    configured_paths = []
    reloaded_runs = {2: {"guid": "guid-a2", "run_timestamp": 2.0}}

    with (
        patch.object(
            database_actions,
            "get_DB_location",
            return_value=instance.logical_path,
        ),
        patch.object(
            database_actions,
            "database_instance",
            return_value=instance,
        ),
        patch.object(database_actions, "DatabaseLoadWorker", return_value=worker),
        patch.object(
            database_actions,
            "set_qcodes_database_location",
            side_effect=configured_paths.append,
        ),
        patch.object(database_actions, "log_event"),
    ):
        assert harness.load_file(instance.logical_path, force=True)
        generation = harness._database_load_generation
        pending_service.accepted = True
        harness.database_load_finished(
            generation,
            instance.logical_path,
            reloaded_runs,
            None,
            worker,
        )

    assert harness._trusted_read_service is pending_service
    assert active_service.close_async_calls == 1
    assert active_service in harness._retired_trusted_read_services
    assert pending_service.close_async_calls == 0
    assert harness._pending_trusted_read_services == {}
    assert harness._database_access_mode == database_actions.TRUSTED_LIVE_MODE
    assert harness._loaded_database_instance == instance
    assert harness.fileTextbox.text() == instance.logical_path
    assert harness.RunList.all_run_metadata() == reloaded_runs
    assert configured_paths == [instance.logical_path]


def test_failed_forced_same_instance_load_does_not_retire_active_service():
    instance = _instance("database-a.db", (1, 1))
    active_service = _FakeService(instance)
    active_service.adapter_cache = {1: "committed selected-run detail"}
    pending_service = _FakeService(instance, accepted=False)
    pending_service.adapter_cache = {2: "tentative detail"}
    worker = _FakeLoadWorker(pending_service)
    harness = _LifecycleHarness(instance, active_service)
    load_error = RuntimeError("injected same-instance reload failure")

    with (
        patch.object(
            database_actions,
            "get_DB_location",
            return_value=instance.logical_path,
        ),
        patch.object(
            database_actions,
            "database_instance",
            return_value=instance,
        ),
        patch.object(database_actions, "DatabaseLoadWorker", return_value=worker),
        patch.object(database_actions, "log_event"),
        patch.object(database_actions, "log_exception"),
    ):
        assert harness.load_file(instance.logical_path, force=True)
        generation = harness._database_load_generation
        worker.trusted_service.adapter_cache.clear()
        harness.database_load_finished(
            generation,
            instance.logical_path,
            {},
            load_error,
            worker,
        )

    assert harness._trusted_read_service is active_service
    assert active_service.adapter_cache == {1: "committed selected-run detail"}
    assert active_service.close_async_calls == 0
    assert pending_service.close_async_calls == 1
    assert pending_service in harness._retired_trusted_read_services
    assert harness.fileTextbox.text() == instance.logical_path
    assert harness.RunList.all_run_metadata() == {
        1: {"guid": "guid-a", "run_timestamp": 1.0},
    }
    assert harness.error_messages[-1] == (
        "Database Load Failed",
        f"Could not load database {instance.logical_path}.",
        str(load_error),
    )


def test_stale_forced_same_instance_callback_does_not_retire_active_service():
    instance = _instance("database-a.db", (1, 1))
    active_service = _FakeService(instance)
    pending_service = _FakeService(instance, accepted=False)
    worker = _FakeLoadWorker(pending_service)
    harness = _LifecycleHarness(instance, active_service)

    with (
        patch.object(
            database_actions,
            "get_DB_location",
            return_value=instance.logical_path,
        ),
        patch.object(
            database_actions,
            "database_instance",
            return_value=instance,
        ),
        patch.object(database_actions, "DatabaseLoadWorker", return_value=worker),
        patch.object(database_actions, "log_event"),
    ):
        assert harness.load_file(instance.logical_path, force=True)
        stale_generation = harness._database_load_generation
        harness._database_load_generation += 1
        harness.database_load_finished(
            stale_generation,
            instance.logical_path,
            {2: {"guid": "stale-guid"}},
            None,
            worker,
        )

    assert harness._trusted_read_service is active_service
    assert active_service.close_async_calls == 0
    assert pending_service.close_async_calls == 1
    assert pending_service in harness._retired_trusted_read_services
    assert harness._pending_trusted_read_services == {}
    assert harness.fileTextbox.text() == instance.logical_path
    assert harness.RunList.all_run_metadata() == {
        1: {"guid": "guid-a", "run_timestamp": 1.0},
    }


def test_trusted_same_instance_with_lost_service_starts_a_new_session():
    instance = _instance("database-a.db", (1, 1))
    lost_service = _FakeService(instance)
    lost_service.closed = True
    replacement_service = _FakeService(instance, accepted=False)
    replacement_worker = _FakeLoadWorker(replacement_service)
    harness = _LifecycleHarness(instance, lost_service)

    with (
        patch.object(
            database_actions,
            "get_DB_location",
            return_value=instance.logical_path,
        ),
        patch.object(
            database_actions,
            "database_instance",
            return_value=instance,
        ),
        patch.object(
            database_actions,
            "DatabaseLoadWorker",
            return_value=replacement_worker,
        ),
        patch.object(database_actions, "log_event"),
    ):
        assert harness.load_file(instance.logical_path)

    assert harness.databaseLoadThreadPool.started == [replacement_worker]
    assert harness._trusted_read_service is lost_service
    assert harness._pending_trusted_read_services == {
        harness._database_load_generation: replacement_service,
    }


def test_failed_b_ui_publication_restores_a_and_retires_only_b():
    instance_a = _instance("database-a.db", (1, 1))
    instance_b = _instance("database-b.db", (1, 2))
    service_a = _FakeService(instance_a)
    service_b = _FakeService(instance_b, accepted=False)
    worker_b = _FakeLoadWorker(service_b)
    harness = _LifecycleHarness(instance_a, service_a)
    old_runs = harness.RunList.all_run_metadata()
    old_dataset = harness.ds
    old_dataset_key = harness._selected_dataset_key
    configured_path = [instance_a.logical_path]
    original_add_runs = harness.RunList.addRuns
    failed_once = False

    def add_runs(runs, **kwargs) -> bool:
        nonlocal failed_once
        if 9 in runs and not failed_once:
            failed_once = True
            raise RuntimeError("injected B run-list publication failure")
        return original_add_runs(runs, **kwargs)

    def configured_database() -> str:
        return configured_path[-1]

    def configure_database(path: str) -> None:
        configured_path.append(path)

    instances = {
        instance_a.logical_path: instance_a,
        instance_b.logical_path: instance_b,
    }
    harness.RunList.addRuns = add_runs
    with (
        patch.object(
            database_actions,
            "get_DB_location",
            side_effect=configured_database,
        ),
        patch.object(
            database_actions,
            "database_instance",
            side_effect=lambda path: instances[logical_database_path(path)],
        ),
        patch.object(database_actions, "DatabaseLoadWorker", return_value=worker_b),
        patch.object(
            database_actions,
            "set_qcodes_database_location",
            side_effect=configure_database,
        ),
        patch.object(database_actions, "log_event"),
        patch.object(database_actions, "log_exception"),
    ):
        assert harness.load_file(instance_b.logical_path)
        generation = harness._database_load_generation
        service_b.accepted = True
        harness.database_load_finished(
            generation,
            instance_b.logical_path,
            {9: {"guid": "guid-b", "run_timestamp": 9.0}},
            None,
            worker_b,
        )

    assert configured_path[-1] == instance_a.logical_path
    assert harness._trusted_read_service is service_a
    assert harness._database_access_mode == database_actions.TRUSTED_LIVE_MODE
    assert harness._loaded_database_instance == instance_a
    assert harness.fileTextbox.text() == instance_a.logical_path
    assert harness.RunList.all_run_metadata() == old_runs
    assert [watcher.run_id for watcher in harness.RunList.watching] == [1]
    assert all(watcher.valid for watcher in harness.RunList.watching)
    assert harness.infoBox.preview.database_runs == (instance_a.logical_path, old_runs)
    assert harness.ds is old_dataset
    assert harness._selected_dataset_key is old_dataset_key
    assert service_a.close_async_calls == 0
    assert service_b.close_async_calls == 1
    assert service_b in harness._retired_trusted_read_services
    assert not getattr(harness, "_database_load_publication_active", False)
    assert harness.error_messages[-1] == (
        "Database Load Failed",
        f"Could not publish database {instance_b.logical_path}.",
        "injected B run-list publication failure",
    )


def test_rollback_failure_retires_b_and_leaves_a_view_safely_closed():
    instance_a = _instance("database-a.db", (1, 1))
    instance_b = _instance("database-b.db", (1, 2))
    service_a = _FakeService(instance_a)
    service_b = _FakeService(instance_b, accepted=False)
    worker_b = _FakeLoadWorker(service_b)
    harness = _LifecycleHarness(instance_a, service_a)
    rollback_error = RuntimeError("injected rollback failure")

    def fail_b_publication(runs, **_kwargs) -> bool:
        if 9 in runs:
            raise RuntimeError("injected B publication failure")
        return True

    def fail_partial_rollback(*_args, **_kwargs):
        # Model rollback failing after the controller no longer exposes A as
        # its active service.  A must still be retained through the snapshot.
        harness._trusted_read_service = service_b
        raise rollback_error

    instances = {
        instance_a.logical_path: instance_a,
        instance_b.logical_path: instance_b,
    }
    harness.RunList.addRuns = fail_b_publication
    with (
        patch.object(
            database_actions,
            "get_DB_location",
            return_value=instance_a.logical_path,
        ),
        patch.object(
            database_actions,
            "database_instance",
            side_effect=lambda path: instances[logical_database_path(path)],
        ),
        patch.object(database_actions, "DatabaseLoadWorker", return_value=worker_b),
        patch.object(
            database_actions.DatabaseActionsMixin,
            "_restore_database_load_publication",
            side_effect=fail_partial_rollback,
        ),
        patch.object(database_actions, "log_event"),
        patch.object(database_actions, "log_exception"),
    ):
        assert harness.load_file(instance_b.logical_path)
        generation = harness._database_load_generation
        service_b.accepted = True
        harness.database_load_finished(
            generation,
            instance_b.logical_path,
            {9: {"guid": "guid-b", "run_timestamp": 9.0}},
            None,
            worker_b,
        )

    assert harness._trusted_read_service is None
    assert harness._pending_trusted_read_services == {}
    assert service_a.close_async_calls == 1
    assert service_b.close_async_calls == 1
    assert {service_a, service_b}.issubset(harness._retired_trusted_read_services)
    assert harness.fileTextbox.text() == ""
    assert harness.RunList.all_run_metadata() == {}
    assert harness.error_messages[-1] == (
        "Database Recovery Failed",
        "qPlot closed the database view because its previous state could not "
        "be restored safely.",
        str(rollback_error),
    )


def test_dual_replacement_does_not_republish_a_when_b_rollback_is_needed():
    instance_a = _instance("database-a.db", (1, 1))
    replaced_a = _instance("database-a.db", (1, 3))
    instance_b = _instance("database-b.db", (1, 2))
    replaced_b = _instance("database-b.db", (1, 4))
    service_a = _FakeService(instance_a)
    service_b = _FakeService(instance_b, accepted=False)
    worker_b = _FakeLoadWorker(service_b)
    harness = _LifecycleHarness(instance_a, service_a)
    instances = {
        instance_a.logical_path: instance_a,
        instance_b.logical_path: instance_b,
    }
    retry_callbacks = []

    def replace_b_and_a_during_staging(runs, **_kwargs) -> bool:
        if 9 in runs:
            instances[instance_a.logical_path] = replaced_a
            instances[instance_b.logical_path] = replaced_b
        return True

    harness.RunList.addRuns = replace_b_and_a_during_staging
    with (
        patch.object(
            database_actions,
            "get_DB_location",
            return_value=instance_a.logical_path,
        ),
        patch.object(
            database_actions,
            "database_instance",
            side_effect=lambda path: instances[logical_database_path(path)],
        ),
        patch.object(database_actions, "DatabaseLoadWorker", return_value=worker_b),
        patch.object(
            database_actions.QtCore.QTimer,
            "singleShot",
            side_effect=lambda _delay, callback: retry_callbacks.append(callback),
        ),
        patch.object(database_actions, "log_event"),
        patch.object(database_actions, "log_exception"),
    ):
        assert harness.load_file(instance_b.logical_path)
        generation = harness._database_load_generation
        service_b.accepted = True
        harness.database_load_finished(
            generation,
            instance_b.logical_path,
            {9: {"guid": "guid-b", "run_timestamp": 9.0}},
            None,
            worker_b,
        )

    assert harness._trusted_read_service is None
    assert harness._pending_trusted_read_services == {}
    assert service_a.close_async_calls == 1
    assert service_b.close_async_calls == 1
    assert harness.fileTextbox.text() == ""
    assert harness.RunList.all_run_metadata() == {}
    assert len(retry_callbacks) == 1

    with patch.object(
        database_actions.DatabaseActionsMixin,
        "_retry_replaced_database_if_current",
        return_value=True,
    ) as retry:
        retry_callbacks[0]()
    retry.assert_called_once()
    retry_args, retry_kwargs = retry.call_args
    assert retry_args == (
        harness,
        harness._database_load_generation,
        instance_a.logical_path,
    )
    assert isinstance(retry_kwargs["load_started_at"], float)


def test_post_commit_selection_failure_still_retires_old_service():
    instance_a = _instance("database-a.db", (1, 1))
    instance_b = _instance("database-b.db", (1, 2))
    service_a = _FakeService(instance_a)
    service_b = _FakeService(instance_b, accepted=False)
    worker_b = _FakeLoadWorker(service_b)
    harness = _LifecycleHarness(instance_a, service_a)
    selection_error = RuntimeError("injected post-commit selection failure")
    instances = {
        instance_a.logical_path: instance_a,
        instance_b.logical_path: instance_b,
    }
    harness.select_default_run = lambda: (_ for _ in ()).throw(selection_error)

    with (
        patch.object(
            database_actions,
            "get_DB_location",
            return_value=instance_a.logical_path,
        ),
        patch.object(
            database_actions,
            "database_instance",
            side_effect=lambda path: instances[logical_database_path(path)],
        ),
        patch.object(database_actions, "DatabaseLoadWorker", return_value=worker_b),
        patch.object(database_actions, "set_qcodes_database_location"),
        patch.object(database_actions, "log_event"),
    ):
        assert harness.load_file(instance_b.logical_path)
        generation = harness._database_load_generation
        service_b.accepted = True
        with pytest.raises(RuntimeError, match="post-commit selection failure"):
            harness.database_load_finished(
                generation,
                instance_b.logical_path,
                {9: {"guid": "guid-b", "run_timestamp": 9.0}},
                None,
                worker_b,
            )

    assert harness._trusted_read_service is service_b
    assert harness._pending_trusted_read_services == {}
    assert service_a.close_async_calls == 1
    assert service_a in harness._retired_trusted_read_services
    assert service_b.close_async_calls == 0


def test_b_publication_blocks_reentry_and_retires_a_after_full_commit():
    instance_a = _instance("database-a.db", (1, 1))
    instance_b = _instance("database-b.db", (1, 2))
    service_a = _FakeService(instance_a)
    service_b = _FakeService(instance_b, accepted=False)
    worker_b = _FakeLoadWorker(service_b)
    harness = _LifecycleHarness(instance_a, service_a)
    configured_path = [instance_a.logical_path]
    original_add_runs = harness.RunList.addRuns
    original_close_a = service_a.close_async
    staging_observations = []
    retirement_observations = []

    def configured_database() -> str:
        return configured_path[-1]

    def configure_database(path: str) -> None:
        configured_path.append(path)

    def add_runs(runs, **kwargs) -> bool:
        if 9 in runs:
            staging_observations.append(
                (
                    harness._trusted_read_service,
                    harness._loaded_database_instance,
                    harness.fileTextbox.text(),
                    configured_database(),
                    harness._database_load_publication_active,
                    harness.load_file("database-c.db"),
                )
            )
        return original_add_runs(runs, **kwargs)

    def close_a() -> None:
        retirement_observations.append(
            (
                harness._trusted_read_service,
                harness._loaded_database_instance,
                harness.fileTextbox.text(),
                harness.RunList.all_run_metadata(),
                configured_database(),
            )
        )
        original_close_a()

    instances = {
        instance_a.logical_path: instance_a,
        instance_b.logical_path: instance_b,
    }
    harness.RunList.addRuns = add_runs
    service_a.close_async = close_a
    new_runs = {9: {"guid": "guid-b", "run_timestamp": 9.0}}
    with (
        patch.object(
            database_actions,
            "get_DB_location",
            side_effect=configured_database,
        ),
        patch.object(
            database_actions,
            "database_instance",
            side_effect=lambda path: instances[logical_database_path(path)],
        ),
        patch.object(database_actions, "DatabaseLoadWorker", return_value=worker_b),
        patch.object(
            database_actions,
            "set_qcodes_database_location",
            side_effect=configure_database,
        ),
        patch.object(database_actions, "log_event"),
    ):
        assert harness.load_file(instance_b.logical_path)
        generation = harness._database_load_generation
        service_b.accepted = True
        harness.database_load_finished(
            generation,
            instance_b.logical_path,
            new_runs,
            None,
            worker_b,
        )

    assert staging_observations == [
        (
            service_a,
            instance_a,
            instance_a.logical_path,
            instance_a.logical_path,
            True,
            False,
        )
    ]
    assert retirement_observations == [
        (
            service_b,
            instance_b,
            instance_b.logical_path,
            new_runs,
            instance_b.logical_path,
        )
    ]
    assert harness._trusted_read_service is service_b
    assert harness._loaded_database_instance == instance_b
    assert service_a.close_async_calls == 1
    assert service_b.close_async_calls == 0


def test_close_during_b_row_staging_retires_b_and_leaves_closed_view_empty():
    instance_a = _instance("database-a.db", (1, 1))
    instance_b = _instance("database-b.db", (1, 2))
    service_a = _FakeService(instance_a)
    service_b = _FakeService(instance_b, accepted=False)
    worker_b = _FakeLoadWorker(service_b)
    harness = _LifecycleHarness(instance_a, service_a)
    original_add_runs = harness.RunList.addRuns

    def close_during_add(runs, **kwargs) -> bool:
        harness.close_database(status=False)
        # Model a real RunList iteration resuming once after its nested event
        # loop: publication must clear this late staged row before returning.
        original_add_runs(runs)
        continue_loading = kwargs.get("continue_loading")
        return not callable(continue_loading) or continue_loading()

    instances = {
        instance_a.logical_path: instance_a,
        instance_b.logical_path: instance_b,
    }
    harness.RunList.addRuns = close_during_add
    with (
        patch.object(
            database_actions,
            "get_DB_location",
            return_value=instance_a.logical_path,
        ),
        patch.object(
            database_actions,
            "database_instance",
            side_effect=lambda path: instances[logical_database_path(path)],
        ),
        patch.object(database_actions, "DatabaseLoadWorker", return_value=worker_b),
        patch.object(database_actions, "set_qcodes_database_location") as configure,
        patch.object(database_actions, "log_event"),
    ):
        assert harness.load_file(instance_b.logical_path)
        generation = harness._database_load_generation
        service_b.accepted = True
        harness.database_load_finished(
            generation,
            instance_b.logical_path,
            {9: {"guid": "guid-b", "run_timestamp": 9.0}},
            None,
            worker_b,
        )

    configure.assert_not_called()
    assert harness._trusted_read_service is None
    assert harness._pending_trusted_read_services == {}
    assert service_a.close_async_calls == 1
    assert service_b.close_async_calls == 1
    assert {service_a, service_b}.issubset(harness._retired_trusted_read_services)
    assert harness.fileTextbox.text() == ""
    assert harness.RunList.all_run_metadata() == {}
    assert harness.default_selections == 0
    assert harness.detail_loads == []
    assert harness.error_messages == []


def test_stale_load_callback_retires_only_its_pending_service():
    instance_a = _instance("database-a.db", (1, 1))
    instance_b = _instance("database-b.db", (1, 2))
    instance_c = _instance("database-c.db", (1, 3))
    service_a = _FakeService(instance_a)
    service_b = _FakeService(instance_b)
    service_c = _FakeService(instance_c)
    harness = _LifecycleHarness(instance_a, service_a)
    harness._database_load_generation = 12
    harness._database_load_active = True
    harness._database_load_state = {"abspath": instance_c.logical_path}
    harness._database_load_worker = _FakeLoadWorker(service_c)
    harness._pending_trusted_read_services = {11: service_b, 12: service_c}

    harness.database_load_finished(
        11,
        instance_b.logical_path,
        {2: {"guid": "stale-guid"}},
        None,
        _FakeLoadWorker(service_b),
    )

    assert harness._trusted_read_service is service_a
    assert service_a.close_async_calls == 0
    assert service_b.close_async_calls == 1
    assert service_b in harness._retired_trusted_read_services
    assert harness._pending_trusted_read_services == {12: service_c}
    assert service_c.close_async_calls == 0
    assert harness._database_load_active
    assert harness.fileTextbox.text() == instance_a.logical_path
    assert harness.RunList.runs == {
        1: {"guid": "guid-a", "run_timestamp": 1.0},
    }


def test_close_database_asynchronously_retires_active_and_pending_services():
    instance_a = _instance("database-a.db", (1, 1))
    instance_b = _instance("database-b.db", (1, 2))
    instance_c = _instance("database-c.db", (1, 3))
    service_a = _FakeService(instance_a)
    service_b = _FakeService(instance_b)
    service_c = _FakeService(instance_c)
    harness = _LifecycleHarness(instance_a, service_a)
    harness._pending_trusted_read_services = {5: service_b, 6: service_c}

    harness.close_database(status=False)

    assert harness._trusted_read_service is None
    assert harness._pending_trusted_read_services == {}
    assert harness._database_access_mode is None
    assert harness._database_fallback_reason is None
    assert harness._retired_trusted_read_services == {
        service_a,
        service_b,
        service_c,
    }
    assert service_a.close_async_calls == 1
    assert service_b.close_async_calls == 1
    assert service_c.close_async_calls == 1
    assert harness.fileTextbox.text() == ""
    assert harness.selected_run_id is None
    assert harness.monitor.stopped


def test_trusted_load_selection_scroll_and_detail_completion_start_no_previews():
    instance_a = _instance("preview-a.db", (2, 1))
    instance_b = _instance("preview-b.db", (2, 2))
    service_a = _FakeService(instance_a)
    service_b = _FakeService(instance_b, accepted=False)
    worker_b = _FakeLoadWorker(service_b)
    harness = _TrustedPreviewBoundaryHarness(instance_a, service_a)
    selected_workers = []
    new_runs = {
        7: {
            "guid": "guid-7",
            "name": "trusted run",
            "run_timestamp": 7.0,
            "result_count": 12,
            "measure_parameters": ("signal",),
            "sweep_parameters": ("gate",),
        }
    }

    def selected_worker_factory(*args, **kwargs):
        worker = _FakeSelectedWorker(args, kwargs)
        selected_workers.append(worker)
        return worker

    instances = {
        instance_a.logical_path: instance_a,
        instance_b.logical_path: instance_b,
    }

    try:
        with (
            patch.object(
                database_actions,
                "get_DB_location",
                return_value=instance_a.logical_path,
            ),
            patch.object(
                database_actions,
                "database_instance",
                side_effect=lambda path: instances[logical_database_path(path)],
            ),
            patch.object(
                database_actions,
                "DatabaseLoadWorker",
                return_value=worker_b,
            ),
            patch.object(
                database_actions,
                "DatabaseSelectedRunWorker",
                side_effect=selected_worker_factory,
            ),
            patch.object(database_actions, "set_qcodes_database_location"),
            patch.object(database_actions, "log_event"),
            patch.object(preview_module, "PreviewWorker") as legacy_preview_worker,
        ):
            assert harness.load_file(instance_b.logical_path)
            generation = harness._database_load_generation
            service_b.accepted = True
            harness.database_load_finished(
                generation,
                instance_b.logical_path,
                new_runs,
                None,
                worker_b,
            )

            assert harness._selected_run_guid == "guid-7"
            assert harness.selected_run_id == 7
            assert len(selected_workers) == 1
            assert harness.databaseSelectedRunThreadPool.started == selected_workers

            # A real viewport-priority pass is the path used by scrolling.
            harness._run_table_view_changed()

            harness._database_detail_generation = 31
            harness._database_detail_active = True
            harness._database_detail_instance = instance_b
            harness.database_detail_batch_ready(
                31,
                instance_b.logical_path,
                {7: {"guid": "guid-7", "result_count": 13}},
            )
            harness.database_detail_finished(31, instance_b.logical_path, None)

            harness._database_expensive_detail_generation = 32
            harness._database_expensive_detail_active = True
            harness._database_expensive_detail_instance = instance_b
            harness.database_expensive_detail_batch_ready(
                32,
                instance_b.logical_path,
                {7: {"guid": "guid-7", "storage_bytes": 4096}},
            )
            harness.database_expensive_detail_finished(
                32,
                instance_b.logical_path,
                None,
            )

            selected_generation = harness._database_selected_run_generation
            harness.database_selected_run_finished(
                selected_generation,
                instance_b.logical_path,
                "guid-7",
                _selected_detail(),
                None,
            )
            harness._run_table_view_changed()

        legacy_preview_worker.assert_not_called()
        assert harness.infoBox.preview.database_path == ""
        assert harness.infoBox.preview.queue == {}
        assert harness.infoBox.preview._workers == {}
        assert harness.infoBox.preview.current_guid is None
        assert harness.ds is None
        assert harness.RunList.all_run_metadata()[7]["storage_bytes"] == 4096
    finally:
        harness.dispose()


@pytest.mark.parametrize(
    "publication_fails",
    (False, True),
    ids=("trusted-commit", "snapshot-rollback"),
)
def test_legacy_preview_callback_cannot_land_on_staged_trusted_rows(
    tmp_path,
    publication_fails,
):
    database_a = tmp_path / "preview-source-a.db"
    database_b = tmp_path / "preview-source-b.db"
    database_a.write_bytes(b"snapshot source A")
    database_b.write_bytes(b"trusted source B")
    instance_a = database_instance(database_a)
    instance_b = database_instance(database_b)
    service_a = _FakeService(instance_a)
    service_b = _FakeService(instance_b, accepted=False)
    worker_b = _FakeLoadWorker(service_b)
    harness = _TrustedPreviewBoundaryHarness(instance_a, service_a)
    old_runs = {
        1: {
            "guid": "shared-guid",
            "run_timestamp": 1.0,
            "is_completed": True,
            "measure_parameters": ("legacy-signal",),
            "sweep_parameters": (),
        }
    }
    new_runs = {
        run_id: {
            "guid": "shared-guid" if run_id == 1 else f"trusted-guid-{run_id}",
            "run_timestamp": float(run_id),
            "is_completed": True,
            "measure_parameters": ("trusted-signal",),
            "sweep_parameters": (),
        }
        for run_id in range(1, 302)
    }
    stale_image = QtGui.QImage(2, 2, QtGui.QImage.Format.Format_RGB32)
    stale_image.fill(QtGui.QColor("red"))
    images_seen_during_yield = []
    emitted = False

    harness._database_access_mode = database_actions.SNAPSHOT_FALLBACK_MODE
    harness._trusted_read_service = None
    harness._loaded_database_identity = instance_a.identity
    harness._loaded_database_instance = instance_a
    harness.fileTextbox.setText(instance_a.logical_path)
    harness.localLastFile = instance_a.logical_path
    harness.RunList.clear()
    harness.RunList.addRuns(old_runs)
    harness.infoBox.preview.set_database_runs(instance_a.logical_path, old_runs)
    harness.infoBox.preview.previewsReady.connect(harness.RunList.set_run_previews)
    harness.infoBox.preview.previewGenerationChanged.connect(
        harness.RunList.set_run_preview_generating
    )

    def finish_legacy_preview(*_args) -> None:
        nonlocal emitted
        if emitted:
            return
        emitted = True
        staged_cell = harness.RunList.preview_cells["shared-guid"]
        harness.infoBox.preview.previewsReady.emit(
            "shared-guid",
            [
                {
                    "parameter": "legacy-signal",
                    "axes": [],
                    "title": "stale preview from source A",
                    "image": stale_image,
                }
            ],
        )
        images_seen_during_yield.append(
            len(
                staged_cell.findChildren(
                    qtw.QLabel,
                    "measurementPreviewImage",
                )
            )
        )

    instances = {
        instance_a.logical_path: instance_a,
        instance_b.logical_path: instance_b,
    }

    def configure_database(path: str) -> None:
        if publication_fails and logical_database_path(path) == instance_b.logical_path:
            raise RuntimeError("injected trusted publication failure")

    try:
        with (
            patch.object(
                database_actions,
                "get_DB_location",
                return_value=instance_a.logical_path,
            ),
            patch.object(
                database_actions,
                "database_instance",
                side_effect=lambda path: instances[logical_database_path(path)],
            ),
            patch.object(
                database_actions,
                "DatabaseLoadWorker",
                return_value=worker_b,
            ),
            patch.object(
                database_actions,
                "set_qcodes_database_location",
                side_effect=configure_database,
            ),
            patch.object(database_actions, "log_event"),
            patch.object(database_actions, "log_exception"),
            patch.object(harness, "select_default_run", return_value=None),
            patch.object(
                treeWidgets.QtCore.QCoreApplication,
                "processEvents",
                side_effect=finish_legacy_preview,
            ),
        ):
            assert harness.load_file(instance_b.logical_path)
            generation = harness._database_load_generation
            service_b.accepted = True
            harness.database_load_finished(
                generation,
                instance_b.logical_path,
                new_runs,
                None,
                worker_b,
            )

        visible_cell = harness.RunList.preview_cells["shared-guid"]
        assert emitted
        assert images_seen_during_yield == [0]
        assert (
            visible_cell.findChildren(
                qtw.QLabel,
                "measurementPreviewImage",
            )
            == []
        )
        assert (
            len(
                visible_cell.findChildren(
                    qtw.QLabel,
                    "measurementPreviewPlaceholder",
                )
            )
            == 1
        )
        assert not visible_cell._has_rendered_previews
        assert not harness.RunList._preview_publication_suspended
        if publication_fails:
            assert harness.RunList.all_run_metadata() == old_runs
            assert harness.infoBox.preview.database_path == instance_a.logical_path
            assert (
                harness._database_access_mode == database_actions.SNAPSHOT_FALLBACK_MODE
            )
            assert harness._trusted_read_service is None

            # Rollback has restored A's source and lifted the transaction gate,
            # so a subsequent current-A callback remains fully functional.
            harness.infoBox.preview.previewsReady.emit(
                "shared-guid",
                [
                    {
                        "parameter": "legacy-signal",
                        "axes": [],
                        "title": "current preview from restored source A",
                        "image": stale_image,
                    }
                ],
            )
            assert (
                len(
                    visible_cell.findChildren(
                        qtw.QLabel,
                        "measurementPreviewImage",
                    )
                )
                == 1
            )
            assert service_b.close_async_calls == 1
        else:
            assert harness.RunList.all_run_metadata() == new_runs
            assert harness.infoBox.preview.database_path == ""
            assert harness._database_access_mode == database_actions.TRUSTED_LIVE_MODE
            assert harness._trusted_read_service is service_b
    finally:
        harness.dispose()


@pytest.mark.parametrize(
    ("access_mode", "expected_close_calls"),
    (
        (database_actions.TRUSTED_LIVE_MODE, [1, 1]),
        (database_actions.SNAPSHOT_FALLBACK_MODE, [1, 1]),
    ),
)
def test_explicit_plot_and_export_materialize_guid_for_exact_instance(
    tmp_path,
    access_mode,
    expected_close_calls,
):
    database_path = tmp_path / "trusted-actions.db"
    database_path.write_bytes(b"trusted action identity")
    instance = database_instance(database_path)
    guid = "trusted-action-guid"
    service = _FakeService(instance)
    harness = _TrustedActionHarness(instance, service, guid)
    harness._database_access_mode = access_mode
    load_calls = []
    datasets = []
    published = []

    def load_selected_guid(*args, **kwargs):
        dataset = _ActionDataset(guid, 7)
        datasets.append(dataset)
        load_calls.append((args, kwargs))
        return dataset

    def publish_without_disk_write(
        filename,
        _writer,
        *,
        before_publish=None,
    ) -> None:
        if before_publish is not None:
            before_publish()
        published.append(filename)

    with (
        patch.object(
            plot_actions,
            "load_by_guid_read_only",
            side_effect=load_selected_guid,
        ),
        patch.object(
            plot_actions,
            "write_export_atomically",
            side_effect=publish_without_disk_write,
        ),
    ):
        harness.openPlot(guid, show=False)
        harness.exportRunCsv()

    assert service.accepted
    assert service.database_instance == instance
    assert len(harness.opened) == 1
    assert harness.opened[0][3].guid == guid
    assert harness.opened[0][3].database_identity == instance.identity
    assert load_calls == [
        (
            (guid, instance.logical_path),
            {"expected_database_identity": instance.identity},
        ),
        (
            (guid, instance.logical_path),
            {"expected_database_identity": instance.identity},
        ),
    ]
    assert published == [harness.export_filename]
    assert [dataset.conn.close_calls for dataset in datasets] == expected_close_calls
    assert harness.ds is None
    assert harness.error_messages == []


@pytest.mark.parametrize("action_outcome", ["no-plottable", "plot-error"])
def test_trusted_failed_explicit_plot_leaves_selection_with_no_dataset_cleanup(
    tmp_path,
    action_outcome,
):
    """An explicit action owns cleanup; the next DB-free selection does not."""

    class Harness(_TrustedActionHarness, database_actions.DatabaseActionsMixin):
        def __init__(self, instance, service, guid) -> None:
            super().__init__(instance, service, guid)
            self.RunList = _SelectionRunList(7, guid)
            self.infoBox = _RecordingInfoBox()
            self.run_idBox = _Field("7")
            self.databaseDetailThreadPool = _ThreadPool()
            self._database_selected_run_generation = 0
            self._database_selected_run_worker = None
            self._database_selected_run_instance = None
            self._selected_run_detail_cache = {}

    class NoPlottableDataset(_ActionDataset):
        @staticmethod
        def get_parameters():
            return []

    database_path = tmp_path / "trusted-failed-action.db"
    database_path.write_bytes(b"trusted failed action identity")
    instance = database_instance(database_path)
    guid = "trusted-failed-action-guid"
    service = _FakeService(instance)
    harness = Harness(instance, service, guid)
    dataset = NoPlottableDataset(guid, 7)
    closed_datasets = []

    def close_action_dataset(candidate):
        closed_datasets.append(candidate)
        candidate.conn.close()
        return True

    def fail_open_plot(*_args, **_kwargs):
        raise RuntimeError("injected explicit plot failure")

    with (
        patch.object(
            plot_actions,
            "load_by_guid_read_only",
            return_value=dataset,
        ) as load_by_guid,
        patch.object(
            plot_actions,
            "close_dataset_connection",
            side_effect=close_action_dataset,
        ) as close_dataset,
        patch.object(
            database_actions,
            "database_instance",
            return_value=instance,
        ),
        patch.object(
            database_actions,
            "DatabaseSelectedRunWorker",
            side_effect=lambda *args, **kwargs: _FakeSelectedWorker(args, kwargs),
        ),
    ):
        if action_outcome == "plot-error":
            harness.openPlot = fail_open_plot
            with pytest.raises(RuntimeError, match="injected explicit plot failure"):
                harness.openRun()
        else:
            harness.openRun()

        assert harness.ds is None
        assert harness._selected_dataset_key is None
        assert closed_datasets == [dataset]
        assert dataset.conn.close_calls == 1

        # This is the ordinary Stage 4 selection path. It may schedule a plain
        # trusted detail worker, but it must not load, snapshot, or close any
        # QCoDeS/SQLite DataSet handle.
        harness.updateSelected(guid)

    load_by_guid.assert_called_once_with(
        guid,
        instance.logical_path,
        expected_database_identity=instance.identity,
    )
    close_dataset.assert_called_once_with(dataset)
    assert closed_datasets == [dataset]
    assert dataset.conn.close_calls == 1
    assert harness.ds is None
    assert harness.databaseDetailThreadPool.started


def test_delayed_trusted_open_query_cancel_and_close_keep_gui_responsive():
    instance = _instance("delayed-service.db", (3, 1))
    supervisor = _DelayedSupervisor(instance)
    open_entered = threading.Event()
    open_release = threading.Event()
    ticks = []
    timer = QtCore.QTimer()
    timer.setInterval(5)
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start()
    service = None

    def delayed_open(*_args, **_kwargs):
        open_entered.set()
        if not open_release.wait(5):
            raise AssertionError("delayed trusted open was not released")
        return supervisor

    try:
        with patch.object(
            trusted_service_module,
            "TrustedMetadataQueryAdapter",
            _DelayedAdapter,
        ):
            service = TrustedLiveReadService(
                instance.logical_path,
                expected_database_instance=instance,
                supervisor_factory=delayed_open,
            )
            harness = _LifecycleHarness(instance, service)
            request = service.submit_bootstrap()

            _wait_for_gui_event(open_entered)
            _assert_gui_timer_advances(ticks)
            open_release.set()

            _wait_for_gui_event(supervisor.query_entered)
            _assert_gui_timer_advances(ticks)

            started = time.monotonic()
            assert request.cancel()
            assert time.monotonic() - started < 0.5
            _wait_for_gui_event(supervisor.cancel_entered)
            _assert_gui_timer_advances(ticks)

            started = time.monotonic()
            harness.close_database(status=False)
            assert time.monotonic() - started < 0.5
            assert harness._trusted_read_service is None
            _wait_for_gui_event(supervisor.close_entered)
            _assert_gui_timer_advances(ticks)
    finally:
        open_release.set()
        supervisor.cancel_release.set()
        supervisor.close_release.set()
        if service is not None:
            service.close_async()
            assert service.wait_closed(3)
        timer.stop()
