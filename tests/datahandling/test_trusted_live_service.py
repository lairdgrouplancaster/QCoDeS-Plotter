from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from qplot.datahandling import trusted_live_service as service_module
from qplot.datahandling.file_identity import DatabaseInstance
from qplot.datahandling.trusted_live import (
    TrustedLiveBusyTimeoutError,
    TrustedLiveCancelledError,
    TrustedLiveCleanupError,
    TrustedLiveDeadlineExceededError,
    TrustedLiveInvalidDatabaseError,
    TrustedLiveQueryError,
    TrustedLiveReaderClosedError,
    TrustedLiveReaderError,
    TrustedLiveReaderThreadError,
    TrustedLiveReaderUnavailableError,
    TrustedLiveResultLimitError,
    TrustedLiveSourceChangedError,
    TrustedLiveSourceIOError,
    TrustedLiveSqlRejectedError,
    TrustedLiveTransactionError,
    TrustedLiveUnsupportedSourceError,
    TrustedQuery,
    TrustedQueryResult,
)
from qplot.datahandling.trusted_live_queries import (
    TrustedBootstrapResult,
    TrustedRefreshResult,
    TrustedRunPage,
    TrustedRunRecord,
    TrustedSelectedRunDetail,
)
from qplot.datahandling.trusted_live_service import (
    TrustedLiveReadService,
    TrustedReadPriority,
    TrustedReadQueueFullError,
    TrustedReadRequestCancelledError,
    TrustedReadRequestDeadlineError,
    TrustedReadServiceClosedError,
    TrustedReadSessionFailedError,
)
from qplot.datahandling.trusted_live_supervisor import (
    TrustedLiveHelperExitedError,
    TrustedLiveHelperForcedTerminationError,
    TrustedLiveHelperReplyTimeoutError,
    TrustedLiveHelperStartupError,
    TrustedLiveProtocolError,
    TrustedLiveSupervisorClosedError,
    TrustedLiveSupervisorLiveness,
)
from qplot.datahandling.trusted_presentation import build_selected_run_presentation
from qplot.datahandling.trusted_snapshot import normalize_trusted_snapshot


def _wait_for(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("Timed out waiting for deterministic broker state")


@dataclass(slots=True)
class _FakeJob:
    label: str
    result: object
    blocked: bool
    failure: BaseException | None
    started: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    cancelled: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        if not self.blocked:
            self.release.set()


class _FakeSupervisor:
    """Thread-safe spawned-supervisor stand-in with exact fake jobs."""

    def __init__(self, accepted_instance: DatabaseInstance) -> None:
        self.database_instance = accepted_instance
        self.incarnation = 1
        self.helper_pid = 424_242
        self._helper_alive = True
        self._receiver_alive = True
        self._open_endpoints = 3
        self._unreaped_incarnation = False
        self._retain_unreaped_on_close = False
        self._closed = False
        self._lock = threading.Lock()
        self._blocked_labels: set[str] = set()
        self._failures: dict[str, BaseException] = {}
        self._cancel_error: BaseException | None = None
        self.jobs: list[_FakeJob] = []
        self.submit_threads: list[tuple[str, int, str]] = []
        self.wait_threads: list[tuple[str, int, str]] = []
        self.cancel_threads: list[tuple[str, int, str]] = []
        self.close_threads: list[tuple[int, str]] = []
        self.cancel_entered = threading.Event()
        self.cancel_release = threading.Event()
        self.cancel_release.set()
        self.close_entered = threading.Event()
        self.close_release = threading.Event()
        self.close_release.set()

    @property
    def helper_alive(self) -> bool:
        with self._lock:
            return self._helper_alive

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def resource_liveness(self) -> TrustedLiveSupervisorLiveness:
        with self._lock:
            active_incarnation = self._helper_alive
            unreaped_incarnation = self._unreaped_incarnation
            return TrustedLiveSupervisorLiveness(
                helper_pid=(
                    self.helper_pid
                    if active_incarnation or unreaped_incarnation
                    else None
                ),
                process_alive=self._helper_alive,
                receiver_alive=self._receiver_alive,
                open_endpoints=self._open_endpoints,
                active_incarnation=active_incarnation,
                unreaped_incarnation=unreaped_incarnation,
                active_job=False,
                closing=False,
                closed=self._closed,
            )

    def reap_closed_resources(self) -> TrustedLiveSupervisorLiveness:
        return self.resource_liveness()

    def retain_unreaped_on_close(self) -> None:
        with self._lock:
            self._retain_unreaped_on_close = True

    def release_unreaped_resources(self) -> None:
        with self._lock:
            self._retain_unreaped_on_close = False
            self._receiver_alive = False
            self._open_endpoints = 0
            self._unreaped_incarnation = False

    def block(self, label: str) -> None:
        with self._lock:
            self._blocked_labels.add(label)

    def fail_fatally(self, label: str) -> None:
        self.fail_with(
            label, TrustedLiveProtocolError(f"Fake fatal failure for {label}")
        )

    def fail_with(self, label: str, error: BaseException) -> None:
        with self._lock:
            self._failures[label] = error

    def fail_cancel_with(self, error: BaseException) -> None:
        with self._lock:
            self._cancel_error = error

    def pause_cancel(self) -> None:
        self.cancel_release.clear()

    def pause_close(self) -> None:
        self.close_release.clear()

    def labels_submitted(self) -> list[str]:
        with self._lock:
            return [job.label for job in self.jobs]

    def cancel_count(self) -> int:
        with self._lock:
            return len(self.cancel_threads)

    def job(self, label: str) -> _FakeJob | None:
        with self._lock:
            return next((job for job in self.jobs if job.label == label), None)

    def wait_until_started(self, label: str) -> _FakeJob:
        selected: list[_FakeJob] = []

        def started() -> bool:
            job = self.job(label)
            if job is None or not job.started.is_set():
                return False
            selected.append(job)
            return True

        _wait_for(started)
        return selected[0]

    def release_job(self, label: str) -> None:
        job = self.job(label)
        if job is None:
            raise AssertionError(f"No fake supervisor job {label!r} was submitted")
        job.release.set()

    def release_everything(self) -> None:
        with self._lock:
            jobs = tuple(self.jobs)
        for job in jobs:
            job.release.set()
        self.cancel_release.set()
        self.close_release.set()
        self.release_unreaped_resources()

    def _new_job(self, label: str, result: object) -> _FakeJob:
        current = threading.current_thread()
        with self._lock:
            job = _FakeJob(
                label,
                result,
                label in self._blocked_labels,
                self._failures.get(label),
            )
            self.jobs.append(job)
            self.submit_threads.append((label, threading.get_ident(), current.name))
        return job

    def submit_query(
        self,
        sql: str,
        bindings: object = None,
        *,
        timeout: float | None = None,
    ) -> _FakeJob:
        del bindings, timeout
        return self._new_job(sql, TrustedQueryResult(("value",), ((sql,),)))

    def submit_query_batch(
        self,
        queries: tuple[TrustedQuery, ...],
        *,
        timeout: float | None = None,
    ) -> _FakeJob:
        del timeout
        label = "batch:" + "|".join(query.sql for query in queries)
        results = tuple(
            TrustedQueryResult(("value",), ((query.sql,),)) for query in queries
        )
        return self._new_job(label, results)

    def submit_data_version(self, *, timeout: float | None = None) -> _FakeJob:
        del timeout
        return self._new_job("data-version", 1)

    def wait(self, job: _FakeJob, *, timeout: float | None = None) -> Any:
        current = threading.current_thread()
        with self._lock:
            self.wait_threads.append((job.label, threading.get_ident(), current.name))
        job.started.set()
        wait_timeout = 5.0 if timeout is None else min(5.0, max(0.01, timeout))
        if not job.release.wait(wait_timeout):
            raise AssertionError(f"Fake job {job.label!r} was not released")
        if job.cancelled.is_set():
            raise TrustedLiveCancelledError(f"Fake job {job.label!r} was cancelled")
        if job.failure is not None:
            raise job.failure
        return job.result

    def cancel(self, job: _FakeJob) -> bool:
        current = threading.current_thread()
        with self._lock:
            self.cancel_threads.append((job.label, threading.get_ident(), current.name))
            cancel_error = self._cancel_error
        self.cancel_entered.set()
        job.cancelled.set()
        if cancel_error is None:
            job.release.set()
        if not self.cancel_release.wait(5.0):
            raise AssertionError("Fake supervisor cancellation was not released")
        if cancel_error is not None:
            raise cancel_error
        return True

    def close(self) -> None:
        current = threading.current_thread()
        with self._lock:
            self.close_threads.append((threading.get_ident(), current.name))
        self.close_entered.set()
        if not self.close_release.wait(5.0):
            raise AssertionError("Fake supervisor close was not released")
        with self._lock:
            self._helper_alive = False
            self._closed = True
            if self._retain_unreaped_on_close:
                self._receiver_alive = True
                self._open_endpoints = 1
                self._unreaped_incarnation = True
            else:
                self._receiver_alive = False
                self._open_endpoints = 0
                self._unreaped_incarnation = False


class _FakeMetadataAdapter:
    """Label one broker operation while retaining the current page-level API."""

    def __init__(self, executor: object, database_path: str) -> None:
        del database_path
        self._executor = executor

    def bind_executor(self, executor: object) -> None:
        self._executor = executor

    def _touch(self, label: str) -> None:
        executor = self._executor
        executor.query(label)  # type: ignore[attr-defined]

    def bootstrap(self) -> TrustedBootstrapResult:
        self._touch("bootstrap")
        return TrustedBootstrapResult(10, 1, 1)

    def refresh_new_runs(
        self,
        accepted_run_id: int | None = None,
    ) -> TrustedRefreshResult:
        self._touch("refresh")
        return TrustedRefreshResult(accepted_run_id or 0, 10, 1, True, 1)

    def basic_run_page(self, after_run_id: int, through_run_id: int) -> TrustedRunPage:
        self._touch(f"page:{after_run_id}:{through_run_id}")
        return TrustedRunPage(
            (),
            after_run_id,
            through_run_id,
            through_run_id,
            True,
        )

    def cheap_run(self, run_id: int) -> TrustedRunRecord:
        self._touch(f"cheap:{run_id}")
        return TrustedRunRecord(run_id, (("kind", "cheap"),))

    def expensive_run(self, run_id: int) -> TrustedRunRecord:
        self._touch(f"expensive:{run_id}")
        return TrustedRunRecord(run_id, (("kind", "expensive"),))

    def selected_run_detail(self, run_id: int) -> TrustedSelectedRunDetail:
        self._touch(f"selected:{run_id}")
        return TrustedSelectedRunDetail(
            run=TrustedRunRecord(run_id, ()),
            parameters=(),
            metadata=(),
            snapshot=normalize_trusted_snapshot(None),
            setpoint_summaries=(),
            presentation=build_selected_run_presentation(
                run_fields={"run_id": run_id},
                metadata_fields={},
                parameters=(),
                snapshot_summary={"Status": "empty"},
                setpoint_summaries=(),
                unavailable_fields=(),
            ),
        )


@dataclass(slots=True)
class _ServiceHarness:
    service: TrustedLiveReadService
    supervisor: _FakeSupervisor
    factory_calls: list[tuple[str, DatabaseInstance | None, int, str]]
    factory_options: list[dict[str, object]]


@pytest.fixture(autouse=True)
def _replace_metadata_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        service_module,
        "TrustedMetadataQueryAdapter",
        _FakeMetadataAdapter,
    )


@pytest.fixture
def service_factory(tmp_path: Path):
    harnesses: list[_ServiceHarness] = []
    next_identity = 1

    def create(
        *,
        queue_capacity: int = 512,
        request_timeout_seconds: float = 30.0,
        supervisor_options: dict[str, object] | None = None,
        factory_hook: Callable[[dict[str, object]], None] | None = None,
    ) -> _ServiceHarness:
        nonlocal next_identity
        path = tmp_path / f"service-{next_identity}.db"
        accepted = DatabaseInstance(
            logical_path=str(path),
            resolved_path=str(path),
            identity=(1, next_identity),
        )
        next_identity += 1
        supervisor = _FakeSupervisor(accepted)
        factory_calls: list[tuple[str, DatabaseInstance | None, int, str]] = []
        factory_options: list[dict[str, object]] = []

        def factory(
            database_path: str,
            *,
            expected_database_instance: DatabaseInstance | None = None,
            **_options: object,
        ) -> _FakeSupervisor:
            current = threading.current_thread()
            factory_calls.append(
                (
                    database_path,
                    expected_database_instance,
                    threading.get_ident(),
                    current.name,
                )
            )
            options = dict(_options)
            factory_options.append(options)
            if factory_hook is not None:
                factory_hook(options)
            return supervisor

        service = TrustedLiveReadService(
            path,
            expected_database_instance=accepted,
            queue_capacity=queue_capacity,
            request_timeout_seconds=request_timeout_seconds,
            supervisor_factory=factory,  # type: ignore[arg-type]
            supervisor_options=supervisor_options,
        )
        harness = _ServiceHarness(
            service,
            supervisor,
            factory_calls,
            factory_options,
        )
        harnesses.append(harness)
        return harness

    yield create

    for harness in harnesses:
        harness.supervisor.release_everything()
        harness.service.close_async()
        assert harness.service.wait_closed(5.0)


def test_bounded_queue_applies_backpressure(
    service_factory: Callable[..., _ServiceHarness],
) -> None:
    harness = service_factory(queue_capacity=2)
    harness.supervisor.block("page:0:1")

    active = harness.service.submit_basic_page(0, 1)
    harness.supervisor.wait_until_started("page:0:1")
    queued = harness.service.submit_basic_page(1, 2)

    assert harness.service.liveness().outstanding_requests == 2
    with pytest.raises(TrustedReadQueueFullError):
        harness.service.submit_basic_page(2, 3)

    harness.supervisor.release_job("page:0:1")
    assert active.wait(2.0).through_run_id == 1
    assert queued.wait(2.0).through_run_id == 2
    assert harness.supervisor.labels_submitted() == ["page:0:1", "page:1:2"]


def test_selected_snapshot_normalization_runs_on_dispatcher_not_caller(
    service_factory: Callable[..., _ServiceHarness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller_thread_id = threading.get_ident()
    normalization_threads: list[tuple[int, str]] = []

    class SnapshotNormalizingAdapter(_FakeMetadataAdapter):
        def selected_run_detail(self, run_id: int) -> TrustedSelectedRunDetail:
            self._touch(f"selected:{run_id}")
            current = threading.current_thread()
            normalization_threads.append((threading.get_ident(), current.name))
            snapshot = normalize_trusted_snapshot(
                '{"station":{"parameters":{"gate":{"value":1.5}}}}'
            )
            return TrustedSelectedRunDetail(
                run=TrustedRunRecord(run_id, (("guid", f"guid-{run_id}"),)),
                parameters=(),
                metadata=(),
                snapshot=snapshot,
                setpoint_summaries=(),
                presentation=build_selected_run_presentation(
                    run_fields={"run_id": run_id, "guid": f"guid-{run_id}"},
                    metadata_fields={},
                    parameters=(),
                    snapshot_summary={"Status": snapshot.status},
                    setpoint_summaries=(),
                    unavailable_fields=(),
                ),
            )

    monkeypatch.setattr(
        service_module,
        "TrustedMetadataQueryAdapter",
        SnapshotNormalizingAdapter,
    )
    harness = service_factory()

    detail = harness.service.submit_selected_run(7).wait(2.0)

    assert detail.snapshot.status == "available"
    assert normalization_threads
    thread_id, thread_name = normalization_threads[0]
    assert thread_id != caller_thread_id
    assert "trusted-read-dispatcher" in thread_name


def test_duplicate_requests_coalesce_to_one_supervisor_job(
    service_factory: Callable[..., _ServiceHarness],
) -> None:
    harness = service_factory()
    harness.supervisor.block("page:0:5")
    shared_deadline = time.monotonic() + 5.0

    first = harness.service.submit_basic_page(0, 5, deadline=shared_deadline)
    harness.supervisor.wait_until_started("page:0:5")
    second = harness.service.submit_basic_page(0, 5, deadline=shared_deadline)

    assert first.request_id != second.request_id
    harness.supervisor.release_job("page:0:5")
    assert first.wait(2.0) == second.wait(2.0)
    assert harness.supervisor.labels_submitted().count("page:0:5") == 1


def test_cancelling_one_coalesced_subscriber_does_not_cancel_shared_job(
    service_factory: Callable[..., _ServiceHarness],
) -> None:
    harness = service_factory()
    harness.supervisor.block("page:0:5")
    shared_deadline = time.monotonic() + 5.0

    cancelled = harness.service.submit_basic_page(0, 5, deadline=shared_deadline)
    harness.supervisor.wait_until_started("page:0:5")
    survivor = harness.service.submit_basic_page(0, 5, deadline=shared_deadline)

    assert cancelled.cancel()
    with pytest.raises(TrustedReadRequestCancelledError):
        cancelled.wait(0.0)
    assert harness.supervisor.cancel_count() == 0

    harness.supervisor.release_job("page:0:5")
    assert survivor.wait(2.0).complete
    assert harness.supervisor.cancel_count() == 0


def test_staggered_default_duplicates_share_one_bounded_operation(
    service_factory: Callable[..., _ServiceHarness],
) -> None:
    harness = service_factory(request_timeout_seconds=0.45)
    harness.supervisor.block("page:0:5")

    cancelled = harness.service.submit_basic_page(0, 5)
    harness.supervisor.wait_until_started("page:0:5")
    time.sleep(0.12)
    survivor = harness.service.submit_basic_page(0, 5)

    assert survivor.deadline == cancelled.deadline
    assert cancelled.cancel()
    with pytest.raises(TrustedReadRequestCancelledError):
        cancelled.wait(0.0)
    assert harness.supervisor.cancel_count() == 0

    harness.supervisor.release_job("page:0:5")
    assert survivor.wait(1.0).complete
    assert harness.supervisor.labels_submitted().count("page:0:5") == 1
    assert not harness.service.closed


def test_different_explicit_deadlines_remain_exact_after_earlier_cancellation(
    service_factory: Callable[..., _ServiceHarness],
) -> None:
    harness = service_factory(request_timeout_seconds=0.45)
    harness.supervisor.block("page:0:5")
    first_deadline = time.monotonic() + 0.45

    cancelled = harness.service.submit_basic_page(
        0,
        5,
        deadline=first_deadline,
    )
    first_job = harness.supervisor.wait_until_started("page:0:5")
    time.sleep(0.12)
    survivor_deadline = time.monotonic() + 0.45
    survivor = harness.service.submit_basic_page(
        0,
        5,
        deadline=survivor_deadline,
    )

    assert cancelled.deadline == first_deadline
    assert survivor.deadline == survivor_deadline
    assert survivor.deadline > cancelled.deadline
    assert cancelled.cancel()
    with pytest.raises(TrustedReadRequestCancelledError):
        cancelled.wait(0.0)

    def second_job_started() -> bool:
        matching = [job for job in harness.supervisor.jobs if job.label == "page:0:5"]
        return len(matching) == 2 and matching[1].started.is_set()

    _wait_for(second_job_started)
    second_job = [job for job in harness.supervisor.jobs if job.label == "page:0:5"][1]
    assert second_job is not first_job
    time.sleep(max(0.0, cancelled.deadline - time.monotonic() + 0.03))
    assert time.monotonic() < survivor.deadline
    second_job.release.set()

    assert survivor.wait(1.0).complete
    assert harness.supervisor.labels_submitted().count("page:0:5") == 2
    assert harness.service.submit_basic_page(1, 2).wait(1.0).complete
    assert not harness.service.closed


def test_promotion_reorders_queued_operations(
    service_factory: Callable[..., _ServiceHarness],
) -> None:
    harness = service_factory()
    harness.supervisor.block("page:0:1")

    blocker = harness.service.submit_basic_page(0, 1)
    harness.supervisor.wait_until_started("page:0:1")
    ordinary = harness.service.submit_basic_page(
        1,
        2,
        priority=TrustedReadPriority.REMAINING_CHEAP,
    )
    promoted = harness.service.submit_basic_page(
        2,
        3,
        priority=TrustedReadPriority.REMAINING_EXPENSIVE,
    )

    assert promoted.promote(TrustedReadPriority.SELECTED_CHEAP)
    harness.supervisor.release_job("page:0:1")
    blocker.wait(2.0)
    promoted.wait(2.0)
    ordinary.wait(2.0)

    assert harness.supervisor.labels_submitted() == [
        "page:0:1",
        "page:2:3",
        "page:1:2",
    ]


def test_reprioritizing_coalesced_subscribers_recomputes_effective_priority(
    service_factory: Callable[..., _ServiceHarness],
) -> None:
    harness = service_factory()
    harness.supervisor.block("page:0:1")

    blocker = harness.service.submit_basic_page(0, 1)
    harness.supervisor.wait_until_started("page:0:1")
    ordinary = harness.service.submit_cheap_run(
        6,
        priority=TrustedReadPriority.REMAINING_CHEAP,
    )
    first = harness.service.submit_expensive_run(
        5,
        priority=TrustedReadPriority.REMAINING_EXPENSIVE,
    )
    second = harness.service.submit_expensive_run(
        5,
        priority=TrustedReadPriority.REMAINING_EXPENSIVE,
    )

    assert first._state.operation_id == second._state.operation_id
    assert first.reprioritize(TrustedReadPriority.SELECTED_EXPENSIVE)
    with harness.service._condition:
        operation = harness.service._operations[first._state.operation_id]
        assert operation.status == "queued"
        assert operation.priority == TrustedReadPriority.SELECTED_EXPENSIVE
    assert first.priority == TrustedReadPriority.SELECTED_EXPENSIVE
    assert second.priority == TrustedReadPriority.REMAINING_EXPENSIVE

    assert first.reprioritize(TrustedReadPriority.REMAINING_EXPENSIVE)
    assert not first.reprioritize(TrustedReadPriority.REMAINING_EXPENSIVE)
    with harness.service._condition:
        operation = harness.service._operations[first._state.operation_id]
        assert operation.priority == TrustedReadPriority.REMAINING_EXPENSIVE

    harness.supervisor.release_job("page:0:1")
    assert blocker.wait(2.0).complete
    assert ordinary.wait(2.0).run_id == 6
    assert first.wait(2.0) == second.wait(2.0)
    assert harness.supervisor.labels_submitted() == [
        "page:0:1",
        "cheap:6",
        "expensive:5",
    ]


def test_priority_round_trip_preserves_equal_priority_fifo(
    service_factory: Callable[..., _ServiceHarness],
) -> None:
    harness = service_factory()
    harness.supervisor.block("page:0:1")

    blocker = harness.service.submit_basic_page(0, 1)
    harness.supervisor.wait_until_started("page:0:1")
    first = harness.service.submit_basic_page(
        1,
        2,
        priority=TrustedReadPriority.REMAINING_CHEAP,
    )
    second = harness.service.submit_basic_page(
        2,
        3,
        priority=TrustedReadPriority.REMAINING_CHEAP,
    )

    assert second.reprioritize(TrustedReadPriority.SELECTED_CHEAP)
    assert second.reprioritize(TrustedReadPriority.REMAINING_CHEAP)
    with harness.service._condition:
        first_operation = harness.service._operations[first._state.operation_id]
        second_operation = harness.service._operations[second._state.operation_id]
        assert first_operation.priority == second_operation.priority
        assert first_operation.sequence < second_operation.sequence

    harness.supervisor.release_job("page:0:1")
    assert blocker.wait(2.0).complete
    assert first.wait(2.0).complete
    assert second.wait(2.0).complete
    assert harness.supervisor.labels_submitted() == [
        "page:0:1",
        "page:1:2",
        "page:2:3",
    ]


def test_cancelling_active_promoted_subscriber_restores_survivor_priority(
    service_factory: Callable[..., _ServiceHarness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_transaction_done = threading.Event()
    continue_background = threading.Event()

    class MultiTransactionAdapter(_FakeMetadataAdapter):
        def expensive_run(self, run_id: int) -> TrustedRunRecord:
            self._touch(f"expensive-step-1:{run_id}")
            first_transaction_done.set()
            assert continue_background.wait(2.0)
            self._touch(f"expensive-step-2:{run_id}")
            return TrustedRunRecord(run_id, (("kind", "expensive"),))

    monkeypatch.setattr(
        service_module,
        "TrustedMetadataQueryAdapter",
        MultiTransactionAdapter,
    )
    harness = service_factory()
    background = harness.service.submit_expensive_run(
        11,
        priority=TrustedReadPriority.REMAINING_EXPENSIVE,
    )
    assert first_transaction_done.wait(2.0)

    promoted = harness.service.submit_expensive_run(
        11,
        priority=TrustedReadPriority.SELECTED_CHEAP,
    )
    assert promoted.cancel()
    selected = harness.service.submit_selected_run(
        12,
        priority=TrustedReadPriority.SELECTED_EXPENSIVE,
    )
    continue_background.set()

    with pytest.raises(TrustedReadRequestCancelledError):
        promoted.wait(0.0)
    assert selected.wait(2.0).run.run_id == 12
    assert background.wait(2.0).run_id == 11
    assert harness.supervisor.labels_submitted() == [
        "expensive-step-1:11",
        "selected:12",
        "expensive-step-2:11",
    ]


def test_cancelled_queued_requests_cannot_accumulate_heap_tombstones(
    service_factory: Callable[..., _ServiceHarness],
) -> None:
    harness = service_factory(queue_capacity=2)
    harness.supervisor.block("page:0:1")
    active = harness.service.submit_basic_page(0, 1)
    harness.supervisor.wait_until_started("page:0:1")

    for run_id in range(2, 202):
        transient = harness.service.submit_basic_page(run_id, run_id + 1)
        assert transient.cancel()
        with pytest.raises(TrustedReadRequestCancelledError):
            transient.wait(0.0)
        assert len(harness.service._heap) <= 1

    queued = harness.service.submit_basic_page(1, 2)
    assert len(harness.service._heap) == 1
    harness.supervisor.release_job("page:0:1")
    assert active.wait(2.0).complete
    assert queued.wait(2.0).complete


def test_queued_deadline_completes_while_active_supervisor_wait_is_blocked(
    service_factory: Callable[..., _ServiceHarness],
) -> None:
    harness = service_factory()
    harness.supervisor.block("page:0:1")
    active = harness.service.submit_basic_page(0, 1)
    harness.supervisor.wait_until_started("page:0:1")

    queued = harness.service.submit_basic_page(
        1,
        2,
        deadline=time.monotonic() + 0.08,
    )
    with pytest.raises(TrustedReadRequestDeadlineError):
        queued.wait(1.0)
    assert not active.done
    assert "page:1:2" not in harness.supervisor.labels_submitted()

    harness.supervisor.release_job("page:0:1")
    assert active.wait(2.0).complete


def test_recurring_refresh_cannot_starve_oldest_background_metadata(
    service_factory: Callable[..., _ServiceHarness],
) -> None:
    harness = service_factory()
    harness.supervisor.block("page:0:1")
    blocker = harness.service.submit_basic_page(0, 1)
    harness.supervisor.wait_until_started("page:0:1")
    background = harness.service.submit_expensive_run(
        999,
        priority=TrustedReadPriority.REMAINING_EXPENSIVE,
    )
    refreshes = [harness.service.submit_refresh(run_id) for run_id in range(1, 13)]
    harness.supervisor.release_job("page:0:1")

    blocker.wait(2.0)
    background.wait(2.0)
    _wait_for(lambda: "expensive:999" in harness.supervisor.labels_submitted())
    labels = harness.supervisor.labels_submitted()
    background_index = labels.index("expensive:999")
    assert labels[0] == "page:0:1"
    assert 1 <= labels[1:background_index].count("refresh") <= 8
    assert set(labels[1:background_index]) == {"refresh"}
    assert len(refreshes) == 12


def test_selected_request_overtakes_expensive_work_at_next_transaction_boundary(
    service_factory: Callable[..., _ServiceHarness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_transaction_done = threading.Event()
    continue_expensive = threading.Event()

    class MultiTransactionAdapter(_FakeMetadataAdapter):
        def expensive_run(self, run_id: int) -> TrustedRunRecord:
            self._touch(f"expensive-step-1:{run_id}")
            first_transaction_done.set()
            assert continue_expensive.wait(2.0)
            self._touch(f"expensive-step-2:{run_id}")
            return TrustedRunRecord(run_id, (("kind", "expensive"),))

    monkeypatch.setattr(
        service_module,
        "TrustedMetadataQueryAdapter",
        MultiTransactionAdapter,
    )
    harness = service_factory()
    background = harness.service.submit_expensive_run(
        11,
        priority=TrustedReadPriority.REMAINING_EXPENSIVE,
    )
    assert first_transaction_done.wait(2.0)
    selected = harness.service.submit_selected_run(
        12,
        priority=TrustedReadPriority.SELECTED_EXPENSIVE,
    )
    continue_expensive.set()

    assert selected.wait(2.0).run.run_id == 12
    assert background.wait(2.0).run_id == 11
    assert harness.supervisor.labels_submitted() == [
        "expensive-step-1:11",
        "selected:12",
        "expensive-step-2:11",
    ]


def test_active_demotion_allows_selected_work_at_next_transaction_boundary(
    service_factory: Callable[..., _ServiceHarness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_transaction_done = threading.Event()
    continue_expensive = threading.Event()

    class MultiTransactionAdapter(_FakeMetadataAdapter):
        def expensive_run(self, run_id: int) -> TrustedRunRecord:
            self._touch(f"expensive-step-1:{run_id}")
            first_transaction_done.set()
            assert continue_expensive.wait(2.0)
            self._touch(f"expensive-step-2:{run_id}")
            return TrustedRunRecord(run_id, (("kind", "expensive"),))

    monkeypatch.setattr(
        service_module,
        "TrustedMetadataQueryAdapter",
        MultiTransactionAdapter,
    )
    harness = service_factory()
    background = harness.service.submit_expensive_run(
        11,
        priority=TrustedReadPriority.SELECTED_EXPENSIVE,
    )
    assert first_transaction_done.wait(2.0)
    with harness.service._condition:
        assert harness.service._active_operation is not None
        assert (
            harness.service._active_operation.priority
            == TrustedReadPriority.SELECTED_EXPENSIVE
        )

    assert background.reprioritize(TrustedReadPriority.REMAINING_EXPENSIVE)
    with harness.service._condition:
        assert harness.service._active_operation is not None
        assert (
            harness.service._active_operation.priority
            == TrustedReadPriority.REMAINING_EXPENSIVE
        )
    selected = harness.service.submit_selected_run(
        12,
        priority=TrustedReadPriority.SELECTED_EXPENSIVE,
    )
    continue_expensive.set()

    assert selected.wait(2.0).run.run_id == 12
    assert background.wait(2.0).run_id == 11
    assert harness.supervisor.labels_submitted() == [
        "expensive-step-1:11",
        "selected:12",
        "expensive-step-2:11",
    ]


def test_recurring_nested_refresh_allows_each_background_transaction(
    service_factory: Callable[..., _ServiceHarness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_transaction_done = threading.Event()
    continue_background = threading.Event()

    class MultiTransactionAdapter(_FakeMetadataAdapter):
        def expensive_run(self, run_id: int) -> TrustedRunRecord:
            for step in range(1, 5):
                self._touch(f"background-step-{step}:{run_id}")
                if step == 1:
                    first_transaction_done.set()
                    assert continue_background.wait(2.0)
            return TrustedRunRecord(run_id, (("kind", "expensive"),))

    monkeypatch.setattr(
        service_module,
        "TrustedMetadataQueryAdapter",
        MultiTransactionAdapter,
    )
    harness = service_factory()
    background = harness.service.submit_expensive_run(
        21,
        priority=TrustedReadPriority.REMAINING_EXPENSIVE,
    )
    assert first_transaction_done.wait(2.0)
    refreshes = [harness.service.submit_refresh(run_id) for run_id in range(4)]
    continue_background.set()

    assert background.wait(2.0).run_id == 21
    labels = harness.supervisor.labels_submitted()
    background_labels = [
        label for label in labels if label.startswith("background-step-")
    ]
    assert background_labels == [
        "background-step-1:21",
        "background-step-2:21",
        "background-step-3:21",
        "background-step-4:21",
    ]
    assert "refresh" in labels[1:]
    assert len(refreshes) == 4


def test_service_exposes_no_callbacks_or_callback_thread(
    service_factory: Callable[..., _ServiceHarness],
) -> None:
    harness = service_factory()

    with pytest.raises(TypeError, match="callback"):
        harness.service.submit_basic_page(0, 1, callback=lambda *_args: None)  # type: ignore[call-arg]

    assert not hasattr(harness.service, "_callback_thread")
    assert not any(
        thread.name.startswith("qplot-trusted-read-callback-")
        for thread in threading.enumerate()
    )


def test_active_cancel_is_prompt_and_runs_on_distinct_control_thread(
    service_factory: Callable[..., _ServiceHarness],
) -> None:
    harness = service_factory()
    harness.supervisor.block("page:0:1")
    request = harness.service.submit_basic_page(0, 1)
    harness.supervisor.wait_until_started("page:0:1")

    caller_ident = threading.get_ident()
    started = time.monotonic()
    assert request.cancel()
    assert time.monotonic() - started < 0.5
    assert harness.supervisor.cancel_entered.wait(2.0)
    with pytest.raises(TrustedReadRequestCancelledError):
        request.wait(0.0)

    assert harness.supervisor.cancel_count() == 1
    cancel_label, cancel_ident, cancel_name = harness.supervisor.cancel_threads[0]
    wait_label, wait_ident, wait_name = harness.supervisor.wait_threads[0]
    assert cancel_label == wait_label == "page:0:1"
    assert cancel_ident not in {caller_ident, wait_ident}
    assert cancel_name.startswith("qplot-trusted-read-control-")
    assert wait_name.startswith("qplot-trusted-read-dispatcher-")


def test_expired_queued_request_performs_no_supervisor_ipc(
    service_factory: Callable[..., _ServiceHarness],
) -> None:
    harness = service_factory()

    request = harness.service.submit_basic_page(
        0,
        1,
        deadline=time.monotonic() - 0.01,
    )

    with pytest.raises(TrustedReadRequestDeadlineError):
        request.wait(2.0)
    assert harness.factory_calls == []
    assert harness.supervisor.labels_submitted() == []


def test_expired_queued_request_releases_capacity_without_later_work(
    service_factory: Callable[..., _ServiceHarness],
) -> None:
    harness = service_factory(queue_capacity=1)

    request = harness.service.submit_basic_page(
        0,
        1,
        deadline=time.monotonic() - 0.01,
    )

    with pytest.raises(TrustedReadRequestDeadlineError):
        request.wait(2.0)
    assert harness.factory_calls == []
    replacement = harness.service.submit_basic_page(1, 2)
    assert replacement.wait(2.0).complete
    assert len(harness.factory_calls) == 1


def test_initial_supervisor_startup_is_bounded_by_request_deadline(
    service_factory: Callable[..., _ServiceHarness],
) -> None:
    caller_options: dict[str, object] = {
        "startup_timeout_seconds": 5.0,
        "reply_timeout_seconds": 4.0,
    }

    def delay_for_bound(options: dict[str, object]) -> None:
        startup_timeout = options["startup_timeout_seconds"]
        assert isinstance(startup_timeout, float)
        time.sleep(startup_timeout + 0.02)

    harness = service_factory(
        supervisor_options=caller_options,
        factory_hook=delay_for_bound,
    )
    started = time.monotonic()
    request = harness.service.submit_bootstrap(deadline=started + 0.08)

    with pytest.raises(TrustedReadRequestDeadlineError):
        request.wait(0.5)
    assert time.monotonic() - started < 0.5
    assert caller_options == {
        "startup_timeout_seconds": 5.0,
        "reply_timeout_seconds": 4.0,
    }
    assert len(harness.factory_options) == 1
    startup_timeout = harness.factory_options[0]["startup_timeout_seconds"]
    assert isinstance(startup_timeout, float)
    assert 0 < startup_timeout <= 0.08
    assert harness.factory_options[0]["reply_timeout_seconds"] == 4.0
    assert harness.supervisor.labels_submitted() == []


def test_cancel_is_prompt_during_deadline_bounded_supervisor_startup(
    service_factory: Callable[..., _ServiceHarness],
) -> None:
    factory_entered = threading.Event()

    def delay_for_bound(options: dict[str, object]) -> None:
        startup_timeout = options["startup_timeout_seconds"]
        assert isinstance(startup_timeout, float)
        factory_entered.set()
        time.sleep(startup_timeout + 0.02)

    harness = service_factory(factory_hook=delay_for_bound)
    request = harness.service.submit_bootstrap(deadline=time.monotonic() + 0.15)
    assert factory_entered.wait(1.0)

    started = time.monotonic()
    assert request.cancel()
    assert time.monotonic() - started < 0.5
    with pytest.raises(TrustedReadRequestCancelledError):
        request.wait(0.0)
    assert harness.service.close_async()
    assert harness.service.wait_closed(1.0)


@pytest.mark.parametrize(
    "error_type",
    [
        TrustedLiveSqlRejectedError,
        TrustedLiveQueryError,
        TrustedLiveResultLimitError,
        TrustedLiveBusyTimeoutError,
        TrustedLiveCancelledError,
    ],
    ids=lambda error_type: error_type.__name__,
)
def test_stage3_reusable_failure_keeps_queued_work_and_session_alive(
    service_factory: Callable[..., _ServiceHarness],
    error_type: type[BaseException],
) -> None:
    harness = service_factory()
    harness.supervisor.block("page:0:1")
    failure = error_type("Fake reusable operation failure")
    harness.supervisor.fail_with("page:0:1", failure)

    failed = harness.service.submit_basic_page(0, 1)
    harness.supervisor.wait_until_started("page:0:1")
    queued = harness.service.submit_basic_page(1, 2)
    harness.supervisor.release_job("page:0:1")

    with pytest.raises(error_type):
        failed.wait(2.0)
    assert queued.wait(2.0).complete
    assert harness.supervisor.labels_submitted() == ["page:0:1", "page:1:2"]
    assert not harness.service.closed


@pytest.mark.parametrize(
    "error_type",
    [
        TrustedLiveReaderUnavailableError,
        TrustedLiveUnsupportedSourceError,
        TrustedLiveSourceChangedError,
        TrustedLiveSourceIOError,
        TrustedLiveDeadlineExceededError,
        TrustedLiveInvalidDatabaseError,
        TrustedLiveCleanupError,
        TrustedLiveReaderClosedError,
        TrustedLiveReaderThreadError,
        TrustedLiveTransactionError,
        TrustedLiveReaderError,
        TrustedLiveHelperStartupError,
        TrustedLiveProtocolError,
        TrustedLiveHelperExitedError,
        TrustedLiveHelperReplyTimeoutError,
        TrustedLiveHelperForcedTerminationError,
        RuntimeError,
    ],
    ids=lambda error_type: error_type.__name__,
)
def test_terminal_failure_closes_session_and_never_attempts_queued_work(
    service_factory: Callable[..., _ServiceHarness],
    error_type: type[BaseException],
) -> None:
    harness = service_factory()
    harness.supervisor.block("page:0:1")
    failure = error_type("Fake terminal session failure")
    harness.supervisor.fail_with("page:0:1", failure)

    failed = harness.service.submit_basic_page(0, 1)
    harness.supervisor.wait_until_started("page:0:1")
    queued = harness.service.submit_basic_page(1, 2)
    harness.supervisor.release_job("page:0:1")

    with pytest.raises(error_type):
        failed.wait(2.0)
    with pytest.raises(TrustedReadSessionFailedError) as queued_error:
        queued.wait(2.0)
    assert queued_error.value.__cause__ is failure
    assert harness.service.wait_closed(2.0)
    assert harness.supervisor.labels_submitted() == ["page:0:1"]


def test_fatal_failure_invalidates_queued_work_without_replay(
    service_factory: Callable[..., _ServiceHarness],
) -> None:
    harness = service_factory()
    harness.supervisor.block("page:0:1")
    harness.supervisor.fail_fatally("page:0:1")

    failed = harness.service.submit_basic_page(0, 1)
    harness.supervisor.wait_until_started("page:0:1")
    queued_one = harness.service.submit_basic_page(1, 2)
    queued_two = harness.service.submit_basic_page(2, 3)
    harness.supervisor.release_job("page:0:1")

    with pytest.raises(TrustedLiveProtocolError):
        failed.wait(2.0)
    with pytest.raises(TrustedReadSessionFailedError):
        queued_one.wait(2.0)
    with pytest.raises(TrustedReadSessionFailedError):
        queued_two.wait(2.0)
    assert harness.service.wait_closed(2.0)
    assert harness.supervisor.labels_submitted() == ["page:0:1"]


def test_injected_dispatcher_failure_completes_active_and_queued_accounting(
    service_factory: Callable[..., _ServiceHarness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = service_factory()
    harness.supervisor.block("page:0:1")
    injected = RuntimeError("injected dispatcher completion failure")

    active = harness.service.submit_basic_page(0, 1)
    harness.supervisor.wait_until_started("page:0:1")
    queued = harness.service.submit_basic_page(1, 2)

    def fail_completion(*_args: object, **_kwargs: object) -> None:
        raise injected

    monkeypatch.setattr(harness.service, "_finish_operation_locked", fail_completion)
    harness.supervisor.release_job("page:0:1")

    for request in (active, queued):
        with pytest.raises(TrustedReadSessionFailedError) as failure:
            request.wait(2.0)
        assert failure.value.__cause__ is injected

    assert harness.service.wait_closed(2.0)
    assert harness.service._requests == {}
    assert harness.service._operations == {}
    assert harness.service._coalesced == {}
    assert harness.service._heap == []
    assert harness.service._active_operation is None
    assert harness.service.liveness().outstanding_requests == 0


def test_injected_control_loop_failure_completes_active_and_queued_accounting(
    service_factory: Callable[..., _ServiceHarness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = service_factory()
    harness.supervisor.block("page:0:1")
    injected = RuntimeError("injected control-loop failure")

    active = harness.service.submit_basic_page(0, 1)
    harness.supervisor.wait_until_started("page:0:1")
    queued = harness.service.submit_basic_page(1, 2)
    _wait_for(lambda: not harness.service._control_wakeup.is_set())

    def fail_deadline_scan(_now: float) -> None:
        raise injected

    monkeypatch.setattr(
        harness.service,
        "_expire_queued_requests_locked",
        fail_deadline_scan,
    )
    harness.service._control_wakeup.set()

    for request in (active, queued):
        with pytest.raises(TrustedReadSessionFailedError) as failure:
            request.wait(2.0)
        assert failure.value.__cause__ is injected

    assert harness.service.wait_closed(2.0)
    assert harness.supervisor.cancel_entered.is_set()
    assert harness.service._requests == {}
    assert harness.service._operations == {}
    assert harness.service._coalesced == {}
    assert harness.service._heap == []
    assert harness.service._active_operation is None
    assert harness.service.liveness().outstanding_requests == 0


@pytest.mark.parametrize(
    "cancel_error_type",
    [TrustedLiveSupervisorClosedError, RuntimeError],
    ids=lambda error_type: error_type.__name__,
)
def test_unexpected_control_cancel_failure_terminally_fails_queued_requests(
    service_factory: Callable[..., _ServiceHarness],
    cancel_error_type: type[BaseException],
) -> None:
    harness = service_factory()
    harness.supervisor.block("page:0:1")
    cancel_failure = cancel_error_type("Fake control cancellation failure")
    harness.supervisor.fail_cancel_with(cancel_failure)

    active = harness.service.submit_basic_page(0, 1)
    harness.supervisor.wait_until_started("page:0:1")
    queued = harness.service.submit_basic_page(1, 2)

    assert active.cancel()
    assert harness.supervisor.cancel_entered.wait(2.0)
    with pytest.raises(TrustedReadRequestCancelledError):
        active.wait(0.0)
    with pytest.raises(TrustedReadSessionFailedError) as queued_error:
        queued.wait(2.0)
    assert queued_error.value.__cause__ is cancel_failure
    assert harness.service.liveness().outstanding_requests == 0

    harness.supervisor.release_job("page:0:1")
    assert harness.service.wait_closed(2.0)
    assert harness.supervisor.labels_submitted() == ["page:0:1"]


def test_stale_cancel_exception_after_dispatcher_close_is_benign(
    service_factory: Callable[..., _ServiceHarness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = service_factory()
    harness.supervisor.block("page:0:1")
    cancel_snapshot_taken = threading.Event()
    allow_stale_cancel = threading.Event()
    original_cancel = harness.supervisor.cancel

    def cancel_after_dispatcher_close(job: _FakeJob) -> bool:
        cancel_snapshot_taken.set()
        assert allow_stale_cancel.wait(2.0)
        if harness.supervisor.closed:
            raise TrustedLiveSupervisorClosedError(
                "The dispatcher already closed this fake supervisor."
            )
        return original_cancel(job)

    monkeypatch.setattr(
        harness.supervisor,
        "cancel",
        cancel_after_dispatcher_close,
    )
    request = harness.service.submit_basic_page(0, 1)
    harness.supervisor.wait_until_started("page:0:1")

    assert harness.service.close_async()
    assert cancel_snapshot_taken.wait(2.0)
    harness.supervisor.release_job("page:0:1")
    assert harness.supervisor.close_entered.wait(2.0)
    _wait_for(lambda: harness.service.closed)

    allow_stale_cancel.set()
    assert harness.service.wait_closed(2.0)
    with pytest.raises(TrustedReadServiceClosedError):
        request.wait(0.0)
    assert harness.service._fatal_error is None
    assert harness.service.close_error is None
    liveness = harness.service.liveness()
    assert liveness.closed
    assert not liveness.closing
    assert not liveness.dispatcher_alive
    assert not liveness.control_alive
    assert not liveness.helper_alive
    assert liveness.outstanding_requests == 0


def test_async_close_reports_full_liveness_until_shutdown_finishes(
    service_factory: Callable[..., _ServiceHarness],
) -> None:
    harness = service_factory()
    harness.supervisor.block("page:0:1")
    harness.supervisor.pause_cancel()
    harness.supervisor.pause_close()
    request = harness.service.submit_basic_page(0, 1)
    harness.supervisor.wait_until_started("page:0:1")

    started = time.monotonic()
    assert harness.service.close_async()
    assert time.monotonic() - started < 0.5
    assert harness.service.closing
    assert not harness.service.closed
    with pytest.raises(TrustedReadServiceClosedError):
        request.wait(0.0)

    assert harness.supervisor.cancel_entered.wait(2.0)
    assert harness.supervisor.close_entered.wait(2.0)
    during_close = harness.service.liveness()
    assert during_close.dispatcher_alive
    assert during_close.control_alive
    assert during_close.helper_alive
    assert during_close.receiver_alive
    assert during_close.open_supervisor_endpoints == 3
    assert during_close.resource_cleanup_pending
    assert during_close.outstanding_requests == 0
    assert during_close.closing
    assert not during_close.closed

    harness.supervisor.cancel_release.set()
    harness.supervisor.close_release.set()
    assert harness.service.wait_closed(2.0)
    _wait_for(
        lambda: (
            not harness.service.liveness().dispatcher_alive
            and not harness.service.liveness().control_alive
        )
    )
    after_close = harness.service.liveness()
    assert not harness.service.closing
    assert harness.service.closed
    assert not after_close.dispatcher_alive
    assert not after_close.control_alive
    assert not after_close.helper_alive
    assert not after_close.receiver_alive
    assert after_close.open_supervisor_endpoints == 0
    assert after_close.unreaped_incarnations == 0
    assert not after_close.resource_cleanup_pending
    assert after_close.outstanding_requests == 0
    assert not after_close.closing
    assert after_close.closed
    assert harness.supervisor.cancel_count() == 1
    assert harness.supervisor.close_threads[0][1].startswith(
        "qplot-trusted-read-dispatcher-"
    )


def test_liveness_probe_preserves_exact_supervisor_exception(
    service_factory: Callable[..., _ServiceHarness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = service_factory()
    harness.service.submit_basic_page(0, 1).wait(2.0)

    def fail_liveness() -> TrustedLiveSupervisorLiveness:
        raise RuntimeError("injected supervisor liveness failure")

    with monkeypatch.context() as context:
        context.setattr(harness.supervisor, "resource_liveness", fail_liveness)
        with pytest.raises(
            RuntimeError,
            match="injected supervisor liveness failure",
        ):
            harness.service.liveness()


def test_async_close_retains_unreaped_receiver_and_pipe_until_zero_wait_reap(
    service_factory: Callable[..., _ServiceHarness],
) -> None:
    harness = service_factory()
    harness.service.submit_basic_page(0, 1).wait(2.0)
    harness.supervisor.retain_unreaped_on_close()

    assert harness.service.close_async()
    _wait_for(lambda: harness.service.closed)
    assert not harness.service.wait_closed(0.0)
    liveness = harness.service.liveness()
    assert not liveness.dispatcher_alive
    assert not liveness.control_alive
    assert not liveness.helper_alive
    assert liveness.receiver_alive
    assert liveness.open_supervisor_endpoints == 1
    assert liveness.unreaped_incarnations == 1
    assert liveness.resource_cleanup_pending

    harness.supervisor.release_unreaped_resources()
    assert harness.service.wait_closed(2.0)
    liveness = harness.service.liveness()
    assert not liveness.receiver_alive
    assert liveness.open_supervisor_endpoints == 0
    assert liveness.unreaped_incarnations == 0
    assert not liveness.resource_cleanup_pending
