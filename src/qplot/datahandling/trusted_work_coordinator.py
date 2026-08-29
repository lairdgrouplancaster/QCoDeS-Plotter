"""Qt-independent owner/executor boundary for trusted Stage 5B derived work."""

from __future__ import annotations

import hashlib
import queue
import threading
import time
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
    TrustedRunWorkSource,
    TrustedWorkKind,
    TrustedWorkScheduler,
    TrustedWorkState,
    WorkFormat,
    WorkPublication,
)

TRUSTED_DERIVED_DEFAULT_DEADLINE_SECONDS = 15.0
TRUSTED_DERIVED_MAX_REUSED_SOURCE_BYTES = 8 * 1024 * 1024

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


ErrorCallback: TypeAlias = Callable[[ScheduledWork, TrustedDerivedErrorRecord], None]


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
    cache_hit: bool


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


_WorkerResult: TypeAlias = _ExecutionResult | _ExecutionFailure


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
            self._timer = None
            self._deadline = None
            # Invoke while holding the notifier-only lock so cancel() cannot
            # return while an obsolete callback is still about to run.
            assert self._wakeup is not None
            self._wakeup()


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
        self._reused_observation: TrustedDerivedSourceObservation | None = None
        self._memory_payload: tuple[TrustedCacheWorkKey, DerivedPayload] | None = None
        self._deferred_invalidations: set[int] = set()
        self._retry_attempts: dict[tuple[str, TrustedWorkKind], int] = {}
        self._retry_not_before = 0.0
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
        self._scheduler.select_run(run_index)
        self._pump()

    def set_visible_range(self, start: int, stop: int) -> None:
        self._require_owner()
        self._scheduler.set_visible_range(start, stop)
        self._pump()

    def set_visible_indices(self, indices: Sequence[int]) -> None:
        self._require_owner()
        self._scheduler.set_visible_indices(indices)
        self._pump()

    def reconcile_runs(self, runs: Sequence[TrustedDerivedRun]) -> None:
        self._require_owner()
        updated = self._validated_runs(runs)
        if len(updated) < len(self._runs) or updated[: len(self._runs)] != self._runs:
            raise ValueError("Coordinator run reconciliation must be append-only.")
        self._runs = updated
        self._scheduler.reconcile_runs(tuple(run.scheduler_source() for run in updated))
        self._pump()

    def source_changed(self, run_index: int) -> None:
        """Coalesce appends to active work; publish its prefix before refreshing."""

        self._require_owner()
        self._require_open()
        if not 0 <= run_index < len(self._runs):
            raise IndexError("run_index is outside the stable run table.")
        if self._active is not None and self._active.work.run_index == run_index:
            self._deferred_invalidations.add(run_index)
            return
        self._invalidate_now(run_index)
        self._pump()

    def update_format(self, kind: TrustedWorkKind, work_format: WorkFormat) -> None:
        self._require_owner()
        self._scheduler.update_format(kind, work_format)
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
    ) -> None:
        self._require_owner()
        self._require_open()
        if service.database_instance != database_instance:
            raise ValueError("The replacement service is bound to another database.")
        prior_service = self._service
        prior_owned = self._own_service
        updated = self._validated_runs(runs)
        self._runs = updated
        self._service = service
        if own_service is not None:
            self._own_service = bool(own_service)
        self._reused_observation = None
        self._memory_payload = None
        self._deferred_invalidations.clear()
        self._retry_attempts.clear()
        self._retry_not_before = 0.0
        self._retry_notifier.cancel()
        self._configure_cache(database_instance)
        self._scheduler.switch_database(
            database_instance,
            tuple(run.scheduler_source() for run in updated),
        )
        if prior_owned and prior_service is not service:
            prior_service.close_async()
        self._pump()

    def helper_restarted(self) -> None:
        """Invalidate every result after a helper-incarnation boundary."""

        self._require_owner()
        self._require_open()
        replacements = tuple(
            TrustedDerivedRun(
                run.run_id,
                run.run_guid,
                self._invalidation_revision(index),
            )
            for index, run in enumerate(self._runs)
        )
        self._runs = replacements
        self._reused_observation = None
        self._memory_payload = None
        self._deferred_invalidations.clear()
        self._retry_attempts.clear()
        self._retry_not_before = 0.0
        self._retry_notifier.cancel()
        self._scheduler.switch_database(
            self._scheduler.database_instance,
            tuple(run.scheduler_source() for run in replacements),
        )
        self._pump()

    def poll(self) -> int:
        """Marshal ready worker completions onto the scheduler owner thread."""

        self._require_owner()
        handled = 0
        while True:
            try:
                completion = self._completions.get_nowait()
            except queue.Empty:
                break
            handled += 1
            active = self._active
            if active is None or active.work is not completion.work:
                continue
            self._active = None
            if not self._scheduler.is_current_claim(completion.work):
                self._pump()
                continue
            if isinstance(completion, _ExecutionFailure):
                self._handle_failure(completion)
            else:
                self._handle_success(completion)
            self._pump()
        self._pump()
        return handled

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
        self._retry_notifier.cancel()
        self._scheduler.close()
        if self._own_service:
            self._service.close_async()
        active = self._active
        if active is not None:
            active.work.cancellation.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._reused_observation = None
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

    def _pump(self) -> None:
        if (
            self._closed
            or self._active is not None
            or time.monotonic() < self._retry_not_before
        ):
            return
        work = self._scheduler.claim_next()
        if work is None:
            return
        run = self._runs[work.run_index]
        memory_payload = self._memory_payload
        if memory_payload is not None and memory_payload[0] == work.key:
            self._memory_payload = None
        else:
            memory_payload = None
        observation = self._reused_observation
        deadline = time.monotonic() + self._deadline_seconds
        future = self._executor.submit(
            self._execute,
            work,
            run,
            self._service,
            observation,
            memory_payload,
            deadline,
        )
        self._active = _ActiveClaim(
            work,
            future,
            bytes(self._service.source_revision_namespace.nonce),
            deadline,
        )
        future.add_done_callback(self._worker_done)

    def _execute(
        self,
        work: ScheduledWork,
        run: TrustedDerivedRun,
        service: TrustedLiveReadService,
        reused: TrustedDerivedSourceObservation | None,
        memory_payload: tuple[TrustedCacheWorkKey, DerivedPayload] | None,
        deadline: float,
    ) -> _WorkerResult:
        request = None
        try:

            def cancel_check() -> None:
                work.cancellation.raise_if_cancelled()
                if time.monotonic() >= deadline:
                    raise _JobDeadlineExceeded(
                        "The trusted derived job exceeded its absolute deadline."
                    )

            cancel_check()
            if memory_payload is not None:
                cancel_check()
                return _ExecutionResult(work, work.key, memory_payload[1], reused, True)
            cached = self._cache.get(work.key, cancel_check=cancel_check)
            if cached is not None:
                cancel_check()
                return _ExecutionResult(work, work.key, cached, reused, True)

            observation = reused
            if (
                observation is None
                or observation.database_instance != work.key.database_instance
                or observation.run_guid != run.run_guid
                or trusted_derived_source_revision(observation)
                != work.key.source_revision
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
            cancel_check()
            if (
                observation.database_instance != work.key.database_instance
                or observation.run_id != run.run_id
                or observation.run_guid != run.run_guid
                or observation.service_namespace
                != service.source_revision_namespace.nonce
            ):
                raise RuntimeError("A stale trusted source observation was rejected.")
            revision = trusted_derived_source_revision(observation)
            actual_key = TrustedCacheWorkKey(
                work.key.database_instance,
                work.key.run_guid,
                work.key.kind,
                revision,
                work.key.renderer_version,
                work.key.rendering_options,
            )
            cached = self._cache.get(actual_key, cancel_check=cancel_check)
            if cached is not None:
                cancel_check()
                return _ExecutionResult(work, actual_key, cached, observation, True)
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
            return _ExecutionResult(work, actual_key, payload, observation, False)
        except BaseException as error:
            if request is not None and not request.done:
                request.cancel()
            return _ExecutionFailure(work, self._error_record(error))

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

    def _handle_success(self, result: _ExecutionResult) -> None:
        slot = (result.work.key.run_guid, result.work.key.kind)
        self._retry_attempts.pop(slot, None)
        self._retry_not_before = 0.0
        self._retry_notifier.cancel()
        if result.key != result.work.key:
            disposition = self._scheduler.refine_claim_source_revision(
                result.work,
                result.key.source_revision,
            )
            if disposition is CompletionDisposition.STALE:
                return
            self._adopt_observation(result.observation)
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
        self._adopt_observation(result.observation)
        disposition = self._scheduler.complete(result.work, result.payload)
        self._process_deferred(result.work, disposition, terminal=True)

    def _handle_failure(self, failure: _ExecutionFailure) -> None:
        if failure.error.category is TrustedDerivedErrorCategory.STALE:
            disposition = self._scheduler.abandon(failure.work)
            self._process_deferred(failure.work, disposition, terminal=False)
            return
        if failure.error.category is TrustedDerivedErrorCategory.TRANSIENT:
            disposition = self._scheduler.abandon(failure.work)
            if disposition is CompletionDisposition.ACCEPTED:
                slot = (failure.work.key.run_guid, failure.work.key.kind)
                attempt = min(self._retry_attempts.get(slot, 0) + 1, 8)
                self._retry_attempts[slot] = attempt
                self._retry_not_before = time.monotonic() + min(
                    1.0, 0.025 * (2 ** (attempt - 1))
                )
                self._retry_notifier.schedule(self._retry_not_before)
            self._process_deferred(
                failure.work,
                disposition,
                terminal=False,
                preserve_retry=True,
            )
            return
        if self._on_error is not None:
            self._on_error(failure.work, failure.error)
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

    def _invalidate_now(self, run_index: int) -> None:
        revision = self._invalidation_revision(run_index)
        run = self._runs[run_index]
        updated = list(self._runs)
        updated[run_index] = TrustedDerivedRun(run.run_id, run.run_guid, revision)
        self._runs = tuple(updated)
        self._reused_observation = None
        self._memory_payload = None
        self._scheduler.update_source_revision(run_index, revision)

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
    ) -> None:
        if observation is not None:
            self._reused_observation = (
                observation
                if self._observation_size(observation)
                <= TRUSTED_DERIVED_MAX_REUSED_SOURCE_BYTES
                else None
            )

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
            if not preserve_retry:
                self._retry_attempts = {
                    slot: attempt
                    for slot, attempt in self._retry_attempts.items()
                    if slot[0] != work.key.run_guid
                }
                self._retry_not_before = 0.0
                self._retry_notifier.cancel()
            self._invalidate_now(run_index)

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
        text = sum(
            len(value.encode("utf-8"))
            for value in (
                observation.run_guid,
                observation.result_table_name,
                *observation.result_columns,
                *observation.sample_columns,
            )
        )
        return text + sum(len(row) * 32 for row in observation.sample_rows)

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
