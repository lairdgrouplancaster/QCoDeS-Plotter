from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from PyQt6 import QtCore

from qplot.datahandling import database as database_module
from qplot.datahandling.file_identity import DatabaseInstance, database_instance
from qplot.datahandling.trusted_live import (
    TrustedLiveQueryError,
    TrustedLiveReaderUnavailableError,
    TrustedLiveUnsupportedSourceError,
)
from qplot.datahandling.trusted_live_queries import (
    TrustedBootstrapResult,
    TrustedRefreshResult,
    TrustedRunPage,
    TrustedRunRecord,
    TrustedSelectedRunDetail,
)
from qplot.datahandling.trusted_live_service import (
    SNAPSHOT_FALLBACK_MODE,
    TRUSTED_LIVE_MODE,
    TrustedReadPriority,
    TrustedReadRequestCancelledError,
)
from qplot.datahandling.trusted_presentation import (
    TRUSTED_PRESENTATION_MAX_ERROR_BYTES,
    build_selected_run_presentation,
)
from qplot.datahandling.trusted_snapshot import normalize_trusted_snapshot


def _wait_for(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("Timed out waiting for deterministic worker state")


@dataclass(slots=True)
class _QueuedOutcome:
    result: object | None = None
    error: BaseException | None = None
    blocked: bool = False
    on_wait: Callable[[], None] | None = None


class _FakeTrustedRequest:
    def __init__(
        self,
        service: _FakeTrustedService,
        label: str,
        outcome: _QueuedOutcome,
        priority: int | TrustedReadPriority = TrustedReadPriority.BOOTSTRAP,
    ) -> None:
        self.service = service
        self.label = label
        self.outcome = outcome
        self.started = threading.Event()
        self.release = threading.Event()
        self.cancelled = threading.Event()
        self._lock = threading.Lock()
        self.cancel_calls = 0
        self.priority = int(priority)
        self.promotions: list[int] = []
        self.reprioritizations: list[int] = []
        if not outcome.blocked:
            self.release.set()

    def wait(self, timeout: float | None = None) -> object:
        del timeout
        self.service.record(("wait", self.label))
        self.started.set()
        callback = self.outcome.on_wait
        if callback is not None:
            callback()
        if not self.release.wait(3.0):
            raise AssertionError(f"Fake request {self.label!r} was not released")
        if self.cancelled.is_set():
            raise TrustedReadRequestCancelledError(
                f"Fake request {self.label!r} was cancelled"
            )
        if self.outcome.error is not None:
            raise self.outcome.error
        return self.outcome.result

    def cancel(self) -> bool:
        with self._lock:
            self.cancel_calls += 1
        self.service.record(("cancel", self.label))
        self.cancelled.set()
        self.release.set()
        return True

    def promote(self, priority: int | TrustedReadPriority) -> bool:
        requested_priority = int(priority)
        if requested_priority >= self.priority:
            return False
        self.promotions.append(requested_priority)
        self.priority = requested_priority
        return True

    def reprioritize(self, priority: int | TrustedReadPriority) -> bool:
        requested_priority = int(priority)
        if requested_priority == self.priority:
            return False
        self.reprioritizations.append(requested_priority)
        self.priority = requested_priority
        return True


class _FakeTrustedService:
    """Deterministic worker-facing service with exact public requests."""

    def __init__(self) -> None:
        self.accepted = False
        self.close_error: BaseException | None = None
        self.closing = False
        self.closed = False
        self.events: list[tuple[Any, ...]] = []
        self.requests: list[_FakeTrustedRequest] = []
        self._outcomes: dict[str, deque[_QueuedOutcome]] = {}
        self._lock = threading.RLock()

    def queue(self, kind: str, *outcomes: _QueuedOutcome) -> None:
        with self._lock:
            self._outcomes.setdefault(kind, deque()).extend(outcomes)

    def record(self, event: tuple[Any, ...]) -> None:
        with self._lock:
            self.events.append(event)

    def _submit(
        self, kind: str, *args: object, **kwargs: object
    ) -> _FakeTrustedRequest:
        with self._lock:
            outcomes = self._outcomes.get(kind)
            if not outcomes:
                raise AssertionError(f"No fake outcome was queued for {kind!r}")
            outcome = outcomes.popleft()
            label = f"{kind}-{len(self.requests) + 1}"
            request = _FakeTrustedRequest(
                self,
                label,
                outcome,
                priority=kwargs.get("priority", TrustedReadPriority.BOOTSTRAP),
            )
            self.requests.append(request)
            self.events.append(("submit", kind, args, kwargs, label))
            return request

    def submit_bootstrap(self, *, deadline: float | None = None) -> _FakeTrustedRequest:
        return self._submit("bootstrap", deadline=deadline)

    def submit_refresh(
        self,
        accepted_run_id: int | None = None,
        *,
        deadline: float | None = None,
    ) -> _FakeTrustedRequest:
        return self._submit("refresh", accepted_run_id, deadline=deadline)

    def submit_basic_page(
        self,
        after_run_id: int,
        through_run_id: int,
        *,
        priority: int | TrustedReadPriority,
        deadline: float | None = None,
    ) -> _FakeTrustedRequest:
        return self._submit(
            "page",
            after_run_id,
            through_run_id,
            priority=priority,
            deadline=deadline,
        )

    def submit_cheap_run(
        self,
        run_id: int,
        *,
        priority: int | TrustedReadPriority,
        deadline: float | None = None,
    ) -> _FakeTrustedRequest:
        return self._submit(
            "cheap",
            run_id,
            priority=priority,
            deadline=deadline,
        )

    def submit_expensive_run(
        self,
        run_id: int,
        *,
        priority: int | TrustedReadPriority,
        deadline: float | None = None,
    ) -> _FakeTrustedRequest:
        return self._submit(
            "expensive",
            run_id,
            priority=priority,
            deadline=deadline,
        )

    def submit_selected_run(
        self,
        run_id: int,
        *,
        priority: int | TrustedReadPriority,
        deadline: float | None = None,
    ) -> _FakeTrustedRequest:
        return self._submit(
            "selected",
            run_id,
            priority=priority,
            deadline=deadline,
        )

    def close_async(self) -> bool:
        self.record(("close_async",))
        self.closed = True
        return True

    def wait_closed(self, timeout: float | None = None) -> bool:
        self.record(("wait_closed", timeout))
        return self.closed


def _bound_database(tmp_path: Path, name: str) -> tuple[Path, DatabaseInstance]:
    path = tmp_path / name
    path.touch()
    return path, database_instance(path)


def _record(run_id: int, guid: str, **fields: object) -> TrustedRunRecord:
    values = (("guid", guid), *(tuple(fields.items())))
    return TrustedRunRecord(run_id, values)  # type: ignore[arg-type]


def _selected_detail(
    run_id: int,
    guid: str,
    **fields: object,
) -> TrustedSelectedRunDetail:
    return TrustedSelectedRunDetail(
        run=_record(run_id, guid, **fields),
        parameters=(),
        metadata=(),
        snapshot=normalize_trusted_snapshot(None),
        setpoint_summaries=(),
        presentation=build_selected_run_presentation(
            run_fields={"run_id": run_id, "guid": guid, **fields},
            metadata_fields={},
            parameters=(),
            snapshot_summary={"Status": "empty"},
            setpoint_summaries=(),
            unavailable_fields=(),
        ),
    )


def test_database_load_uses_trusted_pages_before_any_snapshot_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, accepted = _bound_database(tmp_path, "trusted-first.db")
    service = _FakeTrustedService()
    service.queue(
        "bootstrap",
        _QueuedOutcome(TrustedBootstrapResult(2, 10, 1)),
    )
    service.queue(
        "page",
        _QueuedOutcome(TrustedRunPage((_record(1, "guid-1"),), 0, 2, 1, False)),
        _QueuedOutcome(TrustedRunPage((_record(2, "guid-2"),), 1, 2, 2, True)),
    )
    legacy_calls: list[str] = []
    monkeypatch.setattr(
        database_module,
        "database_is_likely_cloud_placeholder",
        lambda _path: False,
    )
    monkeypatch.setattr(
        database_module,
        "database_access_error",
        lambda *_args, **_kwargs: legacy_calls.append("probe"),
    )
    monkeypatch.setattr(
        database_module,
        "get_runs_basic_via_sql",
        lambda *_args, **_kwargs: legacy_calls.append("snapshot"),
    )

    worker = database_module.DatabaseLoadWorker(
        7,
        str(path),
        expected_database_instance=accepted,
        trusted_service=service,
    )
    statuses: list[tuple[object, ...]] = []
    finished: list[tuple[object, ...]] = []
    worker.signals.status.connect(lambda *args: statuses.append(args))
    worker.signals.finished.connect(lambda *args: finished.append(args))

    worker.run()

    assert legacy_calls == []
    assert [event[1] for event in service.events if event[0] == "submit"] == [
        "bootstrap",
        "page",
        "page",
    ]
    page_submissions = [
        event for event in service.events if event[:2] == ("submit", "page")
    ]
    assert [event[2] for event in page_submissions] == [(0, 2), (1, 2)]
    assert worker.access_mode == TRUSTED_LIVE_MODE
    assert worker.fallback_reason is None
    assert statuses == [
        (7, "Opening trusted live database..."),
        (7, "Loading basic run list..."),
    ]
    assert finished == [
        (
            7,
            str(path),
            {1: {"guid": "guid-1"}, 2: {"guid": "guid-2"}},
            None,
        )
    ]


@pytest.mark.parametrize(
    "error_type",
    [TrustedLiveReaderUnavailableError, TrustedLiveUnsupportedSourceError],
)
def test_database_load_falls_back_only_for_exact_initial_open_outcomes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[BaseException],
) -> None:
    path, accepted = _bound_database(tmp_path, f"fallback-{error_type.__name__}.db")
    service = _FakeTrustedService()
    service.queue(
        "bootstrap",
        _QueuedOutcome(error=error_type("expected initial-open outcome")),
    )
    monkeypatch.setattr(
        database_module,
        "database_is_likely_cloud_placeholder",
        lambda _path: False,
    )

    def probe(*_args: object, **_kwargs: object) -> None:
        service.record(("snapshot_probe",))
        return None

    def snapshot(*_args: object, **_kwargs: object) -> dict[int, dict[str, str]]:
        service.record(("snapshot_read",))
        return {4: {"guid": "snapshot-guid"}}

    monkeypatch.setattr(database_module, "database_access_error", probe)
    monkeypatch.setattr(database_module, "get_runs_basic_via_sql", snapshot)
    worker = database_module.DatabaseLoadWorker(
        8,
        str(path),
        expected_database_instance=accepted,
        trusted_service=service,
    )
    finished: list[tuple[object, ...]] = []
    worker.signals.finished.connect(lambda *args: finished.append(args))

    worker.run()

    event_kinds = [event[0] for event in service.events]
    assert event_kinds == [
        "submit",
        "wait",
        "close_async",
        "wait_closed",
        "snapshot_probe",
        "snapshot_read",
    ]
    assert worker.access_mode == SNAPSHOT_FALLBACK_MODE
    assert worker.fallback_reason == error_type.__name__
    assert finished == [(8, str(path), {4: {"guid": "snapshot-guid"}}, None)]


def test_snapshot_load_strips_raw_run_payloads_before_qt_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, accepted = _bound_database(tmp_path, "bounded-fallback.db")
    service = _FakeTrustedService()
    service.queue(
        "bootstrap",
        _QueuedOutcome(
            error=TrustedLiveUnsupportedSourceError("expected snapshot fallback")
        ),
    )
    marker = "private-fallback-run-value-" * 100_000
    monkeypatch.setattr(
        database_module,
        "database_is_likely_cloud_placeholder",
        lambda _path: False,
    )
    monkeypatch.setattr(
        database_module,
        "database_access_error",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        database_module,
        "get_runs_basic_via_sql",
        lambda *_args, **_kwargs: {
            4: {
                "guid": "snapshot-guid",
                "name": marker,
                "parameters": marker,
                "run_description": marker,
                "snapshot": marker,
                "parent_datasets": marker,
                "measure_parameters": tuple(marker for _index in range(10_000)),
            }
        },
    )
    worker = database_module.DatabaseLoadWorker(
        8,
        str(path),
        expected_database_instance=accepted,
        trusted_service=service,
    )
    finished: list[tuple[object, ...]] = []
    worker.signals.finished.connect(
        lambda *args: finished.append(args),
        type=QtCore.Qt.ConnectionType.DirectConnection,
    )

    worker.run()

    assert len(finished) == 1
    published = finished[0][2][4]
    assert {"parameters", "run_description", "snapshot", "parent_datasets"}.isdisjoint(
        published
    )
    assert marker not in repr(published)
    assert len(published["measure_parameters"]) <= 32


def test_snapshot_refresh_bounds_new_runs_and_statuses_before_qt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, accepted = _bound_database(tmp_path, "bounded-refresh.db")
    marker = "private-refresh-run-value-" * 100_000
    raw = {
        "guid": "guid-4",
        "name": marker,
        "parameters": marker,
        "run_description": marker,
        "snapshot": marker,
        "parent_datasets": marker,
        "measure_parameters": tuple(f"parameter-{index}" for index in range(10_000)),
    }
    monkeypatch.setattr(
        database_module,
        "find_new_runs",
        lambda *_args, **_kwargs: {4: dict(raw)},
    )
    monkeypatch.setattr(
        database_module,
        "get_run_status",
        lambda *_args, **_kwargs: dict(raw),
    )
    worker = database_module.DatabaseRefreshWorker(
        9,
        str(path),
        3,
        ["guid-4"],
        expected_database_instance=accepted,
    )
    finished: list[tuple[object, ...]] = []
    worker.signals.finished.connect(
        lambda *args: finished.append(args),
        type=QtCore.Qt.ConnectionType.DirectConnection,
    )

    worker.run()

    assert len(finished) == 1
    new_run = finished[0][2][4]
    status = finished[0][3]["guid-4"]
    for published in (new_run, status):
        assert {
            "parameters",
            "run_description",
            "snapshot",
            "parent_datasets",
        }.isdisjoint(published)
        assert marker not in repr(published)
        assert len(published["measure_parameters"]) <= 32


def test_refresh_error_drops_traceback_locals_before_qt_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, accepted = _bound_database(tmp_path, "bounded-refresh-error.db")
    marker = "private-traceback-run-description-" * 100_000

    def fail_with_raw_local(*_args: object, **_kwargs: object):
        raw_run = {"run_description": marker}
        if raw_run:
            raise RuntimeError("bounded refresh failure")

    monkeypatch.setattr(database_module, "find_new_runs", fail_with_raw_local)
    monkeypatch.setattr(database_module, "log_exception", lambda *_args: None)
    worker = database_module.DatabaseRefreshWorker(
        10,
        str(path),
        0,
        [],
        expected_database_instance=accepted,
    )
    finished: list[tuple[object, ...]] = []
    worker.signals.finished.connect(
        lambda *args: finished.append(args),
        type=QtCore.Qt.ConnectionType.DirectConnection,
    )

    worker.run()

    assert len(finished) == 1
    assert finished[0][2:4] == ({}, {})
    error = finished[0][4]
    assert isinstance(error, str)
    assert error == "bounded refresh failure"
    assert marker not in repr(error)


class _DerivedUnavailableError(TrustedLiveReaderUnavailableError):
    pass


def test_database_load_does_not_fallback_for_unlisted_unavailable_subclass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, accepted = _bound_database(tmp_path, "derived-unavailable.db")
    service = _FakeTrustedService()
    failure = _DerivedUnavailableError("helper failure is not an open outcome")
    service.queue("bootstrap", _QueuedOutcome(error=failure))
    fallback_calls: list[str] = []
    monkeypatch.setattr(
        database_module,
        "database_is_likely_cloud_placeholder",
        lambda _path: False,
    )
    monkeypatch.setattr(
        database_module,
        "database_access_error",
        lambda *_args, **_kwargs: fallback_calls.append("probe"),
    )
    monkeypatch.setattr(
        database_module,
        "get_runs_basic_via_sql",
        lambda *_args, **_kwargs: fallback_calls.append("snapshot"),
    )
    monkeypatch.setattr(database_module, "log_exception", lambda *_args: None)
    worker = database_module.DatabaseLoadWorker(
        9,
        str(path),
        expected_database_instance=accepted,
        trusted_service=service,
    )
    finished: list[tuple[object, ...]] = []
    worker.signals.finished.connect(lambda *args: finished.append(args))

    worker.run()

    assert fallback_calls == []
    assert [event[0] for event in service.events] == ["submit", "wait"]
    assert worker.access_mode is None
    assert worker.fallback_reason is None
    assert len(finished) == 1
    assert finished[0][:3] == (9, str(path), {})
    assert finished[0][3] == str(failure)


def test_accepted_trusted_session_failure_never_enters_snapshot_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, accepted = _bound_database(tmp_path, "accepted-session.db")
    service = _FakeTrustedService()
    service.queue(
        "bootstrap",
        _QueuedOutcome(
            TrustedBootstrapResult(1, 4, 1),
            on_wait=lambda: setattr(service, "accepted", True),
        ),
    )
    failure = TrustedLiveReaderUnavailableError("accepted helper failed")
    service.queue("page", _QueuedOutcome(error=failure))
    fallback_calls: list[str] = []
    monkeypatch.setattr(
        database_module,
        "database_is_likely_cloud_placeholder",
        lambda _path: False,
    )
    monkeypatch.setattr(
        database_module,
        "database_access_error",
        lambda *_args, **_kwargs: fallback_calls.append("probe"),
    )
    monkeypatch.setattr(
        database_module,
        "get_runs_basic_via_sql",
        lambda *_args, **_kwargs: fallback_calls.append("snapshot"),
    )
    monkeypatch.setattr(database_module, "log_exception", lambda *_args: None)
    worker = database_module.DatabaseLoadWorker(
        10,
        str(path),
        expected_database_instance=accepted,
        trusted_service=service,
    )
    finished: list[tuple[object, ...]] = []
    worker.signals.finished.connect(lambda *args: finished.append(args))

    worker.run()

    assert service.accepted
    assert fallback_calls == []
    assert [event[1] for event in service.events if event[0] == "submit"] == [
        "bootstrap",
        "page",
    ]
    assert not service.closed
    assert worker.access_mode is None
    assert worker.fallback_reason is None
    assert len(finished) == 1
    assert finished[0][:3] == (10, str(path), {})
    assert finished[0][3] == str(failure)


def test_trusted_refresh_publishes_new_runs_before_querying_watched_statuses(
    tmp_path: Path,
) -> None:
    path, accepted = _bound_database(tmp_path, "refresh-order.db")
    service = _FakeTrustedService()
    service.accepted = True
    service.queue(
        "refresh",
        _QueuedOutcome(TrustedRefreshResult(4, 5, 11, True, 1)),
    )
    service.queue(
        "page",
        _QueuedOutcome(TrustedRunPage((_record(5, "guid-5"),), 4, 5, 5, True)),
    )
    service.queue(
        "cheap",
        _QueuedOutcome(_record(3, "guid-3", is_completed=False)),
    )
    service.queue(
        "expensive",
        _QueuedOutcome(_record(3, "guid-3", result_count=12)),
    )
    worker = database_module.DatabaseRefreshWorker(
        11,
        str(path),
        4,
        [(3, "guid-3")],
        expected_database_instance=accepted,
        trusted_service=service,
        require_publication_ack=True,
    )
    new_runs_emitted = threading.Event()

    def record_new_runs(*args: object) -> None:
        service.record(("new_runs_ready", *args))
        new_runs_emitted.set()

    worker.signals.new_runs_ready.connect(
        record_new_runs,
        type=QtCore.Qt.ConnectionType.DirectConnection,
    )
    worker.signals.finished.connect(
        lambda *args: service.record(("finished", *args)),
        type=QtCore.Qt.ConnectionType.DirectConnection,
    )

    worker_thread = threading.Thread(target=worker.run)
    worker_thread.start()
    assert new_runs_emitted.wait(2)
    assert not any(
        event[:2] in {("submit", "cheap"), ("submit", "expensive")}
        for event in service.events
    )
    worker.acknowledge_new_runs_published()
    worker_thread.join(2)
    assert not worker_thread.is_alive()

    event_kinds = [
        event[1] if event[0] == "submit" else event[0] for event in service.events
    ]
    assert event_kinds == [
        "refresh",
        "wait",
        "page",
        "wait",
        "new_runs_ready",
        "cheap",
        "wait",
        "expensive",
        "wait",
        "finished",
    ]
    new_runs_event = next(
        event for event in service.events if event[0] == "new_runs_ready"
    )
    assert new_runs_event[1:] == (
        11,
        str(path),
        {5: {"guid": "guid-5"}},
    )
    finished_event = next(event for event in service.events if event[0] == "finished")
    assert finished_event[1:] == (
        11,
        str(path),
        {},
        {
            "guid-3": {
                "guid": "guid-3",
                "is_completed": False,
                "result_count": 12,
            }
        },
        None,
    )


def test_trusted_refresh_preserves_selected_visible_and_remaining_priorities(
    tmp_path: Path,
) -> None:
    path, accepted = _bound_database(tmp_path, "refresh-priorities.db")
    service = _FakeTrustedService()
    service.accepted = True
    service.queue(
        "refresh",
        _QueuedOutcome(TrustedRefreshResult(9, 9, 12, True, 1)),
    )
    service.queue(
        "page",
        _QueuedOutcome(TrustedRunPage((), 9, 9, 9, True)),
    )
    for run_id in (7, 8, 6):
        service.queue("cheap", _QueuedOutcome(_record(run_id, f"guid-{run_id}")))
        service.queue(
            "expensive",
            _QueuedOutcome(_record(run_id, f"guid-{run_id}")),
        )

    worker = database_module.DatabaseRefreshWorker(
        13,
        str(path),
        9,
        [
            (7, "guid-7", "selected"),
            (8, "guid-8", "visible"),
            (6, "guid-6", "remaining"),
        ],
        expected_database_instance=accepted,
        trusted_service=service,
    )

    worker.run()

    detail_submissions = [
        (event[1], event[2][0], event[3]["priority"])
        for event in service.events
        if event[:2] in {("submit", "cheap"), ("submit", "expensive")}
    ]
    assert detail_submissions == [
        ("cheap", 7, TrustedReadPriority.SELECTED_CHEAP),
        ("expensive", 7, TrustedReadPriority.SELECTED_EXPENSIVE),
        ("cheap", 8, TrustedReadPriority.VISIBLE_CHEAP),
        ("expensive", 8, TrustedReadPriority.VISIBLE_EXPENSIVE),
        ("cheap", 6, TrustedReadPriority.REMAINING_CHEAP),
        ("expensive", 6, TrustedReadPriority.REMAINING_EXPENSIVE),
    ]


def test_failed_basic_row_publication_retires_service_before_result_queries(
    tmp_path: Path,
) -> None:
    path, accepted = _bound_database(tmp_path, "refresh-publication-failure.db")
    service = _FakeTrustedService()
    service.accepted = True
    service.queue(
        "refresh",
        _QueuedOutcome(TrustedRefreshResult(4, 5, 11, True, 1)),
    )
    service.queue(
        "page",
        _QueuedOutcome(TrustedRunPage((_record(5, "guid-5"),), 4, 5, 5, True)),
    )
    worker = database_module.DatabaseRefreshWorker(
        12,
        str(path),
        4,
        [(3, "guid-3")],
        expected_database_instance=accepted,
        trusted_service=service,
        require_publication_ack=True,
    )
    finished: list[tuple[object, ...]] = []

    def reject_page(*_args: object) -> None:
        worker.reject_new_runs_publication(
            RuntimeError("injected run-list publication failure")
        )

    worker.signals.new_runs_ready.connect(
        reject_page,
        type=QtCore.Qt.ConnectionType.DirectConnection,
    )
    worker.signals.finished.connect(
        lambda *args: finished.append(args),
        type=QtCore.Qt.ConnectionType.DirectConnection,
    )

    worker.run()

    assert service.closed
    assert not any(
        event[:2] in {("submit", "cheap"), ("submit", "expensive")}
        for event in service.events
    )
    assert len(finished) == 1
    assert finished[0][:4] == (12, str(path), {}, {})
    error = finished[0][4]
    assert isinstance(error, str)
    assert "retirement was requested to prevent an omitted run" in str(error)


def test_qt_runnable_acknowledges_basic_rows_before_result_table_queries(
    tmp_path: Path,
) -> None:
    path, accepted = _bound_database(tmp_path, "qt-refresh-order.db")
    service = _FakeTrustedService()
    service.accepted = True
    service.queue(
        "refresh",
        _QueuedOutcome(TrustedRefreshResult(4, 5, 11, True, 1)),
    )
    service.queue(
        "page",
        _QueuedOutcome(TrustedRunPage((_record(5, "guid-5"),), 4, 5, 5, True)),
    )
    service.queue(
        "cheap",
        _QueuedOutcome(_record(3, "guid-3", is_completed=False)),
    )
    service.queue(
        "expensive",
        _QueuedOutcome(_record(3, "guid-3", result_count=12)),
    )
    worker = database_module.DatabaseRefreshWorker(
        12,
        str(path),
        4,
        [(3, "guid-3")],
        expected_database_instance=accepted,
        trusted_service=service,
        require_publication_ack=True,
    )
    published = threading.Event()
    finished = threading.Event()

    class Receiver(QtCore.QObject):
        @QtCore.pyqtSlot(int, str, object)
        def publish_page(self, generation: int, database_path: str, runs) -> None:
            assert QtCore.QThread.currentThread() is self.thread()
            assert not any(
                event[:2] in {("submit", "cheap"), ("submit", "expensive")}
                for event in service.events
            )
            service.record(("gui_page_applied", generation, database_path, dict(runs)))
            published.set()
            worker.acknowledge_new_runs_published()

        @QtCore.pyqtSlot(int, str, object, object, object)
        def refresh_finished(self, *_args: object) -> None:
            finished.set()

    receiver = Receiver()
    worker.signals.new_runs_ready.connect(receiver.publish_page)
    worker.signals.finished.connect(receiver.refresh_finished)
    pool = QtCore.QThreadPool()
    pool.setMaxThreadCount(1)
    ticks = []
    timer = QtCore.QTimer()
    timer.setInterval(0)
    timer.timeout.connect(lambda: ticks.append(True))
    timer.start()
    try:
        pool.start(worker)
        deadline = time.monotonic() + 3
        while not finished.is_set() and time.monotonic() < deadline:
            QtCore.QCoreApplication.processEvents()
            time.sleep(0.001)
        assert finished.is_set()
        assert published.is_set()
        assert ticks
        event_kinds = [
            event[1] if event[0] == "submit" else event[0] for event in service.events
        ]
        assert event_kinds.index("gui_page_applied") < event_kinds.index("cheap")
        assert event_kinds.index("gui_page_applied") < event_kinds.index("expensive")
    finally:
        timer.stop()
        worker.cancel()
        assert pool.waitForDone(3_000)


def test_worker_cancel_only_cancels_its_exact_shared_service_request(
    tmp_path: Path,
) -> None:
    path, accepted = _bound_database(tmp_path, "exact-cancel.db")
    service = _FakeTrustedService()
    service.accepted = True
    unchanged = TrustedRefreshResult(5, 5, 9, False, 1)
    service.queue(
        "refresh",
        _QueuedOutcome(unchanged, blocked=True),
        _QueuedOutcome(unchanged, blocked=True),
    )
    first_worker = database_module.DatabaseRefreshWorker(
        12,
        str(path),
        5,
        [],
        expected_database_instance=accepted,
        trusted_service=service,
    )
    second_worker = database_module.DatabaseRefreshWorker(
        13,
        str(path),
        5,
        [],
        expected_database_instance=accepted,
        trusted_service=service,
    )
    second_finished: list[tuple[object, ...]] = []
    second_worker.signals.finished.connect(
        lambda *args: second_finished.append(args),
        type=QtCore.Qt.ConnectionType.DirectConnection,
    )
    first_thread = threading.Thread(target=first_worker.run, name="first-db-worker")
    second_thread = threading.Thread(target=second_worker.run, name="second-db-worker")
    first_thread.start()
    second_thread.start()

    def both_waiting() -> bool:
        with first_worker._trusted_request_lock:
            first = first_worker._trusted_request
        with second_worker._trusted_request_lock:
            second = second_worker._trusted_request
        return bool(
            first is not None
            and second is not None
            and first is not second
            and first.started.is_set()
            and second.started.is_set()
        )

    _wait_for(both_waiting)
    with first_worker._trusted_request_lock:
        first_request = first_worker._trusted_request
    with second_worker._trusted_request_lock:
        second_request = second_worker._trusted_request
    assert isinstance(first_request, _FakeTrustedRequest)
    assert isinstance(second_request, _FakeTrustedRequest)

    first_worker.cancel()

    assert first_request.cancelled.wait(1.0)
    assert first_request.cancel_calls == 1
    assert second_request.cancel_calls == 0
    assert not second_request.cancelled.is_set()
    assert not service.closed
    first_thread.join(2.0)
    assert not first_thread.is_alive()

    second_request.release.set()
    second_thread.join(2.0)
    assert not second_thread.is_alive()
    assert second_request.cancel_calls == 0
    assert second_finished == [(13, str(path), {}, {}, None)]


def test_active_trusted_detail_drain_accepts_new_run_without_replay(
    tmp_path: Path,
) -> None:
    path, accepted = _bound_database(tmp_path, "incremental-details.db")
    service = _FakeTrustedService()
    service.accepted = True
    service.queue(
        "cheap",
        _QueuedOutcome(_record(1, "guid-1"), blocked=True),
        _QueuedOutcome(_record(2, "guid-2")),
        _QueuedOutcome(_record(3, "guid-3")),
    )
    worker = database_module.DatabaseDetailWorker(
        14,
        str(path),
        [1, 2],
        expected_database_instance=accepted,
        trusted_service=service,
    )
    batches: list[dict[int, dict[str, Any]]] = []
    finished: list[tuple[object, ...]] = []
    worker.signals.batch_ready.connect(
        lambda _generation, _path, details: batches.append(details),
        type=QtCore.Qt.ConnectionType.DirectConnection,
    )
    worker.signals.finished.connect(
        lambda *args: finished.append(args),
        type=QtCore.Qt.ConnectionType.DirectConnection,
    )
    thread = threading.Thread(target=worker.run, name="incremental-detail-worker")
    thread.start()

    _wait_for(lambda: bool(service.requests) and service.requests[0].started.is_set())
    assert worker.add_run_ids([3])
    service.requests[0].release.set()
    thread.join(3.0)

    assert not thread.is_alive()
    submitted_run_ids = [
        event[2][0] for event in service.events if event[:2] == ("submit", "cheap")
    ]
    assert submitted_run_ids == [1, 2, 3]
    assert [next(iter(batch)) for batch in batches] == [1, 2, 3]
    assert finished == [(14, str(path), None)]
    assert not worker.add_run_ids([4])


def test_dynamic_priority_a_to_b_demotes_a_to_stable_table_order(
    tmp_path: Path,
) -> None:
    path, accepted = _bound_database(tmp_path, "replace-detail-priority.db")
    service = _FakeTrustedService()
    service.accepted = True
    service.queue(
        "cheap",
        _QueuedOutcome(_record(4, "guid-4"), blocked=True),
        _QueuedOutcome(_record(3, "guid-3")),
        _QueuedOutcome(_record(1, "guid-1")),
        _QueuedOutcome(_record(2, "guid-2")),
    )
    worker = database_module.DatabaseDetailWorker(
        18,
        str(path),
        [1, 2, 3, 4],
        expected_database_instance=accepted,
        trusted_service=service,
    )
    worker.prioritize_run_ids([4])
    thread = threading.Thread(target=worker.run, name="replace-priority-worker")
    thread.start()

    _wait_for(lambda: bool(service.requests) and service.requests[0].started.is_set())
    worker.prioritize_run_ids([2])
    worker.prioritize_run_ids([3])
    service.requests[0].release.set()
    thread.join(3.0)

    assert not thread.is_alive()
    submitted_run_ids = [
        event[2][0] for event in service.events if event[:2] == ("submit", "cheap")
    ]
    assert submitted_run_ids == [4, 3, 1, 2]
    assert service.requests[0].reprioritizations == [
        int(TrustedReadPriority.REMAINING_CHEAP)
    ]


def test_viewport_leave_clears_promotions_and_restores_stable_drain(
    tmp_path: Path,
) -> None:
    path, accepted = _bound_database(tmp_path, "viewport-detail-priority.db")
    service = _FakeTrustedService()
    service.accepted = True
    service.queue(
        "cheap",
        _QueuedOutcome(_record(4, "guid-4"), blocked=True),
        _QueuedOutcome(_record(1, "guid-1")),
        _QueuedOutcome(_record(2, "guid-2")),
        _QueuedOutcome(_record(3, "guid-3")),
    )
    worker = database_module.DatabaseDetailWorker(
        19,
        str(path),
        [1, 2, 3, 4],
        expected_database_instance=accepted,
        trusted_service=service,
    )
    worker.prioritize_run_ids([4])
    thread = threading.Thread(target=worker.run, name="viewport-priority-worker")
    thread.start()

    _wait_for(lambda: bool(service.requests) and service.requests[0].started.is_set())
    worker.prioritize_run_ids([2, 3])
    worker.prioritize_run_ids([])
    service.requests[0].release.set()
    thread.join(3.0)

    assert not thread.is_alive()
    submitted_run_ids = [
        event[2][0] for event in service.events if event[:2] == ("submit", "cheap")
    ]
    assert submitted_run_ids == [4, 1, 2, 3]
    assert service.requests[0].reprioritizations == [
        int(TrustedReadPriority.REMAINING_CHEAP)
    ]


def test_active_request_tracks_visible_selected_and_remaining_transitions(
    tmp_path: Path,
) -> None:
    path, accepted = _bound_database(tmp_path, "active-priority-transitions.db")
    service = _FakeTrustedService()
    service.accepted = True
    service.queue(
        "cheap",
        _QueuedOutcome(_record(1, "guid-1"), blocked=True),
        _QueuedOutcome(_record(2, "guid-2")),
    )
    worker = database_module.DatabaseDetailWorker(
        20,
        str(path),
        [1, 2],
        expected_database_instance=accepted,
        trusted_service=service,
    )
    thread = threading.Thread(target=worker.run, name="priority-transitions-worker")
    thread.start()

    _wait_for(lambda: bool(service.requests) and service.requests[0].started.is_set())
    worker.prioritize_run_ids([2, 1])
    worker.prioritize_run_ids([1, 2])
    worker.prioritize_run_ids([])
    request = service.requests[0]
    assert request.reprioritizations == [
        int(TrustedReadPriority.VISIBLE_CHEAP),
        int(TrustedReadPriority.SELECTED_CHEAP),
        int(TrustedReadPriority.REMAINING_CHEAP),
    ]
    request.release.set()
    thread.join(3.0)

    assert not thread.is_alive()
    submitted_run_ids = [
        event[2][0] for event in service.events if event[:2] == ("submit", "cheap")
    ]
    assert submitted_run_ids == [1, 2]


def test_promotion_during_submit_is_applied_after_request_install(
    tmp_path: Path,
) -> None:
    path, accepted = _bound_database(tmp_path, "install-detail-priority.db")

    class SubmitBarrierService(_FakeTrustedService):
        def __init__(self) -> None:
            super().__init__()
            self.submit_started = threading.Event()
            self.release_submit = threading.Event()

        def submit_cheap_run(
            self,
            run_id: int,
            *,
            priority: int | TrustedReadPriority,
            deadline: float | None = None,
        ) -> _FakeTrustedRequest:
            request = super().submit_cheap_run(
                run_id,
                priority=priority,
                deadline=deadline,
            )
            self.submit_started.set()
            if not self.release_submit.wait(3.0):
                raise AssertionError("Test did not release trusted submit")
            return request

    service = SubmitBarrierService()
    service.accepted = True
    service.queue(
        "cheap",
        _QueuedOutcome(_record(1, "guid-1"), blocked=True),
    )
    worker = database_module.DatabaseDetailWorker(
        20,
        str(path),
        [1],
        expected_database_instance=accepted,
        trusted_service=service,
    )
    worker_thread = threading.Thread(target=worker.run, name="install-priority-worker")
    worker_thread.start()
    assert service.submit_started.wait(1.0)

    update_started = threading.Event()
    update_finished = threading.Event()

    def promote_active_run() -> None:
        update_started.set()
        worker.prioritize_run_ids([1])
        update_finished.set()

    update_thread = threading.Thread(target=promote_active_run, name="priority-update")
    update_thread.start()
    assert update_started.wait(1.0)
    assert not update_finished.wait(0.05)

    service.release_submit.set()
    assert update_finished.wait(1.0)
    update_thread.join(1.0)
    assert not update_thread.is_alive()
    request = service.requests[0]
    assert request.started.wait(1.0)
    assert request.reprioritizations == [int(TrustedReadPriority.SELECTED_CHEAP)]
    assert request.priority == int(TrustedReadPriority.SELECTED_CHEAP)
    request.release.set()
    worker_thread.join(3.0)

    assert not worker_thread.is_alive()
    submission = next(
        event for event in service.events if event[:2] == ("submit", "cheap")
    )
    assert submission[3]["priority"] == TrustedReadPriority.REMAINING_CHEAP


def test_selected_expensive_submission_outranks_remaining_cheap_drain(
    tmp_path: Path,
) -> None:
    path, accepted = _bound_database(tmp_path, "cross-worker-priority.db")
    service = _FakeTrustedService()
    service.accepted = True
    service.queue(
        "cheap",
        _QueuedOutcome(_record(1, "guid-1"), blocked=True),
    )
    service.queue(
        "expensive",
        _QueuedOutcome(_record(2, "guid-2")),
    )
    cheap_worker = database_module.DatabaseDetailWorker(
        21,
        str(path),
        [1],
        expected_database_instance=accepted,
        trusted_service=service,
    )
    expensive_worker = database_module.DatabaseExpensiveDetailWorker(
        22,
        str(path),
        [2],
        expected_database_instance=accepted,
        trusted_service=service,
    )
    expensive_worker.prioritize_run_ids([2])

    cheap_thread = threading.Thread(target=cheap_worker.run, name="remaining-cheap")
    cheap_thread.start()
    _wait_for(lambda: bool(service.requests) and service.requests[0].started.is_set())
    expensive_worker.run()
    service.requests[0].release.set()
    cheap_thread.join(3.0)

    assert not cheap_thread.is_alive()
    submissions = [
        (event[1], event[2][0], event[3]["priority"])
        for event in service.events
        if event[:2] in {("submit", "cheap"), ("submit", "expensive")}
    ]
    assert submissions == [
        ("cheap", 1, TrustedReadPriority.REMAINING_CHEAP),
        ("expensive", 2, TrustedReadPriority.SELECTED_EXPENSIVE),
    ]
    assert TrustedReadPriority.SELECTED_EXPENSIVE < TrustedReadPriority.REMAINING_CHEAP


def test_trusted_detail_drain_continues_after_reusable_per_run_error(
    tmp_path: Path,
) -> None:
    path, accepted = _bound_database(tmp_path, "reusable-detail-error.db")
    service = _FakeTrustedService()
    service.accepted = True
    failure = TrustedLiveQueryError("injected reusable run-1 query failure")
    service.queue(
        "cheap",
        _QueuedOutcome(error=failure),
        _QueuedOutcome(_record(2, "guid-2")),
        _QueuedOutcome(_record(3, "guid-3")),
    )
    worker = database_module.DatabaseDetailWorker(
        15,
        str(path),
        [1, 2, 3],
        expected_database_instance=accepted,
        trusted_service=service,
    )
    batches: list[dict[int, dict[str, Any]]] = []
    finished: list[tuple[object, ...]] = []
    worker.signals.batch_ready.connect(
        lambda _generation, _path, details: batches.append(details),
        type=QtCore.Qt.ConnectionType.DirectConnection,
    )
    worker.signals.finished.connect(
        lambda *args: finished.append(args),
        type=QtCore.Qt.ConnectionType.DirectConnection,
    )

    worker.run()

    submitted_run_ids = [
        event[2][0] for event in service.events if event[:2] == ("submit", "cheap")
    ]
    assert submitted_run_ids == [1, 2, 3]
    assert [next(iter(batch)) for batch in batches] == [2, 3]
    assert finished == [(15, str(path), str(failure))]


def test_trusted_detail_drain_stops_when_per_run_error_retires_service(
    tmp_path: Path,
) -> None:
    path, accepted = _bound_database(tmp_path, "terminal-detail-error.db")
    service = _FakeTrustedService()
    service.accepted = True
    failure = RuntimeError("injected terminal run-1 query failure")
    service.queue(
        "cheap",
        _QueuedOutcome(
            error=failure,
            on_wait=lambda: setattr(service, "closing", True),
        ),
        _QueuedOutcome(_record(2, "guid-2")),
    )
    worker = database_module.DatabaseDetailWorker(
        16,
        str(path),
        [1, 2],
        expected_database_instance=accepted,
        trusted_service=service,
    )
    batches: list[dict[int, dict[str, Any]]] = []
    finished: list[tuple[object, ...]] = []
    worker.signals.batch_ready.connect(
        lambda _generation, _path, details: batches.append(details),
        type=QtCore.Qt.ConnectionType.DirectConnection,
    )
    worker.signals.finished.connect(
        lambda *args: finished.append(args),
        type=QtCore.Qt.ConnectionType.DirectConnection,
    )

    worker.run()

    submitted_run_ids = [
        event[2][0] for event in service.events if event[:2] == ("submit", "cheap")
    ]
    assert submitted_run_ids == [1]
    assert batches == []
    assert finished == [(16, str(path), str(failure))]


def test_trusted_detail_drain_stops_on_database_instance_error(
    tmp_path: Path,
) -> None:
    path, accepted = _bound_database(tmp_path, "replaced-detail-source.db")
    service = _FakeTrustedService()
    service.accepted = True
    failure = database_module.DatabaseInstanceChangedError(
        "injected detail source replacement"
    )
    service.queue(
        "cheap",
        _QueuedOutcome(error=failure),
        _QueuedOutcome(_record(2, "guid-2")),
    )
    worker = database_module.DatabaseDetailWorker(
        17,
        str(path),
        [1, 2],
        expected_database_instance=accepted,
        trusted_service=service,
    )
    finished: list[tuple[object, ...]] = []
    worker.signals.finished.connect(
        lambda *args: finished.append(args),
        type=QtCore.Qt.ConnectionType.DirectConnection,
    )

    worker.run()

    submitted_run_ids = [
        event[2][0] for event in service.events if event[:2] == ("submit", "cheap")
    ]
    assert submitted_run_ids == [1]
    assert len(finished) == 1
    bounded_error = finished[0][2]
    assert isinstance(bounded_error, database_module.DatabaseInstanceChangedError)
    assert str(bounded_error) == str(failure)
    assert bounded_error is not failure
    assert bounded_error.__traceback__ is None
    assert bounded_error.__context__ is None


def test_selected_worker_publishes_cheap_detail_before_expensive_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, accepted = _bound_database(tmp_path, "progressive-selected.db")
    service = _FakeTrustedService()
    service.accepted = True
    initial = _selected_detail(7, "guid-7", name="initial")
    final = _selected_detail(7, "guid-7", name="final", result_count=12)
    service.queue("cheap", _QueuedOutcome(_record(7, "guid-7", name="initial")))
    service.queue("selected", _QueuedOutcome(initial), _QueuedOutcome(final))
    service.queue(
        "expensive",
        _QueuedOutcome(_record(7, "guid-7", result_count=12), blocked=True),
    )
    monkeypatch.setattr(
        database_module,
        "TrustedLiveReadService",
        _FakeTrustedService,
    )
    worker = database_module.DatabaseSelectedRunWorker(
        18,
        str(path),
        7,
        "guid-7",
        service,
        expected_database_instance=accepted,
    )
    progress: list[tuple[object, ...]] = []
    finished: list[tuple[object, ...]] = []
    worker.signals.progress.connect(
        lambda *args: progress.append(args),
        type=QtCore.Qt.ConnectionType.DirectConnection,
    )
    worker.signals.finished.connect(
        lambda *args: finished.append(args),
        type=QtCore.Qt.ConnectionType.DirectConnection,
    )
    thread = threading.Thread(target=worker.run, name="selected-detail-worker")
    thread.start()

    _wait_for(
        lambda: len(service.requests) == 3 and service.requests[2].started.is_set()
    )
    assert progress == [(18, str(path), "guid-7", initial)]
    assert finished == []
    assert [event[1] for event in service.events if event[0] == "submit"] == [
        "cheap",
        "selected",
        "expensive",
    ]

    service.requests[2].release.set()
    thread.join(3.0)

    assert not thread.is_alive()
    assert [event[1] for event in service.events if event[0] == "submit"] == [
        "cheap",
        "selected",
        "expensive",
        "selected",
    ]
    assert finished == [(18, str(path), "guid-7", final, None)]


def test_selected_worker_bounds_failure_before_signal_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, accepted = _bound_database(tmp_path, "bounded-selected-error.db")
    service = _FakeTrustedService()
    service.accepted = True
    marker = "private-selected-error-" * 100_000
    service.queue("cheap", _QueuedOutcome(error=RuntimeError(marker)))
    monkeypatch.setattr(database_module, "log_exception", lambda *_args: None)
    monkeypatch.setattr(
        database_module,
        "TrustedLiveReadService",
        _FakeTrustedService,
    )
    worker = database_module.DatabaseSelectedRunWorker(
        19,
        str(path),
        7,
        "guid-7",
        service,
        expected_database_instance=accepted,
    )
    finished: list[tuple[object, ...]] = []
    receiver_threads: list[int] = []

    def receive(*args: object) -> None:
        receiver_threads.append(threading.get_ident())
        finished.append(args)

    worker.signals.finished.connect(
        receive,
        type=QtCore.Qt.ConnectionType.DirectConnection,
    )
    caller_thread = threading.get_ident()
    thread = threading.Thread(target=worker.run, name="bounded-selected-error")
    thread.start()
    thread.join(3)

    assert not thread.is_alive()
    assert receiver_threads and receiver_threads[0] != caller_thread
    assert len(finished) == 1
    error = finished[0][4]
    assert isinstance(error, str)
    assert len(error.encode("utf-8")) <= TRUSTED_PRESENTATION_MAX_ERROR_BYTES
    assert marker not in error
