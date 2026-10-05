"""Deterministic Qt-boundary regressions for Stage 5C trusted derived work."""

from __future__ import annotations

import json
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from PyQt6 import QtCore, QtGui, QtWidgets

from qplot.datahandling.file_identity import DatabaseInstance, database_instance
from qplot.datahandling.trusted_live_queries import (
    TrustedParameterView,
    TrustedRunRecord,
    TrustedSelectedRunDetail,
    TrustedSetpointSummary,
    TrustedSourceRevision,
    TrustedSourceRevisionNamespace,
)
from qplot.datahandling.trusted_live_service import TrustedLiveReadService
from qplot.datahandling.trusted_presentation import build_selected_run_presentation
from qplot.datahandling.trusted_snapshot import (
    TRUSTED_SNAPSHOT_LOAD_MORE_TEXT,
    normalize_trusted_snapshot,
)
from qplot.datahandling.trusted_work_coordinator import (
    TrustedDerivedRun,
    TrustedSelectedDetailPublication,
)
from qplot.datahandling.trusted_work_coordinator import (
    TrustedWorkCoordinator as _RealTrustedWorkCoordinator,
)
from qplot.datahandling.trusted_work_scheduler import (
    TrustedCacheWorkKey,
    TrustedWorkKind,
    WorkPublication,
)
from qplot.windows import _database_actions as database_actions
from qplot.windows import _trusted_derived_qt as bridge_module
from qplot.windows._trusted_derived_qt import TrustedDerivedQtBridge
from qplot.windows._widgets.details_tables import (
    SnapshotTreePageRequest,
    SnapshotTreeValueRequest,
)
from qplot.windows._widgets.preview import PreviewTab
from qplot.windows._widgets.treeWidgets import moreInfo
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
        self._session_generation = 1

    @property
    def database_instance(self) -> DatabaseInstance:
        return self._instance

    @property
    def source_revision_namespace(self) -> TrustedSourceRevisionNamespace:
        return self._namespace


class _ObjectSignal:
    def __init__(self) -> None:
        self._slots = []

    def connect(self, slot) -> None:
        self._slots.append(slot)

    def emit(self, value) -> None:
        for slot in tuple(self._slots):
            slot(value)


class _BlockingSnapshotSource:
    def __init__(self, session_token: str) -> None:
        self.session_token = session_token
        self.release = threading.Event()
        self.started = threading.Event()
        self.finished = threading.Event()
        self.calls: list[tuple[str, object, object | None, int]] = []

    def _wait(self, cancel_check) -> None:
        self.started.set()
        while not self.release.wait(0.002):
            cancel_check()

    def page(self, parent, cursor, *, cancel_check):
        self.calls.append(("page", parent, cursor, threading.get_ident()))
        try:
            self._wait(cancel_check)
        finally:
            self.finished.set()
        return SimpleNamespace(
            parent_handle=parent,
            request_continuation=cursor,
            nodes=(),
            continuation=None,
            rendered_text_bytes=0,
        )

    def full_value(self, handle, *, cancel_check):
        self.calls.append(("value", handle, None, threading.get_ident()))
        try:
            self._wait(cancel_check)
        finally:
            self.finished.set()
        return SimpleNamespace(text="complete value", utf8_bytes=14)


@dataclass(frozen=True, slots=True)
class _SourceSnapshotView:
    nodes: tuple[object, ...]
    parameters: tuple[object, ...]
    status: str
    message: str
    input_bytes: int
    session_token: str
    source: object | None


def _with_snapshot_source(
    detail: TrustedSelectedRunDetail,
    source: _BlockingSnapshotSource,
) -> TrustedSelectedRunDetail:
    snapshot = detail.snapshot
    return replace(
        detail,
        snapshot=_SourceSnapshotView(
            nodes=tuple(snapshot.nodes),
            parameters=tuple(snapshot.parameters),
            status=snapshot.status,
            message=snapshot.message,
            input_bytes=snapshot.input_bytes,
            session_token=source.session_token,
            source=source,
        ),
    )


class _SelectedRequest:
    def __init__(
        self,
        detail: TrustedSelectedRunDetail,
        release: threading.Event | None = None,
    ) -> None:
        self.detail = detail
        self.release = release
        self.cancelled = False

    @property
    def done(self) -> bool:
        return self.cancelled or self.release is None or self.release.is_set()

    def cancel(self) -> bool:
        self.cancelled = True
        return True

    def wait(self, _timeout: float | None = None) -> TrustedSelectedRunDetail:
        if self.cancelled:
            raise InterruptedError("selected-detail request cancelled")
        if not self.done:
            raise TimeoutError("selected-detail request remains blocked")
        return self.detail


class _SelectedService(_Service):
    def __init__(
        self,
        instance: DatabaseInstance,
        details: dict[int, TrustedSelectedRunDetail],
        *,
        releases: dict[int, threading.Event] | None = None,
    ) -> None:
        super().__init__(instance, b"selected-detail")
        self.details = dict(details)
        self.releases = dict(releases or {})
        self.selected_submissions: list[tuple[int, dict[str, object]]] = []
        self.selected_requests: dict[int, list[_SelectedRequest]] = {}

    def submit_selected_run(self, run_id: int, **kwargs) -> _SelectedRequest:
        self.selected_submissions.append((run_id, dict(kwargs)))
        request = _SelectedRequest(self.details[run_id], self.releases.get(run_id))
        self.selected_requests.setdefault(run_id, []).append(request)
        return request


class _DerivedCacheHits:
    """Return deterministic derived hits without retaining selected detail."""

    def __init__(self, run_ids: dict[str, int]) -> None:
        self.run_ids = dict(run_ids)
        self.hits: list[tuple[str, TrustedWorkKind]] = []
        self.puts = 0

    def configure_for_database(self, _database: DatabaseInstance) -> None:
        return None

    def get(self, key, *, cancel_check=None):
        if cancel_check is not None:
            cancel_check()
        self.hits.append((key.run_guid, key.kind))
        run_id = self.run_ids[key.run_guid]
        payload: dict[str, object] = {
            "format": "qplot-trusted-derived-payload-v1",
            "kind": key.kind.name.lower(),
            "status": "empty",
            "description": "cached derived result",
            "source": (
                ("run_id", run_id),
                ("run_guid", key.run_guid),
                ("helper_incarnation", 1),
            ),
            "images": (),
        }
        if key.kind is TrustedWorkKind.METADATA:
            payload.update(
                status="ok",
                metadata=(
                    ("run_id", run_id),
                    ("guid", key.run_guid),
                    (
                        "run_fields",
                        (
                            ("run_id", run_id),
                            ("guid", key.run_guid),
                            ("name", f"cached-{key.run_guid}"),
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
                ),
            )
        return payload

    def put(self, *_args, **_kwargs) -> None:
        self.puts += 1


def _rich_selected_detail(run_id: int, guid: str) -> TrustedSelectedRunDetail:
    parameters = (
        TrustedParameterView("x", "Gate", "V", (), "numeric"),
        TrustedParameterView("signal", "Signal", "A", ("x",), "numeric"),
    )
    summaries = (TrustedSetpointSummary("x", 0.0, 1.0, 3),)
    metadata = (("operator", "Ada"), ("purpose", "selected detail"))
    snapshot = normalize_trusted_snapshot(
        '{"station":{"parameters":{"gate":{"value":1.25,"unit":"V"}}}}'
    )
    run_fields = {
        "run_id": run_id,
        "guid": guid,
        "name": f"detail-{guid}",
        "result_count": 3,
    }
    presentation = build_selected_run_presentation(
        run_fields=run_fields,
        metadata_fields=dict(metadata),
        parameters=tuple(
            {
                "name": parameter.name,
                "label": parameter.label,
                "unit": parameter.unit,
                "depends_on": parameter.depends_on,
                "type": parameter.paramtype,
            }
            for parameter in parameters
        ),
        snapshot_summary={
            "Status": snapshot.status,
            "Message": snapshot.message,
            "Input bytes": snapshot.input_bytes,
            "Rendered nodes": len(snapshot.nodes),
        },
        setpoint_summaries=tuple(
            {
                "name": summary.name,
                "from": summary.first,
                "to": summary.last,
                "steps": summary.steps,
            }
            for summary in summaries
        ),
        unavailable_fields=("optional_field",),
    )
    return TrustedSelectedRunDetail(
        run=TrustedRunRecord(run_id, tuple(run_fields.items())),
        parameters=parameters,
        metadata=metadata,
        snapshot=snapshot,
        setpoint_summaries=summaries,
        presentation=presentation,
        unavailable_fields=("optional_field",),
    )


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
        self.loading.append((dict(run), threading.get_ident()))
        self.preview.set_current_guid(str(run["guid"]))


class _TrackingSnapshotInfoBox(moreInfo):
    """Production details tabs with observable source-free publications."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.displayed_snapshot_sources: list[object | None] = []
        self.displayed_snapshot_sessions: list[str] = []
        self.snapshot_invalidation_count = 0

    def set_snapshot_run_detail(self, detail: TrustedSelectedRunDetail) -> None:
        self.displayed_snapshot_sources.append(detail.snapshot.source)
        self.displayed_snapshot_sessions.append(detail.snapshot.session_token)
        super().set_snapshot_run_detail(detail)

    def invalidate_trusted_snapshot(self) -> None:
        self.snapshot_invalidation_count += 1
        super().invalidate_trusted_snapshot()


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


def _visible_run_guids(run_list: _RunList) -> tuple[str, ...]:
    viewport = run_list.viewport()
    first = run_list.itemAt(QtCore.QPoint(1, 1))
    assert viewport is not None and first is not None
    visible: list[str] = []
    item = first
    while item is not None:
        rect = run_list.visualItemRect(item)
        if rect.isValid() and rect.top() > viewport.rect().bottom():
            break
        visible.append(str(item.guid))
        item = run_list.itemBelow(item)
    return tuple(visible)


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


def _selected_detail_publication(
    bridge: TrustedDerivedQtBridge,
    coordinator: Any,
    detail: TrustedSelectedRunDetail,
    *,
    helper_incarnation: int = 1,
    selection_generation: int = 7,
) -> TrustedSelectedDetailPublication:
    guid = str(detail.run.as_dict()["guid"])
    publication = _publication(
        bridge,
        coordinator,
        guid,
        TrustedWorkKind.METADATA,
        helper_incarnation=helper_incarnation,
    )
    run_index = next(
        index for index, run in enumerate(coordinator.runs) if run.run_guid == guid
    )
    return TrustedSelectedDetailPublication(
        generation=publication.generation,
        key=publication.key,
        run_index=run_index,
        run_id=detail.run.run_id,
        run_guid=guid,
        helper_incarnation=helper_incarnation,
        selection_generation=selection_generation,
        detail=detail,
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


def _bind_real_detail_bridge(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    *,
    selected_guid: str,
    details: dict[int, TrustedSelectedRunDetail],
    releases: dict[int, threading.Event] | None = None,
):
    path = tmp_path / "selected-detail.db"
    path.write_bytes(b"selected-detail")
    instance = database_instance(path)
    runs = {
        run_id: {
            "run_id": run_id,
            "guid": detail.run.as_dict()["guid"],
            "name": f"run-{run_id}",
            "result_count": 3,
        }
        for run_id, detail in details.items()
    }
    service = _SelectedService(instance, details, releases=releases)
    cache = _DerivedCacheHits(
        {str(metadata["guid"]): run_id for run_id, metadata in runs.items()}
    )

    def coordinator_factory(database, derived_runs, active_service, **kwargs):
        return _RealTrustedWorkCoordinator(
            database,
            derived_runs,
            active_service,
            cache=cache,
            **kwargs,
        )

    monkeypatch.setattr(
        bridge_module,
        "TrustedWorkCoordinator",
        coordinator_factory,
    )
    window = _Window(instance, service, runs)
    window._selected_run_guid = selected_guid
    bridge = TrustedDerivedQtBridge(window)
    bridge.bind_database(instance, runs, service)
    coordinator = bridge.coordinator
    assert coordinator is not None
    return window, bridge, coordinator, service, cache


def _shutdown_real_bridge(window: _Window, bridge: TrustedDerivedQtBridge) -> None:
    bridge.shutdown()
    _process_until(lambda: not bridge.background_active())
    window.hide()
    window.deleteLater()


def _install_snapshot_session(
    window: _Window,
    bridge: TrustedDerivedQtBridge,
    coordinator: _FakeCoordinator,
    source: _BlockingSnapshotSource,
) -> tuple[TrustedSelectedRunDetail, TrustedSelectedDetailPublication]:
    window._selected_run_guid = "guid-1"
    bridge.select_run("guid-1")
    detail = _with_snapshot_source(_rich_selected_detail(1, "guid-1"), source)
    publication = _selected_detail_publication(bridge, coordinator, detail)
    bridge._publish_selected_detail(publication)
    return detail, publication


def _wait_snapshot_worker_without_qt(
    bridge: TrustedDerivedQtBridge,
    timeout: float = 2.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with bridge._snapshot_work_lock:
            if not bridge._snapshot_live_futures:
                return
        time.sleep(0.002)
    raise AssertionError("Snapshot worker did not finish")


def test_snapshot_source_is_detached_and_page_and_value_publish_on_owner(
    bound_bridge,
) -> None:
    window, bridge, coordinator, _runs = bound_bridge
    owner = threading.get_ident()
    source = _BlockingSnapshotSource("snapshot-session-1")
    detail, _publication = _install_snapshot_session(
        window,
        bridge,
        coordinator,
        source,
    )

    displayed = window.infoBox.active_detail
    retained = bridge._selected_detail_publication
    assert displayed is not None and displayed is not detail
    assert displayed.snapshot.source is None
    assert retained is not None and retained.detail.snapshot.source is None
    assert bridge._snapshot_source is source

    page_request = SnapshotTreePageRequest(
        source.session_token,
        "root-container",
        None,
    )
    window.infoBox.snapshotPageRequested.emit(page_request)
    assert source.started.wait(1.0)
    assert bridge.background_active()
    source.release.set()
    _process_until(lambda: len(window.infoBox.snapshot_pages) == 1)

    page, page_thread = window.infoBox.snapshot_pages[0]
    assert page.parent_handle == "root-container"
    assert page_thread == owner
    assert source.calls[0][3] != owner

    value_request = SnapshotTreeValueRequest(
        source.session_token,
        "full-value-handle",
        "/Snapshot/value (value)",
        "root-container",
        None,
    )
    window.infoBox.snapshotFullValueRequested.emit(value_request)
    _process_until(lambda: len(window.infoBox.snapshot_values) == 1)
    accepted_request, result, value_thread = window.infoBox.snapshot_values[0]
    assert accepted_request is value_request
    assert result.text == "complete value"
    assert value_thread == owner
    assert not window.infoBox.snapshot_rejections


@pytest.mark.parametrize("request_kind", ["page", "value"])
def test_snapshot_result_queued_past_owner_deadline_is_rejected(
    bound_bridge,
    request_kind,
) -> None:
    window, bridge, coordinator, _runs = bound_bridge
    source = _BlockingSnapshotSource("snapshot-owner-deadline")
    _install_snapshot_session(window, bridge, coordinator, source)
    if request_kind == "page":
        request = SnapshotTreePageRequest(
            source.session_token,
            "root-container",
            None,
        )
        window.infoBox.snapshotPageRequested.emit(request)
    else:
        request = SnapshotTreeValueRequest(
            source.session_token,
            "full-value-handle",
            "/Snapshot/value (value)",
            "root-container",
            None,
        )
        window.infoBox.snapshotFullValueRequested.emit(request)
    assert source.started.wait(1.0)
    source.release.set()
    assert source.finished.wait(1.0)
    _wait_snapshot_worker_without_qt(bridge)
    with bridge._snapshot_work_lock:
        work = next(iter(bridge._snapshot_slots.values()))

    with patch.object(bridge_module.time, "monotonic", return_value=work.deadline):
        bridge._publish_snapshot_results()

    assert not window.infoBox.snapshot_pages
    assert not window.infoBox.snapshot_values
    assert [item for item, _thread in window.infoBox.snapshot_rejections] == [request]


def test_many_snapshot_sessions_retain_only_current_source_and_replace_tree(
    tmp_path,
) -> None:
    path = tmp_path / "many-snapshot-sessions.db"
    path.write_bytes(b"many-snapshot-sessions")
    instance = database_instance(path)
    service = _Service(instance)
    runs = {
        1: {
            "run_id": 1,
            "guid": "guid-1",
            "name": "run-1",
            "result_count": 150,
        }
    }
    window = _Window(instance, service, runs)
    info_box = _TrackingSnapshotInfoBox(window)
    window.infoBox = info_box
    layout = window.layout()
    assert layout is not None
    layout.addWidget(info_box)
    bridge = TrustedDerivedQtBridge(window)
    bridge.bind_database(instance, runs, service)
    coordinator = _FakeCoordinator.created[-1]
    base_detail = _rich_selected_detail(1, "guid-1")
    sources = []
    prior_cancel_events: list[threading.Event] = []
    session_count = 24

    try:
        window._selected_run_guid = "guid-1"
        bridge.select_run("guid-1")
        for session_index in range(session_count):
            prefix = f"session_{session_index:02d}_field_"
            snapshot = normalize_trusted_snapshot(
                json.dumps(
                    {
                        f"{prefix}{field_index:03d}": field_index
                        for field_index in range(150)
                    },
                    separators=(",", ":"),
                )
            )
            assert snapshot.source is not None
            sources.append(snapshot.source)
            detail = replace(base_detail, snapshot=snapshot)
            bridge._publish_selected_detail(
                _selected_detail_publication(bridge, coordinator, detail)
            )

            assert bridge._snapshot_source is snapshot.source
            assert bridge._selected_detail_publication is not None
            assert bridge._selected_detail_publication.detail.snapshot.source is None
            assert info_box.displayed_snapshot_sources[-1] is None
            assert info_box.snapshot.snapshotSessionToken == snapshot.session_token
            assert all(cancel.is_set() for cancel in prior_cancel_events)

            snapshot_tree = info_box.snapshot
            assert snapshot_tree.topLevelItemCount() == 128
            current_keys = tuple(
                snapshot_tree.topLevelItem(index).text(0)
                for index in range(snapshot_tree.topLevelItemCount())
            )
            assert current_keys[:-1] == tuple(
                f"{prefix}{field_index:03d}" for field_index in range(127)
            )
            assert current_keys[-1] == TRUSTED_SNAPSHOT_LOAD_MORE_TEXT
            assert all(
                value is not source
                for source in sources
                for value in snapshot_tree.__dict__.values()
            )

            if session_index + 1 < session_count:
                current_cancel = bridge._snapshot_session_cancel
                assert current_cancel is not None and not current_cancel.is_set()
                prior_cancel_events.append(current_cancel)
                snapshot_tree.itemActivated.emit(
                    snapshot_tree.topLevelItem(127),
                    0,
                )

        _process_until(lambda: not bridge.background_active(), timeout=5.0)
        QtWidgets.QApplication.processEvents()

        assert bridge._snapshot_source is sources[-1]
        assert all(bridge._snapshot_source is not source for source in sources[:-1])
        assert all(cancel.is_set() for cancel in prior_cancel_events)
        assert info_box.displayed_snapshot_sources == [None] * session_count
        assert len(info_box.displayed_snapshot_sessions) == session_count
        assert info_box.displayed_snapshot_sessions[-1] == sources[-1].session_token
        assert info_box.snapshot_invalidation_count >= session_count
        assert info_box.snapshot.topLevelItemCount() == 128
        assert (
            info_box.snapshot.topLevelItem(0)
            .text(0)
            .startswith(f"session_{session_count - 1:02d}_")
        )
        assert info_box.snapshot.topLevelItem(127).text(0) == (
            TRUSTED_SNAPSHOT_LOAD_MORE_TEXT
        )
    finally:
        bridge.shutdown()
        _process_until(lambda: not bridge.background_active())
        window.hide()
        window.deleteLater()


def test_snapshot_diagnostic_without_source_is_still_accepted(bound_bridge) -> None:
    window, bridge, coordinator, _runs = bound_bridge
    window._selected_run_guid = "guid-1"
    bridge.select_run("guid-1")
    detail = replace(
        _rich_selected_detail(1, "guid-1"),
        snapshot=normalize_trusted_snapshot(None),
    )
    bridge._publish_selected_detail(
        _selected_detail_publication(bridge, coordinator, detail)
    )

    assert window.infoBox.active_detail is detail
    assert detail.snapshot.source is None
    assert detail.snapshot.session_token == ""
    assert bridge._snapshot_source is None


@pytest.mark.parametrize("request_kind", ["page", "value"])
def test_snapshot_widget_rejection_releases_exact_request(
    bound_bridge,
    request_kind,
) -> None:
    window, bridge, coordinator, _runs = bound_bridge
    source = _BlockingSnapshotSource("snapshot-widget-rejection")
    _install_snapshot_session(window, bridge, coordinator, source)
    request = SnapshotTreeValueRequest(
        source.session_token,
        "value-handle",
        "/Snapshot/value (value)",
        "root-parent",
        None,
    )
    if request_kind == "page":
        request = SnapshotTreePageRequest(
            source.session_token,
            "root-parent",
            None,
        )
        window.infoBox.accept_snapshot_pages = False
        window.infoBox.snapshotPageRequested.emit(request)
    else:
        window.infoBox.accept_snapshot_values = False
        window.infoBox.snapshotFullValueRequested.emit(request)
    assert source.started.wait(1.0)
    source.release.set()
    _process_until(lambda: bool(window.infoBox.snapshot_rejections))

    assert [item for item, _thread in window.infoBox.snapshot_rejections] == [request]
    assert not window.infoBox.snapshot_pages
    assert not window.infoBox.snapshot_values


@pytest.mark.parametrize("request_kind", ["page", "value"])
@pytest.mark.parametrize(
    "stale_component",
    [
        "database_instance",
        "binding_serial",
        "coordinator_generation",
        "helper_incarnation",
        "source_revision",
        "selection_generation",
        "run_index",
        "run_id",
        "run_guid",
        "service_session_generation",
        "snapshot_token",
        "source_identity",
        "parent_handle",
        "continuation",
    ],
)
def test_late_snapshot_result_is_rejected_by_every_fence_component(
    bound_bridge,
    tmp_path,
    request_kind,
    stale_component,
) -> None:
    window, bridge, coordinator, _runs = bound_bridge
    source = _BlockingSnapshotSource("snapshot-stale-session")
    _detail, _publication = _install_snapshot_session(
        window,
        bridge,
        coordinator,
        source,
    )
    request = SimpleNamespace(
        session_token=source.session_token,
        parent_handle="parent-1",
        continuation="cursor-1",
        handle="value-1",
        path="/Snapshot/value (value)",
    )
    if request_kind == "page":
        window.infoBox.snapshotPageRequested.emit(request)
    else:
        window.infoBox.snapshotFullValueRequested.emit(request)
    assert source.started.wait(1.0)
    source.release.set()
    assert source.finished.wait(1.0)
    _wait_snapshot_worker_without_qt(bridge)

    retained = bridge._selected_detail_publication
    assert retained is not None
    if stale_component == "database_instance":
        other_path = tmp_path / "other-snapshot.db"
        other_path.write_bytes(b"other snapshot")
        bridge._database_instance = database_instance(other_path)
    elif stale_component == "binding_serial":
        bridge._binding_serial += 1
    elif stale_component == "coordinator_generation":
        assert bridge._coordinator_generation is not None
        bridge._coordinator_generation += 1
    elif stale_component == "helper_incarnation":
        bridge._selected_detail_publication = replace(
            retained,
            helper_incarnation=retained.helper_incarnation + 1,
        )
    elif stale_component == "source_revision":
        bridge._selected_detail_publication = replace(
            retained,
            key=replace(
                retained.key,
                source_revision=TrustedSourceRevision(b"stale-source"),
            ),
        )
    elif stale_component == "selection_generation":
        bridge._selected_detail_publication = replace(
            retained,
            selection_generation=retained.selection_generation + 1,
        )
    elif stale_component == "run_index":
        bridge._selected_detail_publication = replace(
            retained,
            run_index=retained.run_index + 1,
        )
    elif stale_component == "run_id":
        bridge._selected_detail_publication = replace(
            retained,
            run_id=retained.run_id + 1,
        )
    elif stale_component == "run_guid":
        bridge._selected_detail_publication = replace(
            retained,
            run_guid="guid-2",
        )
    elif stale_component == "service_session_generation":
        window._trusted_read_service._session_generation += 1
    elif stale_component == "snapshot_token":
        bridge._snapshot_session_token = "new-snapshot-token"
    elif stale_component == "source_identity":
        bridge._snapshot_source = object()
    elif stale_component == "parent_handle":
        request.parent_handle = "parent-2"
    elif stale_component == "continuation":
        request.continuation = "cursor-2"
    else:  # pragma: no cover - parametrization is exhaustive
        raise AssertionError(stale_component)

    # The worker drops its live-future marker before emitting the queued Qt
    # signal. One processEvents call may precede that emission entirely.
    _process_until(lambda: bool(window.infoBox.snapshot_rejections))
    assert not window.infoBox.snapshot_pages
    assert not window.infoBox.snapshot_values
    assert [item for item, _thread in window.infoBox.snapshot_rejections] == [request]


def test_snapshot_executor_has_one_worker_two_slots_and_cancels_with_session(
    bound_bridge,
) -> None:
    window, bridge, coordinator, _runs = bound_bridge
    source = _BlockingSnapshotSource("snapshot-bounded-session")
    _install_snapshot_session(window, bridge, coordinator, source)
    requests = [
        SnapshotTreePageRequest(source.session_token, f"parent-{index}", None)
        for index in range(3)
    ]

    window.infoBox.snapshotPageRequested.emit(requests[0])
    assert source.started.wait(1.0)
    window.infoBox.snapshotPageRequested.emit(requests[1])
    window.infoBox.snapshotPageRequested.emit(requests[2])

    assert len(source.calls) == 1
    assert bridge.background_active()
    assert [item for item, _thread in window.infoBox.snapshot_rejections] == [
        requests[2]
    ]

    bridge.source_changed((1,))
    _process_until(lambda: not bridge.background_active())
    assert bridge._snapshot_source is None
    assert not window.infoBox.snapshot_pages


def test_replaced_snapshot_session_releases_cancelled_work_source(
    bound_bridge,
) -> None:
    window, bridge, coordinator, _runs = bound_bridge
    first = _BlockingSnapshotSource("snapshot-replaced-first")
    _install_snapshot_session(window, bridge, coordinator, first)
    request = SnapshotTreePageRequest(
        first.session_token,
        "root-container",
        None,
    )
    window.infoBox.snapshotPageRequested.emit(request)
    assert first.started.wait(1.0)
    with bridge._snapshot_work_lock:
        cancelled_work = next(iter(bridge._snapshot_slots.values()))
    assert cancelled_work.source is first

    second = _BlockingSnapshotSource("snapshot-replaced-second")
    detail = _with_snapshot_source(_rich_selected_detail(1, "guid-1"), second)
    bridge._publish_selected_detail(
        _selected_detail_publication(bridge, coordinator, detail)
    )

    assert bridge._snapshot_source is second
    assert cancelled_work.source is None
    assert first.finished.wait(1.0)
    _process_until(lambda: not bridge.background_active())
    assert not window.infoBox.snapshot_pages


@pytest.mark.parametrize("request_kind", ["page", "value"])
def test_shutdown_rejects_late_snapshot_page_or_dialog(
    bound_bridge,
    request_kind,
) -> None:
    window, bridge, coordinator, _runs = bound_bridge
    source = _BlockingSnapshotSource("snapshot-shutdown")
    _install_snapshot_session(window, bridge, coordinator, source)
    if request_kind == "page":
        request = SnapshotTreePageRequest(
            source.session_token,
            "root-container",
            None,
        )
        window.infoBox.snapshotPageRequested.emit(request)
    else:
        request = SnapshotTreeValueRequest(
            source.session_token,
            "full-value-handle",
            "/Snapshot/value (value)",
            "root-container",
            None,
        )
        window.infoBox.snapshotFullValueRequested.emit(request)
    assert source.started.wait(1.0)
    with bridge._snapshot_work_lock:
        cancelled_work = next(iter(bridge._snapshot_slots.values()))

    bridge.shutdown()

    assert bridge._snapshot_source is None
    assert cancelled_work.source is None
    assert source.finished.wait(1.0)
    _process_until(lambda: not bridge.background_active())
    QtWidgets.QApplication.processEvents()
    assert not window.infoBox.snapshot_pages
    assert not window.infoBox.snapshot_values
    assert [item for item, _thread in window.infoBox.snapshot_rejections] == [request]


def test_selected_metadata_cache_hit_carries_bounded_detail_without_losing_preview(
    monkeypatch,
    tmp_path,
) -> None:
    detail = _rich_selected_detail(1, "guid-1")
    window, bridge, coordinator, service, cache = _bind_real_detail_bridge(
        monkeypatch,
        tmp_path,
        selected_guid="guid-1",
        details={1: detail},
    )
    try:
        _process_until(lambda: bool(window.RunList.metadata_updates))

        assert ("guid-1", TrustedWorkKind.METADATA) in cache.hits
        _process_until(lambda: bool(service.selected_submissions))
        assert [run_id for run_id, _kwargs in service.selected_submissions] == [1]
        _process_until(lambda: window.infoBox.active_detail is not None)
        accepted_detail = window.infoBox.active_detail
        assert accepted_detail is not detail
        assert accepted_detail.snapshot.source is None
        assert bridge._snapshot_source is detail.snapshot.source

        raw_keys = {node.key for node in detail.presentation.raw.nodes}
        assert {
            "Run",
            "Metadata",
            "Snapshot",
            "Parameters",
            "Setpoint summaries",
            "Unavailable fields",
        } <= raw_keys
        assert detail.presentation.raw.status == "truncated"
        assert detail.presentation.metadata.status == "available"
        assert detail.snapshot.status == "available"
        assert window.infoBox.preview.current_guid == "guid-1"
        assert cache.puts == 0

        accepted_count = len(window.infoBox.details)
        derived_count = len(window.infoBox.metadata)
        bridge._publish(
            _publication(
                bridge,
                coordinator,
                "guid-1",
                TrustedWorkKind.METADATA,
            )
        )
        bridge._publish(
            _image_publication(
                bridge,
                coordinator,
                "guid-1",
                TrustedWorkKind.THUMBNAIL,
            )
        )
        bridge._publish(
            _image_publication(
                bridge,
                coordinator,
                "guid-1",
                TrustedWorkKind.PREVIEW,
            )
        )

        assert window.infoBox.active_detail is accepted_detail
        assert len(window.infoBox.details) == accepted_count
        assert len(window.infoBox.metadata) == derived_count
        assert window.infoBox.preview.current_guid == "guid-1"
    finally:
        _shutdown_real_bridge(window, bridge)


def test_reselection_rejects_stale_selected_detail_and_preserves_newer_tabs(
    monkeypatch,
    tmp_path,
) -> None:
    first_release = threading.Event()
    first = _rich_selected_detail(1, "guid-1")
    second = _rich_selected_detail(2, "guid-2")
    window, bridge, coordinator, service, _cache = _bind_real_detail_bridge(
        monkeypatch,
        tmp_path,
        selected_guid="guid-1",
        details={1: first, 2: second},
        releases={1: first_release},
    )
    try:
        _process_until(
            lambda: any(run_id == 1 for run_id, _ in service.selected_submissions)
        )

        window._selected_run_guid = "guid-2"
        bridge.select_run("guid-2")
        bridge._apply_priority()
        first_release.set()
        _process_until(
            lambda: any(run_id == 2 for run_id, _ in service.selected_submissions)
        )
        _process_until(
            lambda: (
                window.infoBox.active_detail is not None
                and window.infoBox.active_detail.run.run_id == 2
            )
        )
        accepted_second = window.infoBox.active_detail

        assert all(detail is not first for detail, _thread in window.infoBox.details)
        assert accepted_second is not second
        assert accepted_second.snapshot.source is None
        assert bridge._snapshot_source is second.snapshot.source
        assert window.infoBox.preview.current_guid == "guid-2"

        bridge._publish(
            _publication(
                bridge,
                coordinator,
                "guid-1",
                TrustedWorkKind.METADATA,
            )
        )
        bridge._publish(
            _publication(
                bridge,
                coordinator,
                "guid-2",
                TrustedWorkKind.METADATA,
            )
        )

        assert window.infoBox.active_detail is accepted_second
        assert window.infoBox.preview.current_guid == "guid-2"
    finally:
        first_release.set()
        _shutdown_real_bridge(window, bridge)


def test_selected_metadata_clears_count_in_overview_and_preview(bound_bridge):
    window, bridge, coordinator, _runs = bound_bridge
    info_box = _TrackingSnapshotInfoBox(window)
    window.infoBox = info_box
    window.layout().addWidget(info_box)
    info_box.preview.set_trusted_derived_runs(window.RunList.all_run_metadata())
    window._selected_run_guid = "guid-1"
    bridge.select_run("guid-1")
    base = _publication(bridge, coordinator, "guid-1", TrustedWorkKind.METADATA)

    def publish(fields):
        payload = dict(base.result)
        metadata = dict(payload["metadata"])
        metadata["run_fields"] = tuple(
            {"run_id": 1, "guid": "guid-1", **fields}.items()
        )
        payload["metadata"] = tuple(metadata.items())
        bridge._publish(replace(base, result=payload))

    def overview():
        return {
            info_box.overview.item(row, 0).text():
            info_box.overview.item(row, 1).text()
            for row in range(info_box.overview.rowCount())
        }

    publish({"result_count": 6, "read_setpoint_count": 6})
    assert overview()["Data points"] == "6"
    publish({})
    assert overview()["Data points"] == "6"
    for _ in range(3):
        publish({"result_count": 106, "read_setpoint_count": None})
        assert "Data points" not in overview()
        assert info_box.preview.run_metadata["guid-1"]["read_setpoint_count"] is None
        publish({})
        assert "Data points" not in overview()


def test_metadata_revision_change_invalidates_viewer_until_current_detail_arrives(
    bound_bridge,
) -> None:
    window, bridge, coordinator, _runs = bound_bridge
    info_box = _TrackingSnapshotInfoBox(window)
    window.infoBox = info_box
    window.layout().addWidget(info_box)
    window._selected_run_guid = "guid-1"
    bridge.select_run("guid-1")

    def selected_detail(text):
        detail = _rich_selected_detail(1, "guid-1")
        return replace(
            detail,
            presentation=build_selected_run_presentation(
                run_fields=detail.run.as_dict(),
                metadata_fields={"operator": text},
                parameters=(),
                setpoint_summaries=(),
                snapshot_summary={},
                unavailable_fields=(),
            ),
        )

    def open_operator_value(text):
        value = next(
            value for value in info_box._trusted_full_values.values()
            if value.text == text
        )
        info_box._show_trusted_full_value(value.identifier, "/Metadata/operator")
        dialog = info_box._full_value_dialog
        assert dialog is not None
        assert dialog._exact_text == text
        return dialog

    old_text = "old operator " * 200
    bridge._publish_selected_detail(
        _selected_detail_publication(bridge, coordinator, selected_detail(old_text))
    )
    dialog = open_operator_value(old_text)
    retained = bridge._selected_detail_publication
    metadata = _publication(bridge, coordinator, "guid-1", TrustedWorkKind.METADATA)

    # Same-revision background metadata must leave an open exact viewer alone.
    bridge._publish(metadata)
    assert bridge._selected_detail_publication is retained
    assert info_box._full_value_dialog is dialog
    assert dialog._exact_text == old_text

    revision = TrustedSourceRevision(b"refined-metadata-revision")
    coordinator._runs = tuple(
        replace(run, source_revision=revision) if run.run_guid == "guid-1" else run
        for run in coordinator.runs
    )
    bridge._publish(replace(metadata, key=replace(metadata.key, source_revision=revision)))
    assert bridge._selected_detail_publication is None
    assert bridge._snapshot_source is None
    assert info_box._full_value_dialog is None
    assert not info_box._trusted_full_values
    assert dialog._exact_text is None

    # A new accepted detail restores current values, never the retired backing.
    new_text = "new operator " * 200
    bridge._publish_selected_detail(
        _selected_detail_publication(bridge, coordinator, selected_detail(new_text))
    )
    assert open_operator_value(new_text) is not dialog
    assert all(value.text != old_text for value in info_box._trusted_full_values.values())


def test_selection_source_helper_database_and_shutdown_boundaries_invalidate_viewer(
    bound_bridge,
) -> None:
    window, bridge, _coordinator, _runs = bound_bridge
    baseline = window.infoBox.full_value_invalidations

    window._selected_run_guid = "guid-1"
    bridge.select_run("guid-1")
    assert window.infoBox.full_value_invalidations == baseline + 1

    bridge.source_changed((1,))
    assert window.infoBox.full_value_invalidations == baseline + 2

    bridge.helper_restarted()
    assert window.infoBox.full_value_invalidations == baseline + 3

    window._selected_run_guid = "guid-2"
    bridge.select_run("guid-2")
    assert window.infoBox.full_value_invalidations == baseline + 4

    bridge.clear_database()
    assert window.infoBox.full_value_invalidations == baseline + 5


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
    assert coordinator.priority_updates[-1] == (
        6,
        coordinator.visible_updates[-1],
    )
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
    assert coordinator.priority_updates[-1] == (
        2,
        coordinator.visible_updates[-1],
    )
    assert bridge.coordinator is original


def test_idle_append_adopts_current_sorted_view_before_first_new_claim(
    bound_bridge,
) -> None:
    window, bridge, coordinator, runs = bound_bridge
    _process_until(lambda: not bridge._priority_timer.isActive())
    assert not coordinator.active
    assert coordinator.snapshot().pending_count == 0

    window._selected_run_guid = "guid-1"
    window.RunList.sortItems(1, QtCore.Qt.SortOrder.AscendingOrder)
    window.RunList.scrollToTop()
    bridge._apply_priority()
    old_priority = coordinator.priority_updates[-1]

    extended = dict(runs)
    extended.update(
        {
            run_id: {
                "run_id": run_id,
                "guid": f"guid-{run_id}",
                "name": f"zz-appended-{run_id:03d}",
            }
            for run_id in range(13, 33)
        }
    )
    window.RunList.set_runs(extended)
    window.RunList.sortItems(1, QtCore.Qt.SortOrder.AscendingOrder)
    selected_item = window.RunList._item_for_guid("guid-32")
    assert selected_item is not None
    window._selected_run_guid = "guid-32"
    window.RunList.setCurrentItem(selected_item)
    window.RunList.scrollToItem(
        selected_item,
        QtWidgets.QAbstractItemView.ScrollHint.PositionAtBottom,
    )
    _process_until(lambda: not bridge._priority_timer.isActive())

    visible_guids = _visible_run_guids(window.RunList)

    stable_index = {
        str(metadata["guid"]): index
        for index, (_run_id, metadata) in enumerate(sorted(extended.items()))
    }
    expected_priority = (
        stable_index["guid-32"],
        tuple(stable_index[guid] for guid in visible_guids),
    )
    assert expected_priority[0] in expected_priority[1]
    assert any(index >= len(runs) for index in expected_priority[1])
    assert expected_priority != old_priority
    assert coordinator._applied_priority != expected_priority

    bridge.reconcile_runs(extended)

    assert coordinator.reconciliation_priorities == [expected_priority]
    assert coordinator.append_claim_starts == [
        (expected_priority[0], expected_priority)
    ]


def test_reused_idle_coordinator_installs_new_database_priority_before_claim(
    bound_bridge,
    tmp_path,
) -> None:
    window, bridge, coordinator, _runs = bound_bridge
    _process_until(lambda: not bridge._priority_timer.isActive())
    assert not coordinator.active
    assert coordinator.snapshot().pending_count == 0

    replacement_path = tmp_path / "replacement.db"
    replacement_path.write_bytes(b"replacement")
    replacement = database_instance(replacement_path)
    replacement_service = _Service(replacement, b"replacement")
    replacement_runs = {
        run_id: {
            "run_id": run_id,
            "guid": f"replacement-guid-{run_id}",
            "name": f"replacement-{run_id:03d}",
        }
        for run_id in range(101, 125)
    }
    window.RunList.set_runs(replacement_runs)
    window.RunList.sortItems(1, QtCore.Qt.SortOrder.AscendingOrder)
    selected_guid = "replacement-guid-121"
    selected_item = window.RunList._item_for_guid(selected_guid)
    assert selected_item is not None
    window._selected_run_guid = selected_guid
    window.RunList.setCurrentItem(selected_item)
    window.RunList.scrollToBottom()
    _process_until(lambda: not bridge._priority_timer.isActive())

    stable_index = {
        str(metadata["guid"]): index
        for index, (_run_id, metadata) in enumerate(sorted(replacement_runs.items()))
    }
    expected_priority = (
        stable_index[selected_guid],
        tuple(stable_index[guid] for guid in _visible_run_guids(window.RunList)),
    )
    assert expected_priority[0] != 0
    assert expected_priority[0] in expected_priority[1]
    assert coordinator._applied_priority != expected_priority
    starts_before = coordinator.started

    window._loaded_database_instance = replacement
    window._trusted_read_service = replacement_service
    bridge.bind_database(replacement, replacement_runs, replacement_service)

    assert bridge.coordinator is coordinator
    assert len(_FakeCoordinator.created) == 1
    assert coordinator.database_switches == [(replacement, expected_priority, True)]
    assert coordinator.database_switch_events[:2] == [
        ("priority", expected_priority),
        ("claim", expected_priority),
    ]
    assert coordinator.database_claim_starts == [
        (replacement, expected_priority[0], expected_priority)
    ]
    assert coordinator.started == starts_before + 1


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
    assert dict(formats_before[TrustedWorkKind.THUMBNAIL].options.values) == {
        "height": 96,
        "width": 96,
    }
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
    prior_updates = len(coordinator.priority_updates)
    for _ in range(1_000):
        bridge.request_priority_update()
    QtWidgets.QApplication.processEvents()
    assert len(coordinator.priority_updates) <= prior_updates + 1
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
