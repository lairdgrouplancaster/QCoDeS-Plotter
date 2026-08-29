"""Qt-independent Stage 4 broker for one trusted QCoDeS database instance.

The dispatcher is the sole normal query path and serialises all operations for
one persistent :class:`TrustedLiveReaderSupervisor`.  A separate control
thread is allowed to call the supervisor's synchronous exact-job cancellation
while the dispatcher is blocked in ``wait``.  Public cancellation and close
only mutate bounded broker state and wake that control path, so they return
promptly and are safe to call from the Qt GUI thread.  Results are consumed
through request handles; the broker deliberately exposes no callback facility
whose arbitrary lifetime could hold service retirement open.
"""

from __future__ import annotations

import heapq
import logging
import math
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Any, Generic, TypeVar, cast

from qplot.datahandling.file_identity import DatabaseInstance, database_instance
from qplot.datahandling.trusted_live import (
    SqliteBindings,
    TrustedLiveBusyTimeoutError,
    TrustedLiveCancelledError,
    TrustedLiveQueryError,
    TrustedLiveResultLimitError,
    TrustedLiveSqlRejectedError,
    TrustedQuery,
    TrustedQueryResult,
)
from qplot.datahandling.trusted_live_queries import (
    TrustedBootstrapResult,
    TrustedDerivedSourceObservation,
    TrustedMetadataQueryAdapter,
    TrustedRefreshResult,
    TrustedRunPage,
    TrustedRunRecord,
    TrustedSelectedRunDetail,
    TrustedSourceRevisionNamespace,
)
from qplot.datahandling.trusted_live_supervisor import (
    TrustedLiveJob,
    TrustedLiveReaderSupervisor,
    TrustedLiveSupervisorLiveness,
)

_LOG = logging.getLogger(__name__)
_ResultT = TypeVar("_ResultT")
_NO_RESULT = object()
_DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0
_MAX_REQUEST_TIMEOUT_SECONDS = 300.0
_DEFAULT_QUEUE_CAPACITY = 512
_MAX_FOREGROUND_DISPATCH_BURST = 8

TRUSTED_LIVE_MODE = "trusted_live"
SNAPSHOT_FALLBACK_MODE = "snapshot_fallback"


class TrustedReadOperation(StrEnum):
    BOOTSTRAP = "bootstrap"
    BASIC_PAGE = "basic-page"
    REFRESH = "refresh"
    CHEAP_RUN = "cheap-run"
    EXPENSIVE_RUN = "expensive-run"
    SELECTED_RUN = "selected-run"
    DERIVED_SOURCE = "derived-source"


class TrustedReadPriority(IntEnum):
    BOOTSTRAP = 0
    REFRESH = 10
    SELECTED_CHEAP = 20
    SELECTED_EXPENSIVE = 30
    VISIBLE_CHEAP = 40
    VISIBLE_EXPENSIVE = 50
    REMAINING_CHEAP = 60
    REMAINING_EXPENSIVE = 70


class TrustedReadServiceError(RuntimeError):
    """Base class for failures introduced by the Stage 4 broker."""


class TrustedReadServiceClosedError(TrustedReadServiceError):
    """A request targeted a closing or closed broker session."""


class TrustedReadQueueFullError(TrustedReadServiceError):
    """The bounded broker queue rejected additional work."""


class TrustedReadRequestCancelledError(
    InterruptedError,
    TrustedReadServiceError,
):
    """One exact public broker request was cancelled."""


class TrustedReadRequestDeadlineError(TimeoutError, TrustedReadServiceError):
    """A broker request expired before it could publish a result."""


class TrustedReadSessionFailedError(TrustedReadServiceError):
    """A terminal helper/session failure invalidated queued work."""


@dataclass(frozen=True, slots=True)
class TrustedReadRequestIdentity:
    """Immutable identity and scheduling data for one public request."""

    session_generation: int
    request_id: int
    database_instance: DatabaseInstance
    operation: TrustedReadOperation
    initial_priority: int
    deadline: float


@dataclass(frozen=True, slots=True)
class TrustedReadServiceLiveness:
    dispatcher_alive: bool
    control_alive: bool
    helper_alive: bool
    helper_pid: int | None
    receiver_alive: bool
    open_supervisor_endpoints: int
    unreaped_incarnations: int
    resource_cleanup_pending: bool
    outstanding_requests: int
    closing: bool
    closed: bool


@dataclass(slots=True)
class _RequestState:
    identity: TrustedReadRequestIdentity
    priority: int
    operation_id: int
    done: threading.Event = field(default_factory=threading.Event)
    result: object = _NO_RESULT
    error: BaseException | None = None
    cancelled: bool = False
    request: TrustedReadRequest[Any] | None = None
    slot_released: bool = False


@dataclass(slots=True)
class _OperationState:
    operation_id: int
    kind: TrustedReadOperation
    payload: tuple[object, ...]
    coalesce_key: tuple[object, ...]
    priority: int
    sequence: int
    deadline: float
    subscribers: set[int] = field(default_factory=set)
    revision: int = 0
    status: str = "queued"
    supervisor_job: TrustedLiveJob[Any] | None = None
    cancel_underlying: bool = False
    force_next_transaction: bool = False


class TrustedReadRequest(Generic[_ResultT]):
    """One exact cancellable handle returned by :class:`TrustedLiveReadService`."""

    __slots__ = ("_service", "_state")

    def __init__(
        self,
        service: TrustedLiveReadService,
        state: _RequestState,
    ) -> None:
        self._service = service
        self._state = state

    @property
    def identity(self) -> TrustedReadRequestIdentity:
        return self._state.identity

    @property
    def request_id(self) -> int:
        return self._state.identity.request_id

    @property
    def priority(self) -> int:
        return self._service._request_priority(self._state)

    @property
    def deadline(self) -> float:
        return self._state.identity.deadline

    @property
    def done(self) -> bool:
        return self._state.done.is_set()

    @property
    def cancelled(self) -> bool:
        return self._state.cancelled

    def cancel(self) -> bool:
        """Mark only this public request; never block on supervisor cleanup."""

        return self._service.cancel(self)

    def promote(self, priority: int | TrustedReadPriority) -> bool:
        return self._service.promote(self, priority)

    def reprioritize(self, priority: int | TrustedReadPriority) -> bool:
        """Replace this subscriber's priority, including a demotion."""

        return self._service.reprioritize(self, priority)

    def wait(self, timeout: float | None = None) -> _ResultT:
        """Wait for publication; callers must keep this off the Qt GUI thread."""

        if timeout is not None:
            timeout = _finite_duration(timeout, allow_zero=True)
        if not self._state.done.wait(timeout):
            raise TimeoutError("The broker request wait timed out.")
        error = self._state.error
        if error is not None:
            raise error
        if self._state.result is _NO_RESULT:
            raise TrustedReadServiceError(
                "The broker request completed without a result or error."
            )
        return cast(_ResultT, self._state.result)


class _BrokerQueryExecutor:
    """Expose supervisor operations while publishing the exact active job."""

    def __init__(
        self,
        service: TrustedLiveReadService,
        operation: _OperationState,
    ) -> None:
        self._service = service
        self._operation = operation

    @property
    def incarnation(self) -> int:
        return self._service._required_supervisor().incarnation

    def query(
        self,
        sql: str,
        bindings: SqliteBindings = None,
        *,
        timeout: float | None = None,
        wait_timeout: float | None = None,
    ) -> TrustedQueryResult:
        del timeout, wait_timeout
        supervisor = self._service._required_supervisor()
        remaining = self._service._prepare_supervisor_transaction(
            self._operation,
            self,
        )
        job = supervisor.submit_query(sql, bindings, timeout=remaining)
        return self._service._wait_supervisor_job(
            self._operation,
            job,
            remaining,
        )

    def query_batch(
        self,
        queries: tuple[TrustedQuery, ...],
        *,
        timeout: float | None = None,
        wait_timeout: float | None = None,
    ) -> tuple[TrustedQueryResult, ...]:
        del timeout, wait_timeout
        supervisor = self._service._required_supervisor()
        remaining = self._service._prepare_supervisor_transaction(
            self._operation,
            self,
        )
        job = supervisor.submit_query_batch(queries, timeout=remaining)
        return self._service._wait_supervisor_job(
            self._operation,
            job,
            remaining,
        )

    def data_version(
        self,
        *,
        timeout: float | None = None,
        wait_timeout: float | None = None,
    ) -> int:
        del timeout, wait_timeout
        supervisor = self._service._required_supervisor()
        remaining = self._service._prepare_supervisor_transaction(
            self._operation,
            self,
        )
        job = supervisor.submit_data_version(timeout=remaining)
        return self._service._wait_supervisor_job(
            self._operation,
            job,
            remaining,
        )


def _finite_duration(value: object, *, allow_zero: bool) -> float:
    if isinstance(value, bool):
        raise ValueError("A request timeout must be a finite number of seconds.")
    try:
        duration = float(cast(Any, value))
    except (TypeError, ValueError) as error:
        raise ValueError(
            "A request timeout must be a finite number of seconds."
        ) from error
    if (
        not math.isfinite(duration)
        or duration < 0
        or (not allow_zero and duration == 0)
        or duration > _MAX_REQUEST_TIMEOUT_SECONDS
    ):
        raise ValueError(
            "A request timeout must be finite, non-negative, and no greater "
            f"than {_MAX_REQUEST_TIMEOUT_SECONDS:g} seconds."
        )
    return duration


def _priority(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("priority must be an integer.")
    try:
        priority = int(cast(Any, value))
    except (TypeError, ValueError) as error:
        raise ValueError("priority must be an integer.") from error
    if priority < -1_000_000 or priority > 1_000_000:
        raise ValueError("priority is outside the bounded broker range.")
    return priority


class TrustedLiveReadService:
    """Own exactly one persistent trusted helper for one database instance."""

    def __init__(
        self,
        database_path: str | os.PathLike[str],
        *,
        expected_database_instance: DatabaseInstance | None = None,
        session_generation: int = 1,
        queue_capacity: int = _DEFAULT_QUEUE_CAPACITY,
        request_timeout_seconds: float = _DEFAULT_REQUEST_TIMEOUT_SECONDS,
        supervisor_factory: Callable[..., TrustedLiveReaderSupervisor] | None = None,
        supervisor_options: dict[str, Any] | None = None,
    ) -> None:
        if type(session_generation) is not int or session_generation <= 0:
            raise ValueError("session_generation must be a positive integer.")
        if type(queue_capacity) is not int or queue_capacity <= 0:
            raise ValueError("queue_capacity must be a positive integer.")
        selected_path = os.fspath(database_path)
        if expected_database_instance is None:
            expected_database_instance = database_instance(selected_path)
        elif not isinstance(expected_database_instance, DatabaseInstance):
            raise TypeError(
                "expected_database_instance must be a DatabaseInstance or None."
            )
        self._database_path = selected_path
        self._database_instance = expected_database_instance
        self._session_generation = session_generation
        self._capacity = queue_capacity
        self._request_timeout = _finite_duration(
            request_timeout_seconds,
            allow_zero=False,
        )
        self._supervisor_factory = (
            supervisor_factory or TrustedLiveReaderSupervisor.open
        )
        self._supervisor_options = dict(supervisor_options or {})
        self._supervisor: TrustedLiveReaderSupervisor | None = None
        self._closing_supervisor: TrustedLiveReaderSupervisor | None = None
        self._retained_supervisors: list[TrustedLiveReaderSupervisor] = []
        self._adapter: TrustedMetadataQueryAdapter | None = None
        self._source_revision_namespace = TrustedSourceRevisionNamespace.create()
        self._accepted_database_instance: DatabaseInstance | None = None

        self._condition = threading.Condition(threading.RLock())
        self._control_wakeup = threading.Event()
        self._control_stop = threading.Event()
        self._dispatcher_done = threading.Event()
        self._control_done = threading.Event()
        self._closed_event = threading.Event()
        self._requests: dict[int, _RequestState] = {}
        self._request_slots_in_use = 0
        self._operations: dict[int, _OperationState] = {}
        self._coalesced: dict[tuple[object, ...], int] = {}
        self._heap: list[tuple[int, int, int, int]] = []
        self._next_request_id = 1
        self._next_operation_id = 1
        self._next_sequence = 1
        self._foreground_dispatch_burst = 0
        self._active_operation: _OperationState | None = None
        self._control_cancel_job: TrustedLiveJob[Any] | None = None
        self._closing = False
        self._closed = False
        self._fatal_error: BaseException | None = None
        self._close_error: BaseException | None = None

        self._control_thread = threading.Thread(
            target=self._control_loop,
            name=f"qplot-trusted-read-control-{session_generation}",
            daemon=True,
        )
        self._dispatcher_thread = threading.Thread(
            target=self._dispatcher_loop,
            name=f"qplot-trusted-read-dispatcher-{session_generation}",
            daemon=True,
        )
        self._control_thread.start()
        self._dispatcher_thread.start()

    @property
    def database_instance(self) -> DatabaseInstance:
        with self._condition:
            return self._accepted_database_instance or self._database_instance

    @property
    def session_generation(self) -> int:
        return self._session_generation

    @property
    def source_revision_namespace(self) -> TrustedSourceRevisionNamespace:
        """Restart-unique identity used by all derived observations in this service."""

        return self._source_revision_namespace

    @property
    def mode(self) -> str:
        return TRUSTED_LIVE_MODE

    @property
    def accepted(self) -> bool:
        """Whether Stage 3 startup accepted and bound the source instance."""

        with self._condition:
            return self._accepted_database_instance is not None

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    @property
    def closing(self) -> bool:
        """Whether asynchronous retirement has begun but is not yet complete."""

        with self._condition:
            return self._closing

    @property
    def close_error(self) -> BaseException | None:
        with self._condition:
            return self._close_error

    @property
    def fatal_error(self) -> BaseException | None:
        """Return the exact terminal dispatcher/control failure, if any."""

        with self._condition:
            return self._fatal_error

    def liveness(self) -> TrustedReadServiceLiveness:
        with self._condition:
            supervisors = self._owned_supervisors_locked()
            outstanding_requests = self._request_slots_in_use
            closing = self._closing
            closed = self._closed
        helper_alive = False
        helper_pid = None
        receiver_alive = False
        open_endpoints = 0
        unreaped_incarnations = 0
        resource_cleanup_pending = False
        for supervisor in supervisors:
            snapshot = self._supervisor_resource_liveness(
                supervisor,
                conservative_on_error=False,
            )
            helper_alive = helper_alive or snapshot.process_alive
            if helper_pid is None:
                helper_pid = snapshot.helper_pid
            receiver_alive = receiver_alive or snapshot.receiver_alive
            open_endpoints += snapshot.open_endpoints
            unreaped_incarnations += int(snapshot.unreaped_incarnation)
            resource_cleanup_pending = (
                resource_cleanup_pending or snapshot.resources_owned
            )
        return TrustedReadServiceLiveness(
            dispatcher_alive=self._dispatcher_thread.is_alive(),
            control_alive=self._control_thread.is_alive(),
            helper_alive=helper_alive,
            helper_pid=helper_pid,
            receiver_alive=receiver_alive,
            open_supervisor_endpoints=open_endpoints,
            unreaped_incarnations=unreaped_incarnations,
            resource_cleanup_pending=resource_cleanup_pending,
            outstanding_requests=outstanding_requests,
            closing=closing,
            closed=closed,
        )

    def submit_bootstrap(
        self,
        *,
        deadline: float | None = None,
    ) -> TrustedReadRequest[TrustedBootstrapResult]:
        return cast(
            TrustedReadRequest[TrustedBootstrapResult],
            self._submit(
                TrustedReadOperation.BOOTSTRAP,
                (),
                TrustedReadPriority.BOOTSTRAP,
                deadline,
            ),
        )

    def submit_refresh(
        self,
        accepted_run_id: int | None = None,
        *,
        deadline: float | None = None,
    ) -> TrustedReadRequest[TrustedRefreshResult]:
        if accepted_run_id is not None and (
            type(accepted_run_id) is not int or accepted_run_id < 0
        ):
            raise ValueError("accepted_run_id must be a non-negative integer or None.")
        return cast(
            TrustedReadRequest[TrustedRefreshResult],
            self._submit(
                TrustedReadOperation.REFRESH,
                (accepted_run_id,),
                TrustedReadPriority.REFRESH,
                deadline,
            ),
        )

    def submit_basic_page(
        self,
        after_run_id: int,
        through_run_id: int,
        *,
        priority: int | TrustedReadPriority = TrustedReadPriority.BOOTSTRAP,
        deadline: float | None = None,
    ) -> TrustedReadRequest[TrustedRunPage]:
        if type(after_run_id) is not int or after_run_id < 0:
            raise ValueError("after_run_id must be a non-negative integer.")
        if type(through_run_id) is not int or through_run_id < after_run_id:
            raise ValueError(
                "through_run_id must be an integer no smaller than after_run_id."
            )
        return cast(
            TrustedReadRequest[TrustedRunPage],
            self._submit(
                TrustedReadOperation.BASIC_PAGE,
                (after_run_id, through_run_id),
                priority,
                deadline,
            ),
        )

    def submit_cheap_run(
        self,
        run_id: int,
        *,
        priority: int | TrustedReadPriority = TrustedReadPriority.REMAINING_CHEAP,
        deadline: float | None = None,
    ) -> TrustedReadRequest[TrustedRunRecord]:
        return cast(
            TrustedReadRequest[TrustedRunRecord],
            self._submit(
                TrustedReadOperation.CHEAP_RUN,
                (self._validated_run_id(run_id),),
                priority,
                deadline,
            ),
        )

    def submit_expensive_run(
        self,
        run_id: int,
        *,
        priority: int | TrustedReadPriority = (TrustedReadPriority.REMAINING_EXPENSIVE),
        deadline: float | None = None,
    ) -> TrustedReadRequest[TrustedRunRecord]:
        return cast(
            TrustedReadRequest[TrustedRunRecord],
            self._submit(
                TrustedReadOperation.EXPENSIVE_RUN,
                (self._validated_run_id(run_id),),
                priority,
                deadline,
            ),
        )

    def submit_selected_run(
        self,
        run_id: int,
        *,
        priority: int | TrustedReadPriority = TrustedReadPriority.SELECTED_EXPENSIVE,
        deadline: float | None = None,
    ) -> TrustedReadRequest[TrustedSelectedRunDetail]:
        return cast(
            TrustedReadRequest[TrustedSelectedRunDetail],
            self._submit(
                TrustedReadOperation.SELECTED_RUN,
                (self._validated_run_id(run_id),),
                priority,
                deadline,
            ),
        )

    def submit_derived_source(
        self,
        run_id: int,
        *,
        priority: int | TrustedReadPriority = TrustedReadPriority.REMAINING_EXPENSIVE,
        deadline: float | None = None,
    ) -> TrustedReadRequest[TrustedDerivedSourceObservation]:
        """Capture one bounded immutable prefix through the persistent broker."""

        return cast(
            TrustedReadRequest[TrustedDerivedSourceObservation],
            self._submit(
                TrustedReadOperation.DERIVED_SOURCE,
                (self._validated_run_id(run_id),),
                priority,
                deadline,
            ),
        )

    @staticmethod
    def _validated_run_id(run_id: object) -> int:
        if type(run_id) is not int or run_id <= 0:
            raise ValueError("run_id must be a positive integer.")
        return run_id

    def _submit(
        self,
        kind: TrustedReadOperation,
        payload: tuple[object, ...],
        priority: int | TrustedReadPriority,
        deadline: float | None,
    ) -> TrustedReadRequest[Any]:
        requested_priority = _priority(priority)
        now = time.monotonic()
        default_deadline = deadline is None
        if deadline is None:
            request_deadline = now + self._request_timeout
        else:
            if isinstance(deadline, bool):
                raise ValueError("deadline must be a finite monotonic timestamp.")
            request_deadline = float(deadline)
            if not math.isfinite(request_deadline):
                raise ValueError("deadline must be a finite monotonic timestamp.")
            if request_deadline - now > _MAX_REQUEST_TIMEOUT_SECONDS:
                raise ValueError("deadline exceeds the bounded broker request horizon.")

        with self._condition:
            if self._closing or self._closed:
                raise TrustedReadServiceClosedError(
                    "The trusted read service is closing or closed."
                )
            if self._request_slots_in_use >= self._capacity:
                raise TrustedReadQueueFullError(
                    f"The trusted read queue reached its {self._capacity}-request cap."
                )
            request_id = self._next_request_id
            self._next_request_id += 1
            # Ordinary duplicate submissions share the first operation's
            # bounded default deadline; an active supervisor wait is therefore
            # never extended. Explicit deadlines coalesce only when exactly
            # equal, preserving every caller's requested bound.
            coalesce_key = (
                (kind, payload, "default")
                if default_deadline
                else (kind, payload, "explicit", request_deadline)
            )
            operation_id = self._coalesced.get(coalesce_key)
            operation = (
                self._operations.get(operation_id) if operation_id is not None else None
            )
            coalesce_eligible = bool(
                operation is not None
                and operation.status in {"queued", "active"}
                and not operation.cancel_underlying
                and operation.deadline > now
            )
            if not coalesce_eligible:
                operation_id = self._next_operation_id
                self._next_operation_id += 1
                sequence = self._next_sequence
                self._next_sequence += 1
                operation = _OperationState(
                    operation_id,
                    kind,
                    payload,
                    coalesce_key,
                    requested_priority,
                    sequence,
                    request_deadline,
                )
                self._operations[operation_id] = operation
                self._coalesced[coalesce_key] = operation_id
                heapq.heappush(
                    self._heap,
                    (
                        operation.priority,
                        operation.sequence,
                        operation.revision,
                        operation.operation_id,
                    ),
                )
            else:
                assert operation is not None
                if default_deadline:
                    request_deadline = operation.deadline
            assert operation is not None
            identity = TrustedReadRequestIdentity(
                self._session_generation,
                request_id,
                self._database_instance,
                kind,
                requested_priority,
                request_deadline,
            )
            state = _RequestState(
                identity,
                requested_priority,
                operation.operation_id,
            )
            request: TrustedReadRequest[Any] = TrustedReadRequest(self, state)
            state.request = request
            self._requests[request_id] = state
            self._request_slots_in_use += 1
            operation.subscribers.add(request_id)
            # A coalesced operation is always scheduled from the complete set
            # of live subscribers.  This is also the single path used after
            # cancellation and request-level reprioritization.
            self._recompute_operation_schedule_locked(operation)
            self._condition.notify()
            self._control_wakeup.set()
            return request

    def _request_priority(self, state: _RequestState) -> int:
        with self._condition:
            return state.priority

    def promote(
        self,
        request: TrustedReadRequest[Any],
        priority: int | TrustedReadPriority,
    ) -> bool:
        requested_priority = _priority(priority)
        with self._condition:
            state = self._owned_request_state(request)
            if state.done.is_set() or requested_priority >= state.priority:
                return False
            return self._reprioritize_request_locked(state, requested_priority)

    def reprioritize(
        self,
        request: TrustedReadRequest[Any],
        priority: int | TrustedReadPriority,
    ) -> bool:
        """Replace one live subscriber priority and reschedule its operation."""

        requested_priority = _priority(priority)
        with self._condition:
            state = self._owned_request_state(request)
            if state.done.is_set() or requested_priority == state.priority:
                return False
            return self._reprioritize_request_locked(state, requested_priority)

    def _reprioritize_request_locked(
        self,
        state: _RequestState,
        requested_priority: int,
    ) -> bool:
        operation = self._operations.get(state.operation_id)
        if operation is None:
            return False
        state.priority = requested_priority
        self._recompute_operation_schedule_locked(operation)
        self._condition.notify_all()
        return True

    def cancel(self, request: TrustedReadRequest[Any]) -> bool:
        with self._condition:
            state = self._owned_request_state(request)
            if state.done.is_set():
                return False
            state.cancelled = True
            self._complete_request_locked(
                state,
                error=TrustedReadRequestCancelledError(
                    f"Trusted request {state.identity.request_id} was cancelled."
                ),
            )
            operation = self._operations.get(state.operation_id)
            if operation is not None:
                operation.subscribers.discard(state.identity.request_id)
                if not operation.subscribers:
                    if operation.status == "queued":
                        self._discard_operation_locked(operation)
                    elif operation.status == "active":
                        operation.cancel_underlying = True
                else:
                    self._recompute_operation_schedule_locked(operation)
            self._condition.notify_all()
        # Wake the deadline/cancellation control path even for a queued
        # cancellation so it can recompute its earliest timed wait.
        self._control_wakeup.set()
        return True

    def _owned_request_state(
        self,
        request: TrustedReadRequest[Any],
    ) -> _RequestState:
        if not isinstance(request, TrustedReadRequest) or request._service is not self:
            raise ValueError("The request does not belong to this trusted service.")
        return request._state

    def close_async(self) -> bool:
        """Begin cancellation/retirement and return without waiting or joining."""

        with self._condition:
            if self._closing or self._closed:
                return False
            self._closing = True
            error = TrustedReadServiceClosedError(
                "The trusted read service closed before the request completed."
            )
            for state in list(self._requests.values()):
                state.cancelled = True
                self._complete_request_locked(state, error=error)
            for operation in list(self._operations.values()):
                operation.subscribers.clear()
                if operation.status == "queued":
                    self._discard_operation_locked(operation)
                elif operation.status == "active":
                    operation.cancel_underlying = True
            self._condition.notify_all()
        self._control_wakeup.set()
        return True

    def escalate_cleanup_async(self) -> None:
        """Repeat retirement signals and perform one zero-wait resource reap.

        This method is safe for a Qt shutdown timer: it never waits for a
        broker thread or helper.  The dispatcher/control paths retain sole
        ownership of blocking Stage 3 cancellation and close operations.
        """

        self.close_async()
        with self._condition:
            operation = self._active_operation
            if operation is not None:
                operation.cancel_underlying = True
            self._condition.notify_all()
        self._control_wakeup.set()
        self._reap_retained_supervisors_once()

    def close(self, *, timeout: float | None = None) -> None:
        """Blocking convenience for non-GUI owners and tests."""

        self.close_async()
        if not self.wait_closed(timeout):
            raise TimeoutError("The trusted read service did not close in time.")
        error = self.close_error
        if error is not None:
            raise error

    def wait_closed(self, timeout: float | None = None) -> bool:
        if timeout is not None:
            timeout = _finite_duration(timeout, allow_zero=True)
        deadline = None if timeout is None else time.monotonic() + timeout
        threads = (
            self._dispatcher_thread,
            self._control_thread,
        )
        current = threading.current_thread()
        if current in threads:
            return False
        for event in (
            self._dispatcher_done,
            self._control_done,
            self._closed_event,
        ):
            remaining = (
                None if deadline is None else max(0.0, deadline - time.monotonic())
            )
            if not event.wait(remaining):
                return False
        for thread in threads:
            remaining = (
                None if deadline is None else max(0.0, deadline - time.monotonic())
            )
            thread.join(remaining)
            if thread.is_alive():
                return False
        while self._reap_retained_supervisors_once():
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                time.sleep(min(0.01, remaining))
            else:
                time.sleep(0.01)
        return True

    def _dispatcher_loop(self) -> None:
        try:
            while True:
                with self._condition:
                    operation = self._next_operation_locked()
                    if operation is None:
                        should_stop = self._closing
                        if not should_stop:
                            self._condition.wait(timeout=0.1)
                            continue
                    else:
                        should_stop = False
                        operation.status = "active"
                        self._active_operation = operation
                        # Fairness may select an operation directly rather than
                        # popping its live heap entry.  Keep the physical heap
                        # exactly bounded to the remaining queued operations.
                        self._compact_heap_locked()
                if operation is None:
                    if should_stop:
                        break
                    continue

                result: object = _NO_RESULT
                error: BaseException | None = None
                try:
                    result = self._execute_operation(operation)
                except BaseException as operation_error:
                    error = operation_error

                with self._condition:
                    self._finish_operation_locked(
                        operation,
                        result,
                        error,
                    )
        except BaseException as dispatcher_error:
            with self._condition:
                self._fatal_error = dispatcher_error
                self._closing = True
                self._fail_all_locked(dispatcher_error)
                self._condition.notify_all()
        finally:
            self._close_supervisor_from_dispatcher()
            with self._condition:
                self._closed = True
                self._closing = False
                self._condition.notify_all()
            self._dispatcher_done.set()
            self._control_stop.set()
            self._control_wakeup.set()
            with self._condition:
                self._condition.notify_all()
            self._closed_event.set()

    def _next_operation_locked(self) -> _OperationState | None:
        while True:
            now = time.monotonic()
            background = min(
                (
                    operation
                    for operation in self._operations.values()
                    if operation.status == "queued"
                    and operation.priority >= TrustedReadPriority.VISIBLE_CHEAP
                ),
                key=lambda operation: operation.sequence,
                default=None,
            )
            fairness_forced = (
                background is not None
                and self._foreground_dispatch_burst >= _MAX_FOREGROUND_DISPATCH_BURST
            )
            if fairness_forced:
                operation = background
            else:
                operation = self._pop_priority_operation_locked()
            if operation is None:
                self._foreground_dispatch_burst = 0
                return None
            if not self._prepare_queued_operation_locked(
                operation,
                now,
            ):
                continue
            if operation.priority >= TrustedReadPriority.VISIBLE_CHEAP:
                self._foreground_dispatch_burst = 0
            elif background is not None:
                self._foreground_dispatch_burst += 1
            else:
                self._foreground_dispatch_burst = 0
            operation.force_next_transaction = fairness_forced
            return operation

    def _pop_priority_operation_locked(self) -> _OperationState | None:
        while self._heap:
            priority, sequence, revision, operation_id = heapq.heappop(self._heap)
            operation = self._operations.get(operation_id)
            if (
                operation is not None
                and operation.status == "queued"
                and operation.priority == priority
                and operation.sequence == sequence
                and operation.revision == revision
            ):
                return operation
        return None

    def _compact_heap_locked(self) -> None:
        """Retain exactly one heap entry for every queued operation.

        Promotions and queued cancellation used to leave lazy tombstones in
        ``_heap``.  Because completed requests immediately release queue
        capacity, a producer could create an unbounded number of those physical
        entries behind one blocked active job.  Rebuilding at the bounded
        operation cardinality keeps backpressure true for the actual storage.
        """

        self._heap = [
            (
                operation.priority,
                operation.sequence,
                operation.revision,
                operation.operation_id,
            )
            for operation in self._operations.values()
            if operation.status == "queued"
        ]
        heapq.heapify(self._heap)

    def _recompute_operation_schedule_locked(
        self,
        operation: _OperationState,
    ) -> None:
        """Recompute a shared operation from only its live subscribers."""

        live_states: list[_RequestState] = []
        for request_id in tuple(operation.subscribers):
            state = self._requests.get(request_id)
            if state is None or state.done.is_set():
                operation.subscribers.discard(request_id)
            else:
                live_states.append(state)
        if not live_states:
            if operation.status == "queued":
                self._discard_operation_locked(operation)
            elif operation.status == "active":
                operation.cancel_underlying = True
                self._control_wakeup.set()
            return

        priority = min(state.priority for state in live_states)
        operation.deadline = min(state.identity.deadline for state in live_states)
        if priority == operation.priority:
            return
        operation.priority = priority
        operation.revision += 1
        if operation.status == "queued":
            self._compact_heap_locked()

    def _expire_queued_requests_locked(self, now: float) -> None:
        """Complete queued subscribers at their own monotonic deadlines."""

        for operation in tuple(self._operations.values()):
            if operation.status != "queued":
                continue
            expired = tuple(
                request_id
                for request_id in operation.subscribers
                if (
                    (state := self._requests.get(request_id)) is not None
                    and state.identity.deadline <= now
                )
            )
            for request_id in expired:
                state = self._requests.get(request_id)
                if state is None:
                    operation.subscribers.discard(request_id)
                    continue
                operation.subscribers.discard(request_id)
                self._complete_request_locked(
                    state,
                    error=TrustedReadRequestDeadlineError(
                        f"Trusted request {request_id} expired in the queue."
                    ),
                )
            if expired:
                self._recompute_operation_schedule_locked(operation)
        self._condition.notify_all()

    def _next_queued_deadline_locked(self) -> float | None:
        return min(
            (
                state.identity.deadline
                for operation in self._operations.values()
                if operation.status == "queued"
                for request_id in operation.subscribers
                if (state := self._requests.get(request_id)) is not None
            ),
            default=None,
        )

    def _next_higher_priority_operation_locked(
        self,
        current_priority: int,
    ) -> _OperationState | None:
        while True:
            operation = self._pop_priority_operation_locked()
            if operation is None:
                return None
            if operation.priority >= current_priority:
                heapq.heappush(
                    self._heap,
                    (
                        operation.priority,
                        operation.sequence,
                        operation.revision,
                        operation.operation_id,
                    ),
                )
                return None
            if self._prepare_queued_operation_locked(
                operation,
                time.monotonic(),
            ):
                return operation

    def _prepare_queued_operation_locked(
        self,
        operation: _OperationState,
        now: float,
    ) -> bool:
        for request_id in tuple(operation.subscribers):
            state = self._requests.get(request_id)
            if state is None:
                operation.subscribers.discard(request_id)
                continue
            if state.identity.deadline <= now:
                operation.subscribers.discard(request_id)
                self._complete_request_locked(
                    state,
                    error=TrustedReadRequestDeadlineError(
                        f"Trusted request {request_id} expired in the queue."
                    ),
                )
        if not operation.subscribers:
            self._discard_operation_locked(operation)
            return False
        self._recompute_operation_schedule_locked(operation)
        return True

    def _execute_operation(self, operation: _OperationState) -> object:
        startup_remaining = self._operation_remaining(operation)
        supervisor = self._ensure_supervisor(startup_remaining)
        with self._condition:
            if self._closing or operation.cancel_underlying:
                raise TrustedReadRequestCancelledError(
                    "The trusted operation was cancelled before execution."
                )
            adapter = self._adapter
            if adapter is None:
                adapter = TrustedMetadataQueryAdapter(
                    _BrokerQueryExecutor(self, operation),
                    self.database_instance.resolved_path,
                )
                self._adapter = adapter
            else:
                # The facade is operation-bound so exact cancellation always
                # targets the currently executing broker attempt.
                adapter.bind_executor(_BrokerQueryExecutor(self, operation))
        del supervisor
        if operation.kind is TrustedReadOperation.BOOTSTRAP:
            return adapter.bootstrap()
        if operation.kind is TrustedReadOperation.BASIC_PAGE:
            return adapter.basic_run_page(
                cast(int, operation.payload[0]),
                cast(int, operation.payload[1]),
            )
        if operation.kind is TrustedReadOperation.REFRESH:
            return adapter.refresh_new_runs(cast(int | None, operation.payload[0]))
        run_id = cast(int, operation.payload[0])
        if operation.kind is TrustedReadOperation.CHEAP_RUN:
            return adapter.cheap_run(run_id)
        if operation.kind is TrustedReadOperation.EXPENSIVE_RUN:
            return adapter.expensive_run(run_id)
        if operation.kind is TrustedReadOperation.SELECTED_RUN:
            return adapter.selected_run_detail(run_id)
        if operation.kind is TrustedReadOperation.DERIVED_SOURCE:
            return adapter.derived_source_observation(
                run_id,
                database_instance=self.database_instance,
                namespace=self._source_revision_namespace,
            )
        raise TrustedReadServiceError(
            f"Unsupported trusted broker operation {operation.kind!r}."
        )

    def _ensure_supervisor(
        self,
        startup_remaining: float,
    ) -> TrustedLiveReaderSupervisor:
        with self._condition:
            existing = self._supervisor
        if existing is not None:
            return existing
        supervisor_options = dict(self._supervisor_options)
        startup_key = "startup_timeout_seconds"
        if startup_key not in supervisor_options:
            supervisor_options["startup_timeout_seconds"] = startup_remaining
        else:
            configured_startup_timeout = supervisor_options[startup_key]
            try:
                configured_duration = (
                    math.nan
                    if isinstance(configured_startup_timeout, bool)
                    else float(cast(Any, configured_startup_timeout))
                )
            except (TypeError, ValueError):
                configured_duration = math.nan
            if (
                math.isfinite(configured_duration)
                and configured_duration > 0
                and startup_remaining < configured_duration
            ):
                supervisor_options[startup_key] = startup_remaining
        supervisor = self._supervisor_factory(
            self._database_path,
            expected_database_instance=self._database_instance,
            **supervisor_options,
        )
        with self._condition:
            if self._supervisor is not None:
                # This should be unreachable because only the dispatcher opens,
                # but fail closed without leaking an unexpected second owner.
                duplicate = supervisor
                supervisor = self._supervisor
            else:
                duplicate = None
                self._supervisor = supervisor
                self._accepted_database_instance = supervisor.database_instance
        if duplicate is not None:
            try:
                duplicate.close()
            finally:
                self._retain_supervisor_if_needed(duplicate)
        return supervisor

    def _required_supervisor(self) -> TrustedLiveReaderSupervisor:
        with self._condition:
            supervisor = self._supervisor
        if supervisor is None:
            raise TrustedReadServiceError(
                "The trusted supervisor has not completed startup."
            )
        return supervisor

    def _operation_remaining(self, operation: _OperationState) -> float:
        remaining = operation.deadline - time.monotonic()
        if remaining <= 0:
            raise TrustedReadRequestDeadlineError(
                f"Trusted operation {operation.operation_id} reached its deadline."
            )
        return min(remaining, _MAX_REQUEST_TIMEOUT_SECONDS)

    def _prepare_supervisor_transaction(
        self,
        operation: _OperationState,
        resume_executor: _BrokerQueryExecutor,
    ) -> float:
        """Yield at a job-free boundary to one strictly higher-priority request."""

        with self._condition:
            if self._active_operation is not operation:
                raise TrustedReadServiceError(
                    "The supervisor transaction does not match the active operation."
                )
            if self._closing or operation.cancel_underlying:
                raise TrustedReadRequestCancelledError(
                    "The trusted operation was cancelled before its next transaction."
                )
            if operation.supervisor_job is not None:
                raise TrustedReadServiceError(
                    "Cooperative scheduling requires a job-free transaction boundary."
                )
            if operation.force_next_transaction:
                operation.force_next_transaction = False
                nested = None
            else:
                nested = self._next_higher_priority_operation_locked(
                    operation.priority,
                )
            if nested is not None:
                if (
                    operation.priority >= TrustedReadPriority.VISIBLE_CHEAP
                    and nested.priority < TrustedReadPriority.VISIBLE_CHEAP
                ):
                    self._foreground_dispatch_burst += 1
                nested.status = "active"
                self._active_operation = nested
                self._compact_heap_locked()

        if nested is not None:
            result: object = _NO_RESULT
            nested_error: BaseException | None = None
            try:
                result = self._execute_operation(nested)
            except BaseException as error:
                nested_error = error
            with self._condition:
                self._finish_operation_locked(
                    nested,
                    result,
                    nested_error,
                )
                self._active_operation = operation
                self._condition.notify_all()
            adapter = self._adapter
            if adapter is not None:
                adapter.bind_executor(resume_executor)
            if nested_error is not None and self._terminal_session_error(nested_error):
                raise nested_error

        with self._condition:
            if self._active_operation is not operation:
                raise TrustedReadServiceError(
                    "The original operation was not restored after scheduling."
                )
            if self._closing or operation.cancel_underlying:
                raise TrustedReadRequestCancelledError(
                    "The trusted operation was cancelled before its next transaction."
                )
        return self._operation_remaining(operation)

    def _finish_operation_locked(
        self,
        operation: _OperationState,
        result: object,
        error: BaseException | None,
    ) -> None:
        operation.supervisor_job = None
        if self._active_operation is operation:
            self._active_operation = None
        operation.status = "finished"
        if self._coalesced.get(operation.coalesce_key) == operation.operation_id:
            self._coalesced.pop(operation.coalesce_key, None)
        self._operations.pop(operation.operation_id, None)
        now = time.monotonic()
        for request_id in tuple(operation.subscribers):
            state = self._requests.get(request_id)
            if state is None or state.done.is_set():
                continue
            publication_error = error
            publication_result = result
            if publication_error is None and now >= state.identity.deadline:
                publication_result = _NO_RESULT
                publication_error = TrustedReadRequestDeadlineError(
                    f"Trusted request {request_id} reached its deadline."
                )
            self._complete_request_locked(
                state,
                result=publication_result,
                error=publication_error,
            )
        if error is not None and self._terminal_session_error(error):
            self._fatal_error = error
            self._closing = True
            self._fail_queued_locked(error)
        self._condition.notify_all()

    def _wait_supervisor_job(
        self,
        operation: _OperationState,
        job: TrustedLiveJob[_ResultT],
        wait_timeout: float,
    ) -> _ResultT:
        wake_control = False
        with self._condition:
            if self._active_operation is not operation:
                raise TrustedReadServiceError(
                    "The supervisor job does not match the active broker operation."
                )
            operation.supervisor_job = job
            wake_control = operation.cancel_underlying or self._closing
        if wake_control:
            self._control_wakeup.set()
        try:
            return self._required_supervisor().wait(job, timeout=wait_timeout)
        finally:
            with self._condition:
                if operation.supervisor_job is job:
                    operation.supervisor_job = None
                if operation.priority >= TrustedReadPriority.VISIBLE_CHEAP:
                    # A nested yield runs at most one complete foreground
                    # operation before returning here for a real suspended
                    # background transaction.  That progress ends the burst.
                    self._foreground_dispatch_burst = 0

    def _control_loop(self) -> None:
        fatal_supervisor: TrustedLiveReaderSupervisor | None = None
        fatal_job: TrustedLiveJob[Any] | None = None
        try:
            while True:
                # Clear before observing broker state: a mutation after this
                # snapshot leaves the event set and cannot be lost between an
                # Event.wait() return and a subsequent clear().
                self._control_wakeup.clear()
                with self._condition:
                    self._expire_queued_requests_locked(time.monotonic())
                    if self._control_stop.is_set():
                        return
                    operation = self._active_operation
                    supervisor = self._supervisor
                    job = None if operation is None else operation.supervisor_job
                    should_cancel = bool(
                        operation is not None
                        and job is not None
                        and (operation.cancel_underlying or self._closing)
                        and job is not self._control_cancel_job
                    )
                    if should_cancel:
                        self._control_cancel_job = job
                    next_deadline = self._next_queued_deadline_locked()
                if should_cancel and supervisor is not None and job is not None:
                    try:
                        supervisor.cancel(job)
                    except TrustedLiveCancelledError:
                        pass
                    except BaseException as error:
                        with self._condition:
                            # The exact job can finish after the unlocked
                            # snapshot above, and the dispatcher can then close
                            # its supervisor before cancel() obtains the Stage 3
                            # lock.  A resulting closed/stale-job exception says
                            # nothing about the current session.  Terminalise
                            # only while this service still owns the same live
                            # supervisor operation and is not already retiring.
                            still_current = bool(
                                not self._closing
                                and not self._closed
                                and self._supervisor is supervisor
                                and operation is not None
                                and self._active_operation is operation
                                and operation.supervisor_job is job
                            )
                            if still_current:
                                if self._fatal_error is None:
                                    self._fatal_error = error
                                self._closing = True
                                self._fail_all_locked(error)
                                self._condition.notify_all()
                    continue

                timeout = (
                    None
                    if next_deadline is None
                    else max(0.0, next_deadline - time.monotonic())
                )
                self._control_wakeup.wait(timeout)
        except BaseException as control_error:
            with self._condition:
                if self._fatal_error is None:
                    self._fatal_error = control_error
                self._closing = True
                operation = self._active_operation
                fatal_supervisor = self._supervisor
                fatal_job = None if operation is None else operation.supervisor_job
                self._fail_all_locked(control_error)
                self._condition.notify_all()
            # A control-loop fault must publish terminal request outcomes before
            # attempting bounded Stage 3 cancellation.  Cancellation remains
            # off the GUI thread and any second failure is diagnostic only.
            if fatal_supervisor is not None and fatal_job is not None:
                try:
                    fatal_supervisor.cancel(fatal_job)
                except BaseException:
                    _LOG.exception(
                        "Trusted read-service control failure cleanup also failed"
                    )
        finally:
            self._control_done.set()

    def _close_supervisor_from_dispatcher(self) -> None:
        with self._condition:
            supervisor, self._supervisor = self._supervisor, None
            self._closing_supervisor = supervisor
        if supervisor is None:
            return
        try:
            supervisor.close()
        except BaseException as error:
            with self._condition:
                self._close_error = error
        finally:
            self._retain_supervisor_if_needed(supervisor)
            with self._condition:
                if self._closing_supervisor is supervisor:
                    self._closing_supervisor = None

    def _owned_supervisors_locked(self) -> tuple[TrustedLiveReaderSupervisor, ...]:
        supervisors: list[TrustedLiveReaderSupervisor] = []
        for supervisor in (
            self._supervisor,
            self._closing_supervisor,
            *self._retained_supervisors,
        ):
            if supervisor is not None and all(
                supervisor is not owned for owned in supervisors
            ):
                supervisors.append(supervisor)
        return tuple(supervisors)

    @staticmethod
    def _supervisor_resource_liveness(
        supervisor: TrustedLiveReaderSupervisor,
        *,
        conservative_on_error: bool = True,
    ) -> TrustedLiveSupervisorLiveness:
        probe = getattr(supervisor, "resource_liveness", None)
        if callable(probe):
            try:
                snapshot = probe()
                if not isinstance(snapshot, TrustedLiveSupervisorLiveness):
                    raise TypeError("Invalid supervisor resource-liveness snapshot.")
                return snapshot
            except BaseException:
                if not conservative_on_error:
                    raise
                # Losing observability must never be mistaken for successful
                # cleanup: retain the supervisor and report every dimension
                # conservatively alive.
                return TrustedLiveSupervisorLiveness(
                    helper_pid=None,
                    process_alive=True,
                    receiver_alive=True,
                    open_endpoints=1,
                    active_incarnation=True,
                    unreaped_incarnation=True,
                    active_job=True,
                    closing=True,
                    closed=False,
                )
        if conservative_on_error:
            try:
                helper_alive = supervisor.helper_alive
            except BaseException:
                helper_alive = True
            try:
                helper_pid = supervisor.helper_pid
            except BaseException:
                helper_pid = None
            try:
                closed = supervisor.closed
            except BaseException:
                closed = not helper_alive
        else:
            helper_alive = supervisor.helper_alive
            helper_pid = supervisor.helper_pid
            closed = supervisor.closed
        # Compatibility for supervisor test doubles predating the resource
        # API.  Production supervisors always take the exact path above.
        return TrustedLiveSupervisorLiveness(
            helper_pid=helper_pid,
            process_alive=helper_alive,
            receiver_alive=False,
            open_endpoints=0,
            active_incarnation=helper_alive,
            unreaped_incarnation=False,
            active_job=False,
            closing=False,
            closed=closed,
        )

    def _retain_supervisor_if_needed(
        self,
        supervisor: TrustedLiveReaderSupervisor,
    ) -> bool:
        snapshot = self._supervisor_resource_liveness(supervisor)
        with self._condition:
            retained = any(supervisor is item for item in self._retained_supervisors)
            if snapshot.resources_owned and not retained:
                self._retained_supervisors.append(supervisor)
            elif not snapshot.resources_owned and retained:
                self._retained_supervisors = [
                    item
                    for item in self._retained_supervisors
                    if item is not supervisor
                ]
        return snapshot.resources_owned

    def _reap_retained_supervisors_once(self) -> bool:
        with self._condition:
            supervisors = tuple(self._retained_supervisors)
        pending = False
        for supervisor in supervisors:
            reap = getattr(supervisor, "reap_closed_resources", None)
            if callable(reap):
                try:
                    snapshot = reap()
                    if not isinstance(snapshot, TrustedLiveSupervisorLiveness):
                        raise TypeError("Invalid supervisor resource-reap snapshot.")
                except BaseException:
                    snapshot = self._supervisor_resource_liveness(supervisor)
            else:
                snapshot = self._supervisor_resource_liveness(supervisor)
            if snapshot.resources_owned:
                pending = True
                continue
            with self._condition:
                self._retained_supervisors = [
                    item
                    for item in self._retained_supervisors
                    if item is not supervisor
                ]
        return pending

    def _complete_request_locked(
        self,
        state: _RequestState,
        *,
        result: object = _NO_RESULT,
        error: BaseException | None = None,
    ) -> None:
        if state.done.is_set():
            return
        state.result = result
        state.error = error
        state.done.set()
        self._requests.pop(state.identity.request_id, None)
        state.request = None
        self._release_request_slot_locked(state)

    def _release_request_slot_locked(self, state: _RequestState) -> None:
        if state.slot_released:
            return
        state.slot_released = True
        if self._request_slots_in_use <= 0:
            raise TrustedReadServiceError(
                "The broker request-capacity accounting became inconsistent."
            )
        self._request_slots_in_use -= 1

    def _discard_operation_locked(self, operation: _OperationState) -> None:
        operation.status = "discarded"
        self._operations.pop(operation.operation_id, None)
        if self._coalesced.get(operation.coalesce_key) == operation.operation_id:
            self._coalesced.pop(operation.coalesce_key, None)
        self._compact_heap_locked()

    def _fail_queued_locked(
        self,
        cause: BaseException,
    ) -> None:
        for operation in list(self._operations.values()):
            if operation.status != "queued":
                continue
            for request_id in tuple(operation.subscribers):
                state = self._requests.get(request_id)
                if state is None:
                    continue
                error = TrustedReadSessionFailedError(
                    "A terminal trusted-reader failure invalidated this queued "
                    f"request ({type(cause).__name__}: {cause})."
                )
                error.__cause__ = cause
                self._complete_request_locked(state, error=error)
            operation.subscribers.clear()
            self._discard_operation_locked(operation)

    def _fail_all_locked(self, cause: BaseException) -> None:
        """Terminally complete every request and clear all broker accounting."""

        for state in tuple(self._requests.values()):
            error = TrustedReadSessionFailedError(
                "A fatal trusted read-service loop failure invalidated this "
                f"request ({type(cause).__name__}: {cause})."
            )
            error.__cause__ = cause
            self._complete_request_locked(state, error=error)

        for operation in self._operations.values():
            operation.cancel_underlying = True
            operation.supervisor_job = None
            operation.subscribers.clear()
            operation.status = "failed"
        self._operations.clear()
        self._coalesced.clear()
        self._heap.clear()
        self._active_operation = None
        self._foreground_dispatch_burst = 0
        self._control_cancel_job = None
        if self._requests or self._request_slots_in_use:
            raise TrustedReadServiceError(
                "Fatal broker cleanup left live request-capacity accounting."
            )

    @staticmethod
    def _terminal_session_error(error: BaseException) -> bool:
        # Broker-local cancellation/deadline outcomes do not describe helper
        # health.  For errors returned by Stage 3, mirror its protocol
        # taxonomy exactly: these five operation failures are the complete
        # reusable set and every other outcome retires this broker session.
        if isinstance(
            error,
            (
                TrustedReadRequestCancelledError,
                TrustedReadRequestDeadlineError,
            ),
        ):
            return False
        return not isinstance(
            error,
            (
                TrustedLiveSqlRejectedError,
                TrustedLiveQueryError,
                TrustedLiveResultLimitError,
                TrustedLiveBusyTimeoutError,
                TrustedLiveCancelledError,
            ),
        )


__all__ = [
    "SNAPSHOT_FALLBACK_MODE",
    "TRUSTED_LIVE_MODE",
    "TrustedLiveReadService",
    "TrustedReadOperation",
    "TrustedReadPriority",
    "TrustedReadQueueFullError",
    "TrustedReadRequest",
    "TrustedReadRequestCancelledError",
    "TrustedReadRequestDeadlineError",
    "TrustedReadRequestIdentity",
    "TrustedReadServiceClosedError",
    "TrustedReadServiceError",
    "TrustedReadServiceLiveness",
    "TrustedReadSessionFailedError",
]
