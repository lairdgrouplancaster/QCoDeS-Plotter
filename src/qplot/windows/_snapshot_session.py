"""Bounded selected-run Snapshot browsing, independent of Qt widgets.

SnapshotSession owns the only retained raw source, its cancellation event, and
the single-worker executor. All public methods run on the creating thread.
Worker completion only touches the locked queue and calls ``ready``; that
callback must arrange a queued owner-thread wakeup. ``is_current`` and ``reject``
run only on the owner thread, so database/selection checks and widget mutation
remain the Qt bridge's responsibility.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:
    from qplot.datahandling.file_identity import DatabaseInstance
    from qplot.datahandling.trusted_live_service import TrustedDerivedReadService
    from qplot.datahandling.trusted_query_types import (
        TrustedSelectedRunDetail,
        TrustedSourceRevision,
    )
    from qplot.datahandling.trusted_work_coordinator import (
        TrustedSelectedDetailPublication,
    )

_REQUEST_DEADLINE_SECONDS = 15.0
_MAX_OUTSTANDING = 2
_MISSING = object()


class SnapshotSource(Protocol):
    """The worker-only portion of a retained trusted Snapshot source."""

    session_token: object

    def page(
        self,
        parent_handle: object,
        continuation: object,
        *,
        cancel_check: Callable[[], None],
    ) -> object: ...

    def full_value(
        self,
        handle: object,
        *,
        cancel_check: Callable[[], None],
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class SnapshotRequestFence:
    """Complete request and publication identity for one selected Snapshot request."""

    database_instance: DatabaseInstance
    binding_serial: int
    coordinator_generation: int
    helper_incarnation: int
    source_revision: TrustedSourceRevision
    selection_generation: int
    run_index: int
    run_id: int
    run_guid: str
    service: TrustedDerivedReadService
    service_session_generation: int
    snapshot_token: object
    session_serial: int
    parent_handle: object
    continuation: object
    request: object
    kind: str
    handle: object | None = None
    path: object | None = None


@dataclass(slots=True)
class _SnapshotWork:
    """Worker-only arguments captured before a Snapshot worker is submitted."""

    work_id: int
    fence: SnapshotRequestFence
    source: SnapshotSource | None
    cancelled: threading.Event
    deadline: float


@dataclass(frozen=True, slots=True)
class SnapshotResult:
    """Traceback-free worker outcome queued for owner-thread publication."""

    work: _SnapshotWork
    value: object | None
    accepted: bool


def detach_snapshot_source(
    publication: TrustedSelectedDetailPublication,
) -> (
    tuple[
        TrustedSelectedDetailPublication,
        TrustedSelectedRunDetail,
        SnapshotSource | None,
        object | None,
    ]
    | None
):
    """Remove the sole raw source before retaining or publishing a detail."""

    detail = publication.detail
    snapshot = detail.snapshot
    source = getattr(snapshot, "source", _MISSING)
    token = getattr(snapshot, "session_token", _MISSING)
    if source is _MISSING and token is _MISSING:
        # Compatibility with bounded pre-pagination views: there is no raw
        # source to retain, so preserve object identity for those callers.
        return publication, detail, None, None
    if source is _MISSING or source is None:
        if token is _MISSING or token is None or token == "":
            return publication, detail, None, None
        return None
    if token is _MISSING or token is None or token == "":
        return None
    source_token = getattr(source, "session_token", _MISSING)
    if (
        source_token is _MISSING
        or not safe_equal(source_token, token)
        or not callable(getattr(source, "page", None))
        or not callable(getattr(source, "full_value", None))
    ):
        return None
    try:
        detached_snapshot = replace(snapshot, source=None)
        detached_detail = replace(detail, snapshot=detached_snapshot)
        detached_publication = replace(publication, detail=detached_detail)
    except (TypeError, ValueError):
        return None
    return (
        detached_publication,
        detached_detail,
        cast(SnapshotSource, source),
        token,
    )


def _request_matches_fence(fence: SnapshotRequestFence) -> bool:
    request = fence.request
    if (
        not safe_equal(
            getattr(request, "session_token", _MISSING),
            fence.snapshot_token,
        )
        or not safe_equal(
            getattr(request, "parent_handle", _MISSING),
            fence.parent_handle,
        )
        or not safe_equal(
            getattr(request, "continuation", _MISSING),
            fence.continuation,
        )
    ):
        return False
    if fence.kind != "value":
        return True
    return safe_equal(
        getattr(request, "handle", _MISSING),
        fence.handle,
    ) and safe_equal(
        getattr(request, "path", _MISSING),
        fence.path,
    )


def safe_equal(left: object, right: object) -> bool:
    try:
        result = left == right
    except BaseException:  # noqa: BLE001 - malformed requests are rejected
        return False
    return result if type(result) is bool else False


class SnapshotSession:
    """Own source lifetime and bounded work; delegate publication policy."""

    def __init__(
        self,
        *,
        ready: Callable[[], None],
        is_current: Callable[[SnapshotRequestFence], bool],
        reject: Callable[[object], None],
    ) -> None:
        self._owner_thread_id = threading.get_ident()
        self._ready = ready
        self._is_current = is_current
        self._reject = reject
        self._source: SnapshotSource | None = None
        self._token: object | None = None
        self._cancelled: threading.Event | None = None
        self._serial = 0
        self._executor: ThreadPoolExecutor | None = None
        self._executor_closed = False
        self._work_serial = 0
        self._lock = threading.Lock()
        self._slots: dict[int, _SnapshotWork] = {}
        self._futures: dict[int, Future[SnapshotResult]] = {}
        self._live_futures: set[Future[SnapshotResult]] = set()
        self._results: queue.Queue[SnapshotResult] = queue.Queue(maxsize=_MAX_OUTSTANDING)

    @property
    def source(self) -> SnapshotSource | None:
        self._require_owner()
        return self._source

    @property
    def serial(self) -> int:
        self._require_owner()
        return self._serial

    def bind(self, source: SnapshotSource, token: object) -> None:
        """Cancel the prior session before retaining a replacement source."""

        self._require_owner()
        if self._executor_closed:
            raise RuntimeError("A closed Snapshot session cannot be rebound.")
        self.cancel()
        self._source = source
        self._token = token
        self._cancelled = threading.Event()

    def pending_counts(self) -> tuple[int, int]:
        """Return retained work and live futures for liveness/diagnostics."""

        self._require_owner()
        with self._lock:
            return len(self._slots), len(self._live_futures)

    def submit(self, fence: SnapshotRequestFence) -> bool:
        """Reserve a slot only while both session and publication are current."""

        self._require_owner()
        source = self._source
        session_cancel = self._cancelled
        if source is None or session_cancel is None:
            return False
        if not self._fence_is_current(fence, source):
            return False
        if self._executor_closed:
            return False
        executor = self._executor
        if executor is None:
            executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="qplot-trusted-snapshot",
            )
            self._executor = executor
        self._work_serial += 1
        work = _SnapshotWork(
            work_id=self._work_serial,
            fence=fence,
            source=source,
            cancelled=session_cancel,
            deadline=time.monotonic() + _REQUEST_DEADLINE_SECONDS,
        )
        # This is the final owner-side revalidation immediately before submit.
        if not self._fence_is_current(fence, source):
            return False
        with self._lock:
            queue_full = len(self._slots) >= _MAX_OUTSTANDING
            if not queue_full:
                self._slots[work.work_id] = work
        if queue_full:
            return False
        try:
            future = executor.submit(self._run_work, work)
        except RuntimeError:
            with self._lock:
                self._slots.pop(work.work_id, None)
            return False
        with self._lock:
            self._futures[work.work_id] = future
            self._live_futures.add(future)

        def publish_when_done(completed: Future[SnapshotResult]) -> None:
            self._work_done(work, completed)

        future.add_done_callback(publish_when_done)
        return True

    @staticmethod
    def _run_work(work: _SnapshotWork) -> SnapshotResult:
        """Run pure Snapshot source work without reading or mutating Qt state."""

        def cancel_check() -> None:
            if work.cancelled.is_set():
                raise InterruptedError("The selected Snapshot session was cancelled.")
            if time.monotonic() >= work.deadline:
                raise TimeoutError(
                    "The selected Snapshot request exceeded its deadline."
                )

        try:
            cancel_check()
            source = work.source
            if source is None:
                raise InterruptedError("The selected Snapshot source was released.")
            if work.fence.kind == "page":
                value = source.page(
                    work.fence.parent_handle,
                    work.fence.continuation,
                    cancel_check=cancel_check,
                )
            else:
                value = source.full_value(
                    work.fence.handle,
                    cancel_check=cancel_check,
                )
            cancel_check()
        except BaseException:  # noqa: BLE001 - only a traceback-free rejection escapes
            return SnapshotResult(work, None, False)
        return SnapshotResult(work, value, value is not None)

    def _work_done(
        self,
        work: _SnapshotWork,
        future: Future[SnapshotResult],
    ) -> None:
        """Completion hook: queue a result, then notify without touching owner state."""

        try:
            outcome = future.result()
        except BaseException:  # noqa: BLE001 - discard exception/traceback at boundary
            outcome = SnapshotResult(work, None, False)
        with self._lock:
            self._live_futures.discard(future)
            self._futures.pop(work.work_id, None)
            if self._slots.get(work.work_id) is not work:
                return
            if work.cancelled.is_set():
                self._slots.pop(work.work_id, None)
                return
            try:
                self._results.put_nowait(outcome)
            except queue.Full:
                self._slots.pop(work.work_id, None)
                return
        try:
            self._ready()
        except RuntimeError:
            with self._lock:
                self._slots.pop(work.work_id, None)
                self._drain_results_locked(work.work_id)

    def take_results(self) -> list[SnapshotResult]:
        """Release completed slots; revalidate each result before publishing it."""

        self._require_owner()
        outcomes: list[SnapshotResult] = []
        while True:
            try:
                outcome = self._results.get_nowait()
            except queue.Empty:
                break
            with self._lock:
                if self._slots.get(outcome.work.work_id) is not outcome.work:
                    continue
                self._slots.pop(outcome.work.work_id, None)
            outcomes.append(outcome)
        return outcomes

    def accepts(self, outcome: SnapshotResult) -> bool:
        """Repeat all checks immediately before each widget publication.

        A preceding result can trigger reentrant UI code that changes selection,
        so draining the queue must not pre-approve an entire batch.
        """

        self._require_owner()
        source = outcome.work.source
        return (
            outcome.accepted
            and source is not None
            and time.monotonic() < outcome.work.deadline
            and self._fence_is_current(outcome.work.fence, source)
        )

    def _fence_is_current(
        self,
        fence: SnapshotRequestFence,
        source: SnapshotSource,
    ) -> bool:
        cancel = self._cancelled
        return (
            cancel is not None
            and not cancel.is_set()
            and self._source is source
            and self._serial == fence.session_serial
            and safe_equal(self._token, fence.snapshot_token)
            and _request_matches_fence(fence)
            and self._is_current(fence)
        )

    def cancel(self) -> None:
        """Cancel source work and release the raw selected-run Snapshot source."""

        self._require_owner()
        cancel = self._cancelled
        self._source = None
        self._token = None
        self._cancelled = None
        self._serial += 1
        if cancel is None:
            return
        futures: list[Future[SnapshotResult]] = []
        rejected_requests: list[object] = []

        def retain_rejection(request: object) -> None:
            if all(existing is not request for existing in rejected_requests):
                rejected_requests.append(request)

        with self._lock:
            # Publish cancellation under the same lock used by completion.
            # A callback can therefore only queue before this point (and be
            # drained below), or observe cancellation after every retained
            # work descriptor has released its source and rejection.
            cancel.set()
            while True:
                try:
                    outcome = self._results.get_nowait()
                except queue.Empty:
                    break
                if outcome.work.cancelled is cancel:
                    retain_rejection(outcome.work.fence.request)
                outcome.work.source = None
                self._slots.pop(outcome.work.work_id, None)
            for work_id, work in tuple(self._slots.items()):
                if work.cancelled is not cancel:
                    continue
                retain_rejection(work.fence.request)
                work.source = None
                future = self._futures.get(work_id)
                if future is None:
                    self._slots.pop(work_id, None)
                else:
                    futures.append(future)
        # Future.cancel() can invoke callbacks synchronously, so it must run
        # outside the lock those callbacks acquire.
        for future in futures:
            future.cancel()
        for request in rejected_requests:
            self._reject(request)

    def _drain_results_locked(self, work_id: int) -> None:
        retained: list[SnapshotResult] = []
        while True:
            try:
                outcome = self._results.get_nowait()
            except queue.Empty:
                break
            if outcome.work.work_id != work_id:
                retained.append(outcome)
        for outcome in retained:
            self._results.put_nowait(outcome)

    def shutdown(self) -> None:
        """Cancel the current session and close without waiting for workers."""

        self._require_owner()
        self.cancel()
        if self._executor_closed:
            return
        self._executor_closed = True
        executor = self._executor
        self._executor = None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    def _require_owner(self) -> None:
        if threading.get_ident() != self._owner_thread_id:
            raise RuntimeError("Snapshot sessions must be used on their owner thread.")
