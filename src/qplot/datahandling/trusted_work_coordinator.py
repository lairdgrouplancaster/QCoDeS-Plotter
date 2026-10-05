"""Qt-independent owner/executor boundary for trusted Stage 5B derived work."""

from __future__ import annotations

import hashlib
import queue
import threading
import time
from array import array
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias

from qplot.datahandling.file_identity import DatabaseInstance
from qplot.datahandling.trusted_derived_cache import TrustedDerivedDiskCache
from qplot.datahandling.trusted_derived_rendering import (
    DerivedPayload,
    render_trusted_derived_payload,
    validate_trusted_derived_payload,
)
from qplot.datahandling.trusted_live import (
    TrustedLiveBusyTimeoutError,
    TrustedLiveCancelledError,
    TrustedLiveDeadlineExceededError,
    TrustedLiveSourceChangedError,
)
from qplot.datahandling.trusted_live_queries import (
    TrustedDerivedSourceObservation,
    TrustedSelectedRunDetail,
    TrustedSourceRevision,
    trusted_derived_source_revision,
)
from qplot.datahandling.trusted_live_service import (
    TrustedLiveReadService,
    TrustedReadQueueFullError,
    TrustedReadRequestCancelledError,
    TrustedReadRequestDeadlineError,
    TrustedReadSessionFailedError,
)
from qplot.datahandling.trusted_work_scheduler import (
    CompletionDisposition,
    ScheduledWork,
    SchedulerLifecycle,
    SchedulerSnapshot,
    TrustedCacheWorkKey,
    TrustedRunTier,
    TrustedRunWorkSource,
    TrustedWorkKind,
    TrustedWorkScheduler,
    TrustedWorkState,
    WorkFormat,
    WorkPublication,
)

TRUSTED_DERIVED_DEFAULT_DEADLINE_SECONDS = 15.0
TRUSTED_DERIVED_MAX_REUSED_SOURCE_BYTES = 8 * 1024 * 1024
TRUSTED_DERIVED_MAX_REUSED_SOURCES = 512
TRUSTED_DERIVED_BACKGROUND_AGING_CLAIM_BOUND = 8
TRUSTED_DERIVED_SAME_TIER_PROGRESSIVE_CLAIM_BOUND = 3
TRUSTED_DERIVED_PROGRESSIVE_MIN_INTERVAL_SECONDS = 0.01
_PROGRESSIVE_ACTIVE_BIT = 1
_PROGRESSIVE_SERVED_BIT = 2
_RETRY_KIND_COUNT = len(TrustedWorkKind)

WakeupCallback: TypeAlias = Callable[[], None]
PublicationCallback: TypeAlias = Callable[[WorkPublication], None]


class TrustedDerivedErrorCategory(StrEnum):
    """Bounded worker outcome classification safe to marshal to the owner."""

    TRANSIENT = "transient"
    PERMANENT = "permanent"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class TrustedDerivedErrorRecord:
    """Non-executable bounded error data; never retains traceback objects."""

    category: TrustedDerivedErrorCategory
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class TrustedSelectedDetailPublication:
    """One ephemeral selected-detail result with complete owner-side fencing."""

    generation: int
    key: TrustedCacheWorkKey
    run_index: int
    run_id: int
    run_guid: str
    helper_incarnation: int
    selection_generation: int
    detail: TrustedSelectedRunDetail


SelectedDetailCallback: TypeAlias = Callable[[TrustedSelectedDetailPublication], None]
ErrorCallback: TypeAlias = Callable[[ScheduledWork, TrustedDerivedErrorRecord], None]


@dataclass(frozen=True, slots=True)
class _SelectedDetailAttempt:
    database_instance: DatabaseInstance
    generation: int
    source_revision: TrustedSourceRevision
    helper_incarnation: int
    selection_generation: int
    run_index: int
    run_id: int
    run_guid: str
    cancelled: threading.Event


class _JobDeadlineExceeded(TimeoutError):
    pass


@dataclass(frozen=True, slots=True)
class TrustedDerivedRun:
    """Stable run-table entry used by the coordinator and persistent broker."""

    run_id: int
    run_guid: str
    source_revision: TrustedSourceRevision

    def __post_init__(self) -> None:
        if type(self.run_id) is not int or self.run_id <= 0:
            raise ValueError("run_id must be a positive integer.")
        if not self.run_guid:
            raise ValueError("run_guid must be non-empty.")
        if not isinstance(self.source_revision, TrustedSourceRevision):
            raise TypeError("source_revision must be TrustedSourceRevision.")

    def scheduler_source(self) -> TrustedRunWorkSource:
        return TrustedRunWorkSource(self.run_guid, self.source_revision)


@dataclass(frozen=True, slots=True)
class _ExecutionResult:
    work: ScheduledWork
    key: TrustedCacheWorkKey
    payload: DerivedPayload
    observation: TrustedDerivedSourceObservation | None
    observation_revision: TrustedSourceRevision | None
    observation_size: int
    cache_hit: bool
    selected_detail_attempt: _SelectedDetailAttempt | None = None
    selected_detail: TrustedSelectedRunDetail | None = None


@dataclass(frozen=True, slots=True)
class _ProgressiveExecutionResult:
    """One metadata-only layout page which is not yet publishable."""

    work: ScheduledWork
    key: TrustedCacheWorkKey
    observation: TrustedDerivedSourceObservation
    observation_revision: TrustedSourceRevision
    observation_size: int
    selected_detail_attempt: _SelectedDetailAttempt | None = None
    selected_detail: TrustedSelectedRunDetail | None = None


@dataclass(frozen=True, slots=True)
class _ExecutionFailure:
    work: ScheduledWork
    error: TrustedDerivedErrorRecord


@dataclass(frozen=True, slots=True)
class _ActiveClaim:
    work: ScheduledWork
    future: Future[_WorkerResult]
    source_namespace: bytes
    deadline: float
    selected_detail_attempt: _SelectedDetailAttempt | None


_SuccessfulResult: TypeAlias = _ExecutionResult | _ProgressiveExecutionResult
_WorkerResult: TypeAlias = _SuccessfulResult | _ExecutionFailure
_RetainedObservation: TypeAlias = tuple[
    TrustedDerivedSourceObservation,
    TrustedSourceRevision,
    int,
]


class _RetryWakeupNotifier:
    """Own at most one backoff timer which only invokes the UI notifier.

    The timer thread never reads scheduler/coordinator state. Owner-thread
    generation changes synchronously cancel its opaque token before returning.
    """

    def __init__(self, wakeup: WakeupCallback | None) -> None:
        self._wakeup = wakeup
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._deadline: float | None = None
        self._serial = 0

    def schedule(self, deadline: float) -> None:
        if self._wakeup is None:
            return
        with self._lock:
            if self._deadline == deadline and self._timer is not None:
                return
            prior = self._timer
            self._serial += 1
            serial = self._serial
            self._deadline = deadline
            timer = threading.Timer(
                max(0.0, deadline - time.monotonic()),
                self._fire,
                args=(serial,),
            )
            timer.daemon = True
            self._timer = timer
            if prior is not None:
                prior.cancel()
            timer.start()

    def cancel(self) -> None:
        with self._lock:
            self._serial += 1
            timer = self._timer
            self._timer = None
            self._deadline = None
            if timer is not None:
                timer.cancel()

    def _fire(self, serial: int) -> None:
        with self._lock:
            if serial != self._serial or self._timer is None:
                return
            assert self._deadline is not None
            remaining = self._deadline - time.monotonic()
            if remaining > 0.0:
                # Some Windows timer implementations can wake slightly before
                # their requested monotonic deadline.  A one-shot early
                # notification would be consumed while the coordinator still
                # refuses to pump, stranding pending retry work.  Retain the
                # same opaque serial and re-arm for the exact remainder.
                timer = threading.Timer(remaining, self._fire, args=(serial,))
                timer.daemon = True
                self._timer = timer
                timer.start()
                return
            self._timer = None
            self._deadline = None
            # Invoke while holding the notifier-only lock so cancel() cannot
            # return while an obsolete callback is still about to run.
            assert self._wakeup is not None
            self._wakeup()


class _ProgressiveWakeupNotifier(_RetryWakeupNotifier):
    """Own the sole coalesced timer used to pace progressive page restarts."""


class TrustedWorkCoordinator:
    """Execute exactly one lazy scheduler claim on one controlled worker.

    The constructing thread owns every scheduler call.  The worker callback
    puts at most one completion into ``_completions`` and invokes ``wakeup``;
    Stage 5C can map that callback to a queued Qt signal without changing this
    backend.  ``poll`` is the only completion/publication path.
    """

    def __init__(
        self,
        database_instance: DatabaseInstance,
        runs: Sequence[TrustedDerivedRun],
        service: TrustedLiveReadService,
        *,
        cache: TrustedDerivedDiskCache | None = None,
        formats: Mapping[TrustedWorkKind, WorkFormat] | None = None,
        wakeup: WakeupCallback | None = None,
        on_publish: PublicationCallback | None = None,
        on_selected_detail: SelectedDetailCallback | None = None,
        on_error: ErrorCallback | None = None,
        deadline_seconds: float = TRUSTED_DERIVED_DEFAULT_DEADLINE_SECONDS,
        own_service: bool = False,
    ) -> None:
        if not isinstance(service, TrustedLiveReadService):
            raise TypeError("service must be TrustedLiveReadService.")
        if service.database_instance != database_instance:
            raise ValueError("The service and coordinator database instances differ.")
        if not 0 < deadline_seconds <= 300:
            raise ValueError("deadline_seconds must be from zero through 300 seconds.")
        self._owner_thread_id = threading.get_ident()
        self._runs = self._validated_runs(runs)
        self._service = service
        self._cache = cache or TrustedDerivedDiskCache(enabled=True)
        self._wakeup = wakeup
        self._on_publish = on_publish
        self._on_selected_detail = on_selected_detail
        self._on_error = on_error
        self._deadline_seconds = float(deadline_seconds)
        self._own_service = bool(own_service)
        self._scheduler = TrustedWorkScheduler(
            database_instance,
            tuple(run.scheduler_source() for run in self._runs),
            formats=formats,
            on_publish=self._publish,
        )
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="qplot-trusted-derived",
        )
        self._completions: queue.Queue[_WorkerResult] = queue.Queue(maxsize=1)
        self._active: _ActiveClaim | None = None
        self._selected_index: int | None = None
        self._selection_generation = 0
        self._helper_incarnation = 0
        self._accepted_selected_detail: (
            tuple[int, int, int, int, str, TrustedSourceRevision] | None
        ) = None
        self._run_indices_by_guid = {
            run.run_guid: index for index, run in enumerate(self._runs)
        }
        self._reused_observations: OrderedDict[str, _RetainedObservation] = (
            OrderedDict()
        )
        self._reused_observation_bytes = 0
        # One byte per stable run is the complete continuation-ready state.
        # Its served bit is compared with the global round epoch, allowing a
        # lazy full rotation without materialising an O(run-count) ready deque.
        self._progressive_state = bytearray(len(self._runs))
        self._progressive_round_epoch = False
        self._progressive_active_count = 0
        self._progressive_remaining_cursor = 0
        self._progressive_restart_deferred = False
        self._progressive_not_before = 0.0
        self._progressive_notifier = _ProgressiveWakeupNotifier(wakeup)
        self._foreground_claims_since_background_metadata = 0
        self._age_progressive_background_next = True
        self._ordinary_claims_since_progressive_page = 0
        self._memory_payload: tuple[TrustedCacheWorkKey, DerivedPayload] | None = None
        self._deferred_invalidations: set[int] = set()
        retry_slot_count = len(self._runs) * _RETRY_KIND_COUNT
        self._retry_attempts = bytearray(retry_slot_count)
        self._retry_deadlines = array("d", [0.0]) * retry_slot_count
        self._retry_notifier = _RetryWakeupNotifier(wakeup)
        self._invalidation_serial = 0
        self._closed = False
        self._executor_joined = False
        self._configure_cache(database_instance)

    @property
    def scheduler(self) -> TrustedWorkScheduler:
        """Expose owner-thread state for Stage 5C diagnostics, not worker use."""

        self._require_owner()
        return self._scheduler

    @property
    def active(self) -> bool:
        self._require_owner()
        return self._active is not None

    @property
    def runs(self) -> tuple[TrustedDerivedRun, ...]:
        """Return the current stable table, including refined revisions."""

        self._require_owner()
        return self._runs

    def snapshot(self) -> SchedulerSnapshot:
        self._require_owner()
        return self._scheduler.snapshot()

    def start(self) -> None:
        self._require_owner()
        self._require_open()
        self._pump()

    def select_run(self, run_index: int | None) -> None:
        self._require_owner()
        if run_index == self._selected_index:
            self._scheduler.select_run(run_index)
            self._pump()
            return
        # Validate before changing the selected-detail epoch.  A rejected UI
        # index must leave every previously accepted detail fence intact.
        self._scheduler.select_run(run_index)
        self._selection_changed(run_index)
        self._pump()

    def set_priority(
        self,
        selected_index: int | None,
        visible_indices: Sequence[int],
        *,
        pump: bool = True,
    ) -> None:
        """Atomically apply selection and viewport state at one work boundary."""

        self._require_owner()
        prior_visible = self._scheduler.snapshot().visible_indices
        selection_changed = selected_index != self._selected_index
        self._scheduler.set_priority(selected_index, visible_indices)
        if selection_changed:
            self._selection_changed(selected_index)
        self._viewport_changed(
            prior_visible, self._scheduler.snapshot().visible_indices
        )
        if pump:
            self._pump()

    def _selection_changed(self, run_index: int | None) -> None:
        """Advance selected-detail state after scheduler validation succeeds."""

        self._cancel_selected_detail_attempt()
        self._selection_generation += 1
        self._selected_index = run_index
        self._accepted_selected_detail = None
        self._progressive_remaining_cursor = 0
        if run_index is not None and self._progressive_is_active(run_index):
            # An explicit new selection gets a page at the next boundary even
            # if this run already consumed its current round quantum.  This is
            # the sole interaction-driven escape from autonomous 10 ms pacing.
            self._mark_progressive_unserved(run_index)
            self._clear_progressive_pacing()
        if (
            run_index is not None
            and self._scheduler.state_for(run_index, TrustedWorkKind.METADATA)
            is TrustedWorkState.COMPLETED
        ):
            run = self._runs[run_index]
            self._scheduler.request_completed_work(
                run_index,
                TrustedWorkKind.METADATA,
                database_instance=self._scheduler.database_instance,
                generation=self._scheduler.generation,
                run_guid=run.run_guid,
            )

    def set_visible_range(self, start: int, stop: int) -> None:
        self._require_owner()
        prior_visible = self._scheduler.snapshot().visible_indices
        self._scheduler.set_visible_range(start, stop)
        self._viewport_changed(
            prior_visible, self._scheduler.snapshot().visible_indices
        )
        self._pump()

    def set_visible_indices(self, indices: Sequence[int]) -> None:
        self._require_owner()
        prior_visible = self._scheduler.snapshot().visible_indices
        self._scheduler.set_visible_indices(indices)
        self._viewport_changed(
            prior_visible, self._scheduler.snapshot().visible_indices
        )
        self._pump()

    def _viewport_changed(
        self,
        prior_visible: Sequence[int],
        visible_indices: Sequence[int],
    ) -> None:
        """Make newly visible continuations eligible at the next boundary."""

        prior = frozenset(prior_visible)
        current = frozenset(visible_indices)
        if current == prior:
            return
        self._progressive_remaining_cursor = 0
        promoted = False
        for run_index in current - prior:
            if self._progressive_is_active(run_index):
                self._mark_progressive_unserved(run_index)
                promoted = True
        if promoted:
            self._clear_progressive_pacing()

    def reconcile_runs(
        self,
        runs: Sequence[TrustedDerivedRun],
        *,
        priority: tuple[int | None, Sequence[int]] | None = None,
    ) -> None:
        """Append runs and optionally adopt one atomic post-append priority."""

        self._require_owner()
        updated = self._validated_runs(runs)
        old_count = len(self._runs)
        if len(updated) < old_count or updated[:old_count] != self._runs:
            raise ValueError("Coordinator run reconciliation must be append-only.")
        prior_visible = self._scheduler.snapshot().visible_indices
        self._runs = updated
        self._run_indices_by_guid = {
            run.run_guid: index for index, run in enumerate(updated)
        }
        self._progressive_state.extend(
            b"\0" * (len(updated) - len(self._progressive_state))
        )
        retry_additions = (len(updated) - old_count) * _RETRY_KIND_COUNT
        if retry_additions:
            self._retry_attempts.extend(b"\0" * retry_additions)
            self._retry_deadlines.extend([0.0] * retry_additions)
        self._scheduler.reconcile_runs(tuple(run.scheduler_source() for run in updated))
        if priority is not None:
            selected_index, visible_indices = priority
            selection_changed = selected_index != self._selected_index
            self._scheduler.set_priority(selected_index, visible_indices)
            if selection_changed:
                self._selection_changed(selected_index)
            self._viewport_changed(
                prior_visible,
                self._scheduler.snapshot().visible_indices,
            )
        self._pump()

    def source_changed(self, run_index: int) -> None:
        """Coalesce appends to active work; publish its prefix before refreshing."""

        self._require_owner()
        self._require_open()
        if not 0 <= run_index < len(self._runs):
            raise IndexError("run_index is outside the stable run table.")
        if run_index == self._selected_index:
            self._invalidate_selected_detail()
        if self._active is not None and self._active.work.run_index == run_index:
            self._deferred_invalidations.add(run_index)
            return
        self._invalidate_now(run_index, preserve_all_retries=True)
        self._pump()

    def update_format(self, kind: TrustedWorkKind, work_format: WorkFormat) -> None:
        self._require_owner()
        self._scheduler.update_format(kind, work_format)
        self._clear_retry_kind(kind)
        self._memory_payload = None
        self._pump()

    def request_completed_work(
        self,
        run_index: int,
        kind: TrustedWorkKind,
        *,
        database_instance: DatabaseInstance,
        generation: int,
        run_guid: str,
        prioritize: bool = False,
    ) -> bool:
        """Replay one exact completed item, normally from the disk cache."""

        self._require_owner()
        self._require_open()
        accepted = self._scheduler.request_completed_work(
            run_index,
            kind,
            database_instance=database_instance,
            generation=generation,
            run_guid=run_guid,
        )
        if accepted:
            if prioritize:
                self._scheduler.select_run(run_index)
            self._memory_payload = None
            self._pump()
        return accepted

    def switch_database(
        self,
        database_instance: DatabaseInstance,
        runs: Sequence[TrustedDerivedRun],
        service: TrustedLiveReadService,
        *,
        own_service: bool | None = None,
        priority: tuple[int | None, Sequence[int]] | None = None,
        defer_start: bool = False,
    ) -> None:
        self._require_owner()
        self._require_open()
        if service.database_instance != database_instance:
            raise ValueError("The replacement service is bound to another database.")
        prior_service = self._service
        prior_owned = self._own_service
        updated = self._validated_runs(runs)
        self._cancel_selected_detail_attempt()
        self._selection_generation += 1
        self._helper_incarnation += 1
        self._selected_index = None
        self._accepted_selected_detail = None
        self._runs = updated
        self._run_indices_by_guid = {
            run.run_guid: index for index, run in enumerate(updated)
        }
        self._service = service
        if own_service is not None:
            self._own_service = bool(own_service)
        self._clear_reused_observations()
        self._progressive_state = bytearray(len(updated))
        self._progressive_round_epoch = False
        self._progressive_active_count = 0
        self._progressive_remaining_cursor = 0
        self._progressive_restart_deferred = False
        self._progressive_not_before = 0.0
        self._progressive_notifier.cancel()
        self._foreground_claims_since_background_metadata = 0
        self._age_progressive_background_next = True
        self._ordinary_claims_since_progressive_page = 0
        self._memory_payload = None
        self._deferred_invalidations.clear()
        retry_slot_count = len(updated) * _RETRY_KIND_COUNT
        self._retry_attempts = bytearray(retry_slot_count)
        self._retry_deadlines = array("d", [0.0]) * retry_slot_count
        self._retry_notifier.cancel()
        self._configure_cache(database_instance)
        self._scheduler.switch_database(
            database_instance,
            tuple(run.scheduler_source() for run in updated),
        )
        if priority is not None:
            selected_index, visible_indices = priority
            self._scheduler.set_priority(selected_index, visible_indices)
            self._selection_changed(selected_index)
        if prior_owned and prior_service is not service:
            prior_service.close_async()
        if not defer_start:
            self._pump()

    def helper_restarted(self) -> None:
        """Invalidate every result after a helper-incarnation boundary."""

        self._require_owner()
        self._require_open()
        self._cancel_selected_detail_attempt()
        self._selection_generation += 1
        self._helper_incarnation += 1
        self._accepted_selected_detail = None
        selected_index = self._selected_index
        visible_indices = self._scheduler.snapshot().visible_indices
        replacements = tuple(
            TrustedDerivedRun(
                run.run_id,
                run.run_guid,
                self._invalidation_revision(index),
            )
            for index, run in enumerate(self._runs)
        )
        self._runs = replacements
        self._clear_reused_observations()
        self._progressive_state = bytearray(len(replacements))
        self._progressive_round_epoch = False
        self._progressive_active_count = 0
        self._progressive_remaining_cursor = 0
        self._progressive_restart_deferred = False
        self._progressive_not_before = 0.0
        self._progressive_notifier.cancel()
        self._foreground_claims_since_background_metadata = 0
        self._age_progressive_background_next = True
        self._ordinary_claims_since_progressive_page = 0
        self._memory_payload = None
        self._deferred_invalidations.clear()
        retry_slot_count = len(replacements) * _RETRY_KIND_COUNT
        self._retry_attempts = bytearray(retry_slot_count)
        self._retry_deadlines = array("d", [0.0]) * retry_slot_count
        self._retry_notifier.cancel()
        self._scheduler.switch_database(
            self._scheduler.database_instance,
            tuple(run.scheduler_source() for run in replacements),
        )
        self._scheduler.set_priority(selected_index, visible_indices)
        self._pump()

    def poll(self) -> int:
        """Marshal at most one worker completion onto the scheduler owner thread.

        A bounded owner turn is important for progressive layout inspection.  A
        worker can complete quickly enough for its callback to refill the queue
        while this method is running; draining until empty would then let one Qt
        event consume arbitrarily many pages and prevent a priority change from
        intervening.  Every completion schedules (or has already scheduled) a
        fresh wakeup, so one-at-a-time draining preserves throughput without
        relying on worker timing for GUI fairness.
        """

        self._require_owner()
        try:
            completion = self._completions.get_nowait()
        except queue.Empty:
            # A deferred progressive restart is admitted only from a fresh
            # owner event (or an explicit owner action that calls ``_pump``).
            self._pump(allow_progressive_restart=True)
            return 0

        active = self._active
        if active is not None and active.work is completion.work:
            self._active = None
            if self._scheduler.is_current_claim(completion.work):
                if isinstance(completion, _ExecutionFailure):
                    self._handle_failure(completion)
                elif isinstance(completion, _ProgressiveExecutionResult):
                    self._handle_progressive(completion)
                else:
                    self._handle_success(completion)
        # Ordinary work may continue immediately, but reaching the end of a
        # progressive pass schedules a separate owner turn before another page.
        self._pump(allow_progressive_restart=False)
        return 1

    def close(self, *, timeout: float = 30.0) -> None:
        """Cancel work, optionally retire the service, and join the sole worker."""

        self._require_owner()
        if not 0 <= timeout <= 300:
            raise ValueError("timeout must be from zero through 300 seconds.")
        self.close_async()
        if not self.wait_closed(timeout):
            raise TimeoutError(
                "The trusted derived worker did not stop within its deadline."
            )

    def close_async(self) -> None:
        """Promptly cancel scheduling without waiting on the owner/GUI thread."""

        self._require_owner()
        if self._closed:
            return
        self._closed = True
        self._cancel_selected_detail_attempt()
        self._selection_generation += 1
        self._selected_index = None
        self._accepted_selected_detail = None
        self._retry_attempts = bytearray(len(self._retry_attempts))
        self._retry_deadlines = array("d", [0.0]) * len(self._retry_deadlines)
        self._retry_notifier.cancel()
        self._progressive_notifier.cancel()
        self._scheduler.close()
        if self._own_service:
            self._service.close_async()
        active = self._active
        if active is not None:
            active.work.cancellation.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._clear_reused_observations()
        self._progressive_state = bytearray(len(self._runs))
        self._progressive_round_epoch = False
        self._progressive_active_count = 0
        self._progressive_remaining_cursor = 0
        self._progressive_restart_deferred = False
        self._progressive_not_before = 0.0
        self._foreground_claims_since_background_metadata = 0
        self._ordinary_claims_since_progressive_page = 0
        self._memory_payload = None

    def wait_closed(self, timeout: float = 0.0) -> bool:
        """Wait for the already-cancelled worker under one explicit bound."""

        self._require_owner()
        if not 0 <= timeout <= 300:
            raise ValueError("timeout must be from zero through 300 seconds.")
        self.close_async()
        deadline = time.monotonic() + timeout
        active = self._active
        if active is not None:
            try:
                active.future.result(timeout=max(0.0, deadline - time.monotonic()))
            except FutureTimeout:
                return False
            except BaseException:
                pass
            self._active = None
        while True:
            try:
                self._completions.get_nowait()
            except queue.Empty:
                break
        if self._own_service and not self._service.wait_closed(
            max(0.0, deadline - time.monotonic())
        ):
            return False
        if not self._executor_joined:
            self._executor.shutdown(wait=True, cancel_futures=True)
            self._executor_joined = True
        return True

    def _pump(self, *, allow_progressive_restart: bool = True) -> None:
        if self._closed or self._active is not None:
            return

        snapshot = self._scheduler.snapshot()
        now = time.monotonic()
        retry_deadline = self._reschedule_retry_notifier(now)
        retry_filter_active = retry_deadline is not None
        next_priority = (
            self._scheduler.next_pending_priority(
                admissible=lambda run_index, kind: self._retry_work_is_admissible(
                    run_index,
                    kind,
                    now,
                )
            )
            if retry_filter_active
            else snapshot.next_priority_key
        )
        selected_index = snapshot.selected_index
        selected_image_ready = bool(
            next_priority is not None
            and TrustedRunTier(next_priority[0]) is TrustedRunTier.SELECTED
            and TrustedWorkKind(next_priority[1]) is not TrustedWorkKind.METADATA
        ) or bool(
            selected_index is not None
            and self._scheduler.metadata_is_conclusive(selected_index)
            and any(
                self._scheduler.state_for(selected_index, kind)
                is TrustedWorkState.PENDING
                and self._retry_slot_is_ready(selected_index, kind, now)
                for kind in (TrustedWorkKind.THUMBNAIL, TrustedWorkKind.PREVIEW)
            )
        )
        age_background_due = (
            self._foreground_claims_since_background_metadata
            >= TRUSTED_DERIVED_BACKGROUND_AGING_CLAIM_BOUND
            and not selected_image_ready
        )
        progressive_index = self._peek_progressive_run_index(
            snapshot,
            admissible=lambda run_index: self._retry_slot_is_ready(
                run_index,
                TrustedWorkKind.METADATA,
                now,
            ),
        )
        progressive_precedes = False
        if progressive_index is not None:
            progressive_tier = self._tier_for_snapshot(snapshot, progressive_index)
            base_is_progressive = bool(
                next_priority is not None
                and TrustedWorkKind(next_priority[1]) is TrustedWorkKind.METADATA
                and self._progressive_is_active(next_priority[2])
            )
            if next_priority is None:
                progressive_precedes = True
            else:
                # Equal-tier ordinary work wins, but an abandoned progressive
                # slot receives one page after at most one sibling's M/T/P
                # sequence.  This keeps ready images prompt while preventing
                # a stream of newly appended same-tier runs from starving an
                # older continuation forever.
                progressive_precedes = (
                    base_is_progressive
                    or (progressive_tier < TrustedRunTier(next_priority[0]))
                    or (
                        progressive_tier is TrustedRunTier(next_priority[0])
                        and self._ordinary_claims_since_progressive_page
                        >= TRUSTED_DERIVED_SAME_TIER_PROGRESSIVE_CLAIM_BOUND
                    )
                )

        age_ordinary_metadata = False
        ordinary_remaining_index: int | None = None
        if age_background_due:
            ordinary_remaining_index = (
                self._scheduler.next_pending_remaining_metadata_index(
                    admissible=lambda run_index: (
                        not self._progressive_is_active(run_index)
                        and self._retry_slot_is_ready(
                            run_index,
                            TrustedWorkKind.METADATA,
                            now,
                        )
                    ),
                )
            )
            remaining_progressive_index = self._peek_progressive_run_index(
                snapshot,
                required_tier=TrustedRunTier.REMAINING,
                admissible=lambda run_index: self._retry_slot_is_ready(
                    run_index,
                    TrustedWorkKind.METADATA,
                    now,
                ),
            )
            choose_progressive = remaining_progressive_index is not None and (
                ordinary_remaining_index is None
                or self._age_progressive_background_next
            )
            if choose_progressive:
                progressive_index = remaining_progressive_index
                progressive_precedes = True
            elif ordinary_remaining_index is not None:
                progressive_precedes = False
                age_ordinary_metadata = True

        work: ScheduledWork | None = None
        if progressive_precedes:
            if not allow_progressive_restart or now < self._progressive_not_before:
                self._defer_progressive_restart(now)
                return
            assert progressive_index is not None
            run = self._runs[progressive_index]
            self._scheduler.request_completed_work(
                progressive_index,
                TrustedWorkKind.METADATA,
                database_instance=self._scheduler.database_instance,
                generation=self._scheduler.generation,
                run_guid=run.run_guid,
            )
            work = self._scheduler.claim_pending(
                progressive_index,
                TrustedWorkKind.METADATA,
            )
            if work is None:
                if (
                    self._scheduler.state_for(
                        progressive_index,
                        TrustedWorkKind.METADATA,
                    )
                    is TrustedWorkState.COMPLETED
                ):
                    self._discard_progressive_guid(run.run_guid)
                return
            self._progressive_restart_deferred = False
            self._progressive_not_before = 0.0
            self._progressive_notifier.cancel()
        if work is None and age_ordinary_metadata:
            assert ordinary_remaining_index is not None
            work = self._scheduler.claim_pending(
                ordinary_remaining_index,
                TrustedWorkKind.METADATA,
            )
        if work is None and not age_ordinary_metadata:
            if retry_filter_active:
                if next_priority is not None:
                    work = self._scheduler.claim_pending(
                        next_priority[2],
                        TrustedWorkKind(next_priority[1]),
                    )
            else:
                work = self._scheduler.claim_next(
                    age_remaining_metadata=False,
                )
        if work is None:
            if self._progressive_active_count == 0:
                self._clear_progressive_pacing()
            return
        run = self._runs[work.run_index]
        progressive_refresh = (
            work.key.kind is TrustedWorkKind.METADATA
            and self._progressive_is_active(work.run_index)
        )
        if progressive_refresh:
            self._mark_progressive_served(work.run_index, snapshot)
        self._record_claim_admission(work)
        memory_payload = None
        observation = None
        if not progressive_refresh:
            memory_payload = self._memory_payload
            if memory_payload is not None and memory_payload[0] == work.key:
                self._memory_payload = None
            else:
                memory_payload = None
            observation = self._reused_observation_for(work)
        selected_detail_attempt = self._selected_detail_attempt_for(work, run)
        deadline = time.monotonic() + self._deadline_seconds
        future = self._executor.submit(
            self._execute,
            work,
            run,
            self._service,
            observation,
            memory_payload,
            selected_detail_attempt,
            deadline,
            progressive_refresh,
        )
        self._active = _ActiveClaim(
            work,
            future,
            bytes(self._service.source_revision_namespace.nonce),
            deadline,
            selected_detail_attempt,
        )
        future.add_done_callback(self._worker_done)

    def _defer_progressive_restart(self, now: float) -> None:
        self._progressive_restart_deferred = True
        if self._progressive_not_before <= now:
            self._progressive_not_before = (
                now + TRUSTED_DERIVED_PROGRESSIVE_MIN_INTERVAL_SECONDS
            )
        self._progressive_notifier.schedule(self._progressive_not_before)

    def _clear_progressive_pacing(self) -> None:
        self._progressive_restart_deferred = False
        self._progressive_not_before = 0.0
        self._progressive_notifier.cancel()

    def _record_claim_admission(self, work: ScheduledWork) -> None:
        tier = TrustedRunTier(work.priority_key[0])
        if work.key.kind is TrustedWorkKind.METADATA and self._progressive_is_active(
            work.run_index
        ):
            self._ordinary_claims_since_progressive_page = 0
        else:
            self._ordinary_claims_since_progressive_page = min(
                TRUSTED_DERIVED_SAME_TIER_PROGRESSIVE_CLAIM_BOUND,
                self._ordinary_claims_since_progressive_page + 1,
            )
        if tier is TrustedRunTier.REMAINING and (
            work.key.kind is TrustedWorkKind.METADATA
        ):
            self._foreground_claims_since_background_metadata = 0
            self._age_progressive_background_next = not self._progressive_is_active(
                work.run_index
            )
        elif tier is not TrustedRunTier.REMAINING:
            self._foreground_claims_since_background_metadata = min(
                TRUSTED_DERIVED_BACKGROUND_AGING_CLAIM_BOUND,
                self._foreground_claims_since_background_metadata + 1,
            )

    def _retry_offset(self, run_index: int, kind: TrustedWorkKind) -> int:
        return run_index * _RETRY_KIND_COUNT + int(kind)

    def _retry_offset_for_slot(
        self,
        slot: tuple[str, TrustedWorkKind],
    ) -> int | None:
        run_index = self._run_indices_by_guid.get(slot[0])
        if run_index is None:
            return None
        return self._retry_offset(run_index, slot[1])

    def _retry_slot_for_offset(
        self,
        offset: int,
    ) -> tuple[str, TrustedWorkKind] | None:
        run_index, kind_value = divmod(offset, _RETRY_KIND_COUNT)
        if not 0 <= run_index < len(self._runs):
            return None
        return self._runs[run_index].run_guid, TrustedWorkKind(kind_value)

    @property
    def _retry_not_before(self) -> float:
        """Earliest retained slot deadline, exposed for bounded diagnostics."""

        return min(
            (deadline for deadline in self._retry_deadlines if deadline), default=0.0
        )

    @property
    def _retry_blocked_slot(self) -> tuple[str, TrustedWorkKind] | None:
        """Earliest retained retry slot, exposed for deterministic diagnostics."""

        candidate = min(
            (
                (deadline, offset)
                for offset, deadline in enumerate(self._retry_deadlines)
                if deadline
            ),
            default=None,
        )
        if candidate is None:
            return None
        return self._retry_slot_for_offset(candidate[1])

    def _retry_slot_is_ready(
        self,
        run_index: int,
        kind: TrustedWorkKind,
        now: float,
    ) -> bool:
        return self._retry_deadlines[self._retry_offset(run_index, kind)] <= now

    def _retry_work_is_admissible(
        self,
        run_index: int,
        kind: TrustedWorkKind,
        now: float,
    ) -> bool:
        if not self._retry_slot_is_ready(run_index, kind, now):
            return False
        return kind is TrustedWorkKind.METADATA or (
            self._scheduler.metadata_is_conclusive(run_index)
        )

    def _reschedule_retry_notifier(self, now: float) -> float | None:
        deadline = min(
            (candidate for candidate in self._retry_deadlines if candidate > now),
            default=None,
        )
        if deadline is None:
            self._retry_notifier.cancel()
        else:
            self._retry_notifier.schedule(deadline)
        return deadline

    @staticmethod
    def _tier_for_snapshot(
        snapshot: SchedulerSnapshot,
        run_index: int,
    ) -> TrustedRunTier:
        if run_index == snapshot.selected_index:
            return TrustedRunTier.SELECTED
        if run_index in snapshot.visible_indices:
            return TrustedRunTier.VISIBLE
        return TrustedRunTier.REMAINING

    def _peek_progressive_run_index(
        self,
        snapshot: SchedulerSnapshot,
        *,
        required_tier: TrustedRunTier | None = None,
        exclude_index: int | None = None,
        admissible: Callable[[int], bool] | None = None,
    ) -> int | None:
        candidate = self._peek_progressive_current_round(
            snapshot,
            required_tier,
            exclude_index,
            admissible,
        )
        if (
            candidate is None
            and required_tier is None
            and self._progressive_active_count
        ):
            self._progressive_round_epoch = not self._progressive_round_epoch
            self._progressive_remaining_cursor = 0
            candidate = self._peek_progressive_current_round(
                snapshot,
                None,
                exclude_index,
                admissible,
            )
        return candidate

    def _peek_progressive_current_round(
        self,
        snapshot: SchedulerSnapshot,
        required_tier: TrustedRunTier | None,
        exclude_index: int | None,
        admissible: Callable[[int], bool] | None,
    ) -> int | None:
        selected = snapshot.selected_index
        if (
            required_tier in {None, TrustedRunTier.SELECTED}
            and selected is not None
            and selected != exclude_index
            and (admissible is None or admissible(selected))
            and self._progressive_is_unserved(selected)
        ):
            return selected
        if required_tier in {None, TrustedRunTier.VISIBLE}:
            for run_index in snapshot.visible_indices:
                if (
                    run_index != selected
                    and run_index != exclude_index
                    and (admissible is None or admissible(run_index))
                    and self._progressive_is_unserved(run_index)
                ):
                    return run_index
        if required_tier not in {None, TrustedRunTier.REMAINING}:
            return None
        target = _PROGRESSIVE_ACTIVE_BIT | (
            0 if self._progressive_round_epoch else _PROGRESSIVE_SERVED_BIT
        )
        needle = bytes((target,))
        visible = frozenset(snapshot.visible_indices)
        for start, stop in (
            (self._progressive_remaining_cursor, len(self._progressive_state)),
            (0, self._progressive_remaining_cursor),
        ):
            run_index = self._progressive_state.find(needle, start, stop)
            while run_index >= 0:
                if (
                    run_index != selected
                    and run_index != exclude_index
                    and run_index not in visible
                    and (admissible is None or admissible(run_index))
                ):
                    return run_index
                run_index = self._progressive_state.find(
                    needle,
                    run_index + 1,
                    stop,
                )
        return None

    def _next_progressive_run_index(
        self,
        snapshot: SchedulerSnapshot,
    ) -> int | None:
        """Choose one page from a selected/visible/remaining fair round."""

        run_index = self._peek_progressive_run_index(snapshot)
        if run_index is None:
            return None
        self._mark_progressive_served(run_index, snapshot)
        return run_index

    def _execute(
        self,
        work: ScheduledWork,
        run: TrustedDerivedRun,
        service: TrustedLiveReadService,
        reused: _RetainedObservation | None,
        memory_payload: tuple[TrustedCacheWorkKey, DerivedPayload] | None,
        selected_detail_attempt: _SelectedDetailAttempt | None,
        deadline: float,
        progressive_refresh: bool,
    ) -> _WorkerResult:
        request = None
        try:

            def cancel_check() -> None:
                work.cancellation.raise_if_cancelled()
                if time.monotonic() >= deadline:
                    raise _JobDeadlineExceeded(
                        "The trusted derived job exceeded its absolute deadline."
                    )

            def completed(
                key: TrustedCacheWorkKey,
                payload: DerivedPayload,
                observation: TrustedDerivedSourceObservation | None,
                observation_revision: TrustedSourceRevision | None,
                observation_size: int,
                *,
                cache_hit: bool,
            ) -> _ExecutionResult:
                effective_attempt, selected_detail = selected_detail_for(key)
                return _ExecutionResult(
                    work,
                    key,
                    payload,
                    observation,
                    observation_revision,
                    observation_size,
                    cache_hit,
                    effective_attempt,
                    selected_detail,
                )

            def selected_detail_for(
                key: TrustedCacheWorkKey,
            ) -> tuple[
                _SelectedDetailAttempt | None,
                TrustedSelectedRunDetail | None,
            ]:
                selected_detail = None
                effective_attempt = selected_detail_attempt
                if effective_attempt is not None:
                    if effective_attempt.source_revision != key.source_revision:
                        # The first bounded source read commonly refines a
                        # provisional or progressive revision.  Fence the
                        # attached ephemeral detail to that accepted revision,
                        # while sharing the original cancellation event held by
                        # the active claim.
                        effective_attempt = _SelectedDetailAttempt(
                            effective_attempt.database_instance,
                            effective_attempt.generation,
                            key.source_revision,
                            effective_attempt.helper_incarnation,
                            effective_attempt.selection_generation,
                            effective_attempt.run_index,
                            effective_attempt.run_id,
                            effective_attempt.run_guid,
                            effective_attempt.cancelled,
                        )
                    selected_detail = self._read_selected_detail(
                        effective_attempt, service, deadline, cancel_check
                    )
                return effective_attempt, selected_detail

            def progressive(
                key: TrustedCacheWorkKey,
                observation: TrustedDerivedSourceObservation,
                observation_revision: TrustedSourceRevision,
                observation_size: int,
            ) -> _ProgressiveExecutionResult:
                effective_attempt, selected_detail = selected_detail_for(key)
                return _ProgressiveExecutionResult(
                    work,
                    key,
                    observation,
                    observation_revision,
                    observation_size,
                    effective_attempt,
                    selected_detail,
                )

            cancel_check()
            observation: TrustedDerivedSourceObservation | None = None
            observation_revision: TrustedSourceRevision | None = None
            observation_size = 0
            if progressive_refresh and work.key.kind is not TrustedWorkKind.METADATA:
                raise RuntimeError("Only metadata work may refresh a progressive page.")
            if not progressive_refresh and reused is not None:
                observation, observation_revision, observation_size = reused
            if not progressive_refresh and memory_payload is not None:
                cancel_check()
                return completed(
                    work.key,
                    memory_payload[1],
                    observation,
                    observation_revision,
                    observation_size,
                    cache_hit=True,
                )
            if not progressive_refresh:
                cached = self._cache.get(work.key, cancel_check=cancel_check)
                if cached is not None:
                    cancel_check()
                    return completed(
                        work.key,
                        cached,
                        observation,
                        observation_revision,
                        observation_size,
                        cache_hit=True,
                    )

            if (
                observation is None
                or observation.database_instance != work.key.database_instance
                or observation.run_guid != run.run_guid
                or observation_revision != work.key.source_revision
            ):
                request = service.submit_derived_source(
                    run.run_id,
                    deadline=deadline,
                )
                while not request.done:
                    try:
                        cancel_check()
                    except BaseException:
                        request.cancel()
                        raise
                    time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
                observation = request.wait(0)
                observation_revision = None
                observation_size = 0
            cancel_check()
            if (
                observation.database_instance != work.key.database_instance
                or observation.run_id != run.run_id
                or observation.run_guid != run.run_guid
                or observation.service_namespace
                != service.source_revision_namespace.nonce
            ):
                raise RuntimeError("A stale trusted source observation was rejected.")
            if observation_revision is None:
                observation_revision = trusted_derived_source_revision(observation)
                observation_size = self._observation_size(observation)
            revision = observation_revision
            actual_key = TrustedCacheWorkKey(
                work.key.database_instance,
                work.key.run_guid,
                work.key.kind,
                revision,
                work.key.renderer_version,
                work.key.rendering_options,
            )
            if bool(observation.progressive_layout_pending):
                if work.key.kind is not TrustedWorkKind.METADATA:
                    raise RuntimeError(
                        "An inconclusive layout reached non-metadata derived work."
                    )
                cancel_check()
                return progressive(
                    actual_key,
                    observation,
                    observation_revision,
                    observation_size,
                )
            cached = self._cache.get(actual_key, cancel_check=cancel_check)
            if cached is not None:
                cancel_check()
                return completed(
                    actual_key,
                    cached,
                    observation,
                    observation_revision,
                    observation_size,
                    cache_hit=True,
                )
            payload = render_trusted_derived_payload(
                observation,
                work.key.kind,
                work.key.rendering_options,
                cancel_check=cancel_check,
            )
            validate_trusted_derived_payload(payload)
            cancel_check()
            self._cache.put(actual_key, payload, cancel_check=cancel_check)
            cancel_check()
            return completed(
                actual_key,
                payload,
                observation,
                observation_revision,
                observation_size,
                cache_hit=False,
            )
        except BaseException as error:
            if request is not None and not request.done:
                request.cancel()
            return _ExecutionFailure(work, self._error_record(error))

    def _selected_detail_attempt_for(
        self,
        work: ScheduledWork,
        run: TrustedDerivedRun,
    ) -> _SelectedDetailAttempt | None:
        if (
            work.key.kind is not TrustedWorkKind.METADATA
            or work.run_index != self._selected_index
        ):
            return None
        marker = (
            self._selection_generation,
            self._helper_incarnation,
            work.run_index,
            run.run_id,
            run.run_guid,
        )
        accepted = self._accepted_selected_detail
        if accepted is not None and marker == accepted[:5]:
            return None
        return _SelectedDetailAttempt(
            work.key.database_instance,
            work.generation,
            work.key.source_revision,
            self._helper_incarnation,
            self._selection_generation,
            work.run_index,
            run.run_id,
            run.run_guid,
            threading.Event(),
        )

    @staticmethod
    def _read_selected_detail(
        attempt: _SelectedDetailAttempt,
        service: TrustedLiveReadService,
        deadline: float,
        cancel_check: Callable[[], None],
    ) -> TrustedSelectedRunDetail | None:
        request = None

        def selected_cancel_check() -> None:
            cancel_check()
            if attempt.cancelled.is_set():
                raise InterruptedError("The selected-detail request was cancelled.")

        try:
            selected_cancel_check()
            request = service.submit_selected_run(
                attempt.run_id,
                deadline=deadline,
            )
            identity = getattr(request, "identity", None)
            if (
                identity is not None
                and getattr(identity, "database_instance", None)
                != attempt.database_instance
            ):
                request.cancel()
                return None
            while not request.done:
                try:
                    selected_cancel_check()
                except Exception:
                    request.cancel()
                    return None
                time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
            detail = request.wait(0)
            selected_cancel_check()
            if not isinstance(detail, TrustedSelectedRunDetail):
                return None
            if detail.run.run_id != attempt.run_id:
                return None
            fields = detail.run.as_dict()
            if str(fields.get("guid") or "") != attempt.run_guid:
                return None
            return detail
        except Exception:
            if request is not None and not request.done:
                request.cancel()
            return None

    def _cancel_selected_detail_attempt(self) -> None:
        active = self._active
        if active is not None and active.selected_detail_attempt is not None:
            active.selected_detail_attempt.cancelled.set()

    def _invalidate_selected_detail(self) -> None:
        self._cancel_selected_detail_attempt()
        self._selection_generation += 1
        self._accepted_selected_detail = None

    def _worker_done(self, future: Future[_WorkerResult]) -> None:
        try:
            result = future.result()
        except BaseException as error:
            active = self._active
            if active is None:
                return
            result = _ExecutionFailure(active.work, self._error_record(error))
        try:
            self._completions.put_nowait(result)
        except queue.Full:
            return
        if self._wakeup is not None:
            self._wakeup()

    def _handle_progressive(self, result: _ProgressiveExecutionResult) -> None:
        """Park one inconclusive metadata page without rendering or caching it."""

        slot = (result.work.key.run_guid, result.work.key.kind)
        self._clear_retry_backoff(slot)
        disposition = self._scheduler.park_progressive_metadata(
            result.work,
            result.key.source_revision,
        )
        if disposition is CompletionDisposition.STALE:
            return
        self._adopt_observation(
            result.observation,
            result.observation_revision,
            result.observation_size,
        )
        self._ordinary_claims_since_progressive_page = 0
        self._runs = tuple(
            TrustedDerivedRun(
                run.run_id,
                run.run_guid,
                (
                    result.key.source_revision
                    if index == result.work.run_index
                    else run.source_revision
                ),
            )
            for index, run in enumerate(self._runs)
        )
        self._publish_selected_detail(result)
        self._process_deferred(result.work, disposition, terminal=False)
        self._replay_selected_metadata_for_detail(result)

    def _handle_success(self, result: _ExecutionResult) -> None:
        slot = (result.work.key.run_guid, result.work.key.kind)
        self._clear_retry_backoff(slot)
        if (
            result.work.key.kind is TrustedWorkKind.METADATA
            and self._scheduler.unpark_progressive_metadata(result.work)
            is CompletionDisposition.STALE
        ):
            return
        if result.key != result.work.key:
            disposition = self._scheduler.refine_claim_source_revision(
                result.work,
                result.key.source_revision,
            )
            if disposition is CompletionDisposition.STALE:
                return
            self._adopt_observation(
                result.observation,
                result.observation_revision,
                result.observation_size,
            )
            self._memory_payload = (result.key, result.payload)
            self._runs = tuple(
                TrustedDerivedRun(
                    run.run_id,
                    run.run_guid,
                    (
                        result.key.source_revision
                        if index == result.work.run_index
                        else run.source_revision
                    ),
                )
                for index, run in enumerate(self._runs)
            )
            self._process_deferred(result.work, disposition, terminal=False)
            return
        self._adopt_observation(
            result.observation,
            result.observation_revision,
            result.observation_size,
        )
        disposition = self._scheduler.complete(result.work, result.payload)
        if disposition is CompletionDisposition.ACCEPTED:
            self._publish_selected_detail(result)
        self._process_deferred(result.work, disposition, terminal=True)
        if disposition is CompletionDisposition.ACCEPTED:
            self._replay_selected_metadata_for_detail(result)

    def _replay_selected_metadata_for_detail(
        self,
        result: _SuccessfulResult,
    ) -> None:
        """Replay metadata for a missing or terminally stale selected detail."""

        run_index = result.work.run_index
        if (
            result.work.key.kind is not TrustedWorkKind.METADATA
            or run_index != self._selected_index
            or not 0 <= run_index < len(self._runs)
        ):
            return
        run = self._runs[run_index]
        marker = (
            self._selection_generation,
            self._helper_incarnation,
            run_index,
            run.run_id,
            run.run_guid,
        )
        accepted = self._accepted_selected_detail
        if accepted is not None and marker == accepted[:5]:
            if accepted[5] == run.source_revision:
                return
            # Progressive metadata pages refine their revision independently,
            # but must not amplify selected-run Snapshot reads. Once metadata
            # is terminal, release the older accepted identity and replay the
            # completed payload exactly once to obtain detail fenced to the
            # final revision.
            if (
                self._scheduler.state_for(run_index, TrustedWorkKind.METADATA)
                is not TrustedWorkState.COMPLETED
            ):
                return
            self._accepted_selected_detail = None
        attempt = result.selected_detail_attempt
        if (
            attempt is not None
            and not attempt.cancelled.is_set()
            and marker
            == (
                attempt.selection_generation,
                attempt.helper_incarnation,
                attempt.run_index,
                attempt.run_id,
                attempt.run_guid,
            )
        ):
            return
        if (
            self._scheduler.state_for(run_index, TrustedWorkKind.METADATA)
            is not TrustedWorkState.COMPLETED
        ):
            return
        self._scheduler.request_completed_work(
            run_index,
            TrustedWorkKind.METADATA,
            database_instance=self._scheduler.database_instance,
            generation=self._scheduler.generation,
            run_guid=run.run_guid,
        )

    def _publish_selected_detail(self, result: _SuccessfulResult) -> None:
        attempt = result.selected_detail_attempt
        detail = result.selected_detail
        if attempt is None or detail is None or attempt.cancelled.is_set():
            return
        marker = (
            attempt.selection_generation,
            attempt.helper_incarnation,
            attempt.run_index,
            attempt.run_id,
            attempt.run_guid,
        )
        accepted = (*marker, result.key.source_revision)
        if (
            self._closed
            or accepted == self._accepted_selected_detail
            or attempt.database_instance != self._scheduler.database_instance
            or attempt.generation != self._scheduler.generation
            or attempt.source_revision != result.key.source_revision
            or attempt.helper_incarnation != self._helper_incarnation
            or attempt.selection_generation != self._selection_generation
            or attempt.run_index != self._selected_index
            or not 0 <= attempt.run_index < len(self._runs)
        ):
            return
        run = self._runs[attempt.run_index]
        if (
            run.run_id != attempt.run_id
            or run.run_guid != attempt.run_guid
            or run.source_revision != result.key.source_revision
            or result.work.run_index != attempt.run_index
            or result.work.key.kind is not TrustedWorkKind.METADATA
            or result.key.database_instance != attempt.database_instance
            or result.key.run_guid != attempt.run_guid
            or detail.run.run_id != attempt.run_id
            or str(detail.run.as_dict().get("guid") or "") != attempt.run_guid
        ):
            return
        self._accepted_selected_detail = accepted
        if self._on_selected_detail is not None:
            self._on_selected_detail(
                TrustedSelectedDetailPublication(
                    attempt.generation,
                    result.key,
                    attempt.run_index,
                    attempt.run_id,
                    attempt.run_guid,
                    attempt.helper_incarnation,
                    attempt.selection_generation,
                    detail,
                )
            )

    def _handle_failure(self, failure: _ExecutionFailure) -> None:
        slot = (failure.work.key.run_guid, failure.work.key.kind)
        if failure.error.category is TrustedDerivedErrorCategory.STALE:
            self._clear_retry_backoff(slot)
            disposition = self._scheduler.abandon(failure.work)
            self._process_deferred(failure.work, disposition, terminal=False)
            return
        if failure.error.category is TrustedDerivedErrorCategory.TRANSIENT:
            disposition = self._scheduler.abandon(failure.work)
            if disposition is CompletionDisposition.ACCEPTED:
                self._set_retry_backoff(slot, time.monotonic())
            self._process_deferred(
                failure.work,
                disposition,
                terminal=False,
                preserve_retry=True,
            )
            return
        self._clear_retry_backoff(slot)
        if self._on_error is not None:
            self._on_error(failure.work, failure.error)
        if failure.work.key.kind is TrustedWorkKind.METADATA:
            was_progressive = self._progressive_is_active(failure.work.run_index)
            self._discard_progressive_guid(failure.work.key.run_guid)
            if was_progressive:
                # The retained object is the last known-inconclusive page.  A
                # later explicit replay must ask the live service for the next
                # page, never re-park the stale retained page without progress.
                self._discard_reused_observation(failure.work.key.run_guid)
        # A bounded error description is a terminal uncached result for this
        # finite claim.  It prevents a permanent malformed run from spinning.
        payload: DerivedPayload = {
            "format": "qplot-trusted-derived-payload-v1",
            "kind": failure.work.key.kind.name.lower(),
            "status": "error",
            "description": failure.error.message,
            "source": (),
            "images": (),
        }
        disposition = self._scheduler.complete(failure.work, payload)
        self._process_deferred(failure.work, disposition, terminal=True)

    def _invalidate_now(
        self,
        run_index: int,
        *,
        preserve_retry_kind: TrustedWorkKind | None = None,
        preserve_all_retries: bool = False,
    ) -> None:
        revision = self._invalidation_revision(run_index)
        run = self._runs[run_index]
        updated = list(self._runs)
        updated[run_index] = TrustedDerivedRun(run.run_id, run.run_guid, revision)
        self._runs = tuple(updated)
        if not preserve_all_retries:
            self._clear_retry_for_run(
                run_index,
                preserve_kind=preserve_retry_kind,
            )
        self._discard_reused_observation(run.run_guid)
        # Preserve the one-bit continuation classification and its current
        # pacing deadline while discarding the stale page/revision.  Otherwise
        # repeated writer notifications turn each page back into zero-delay
        # ordinary metadata.
        if not self._progressive_is_active(run_index):
            self._discard_progressive_guid(run.run_guid)
        self._memory_payload = None
        self._scheduler.update_source_revision(run_index, revision)

    def _set_retry_backoff(
        self,
        slot: tuple[str, TrustedWorkKind],
        now: float,
    ) -> None:
        offset = self._retry_offset_for_slot(slot)
        if offset is None:
            return
        attempt = min(self._retry_attempts[offset] + 1, 8)
        self._retry_attempts[offset] = attempt
        self._retry_deadlines[offset] = now + min(
            1.0,
            0.025 * (2 ** (attempt - 1)),
        )
        self._reschedule_retry_notifier(now)

    def _clear_retry_backoff(
        self,
        slot: tuple[str, TrustedWorkKind],
    ) -> None:
        offset = self._retry_offset_for_slot(slot)
        if offset is None:
            return
        self._retry_attempts[offset] = 0
        self._retry_deadlines[offset] = 0.0
        self._reschedule_retry_notifier(time.monotonic())

    def _clear_retry_for_run(
        self,
        run_index: int,
        *,
        preserve_kind: TrustedWorkKind | None = None,
    ) -> None:
        for kind in TrustedWorkKind:
            if kind is preserve_kind:
                continue
            offset = self._retry_offset(run_index, kind)
            self._retry_attempts[offset] = 0
            self._retry_deadlines[offset] = 0.0
        self._reschedule_retry_notifier(time.monotonic())

    def _clear_retry_kind(self, kind: TrustedWorkKind) -> None:
        for run_index in range(len(self._runs)):
            offset = self._retry_offset(run_index, kind)
            self._retry_attempts[offset] = 0
            self._retry_deadlines[offset] = 0.0
        self._reschedule_retry_notifier(time.monotonic())

    def _invalidation_revision(self, run_index: int) -> TrustedSourceRevision:
        self._invalidation_serial += 1
        payload = repr(
            (
                "qplot-derived-invalidation-v1",
                self._service.source_revision_namespace.nonce,
                self._scheduler.generation,
                run_index,
                self._invalidation_serial,
            )
        ).encode("utf-8")
        return TrustedSourceRevision(hashlib.sha256(payload).digest())

    def _publish(self, publication: WorkPublication) -> None:
        if self._on_publish is not None:
            self._on_publish(publication)

    def _adopt_observation(
        self,
        observation: TrustedDerivedSourceObservation | None,
        revision: TrustedSourceRevision | None,
        size: int,
    ) -> None:
        if observation is None:
            return
        if not isinstance(revision, TrustedSourceRevision):
            raise TypeError("A retained observation requires its source revision.")
        if type(size) is not int or size < 0:
            raise ValueError("A retained observation requires a bounded size.")
        if bool(getattr(observation, "progressive_layout_pending", False)):
            run_index = self._run_indices_by_guid.get(observation.run_guid)
            if run_index is not None:
                self._mark_progressive_active(run_index)
        else:
            self._discard_progressive_guid(observation.run_guid)
        self._discard_reused_observation(observation.run_guid)
        if size > TRUSTED_DERIVED_MAX_REUSED_SOURCE_BYTES:
            return
        self._reused_observations[observation.run_guid] = (
            observation,
            revision,
            size,
        )
        self._reused_observation_bytes += size
        while (
            len(self._reused_observations) > TRUSTED_DERIVED_MAX_REUSED_SOURCES
            or self._reused_observation_bytes > TRUSTED_DERIVED_MAX_REUSED_SOURCE_BYTES
        ):
            (
                _guid,
                (
                    _discarded,
                    _discarded_revision,
                    discarded_size,
                ),
            ) = self._reused_observations.popitem(last=False)
            self._reused_observation_bytes -= discarded_size

    def _reused_observation_for(
        self,
        work: ScheduledWork,
    ) -> _RetainedObservation | None:
        retained = self._reused_observations.get(work.key.run_guid)
        if retained is None:
            return None
        observation, revision, _size = retained
        if (
            observation.database_instance != work.key.database_instance
            or revision != work.key.source_revision
        ):
            self._discard_reused_observation(work.key.run_guid)
            return None
        self._reused_observations.move_to_end(work.key.run_guid)
        return retained

    def _discard_reused_observation(self, run_guid: str) -> None:
        retained = self._reused_observations.pop(run_guid, None)
        if retained is not None:
            self._reused_observation_bytes -= retained[2]

    def _discard_progressive_guid(self, run_guid: str) -> None:
        run_index = self._run_indices_by_guid.get(run_guid)
        if run_index is None or not self._progressive_is_active(run_index):
            return
        self._progressive_state[run_index] = 0
        self._progressive_active_count -= 1
        self._progressive_remaining_cursor = min(
            self._progressive_remaining_cursor,
            run_index,
        )
        if self._progressive_active_count == 0:
            self._clear_progressive_pacing()

    def _progressive_is_active(self, run_index: int) -> bool:
        return bool(self._progressive_state[run_index] & _PROGRESSIVE_ACTIVE_BIT)

    def _progressive_is_unserved(self, run_index: int) -> bool:
        state = self._progressive_state[run_index]
        return bool(state & _PROGRESSIVE_ACTIVE_BIT) and (
            bool(state & _PROGRESSIVE_SERVED_BIT) != self._progressive_round_epoch
        )

    def _mark_progressive_active(self, run_index: int) -> None:
        if self._progressive_is_active(run_index):
            return
        self._progressive_active_count += 1
        self._mark_progressive_unserved(run_index)

    def _mark_progressive_unserved(self, run_index: int) -> None:
        served_marker = 0 if self._progressive_round_epoch else _PROGRESSIVE_SERVED_BIT
        self._progressive_state[run_index] = _PROGRESSIVE_ACTIVE_BIT | served_marker
        self._progressive_remaining_cursor = min(
            self._progressive_remaining_cursor,
            run_index,
        )

    def _mark_progressive_served(
        self,
        run_index: int,
        snapshot: SchedulerSnapshot,
    ) -> None:
        served_marker = _PROGRESSIVE_SERVED_BIT if self._progressive_round_epoch else 0
        self._progressive_state[run_index] = _PROGRESSIVE_ACTIVE_BIT | served_marker
        if self._tier_for_snapshot(snapshot, run_index) is TrustedRunTier.REMAINING:
            self._progressive_remaining_cursor = max(
                self._progressive_remaining_cursor,
                run_index + 1,
            )

    def _clear_reused_observations(self) -> None:
        self._reused_observations.clear()
        self._reused_observation_bytes = 0

    def _process_deferred(
        self,
        work: ScheduledWork,
        disposition: CompletionDisposition,
        *,
        terminal: bool,
        preserve_retry: bool = False,
    ) -> None:
        run_index = work.run_index
        if (
            disposition is CompletionDisposition.STALE
            or run_index not in self._deferred_invalidations
            or work.generation != self._scheduler.generation
            or work.key.database_instance != self._scheduler.database_instance
            or not 0 <= run_index < len(self._runs)
            or self._runs[run_index].run_guid != work.key.run_guid
        ):
            return
        completed_prefix = terminal and all(
            self._scheduler.state_for(run_index, kind) is TrustedWorkState.COMPLETED
            for kind in TrustedWorkKind
        )
        if not terminal or completed_prefix:
            self._deferred_invalidations.discard(run_index)
            self._invalidate_now(
                run_index,
                preserve_retry_kind=(work.key.kind if preserve_retry else None),
            )

    def _configure_cache(self, database_instance: DatabaseInstance) -> None:
        configure = getattr(self._cache, "configure_for_database", None)
        if callable(configure):
            configure(database_instance)

    @staticmethod
    def _error_record(error: BaseException) -> TrustedDerivedErrorRecord:
        stale_types = (
            InterruptedError,
            TrustedReadRequestCancelledError,
            TrustedLiveCancelledError,
        )
        transient_types = (
            _JobDeadlineExceeded,
            TimeoutError,
            TrustedReadQueueFullError,
            TrustedReadRequestDeadlineError,
            TrustedReadSessionFailedError,
            TrustedLiveBusyTimeoutError,
            TrustedLiveDeadlineExceededError,
            TrustedLiveSourceChangedError,
        )
        if isinstance(error, stale_types):
            category = TrustedDerivedErrorCategory.STALE
        elif isinstance(error, transient_types):
            category = TrustedDerivedErrorCategory.TRANSIENT
        else:
            category = TrustedDerivedErrorCategory.PERMANENT
        code = (
            type(error).__name__.encode("ascii", errors="replace")[:96].decode("ascii")
        )
        message = (
            str(error)
            .encode("utf-8", errors="replace")[:1024]
            .decode("utf-8", errors="ignore")
        )
        return TrustedDerivedErrorRecord(category, code, message)

    @staticmethod
    def _observation_size(observation: TrustedDerivedSourceObservation) -> int:
        """Conservatively bound every retained part of an observation.

        This is an admission bound, not an estimate of CPython's allocator.
        Count scalar payloads plus fixed per-node/container overhead and stop as
        soon as the shared reuse budget is exceeded.  In particular, retained
        run fields, parameter text, layout indexes, and byte-valued cells must
        not sit outside the byte-accounted LRU merely because sampled numeric
        cells are usually its largest component.
        """

        maximum = TRUSTED_DERIVED_MAX_REUSED_SOURCE_BYTES
        total = 2_048

        def add_scalar(value: object) -> bool:
            nonlocal total
            if value is None or isinstance(value, bool):
                total += 16
            elif isinstance(value, (int, float)):
                total += 32
            elif isinstance(value, str):
                total += 49 + len(value.encode("utf-8", errors="surrogatepass"))
            elif isinstance(value, bytes):
                total += 33 + len(value)
            else:
                return False
            return total <= maximum

        primitive_roots: list[object] = [
            observation.database_instance.logical_path,
            observation.database_instance.resolved_path,
            observation.database_instance.identity,
            tuple(observation.database_instance.sidecar_identities),
            observation.run_id,
            observation.run_guid,
            observation.service_namespace,
            observation.helper_incarnation,
            observation.data_version,
            observation.result_table_name,
            observation.result_columns,
            observation.result_schema_sha256,
            observation.result_watermark,
            observation.dependent_parameters,
            observation.planned_shape,
            observation.sample_columns,
            observation.sample_rows,
            observation.unsupported_reason,
            observation.run_fields,
            observation.progressive_layout_pending,
            observation.progressive_layout_cursor,
        ]
        for parameter in observation.parameters:
            primitive_roots.append(
                (
                    parameter.name,
                    parameter.label,
                    parameter.unit,
                    parameter.depends_on,
                    parameter.paramtype,
                )
            )
        for summary in observation.setpoint_summaries:
            primitive_roots.append(
                (summary.name, summary.first, summary.last, summary.steps)
            )
        for layout in observation.validated_2d_layouts:
            primitive_roots.append(
                (
                    layout.dependent,
                    layout.dependencies,
                    layout.shape,
                    layout.fast_axis_index,
                    layout.first_row_id,
                    layout.fast_id_stride,
                    layout.slow_id_stride,
                    layout.sample_slow_indexes,
                    layout.sample_fast_indexes,
                    layout.slow_reversed,
                    layout.fast_reversed,
                    layout.serpentine,
                    layout.complete,
                    layout.source,
                )
            )

        stack = list(primitive_roots)
        nodes = 0
        while stack:
            value = stack.pop()
            nodes += 1
            if nodes > 262_144:
                return maximum + 1
            if isinstance(value, tuple):
                total += 40 + len(value) * 8
                if total > maximum:
                    return total
                stack.extend(value)
            elif not add_scalar(value):
                return maximum + 1
        return total

    @staticmethod
    def _validated_runs(
        runs: Sequence[TrustedDerivedRun],
    ) -> tuple[TrustedDerivedRun, ...]:
        output = tuple(runs)
        if any(not isinstance(run, TrustedDerivedRun) for run in output):
            raise TypeError("runs must contain TrustedDerivedRun values.")
        if len({run.run_id for run in output}) != len(output):
            raise ValueError("run ids must be unique.")
        if len({run.run_guid for run in output}) != len(output):
            raise ValueError("run GUIDs must be unique.")
        return output

    def _require_owner(self) -> None:
        if threading.get_ident() != self._owner_thread_id:
            raise RuntimeError(
                "TrustedWorkCoordinator must be used on its owner thread."
            )

    def _require_open(self) -> None:
        if self._closed or self._scheduler.lifecycle is SchedulerLifecycle.CLOSED:
            raise RuntimeError("The trusted work coordinator is closed.")
