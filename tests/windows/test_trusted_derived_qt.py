"""Deterministic Qt-boundary regressions for Stage 5C trusted derived work."""

from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from PyQt6 import QtCore, QtGui, QtWidgets

from qplot.datahandling.file_identity import DatabaseInstance, database_instance
from qplot.datahandling.trusted_live_queries import (
    TrustedSourceRevision,
    TrustedSourceRevisionNamespace,
)
from qplot.datahandling.trusted_live_service import TrustedLiveReadService
from qplot.datahandling.trusted_work_coordinator import TrustedDerivedRun
from qplot.datahandling.trusted_work_scheduler import (
    TrustedCacheWorkKey,
    TrustedWorkKind,
    WorkPublication,
)
from qplot.windows import _database_actions as database_actions
from qplot.windows import _trusted_derived_qt as bridge_module
from qplot.windows._trusted_derived_qt import TrustedDerivedQtBridge
from qplot.windows._widgets.preview import PreviewTab
from tests.windows.test_trusted_live_ui import (
    _FakeLoadWorker,
    _LifecycleHarness,
)
from tests.windows.test_trusted_live_ui import (
    _FakeService as _LifecycleService,
)
from tests.windows.test_trusted_live_ui import (
    _instance as _lifecycle_instance,
)


def _process_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QtWidgets.QApplication.processEvents()
        if predicate():
            return
        time.sleep(0.002)
    raise AssertionError("Qt condition was not reached")


class _Service(TrustedLiveReadService):
    def __init__(self, instance: DatabaseInstance, nonce: bytes = b"stage5c") -> None:
        self._instance = instance
        self._namespace = TrustedSourceRevisionNamespace(nonce)

    @property
    def database_instance(self) -> DatabaseInstance:
        return self._instance

    @property
    def source_revision_namespace(self) -> TrustedSourceRevisionNamespace:
        return self._namespace


class _FakeCoordinator:
    created: list[_FakeCoordinator] = []

    def __init__(
        self,
        database: DatabaseInstance,
        runs,
        service,
        *,
        formats,
        wakeup,
        on_publish,
        on_error,
    ) -> None:
        self.database = database
        self._runs = tuple(runs)
        self.service = service
        self.formats = dict(formats)
        self.wakeup = wakeup
        self.on_publish = on_publish
        self.on_error = on_error
        self.generation = 1
        self.pending: list[WorkPublication] = []
        self.poll_threads: list[int] = []
        self.poll_count = 0
        self.started = 0
        self.selections: list[int | None] = []
        self.visible_updates: list[tuple[int, ...]] = []
        self.source_changes: list[int] = []
        self.format_updates: list[tuple[TrustedWorkKind, object]] = []
        self.reconciliations: list[int] = []
        self.completed: set[tuple[int, TrustedWorkKind]] = set()
        self.replay_requests: list[tuple[int, TrustedWorkKind]] = []
        self.closed = False
        self.joined = False
        type(self).created.append(self)

    @property
    def runs(self):
        return self._runs

    @property
    def active(self) -> bool:
        return False

    def snapshot(self):
        return SimpleNamespace(generation=self.generation, pending_count=0)

    def start(self) -> None:
        self.started += 1

    def poll(self) -> int:
        self.poll_threads.append(threading.get_ident())
        self.poll_count += 1
        publications, self.pending = self.pending, []
        for publication in publications:
            index = next(
                index
                for index, run in enumerate(self._runs)
                if run.run_guid == publication.key.run_guid
            )
            self.completed.add((index, publication.key.kind))
            self.on_publish(publication)
        return len(publications)

    def select_run(self, index: int | None) -> None:
        self.selections.append(index)

    def set_visible_indices(self, indices) -> None:
        self.visible_updates.append(tuple(indices))

    def reconcile_runs(self, runs) -> None:
        self._runs = tuple(runs)
        self.reconciliations.append(len(self._runs))

    def source_changed(self, index: int) -> None:
        self.source_changes.append(index)
        run = self._runs[index]
        updated = list(self._runs)
        updated[index] = TrustedDerivedRun(
            run.run_id,
            run.run_guid,
            TrustedSourceRevision(f"changed-{len(self.source_changes)}".encode()),
        )
        self._runs = tuple(updated)

    def helper_restarted(self) -> None:
        self.generation += 1
        self._runs = tuple(
            TrustedDerivedRun(
                run.run_id,
                run.run_guid,
                TrustedSourceRevision(f"helper-{self.generation}-{index}".encode()),
            )
            for index, run in enumerate(self._runs)
        )

    def update_format(self, kind, work_format) -> None:
        self.formats[kind] = work_format
        self.format_updates.append((kind, work_format))

    def request_completed_work(
        self,
        run_index,
        kind,
        *,
        database_instance,
        generation,
        run_guid,
        prioritize=False,
    ) -> bool:
        if (
            database_instance != self.database
            or generation != self.generation
            or not 0 <= run_index < len(self._runs)
            or self._runs[run_index].run_guid != run_guid
            or (run_index, kind) not in self.completed
        ):
            return False
        self.completed.remove((run_index, kind))
        if prioritize:
            self.selections.append(run_index)
        self.replay_requests.append((run_index, kind))
        return True

    def switch_database(self, database, runs, service) -> None:
        self.database = database
        self._runs = tuple(runs)
        self.service = service
        self.generation += 1

    def close_async(self) -> None:
        self.closed = True

    def wait_closed(self, _timeout: float = 0.0) -> bool:
        self.joined = self.closed
        return self.joined


class _RunList(QtWidgets.QTreeWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setColumnCount(2)
        self.setHeaderLabels(("ID", "Name"))
        self.setSortingEnabled(True)
        self._items: dict[str, QtWidgets.QTreeWidgetItem] = {}
        self.preview_updates: list[tuple[str, list[dict[str, object]], int]] = []
        self.generating_updates: list[tuple[str, bool, int]] = []
        self.metadata_updates: list[tuple[int, dict[str, object], int]] = []

    def set_runs(self, runs: dict[int, dict[str, object]]) -> None:
        self.clear()
        self._items = {}
        for run_id, metadata in runs.items():
            item = QtWidgets.QTreeWidgetItem(
                (str(run_id), str(metadata.get("name", "")))
            )
            item.guid = str(metadata["guid"])
            item.run_metadata = dict(metadata)
            item.run_metadata.setdefault("run_id", run_id)
            self.addTopLevelItem(item)
            self._items[item.guid] = item

    def all_run_metadata(self):
        return {
            int(self._item_run_id(item)): dict(item.run_metadata)
            for item in self._items.values()
        }

    def _item_for_guid(self, guid):
        return self._items.get(str(guid))

    @staticmethod
    def _item_run_id(item):
        return item.text(0)

    def updateRuns(self, runs):
        for run_id, metadata in runs.items():
            item = self._items.get(str(metadata.get("guid") or ""))
            if item is None:
                continue
            item.run_metadata.update(metadata)
            self.metadata_updates.append(
                (int(run_id), dict(item.run_metadata), threading.get_ident())
            )

    def set_run_previews(self, guid, previews):
        self.preview_updates.append((str(guid), list(previews), threading.get_ident()))

    def accepts_run_preview(self, guid) -> bool:
        return len(self._items) <= 500 and str(guid) in self._items

    def set_run_preview_generating(self, guid, generating):
        self.generating_updates.append(
            (str(guid), bool(generating), threading.get_ident())
        )


class _Preview:
    def __init__(self) -> None:
        self.current_guid: str | None = None
        self.bound_runs: dict[int, dict[str, object]] = {}
        self.retained: OrderedDict[str, list[dict[str, object]]] = OrderedDict()
        self.displayed: tuple[str, list[dict[str, object]]] | None = None
        self.threads: list[int] = []

    def set_trusted_derived_runs(self, runs) -> None:
        self.threads.append(threading.get_ident())
        self.bound_runs = dict(runs)
        self.current_guid = None
        self.retained = OrderedDict()
        self.displayed = None

    def refresh_trusted_derived_runs(self, runs) -> None:
        self.threads.append(threading.get_ident())
        self.bound_runs = dict(runs)
        valid = {
            str(metadata.get("guid") or "") for metadata in self.bound_runs.values()
        }
        self.retained = OrderedDict(
            (guid, previews)
            for guid, previews in self.retained.items()
            if guid in valid
        )

    def add_trusted_derived_runs(self, runs) -> None:
        self.threads.append(threading.get_ident())
        self.bound_runs.update(runs)

    def clear_current_run(self) -> None:
        self.threads.append(threading.get_ident())
        self.current_guid = None
        self.displayed = None

    def set_current_guid(self, guid) -> None:
        self.threads.append(threading.get_ident())
        self.current_guid = str(guid)

    def publish_trusted_previews(self, guid, previews, *, error=None) -> None:
        self.threads.append(threading.get_ident())
        values = list(previews)
        exact_guid = str(guid)
        self.retained.pop(exact_guid, None)
        self.retained[exact_guid] = values
        while len(self.retained) > 512:
            evict = next(
                (
                    retained_guid
                    for retained_guid in self.retained
                    if retained_guid != self.current_guid
                ),
                None,
            )
            if evict is None:
                break
            self.retained.pop(evict)
        if str(guid) == self.current_guid:
            self.displayed = (str(guid), values)

    def trusted_preview_needs_replay(self, guid) -> bool:
        exact_guid = str(guid or "")
        return bool(exact_guid and exact_guid not in self.retained)

    def discard_trusted_previews(self) -> None:
        self.retained = OrderedDict()
        self.displayed = None

    def evict(self, guid) -> None:
        self.retained.pop(str(guid), None)


class _InfoBox:
    def __init__(self) -> None:
        self.preview = _Preview()
        self.metadata: list[tuple[dict[str, object], int]] = []
        self.errors: list[tuple[str, int]] = []
        self.loading: list[tuple[dict[str, object], int]] = []

    def set_trusted_derived_metadata(
        self, run, _parameters, _summaries, _metadata
    ) -> None:
        self.metadata.append((dict(run), threading.get_ident()))
        self.preview.set_current_guid(str(run["guid"]))

    def set_trusted_run_error(self, message, _run) -> None:
        self.errors.append((str(message), threading.get_ident()))

    def set_trusted_run_loading(self, run) -> None:
        self.loading.append((dict(run), threading.get_ident()))
        self.preview.set_current_guid(str(run["guid"]))


class _Window(QtWidgets.QWidget):
    def __init__(self, instance, service, runs) -> None:
        super().__init__()
        self.preview_size = 240
        self.RunList = _RunList(self)
        self.RunList.set_runs(runs)
        self.infoBox = _InfoBox()
        self._selected_run_guid: str | None = None
        self._trusted_read_service = service
        self._loaded_database_instance = instance
        self._shutdown_started = False
        self._shutdown_ready = False
        self.reloads: list[str] = []
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.RunList)
        self.resize(500, 220)
        self.show()

    def _reload_replaced_database(self, path: str) -> None:
        self.reloads.append(path)


@pytest.fixture(autouse=True)
def fake_coordinator(monkeypatch):
    _FakeCoordinator.created = []
    monkeypatch.setattr(bridge_module, "TrustedWorkCoordinator", _FakeCoordinator)


@pytest.fixture
def bound_bridge(tmp_path):
    path = tmp_path / "trusted.db"
    path.write_bytes(b"qplot-stage5c")
    instance = database_instance(path)
    service = _Service(instance)
    runs = {
        index: {
            "run_id": index,
            "guid": f"guid-{index}",
            "name": f"run-{index}",
            "result_count": index,
        }
        for index in range(1, 13)
    }
    window = _Window(instance, service, runs)
    bridge = TrustedDerivedQtBridge(window)
    bridge.bind_database(instance, runs, service)
    yield window, bridge, _FakeCoordinator.created[-1], runs
    bridge.shutdown()
    QtWidgets.QApplication.processEvents()
    window.hide()
    window.deleteLater()


def _publication(
    bridge: TrustedDerivedQtBridge,
    coordinator: _FakeCoordinator,
    guid: str,
    kind: TrustedWorkKind,
    *,
    generation: int | None = None,
    helper_incarnation: int = 1,
    status: str = "ok",
    description: str = "ready",
) -> WorkPublication:
    index = next(i for i, run in enumerate(coordinator.runs) if run.run_guid == guid)
    run = coordinator.runs[index]
    work_format = bridge._formats[kind]
    key = TrustedCacheWorkKey(
        coordinator.database,
        guid,
        kind,
        run.source_revision,
        work_format.renderer_version,
        work_format.options,
    )
    source = (
        ("run_id", run.run_id),
        ("run_guid", guid),
        ("helper_incarnation", helper_incarnation),
    )
    payload: dict[str, Any] = {
        "format": "qplot-trusted-derived-payload-v1",
        "kind": kind.name.lower(),
        "status": status,
        "description": description,
        "source": source,
        "images": (),
    }
    if kind is TrustedWorkKind.METADATA and status == "ok":
        payload["metadata"] = (
            ("run_id", run.run_id),
            ("guid", guid),
            (
                "run_fields",
                (
                    ("run_id", run.run_id),
                    ("guid", guid),
                    ("name", f"derived-{guid}"),
                    ("result_count", 3),
                ),
            ),
            (
                "parameters",
                (
                    ("x", "X", "V", (), "numeric"),
                    ("signal", "Signal", "A", ("x",), "numeric"),
                ),
            ),
            ("setpoint_summaries", (("x", 0.0, 1.0, 3),)),
        )
    return WorkPublication(
        coordinator.generation if generation is None else generation,
        key,
        payload,
        False,
    )


def _image_publication(bridge, coordinator, guid, kind):
    publication = _publication(bridge, coordinator, guid, kind)
    image = QtGui.QImage(4, 3, QtGui.QImage.Format.Format_RGBA8888)
    image.fill(QtGui.QColor("#336699"))
    data = QtCore.QByteArray()
    buffer = QtCore.QBuffer(data)
    buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    payload = dict(publication.result)
    payload["images"] = (
        (
            ("encoding", "png"),
            ("width", 4),
            ("height", 3),
            ("dependent", "signal"),
            ("dimensions", 1),
            ("sampled_points", 3),
            ("bytes", bytes(data)),
        ),
    )
    return WorkPublication(
        publication.generation,
        publication.key,
        payload,
        publication.is_current_selection,
    )


def test_fast_baseline_commits_before_bridge_start_and_legacy_enrichment() -> None:
    first = _lifecycle_instance("first.db", (1, 1))
    second = _lifecycle_instance("second.db", (1, 2))
    old_service = _LifecycleService(first)
    new_service = _LifecycleService(second, accepted=False)
    worker = _FakeLoadWorker(new_service)
    harness = _LifecycleHarness(first, old_service)
    callbacks = []

    class DeferredBridge:
        def __init__(self) -> None:
            self.suspended = 0
            self.cleared = 0
            self.bindings = []

        def suspend_publications(self) -> None:
            self.suspended += 1

        def clear_database(self) -> None:
            self.cleared += 1

        def bind_database(self, instance, runs, service) -> None:
            self.bindings.append((instance, dict(runs), service))

    bridge = DeferredBridge()
    harness._trusted_derived_bridge = bridge
    observed = {
        first.logical_path: first,
        second.logical_path: second,
    }
    with (
        patch.object(
            database_actions,
            "get_DB_location",
            return_value=first.logical_path,
        ),
        patch.object(
            database_actions,
            "database_instance",
            side_effect=lambda path: observed[str(path)],
        ),
        patch.object(database_actions, "DatabaseLoadWorker", return_value=worker),
        patch.object(database_actions, "set_qcodes_database_location"),
        patch.object(database_actions, "log_event"),
        patch.object(
            database_actions.QtCore.QTimer,
            "singleShot",
            side_effect=lambda _delay, callback: callbacks.append(callback),
        ),
    ):
        assert harness.load_file(second.logical_path)
        generation = harness._database_load_generation
        new_service.accepted = True
        runs = {9: {"guid": "guid-b", "run_timestamp": 9.0}}
        harness.database_load_finished(
            generation,
            second.logical_path,
            runs,
            None,
            worker,
        )

        assert harness.RunList.all_run_metadata() == runs
        assert harness.fileTextbox.text() == second.logical_path
        assert bridge.bindings == []
        assert bridge.cleared == 1
        assert harness.detail_loads == []
        assert callbacks
        callbacks[-1]()

    assert bridge.bindings == [(second, runs, new_service)]
    assert bridge.suspended == 1


def test_worker_wakeups_are_coalesced_and_all_ui_mutation_is_on_gui_thread(
    bound_bridge,
) -> None:
    window, bridge, coordinator, _runs = bound_bridge
    owner = threading.get_ident()
    window._selected_run_guid = "guid-1"
    bridge.select_run("guid-1")
    polls_before = coordinator.poll_count
    coordinator.pending.extend(
        (
            _publication(bridge, coordinator, "guid-1", TrustedWorkKind.METADATA),
            _image_publication(
                bridge, coordinator, "guid-1", TrustedWorkKind.THUMBNAIL
            ),
            _image_publication(bridge, coordinator, "guid-1", TrustedWorkKind.PREVIEW),
        )
    )

    threads = [threading.Thread(target=coordinator.wakeup) for _ in range(32)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    _process_until(lambda: len(window.RunList.preview_updates) == 1)
    assert coordinator.poll_count == polls_before + 1
    assert coordinator.poll_threads[-1:] == [owner]
    mutation_threads = [entry[2] for entry in window.RunList.metadata_updates]
    mutation_threads += [entry[2] for entry in window.RunList.preview_updates]
    mutation_threads += [entry[2] for entry in window.RunList.generating_updates]
    mutation_threads += window.infoBox.preview.threads
    mutation_threads += [entry[1] for entry in window.infoBox.metadata]
    assert mutation_threads and set(mutation_threads) == {owner}


def test_selection_sort_scroll_and_resize_feed_stable_priority_without_rebuild(
    bound_bridge,
) -> None:
    window, bridge, coordinator, _runs = bound_bridge
    original = coordinator
    window._selected_run_guid = "guid-7"
    window.RunList.sortItems(0, QtCore.Qt.SortOrder.DescendingOrder)
    window.RunList.scrollToTop()
    for _ in range(20):
        bridge.request_priority_update()
    _process_until(lambda: bool(coordinator.visible_updates))

    visible_guids = []
    for index in coordinator.visible_updates[-1]:
        visible_guids.append(coordinator.runs[index].run_guid)
    assert coordinator.selections[-1] == 6
    assert visible_guids
    assert visible_guids[0] == window.RunList.topLevelItem(0).guid
    assert bridge.coordinator is original

    prior_updates = len(coordinator.visible_updates)
    window._selected_run_guid = "guid-3"
    window.RunList.scrollToBottom()
    window.RunList.resize(520, 260)
    for _ in range(50):
        bridge.request_priority_update()
    _process_until(lambda: len(coordinator.visible_updates) > prior_updates)
    assert len(coordinator.visible_updates) == prior_updates + 1
    assert coordinator.selections[-1] == 2
    assert bridge.coordinator is original


def test_old_database_and_unselected_publications_cannot_replace_selected_tabs(
    tmp_path,
) -> None:
    first_path = tmp_path / "first.db"
    second_path = tmp_path / "second.db"
    first_path.write_bytes(b"first")
    second_path.write_bytes(b"second")
    first = database_instance(first_path)
    second = database_instance(second_path)
    first_service = _Service(first, b"first")
    second_service = _Service(second, b"second")
    window = _Window(first, first_service, {1: {"guid": "guid-a", "name": "a"}})
    bridge = TrustedDerivedQtBridge(window)
    bridge.bind_database(first, window.RunList.all_run_metadata(), first_service)
    coordinator = _FakeCoordinator.created[-1]
    old = _publication(bridge, coordinator, "guid-a", TrustedWorkKind.METADATA)

    second_runs = {
        1: {"guid": "guid-b", "name": "b"},
        2: {"guid": "guid-c", "name": "c"},
    }
    window.RunList.set_runs(second_runs)
    window._loaded_database_instance = second
    window._trusted_read_service = second_service
    window._selected_run_guid = "guid-b"
    bridge.bind_database(second, second_runs, second_service)
    bridge.select_run("guid-b")
    bridge._publish(old)
    assert not window.RunList.metadata_updates

    unselected = _publication(bridge, coordinator, "guid-c", TrustedWorkKind.METADATA)
    bridge._publish(unselected)
    assert window.RunList.metadata_updates[-1][0] == 2
    assert not window.infoBox.metadata
    assert window.infoBox.preview.current_guid == "guid-b"
    bridge.shutdown()
    window.deleteLater()


def test_same_path_replacement_and_helper_restart_reject_obsolete_results(
    bound_bridge,
    tmp_path,
) -> None:
    window, bridge, coordinator, _runs = bound_bridge
    old_helper = _publication(
        bridge,
        coordinator,
        "guid-1",
        TrustedWorkKind.METADATA,
        helper_incarnation=1,
    )
    bridge.helper_restarted()
    bridge._publish(old_helper)
    assert not window.RunList.metadata_updates

    current = _publication(
        bridge,
        coordinator,
        "guid-1",
        TrustedWorkKind.METADATA,
        helper_incarnation=2,
    )
    original_path = window._loaded_database_instance.logical_path
    replacement = tmp_path / "replacement.db"
    replacement.write_bytes(b"replacement-instance")
    os.replace(replacement, original_path)
    bridge._publish(current)
    _process_until(lambda: bool(window.reloads))
    assert not window.RunList.metadata_updates
    assert window.reloads == [original_path]


def test_queued_replacement_reload_is_coalesced_when_another_consumer_wins(
    bound_bridge,
    tmp_path,
) -> None:
    window, bridge, coordinator, _runs = bound_bridge
    current = _publication(
        bridge,
        coordinator,
        "guid-1",
        TrustedWorkKind.METADATA,
    )
    original_path = window._loaded_database_instance.logical_path
    replacement = tmp_path / "replacement.db"
    replacement.write_bytes(b"replacement-instance")
    os.replace(replacement, original_path)

    bridge._publish(current)
    window._reload_replaced_database(original_path)
    bridge.clear_database()
    QtWidgets.QApplication.processEvents()

    assert window.reloads == [original_path]


def test_observed_helper_restart_invalidates_old_generation(bound_bridge) -> None:
    window, bridge, coordinator, _runs = bound_bridge
    bridge._publish(
        _publication(
            bridge,
            coordinator,
            "guid-1",
            TrustedWorkKind.METADATA,
            helper_incarnation=1,
        )
    )
    assert len(window.RunList.metadata_updates) == 1

    old_generation = coordinator.generation
    bridge._publish(
        _publication(
            bridge,
            coordinator,
            "guid-1",
            TrustedWorkKind.METADATA,
            helper_incarnation=2,
        )
    )
    assert coordinator.generation == old_generation + 1
    assert len(window.RunList.metadata_updates) == 1

    bridge._publish(
        _publication(
            bridge,
            coordinator,
            "guid-1",
            TrustedWorkKind.METADATA,
            helper_incarnation=2,
        )
    )
    assert len(window.RunList.metadata_updates) == 2


def test_live_facts_reconciliation_and_format_invalidation_are_narrow(
    bound_bridge,
) -> None:
    window, bridge, coordinator, runs = bound_bridge
    item = window.RunList._item_for_guid("guid-1")
    item.run_metadata.update(
        result_count=50,
        is_completed=True,
        completed_timestamp=99.0,
    )
    bridge._publish(
        _publication(bridge, coordinator, "guid-1", TrustedWorkKind.METADATA)
    )
    published = window.RunList.metadata_updates[-1][1]
    assert published["result_count"] == 50
    assert published["is_completed"] is True
    assert published["completed_timestamp"] == 99.0

    changed_before = tuple(coordinator.runs)
    bridge.source_changed((1, 1, 999, "bad"))
    assert coordinator.source_changes == [0]
    assert coordinator.runs[0].source_revision != changed_before[0].source_revision

    extended = dict(runs)
    extended[13] = {"guid": "guid-13", "name": "new"}
    window.RunList.set_runs(extended)
    bridge.reconcile_runs(extended)
    assert coordinator.reconciliations == [13]

    formats_before = dict(bridge._formats)
    bridge.update_preview_size(333)
    assert [kind for kind, _value in coordinator.format_updates] == [
        TrustedWorkKind.PREVIEW
    ]
    assert (
        bridge._formats[TrustedWorkKind.METADATA]
        == formats_before[TrustedWorkKind.METADATA]
    )
    assert (
        bridge._formats[TrustedWorkKind.THUMBNAIL]
        == formats_before[TrustedWorkKind.THUMBNAIL]
    )


def test_unsupported_and_malformed_images_are_bounded_and_nonfatal(
    bound_bridge,
) -> None:
    window, bridge, coordinator, _runs = bound_bridge
    window._selected_run_guid = "guid-1"
    bridge.select_run("guid-1")
    bridge._publish(
        _publication(bridge, coordinator, "guid-1", TrustedWorkKind.METADATA)
    )
    bridge._publish(
        _publication(
            bridge,
            coordinator,
            "guid-1",
            TrustedWorkKind.PREVIEW,
            status="unsupported",
            description="arrays are not supported",
        )
    )
    displayed = window.infoBox.preview.displayed
    assert displayed is not None
    assert displayed[1][0]["unsupported"] is True
    assert window.RunList.metadata_updates

    malformed = _publication(bridge, coordinator, "guid-1", TrustedWorkKind.THUMBNAIL)
    payload = dict(malformed.result)
    payload["images"] = (
        (
            ("width", 4),
            ("height", 4),
            ("dependent", "signal"),
            ("bytes", b"not-png"),
        ),
    )
    bridge._publish(
        WorkPublication(
            malformed.generation,
            malformed.key,
            payload,
            malformed.is_current_selection,
        )
    )
    assert window.RunList.preview_updates[-1][1][0]["unsupported"] is True


def test_shutdown_is_prompt_and_disarms_timers_and_queued_publication(
    bound_bridge,
) -> None:
    window, bridge, coordinator, _runs = bound_bridge
    coordinator.pending.append(
        _publication(bridge, coordinator, "guid-1", TrustedWorkKind.METADATA)
    )
    thread = threading.Thread(target=coordinator.wakeup)
    thread.start()
    thread.join()
    started = time.monotonic()
    bridge.shutdown()
    elapsed = time.monotonic() - started
    QtWidgets.QApplication.processEvents()

    assert elapsed < 0.2
    assert coordinator.closed and coordinator.joined
    assert not bridge._priority_timer.isActive()
    assert not bridge._retire_timer.isActive()
    assert not window.RunList.metadata_updates


def test_large_binding_has_constant_qobject_and_coalesced_event_structure(
    tmp_path,
) -> None:
    path = tmp_path / "large.db"
    path.write_bytes(b"large")
    instance = database_instance(path)
    service = _Service(instance)
    runs = {
        index: {"guid": f"guid-{index}", "name": "large"} for index in range(1, 5_001)
    }
    window = _Window(instance, service, runs)
    started = time.perf_counter()
    bridge = TrustedDerivedQtBridge(window)
    bridge.bind_database(instance, runs, service)
    elapsed = time.perf_counter() - started
    coordinator = _FakeCoordinator.created[-1]

    assert len(_FakeCoordinator.created) == 1
    assert len(coordinator.runs) == 5_000
    assert len(bridge.findChildren(QtCore.QTimer)) == 2
    assert not bridge.findChildren(QtCore.QThread)
    assert not bridge.findChildren(QtCore.QThreadPool)
    for _ in range(1_000):
        bridge.request_priority_update()
    QtWidgets.QApplication.processEvents()
    assert len(coordinator.visible_updates) <= 2
    print(f"Stage 5C 5000-run bridge binding: {elapsed:.6f}s")

    bridge.shutdown()
    window.hide()
    window.deleteLater()


def test_bridge_does_not_duplicate_more_than_512_decoded_previews(tmp_path) -> None:
    path = tmp_path / "bounded-previews.db"
    path.write_bytes(b"bounded-previews")
    instance = database_instance(path)
    service = _Service(instance)
    runs = {
        index: {"guid": f"guid-{index}", "name": "bounded"} for index in range(1, 521)
    }
    window = _Window(instance, service, runs)
    bridge = TrustedDerivedQtBridge(window)
    bridge.bind_database(instance, runs, service)
    coordinator = _FakeCoordinator.created[-1]

    for index in runs:
        bridge._publish(
            _image_publication(
                bridge,
                coordinator,
                f"guid-{index}",
                TrustedWorkKind.PREVIEW,
            )
        )

    assert len(window.infoBox.preview.retained) == 512
    assert not hasattr(bridge, "_previews_by_guid")

    bridge.shutdown()
    window.hide()
    window.deleteLater()


def test_preview_cache_byte_limit_is_injectable_and_evicts_deterministically() -> None:
    preview = PreviewTab(
        preview_size=40,
        cache_max_entries=10,
        cache_max_bytes=100,
    )
    runs = {
        1: {"guid": "guid-1"},
        2: {"guid": "guid-2"},
    }
    preview.set_trusted_derived_runs(runs)
    image = QtGui.QImage(4, 4, QtGui.QImage.Format.Format_RGBA8888)
    image.fill(QtGui.QColor("#336699"))
    values = [{"parameter": "signal", "title": "Signal", "image": image}]

    preview.publish_trusted_previews("guid-1", values)
    preview.publish_trusted_previews("guid-2", values)

    assert tuple(preview.cache) == ("guid-2",)
    assert preview.cache_bytes == image.sizeInBytes()
    preview.shutdown()
    preview.deleteLater()

    selected = PreviewTab(
        preview_size=40,
        cache_max_entries=10,
        cache_max_bytes=image.sizeInBytes() - 1,
    )
    selected.set_trusted_derived_runs({1: {"guid": "guid-1"}})
    selected.set_current_guid("guid-1")
    selected.publish_trusted_previews("guid-1", values)
    assert selected.cache == {}
    assert selected.cache_bytes == 0
    selected.shutdown()
    selected.deleteLater()


def test_large_run_list_discards_hidden_decoded_thumbnails(tmp_path) -> None:
    path = tmp_path / "bounded-thumbnails.db"
    path.write_bytes(b"bounded-thumbnails")
    instance = database_instance(path)
    service = _Service(instance)
    runs = {
        index: {"guid": f"guid-{index}", "name": "large"} for index in range(1, 1_002)
    }
    window = _Window(instance, service, runs)
    bridge = TrustedDerivedQtBridge(window)
    bridge.bind_database(instance, runs, service)
    coordinator = _FakeCoordinator.created[-1]

    with patch.object(bridge, "_decode_images", wraps=bridge._decode_images) as decode:
        for index in runs:
            bridge._publish(
                _image_publication(
                    bridge,
                    coordinator,
                    f"guid-{index}",
                    TrustedWorkKind.THUMBNAIL,
                )
            )

    assert decode.call_count == 0
    assert not hasattr(bridge, "_thumbnails_by_guid")
    assert window.RunList.preview_updates == []

    bridge.shutdown()
    window.hide()
    window.deleteLater()


def test_selecting_evicted_preview_requests_one_preview_only_replay(
    bound_bridge,
) -> None:
    window, bridge, coordinator, _runs = bound_bridge
    guid = "guid-1"
    for kind in TrustedWorkKind:
        coordinator.completed.add((0, kind))
    window._selected_run_guid = guid
    bridge._publish(_publication(bridge, coordinator, guid, TrustedWorkKind.METADATA))
    bridge._publish(
        _image_publication(bridge, coordinator, guid, TrustedWorkKind.THUMBNAIL)
    )
    bridge._publish(
        _image_publication(bridge, coordinator, guid, TrustedWorkKind.PREVIEW)
    )
    window.infoBox.preview.evict(guid)

    owner = threading.get_ident()
    bridge.select_run(guid)
    bridge.select_run(guid)

    assert coordinator.replay_requests == [(0, TrustedWorkKind.PREVIEW)]
    assert coordinator.selections[-1] == 0
    assert (0, TrustedWorkKind.METADATA) in coordinator.completed
    assert (0, TrustedWorkKind.THUMBNAIL) in coordinator.completed
    bridge._publish(
        _image_publication(bridge, coordinator, guid, TrustedWorkKind.PREVIEW)
    )
    assert window.infoBox.preview.displayed is not None
    assert window.infoBox.preview.threads[-1] == owner
    assert not window.infoBox.preview.__dict__.get("_workers", {})
