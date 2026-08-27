"""Contained process-tree supervision for the production qPlot GUI.

The supervisor is deliberately independent of Qt.  It creates an OS-owned
containment boundary before the GUI can import Qt or start helpers, retains the
exact child/tree ownership objects until the complete tree is gone, and
enforces the single deadline accepted from the GUI.  No process is opened
later by PID and there is no cancellation message after the deadline is armed.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import json
import math
import os
import secrets
import select
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from types import TracebackType
from typing import Any

_FORCED_SHUTDOWN_EXIT_CODE = 70
_BOOTSTRAP_ENVIRONMENT_KEY = "_QPLOT_SHUTDOWN_SUPERVISOR_V1"
_API_LAUNCHER_BOOTSTRAP_ENVIRONMENT_KEY = "_QPLOT_API_LAUNCHER_V1"
_GUI_CHILD_SENTINEL = "_qplot_supervised_gui_v1"
_API_LAUNCHER_SENTINEL = "_qplot_public_api_launcher_v1"
_LOOPBACK_HOST = "127.0.0.1"
_DEFAULT_STARTUP_TIMEOUT_SECONDS = 5.0
_OBSERVATION_INTERVAL_SECONDS = 0.01
_TERMINATION_RETRY_SECONDS = 0.005
_DIAGNOSTIC_PUBLISH_BUDGET_SECONDS = 0.025
_API_RESULT_IO_TIMEOUT_SECONDS = 1.0
_API_RESULT_MAX_BYTES = 256 * 1024
_TERMINATION_DIAGNOSTIC_LIMIT = 64
_POSIX_KILL_STABILITY_PASSES = 3

_PROTOCOL_MAGIC = b"QPLTSV1\0"
_PROTOCOL_VERSION = 1
_HELLO = 1
_STARTUP_READY = 2
_ARM = 3
_ARM_ACK = 4
_API_LAUNCHER_HELLO = 5
_API_LAUNCHER_READY = 6
_API_LAUNCHER_RESULT = 7
_API_LAUNCHER_CANCEL = 8
_NONCE_BYTES = 32
_AUTHENTICATION_BYTES = 32

# Every protocol record has one strict, fixed size.  The eight-byte payload is
# an unsigned PID for HELLO/READY and an IEEE-754 deadline for ARM/ARM_ACK.
_FRAME_BODY = struct.Struct("!8sBB6x32s32s8s")
_FRAME_SIZE = _FRAME_BODY.size + _AUTHENTICATION_BYTES
_PID_PAYLOAD = struct.Struct("!Q")
_DEADLINE_PAYLOAD = struct.Struct("!d")
_API_RESULT_HEADER = struct.Struct("!8sBB6x32sI")


class ShutdownSupervisorError(RuntimeError):
    """A launcher bootstrap or authenticated-protocol operation failed."""


@dataclass(frozen=True)
class _Bootstrap:
    host: str
    port: int
    authentication_key: bytes
    session_nonce: bytes
    startup_timeout: float
    database_path: str | None


@dataclass(frozen=True)
class _ApiLauncherBootstrap:
    host: str
    port: int
    authentication_key: bytes
    session_nonce: bytes
    startup_deadline: float
    caller_pid: int
    child_argv: tuple[str, ...]
    database_path: str | None


@dataclass(frozen=True)
class _SupervisionOutcome:
    return_code: int
    forced: bool = False
    signal_number: int | None = None
    diagnostics: tuple[str, ...] = ()


def _outcome_with_diagnostics(
    outcome: _SupervisionOutcome,
    *diagnostics: str,
) -> _SupervisionOutcome:
    return _SupervisionOutcome(
        outcome.return_code,
        forced=outcome.forced,
        signal_number=outcome.signal_number,
        diagnostics=outcome.diagnostics + tuple(diagnostics),
    )


@dataclass(frozen=True)
class _CommittedArm:
    """The immutable authenticated ARM record installed by one assignment."""

    deadline: float
    arm_nonce: bytes
    deadline_payload: bytes


@dataclass
class _ArmedState:
    """Holder whose committed-record assignment is the irreversible transition."""

    committed: _CommittedArm | None = None

    @property
    def deadline(self) -> float | None:
        record = self.committed
        return None if record is None else record.deadline

    @property
    def arm_nonce(self) -> bytes | None:
        record = self.committed
        return None if record is None else record.arm_nonce

    @property
    def deadline_payload(self) -> bytes | None:
        record = self.committed
        return None if record is None else record.deadline_payload


@dataclass
class _ReadyState:
    """Whether authenticated STARTUP_READY has been sent successfully."""

    committed: bool = False


@dataclass
class _ApiLauncherReadyState:
    """Retain the caller channel across a post-READY interruption."""

    channel: socket.socket | None = None
    committed: bool = False


@dataclass
class _LauncherSignalState:
    """The first ordinary termination signal delivered to the launcher."""

    signal_number: int | None = None
    claimed: bool = False
    cancellation_event: threading.Event | None = None
    cancellation_claimed: bool = False


@dataclass
class _ApiCallerControlState:
    """The immutable caller-cancellation/final-outcome transition."""

    cancellation_event: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _cancellation_diagnostic: str | None = None
    _final_outcome_committed: bool = False

    def commit_cancellation(self, diagnostic: str) -> bool:
        with self._lock:
            if (
                self._final_outcome_committed
                or self._cancellation_diagnostic is not None
            ):
                return False
            self._cancellation_diagnostic = diagnostic
            self.cancellation_event.set()
            return True

    def commit_final_outcome(self) -> None:
        with self._lock:
            self._final_outcome_committed = True

    @property
    def cancellation_diagnostic(self) -> str | None:
        with self._lock:
            return self._cancellation_diagnostic


@dataclass
class _ApiLauncherResultObservation:
    """One result reader's outcome plus process-death EOF observation."""

    outcome: _SupervisionOutcome | None = None
    error: BaseException | None = None
    eof_observed: bool = False
    completed: threading.Event = field(default_factory=threading.Event)


class _CancellationWorkerState(Enum):
    """One committed lifecycle for the caller-to-launcher cancellation owner."""

    NOT_STARTED = "not-started"
    STARTING = "starting"
    RUNNING = "running"
    EOF_FALLBACK = "eof-fallback"
    COMPLETE = "complete"


@dataclass
class _ApiLauncherCancellationSender:
    """Own the caller's sole irreversible control-direction operation.

    Only the dedicated sender thread writes cancellation bytes.  Requesters
    merely set an event and ensure that worker exists, so a caller signal
    cannot leave a partially sent record owned by an unwinding main thread.
    The worker keeps its local byte offset across every absorbed
    ``BaseException`` and resolves exactly once to either a complete
    authenticated frame or write-side EOF.
    """

    channel: socket.socket
    frame: bytes
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _error_lock: threading.Lock = field(default_factory=threading.Lock)
    _worker_condition: threading.Condition = field(
        default_factory=threading.Condition,
    )
    _requested: threading.Event = field(default_factory=threading.Event)
    _completed: threading.Event = field(default_factory=threading.Event)
    _worker_entered: threading.Event = field(default_factory=threading.Event)
    _worker_state: _CancellationWorkerState = _CancellationWorkerState.NOT_STARTED
    _worker_owner_ident: int | None = None
    _worker_creation_attempted: bool = False
    _thread: threading.Thread | None = None
    _start_attempted: bool = False
    _offset: int = 0
    _started_at: float | None = None
    _deadline: float | None = None
    _first_error: BaseException | None = None
    diagnostic: str | None = None

    def request(self) -> None:
        """Request cancellation without allowing control flow to escape."""

        while True:
            try:
                self._requested.set()
                self._ensure_worker_started()
                return
            except BaseException as error:
                self._remember_error(error)

    def _ensure_worker_started(self) -> None:
        owner_ident = threading.get_ident()
        while True:
            try:
                _public_api_cancellation_boundary("worker_lookup")
            except BaseException as error:
                self._remember_error(error)
                continue

            fallback_owner = False
            worker_to_start: threading.Thread | None = None
            resolve_committed_start: threading.Thread | None = None
            try:
                with self._worker_condition:
                    state = self._worker_state
                    if state in {
                        _CancellationWorkerState.RUNNING,
                        _CancellationWorkerState.COMPLETE,
                    }:
                        return
                    if state is _CancellationWorkerState.EOF_FALLBACK:
                        if self._worker_owner_ident == owner_ident:
                            fallback_owner = True
                        else:
                            try:
                                self._worker_condition.wait(
                                    _OBSERVATION_INTERVAL_SECONDS
                                )
                            except BaseException as error:
                                self._remember_error(error)
                            continue
                    elif state is _CancellationWorkerState.STARTING:
                        if self._worker_owner_ident == owner_ident:
                            pass
                        else:
                            try:
                                self._worker_condition.wait(
                                    _OBSERVATION_INTERVAL_SECONDS
                                )
                            except BaseException as error:
                                self._remember_error(error)
                            continue
                    else:
                        # Ownership is committed before STARTING.  An
                        # interruption after either assignment therefore
                        # leaves the same requester able to resume, while all
                        # other requesters wait for that durable owner.
                        committed_owner = self._worker_owner_ident
                        if committed_owner is None:
                            self._worker_owner_ident = owner_ident
                        elif committed_owner != owner_ident:
                            try:
                                self._worker_condition.wait(
                                    _OBSERVATION_INTERVAL_SECONDS
                                )
                            except BaseException as error:
                                self._remember_error(error)
                            continue
                        self._worker_state = _CancellationWorkerState.STARTING

                    if fallback_owner:
                        self._worker_condition.notify_all()
                        continue_startup = False
                    else:
                        continue_startup = True

                    if continue_startup and self._thread is None:
                        if self._worker_creation_attempted:
                            fallback_owner = self._commit_worker_eof_fallback_locked()
                            self._worker_condition.notify_all()
                            continue_startup = False
                        else:
                            while True:
                                try:
                                    _public_api_cancellation_boundary("worker_creation")
                                    break
                                except BaseException as error:
                                    self._remember_error(error)

                            # The committed attempt prevents a factory call
                            # from being repeated if control flow is lost
                            # immediately before or after its side effect.
                            self._worker_creation_attempted = True
                            try:
                                worker = _new_public_api_cancellation_worker(self)
                            except BaseException as error:
                                self._remember_error(error)
                                fallback_owner = (
                                    self._commit_worker_eof_fallback_locked()
                                )
                                self._worker_condition.notify_all()
                                continue_startup = False
                            else:
                                # The object itself is the durable result of
                                # creation.  A post-assignment interruption is
                                # resumed without constructing another worker.
                                self._thread = worker

                    assigned_worker = self._thread
                    if continue_startup and assigned_worker is not None:
                        while True:
                            try:
                                _public_api_cancellation_boundary("worker_assignment")
                                break
                            except BaseException as error:
                                self._remember_error(error)

                        if self._start_attempted:
                            # The only permitted start call was already
                            # committed.  Never retry it: prove entry outside
                            # this lock or fail closed with the unique EOF
                            # owner.
                            resolve_committed_start = assigned_worker
                        else:
                            while True:
                                try:
                                    _public_api_cancellation_boundary("worker_start")
                                    break
                                except BaseException as error:
                                    self._remember_error(error)

                            # Commit before the only Thread.start call.  If an
                            # interruption lands after this assignment, retry
                            # resolves the uncertain attempt and never invokes
                            # start again.
                            self._start_attempted = True
                            worker_to_start = assigned_worker
            except BaseException as error:
                self._remember_error(error)
                continue

            if fallback_owner:
                self._fail_closed_without_worker()
                return

            started = False
            attempted_worker = worker_to_start or resolve_committed_start
            if worker_to_start is not None:
                # STARTING excludes every other requester, but the condition
                # is released before Thread.start because the worker's socket
                # operation must never run under a lifecycle lock.
                try:
                    worker_to_start.start()
                except BaseException as error:
                    self._remember_error(error)

            if attempted_worker is not None:
                while True:
                    try:
                        started = (
                            attempted_worker.ident is not None
                            or self._worker_entered.is_set()
                        )
                        break
                    except BaseException as error:
                        self._remember_error(error)
                if not started:
                    try:
                        self._worker_entered.wait(_OBSERVATION_INTERVAL_SECONDS)
                    except BaseException as error:
                        self._remember_error(error)
                    while True:
                        try:
                            started = (
                                attempted_worker.ident is not None
                                or self._worker_entered.is_set()
                            )
                            break
                        except BaseException as error:
                            self._remember_error(error)

            if started:
                while True:
                    try:
                        _public_api_cancellation_boundary("worker_start_commit")
                        break
                    except BaseException as error:
                        self._remember_error(error)

            while True:
                try:
                    with self._worker_condition:
                        # A fast worker may already have resolved the socket
                        # operation and committed COMPLETE before start returns.
                        if self._worker_state is _CancellationWorkerState.STARTING:
                            # The worker entry handshake uses this same
                            # condition.  Recheck it while committing the
                            # terminal startup direction so EOF can never race
                            # a late sender into socket I/O.
                            if started or self._worker_entered.is_set():
                                self._worker_state = _CancellationWorkerState.RUNNING
                            else:
                                fallback_owner = (
                                    self._commit_worker_eof_fallback_locked()
                                )
                        self._worker_condition.notify_all()
                    break
                except BaseException as error:
                    self._remember_error(error)

            if fallback_owner:
                self._fail_closed_without_worker()
            return

    def _commit_worker_eof_fallback_locked(self) -> bool:
        """Commit the unique synchronous EOF owner under the start condition."""

        if self._worker_state is _CancellationWorkerState.EOF_FALLBACK:
            return False
        while True:
            try:
                _public_api_cancellation_boundary("worker_eof_fallback_commit")
                self._worker_state = _CancellationWorkerState.EOF_FALLBACK
                return True
            except BaseException as error:
                self._remember_error(error)

    def _remember_error(self, error: BaseException) -> None:
        while True:
            try:
                with self._error_lock:
                    if self._first_error is None:
                        self._first_error = error
                return
            except BaseException:
                # Diagnostic retention must not become a new caller-control
                # escape path.  The committed lifecycle remains authoritative.
                continue

    def _fail_closed_without_worker(self) -> None:
        while True:
            try:
                _public_api_cancellation_boundary("write_side_shutdown")
                _public_api_cancellation_shutdown(self.channel)
                break
            except OSError:
                break
            except BaseException as error:
                self._remember_error(error)
        self._finish()

    def _run(self) -> None:
        while True:
            try:
                with self._worker_condition:
                    self._worker_entered.set()
                    self._worker_condition.notify_all()
                    if self._worker_state in {
                        _CancellationWorkerState.EOF_FALLBACK,
                        _CancellationWorkerState.COMPLETE,
                    }:
                        return
                break
            except BaseException as error:
                self._remember_error(error)
        try:
            while True:
                try:
                    self._requested.wait()
                    break
                except BaseException as error:
                    self._remember_error(error)
                if self._requested.is_set():
                    break
            _send_public_api_cancellation_record(self)
        except BaseException as error:
            self._remember_error(error)
            self._fail_closed_without_worker()

    def _finish(self) -> None:
        while True:
            try:
                with self._worker_condition:
                    if self._worker_state is _CancellationWorkerState.STARTING:
                        self._worker_condition.wait(_OBSERVATION_INTERVAL_SECONDS)
                        continue
                break
            except BaseException as error:
                self._remember_error(error)
        while True:
            try:
                _public_api_cancellation_boundary("diagnostic_construction")
                if self._first_error is not None:
                    self.diagnostic = _exact_error(
                        "public-API launcher cancellation",
                        self._first_error,
                    )
                break
            except BaseException as error:
                self._remember_error(error)
        while True:
            try:
                self._completed.set()
                break
            except BaseException as error:
                self._remember_error(error)
        while True:
            try:
                with self._worker_condition:
                    self._worker_state = _CancellationWorkerState.COMPLETE
                    self._worker_condition.notify_all()
                return
            except BaseException as error:
                self._remember_error(error)

    @property
    def requested(self) -> bool:
        return self._requested.is_set()

    @property
    def completed(self) -> threading.Event:
        return self._completed


@dataclass
class _PendingCallerControlFlow:
    """The exact first caller exception retained across containment cleanup."""

    error: BaseException
    traceback: TracebackType | None


class _InterruptGuardTransition(Enum):
    """Separate attempted and confirmed signal-handler side effects."""

    NOT_ATTEMPTED = "not-attempted"
    ATTEMPTED = "attempted"
    CONFIRMED = "confirmed"


@dataclass
class _CallerCleanupInterruptGuard:
    """Temporarily absorb and count later SIGINTs during containment cleanup."""

    _original_handler: tuple[Any] | None = None
    _absorber: Any = field(init=False, repr=False)
    _installation: _InterruptGuardTransition = _InterruptGuardTransition.NOT_ATTEMPTED
    _restoration: _InterruptGuardTransition = _InterruptGuardTransition.NOT_ATTEMPTED
    _not_applicable: bool = False
    absorbed_sigints: int = 0

    def __post_init__(self) -> None:
        # Bound-method lookup creates a new wrapper each time.  Retain the
        # exact object installed with signal.signal so verification can use
        # identity rather than an ambiguous equality comparison.
        self._absorber = self._absorb_sigint

    @property
    def previous_handler(self) -> Any:
        retained = self._original_handler
        return None if retained is None else retained[0]

    @property
    def active(self) -> bool:
        return (
            self._installation is _InterruptGuardTransition.CONFIRMED
            and self._restoration is not _InterruptGuardTransition.CONFIRMED
        )

    @property
    def engagement_complete(self) -> bool:
        return self.active or self._not_applicable

    def engage(self) -> None:
        if self.engagement_complete or (
            self._restoration is _InterruptGuardTransition.CONFIRMED
        ):
            return
        if threading.current_thread() is not threading.main_thread():
            self._not_applicable = True
            return

        if self._original_handler is None:
            _public_api_interrupt_guard_boundary("original_capture_before")
            original_handler = signal.getsignal(signal.SIGINT)
            _public_api_interrupt_guard_boundary("original_capture_after")
            # One tuple assignment is the immutable capture commit.  Retry
            # paths never replace this exact caller-owned object.
            self._original_handler = (original_handler,)

        _public_api_interrupt_guard_boundary("installation_verify_before")
        current_handler = signal.getsignal(signal.SIGINT)
        if current_handler is self._absorber:
            self._installation = _InterruptGuardTransition.CONFIRMED
            return

        self._installation = _InterruptGuardTransition.ATTEMPTED
        _public_api_interrupt_guard_boundary("installation_signal_before")
        signal.signal(signal.SIGINT, self._absorber)
        _public_api_interrupt_guard_boundary("installation_signal_after")
        current_handler = signal.getsignal(signal.SIGINT)
        if current_handler is not self._absorber:
            raise ShutdownSupervisorError(
                "public-API caller cleanup SIGINT guard installation "
                "could not be confirmed"
            )
        self._installation = _InterruptGuardTransition.CONFIRMED

    def _absorb_sigint(self, _signum: int, _frame: Any) -> None:
        self.absorbed_sigints += 1

    def restore(self) -> None:
        if self._not_applicable or (
            self._restoration is _InterruptGuardTransition.CONFIRMED
        ):
            return
        retained = self._original_handler
        if retained is None:
            return
        if threading.current_thread() is not threading.main_thread():
            return

        original_handler = retained[0]
        _public_api_interrupt_guard_boundary("restoration_verify_before")
        current_handler = signal.getsignal(signal.SIGINT)
        if current_handler is original_handler:
            self._restoration = _InterruptGuardTransition.CONFIRMED
            return

        self._restoration = _InterruptGuardTransition.ATTEMPTED
        _public_api_interrupt_guard_boundary("restoration_signal_before")
        signal.signal(signal.SIGINT, original_handler)
        _public_api_interrupt_guard_boundary("restoration_signal_after")
        current_handler = signal.getsignal(signal.SIGINT)
        if current_handler is not original_handler:
            raise ShutdownSupervisorError(
                "public-API caller cleanup SIGINT handler restoration "
                "could not be confirmed"
            )
        self._restoration = _InterruptGuardTransition.CONFIRMED


@dataclass
class _LauncherSignalGuards:
    """Signal dispositions and mask state replaced for one supervision run."""

    previous_handlers: dict[int, Any]
    previous_mask: set[signal.Signals] | None = None


class _LauncherSignalReceived(BaseException):
    """Control-flow marker raised after a launcher signal has been recorded."""

    def __init__(self, signal_number: int):
        super().__init__(signal_number)
        self.signal_number = signal_number


class _LauncherCancellationReceived(BaseException):
    """Control-flow marker for an immutable public-API cancellation."""


@dataclass
class _PosixProcessGroup:
    """Identity-bound POSIX session/process group led by the unreaped child."""

    pgid: int | None = None

    def popen_options(self) -> dict[str, Any]:
        return {"start_new_session": True}

    def assign(self, child: Any) -> None:
        expected_pgid = int(child.pid)
        # start_new_session establishes this identity in the child before
        # exec.  Retain it even if verification itself is interrupted/fails so
        # the launcher still has a bounded cleanup target.
        self.pgid = expected_pgid
        while True:
            try:
                actual_pgid = os.getpgid(expected_pgid)
                break
            except InterruptedError:
                continue
        if actual_pgid != expected_pgid:
            raise ShutdownSupervisorError(
                "shutdown launcher GUI child did not enter its dedicated "
                f"process group: expected {expected_pgid}, found {actual_pgid}"
            )

    def terminate(self) -> None:
        pgid = self.pgid
        if pgid is None:
            raise ShutdownSupervisorError(
                "shutdown launcher POSIX process group is not assigned"
            )
        os.killpg(pgid, signal.SIGKILL)

    def active(self) -> bool:
        pgid = self.pgid
        if pgid is None:
            return False
        while True:
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                return False
            except InterruptedError:
                continue
            except PermissionError:
                raise
            return True

    def close(self) -> None:
        return None


def _exact_error(context: str, error: BaseException) -> str:
    return f"{context} raised {type(error).__name__}: {error}"


def _report_launcher_failure(diagnostic: str) -> None:
    """Report a pre-arm failure without importing qPlot's logging stack."""

    try:
        print(f"qPlot shutdown launcher: {diagnostic}", file=sys.stderr, flush=True)
    except BaseException:
        # Reporting is best effort.  A broken inherited stderr must not turn a
        # protocol failure into a second exception.
        pass


def _launcher_termination_signals() -> tuple[int, ...]:
    names = (
        ("SIGINT", "SIGTERM")
        if os.name == "nt"
        else (
            "SIGHUP",
            "SIGINT",
            "SIGQUIT",
            "SIGTERM",
        )
    )
    return tuple(
        dict.fromkeys(
            int(signal_number)
            for name in names
            if (signal_number := getattr(signal, name, None)) is not None
        )
    )


def _record_launcher_signal(
    state: _LauncherSignalState,
    signum: int,
    _frame: Any,
) -> None:
    """Record only the first signal; ownership is released by the main loop."""

    if state.signal_number is None:
        state.signal_number = int(signum)


def _install_launcher_signal_guards(
    state: _LauncherSignalState,
) -> _LauncherSignalGuards:
    """Protect spawn and establish explicit POSIX child-reaping semantics."""

    guards = _LauncherSignalGuards(previous_handlers={})
    try:
        for signal_number in _launcher_termination_signals():
            guards.previous_handlers[signal_number] = signal.getsignal(signal_number)

            def handler(signum: int, frame: Any, *, _state=state) -> None:
                _record_launcher_signal(_state, signum, frame)

            signal.signal(signal_number, handler)
        if os.name != "nt" and hasattr(signal, "SIGCHLD"):
            signal_number = int(signal.SIGCHLD)
            guards.previous_handlers[signal_number] = signal.getsignal(signal_number)
            signal.signal(signal_number, signal.SIG_DFL)
        pthread_sigmask = getattr(signal, "pthread_sigmask", None)
        if os.name != "nt" and pthread_sigmask is not None:
            guards.previous_mask = pthread_sigmask(
                signal.SIG_UNBLOCK,
                set(_launcher_termination_signals()),
            )
    except BaseException:
        _restore_launcher_signal_guards(guards)
        raise
    return guards


def _restore_launcher_signal_guards(guards: _LauncherSignalGuards) -> None:
    pthread_sigmask = getattr(signal, "pthread_sigmask", None)
    if (
        os.name != "nt"
        and pthread_sigmask is not None
        and guards.previous_mask is not None
    ):
        try:
            pthread_sigmask(signal.SIG_SETMASK, guards.previous_mask)
        except (OSError, RuntimeError, ValueError):
            pass
    for signal_number, previous_handler in guards.previous_handlers.items():
        try:
            signal.signal(signal_number, previous_handler)
        except (OSError, RuntimeError, ValueError):
            pass


def _raise_if_launcher_signalled(state: _LauncherSignalState) -> None:
    if _claim_launcher_cancellation(state):
        raise _LauncherCancellationReceived
    signal_number = _claim_launcher_signal(state)
    if signal_number is not None:
        raise _LauncherSignalReceived(signal_number)


def _claim_launcher_cancellation(state: _LauncherSignalState) -> bool:
    """Claim the immutable API caller-cancellation event exactly once."""

    cancellation_event = state.cancellation_event
    if (
        cancellation_event is None
        or state.cancellation_claimed
        or not cancellation_event.is_set()
    ):
        return False
    state.cancellation_claimed = True
    return True


def _claim_launcher_signal(state: _LauncherSignalState) -> int | None:
    """Claim the first pending signal exactly once for cleanup/propagation."""

    signal_number = state.signal_number
    if signal_number is None or state.claimed:
        return None
    state.claimed = True
    return int(signal_number)


def _encode_frame(
    frame_type: int,
    *,
    authentication_key: bytes,
    session_nonce: bytes,
    message_nonce: bytes,
    payload: bytes,
) -> bytes:
    if len(authentication_key) != _AUTHENTICATION_BYTES:
        raise ValueError("supervisor authentication key has the wrong length")
    if len(session_nonce) != _NONCE_BYTES:
        raise ValueError("supervisor session nonce has the wrong length")
    if len(message_nonce) != _NONCE_BYTES:
        raise ValueError("supervisor message nonce has the wrong length")
    if len(payload) != 8:
        raise ValueError("supervisor frame payload has the wrong length")
    body = _FRAME_BODY.pack(
        _PROTOCOL_MAGIC,
        _PROTOCOL_VERSION,
        frame_type,
        session_nonce,
        message_nonce,
        payload,
    )
    authentication = hmac.digest(authentication_key, body, hashlib.sha256)
    return body + authentication


def _decode_frame(
    frame: bytes,
    *,
    authentication_key: bytes,
    session_nonce: bytes,
) -> tuple[int, bytes, bytes]:
    if len(frame) != _FRAME_SIZE:
        raise ShutdownSupervisorError(
            f"invalid supervisor frame length {len(frame)}; expected {_FRAME_SIZE}"
        )
    body = frame[: _FRAME_BODY.size]
    supplied_authentication = frame[_FRAME_BODY.size :]
    expected_authentication = hmac.digest(
        authentication_key,
        body,
        hashlib.sha256,
    )
    if not hmac.compare_digest(supplied_authentication, expected_authentication):
        raise ShutdownSupervisorError("invalid supervisor frame authentication")
    (
        magic,
        version,
        frame_type,
        supplied_session,
        message_nonce,
        payload,
    ) = _FRAME_BODY.unpack(body)
    if magic != _PROTOCOL_MAGIC:
        raise ShutdownSupervisorError("invalid supervisor frame magic")
    if version != _PROTOCOL_VERSION:
        raise ShutdownSupervisorError(
            f"unsupported supervisor protocol version {version}"
        )
    if not hmac.compare_digest(supplied_session, session_nonce):
        raise ShutdownSupervisorError("invalid supervisor session nonce")
    return frame_type, message_nonce, payload


def _encode_arm(
    authentication_key: bytes,
    session_nonce: bytes,
    arm_nonce: bytes,
    hard_deadline: float,
) -> bytes:
    """Encode one authenticated ARM record (private regression-test seam)."""

    deadline = float(hard_deadline)
    if not math.isfinite(deadline):
        raise ValueError("shutdown hard deadline must be finite")
    return _encode_frame(
        _ARM,
        authentication_key=authentication_key,
        session_nonce=session_nonce,
        message_nonce=arm_nonce,
        payload=_DEADLINE_PAYLOAD.pack(deadline),
    )


def _receive_exact(
    channel: socket.socket,
    size: int,
    *,
    deadline: float | None = None,
    signal_state: _LauncherSignalState | None = None,
) -> bytes:
    payload = bytearray()
    while len(payload) < size:
        if signal_state is not None:
            _raise_if_launcher_signalled(signal_state)
        if deadline is not None:
            remaining = _timeout_within(deadline)
            if remaining <= 0.0:
                raise TimeoutError("supervisor control-channel receive timed out")
            channel.settimeout(min(_OBSERVATION_INTERVAL_SECONDS, remaining))
        try:
            chunk = channel.recv(size - len(payload))
        except TimeoutError:
            if deadline is None:
                raise
            continue
        if not chunk:
            raise ShutdownSupervisorError(
                "supervisor control channel closed before a complete frame"
            )
        payload.extend(chunk)
    return bytes(payload)


def _timeout_within(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


class ShutdownSupervisorClient:
    """GUI-side endpoint for the launcher's single authenticated ARM message."""

    def __init__(self, bootstrap: _Bootstrap):
        self._bootstrap = bootstrap
        self._channel: socket.socket | None = None
        self._connected = False
        self._arm_attempted = False
        self._arm_acknowledged = False
        self._pending_arm_nonce: bytes | None = None
        self._pending_deadline_payload: bytes | None = None

    @classmethod
    def from_environment(cls) -> ShutdownSupervisorClient:
        """Consume the private launcher marker before Qt or helpers start."""

        encoded = os.environ.pop(_BOOTSTRAP_ENVIRONMENT_KEY, None)
        if encoded is None:
            raise ShutdownSupervisorError(
                "private shutdown-launcher bootstrap marker is missing"
            )
        try:
            raw = json.loads(encoded)
        except (TypeError, ValueError) as error:
            raise ShutdownSupervisorError(
                _exact_error("shutdown-launcher bootstrap decoding", error)
            ) from error
        expected_fields = {
            "version",
            "host",
            "port",
            "authentication_key",
            "session_nonce",
            "startup_timeout",
            "database_path",
        }
        if not isinstance(raw, dict) or set(raw) != expected_fields:
            raise ShutdownSupervisorError(
                "shutdown-launcher bootstrap fields are invalid"
            )
        if raw["version"] != _PROTOCOL_VERSION:
            raise ShutdownSupervisorError(
                "shutdown-launcher bootstrap version is unsupported"
            )
        host = raw["host"]
        port = raw["port"]
        startup_timeout = raw["startup_timeout"]
        database_path = raw["database_path"]
        if host != _LOOPBACK_HOST:
            raise ShutdownSupervisorError(
                "shutdown-launcher bootstrap endpoint is not loopback"
            )
        if type(port) is not int or not 0 < port < 65536:
            raise ShutdownSupervisorError("shutdown-launcher bootstrap port is invalid")
        if not isinstance(startup_timeout, (int, float)) or isinstance(
            startup_timeout, bool
        ):
            raise ShutdownSupervisorError(
                "shutdown-launcher bootstrap timeout is invalid"
            )
        startup_timeout = float(startup_timeout)
        if not math.isfinite(startup_timeout) or startup_timeout <= 0.0:
            raise ShutdownSupervisorError(
                "shutdown-launcher bootstrap timeout is invalid"
            )
        if database_path is not None and not isinstance(database_path, str):
            raise ShutdownSupervisorError(
                "shutdown-launcher database-path override is invalid"
            )
        try:
            authentication_key = bytes.fromhex(raw["authentication_key"])
            session_nonce = bytes.fromhex(raw["session_nonce"])
        except (TypeError, ValueError) as error:
            raise ShutdownSupervisorError(
                _exact_error("shutdown-launcher bootstrap nonce decoding", error)
            ) from error
        if len(authentication_key) != _AUTHENTICATION_BYTES:
            raise ShutdownSupervisorError(
                "shutdown-launcher bootstrap authentication key has the wrong length"
            )
        if len(session_nonce) != _NONCE_BYTES:
            raise ShutdownSupervisorError(
                "shutdown-launcher bootstrap session nonce has the wrong length"
            )
        return cls(
            _Bootstrap(
                host=host,
                port=port,
                authentication_key=authentication_key,
                session_nonce=session_nonce,
                startup_timeout=startup_timeout,
                database_path=database_path,
            )
        )

    @property
    def database_path(self) -> str | None:
        return self._bootstrap.database_path

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def arm_acknowledged(self) -> bool:
        return self._arm_acknowledged

    def connect(self) -> ShutdownSupervisorClient:
        """Complete authenticated startup before importing or creating Qt."""

        if self._connected:
            return self
        deadline = time.monotonic() + self._bootstrap.startup_timeout
        channel = socket.create_connection(
            (self._bootstrap.host, self._bootstrap.port),
            timeout=self._bootstrap.startup_timeout,
        )
        self._channel = channel
        hello_nonce = secrets.token_bytes(_NONCE_BYTES)
        hello = _encode_frame(
            _HELLO,
            authentication_key=self._bootstrap.authentication_key,
            session_nonce=self._bootstrap.session_nonce,
            message_nonce=hello_nonce,
            payload=_PID_PAYLOAD.pack(os.getpid()),
        )
        try:
            channel.settimeout(_timeout_within(deadline))
            channel.sendall(hello)
            ready = _receive_exact(channel, _FRAME_SIZE)
            frame_type, message_nonce, payload = _decode_frame(
                ready,
                authentication_key=self._bootstrap.authentication_key,
                session_nonce=self._bootstrap.session_nonce,
            )
            if frame_type != _STARTUP_READY:
                raise ShutdownSupervisorError(
                    f"unexpected supervisor startup frame type {frame_type}"
                )
            if not hmac.compare_digest(message_nonce, hello_nonce):
                raise ShutdownSupervisorError(
                    "supervisor startup acknowledgement nonce does not match"
                )
            if _PID_PAYLOAD.unpack(payload)[0] != os.getpid():
                raise ShutdownSupervisorError(
                    "supervisor startup acknowledgement PID does not match"
                )
        except BaseException:
            channel.close()
            self._channel = None
            raise
        channel.settimeout(None)
        self._connected = True
        return self

    def _require_channel(self) -> socket.socket:
        if not self._connected or self._channel is None:
            raise ShutdownSupervisorError("shutdown launcher is not connected")
        return self._channel

    def _send_arm(self, hard_deadline: float) -> bytes:
        """Send ARM and retain its nonce for a separately testable ACK read."""

        channel = self._require_channel()
        deadline = float(hard_deadline)
        if not math.isfinite(deadline):
            raise ValueError("shutdown hard deadline must be finite")
        arm_nonce = secrets.token_bytes(_NONCE_BYTES)
        deadline_payload = _DEADLINE_PAYLOAD.pack(deadline)
        frame = _encode_arm(
            self._bootstrap.authentication_key,
            self._bootstrap.session_nonce,
            arm_nonce,
            deadline,
        )
        channel.settimeout(_timeout_within(deadline))
        channel.sendall(frame)
        self._pending_arm_nonce = arm_nonce
        self._pending_deadline_payload = deadline_payload
        return arm_nonce

    def _receive_arm_ack(self, hard_deadline: float) -> None:
        """Validate the ACK for the most recently sent ARM record."""

        channel = self._require_channel()
        arm_nonce = self._pending_arm_nonce
        deadline_payload = self._pending_deadline_payload
        if arm_nonce is None or deadline_payload is None:
            raise ShutdownSupervisorError("no ARM record is awaiting acknowledgement")
        channel.settimeout(_timeout_within(float(hard_deadline)))
        acknowledgement = _receive_exact(channel, _FRAME_SIZE)
        frame_type, message_nonce, payload = _decode_frame(
            acknowledgement,
            authentication_key=self._bootstrap.authentication_key,
            session_nonce=self._bootstrap.session_nonce,
        )
        if frame_type != _ARM_ACK:
            raise ShutdownSupervisorError(
                f"unexpected supervisor ARM acknowledgement type {frame_type}"
            )
        if not hmac.compare_digest(message_nonce, arm_nonce):
            raise ShutdownSupervisorError(
                "supervisor ARM acknowledgement nonce does not match"
            )
        if not hmac.compare_digest(payload, deadline_payload):
            raise ShutdownSupervisorError(
                "supervisor ARM acknowledgement deadline does not match"
            )

    def arm(self, hard_deadline: float) -> str | None:
        """Send the sole ARM request and report exact acknowledgement failure."""

        if self._arm_attempted:
            return "process shutdown launcher ARM was already attempted"
        self._arm_attempted = True
        try:
            self._send_arm(hard_deadline)
        except BaseException as error:
            return _exact_error("process shutdown launcher ARM send", error)
        try:
            self._receive_arm_ack(hard_deadline)
        except BaseException as error:
            # The launcher installs its immutable deadline before attempting
            # the ACK.  Therefore an ACK failure means external state is
            # unknown, not that the launcher is safely disarmed.
            return _exact_error(
                "process shutdown launcher ARM acknowledgement",
                error,
            )
        self._arm_acknowledged = True
        return None

    def close(self) -> None:
        """Close the local endpoint; after ARM this cannot cancel the deadline."""

        channel = self._channel
        self._channel = None
        self._connected = False
        if channel is not None:
            try:
                channel.close()
            except OSError:
                pass


def _bootstrap_payload(
    listener: socket.socket,
    *,
    authentication_key: bytes,
    session_nonce: bytes,
    startup_timeout: float,
    database_path: str | os.PathLike[str] | None,
) -> str:
    address = listener.getsockname()
    path_override = None if database_path is None else os.fsdecode(database_path)
    return json.dumps(
        {
            "version": _PROTOCOL_VERSION,
            "host": _LOOPBACK_HOST,
            "port": int(address[1]),
            "authentication_key": authentication_key.hex(),
            "session_nonce": session_nonce.hex(),
            "startup_timeout": startup_timeout,
            "database_path": path_override,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _api_launcher_bootstrap_payload(
    listener: socket.socket,
    *,
    authentication_key: bytes,
    session_nonce: bytes,
    startup_deadline: float,
    caller_pid: int,
    child_argv: Sequence[str | os.PathLike[str]],
    database_path: str | os.PathLike[str] | None,
) -> str:
    """Serialize the caller/launcher channel without inheriting a handle."""

    address = listener.getsockname()
    path_override = None if database_path is None else os.fsdecode(database_path)
    return json.dumps(
        {
            "version": _PROTOCOL_VERSION,
            "host": _LOOPBACK_HOST,
            "port": int(address[1]),
            "authentication_key": authentication_key.hex(),
            "session_nonce": session_nonce.hex(),
            "startup_deadline": float(startup_deadline),
            "caller_pid": int(caller_pid),
            "child_argv": [os.fsdecode(argument) for argument in child_argv],
            "database_path": path_override,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _api_launcher_bootstrap_from_environment() -> _ApiLauncherBootstrap:
    """Consume and strictly validate the public API launcher's private state."""

    encoded = os.environ.pop(_API_LAUNCHER_BOOTSTRAP_ENVIRONMENT_KEY, None)
    if encoded is None:
        raise ShutdownSupervisorError(
            "private public-API launcher bootstrap marker is missing"
        )
    try:
        raw = json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise ShutdownSupervisorError(
            _exact_error("public-API launcher bootstrap decoding", error)
        ) from error
    expected_fields = {
        "version",
        "host",
        "port",
        "authentication_key",
        "session_nonce",
        "startup_deadline",
        "caller_pid",
        "child_argv",
        "database_path",
    }
    if not isinstance(raw, dict) or set(raw) != expected_fields:
        raise ShutdownSupervisorError(
            "public-API launcher bootstrap fields are invalid"
        )
    if raw["version"] != _PROTOCOL_VERSION:
        raise ShutdownSupervisorError(
            "public-API launcher bootstrap version is unsupported"
        )
    host = raw["host"]
    port = raw["port"]
    startup_deadline = raw["startup_deadline"]
    caller_pid = raw["caller_pid"]
    child_argv = raw["child_argv"]
    database_path = raw["database_path"]
    if host != _LOOPBACK_HOST:
        raise ShutdownSupervisorError(
            "public-API launcher bootstrap endpoint is not loopback"
        )
    if type(port) is not int or not 0 < port < 65536:
        raise ShutdownSupervisorError("public-API launcher bootstrap port is invalid")
    if not isinstance(startup_deadline, (int, float)) or isinstance(
        startup_deadline, bool
    ):
        raise ShutdownSupervisorError(
            "public-API launcher bootstrap deadline is invalid"
        )
    startup_deadline = float(startup_deadline)
    if not math.isfinite(startup_deadline):
        raise ShutdownSupervisorError(
            "public-API launcher bootstrap deadline is invalid"
        )
    if type(caller_pid) is not int or caller_pid <= 0:
        raise ShutdownSupervisorError("public-API launcher caller PID is invalid")
    if (
        not isinstance(child_argv, list)
        or not child_argv
        or not all(isinstance(argument, str) for argument in child_argv)
    ):
        raise ShutdownSupervisorError("public-API launcher child arguments are invalid")
    if database_path is not None and not isinstance(database_path, str):
        raise ShutdownSupervisorError(
            "public-API launcher database-path override is invalid"
        )
    try:
        authentication_key = bytes.fromhex(raw["authentication_key"])
        session_nonce = bytes.fromhex(raw["session_nonce"])
    except (TypeError, ValueError) as error:
        raise ShutdownSupervisorError(
            _exact_error("public-API launcher bootstrap nonce decoding", error)
        ) from error
    if len(authentication_key) != _AUTHENTICATION_BYTES:
        raise ShutdownSupervisorError(
            "public-API launcher authentication key has the wrong length"
        )
    if len(session_nonce) != _NONCE_BYTES:
        raise ShutdownSupervisorError(
            "public-API launcher session nonce has the wrong length"
        )
    return _ApiLauncherBootstrap(
        host=host,
        port=port,
        authentication_key=authentication_key,
        session_nonce=session_nonce,
        startup_deadline=startup_deadline,
        caller_pid=caller_pid,
        child_argv=tuple(child_argv),
        database_path=database_path,
    )


def _encode_api_launcher_outcome(
    outcome: _SupervisionOutcome,
    *,
    authentication_key: bytes,
    session_nonce: bytes,
) -> bytes:
    payload = json.dumps(
        {
            "return_code": int(outcome.return_code),
            "forced": bool(outcome.forced),
            "signal_number": outcome.signal_number,
            "diagnostics": list(outcome.diagnostics),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(payload) > _API_RESULT_MAX_BYTES:
        raise ShutdownSupervisorError(
            "public-API launcher outcome exceeds the bounded result size"
        )
    header = _API_RESULT_HEADER.pack(
        _PROTOCOL_MAGIC,
        _PROTOCOL_VERSION,
        _API_LAUNCHER_RESULT,
        session_nonce,
        len(payload),
    )
    authentication = hmac.digest(
        authentication_key,
        header + payload,
        hashlib.sha256,
    )
    return header + payload + authentication


def _decode_api_launcher_outcome(
    frame: bytes,
    *,
    authentication_key: bytes,
    session_nonce: bytes,
) -> _SupervisionOutcome:
    minimum_size = _API_RESULT_HEADER.size + _AUTHENTICATION_BYTES
    if len(frame) < minimum_size:
        raise ShutdownSupervisorError("public-API launcher result frame is incomplete")
    header = frame[: _API_RESULT_HEADER.size]
    magic, version, frame_type, supplied_session, payload_size = (
        _API_RESULT_HEADER.unpack(header)
    )
    if magic != _PROTOCOL_MAGIC:
        raise ShutdownSupervisorError("invalid public-API launcher result magic")
    if version != _PROTOCOL_VERSION:
        raise ShutdownSupervisorError(
            f"unsupported public-API launcher result version {version}"
        )
    if frame_type != _API_LAUNCHER_RESULT:
        raise ShutdownSupervisorError(
            f"unexpected public-API launcher result type {frame_type}"
        )
    if not hmac.compare_digest(supplied_session, session_nonce):
        raise ShutdownSupervisorError(
            "invalid public-API launcher result session nonce"
        )
    if payload_size > _API_RESULT_MAX_BYTES:
        raise ShutdownSupervisorError(
            "public-API launcher result exceeds the bounded result size"
        )
    expected_size = minimum_size + payload_size
    if len(frame) != expected_size:
        raise ShutdownSupervisorError(
            "public-API launcher result frame length does not match its header"
        )
    payload_end = _API_RESULT_HEADER.size + payload_size
    payload = frame[_API_RESULT_HEADER.size : payload_end]
    supplied_authentication = frame[payload_end:]
    expected_authentication = hmac.digest(
        authentication_key,
        header + payload,
        hashlib.sha256,
    )
    if not hmac.compare_digest(
        supplied_authentication,
        expected_authentication,
    ):
        raise ShutdownSupervisorError(
            "invalid public-API launcher result authentication"
        )
    try:
        raw = json.loads(payload)
    except (TypeError, ValueError) as error:
        raise ShutdownSupervisorError(
            _exact_error("public-API launcher result decoding", error)
        ) from error
    expected_fields = {
        "return_code",
        "forced",
        "signal_number",
        "diagnostics",
    }
    if not isinstance(raw, dict) or set(raw) != expected_fields:
        raise ShutdownSupervisorError("public-API launcher result fields are invalid")
    return_code = raw["return_code"]
    forced = raw["forced"]
    signal_number = raw["signal_number"]
    diagnostics = raw["diagnostics"]
    if type(return_code) is not int:
        raise ShutdownSupervisorError("public-API launcher result status is invalid")
    if type(forced) is not bool:
        raise ShutdownSupervisorError(
            "public-API launcher result forced flag is invalid"
        )
    if signal_number is not None and (
        type(signal_number) is not int or signal_number <= 0
    ):
        raise ShutdownSupervisorError("public-API launcher result signal is invalid")
    if (
        not isinstance(diagnostics, list)
        or len(diagnostics) > _TERMINATION_DIAGNOSTIC_LIMIT
        or not all(isinstance(diagnostic, str) for diagnostic in diagnostics)
    ):
        raise ShutdownSupervisorError(
            "public-API launcher result diagnostics are invalid"
        )
    return _SupervisionOutcome(
        return_code,
        forced=forced,
        signal_number=signal_number,
        diagnostics=tuple(diagnostics),
    )


def _posix_child_exit_observed(
    child: Any,
    signal_state: _LauncherSignalState,
    *,
    deadline: float | None = None,
) -> bool | None:
    """Peek at a POSIX child without reaping its PID/process-group anchor."""

    options = os.WEXITED | os.WNOHANG | os.WNOWAIT
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            return None
        _raise_if_launcher_signalled(signal_state)
        try:
            result = os.waitid(os.P_PID, int(child.pid), options)
        except InterruptedError:
            continue
        except ChildProcessError:
            # SIGCHLD is forced to SIG_DFL before spawn and no other launcher
            # code reaps this child.  With no recorded return code, ECHILD is
            # an ownership failure: retain the launcher and keep retrying.
            return_code = getattr(child, "returncode", None)
            if return_code is not None:
                return True
            if deadline is None:
                time.sleep(_TERMINATION_RETRY_SECONDS)
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                return None
            time.sleep(min(_TERMINATION_RETRY_SECONDS, remaining))
            continue
        except OSError as error:
            if error.errno == errno.EINTR:
                continue
            raise
        if deadline is not None and time.monotonic() >= deadline:
            return None
        return result is not None and int(result.si_pid) == int(child.pid)


def _child_exit_observed(
    child: Any,
    signal_state: _LauncherSignalState,
) -> bool:
    if os.name != "nt":
        observed = _posix_child_exit_observed(child, signal_state)
        assert observed is not None
        return observed
    _raise_if_launcher_signalled(signal_state)
    try:
        child.wait(timeout=0.0)
    except subprocess.TimeoutExpired:
        return False
    return True


def _accept_child_channel(
    listener: socket.socket,
    child: Any,
    *,
    deadline: float,
    signal_state: _LauncherSignalState,
) -> tuple[socket.socket | None, bool]:
    listener.settimeout(_OBSERVATION_INTERVAL_SECONDS)
    while True:
        _raise_if_launcher_signalled(signal_state)
        if _child_exit_observed(child, signal_state):
            return None, True
        remaining = _timeout_within(deadline)
        if remaining <= 0.0:
            raise ShutdownSupervisorError("shutdown launcher child readiness timed out")
        listener.settimeout(min(_OBSERVATION_INTERVAL_SECONDS, remaining))
        try:
            channel, _address = listener.accept()
        except TimeoutError:
            continue
        return channel, False


def _authenticate_child_channel(
    channel: socket.socket,
    child: Any,
    *,
    authentication_key: bytes,
    session_nonce: bytes,
    deadline: float,
    signal_state: _LauncherSignalState,
    ready_state: _ReadyState,
) -> None:
    hello = _receive_exact(
        channel,
        _FRAME_SIZE,
        deadline=deadline,
        signal_state=signal_state,
    )
    frame_type, hello_nonce, payload = _decode_frame(
        hello,
        authentication_key=authentication_key,
        session_nonce=session_nonce,
    )
    if frame_type != _HELLO:
        raise ShutdownSupervisorError(
            f"unexpected supervisor child startup frame type {frame_type}"
        )
    claimed_pid = _PID_PAYLOAD.unpack(payload)[0]
    if claimed_pid != child.pid:
        raise ShutdownSupervisorError(
            f"supervisor child PID {claimed_pid} does not match {child.pid}"
        )
    ready = _encode_frame(
        _STARTUP_READY,
        authentication_key=authentication_key,
        session_nonce=session_nonce,
        message_nonce=hello_nonce,
        payload=_PID_PAYLOAD.pack(child.pid),
    )
    remaining = _timeout_within(deadline)
    if remaining <= 0.0:
        raise TimeoutError(
            "shutdown launcher child readiness deadline expired before READY"
        )
    channel.settimeout(remaining)
    channel.sendall(ready)
    if _timeout_within(deadline) <= 0.0:
        raise TimeoutError(
            "shutdown launcher child readiness deadline expired while sending READY"
        )
    ready_state.committed = True
    channel.settimeout(None)


def _observe_until_arm(
    channel: socket.socket,
    child: Any,
    *,
    authentication_key: bytes,
    session_nonce: bytes,
    armed_state: _ArmedState,
    signal_state: _LauncherSignalState,
) -> tuple[bool, str | None]:
    """Observe one pre-ARM frame while continuing to reap a normal child."""

    received = bytearray()
    channel.setblocking(False)
    while True:
        _raise_if_launcher_signalled(signal_state)
        try:
            child_exited = _child_exit_observed(child, signal_state)
        except BaseException as error:
            if isinstance(
                error,
                (_LauncherSignalReceived, _LauncherCancellationReceived),
            ):
                raise
            return (
                False,
                _exact_error("shutdown launcher child observation", error),
            )
        if child_exited:
            return True, None
        try:
            readable, _, _ = select.select(
                [channel],
                [],
                [],
                _OBSERVATION_INTERVAL_SECONDS,
            )
        except (OSError, ValueError) as error:
            return (
                False,
                _exact_error("shutdown launcher pre-ARM observation", error),
            )
        if not readable:
            continue
        try:
            chunk = channel.recv(_FRAME_SIZE - len(received))
        except BlockingIOError:
            continue
        except OSError as error:
            return (
                False,
                _exact_error("shutdown launcher pre-ARM receive", error),
            )
        if not chunk:
            return (
                False,
                "shutdown launcher control channel closed before ARM",
            )
        received.extend(chunk)
        if len(received) < _FRAME_SIZE:
            continue
        try:
            frame_type, arm_nonce, payload = _decode_frame(
                bytes(received),
                authentication_key=authentication_key,
                session_nonce=session_nonce,
            )
            if frame_type != _ARM:
                raise ShutdownSupervisorError(
                    f"unexpected pre-ARM frame type {frame_type}"
                )
            hard_deadline = _DEADLINE_PAYLOAD.unpack(payload)[0]
            candidate = _CommittedArm(hard_deadline, arm_nonce, payload)
            if not math.isfinite(hard_deadline):
                raise ShutdownSupervisorError("ARM deadline is not finite")
            # This assignment is the irreversible ARMED transition.  It is
            # deliberately before ACK allocation/encoding/send or any other
            # fallible protocol work.  Every enclosing exception handler tests
            # this field and therefore remains fail-closed for this deadline.
            armed_state.committed = candidate
        except (ShutdownSupervisorError, struct.error, ValueError) as error:
            return (
                False,
                _exact_error("shutdown launcher pre-ARM protocol", error),
            )
        return False, None


def _send_arm_acknowledgement(channel: socket.socket, acknowledgement: bytes) -> None:
    """Send a nonblocking ACK after the deadline state is installed.

    Failure is reported only after the already-armed child has exited or been
    terminated.  Raising here therefore records the exact send detail without
    weakening or delaying the immutable deadline.
    """

    channel.setblocking(False)
    # The fixed record is far smaller than a loopback socket's send buffer.
    sent = channel.send(acknowledgement)
    if sent != len(acknowledgement):
        raise ShutdownSupervisorError(
            "shutdown launcher ARM acknowledgement send was partial: "
            f"sent {sent} of {len(acknowledgement)} bytes"
        )


def _set_child_return_code(child: Any, status: int) -> int:
    return_code = os.waitstatus_to_exitcode(status)
    child.returncode = return_code
    return int(return_code)


def _record_termination_diagnostic(
    diagnostics: list[str],
    context: str,
    error: BaseException,
) -> None:
    try:
        diagnostic = _exact_error(context, error)
        if diagnostic in diagnostics:
            return
        if len(diagnostics) < _TERMINATION_DIAGNOSTIC_LIMIT - 1:
            diagnostics.append(diagnostic)
            return
        marker = (
            "shutdown launcher additional distinct termination diagnostics "
            f"omitted after {_TERMINATION_DIAGNOSTIC_LIMIT - 1} entries"
        )
        if marker not in diagnostics:
            diagnostics.append(marker)
    except BaseException:
        # Retaining ownership and retrying is more important than formatting a
        # diagnostic under memory pressure or another exceptional condition.
        pass


def _materialize_termination_diagnostics(
    initial_diagnostics: Sequence[str],
    deferred_context: str | None,
    deferred_error: BaseException | None,
) -> list[str]:
    """Allocate/format diagnostics only after the first tree-kill attempt."""

    diagnostics = list(initial_diagnostics)
    if deferred_context is not None and deferred_error is not None:
        _record_termination_diagnostic(
            diagnostics,
            deferred_context,
            deferred_error,
        )
    return diagnostics


def _outcome_from_posix_status(
    child: Any,
    status: int,
    *,
    termination_requested: bool,
    direct_exit_preceded_termination: bool = False,
    launcher_signal: int | None = None,
    diagnostics: Sequence[str] = (),
) -> _SupervisionOutcome:
    return_code = _set_child_return_code(child, status)
    if launcher_signal is not None:
        return _SupervisionOutcome(
            -int(launcher_signal),
            signal_number=int(launcher_signal),
            diagnostics=tuple(diagnostics),
        )
    forced = bool(
        termination_requested
        and not direct_exit_preceded_termination
        and os.WIFSIGNALED(status)
        and os.WTERMSIG(status) == signal.SIGKILL
    )
    if forced:
        return _SupervisionOutcome(
            _FORCED_SHUTDOWN_EXIT_CODE,
            forced=True,
            diagnostics=tuple(diagnostics),
        )
    signal_number = -return_code if return_code < 0 else None
    return _SupervisionOutcome(
        return_code,
        signal_number=signal_number,
        diagnostics=tuple(diagnostics),
    )


def _reap_terminated_posix_child(
    child: Any,
    *,
    termination_requested: bool,
    direct_exit_preceded_termination: bool,
    process_group: _PosixProcessGroup,
    launcher_signal: int | None = None,
    diagnostics: list[str] | None = None,
) -> _SupervisionOutcome:
    retained_diagnostics = [] if diagnostics is None else diagnostics
    while True:
        try:
            waited_pid, status = os.waitpid(child.pid, 0)
        except InterruptedError as error:
            _record_termination_diagnostic(
                retained_diagnostics,
                "shutdown launcher POSIX child reap",
                error,
            )
            continue
        except ChildProcessError as error:
            return_code = getattr(child, "returncode", None)
            if return_code is not None:
                if launcher_signal is not None:
                    outcome = _SupervisionOutcome(
                        -int(launcher_signal),
                        signal_number=int(launcher_signal),
                        diagnostics=tuple(retained_diagnostics),
                    )
                    break
                forced = bool(
                    termination_requested
                    and not direct_exit_preceded_termination
                    and launcher_signal is None
                    and return_code == -signal.SIGKILL
                )
                if forced:
                    outcome = _SupervisionOutcome(
                        _FORCED_SHUTDOWN_EXIT_CODE,
                        forced=True,
                        diagnostics=tuple(retained_diagnostics),
                    )
                else:
                    signal_number = -int(return_code) if int(return_code) < 0 else None
                    outcome = _SupervisionOutcome(
                        int(return_code),
                        signal_number=signal_number,
                        diagnostics=tuple(retained_diagnostics),
                    )
                break
            _record_termination_diagnostic(
                retained_diagnostics,
                "shutdown launcher POSIX child reap",
                error,
            )
            time.sleep(_TERMINATION_RETRY_SECONDS)
            continue
        except OSError as error:
            if error.errno == errno.EINTR:
                continue
            _record_termination_diagnostic(
                retained_diagnostics,
                "shutdown launcher POSIX child reap",
                error,
            )
            time.sleep(_TERMINATION_RETRY_SECONDS)
            continue
        except BaseException as error:
            _record_termination_diagnostic(
                retained_diagnostics,
                "shutdown launcher POSIX child reap",
                error,
            )
            time.sleep(_TERMINATION_RETRY_SECONDS)
            continue
        if waited_pid == child.pid:
            outcome = _outcome_from_posix_status(
                child,
                status,
                termination_requested=termination_requested,
                direct_exit_preceded_termination=(direct_exit_preceded_termination),
                launcher_signal=launcher_signal,
                diagnostics=retained_diagnostics,
            )
            break

    # The leader is now reaped, so its PGID must never be signalled again.  A
    # zero signal only observes orphan/zombie disappearance and cannot harm a
    # process in the extraordinarily unlikely event of immediate PGID reuse.
    while True:
        try:
            active = process_group.active()
        except BaseException as error:
            _record_termination_diagnostic(
                retained_diagnostics,
                "shutdown launcher POSIX process-group observation",
                error,
            )
            time.sleep(_TERMINATION_RETRY_SECONDS)
            continue
        if not active:
            break
        time.sleep(_OBSERVATION_INTERVAL_SECONDS)
    if tuple(retained_diagnostics) == outcome.diagnostics:
        return outcome
    return _SupervisionOutcome(
        outcome.return_code,
        forced=outcome.forced,
        signal_number=outcome.signal_number,
        diagnostics=tuple(retained_diagnostics),
    )


def _terminate_and_reap_posix_child(
    child: Any,
    process_group: _PosixProcessGroup,
    *,
    launcher_signal: int | None = None,
    initial_diagnostics: Sequence[str] = (),
    deferred_diagnostic_context: str | None = None,
    deferred_diagnostic_error: BaseException | None = None,
    child_exit_observed: bool = False,
) -> _SupervisionOutcome:
    diagnostics: list[str] | None = None
    termination_requested = False
    while True:
        try:
            # On a hard-deadline path this call is the first external action.
            # Formatting/reporting, polling, and reaping all happen later.
            process_group.terminate()
            termination_requested = True
            if diagnostics is None:
                diagnostics = _materialize_termination_diagnostics(
                    initial_diagnostics,
                    deferred_diagnostic_context,
                    deferred_diagnostic_error,
                )
            break
        except ProcessLookupError:
            # No member remains in the identity-bound group.  The direct child
            # can still be an unreaped zombie whose exact status is available.
            if diagnostics is None:
                diagnostics = _materialize_termination_diagnostics(
                    initial_diagnostics,
                    deferred_diagnostic_context,
                    deferred_diagnostic_error,
                )
            break
        except PermissionError as error:
            if diagnostics is None:
                diagnostics = _materialize_termination_diagnostics(
                    initial_diagnostics,
                    deferred_diagnostic_context,
                    deferred_diagnostic_error,
                )
            # Darwin reports EPERM when a dedicated group contains only its
            # already-exited zombie leader.  Once waitid(WNOWAIT) proves that
            # state there is no signalable member; reap the retained leader.
            if child_exit_observed:
                break
            try:
                child_exit_observed = bool(
                    _posix_child_exit_observed(
                        child,
                        _LauncherSignalState(),
                    )
                )
            except BaseException:
                child_exit_observed = False
            if child_exit_observed:
                break
            _record_termination_diagnostic(
                diagnostics,
                "shutdown launcher POSIX process-group termination",
                error,
            )
            time.sleep(_TERMINATION_RETRY_SECONDS)
        except InterruptedError as error:
            if diagnostics is None:
                diagnostics = _materialize_termination_diagnostics(
                    initial_diagnostics,
                    deferred_diagnostic_context,
                    deferred_diagnostic_error,
                )
            _record_termination_diagnostic(
                diagnostics,
                "shutdown launcher POSIX process-group termination",
                error,
            )
            continue
        except OSError as error:
            if error.errno == errno.EINTR:
                if diagnostics is None:
                    diagnostics = _materialize_termination_diagnostics(
                        initial_diagnostics,
                        deferred_diagnostic_context,
                        deferred_diagnostic_error,
                    )
                continue
            if diagnostics is None:
                diagnostics = _materialize_termination_diagnostics(
                    initial_diagnostics,
                    deferred_diagnostic_context,
                    deferred_diagnostic_error,
                )
            _record_termination_diagnostic(
                diagnostics,
                "shutdown launcher POSIX process-group termination",
                error,
            )
            time.sleep(_TERMINATION_RETRY_SECONDS)
        except BaseException as error:
            if diagnostics is None:
                diagnostics = _materialize_termination_diagnostics(
                    initial_diagnostics,
                    deferred_diagnostic_context,
                    deferred_diagnostic_error,
                )
            _record_termination_diagnostic(
                diagnostics,
                "shutdown launcher POSIX process-group termination",
                error,
            )
            time.sleep(_TERMINATION_RETRY_SECONDS)
    assert diagnostics is not None
    if termination_requested:
        # Retain the unreaped session leader while repeating the group signal.
        # This catches a descendant whose fork raced the kernel's first group
        # walk; no positive signal is ever issued after the leader is reaped.
        successful_passes = 1
        while successful_passes < _POSIX_KILL_STABILITY_PASSES:
            time.sleep(_TERMINATION_RETRY_SECONDS)
            try:
                process_group.terminate()
            except ProcessLookupError:
                break
            except PermissionError as error:
                # The caller may already have proved the retained leader's
                # exit with waitid(WNOWAIT).  Darwin can accept the first group
                # signal and then report EPERM once only that zombie remains;
                # do not discard the stronger retained observation by trying
                # to peek at the same wait status again.
                if child_exit_observed:
                    break
                try:
                    if _posix_child_exit_observed(
                        child,
                        _LauncherSignalState(),
                    ):
                        break
                except BaseException:
                    pass
                _record_termination_diagnostic(
                    diagnostics,
                    "shutdown launcher POSIX process-group stabilization",
                    error,
                )
                continue
            except BaseException as error:
                _record_termination_diagnostic(
                    diagnostics,
                    "shutdown launcher POSIX process-group stabilization",
                    error,
                )
                continue
            successful_passes += 1
    return _reap_terminated_posix_child(
        child,
        termination_requested=termination_requested,
        direct_exit_preceded_termination=child_exit_observed,
        process_group=process_group,
        launcher_signal=launcher_signal,
        diagnostics=diagnostics,
    )


def _wait_for_armed_posix_child(
    child: Any,
    hard_deadline: float,
    process_group: _PosixProcessGroup,
    signal_state: _LauncherSignalState,
) -> _SupervisionOutcome:
    while True:
        if _claim_launcher_cancellation(signal_state):
            return _terminate_and_reap_posix_child(child, process_group)
        launcher_signal = _claim_launcher_signal(signal_state)
        if launcher_signal is not None:
            return _terminate_and_reap_posix_child(
                child,
                process_group,
                launcher_signal=launcher_signal,
            )
        remaining = hard_deadline - time.monotonic()
        if remaining <= 0.0:
            # On the deadline branch, termination is the first external action.
            # There is no logging, diagnostic I/O, cleanup, or liveness poll.
            return _terminate_and_reap_posix_child(child, process_group)
        try:
            child_exited = _posix_child_exit_observed(
                child,
                signal_state,
                deadline=hard_deadline,
            )
        except _LauncherCancellationReceived:
            return _terminate_and_reap_posix_child(child, process_group)
        except _LauncherSignalReceived as event:
            return _terminate_and_reap_posix_child(
                child,
                process_group,
                launcher_signal=event.signal_number,
            )
        except BaseException as error:
            return _terminate_and_reap_posix_child(
                child,
                process_group,
                deferred_diagnostic_context=(
                    "shutdown launcher POSIX child observation"
                ),
                deferred_diagnostic_error=error,
            )
        if child_exited is None:
            return _terminate_and_reap_posix_child(child, process_group)
        if child_exited:
            return _terminate_and_reap_posix_child(
                child,
                process_group,
                child_exit_observed=True,
            )
        remaining = hard_deadline - time.monotonic()
        if remaining <= 0.0:
            return _terminate_and_reap_posix_child(child, process_group)
        time.sleep(min(_OBSERVATION_INTERVAL_SECONDS, remaining))


def _finish_windows_contained_child(
    child: Any,
    *,
    launcher_signal: int | None = None,
    initial_diagnostics: Sequence[str] = (),
) -> _SupervisionOutcome:
    """Drain a terminated retained Job and release handles only after empty."""

    return_code = int(child.wait())
    child.wait_tree_empty()
    child.close_after_empty()
    diagnostics = tuple(initial_diagnostics) + tuple(child.failures)
    if launcher_signal is not None:
        return _SupervisionOutcome(
            128 + int(launcher_signal),
            signal_number=int(launcher_signal),
            diagnostics=diagnostics,
        )
    # TerminateJobObject supplies its requested code to processes it actually
    # terminates.  A different retained direct-process status therefore proves
    # that exit won the deadline race even if it was not cached before the
    # required kill-first call.  A natural exit with the same code is the one
    # inherently ambiguous Windows case and remains conservatively forced.
    forced = bool(
        child.termination_requested
        and not child.direct_exit_preceded_termination
        and return_code == child.termination_exit_code
    )
    if forced:
        return _SupervisionOutcome(
            _FORCED_SHUTDOWN_EXIT_CODE,
            forced=True,
            diagnostics=diagnostics,
        )
    return _SupervisionOutcome(return_code, diagnostics=diagnostics)


def _terminate_and_reap_windows_child(
    child: Any,
    *,
    launcher_signal: int | None = None,
    initial_diagnostics: Sequence[str] = (),
    deferred_diagnostic_context: str | None = None,
    deferred_diagnostic_error: BaseException | None = None,
) -> _SupervisionOutcome:
    """Terminate the complete retained Job first, then wait and close it."""

    diagnostics: list[str] | None = None
    while True:
        try:
            # This is the first external action on every deadline path.  The
            # retained Job handle, never a PID, identifies the complete tree.
            child.terminate_tree(_FORCED_SHUTDOWN_EXIT_CODE)
            if diagnostics is None:
                diagnostics = _materialize_termination_diagnostics(
                    initial_diagnostics,
                    deferred_diagnostic_context,
                    deferred_diagnostic_error,
                )
            break
        except BaseException as error:
            if diagnostics is None:
                diagnostics = _materialize_termination_diagnostics(
                    initial_diagnostics,
                    deferred_diagnostic_context,
                    deferred_diagnostic_error,
                )
            _record_termination_diagnostic(
                diagnostics,
                "shutdown launcher Windows Job termination",
                error,
            )
            time.sleep(_TERMINATION_RETRY_SECONDS)
    assert diagnostics is not None
    return _finish_windows_contained_child(
        child,
        launcher_signal=launcher_signal,
        initial_diagnostics=diagnostics,
    )


def _wait_for_armed_windows_child(
    child: Any,
    hard_deadline: float,
    signal_state: _LauncherSignalState,
) -> _SupervisionOutcome:
    while True:
        if _claim_launcher_cancellation(signal_state):
            return _terminate_and_reap_windows_child(child)
        launcher_signal = _claim_launcher_signal(signal_state)
        if launcher_signal is not None:
            return _terminate_and_reap_windows_child(
                child,
                launcher_signal=launcher_signal,
            )
        remaining = hard_deadline - time.monotonic()
        if remaining <= 0.0:
            return _terminate_and_reap_windows_child(child)
        try:
            child_exited = child.observe_before_deadline(hard_deadline)
        except BaseException as error:
            return _terminate_and_reap_windows_child(
                child,
                deferred_diagnostic_context=(
                    "shutdown launcher Windows child observation"
                ),
                deferred_diagnostic_error=error,
            )
        if child_exited is None:
            return _terminate_and_reap_windows_child(child)
        if not child_exited:
            continue
        # Even a normally exited GUI can have descendants.  Its retained
        # signalled-handle observation is retained, so Job termination only
        # cleans that residual tree and cannot turn the direct exit into a
        # forced result even when its natural status is also 70.
        return _terminate_and_reap_windows_child(child)


def _terminate_and_reap_armed_child(
    child: Any,
    process_group: _PosixProcessGroup | None,
    *,
    launcher_signal: int | None = None,
    initial_diagnostics: Sequence[str] = (),
    deferred_diagnostic_context: str | None = None,
    deferred_diagnostic_error: BaseException | None = None,
    child_exit_observed: bool = False,
) -> _SupervisionOutcome:
    if os.name == "nt":
        return _terminate_and_reap_windows_child(
            child,
            launcher_signal=launcher_signal,
            initial_diagnostics=initial_diagnostics,
            deferred_diagnostic_context=deferred_diagnostic_context,
            deferred_diagnostic_error=deferred_diagnostic_error,
        )
    if process_group is None:
        raise ShutdownSupervisorError(
            "shutdown launcher POSIX process group is unavailable"
        )
    return _terminate_and_reap_posix_child(
        child,
        process_group,
        launcher_signal=launcher_signal,
        initial_diagnostics=initial_diagnostics,
        deferred_diagnostic_context=deferred_diagnostic_context,
        deferred_diagnostic_error=deferred_diagnostic_error,
        child_exit_observed=child_exit_observed,
    )


def _wait_for_armed_child(
    child: Any,
    hard_deadline: float,
    process_group: _PosixProcessGroup | None,
    signal_state: _LauncherSignalState,
) -> _SupervisionOutcome:
    if os.name == "nt":
        return _wait_for_armed_windows_child(child, hard_deadline, signal_state)
    if process_group is None:
        raise ShutdownSupervisorError(
            "shutdown launcher POSIX process group is unavailable"
        )
    return _wait_for_armed_posix_child(
        child,
        hard_deadline,
        process_group,
        signal_state,
    )


def _observe_and_reap(
    child: Any,
    process_group: _PosixProcessGroup | None,
    signal_state: _LauncherSignalState,
    *,
    initial_diagnostics: Sequence[str] = (),
) -> _SupervisionOutcome:
    """Observe a live child; only clean residual descendants after it exits."""

    diagnostics = list(initial_diagnostics)
    reported_error = False
    while True:
        if _claim_launcher_cancellation(signal_state):
            return _terminate_and_reap_armed_child(
                child,
                process_group,
                initial_diagnostics=diagnostics,
            )
        launcher_signal = _claim_launcher_signal(signal_state)
        if launcher_signal is not None:
            return _terminate_and_reap_armed_child(
                child,
                process_group,
                launcher_signal=launcher_signal,
                initial_diagnostics=diagnostics,
            )
        try:
            child_exited = _child_exit_observed(child, signal_state)
        except _LauncherCancellationReceived:
            return _terminate_and_reap_armed_child(
                child,
                process_group,
                initial_diagnostics=diagnostics,
            )
        except _LauncherSignalReceived as event:
            return _terminate_and_reap_armed_child(
                child,
                process_group,
                launcher_signal=event.signal_number,
                initial_diagnostics=diagnostics,
            )
        except BaseException as error:
            # Before ARM there is no authority to signal the child.  Remaining
            # its direct parent and retrying is preferable to orphaning it.
            if not reported_error:
                _record_termination_diagnostic(
                    diagnostics,
                    "shutdown launcher child observation",
                    error,
                )
                reported_error = True
            try:
                time.sleep(_OBSERVATION_INTERVAL_SECONDS)
            except BaseException:
                pass
            continue
        if child_exited:
            # The exited direct child remains unreaped on POSIX.  Terminate its
            # still-identity-bound group before reaping so leaked helpers can
            # neither survive nor expose an unrelated reused PGID.
            return _terminate_and_reap_armed_child(
                child,
                process_group,
                initial_diagnostics=diagnostics,
                child_exit_observed=True,
            )
        time.sleep(_OBSERVATION_INTERVAL_SECONDS)


def _supervise_child_outcome(
    child_argv: Sequence[str | os.PathLike[str]],
    *,
    env: Mapping[str, str] | None = None,
    startup_timeout: float = _DEFAULT_STARTUP_TIMEOUT_SECONDS,
    database_path: str | os.PathLike[str] | None = None,
    popen_factory: Callable[..., Any] | None = None,
    _cancellation_event: threading.Event | None = None,
) -> _SupervisionOutcome:
    """Launch, authenticate, and supervise one contained qPlot process tree."""

    timeout = float(startup_timeout)
    if not math.isfinite(timeout) or timeout <= 0.0:
        raise ValueError("startup_timeout must be finite and positive")

    # The single startup deadline includes launcher setup and process creation;
    # it is never restarted after Popen returns.
    startup_deadline = time.monotonic() + timeout
    listener: socket.socket | None = None
    child: Any = None
    channel: socket.socket | None = None
    process_group: _PosixProcessGroup | None = None
    signal_guards = _LauncherSignalGuards(previous_handlers={})
    signal_state = _LauncherSignalState(cancellation_event=_cancellation_event)
    ready_state = _ReadyState()
    armed_state = _ArmedState()
    try:
        try:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind((_LOOPBACK_HOST, 0))
            listener.listen(1)
            authentication_key = secrets.token_bytes(_AUTHENTICATION_BYTES)
            session_nonce = secrets.token_bytes(_NONCE_BYTES)
            child_environment = dict(os.environ if env is None else env)
            child_environment[_BOOTSTRAP_ENVIRONMENT_KEY] = _bootstrap_payload(
                listener,
                authentication_key=authentication_key,
                session_nonce=session_nonce,
                startup_timeout=timeout,
                database_path=database_path,
            )
            # Guards and SIGCHLD=SIG_DFL are installed before Popen, closing the
            # spawn race and ensuring the POSIX child cannot be auto-reaped.
            signal_guards = _install_launcher_signal_guards(signal_state)
        except BaseException as error:
            if _claim_launcher_cancellation(signal_state):
                return _SupervisionOutcome(
                    _FORCED_SHUTDOWN_EXIT_CODE,
                    forced=True,
                )
            diagnostic = _exact_error("shutdown launcher setup", error)
            launcher_signal = _claim_launcher_signal(signal_state)
            if launcher_signal is not None:
                return _SupervisionOutcome(
                    -launcher_signal,
                    signal_number=launcher_signal,
                    diagnostics=(diagnostic,),
                )
            return _SupervisionOutcome(
                _FORCED_SHUTDOWN_EXIT_CODE,
                diagnostics=(diagnostic,),
            )

        try:
            _raise_if_launcher_signalled(signal_state)
            if os.name == "nt" and popen_factory is None:
                from qplot._windows_shutdown_job import spawn_contained

                child = spawn_contained(list(child_argv), child_environment)
            else:
                active_popen_factory: Callable[..., Any] = subprocess.Popen
                if popen_factory is not None:
                    active_popen_factory = popen_factory
                popen_options: dict[str, Any] = {"env": child_environment}
                if os.name != "nt":
                    process_group = _PosixProcessGroup()
                    popen_options.update(process_group.popen_options())
                child = active_popen_factory(list(child_argv), **popen_options)
            if process_group is not None:
                # start_new_session established this identity before exec.
                # Retain it without a syscall so cancellation can make group
                # termination the next external action after Popen returns.
                process_group.pgid = int(child.pid)
            _raise_if_launcher_signalled(signal_state)
            if process_group is not None:
                process_group.assign(child)
            _raise_if_launcher_signalled(signal_state)
        except _LauncherCancellationReceived:
            if child is None:
                return _SupervisionOutcome(
                    _FORCED_SHUTDOWN_EXIT_CODE,
                    forced=True,
                )
            return _terminate_and_reap_armed_child(child, process_group)
        except _LauncherSignalReceived as event:
            if child is None:
                return _SupervisionOutcome(
                    -event.signal_number,
                    signal_number=event.signal_number,
                )
            return _terminate_and_reap_armed_child(
                child,
                process_group,
                launcher_signal=event.signal_number,
            )
        except BaseException as error:
            if _claim_launcher_cancellation(signal_state):
                if child is None:
                    return _SupervisionOutcome(
                        _FORCED_SHUTDOWN_EXIT_CODE,
                        forced=True,
                    )
                return _terminate_and_reap_armed_child(child, process_group)
            launcher_signal = _claim_launcher_signal(signal_state)
            if child is None:
                diagnostic = _exact_error("shutdown launcher GUI child launch", error)
                if launcher_signal is not None:
                    return _SupervisionOutcome(
                        -launcher_signal,
                        signal_number=launcher_signal,
                        diagnostics=(diagnostic,),
                    )
                return _SupervisionOutcome(
                    _FORCED_SHUTDOWN_EXIT_CODE,
                    diagnostics=(diagnostic,),
                )
            return _terminate_and_reap_armed_child(
                child,
                process_group,
                launcher_signal=launcher_signal,
                deferred_diagnostic_context="shutdown launcher GUI child launch",
                deferred_diagnostic_error=error,
            )

        try:
            channel, early_exit = _accept_child_channel(
                listener,
                child,
                deadline=startup_deadline,
                signal_state=signal_state,
            )
            if early_exit:
                return _terminate_and_reap_armed_child(
                    child,
                    process_group,
                    child_exit_observed=True,
                )
            if channel is None:
                raise ShutdownSupervisorError(
                    "shutdown launcher child channel is unavailable"
                )
            _authenticate_child_channel(
                channel,
                child,
                authentication_key=authentication_key,
                session_nonce=session_nonce,
                deadline=startup_deadline,
                signal_state=signal_state,
                ready_state=ready_state,
            )
        except _LauncherCancellationReceived:
            return _terminate_and_reap_armed_child(child, process_group)
        except _LauncherSignalReceived as event:
            return _terminate_and_reap_armed_child(
                child,
                process_group,
                launcher_signal=event.signal_number,
            )
        except BaseException as error:
            if _claim_launcher_cancellation(signal_state):
                return _terminate_and_reap_armed_child(child, process_group)
            if ready_state.committed:
                diagnostic = _exact_error("shutdown launcher child readiness", error)
                return _observe_and_reap(
                    child,
                    process_group,
                    signal_state,
                    initial_diagnostics=(diagnostic,),
                )
            # Before authenticated READY, startup failure has bounded authority
            # to terminate the whole contained tree.  No diagnostic I/O occurs
            # before that termination attempt.
            return _terminate_and_reap_armed_child(
                child,
                process_group,
                deferred_diagnostic_context="shutdown launcher child readiness",
                deferred_diagnostic_error=error,
            )
        finally:
            if listener is not None:
                try:
                    listener.close()
                except OSError:
                    pass
                listener = None

        try:
            child_exited, protocol_error = _observe_until_arm(
                channel,
                child,
                authentication_key=authentication_key,
                session_nonce=session_nonce,
                armed_state=armed_state,
                signal_state=signal_state,
            )
        except _LauncherCancellationReceived:
            return _terminate_and_reap_armed_child(child, process_group)
        except _LauncherSignalReceived as event:
            return _terminate_and_reap_armed_child(
                child,
                process_group,
                launcher_signal=event.signal_number,
            )
        except BaseException as error:
            if _claim_launcher_cancellation(signal_state):
                return _terminate_and_reap_armed_child(child, process_group)
            if armed_state.deadline is not None:
                outcome = _wait_for_armed_child(
                    child,
                    armed_state.deadline,
                    process_group,
                    signal_state,
                )
                return _outcome_with_diagnostics(
                    outcome,
                    _exact_error("shutdown launcher post-ARM failure", error),
                )
            return _observe_and_reap(
                child,
                process_group,
                signal_state,
                initial_diagnostics=(
                    _exact_error("shutdown launcher pre-ARM observation", error),
                ),
            )
        if _claim_launcher_cancellation(signal_state):
            return _terminate_and_reap_armed_child(child, process_group)
        if child_exited:
            return _terminate_and_reap_armed_child(
                child,
                process_group,
                child_exit_observed=True,
            )
        if protocol_error is not None and armed_state.deadline is None:
            return _observe_and_reap(
                child,
                process_group,
                signal_state,
                initial_diagnostics=(protocol_error,),
            )

        immutable_hard_deadline = armed_state.deadline
        if immutable_hard_deadline is None:
            return _observe_and_reap(
                child,
                process_group,
                signal_state,
                initial_diagnostics=("shutdown launcher did not receive ARM",),
            )

        try:
            deadline_expired = time.monotonic() >= immutable_hard_deadline
        except BaseException as error:
            return _terminate_and_reap_armed_child(
                child,
                process_group,
                deferred_diagnostic_context=("shutdown launcher deadline observation"),
                deferred_diagnostic_error=error,
            )
        if deadline_expired:
            return _terminate_and_reap_armed_child(child, process_group)

        # ACK construction begins only after the immutable ARMED transition.
        # Any construction/send failure continues waiting against that exact
        # deadline rather than falling back to pre-ARM observe-only behavior.
        acknowledgement_error: BaseException | None = None
        if _claim_launcher_cancellation(signal_state):
            return _terminate_and_reap_armed_child(child, process_group)
        try:
            arm_nonce = armed_state.arm_nonce
            deadline_payload = armed_state.deadline_payload
            if arm_nonce is None or deadline_payload is None:
                raise ShutdownSupervisorError(
                    "shutdown launcher committed ARM metadata is incomplete"
                )
            acknowledgement = _encode_frame(
                _ARM_ACK,
                authentication_key=authentication_key,
                session_nonce=session_nonce,
                message_nonce=arm_nonce,
                payload=deadline_payload,
            )
            _send_arm_acknowledgement(channel, acknowledgement)
        except BaseException as error:
            acknowledgement_error = error

        try:
            outcome = _wait_for_armed_child(
                child,
                immutable_hard_deadline,
                process_group,
                signal_state,
            )
        except BaseException as error:
            outcome = _terminate_and_reap_armed_child(
                child,
                process_group,
                deferred_diagnostic_context="shutdown launcher armed wait",
                deferred_diagnostic_error=error,
            )
        if acknowledgement_error is not None:
            return _outcome_with_diagnostics(
                outcome,
                _exact_error(
                    "shutdown launcher ARM acknowledgement",
                    acknowledgement_error,
                ),
            )
        return outcome
    except _LauncherCancellationReceived:
        if child is None:
            return _SupervisionOutcome(
                _FORCED_SHUTDOWN_EXIT_CODE,
                forced=True,
            )
        return _terminate_and_reap_armed_child(child, process_group)
    except _LauncherSignalReceived as event:
        if child is None:
            return _SupervisionOutcome(
                -event.signal_number,
                signal_number=event.signal_number,
            )
        return _terminate_and_reap_armed_child(
            child,
            process_group,
            launcher_signal=event.signal_number,
        )
    except BaseException as error:
        if _claim_launcher_cancellation(signal_state):
            if child is None:
                return _SupervisionOutcome(
                    _FORCED_SHUTDOWN_EXIT_CODE,
                    forced=True,
                )
            return _terminate_and_reap_armed_child(child, process_group)
        if child is None:
            diagnostic = _exact_error("shutdown launcher unexpected failure", error)
            return _SupervisionOutcome(
                _FORCED_SHUTDOWN_EXIT_CODE,
                diagnostics=(diagnostic,),
            )
        if armed_state.deadline is not None:
            return _terminate_and_reap_armed_child(
                child,
                process_group,
                deferred_diagnostic_context="shutdown launcher post-ARM failure",
                deferred_diagnostic_error=error,
            )
        if ready_state.committed:
            return _observe_and_reap(
                child,
                process_group,
                signal_state,
                initial_diagnostics=(
                    _exact_error("shutdown launcher pre-ARM failure", error),
                ),
            )
        return _terminate_and_reap_armed_child(
            child,
            process_group,
            deferred_diagnostic_context="shutdown launcher startup failure",
            deferred_diagnostic_error=error,
        )
    finally:
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        if channel is not None:
            try:
                channel.close()
            except OSError:
                pass
        if process_group is not None:
            process_group.close()
        _restore_launcher_signal_guards(signal_guards)
        late_launcher_signal = _claim_launcher_signal(signal_state)
        if late_launcher_signal is not None:
            # Containment ownership is already resolved on every returning
            # path.  A signal recorded during reap/epilogue must still retain
            # real signal semantics instead of disappearing with the guards.
            _terminate_launcher_with_signal(late_launcher_signal)


def _supervise_child(
    child_argv: Sequence[str | os.PathLike[str]],
    *,
    env: Mapping[str, str] | None = None,
    startup_timeout: float = _DEFAULT_STARTUP_TIMEOUT_SECONDS,
    database_path: str | os.PathLike[str] | None = None,
    popen_factory: Callable[..., Any] | None = None,
) -> int:
    """Testable integer-returning wrapper around process-tree supervision."""

    outcome = _supervise_child_outcome(
        child_argv,
        env=env,
        startup_timeout=startup_timeout,
        database_path=database_path,
        popen_factory=popen_factory,
    )
    diagnostics_published = _publish_outcome_diagnostics(outcome)
    if not diagnostics_published:
        signal_number = outcome.signal_number
        if signal_number is not None and os.name != "nt":
            _terminate_launcher_with_signal(signal_number)
        os._exit(outcome.return_code)
    return outcome.return_code


def _publish_outcome_diagnostics(outcome: _SupervisionOutcome) -> bool:
    """Publish retained details without letting inherited stderr block exit."""

    if not outcome.diagnostics:
        return True
    completed = threading.Event()

    def publish() -> None:
        try:
            for diagnostic in outcome.diagnostics:
                _report_launcher_failure(diagnostic)
        finally:
            completed.set()

    try:
        publisher = threading.Thread(
            target=publish,
            name="qplot-shutdown-diagnostic-publisher",
            daemon=True,
        )
        publisher.start()
        publisher.join(_DIAGNOSTIC_PUBLISH_BUDGET_SECONDS)
    except BaseException:
        return False
    return completed.is_set()


def _terminate_launcher_with_signal(signal_number: int) -> None:
    """Reproduce a POSIX child/launcher signal status after tree ownership ends."""

    if os.name == "nt":
        os._exit(128 + int(signal_number))
    try:
        signal.signal(signal_number, signal.SIG_DFL)
    except (OSError, RuntimeError, ValueError):
        pass
    pthread_sigmask = getattr(signal, "pthread_sigmask", None)
    if pthread_sigmask is not None:
        pthread_sigmask(signal.SIG_UNBLOCK, {signal_number})
    os.kill(os.getpid(), signal_number)
    # A signal that is unexpectedly masked/ignored must still terminate rather
    # than flowing through SystemExit(-N) as the misleading numeric code 256-N.
    os._exit(128 + int(signal_number))


def launch_gui(
    original_argv: Sequence[str] | None = None,
    *,
    database_path: str | os.PathLike[str] | None = None,
    _force_launcher_exit: Callable[[int], Any] = os._exit,
    _force_launcher_signal: Callable[[int], Any] = _terminate_launcher_with_signal,
) -> int:
    """Start the production GUI inside the launcher's retained containment."""

    preserved_argv = list(sys.argv if original_argv is None else original_argv)
    if not preserved_argv:
        preserved_argv = ["qplot"]
    child_argv = [
        sys.executable,
        "-m",
        "qplot._shutdown_supervisor",
        _GUI_CHILD_SENTINEL,
        *preserved_argv,
    ]
    outcome = _supervise_child_outcome(
        child_argv,
        env=os.environ,
        database_path=database_path,
    )
    diagnostics_published = _publish_outcome_diagnostics(outcome)
    signal_number = outcome.signal_number
    if (
        signal_number is None
        and os.name != "nt"
        and not outcome.forced
        and outcome.return_code < 0
    ):
        signal_number = -outcome.return_code
    if signal_number is not None:
        _force_launcher_signal(signal_number)
        return outcome.return_code
    if outcome.forced or not diagnostics_published:
        # Avoid Python atexit, imported-hook, or thread shutdown after the
        # exact GUI child has been killed and boundedly reaped.  The injectable
        # callable keeps this production hard-exit behavior unit-testable.
        _force_launcher_exit(outcome.return_code)
    return outcome.return_code


def _public_api_gui_child_argv(
    preserved_argv: Sequence[str],
) -> list[str]:
    """Build the GUI command in the caller (a private subprocess-test seam)."""

    return [
        sys.executable,
        "-m",
        "qplot._shutdown_supervisor",
        _GUI_CHILD_SENTINEL,
        *preserved_argv,
    ]


def _spawn_public_api_launcher(
    launcher_argv: Sequence[str],
    environment: Mapping[str, str],
) -> subprocess.Popen[Any]:
    """Spawn the sole GUI parent without inheriting private channel handles."""

    return subprocess.Popen(
        list(launcher_argv),
        env=dict(environment),
        close_fds=True,
    )


def _connect_public_api_result_channel(
    bootstrap: _ApiLauncherBootstrap,
) -> socket.socket:
    remaining = _timeout_within(bootstrap.startup_deadline)
    if remaining <= 0.0:
        raise TimeoutError(
            "public-API launcher startup deadline expired before connection"
        )
    channel = socket.create_connection(
        (bootstrap.host, bootstrap.port),
        timeout=remaining,
    )
    channel.set_inheritable(False)
    hello_nonce = secrets.token_bytes(_NONCE_BYTES)
    hello = _encode_frame(
        _API_LAUNCHER_HELLO,
        authentication_key=bootstrap.authentication_key,
        session_nonce=bootstrap.session_nonce,
        message_nonce=hello_nonce,
        payload=_PID_PAYLOAD.pack(os.getpid()),
    )
    try:
        remaining = _timeout_within(bootstrap.startup_deadline)
        if remaining <= 0.0:
            raise TimeoutError(
                "public-API launcher startup deadline expired before HELLO"
            )
        channel.settimeout(remaining)
        channel.sendall(hello)
        ready = _receive_exact(
            channel,
            _FRAME_SIZE,
            deadline=bootstrap.startup_deadline,
        )
        frame_type, message_nonce, payload = _decode_frame(
            ready,
            authentication_key=bootstrap.authentication_key,
            session_nonce=bootstrap.session_nonce,
        )
        if frame_type != _API_LAUNCHER_READY:
            raise ShutdownSupervisorError(
                f"unexpected public-API launcher startup frame type {frame_type}"
            )
        if not hmac.compare_digest(message_nonce, hello_nonce):
            raise ShutdownSupervisorError(
                "public-API launcher startup acknowledgement nonce does not match"
            )
        if _PID_PAYLOAD.unpack(payload)[0] != os.getpid():
            raise ShutdownSupervisorError(
                "public-API launcher startup acknowledgement PID does not match"
            )
    except BaseException:
        channel.close()
        raise
    channel.settimeout(None)
    return channel


def _authenticate_public_api_launcher(
    listener: socket.socket,
    launcher: subprocess.Popen[Any],
    *,
    authentication_key: bytes,
    session_nonce: bytes,
    startup_deadline: float,
    ready_state: _ApiLauncherReadyState,
) -> socket.socket:
    remaining = _timeout_within(startup_deadline)
    if remaining <= 0.0:
        raise TimeoutError(
            "public-API launcher readiness deadline expired before accept"
        )
    listener.settimeout(remaining)
    channel, _address = listener.accept()
    channel.set_inheritable(False)
    ready_state.channel = channel
    try:
        hello = _receive_exact(channel, _FRAME_SIZE, deadline=startup_deadline)
        frame_type, hello_nonce, payload = _decode_frame(
            hello,
            authentication_key=authentication_key,
            session_nonce=session_nonce,
        )
        if frame_type != _API_LAUNCHER_HELLO:
            raise ShutdownSupervisorError(
                f"unexpected public-API launcher HELLO type {frame_type}"
            )
        claimed_pid = _PID_PAYLOAD.unpack(payload)[0]
        if claimed_pid != launcher.pid:
            raise ShutdownSupervisorError(
                "public-API launcher claimed PID "
                f"{claimed_pid} does not match {launcher.pid}"
            )
        ready = _encode_frame(
            _API_LAUNCHER_READY,
            authentication_key=authentication_key,
            session_nonce=session_nonce,
            message_nonce=hello_nonce,
            payload=_PID_PAYLOAD.pack(launcher.pid),
        )
        remaining = _timeout_within(startup_deadline)
        if remaining <= 0.0:
            raise TimeoutError(
                "public-API launcher readiness deadline expired before READY"
            )
        channel.settimeout(remaining)
        channel.sendall(ready)
        # This is the exact point after which the launcher may create a GUI.
        # The retained channel lets the caller cancel and finish the full
        # result/EOF handshake if control flow interrupts the next bytecode.
        ready_state.committed = True
    except BaseException:
        if not ready_state.committed:
            channel.close()
            ready_state.channel = None
        raise
    channel.settimeout(None)
    return channel


def _receive_public_api_launcher_outcome(
    channel: socket.socket,
    *,
    authentication_key: bytes,
    session_nonce: bytes,
) -> _SupervisionOutcome:
    """Wait without a GUI runtime limit, then bound one authenticated result."""

    channel.settimeout(None)
    first_byte = channel.recv(1)
    if not first_byte:
        raise ShutdownSupervisorError(
            "public-API launcher result channel closed before an outcome"
        )
    result_deadline = time.monotonic() + _API_RESULT_IO_TIMEOUT_SECONDS
    header = first_byte + _receive_exact(
        channel,
        _API_RESULT_HEADER.size - 1,
        deadline=result_deadline,
    )
    try:
        _magic, _version, _frame_type, _session, payload_size = (
            _API_RESULT_HEADER.unpack(header)
        )
    except struct.error as error:
        raise ShutdownSupervisorError(
            _exact_error("public-API launcher result header decoding", error)
        ) from error
    if payload_size > _API_RESULT_MAX_BYTES:
        raise ShutdownSupervisorError(
            "public-API launcher result exceeds the bounded result size"
        )
    remainder = _receive_exact(
        channel,
        payload_size + _AUTHENTICATION_BYTES,
        deadline=result_deadline,
    )
    outcome = _decode_api_launcher_outcome(
        header + remainder,
        authentication_key=authentication_key,
        session_nonce=session_nonce,
    )
    return outcome


def _public_api_cancellation_boundary(_name: str) -> None:
    """Deterministic seam for cancellation boundary regressions."""


def _public_api_interrupt_guard_boundary(_name: str) -> None:
    """Deterministic seam for transactional SIGINT-guard regressions."""


def _public_api_caller_cleanup_boundary(_name: str) -> None:
    """Deterministic seam for post-READY caller cleanup regressions."""


def _new_public_api_cancellation_worker(
    sender: _ApiLauncherCancellationSender,
) -> threading.Thread:
    """Create the one committed cancellation worker incarnation."""

    return threading.Thread(
        target=sender._run,
        name="qplot-public-api-cancellation-sender",
        daemon=True,
    )


def _public_api_cancellation_send(
    channel: socket.socket,
    data: bytes,
) -> int:
    """Send cancellation bytes through a deterministic partial-write seam."""

    return channel.send(data)


def _public_api_cancellation_shutdown(channel: socket.socket) -> None:
    """Irreversibly close the sole caller-to-launcher write direction."""

    channel.shutdown(socket.SHUT_WR)


def _send_public_api_cancellation_record(
    sender: _ApiLauncherCancellationSender,
) -> None:
    """Resolve the sole cancellation write without leaking ``BaseException``."""

    while sender._deadline is None:
        try:
            _public_api_cancellation_boundary("lock_acquisition")
            with sender._lock:
                if sender._started_at is None:
                    _public_api_cancellation_boundary("deadline_time_monotonic")
                    sender._started_at = time.monotonic()
                started_at = sender._started_at
            _public_api_cancellation_boundary("deadline_setup")
            deadline = started_at + _API_RESULT_IO_TIMEOUT_SECONDS
            _public_api_cancellation_boundary("lock_acquisition")
            with sender._lock:
                if sender._deadline is None:
                    sender._deadline = deadline
        except BaseException as error:
            sender._remember_error(error)

    deadline = sender._deadline
    assert deadline is not None
    sent = sender._offset
    fail_closed = False
    while sent < len(sender.frame):
        try:
            _public_api_cancellation_boundary("timeout_within")
            remaining = _timeout_within(deadline)
        except BaseException as error:
            sender._remember_error(error)
            continue
        if remaining <= 0.0:
            break
        try:
            _public_api_cancellation_boundary("select")
            _readable, writable, _exceptional = select.select(
                [],
                [sender.channel],
                [],
                min(_OBSERVATION_INTERVAL_SECONDS, remaining),
            )
        except BaseException as error:
            sender._remember_error(error)
            continue
        if not writable:
            continue
        try:
            _public_api_cancellation_boundary("send_before_progress")
        except BaseException as error:
            sender._remember_error(error)
            continue
        try:
            written = _public_api_cancellation_send(
                sender.channel,
                sender.frame[sent:],
            )
        except BaseException as error:
            # A raising socket implementation does not provide a trustworthy
            # byte count.  Never guess and risk duplicating part of the HMAC
            # frame; EOF is the only safe continuation.
            sender._remember_error(error)
            fail_closed = True
            break
        if written <= 0:
            sender._remember_error(
                ShutdownSupervisorError(
                    "public-API launcher cancellation send made no progress"
                )
            )
            fail_closed = True
            break

        # Persist progress before any injectable boundary.  Only this worker
        # writes bytes, so its local offset remains authoritative even if the
        # shared observation below is interrupted.
        sent += written
        if sent < len(sender.frame):
            try:
                _public_api_cancellation_boundary("after_partial_send")
            except BaseException as error:
                sender._remember_error(error)
        while sender._offset != sent:
            try:
                _public_api_cancellation_boundary("send_offset_accounting")
                _public_api_cancellation_boundary("lock_acquisition")
                with sender._lock:
                    sender._offset = sent
            except BaseException as error:
                sender._remember_error(error)

    if sent == len(sender.frame) and not fail_closed:
        sender._finish()
        return

    if sender._first_error is None:
        sender._remember_error(
            TimeoutError("public-API launcher cancellation send deadline expired")
        )
    while True:
        try:
            # Authenticated READY established the peer identity.  If the fixed
            # cancellation record cannot be completed, write-side EOF is the
            # irreversible ownership-loss request read by that same peer.
            _public_api_cancellation_boundary("write_side_shutdown")
            _public_api_cancellation_shutdown(sender.channel)
            break
        except OSError:
            break
        except BaseException as error:
            sender._remember_error(error)
    sender._finish()


def _observe_public_api_launcher_result(
    channel: socket.socket,
    observation: _ApiLauncherResultObservation,
    cancellation_sender: _ApiLauncherCancellationSender,
    *,
    authentication_key: bytes,
    session_nonce: bytes,
) -> None:
    """Sole caller-side reader for the launcher-to-caller direction."""

    try:
        try:
            observation.outcome = _receive_public_api_launcher_outcome(
                channel,
                authentication_key=authentication_key,
                session_nonce=session_nonce,
            )
        except BaseException as error:
            observation.error = error
            cancellation_sender.request()

        while True:
            try:
                trailing = channel.recv(4096)
            except InterruptedError:
                continue
            except BaseException as error:
                if observation.error is None:
                    observation.error = error
                cancellation_sender.request()
                return
            if not trailing:
                observation.eof_observed = True
                return
            if observation.error is None:
                observation.error = ShutdownSupervisorError(
                    "public-API launcher sent trailing data after its outcome"
                )
                cancellation_sender.request()
    finally:
        observation.completed.set()


def _monitor_public_api_caller(
    channel: socket.socket,
    bootstrap: _ApiLauncherBootstrap,
    control_state: _ApiCallerControlState,
) -> None:
    """Sole launcher-side reader for cancellation or caller disappearance."""

    try:
        frame = _receive_exact(channel, _FRAME_SIZE)
    except BaseException as error:
        if isinstance(error, ShutdownSupervisorError) and "closed" in str(error):
            diagnostic = "public-API caller control channel closed before outcome"
        else:
            diagnostic = _exact_error("public-API caller control receive", error)
        control_state.commit_cancellation(diagnostic)
        return

    try:
        frame_type, _message_nonce, payload = _decode_frame(
            frame,
            authentication_key=bootstrap.authentication_key,
            session_nonce=bootstrap.session_nonce,
        )
        if frame_type != _API_LAUNCHER_CANCEL:
            raise ShutdownSupervisorError(
                f"unexpected public-API caller control type {frame_type}"
            )
        claimed_pid = _PID_PAYLOAD.unpack(payload)[0]
        if claimed_pid != bootstrap.caller_pid:
            raise ShutdownSupervisorError(
                "public-API caller cancellation PID "
                f"{claimed_pid} does not match {bootstrap.caller_pid}"
            )
    except BaseException as error:
        control_state.commit_cancellation(
            _exact_error("public-API caller cancellation protocol", error)
        )
        return
    control_state.commit_cancellation("public-API caller cancellation authenticated")


def _wait_for_public_api_result_completion(completed: threading.Event) -> None:
    """Interruptible caller wait seam; the result reader owns all framing."""

    completed.wait(_OBSERVATION_INTERVAL_SECONDS)


def _remember_caller_control_flow(
    pending: _PendingCallerControlFlow | None,
    error: BaseException,
) -> _PendingCallerControlFlow:
    if pending is not None:
        return pending
    return _PendingCallerControlFlow(error, error.__traceback__)


def _remember_and_guard_caller_control_flow(
    pending: _PendingCallerControlFlow | None,
    error: BaseException,
    interrupt_guard: _CallerCleanupInterruptGuard,
) -> _PendingCallerControlFlow:
    """Retain the first object, then make later real SIGINTs non-escaping."""

    first_error = error
    while pending is None:
        try:
            pending = _PendingCallerControlFlow(
                first_error,
                first_error.__traceback__,
            )
        except BaseException:
            # The original object remains in ``first_error`` even if another
            # caller exception lands during traceback capture/allocation.
            continue
    while not interrupt_guard.engagement_complete:
        try:
            interrupt_guard.engage()
            break
        except BaseException:
            # Injected control flow and rapid later SIGINTs are deliberately
            # absorbed only for the bounded containment-cleanup interval.
            continue
    return pending


def _wait_for_public_api_launcher_exit(
    launcher: subprocess.Popen[Any],
) -> None:
    """Reap if possible; a foreign POSIX reaper may already own the status."""

    try:
        launcher.wait(timeout=_API_RESULT_IO_TIMEOUT_SECONDS)
    except ChildProcessError:
        # The authenticated channel and EOF prove identity and process exit;
        # wait status is deliberately not part of the result protocol.
        launcher.returncode = 0


def _stop_unready_public_api_launcher(
    launcher: subprocess.Popen[Any],
    *,
    startup_deadline: float,
) -> tuple[str, ...]:
    """Close pre-READY ownership without signalling a stale POSIX PID."""

    diagnostics: list[str] = []
    if os.name == "nt":
        # subprocess retains the exact Windows process handle; these methods
        # do not reopen or target an unverified numeric PID.
        try:
            launcher.terminate()
        except (ChildProcessError, OSError):
            pass
        except BaseException as error:
            diagnostics.append(
                _exact_error("public-API unready launcher termination", error)
            )

    observation_deadline = max(startup_deadline, time.monotonic()) + (
        _API_RESULT_IO_TIMEOUT_SECONDS
    )
    while True:
        remaining = _timeout_within(observation_deadline)
        if remaining <= 0.0:
            diagnostics.append(
                "public-API unready launcher did not exit within its "
                "startup/self-exit interval"
            )
            return tuple(diagnostics)
        try:
            launcher.wait(timeout=min(_OBSERVATION_INTERVAL_SECONDS, remaining))
            return tuple(diagnostics)
        except ChildProcessError:
            launcher.returncode = 0
            return tuple(diagnostics)
        except subprocess.TimeoutExpired:
            continue
        except BaseException as error:
            diagnostic = _exact_error(
                "public-API unready launcher exit observation",
                error,
            )
            if diagnostic not in diagnostics:
                diagnostics.append(diagnostic)


def _public_api_failure(context: str, error: BaseException) -> _SupervisionOutcome:
    return _SupervisionOutcome(
        _FORCED_SHUTDOWN_EXIT_CODE,
        diagnostics=(_exact_error(context, error),),
    )


def launch_gui_for_api(
    original_argv: Sequence[str] | None = None,
    *,
    database_path: str | os.PathLike[str] | None = None,
    startup_timeout: float = _DEFAULT_STARTUP_TIMEOUT_SECONDS,
) -> int:
    """Return a contained GUI result without terminating the API caller."""

    timeout = float(startup_timeout)
    if not math.isfinite(timeout) or timeout <= 0.0:
        raise ValueError("startup_timeout must be finite and positive")
    startup_deadline = time.monotonic() + timeout
    preserved_argv = list(sys.argv if original_argv is None else original_argv)
    if not preserved_argv:
        preserved_argv = ["qplot"]
    listener: socket.socket | None = None
    channel: socket.socket | None = None
    launcher: subprocess.Popen[Any] | None = None
    ready = False
    unready_stopped = False
    deferred_readiness_error: BaseException | None = None
    try:
        try:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.set_inheritable(False)
            listener.bind((_LOOPBACK_HOST, 0))
            listener.listen(1)
            authentication_key = secrets.token_bytes(_AUTHENTICATION_BYTES)
            session_nonce = secrets.token_bytes(_NONCE_BYTES)
            cancellation_frame = _encode_frame(
                _API_LAUNCHER_CANCEL,
                authentication_key=authentication_key,
                session_nonce=session_nonce,
                message_nonce=secrets.token_bytes(_NONCE_BYTES),
                payload=_PID_PAYLOAD.pack(os.getpid()),
            )
            child_argv = _public_api_gui_child_argv(preserved_argv)
            environment = dict(os.environ)
            environment[_API_LAUNCHER_BOOTSTRAP_ENVIRONMENT_KEY] = (
                _api_launcher_bootstrap_payload(
                    listener,
                    authentication_key=authentication_key,
                    session_nonce=session_nonce,
                    startup_deadline=startup_deadline,
                    caller_pid=os.getpid(),
                    child_argv=child_argv,
                    database_path=database_path,
                )
            )
        except BaseException as error:
            if not isinstance(error, Exception):
                raise
            outcome = _public_api_failure("public-API launcher setup", error)
            _publish_outcome_diagnostics(outcome)
            return outcome.return_code

        try:
            launcher = _spawn_public_api_launcher(
                [
                    sys.executable,
                    "-m",
                    "qplot._shutdown_supervisor",
                    _API_LAUNCHER_SENTINEL,
                ],
                environment,
            )
        except BaseException as error:
            if not isinstance(error, Exception):
                raise
            outcome = _public_api_failure("public-API launcher launch", error)
            _publish_outcome_diagnostics(outcome)
            return outcome.return_code

        api_ready_state = _ApiLauncherReadyState()
        try:
            channel = _authenticate_public_api_launcher(
                listener,
                launcher,
                authentication_key=authentication_key,
                session_nonce=session_nonce,
                startup_deadline=startup_deadline,
                ready_state=api_ready_state,
            )
            ready = True
        except BaseException as error:
            if api_ready_state.committed and api_ready_state.channel is not None:
                channel = api_ready_state.channel
                ready = True
                deferred_readiness_error = error
                while True:
                    try:
                        channel.settimeout(None)
                        break
                    except BaseException:
                        # READY already committed.  Preserve the original
                        # exception and absorb every later interruption until
                        # the channel is restored to blocking mode.
                        continue
            else:
                if listener is not None:
                    try:
                        listener.close()
                    except OSError:
                        pass
                    listener = None
                cleanup_diagnostics = _stop_unready_public_api_launcher(
                    launcher,
                    startup_deadline=startup_deadline,
                )
                unready_stopped = True
                if not isinstance(error, Exception):
                    raise
                outcome = _public_api_failure(
                    "public-API launcher readiness",
                    error,
                )
                if cleanup_diagnostics:
                    outcome = _outcome_with_diagnostics(
                        outcome,
                        *cleanup_diagnostics,
                    )
                _publish_outcome_diagnostics(outcome)
                return outcome.return_code
        finally:
            if listener is not None:
                while True:
                    try:
                        listener.close()
                        break
                    except OSError:
                        break
                    except BaseException as error:
                        if ready and deferred_readiness_error is None:
                            deferred_readiness_error = error
                        if not ready:
                            raise
                listener = None

        cancellation_sender = _ApiLauncherCancellationSender(
            channel,
            cancellation_frame,
        )
        observation = _ApiLauncherResultObservation()
        result_reader = threading.Thread(
            target=_observe_public_api_launcher_result,
            args=(channel, observation, cancellation_sender),
            kwargs={
                "authentication_key": authentication_key,
                "session_nonce": session_nonce,
            },
            name="qplot-public-api-result-reader",
            daemon=True,
        )
        pending_control_flow: _PendingCallerControlFlow | None = None
        interrupt_guard = _CallerCleanupInterruptGuard()

        def retain_post_ready_error(error: BaseException) -> None:
            nonlocal pending_control_flow
            if not isinstance(error, Exception):
                pending_control_flow = _remember_and_guard_caller_control_flow(
                    pending_control_flow,
                    error,
                    interrupt_guard,
                )
            elif observation.error is None:
                observation.error = error
            cancellation_sender.request()

        if deferred_readiness_error is not None:
            retain_post_ready_error(deferred_readiness_error)

        while result_reader.ident is None:
            try:
                result_reader.start()
                break
            except BaseException as error:
                retain_post_ready_error(error)
                if result_reader.ident is not None:
                    break
                result_reader = threading.Thread(
                    target=_observe_public_api_launcher_result,
                    args=(channel, observation, cancellation_sender),
                    kwargs={
                        "authentication_key": authentication_key,
                        "session_nonce": session_nonce,
                    },
                    name="qplot-public-api-result-reader",
                    daemon=True,
                )

        while not observation.completed.is_set():
            try:
                _public_api_caller_cleanup_boundary("result_eof_processing")
                _wait_for_public_api_result_completion(observation.completed)
            except BaseException as error:
                retain_post_ready_error(error)

        while (
            cancellation_sender.requested and not cancellation_sender.completed.is_set()
        ):
            try:
                cancellation_sender.completed.wait(_OBSERVATION_INTERVAL_SECONDS)
            except BaseException as error:
                retain_post_ready_error(error)

        exit_observation_diagnostics: list[str] = []

        def retain_exit_observation_error(error: BaseException) -> None:
            if not isinstance(error, Exception):
                retain_post_ready_error(error)
                return
            while True:
                try:
                    diagnostic = _exact_error(
                        "public-API launcher exit observation",
                        error,
                    )
                    if diagnostic not in exit_observation_diagnostics:
                        exit_observation_diagnostics.append(diagnostic)
                    cancellation_sender.request()
                    return
                except BaseException as later_error:
                    retain_post_ready_error(later_error)

        launcher_exit_observed = False
        while True:
            try:
                _public_api_caller_cleanup_boundary("launcher_exit_wait")
                _wait_for_public_api_launcher_exit(launcher)
                launcher_exit_observed = True
                break
            except subprocess.TimeoutExpired as error:
                retain_exit_observation_error(error)
                continue
            except BaseException as error:
                # Retain caller control flow before cancellation.  This order
                # is essential when a second exception lands in request().
                retain_exit_observation_error(error)
                continue

        while (
            cancellation_sender.requested and not cancellation_sender.completed.is_set()
        ):
            try:
                cancellation_sender.completed.wait(_OBSERVATION_INTERVAL_SECONDS)
            except BaseException as error:
                retain_post_ready_error(error)

        final_outcome: _SupervisionOutcome | None = None
        while final_outcome is None:
            try:
                _public_api_caller_cleanup_boundary("result_eof_processing")
                if observation.error is not None:
                    final_outcome = _public_api_failure(
                        "public-API launcher result",
                        observation.error,
                    )
                elif observation.outcome is None:
                    final_outcome = _SupervisionOutcome(
                        _FORCED_SHUTDOWN_EXIT_CODE,
                        diagnostics=("public-API launcher produced no outcome",),
                    )
                else:
                    final_outcome = observation.outcome
                retained_diagnostics = list(exit_observation_diagnostics)
                if cancellation_sender.diagnostic is not None:
                    retained_diagnostics.append(cancellation_sender.diagnostic)
                if interrupt_guard.absorbed_sigints:
                    retained_diagnostics.append(
                        "public-API caller cleanup absorbed "
                        f"{interrupt_guard.absorbed_sigints} later SIGINT signal(s)"
                    )
                if retained_diagnostics:
                    final_outcome = _outcome_with_diagnostics(
                        final_outcome,
                        *retained_diagnostics,
                    )
            except BaseException as error:
                final_outcome = None
                retain_post_ready_error(error)

        while channel is not None:
            try:
                _public_api_caller_cleanup_boundary("result_channel_close")
                channel.close()
                channel = None
            except OSError:
                channel = None
            except BaseException as error:
                retain_post_ready_error(error)

        published = False
        while not published:
            try:
                _public_api_caller_cleanup_boundary("final_diagnostic_publication")
                _publish_outcome_diagnostics(final_outcome)
                published = True
            except BaseException as error:
                retain_post_ready_error(error)

        if pending_control_flow is not None:
            # Authenticated outcome, launcher-direction EOF, and exact
            # exit/reap ownership are mandatory before the original caller
            # exception can cross this boundary.
            if (
                observation.outcome is None
                or not observation.eof_observed
                or not launcher_exit_observed
            ):
                while True:
                    try:
                        time.sleep(_OBSERVATION_INTERVAL_SECONDS)
                    except BaseException:
                        continue
            while interrupt_guard.active:
                try:
                    interrupt_guard.restore()
                except BaseException:
                    continue
            raise pending_control_flow.error.with_traceback(
                pending_control_flow.traceback
            )
        return final_outcome.return_code
    finally:
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        if channel is not None:
            try:
                channel.close()
            except OSError:
                pass
        if launcher is not None and not ready and not unready_stopped:
            _stop_unready_public_api_launcher(
                launcher,
                startup_deadline=startup_deadline,
            )


def _run_public_api_launcher() -> None:
    """Own the GUI tree, publish one result, and exit without Python teardown."""

    try:
        bootstrap = _api_launcher_bootstrap_from_environment()
        channel = _connect_public_api_result_channel(bootstrap)
    except BaseException as error:
        _report_launcher_failure(
            _exact_error("public-API launcher bootstrap/readiness", error)
        )
        os._exit(_FORCED_SHUTDOWN_EXIT_CODE)

    control_state = _ApiCallerControlState()
    try:
        control_reader = threading.Thread(
            target=_monitor_public_api_caller,
            args=(channel, bootstrap, control_state),
            name="qplot-public-api-control-reader",
            daemon=True,
        )
        control_reader.start()
    except BaseException as error:
        _report_launcher_failure(
            _exact_error("public-API caller control monitor startup", error)
        )
        os._exit(_FORCED_SHUTDOWN_EXIT_CODE)

    try:
        try:
            outcome = _supervise_child_outcome(
                bootstrap.child_argv,
                env=os.environ,
                database_path=bootstrap.database_path,
                _cancellation_event=control_state.cancellation_event,
            )
        except BaseException as error:
            outcome = _public_api_failure(
                "public-API launcher supervision",
                error,
            )
        # Returning from supervision proves the retained GUI/helper tree is
        # gone.  Seal the control direction before any result I/O so a late or
        # duplicate record cannot alter an already-resolved outcome.
        control_state.commit_final_outcome()
        cancellation_diagnostic = control_state.cancellation_diagnostic
        if cancellation_diagnostic is not None:
            outcome = _outcome_with_diagnostics(
                outcome,
                cancellation_diagnostic,
            )
        try:
            encoded_outcome = _encode_api_launcher_outcome(
                outcome,
                authentication_key=bootstrap.authentication_key,
                session_nonce=bootstrap.session_nonce,
            )
        except BaseException as error:
            fallback = _public_api_failure(
                "public-API launcher outcome encoding",
                error,
            )
            encoded_outcome = _encode_api_launcher_outcome(
                fallback,
                authentication_key=bootstrap.authentication_key,
                session_nonce=bootstrap.session_nonce,
            )
        channel.settimeout(_API_RESULT_IO_TIMEOUT_SECONDS)
        channel.sendall(encoded_outcome)
    except BaseException as error:
        _report_launcher_failure(
            _exact_error("public-API launcher result publication", error)
        )
        os._exit(_FORCED_SHUTDOWN_EXIT_CODE)

    # Keep the sole result endpoint live until process death.  The caller can
    # therefore treat EOF as proof that the dedicated launcher is gone even
    # when an unrelated waitpid(-1) thread has already collected its status.
    os._exit(0)


def _run_gui_child() -> int:
    if len(sys.argv) < 3 or sys.argv[1] != _GUI_CHILD_SENTINEL:
        _report_launcher_failure("invalid private GUI child invocation")
        return _FORCED_SHUTDOWN_EXIT_CODE
    preserved_argv = list(sys.argv[2:])
    try:
        client = ShutdownSupervisorClient.from_environment().connect()
    except BaseException as error:
        _report_launcher_failure(
            _exact_error("shutdown launcher GUI child bootstrap", error)
        )
        return _FORCED_SHUTDOWN_EXIT_CODE

    sys.argv[:] = preserved_argv
    try:
        from qplot.__main__ import _run_gui

        return int(
            _run_gui(
                return_objects=False,
                database_path=client.database_path,
                shutdown_supervisor_client=client,
            )
        )
    finally:
        client.close()


def _module_main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == _API_LAUNCHER_SENTINEL:
        _run_public_api_launcher()
        raise AssertionError("public-API launcher hard exit returned")
    return _run_gui_child()


if __name__ == "__main__":
    raise SystemExit(_module_main())
