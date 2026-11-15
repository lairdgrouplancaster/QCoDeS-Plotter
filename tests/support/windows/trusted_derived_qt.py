"""Shared fixtures and assertions for trusted derived qt."""

from __future__ import annotations

import threading
from collections import OrderedDict
from types import SimpleNamespace
from typing import Any

from PyQt6 import QtWidgets

from qplot.datahandling.file_identity import DatabaseInstance
from qplot.datahandling.trusted_query_types import (
    TrustedSelectedRunDetail,
    TrustedSourceRevision,
)
from qplot.datahandling.trusted_work_coordinator import (
    TrustedDerivedRun,
)
from qplot.datahandling.trusted_work_scheduler import (
    TrustedCacheWorkKey,
    TrustedWorkKind,
    WorkPublication,
)
from qplot.windows._trusted_derived_qt import TrustedDerivedQtBridge

__all__ = [
    '_FakeCoordinator',
    '_Window',
    '_publication',
]


class _ObjectSignal:
    def __init__(self) -> None:
        self._slots = []

    def connect(self, slot) -> None:
        self._slots.append(slot)

    def emit(self, value) -> None:
        for slot in tuple(self._slots):
            slot(value)


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
        on_selected_detail,
        on_error,
    ) -> None:
        self.database = database
        self._runs = tuple(runs)
        self.service = service
        self.formats = dict(formats)
        self.wakeup = wakeup
        self.on_publish = on_publish
        self.on_selected_detail = on_selected_detail
        self.on_error = on_error
        self.generation = 1
        self.pending: list[WorkPublication] = []
        self.poll_threads: list[int] = []
        self.poll_count = 0
        self.started = 0
        self.selections: list[int | None] = []
        self.visible_updates: list[tuple[int, ...]] = []
        self.priority_updates: list[tuple[int | None, tuple[int, ...]]] = []
        self.source_changes: list[int] = []
        self.format_updates: list[tuple[TrustedWorkKind, object]] = []
        self.reconciliations: list[int] = []
        self.reconciliation_priorities: list[
            tuple[int | None, tuple[int, ...]] | None
        ] = []
        self.append_claim_starts: list[
            tuple[int, tuple[int | None, tuple[int, ...]]]
        ] = []
        self.database_switches: list[
            tuple[
                DatabaseInstance,
                tuple[int | None, tuple[int, ...]] | None,
                bool,
            ]
        ] = []
        self.database_switch_events: list[
            tuple[str, tuple[int | None, tuple[int, ...]] | None]
        ] = []
        self.database_claim_starts: list[
            tuple[
                DatabaseInstance,
                int,
                tuple[int | None, tuple[int, ...]],
            ]
        ] = []
        self._applied_priority: tuple[int | None, tuple[int, ...]] = (None, ())
        self._switched_claim_pending = False
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
        return SimpleNamespace(
            generation=self.generation,
            pending_count=0,
            selected_index=self._applied_priority[0],
        )

    def start(self) -> None:
        self.started += 1
        self._start_switched_claim()

    def poll(self) -> int:
        self.poll_threads.append(threading.get_ident())
        self.poll_count += 1
        self._start_switched_claim()
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

    def set_priority(self, selected_index, visible_indices, *, pump=True) -> None:
        exact_visible = tuple(visible_indices)
        self._applied_priority = (selected_index, exact_visible)
        self.priority_updates.append((selected_index, exact_visible))
        self.selections.append(selected_index)
        self.visible_updates.append(exact_visible)
        if pump:
            self._start_switched_claim()

    def reconcile_runs(self, runs, *, priority=None) -> None:
        old_count = len(self._runs)
        self._runs = tuple(runs)
        self.reconciliations.append(len(self._runs))
        exact_priority = None
        if priority is not None:
            selected_index, visible_indices = priority
            exact_priority = (selected_index, tuple(visible_indices))
            self._applied_priority = exact_priority
            self.priority_updates.append(exact_priority)
            self.selections.append(selected_index)
            self.visible_updates.append(exact_priority[1])
        self.reconciliation_priorities.append(exact_priority)
        if len(self._runs) > old_count:
            selected_index, visible_indices = self._applied_priority
            claim_index = next(
                (
                    index
                    for index in (selected_index, *visible_indices)
                    if index is not None and index >= old_count
                ),
                old_count,
            )
            self.append_claim_starts.append((claim_index, self._applied_priority))

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

    def switch_database(
        self,
        database,
        runs,
        service,
        *,
        priority=None,
        defer_start=False,
    ) -> None:
        self.database = database
        self._runs = tuple(runs)
        self.service = service
        self.generation += 1
        exact_priority = None
        if priority is not None:
            selected_index, visible_indices = priority
            exact_priority = (selected_index, tuple(visible_indices))
            self._applied_priority = exact_priority
            self.priority_updates.append(exact_priority)
            self.selections.append(selected_index)
            self.visible_updates.append(exact_priority[1])
        self.database_switches.append((database, exact_priority, bool(defer_start)))
        self.database_switch_events.append(("priority", exact_priority))
        self._switched_claim_pending = True
        if not defer_start:
            self._start_switched_claim()

    def _start_switched_claim(self) -> None:
        if not self._switched_claim_pending:
            return
        selected_index, visible_indices = self._applied_priority
        claim_index = next(
            (
                index
                for index in (selected_index, *visible_indices)
                if index is not None
            ),
            0,
        )
        self.database_claim_starts.append(
            (self.database, claim_index, self._applied_priority)
        )
        self.database_switch_events.append(("claim", self._applied_priority))
        self._switched_claim_pending = False

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
        self.metadata_loading: set[str] = set()

    def set_runs(self, runs: dict[int, dict[str, object]]) -> None:
        self.clear()
        self._items = {}
        self.metadata_loading.clear()
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

    def set_metadata_loading(self, guids, *, loading=True):
        if loading:
            self.metadata_loading.update(guids)
        else:
            self.metadata_loading.difference_update(guids)

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
        self.snapshotPageRequested = _ObjectSignal()
        self.snapshotFullValueRequested = _ObjectSignal()
        self.metadata: list[tuple[dict[str, object], int]] = []
        self.details: list[tuple[TrustedSelectedRunDetail, int]] = []
        self.active_detail: TrustedSelectedRunDetail | None = None
        self.errors: list[tuple[str, int]] = []
        self.loading: list[tuple[dict[str, object], int]] = []
        self.full_value_invalidations = 0
        self.snapshot_invalidations = 0
        self.snapshot_pages: list[tuple[object, int]] = []
        self.snapshot_values: list[tuple[object, object, int]] = []
        self.snapshot_rejections: list[tuple[object, int]] = []
        self.accept_snapshot_pages = True
        self.accept_snapshot_values = True

    def invalidate_trusted_full_values(self) -> None:
        self.full_value_invalidations += 1

    def invalidate_trusted_snapshot(self) -> None:
        self.snapshot_invalidations += 1

    def accept_snapshot_page(self, page: object) -> bool:
        if not self.accept_snapshot_pages:
            return False
        self.snapshot_pages.append((page, threading.get_ident()))
        return True

    def show_snapshot_full_value(self, request: object, result: object) -> bool:
        if not self.accept_snapshot_values:
            return False
        self.snapshot_values.append((request, result, threading.get_ident()))
        return True

    def reject_snapshot_request(self, request: object) -> None:
        self.snapshot_rejections.append((request, threading.get_ident()))

    def set_trusted_derived_metadata(
        self, run, _parameters, _summaries, _metadata
    ) -> None:
        self.metadata.append((dict(run), threading.get_ident()))
        # The production method rebuilds every tab.  A later derived metadata
        # publication must therefore be suppressed or patched narrowly once
        # richer selected detail has been accepted.
        self.active_detail = None
        self.preview.set_current_guid(str(run["guid"]))

    def _accept_detail(self, detail: TrustedSelectedRunDetail) -> None:
        self.details.append((detail, threading.get_ident()))
        self.active_detail = detail
        self.preview.set_current_guid(str(detail.run.as_dict()["guid"]))

    def set_trusted_run_detail(self, detail: TrustedSelectedRunDetail) -> None:
        self._accept_detail(detail)

    def set_snapshot_run_detail(self, detail: TrustedSelectedRunDetail) -> None:
        self._accept_detail(detail)

    def set_trusted_run_error(self, message, _run) -> None:
        self.errors.append((str(message), threading.get_ident()))

    def set_trusted_run_loading(self, run) -> None:
        self.active_detail = None
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


def _publication(
    bridge: TrustedDerivedQtBridge,
    coordinator: Any,
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
    database = (
        coordinator.database
        if hasattr(coordinator, "database")
        else coordinator.snapshot().database_instance
    )
    key = TrustedCacheWorkKey(
        database,
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
    current_generation = (
        coordinator.generation
        if hasattr(coordinator, "generation")
        else coordinator.snapshot().generation
    )
    return WorkPublication(
        current_generation if generation is None else generation,
        key,
        payload,
        False,
    )
