"""Persistent spawned-process supervision for :mod:`trusted_live`.

This is the application-independent Stage 3 boundary.  It deliberately has no
Qt dependencies and is not yet integrated into qPlot's windows or workers.
"""

from __future__ import annotations

import atexit
import math
import multiprocessing
import os
import queue
import secrets
import threading
import time
import weakref
from collections.abc import Sequence
from dataclasses import dataclass, field
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from types import TracebackType
from typing import Any, Generic, Literal, TypeVar, cast

from qplot.datahandling._trusted_live_helper import trusted_live_helper_main
from qplot.datahandling._trusted_live_protocol import (
    MAX_GENERATION,
    MAX_OPERATION_TIMEOUT_MS,
    MAX_REPLY_BYTES,
    ProtocolEnvelope,
    TrustedLiveProtocolValidationError,
    decode_reply_frame,
    decode_reply_payload,
    encode_cancel,
    encode_job_request,
    encode_shutdown,
    encode_startup_request,
    error_code_is_terminal,
    normalize_query_batch,
    normalize_query_specification,
    validate_job_success,
    validate_shutdown_success,
    validate_startup_success,
)
from qplot.datahandling.file_identity import DatabaseInstance, database_instances_differ
from qplot.datahandling.trusted_live import (
    DEFAULT_TRUSTED_OPERATION_TIMEOUT_SECONDS,
    SqliteBindings,
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
    TrustedLiveSourceIdentity,
    TrustedLiveSourceIOError,
    TrustedLiveSqlRejectedError,
    TrustedLiveTransactionError,
    TrustedLiveUnsupportedSourceError,
    TrustedQuery,
    TrustedQueryResult,
)

_T = TypeVar("_T")
_DEFAULT_STARTUP_TIMEOUT_SECONDS = 10.0
_DEFAULT_REPLY_TIMEOUT_SECONDS = 10.0
_DEFAULT_CANCELLATION_GRACE_SECONDS = 0.75
_DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 2.0
_DEFAULT_TERMINATE_TIMEOUT_SECONDS = 1.0
_DEFAULT_KILL_TIMEOUT_SECONDS = 1.0
_DEFAULT_SEND_TIMEOUT_SECONDS = 1.0
_DEFAULT_ORPHAN_GRACE_SECONDS = 1.0
_ATEXIT_LOCK_TIMEOUT_SECONDS = 0.25
_ATEXIT_PROCESS_TIMEOUT_SECONDS = 0.25
_REPLY_RECEIVER_JOIN_TIMEOUT_SECONDS = 0.5
_MAX_PARENT_TIMEOUT_SECONDS = 300.0
_POLL_QUANTUM_SECONDS = 0.05
_NO_VALUE = object()
_PRIVATE_TEST_FAULTS = frozenset(
    {
        "cleanup_quarantine",
        "crash_before_reply",
        "hang_before_operation",
        "hang_close",
        "hang_during_startup",
        "hang_startup",
        "malformed_reply",
        "oversized_reply",
        "partial_job_body",
        "partial_job_header",
        "partial_shutdown_body",
        "partial_shutdown_header",
        "partial_startup_body",
        "partial_startup_header",
        "statement_limit_install",
        "statement_limit_restore",
        "statement_limit_verify",
        "stale_generation_reply",
        "stale_session_reply",
        "wrong_version_reply",
    }
)


class TrustedLiveSupervisorError(TrustedLiveReaderError):
    """Base class for failures introduced by the process boundary."""


class TrustedLiveHelperStartupError(
    TrustedLiveReaderUnavailableError,
    TrustedLiveSupervisorError,
):
    """Raised when a helper cannot start and report readiness in time."""


class TrustedLiveProtocolError(
    TrustedLiveReaderUnavailableError,
    TrustedLiveSupervisorError,
):
    """Raised when either endpoint violates the bounded IPC protocol."""


class TrustedLiveHelperExitedError(
    TrustedLiveReaderUnavailableError,
    TrustedLiveSupervisorError,
):
    """Raised when a helper exits unexpectedly before its reply."""


class TrustedLiveHelperReplyTimeoutError(
    TimeoutError,
    TrustedLiveSupervisorError,
):
    """Raised after a parent reply deadline and cooperative retirement."""


class TrustedLiveHelperForcedTerminationError(
    TimeoutError,
    TrustedLiveSupervisorError,
):
    """Raised when cancellation grace expires and the helper is terminated."""


class TrustedLiveSupervisorClosedError(
    TrustedLiveReaderClosedError,
    TrustedLiveSupervisorError,
):
    """Raised when an operation targets a closed supervisor."""


@dataclass(slots=True)
class TrustedLiveJob(Generic[_T]):
    """Typed handle for one generation-bound helper operation."""

    incarnation: int
    session: str
    generation: int
    operation: str
    _owner_nonce: str = field(repr=False)
    _expected_result_count: int | None = field(default=None, repr=False)
    _done: threading.Event = field(default_factory=threading.Event, repr=False)
    _value: object = field(default=_NO_VALUE, repr=False)
    _error: BaseException | None = field(default=None, repr=False)
    _cancel_requested: bool = field(default=False, repr=False)
    _cancel_deadline: float | None = field(default=None, repr=False)
    _parent_timed_out: bool = field(default=False, repr=False)

    @property
    def done(self) -> bool:
        """Return whether the parent has accepted a terminal outcome."""

        return self._done.is_set()

    @property
    def cancellation_requested(self) -> bool:
        """Return whether exact-generation cancellation was requested."""

        return self._cancel_requested


@dataclass(frozen=True, slots=True)
class TrustedLiveSupervisorLiveness:
    """Conservative snapshot of every resource owned by one supervisor."""

    helper_pid: int | None
    process_alive: bool
    receiver_alive: bool
    open_endpoints: int
    active_incarnation: bool
    unreaped_incarnation: bool
    active_job: bool
    closing: bool
    closed: bool

    @property
    def resources_owned(self) -> bool:
        """Return whether any helper-side resource remains strongly owned."""

        return bool(
            self.active_incarnation
            or self.unreaped_incarnation
            or self.active_job
            or self.process_alive
            or self.receiver_alive
            or self.open_endpoints
        )


_LOCK_CONTENTION_LIVENESS = TrustedLiveSupervisorLiveness(
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


@dataclass(frozen=True, slots=True)
class _ReplyReceiveFailure:
    """One terminal raw-pipe failure published by the sole receiver."""

    kind: Literal["eof", "connection", "unexpected"]
    description: str


_ReplyInboxItem = bytes | _ReplyReceiveFailure


@dataclass(slots=True)
class _HelperIncarnation:
    number: int
    session: str
    process: BaseProcess
    command_send: Connection
    reply_connection: Connection
    control_send: Connection
    test_notify_receive: Connection | None
    reply_inbox: queue.Queue[_ReplyInboxItem]
    reply_ready: threading.Event
    reply_receiver_stop: threading.Event
    reply_receiver_done: threading.Event
    reply_receiver: threading.Thread
    reply_receiver_started: bool = False


def _publish_reply_item(
    inbox: queue.Queue[_ReplyInboxItem],
    ready: threading.Event,
    stop: threading.Event,
    item: _ReplyInboxItem,
) -> bool:
    """Publish into the one-slot inbox without ever stranding its producer."""

    while not stop.is_set():
        # The event is private test observability only.  Set it before the
        # queue publication so a consumer can never clear the event between
        # publication and notification and leave a stale signalled state.
        ready.set()
        try:
            inbox.put(item, timeout=_POLL_QUANTUM_SECONDS)
        except queue.Full:
            continue
        return True
    ready.clear()
    return False


def _reply_receiver_loop(
    connection: Connection,
    inbox: queue.Queue[_ReplyInboxItem],
    ready: threading.Event,
    stop: threading.Event,
    done: threading.Event,
) -> None:
    """Sole raw reply receiver for one helper process incarnation.

    ``Connection.poll()`` only establishes that some bytes are readable on
    stream-backed multiprocessing pipes; it does not establish that a complete
    frame is available.  This one persistent thread may therefore block in
    ``recv_bytes`` while all public callers retain their own finite waits on the
    bounded inbox.  Incarnation retirement closes the peer and this endpoint,
    then proves that this thread has stopped before the helper is reusable.
    """

    try:
        while not stop.is_set():
            try:
                item: _ReplyInboxItem = connection.recv_bytes(MAX_REPLY_BYTES)
                terminal = False
            except EOFError:
                item = _ReplyReceiveFailure(
                    "eof",
                    "The helper closed its reply endpoint.",
                )
                terminal = True
            except (OSError, ValueError) as error:
                item = _ReplyReceiveFailure(
                    "connection",
                    "The helper reply connection failed while receiving "
                    f"a frame ({type(error).__name__}).",
                )
                terminal = True
            except BaseException as error:
                # Never let an arbitrary receiver failure escape through a
                # public operation or leave its incarnation reusable.
                item = _ReplyReceiveFailure(
                    "unexpected",
                    "The helper reply receiver failed unexpectedly "
                    f"({type(error).__name__}).",
                )
                terminal = True
            if not _publish_reply_item(inbox, ready, stop, item):
                return
            if terminal:
                return
    finally:
        done.set()


def _duration(
    value: object,
    *,
    description: str,
    allow_zero: bool,
) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{description} must be a finite number of seconds.")
    try:
        duration = float(cast(Any, value))
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"{description} must be a finite number of seconds."
        ) from error
    if (
        not math.isfinite(duration)
        or duration < 0
        or (duration == 0 and not allow_zero)
        or duration > _MAX_PARENT_TIMEOUT_SECONDS
    ):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(
            f"{description} must be a finite {qualifier} duration no greater "
            f"than {_MAX_PARENT_TIMEOUT_SECONDS:g} seconds."
        )
    return duration


def _timeout_milliseconds(value: object, *, description: str) -> int:
    seconds = _duration(value, description=description, allow_zero=True)
    milliseconds = math.ceil(seconds * 1_000.0)
    if milliseconds > MAX_OPERATION_TIMEOUT_MS:
        raise ValueError(f"{description} exceeds the protocol operation-timeout limit.")
    return milliseconds


def _close_connection(connection: Connection | None) -> None:
    if connection is None:
        return
    try:
        connection.close()
    except OSError:
        pass


def _bounded_send_bytes(
    connection: Connection,
    frame: bytes,
    timeout: float,
) -> None:
    """Bound a potentially blocking pipe write without serialising objects."""

    completed = threading.Event()
    errors: list[BaseException] = []

    def send() -> None:
        try:
            connection.send_bytes(frame)
        except BaseException as error:
            errors.append(error)
        finally:
            completed.set()

    sender = threading.Thread(
        target=send,
        name="qplot-trusted-reader-ipc-send",
        daemon=True,
    )
    sender.start()
    if not completed.wait(timeout):
        _close_connection(connection)
        completed.wait(min(0.1, timeout))
        raise TimeoutError("A bounded trusted-helper IPC send did not finish.")
    sender.join(timeout=0)
    if errors:
        raise OSError("The trusted-helper IPC endpoint rejected a frame.") from errors[
            0
        ]


def _error_from_payload(code: str, message: str) -> BaseException:
    error_types: dict[str, type[BaseException]] = {
        "reader_unavailable": TrustedLiveReaderUnavailableError,
        "unsupported_source": TrustedLiveUnsupportedSourceError,
        "source_changed": TrustedLiveSourceChangedError,
        "source_io": TrustedLiveSourceIOError,
        "sql_rejected": TrustedLiveSqlRejectedError,
        "query_failed": TrustedLiveQueryError,
        "result_limit": TrustedLiveResultLimitError,
        "busy_timeout": TrustedLiveBusyTimeoutError,
        "cancelled": TrustedLiveCancelledError,
        "operation_deadline": TrustedLiveDeadlineExceededError,
        "invalid_database": TrustedLiveInvalidDatabaseError,
        "cleanup_quarantine": TrustedLiveCleanupError,
        "reader_closed": TrustedLiveReaderClosedError,
        "reader_thread": TrustedLiveReaderThreadError,
        "transaction": TrustedLiveTransactionError,
        "reader_error": TrustedLiveReaderError,
        "protocol_error": TrustedLiveProtocolError,
        "internal_error": TrustedLiveHelperExitedError,
    }
    return error_types[code](message)


class TrustedLiveReaderSupervisor:
    """Own one persistent spawned helper for one accepted database instance.

    Only one database job may be active.  A failed job is never replayed.  If
    its process incarnation is retired, a later explicit submit or
    :meth:`restart` may spawn a fresh helper bound to the originally accepted
    :class:`DatabaseInstance`.
    """

    def __init__(
        self,
        database_path: str | os.PathLike[str],
        *,
        expected_database_instance: DatabaseInstance | None = None,
        busy_timeout_ms: int = 5_000,
        operation_timeout_seconds: float = DEFAULT_TRUSTED_OPERATION_TIMEOUT_SECONDS,
        startup_timeout_seconds: float = _DEFAULT_STARTUP_TIMEOUT_SECONDS,
        reply_timeout_seconds: float = _DEFAULT_REPLY_TIMEOUT_SECONDS,
        cancellation_grace_seconds: float = (_DEFAULT_CANCELLATION_GRACE_SECONDS),
        shutdown_timeout_seconds: float = _DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
        terminate_timeout_seconds: float = _DEFAULT_TERMINATE_TIMEOUT_SECONDS,
        kill_timeout_seconds: float = _DEFAULT_KILL_TIMEOUT_SECONDS,
        _test_fault: str | None = None,
    ) -> None:
        try:
            selected_path = os.fspath(database_path)
        except TypeError as error:
            raise TypeError("database_path must be a path-like value.") from error
        if isinstance(selected_path, bytes):
            selected_path = os.fsdecode(selected_path)
        if (
            not isinstance(selected_path, str)
            or not selected_path
            or "\x00" in selected_path
        ):
            raise ValueError("database_path must be non-empty text without NUL bytes.")
        try:
            path_bytes = selected_path.encode("utf-8", errors="strict")
        except UnicodeEncodeError as error:
            raise ValueError(
                "database_path must contain valid Unicode text."
            ) from error
        if len(path_bytes) > 32 * 1024:
            raise ValueError("database_path exceeds the bounded helper path limit.")
        if expected_database_instance is not None and not isinstance(
            expected_database_instance, DatabaseInstance
        ):
            raise TypeError(
                "expected_database_instance must be a DatabaseInstance or None."
            )
        if (
            type(busy_timeout_ms) is not int
            or busy_timeout_ms < 0
            or busy_timeout_ms > 2_147_483_647
        ):
            raise ValueError(
                "busy_timeout_ms must be an integer from 0 through 2147483647."
            )
        if _test_fault is not None and (
            type(_test_fault) is not str or _test_fault not in _PRIVATE_TEST_FAULTS
        ):
            raise ValueError("The private helper test fault mode is invalid.")
        self._database_path = selected_path
        self._configured_expected_instance = expected_database_instance
        self._accepted_source: TrustedLiveSourceIdentity | None = None
        self._busy_timeout_ms = busy_timeout_ms
        self._operation_timeout_ms = _timeout_milliseconds(
            operation_timeout_seconds,
            description="default child operation timeout",
        )
        if self._operation_timeout_ms == 0:
            raise ValueError("default child operation timeout must be positive.")
        self._startup_timeout = _duration(
            startup_timeout_seconds,
            description="helper startup timeout",
            allow_zero=False,
        )
        self._reply_timeout = _duration(
            reply_timeout_seconds,
            description="parent reply timeout",
            allow_zero=False,
        )
        self._cancellation_grace = _duration(
            cancellation_grace_seconds,
            description="cancellation grace period",
            allow_zero=True,
        )
        self._shutdown_timeout = _duration(
            shutdown_timeout_seconds,
            description="helper shutdown timeout",
            allow_zero=True,
        )
        self._terminate_timeout = _duration(
            terminate_timeout_seconds,
            description="helper termination timeout",
            allow_zero=True,
        )
        self._kill_timeout = _duration(
            kill_timeout_seconds,
            description="helper kill timeout",
            allow_zero=True,
        )
        self._send_timeout = min(_DEFAULT_SEND_TIMEOUT_SECONDS, self._reply_timeout)
        self._orphan_grace = min(
            _DEFAULT_ORPHAN_GRACE_SECONDS,
            max(0.05, self._shutdown_timeout),
        )
        self._context = multiprocessing.get_context("spawn")
        self._owner_nonce = secrets.token_hex(16)
        self._next_generation = 1
        self._incarnation_counter = 0
        self._helper: _HelperIncarnation | None = None
        self._unreaped_helper: _HelperIncarnation | None = None
        self._active_job: TrustedLiveJob[Any] | None = None
        self._closing = False
        self._closed = False
        self._lock = threading.RLock()
        self._reply_lock = threading.Lock()
        self._test_fault = _test_fault
        self._test_fault_consumed = False
        self._atexit_registered = False
        supervisor_reference = weakref.ref(self)

        def cleanup_at_exit() -> None:
            supervisor = supervisor_reference()
            if supervisor is not None:
                supervisor._cleanup_at_exit()

        self._atexit_callback = cleanup_at_exit
        atexit.register(self._atexit_callback)
        self._atexit_registered = True

    @classmethod
    def open(
        cls,
        database_path: str | os.PathLike[str],
        *,
        expected_database_instance: DatabaseInstance | None = None,
        busy_timeout_ms: int = 5_000,
        operation_timeout_seconds: float = DEFAULT_TRUSTED_OPERATION_TIMEOUT_SECONDS,
        startup_timeout_seconds: float = _DEFAULT_STARTUP_TIMEOUT_SECONDS,
        reply_timeout_seconds: float = _DEFAULT_REPLY_TIMEOUT_SECONDS,
        cancellation_grace_seconds: float = (_DEFAULT_CANCELLATION_GRACE_SECONDS),
        shutdown_timeout_seconds: float = _DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
        terminate_timeout_seconds: float = _DEFAULT_TERMINATE_TIMEOUT_SECONDS,
        kill_timeout_seconds: float = _DEFAULT_KILL_TIMEOUT_SECONDS,
        _test_fault: str | None = None,
    ) -> TrustedLiveReaderSupervisor:
        """Spawn and open one helper, failing within ``startup_timeout_seconds``."""

        supervisor = cls(
            database_path,
            expected_database_instance=expected_database_instance,
            busy_timeout_ms=busy_timeout_ms,
            operation_timeout_seconds=operation_timeout_seconds,
            startup_timeout_seconds=startup_timeout_seconds,
            reply_timeout_seconds=reply_timeout_seconds,
            cancellation_grace_seconds=cancellation_grace_seconds,
            shutdown_timeout_seconds=shutdown_timeout_seconds,
            terminate_timeout_seconds=terminate_timeout_seconds,
            kill_timeout_seconds=kill_timeout_seconds,
            _test_fault=_test_fault,
        )
        try:
            with supervisor._lock:
                supervisor._spawn_helper_locked()
        except BaseException:
            supervisor._closed = True
            with supervisor._lock:
                supervisor._discard_helper_locked(force=True)
            supervisor._unregister_atexit()
            raise
        return supervisor

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def source_identity(self) -> TrustedLiveSourceIdentity:
        with self._lock:
            source = self._accepted_source
            if source is None:
                raise TrustedLiveReaderUnavailableError(
                    "The supervisor has not accepted a trusted database source."
                )
            return source

    @property
    def database_instance(self) -> DatabaseInstance:
        return self.source_identity.database_instance

    @property
    def incarnation(self) -> int:
        with self._lock:
            return self._incarnation_counter

    @property
    def helper_pid(self) -> int | None:
        with self._lock:
            helper = self._helper
            return None if helper is None else helper.process.pid

    @property
    def helper_alive(self) -> bool:
        with self._lock:
            helper = self._helper
            return helper is not None and helper.process.is_alive()

    def resource_liveness(self) -> TrustedLiveSupervisorLiveness:
        """Describe active and quarantined resources without waiting for them."""

        if not self._lock.acquire(blocking=False):
            # ``close()`` intentionally owns this lock while it performs its
            # bounded helper shutdown.  GUI-side shutdown polling must not
            # inherit that deadline.  Lock contention therefore means only
            # "cleanup is still pending", never "cleanup is complete".
            return _LOCK_CONTENTION_LIVENESS
        try:
            return self._resource_liveness_locked()
        finally:
            self._lock.release()

    def reap_closed_resources(self) -> TrustedLiveSupervisorLiveness:
        """Zero-wait reap a closed supervisor's quarantined incarnation.

        A bounded ``close()`` may have to leave a process handle or reply
        receiver quarantined.  Owners can poll this method off the GUI thread;
        it never terminates an active incarnation and never waits for one.
        """

        if not self._lock.acquire(blocking=False):
            return _LOCK_CONTENTION_LIVENESS
        try:
            if self._closed and not self._closing:
                try:
                    self._reap_quarantined_helper_locked()
                except TrustedLiveHelperForcedTerminationError:
                    pass
            snapshot = self._resource_liveness_locked()
            if snapshot.closed and not snapshot.resources_owned:
                self._unregister_atexit_locked()
            return snapshot
        finally:
            self._lock.release()

    def _resource_liveness_locked(self) -> TrustedLiveSupervisorLiveness:
        helpers = tuple(
            helper
            for helper in (self._helper, self._unreaped_helper)
            if helper is not None
        )
        process_alive = False
        receiver_alive = False
        open_endpoints = 0
        helper_pid: int | None = None
        for helper in helpers:
            if helper_pid is None:
                try:
                    helper_pid = helper.process.pid
                except (AssertionError, OSError, ValueError):
                    pass
            try:
                process_alive = process_alive or helper.process.is_alive()
            except (AssertionError, OSError, ValueError):
                # An incarnation is still owned, so an uninspectable process
                # handle must conservatively keep shutdown pending.
                process_alive = True
            if helper.reply_receiver_started:
                try:
                    receiver_alive = receiver_alive or helper.reply_receiver.is_alive()
                except (AssertionError, RuntimeError):
                    receiver_alive = True
            for connection in (
                helper.command_send,
                helper.reply_connection,
                helper.control_send,
                helper.test_notify_receive,
            ):
                if connection is None:
                    continue
                try:
                    is_open = not connection.closed
                except (AttributeError, OSError, ValueError):
                    is_open = True
                open_endpoints += int(is_open)
        active_job = self._active_job
        return TrustedLiveSupervisorLiveness(
            helper_pid=helper_pid,
            process_alive=process_alive,
            receiver_alive=receiver_alive,
            open_endpoints=open_endpoints,
            active_incarnation=self._helper is not None,
            unreaped_incarnation=self._unreaped_helper is not None,
            active_job=active_job is not None and not active_job.done,
            closing=self._closing,
            closed=self._closed,
        )

    @property
    def active_job(self) -> TrustedLiveJob[Any] | None:
        with self._lock:
            return self._active_job

    def _require_open_locked(self) -> None:
        if self._closed or self._closing:
            raise TrustedLiveSupervisorClosedError(
                "The trusted live-reader supervisor is closing or closed."
            )

    def _require_not_closed_locked(self) -> None:
        if self._closed:
            raise TrustedLiveSupervisorClosedError(
                "The trusted live-reader supervisor is closed."
            )

    def _expected_instance_for_spawn(self) -> DatabaseInstance | None:
        accepted_source = self._accepted_source
        if accepted_source is not None:
            return accepted_source.database_instance
        return self._configured_expected_instance

    def _spawn_helper_locked(self) -> _HelperIncarnation:
        self._require_open_locked()
        self._reap_quarantined_helper_locked()
        if self._helper is not None:
            raise TrustedLiveTransactionError(
                "A trusted helper incarnation is already installed."
            )
        if self._next_generation > MAX_GENERATION:
            raise TrustedLiveProtocolError(
                "The trusted helper job-generation space is exhausted."
            )
        session = secrets.token_hex(16)
        try:
            startup_frame = encode_startup_request(
                session,
                self._next_generation,
                self._database_path,
                self._expected_instance_for_spawn(),
                self._busy_timeout_ms,
                self._operation_timeout_ms,
                math.ceil(self._orphan_grace * 1_000.0),
            )
        except TrustedLiveProtocolValidationError as error:
            raise TrustedLiveProtocolError(
                "The helper startup configuration exceeds the bounded IPC protocol."
            ) from error
        self._incarnation_counter += 1
        number = self._incarnation_counter
        command_receive, command_send = self._context.Pipe(duplex=False)
        reply_receive, reply_send = self._context.Pipe(duplex=False)
        control_receive, control_send = self._context.Pipe(duplex=False)
        test_notify_receive: Connection | None = None
        test_notify_send: Connection | None = None
        fault = None
        if self._test_fault is not None and not self._test_fault_consumed:
            fault_aliases = {"hang_during_startup": "hang_startup"}
            fault = fault_aliases.get(self._test_fault, self._test_fault)
            self._test_fault_consumed = True
            test_notify_receive, test_notify_send = self._context.Pipe(duplex=False)
        process = self._context.Process(
            target=trusted_live_helper_main,
            args=(
                command_receive,
                reply_send,
                control_receive,
                startup_frame,
                fault,
                test_notify_send,
            ),
            name=f"qplot-trusted-live-reader-{number}",
        )
        # The explicit bounded atexit cleanup below normally owns retirement.
        # Daemonic status is a final interpreter-shutdown fallback so an
        # abandoned supervisor cannot make multiprocessing join forever.
        process.daemon = True
        reply_inbox: queue.Queue[_ReplyInboxItem] = queue.Queue(maxsize=1)
        reply_ready = threading.Event()
        reply_receiver_stop = threading.Event()
        reply_receiver_done = threading.Event()
        reply_receiver = threading.Thread(
            target=_reply_receiver_loop,
            args=(
                reply_receive,
                reply_inbox,
                reply_ready,
                reply_receiver_stop,
                reply_receiver_done,
            ),
            name=f"qplot-trusted-reader-reply-receiver-{number}",
            daemon=True,
        )
        helper = _HelperIncarnation(
            number,
            session,
            process,
            command_send,
            reply_receive,
            control_send,
            test_notify_receive,
            reply_inbox,
            reply_ready,
            reply_receiver_stop,
            reply_receiver_done,
            reply_receiver,
        )
        try:
            process.start()
        except BaseException as error:
            for connection in (
                command_receive,
                command_send,
                reply_receive,
                reply_send,
                control_receive,
                control_send,
                test_notify_receive,
                test_notify_send,
            ):
                _close_connection(connection)
            try:
                process.close()
            except (OSError, ValueError):
                pass
            raise TrustedLiveHelperStartupError(
                "The trusted live-reader helper could not be spawned."
            ) from error
        # Process start may lazily register multiprocessing's own exit hook.
        # Re-register ours afterwards so LIFO interpreter shutdown runs this
        # bounded cleanup before multiprocessing considers joining children.
        if self._atexit_registered:
            atexit.unregister(self._atexit_callback)
            atexit.register(self._atexit_callback)
        for child_endpoint in (
            command_receive,
            reply_send,
            control_receive,
            test_notify_send,
        ):
            _close_connection(child_endpoint)
        self._helper = helper
        try:
            try:
                helper.reply_receiver.start()
                helper.reply_receiver_started = True
            except BaseException as error:
                raise TrustedLiveHelperStartupError(
                    "The trusted helper reply receiver could not be started."
                ) from error
            envelope = self._wait_startup_reply_locked(helper)
            status, payload = decode_reply_payload(envelope)
            if envelope.generation != 0:
                raise TrustedLiveProtocolError(
                    "The helper sent an out-of-order startup reply."
                )
            if envelope.operation == "protocol":
                if status != "error" or payload["code"] != "protocol_error":
                    raise TrustedLiveProtocolError(
                        "The helper sent an invalid startup protocol outcome."
                    )
                raise cast(
                    BaseException,
                    _error_from_payload(payload["code"], payload["message"]),
                )
            if envelope.operation != "startup":
                raise TrustedLiveProtocolError(
                    "The helper sent an out-of-order startup reply."
                )
            if status == "error":
                raise _error_from_payload(payload["code"], payload["message"])
            source = validate_startup_success(payload)
            prior = self._expected_instance_for_spawn()
            if prior is not None and database_instances_differ(
                prior,
                source.database_instance,
            ):
                raise TrustedLiveSourceChangedError(
                    "The spawned helper reported a different database instance."
                )
            if self._accepted_source is None:
                self._accepted_source = source
            elif database_instances_differ(
                self._accepted_source.database_instance,
                source.database_instance,
            ):
                raise TrustedLiveSourceChangedError(
                    "A replacement helper opened a different database instance."
                )
            # Preserve the accepted main-file binding while accurately
            # reporting the currently installed WAL/SHM identities and mode.
            self._accepted_source = source
            return helper
        except BaseException:
            self._discard_helper_locked(force=False)
            raise

    @staticmethod
    def _take_reply_item(
        helper: _HelperIncarnation,
        timeout: float,
    ) -> _ReplyInboxItem | None:
        """Wait boundedly for the sole receiver to publish one complete frame."""

        try:
            if timeout <= 0:
                item = helper.reply_inbox.get_nowait()
            else:
                item = helper.reply_inbox.get(timeout=timeout)
        except queue.Empty:
            return None
        if helper.reply_inbox.empty():
            helper.reply_ready.clear()
        return item

    def _wait_startup_reply_locked(
        self,
        helper: _HelperIncarnation,
    ) -> ProtocolEnvelope:
        deadline = time.monotonic() + self._startup_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._discard_helper_locked(force=True)
                raise TrustedLiveHelperStartupError(
                    "The trusted helper did not finish startup before its "
                    "parent deadline."
                )
            try:
                item = self._take_reply_item(
                    helper,
                    min(_POLL_QUANTUM_SECONDS, remaining),
                )
                if item is not None:
                    if isinstance(item, _ReplyReceiveFailure):
                        if item.kind == "eof":
                            raise TrustedLiveHelperExitedError(item.description)
                        raise TrustedLiveProtocolError(item.description)
                    frame = item
                    envelope = decode_reply_frame(frame)
                    if envelope.session != helper.session:
                        raise TrustedLiveProtocolError(
                            "A stale startup reply targeted another incarnation."
                        )
                    return envelope
            except TrustedLiveProtocolValidationError as error:
                raise TrustedLiveProtocolError(
                    "The helper sent malformed or oversized startup IPC."
                ) from error
            except EOFError as error:
                raise TrustedLiveHelperExitedError(
                    "The trusted helper's startup endpoint closed unexpectedly."
                ) from error
            except (OSError, ValueError) as error:
                raise TrustedLiveProtocolError(
                    "The helper sent malformed or oversized startup IPC."
                ) from error
            if not helper.process.is_alive():
                exit_code = helper.process.exitcode
                if helper.reply_receiver_done.is_set() and helper.reply_inbox.empty():
                    raise TrustedLiveHelperExitedError(
                        "The trusted helper exited during startup "
                        f"(exit code {exit_code})."
                    )

    def _ensure_helper_locked(self) -> _HelperIncarnation:
        self._require_open_locked()
        self._reap_quarantined_helper_locked()
        helper = self._helper
        if (
            helper is not None
            and helper.process.is_alive()
            and helper.reply_receiver_started
            and helper.reply_receiver.is_alive()
        ):
            return helper
        if helper is not None:
            # A live process without its sole receiver is unusable and must not
            # accept another generation.
            self._discard_helper_locked(force=helper.process.is_alive())
        return self._spawn_helper_locked()

    def _stop_incarnation(
        self,
        helper: _HelperIncarnation,
        *,
        force: bool,
        terminate_timeout: float | None = None,
        kill_timeout: float | None = None,
    ) -> tuple[bool, bool]:
        """Join and close one exact process, escalating without unbounded waits."""

        forced = False
        terminate_wait = (
            self._terminate_timeout if terminate_timeout is None else terminate_timeout
        )
        kill_wait = self._kill_timeout if kill_timeout is None else kill_timeout

        def process_alive() -> bool:
            try:
                return helper.process.is_alive()
            except (AssertionError, OSError, ValueError):
                return True

        def bounded_join(timeout: float) -> None:
            try:
                helper.process.join(timeout=timeout)
            except (AssertionError, OSError, ValueError):
                pass

        try:
            if not force and process_alive():
                _close_connection(helper.command_send)
                _close_connection(helper.control_send)
                bounded_join(self._shutdown_timeout)
            if process_alive():
                forced = True
                try:
                    helper.process.terminate()
                except (AttributeError, OSError, ValueError):
                    pass
                bounded_join(terminate_wait)
            if process_alive():
                forced = True
                try:
                    kill = getattr(helper.process, "kill", None)
                    if callable(kill):
                        kill()
                    else:
                        helper.process.terminate()
                except (AttributeError, OSError, ValueError):
                    pass
                bounded_join(kill_wait)
        finally:
            for connection in (
                helper.command_send,
                helper.control_send,
                helper.test_notify_receive,
            ):
                _close_connection(connection)
            helper.reply_receiver_stop.set()
            _close_connection(helper.reply_connection)
            if helper.reply_receiver_started:
                try:
                    helper.reply_receiver.join(
                        timeout=_REPLY_RECEIVER_JOIN_TIMEOUT_SECONDS
                    )
                except RuntimeError:
                    pass
        receiver_alive = (
            helper.reply_receiver_started and helper.reply_receiver.is_alive()
        )
        if process_alive() or receiver_alive:
            # Keep the Process handle quarantined.  A later spawn may proceed
            # only after both its process and sole receiver are reaped.
            return True, False
        bounded_join(0)
        try:
            helper.process.close()
        except (OSError, ValueError):
            pass
        return forced, True

    def _reap_quarantined_helper_locked(self) -> None:
        helper = self._unreaped_helper
        if helper is None:
            return
        helper.reply_receiver_stop.set()
        _close_connection(helper.reply_connection)
        if helper.reply_receiver_started:
            try:
                helper.reply_receiver.join(timeout=0)
            except RuntimeError:
                pass
        try:
            helper.process.join(timeout=0)
            alive = helper.process.is_alive()
        except (AssertionError, OSError, ValueError):
            alive = True
        receiver_alive = (
            helper.reply_receiver_started and helper.reply_receiver.is_alive()
        )
        if alive or receiver_alive:
            raise TrustedLiveHelperForcedTerminationError(
                "A previously killed trusted helper or its reply receiver has "
                "not been reaped; a replacement process will not be started."
            )
        try:
            helper.process.close()
        except (OSError, ValueError):
            pass
        self._unreaped_helper = None

    def _force_quarantined_helper_locked(
        self,
        *,
        terminate_timeout: float | None = None,
        kill_timeout: float | None = None,
    ) -> bool:
        helper = self._unreaped_helper
        if helper is None:
            return False
        forced, reaped = self._stop_incarnation(
            helper,
            force=True,
            terminate_timeout=terminate_timeout,
            kill_timeout=kill_timeout,
        )
        if reaped:
            self._unreaped_helper = None
        return forced or not reaped

    def _discard_helper_locked(
        self,
        *,
        force: bool,
        terminate_timeout: float | None = None,
        kill_timeout: float | None = None,
    ) -> bool:
        helper, self._helper = self._helper, None
        if helper is None:
            return False
        forced, reaped = self._stop_incarnation(
            helper,
            force=force,
            terminate_timeout=terminate_timeout,
            kill_timeout=kill_timeout,
        )
        if not reaped:
            self._unreaped_helper = helper
        return forced

    def _validate_job_locked(self, job: TrustedLiveJob[_T]) -> None:
        if not isinstance(job, TrustedLiveJob) or job._owner_nonce != self._owner_nonce:
            raise ValueError("The job does not belong to this supervisor.")

    def _submit(
        self,
        operation: str,
        queries: Sequence[TrustedQuery] | None,
        timeout: float | None,
    ) -> TrustedLiveJob[Any]:
        timeout_ms = (
            self._operation_timeout_ms
            if timeout is None
            else _timeout_milliseconds(
                timeout,
                description="child operation timeout",
            )
        )
        with self._lock:
            self._require_open_locked()
            if self._active_job is not None:
                raise TrustedLiveTransactionError(
                    "Only one trusted database job may be active per helper."
                )
            helper = self._ensure_helper_locked()
            generation = self._next_generation
            if generation > MAX_GENERATION:
                raise TrustedLiveProtocolError(
                    "The trusted helper job-generation space is exhausted."
                )
            try:
                frame = encode_job_request(
                    helper.session,
                    generation,
                    operation,
                    queries,
                    timeout_ms,
                )
            except TrustedLiveProtocolValidationError as error:
                raise TrustedLiveProtocolError(
                    "The database job exceeds the bounded IPC protocol."
                ) from error
            self._next_generation += 1
            expected_result_count = (
                len(queries)
                if operation in {"query", "query_batch"} and queries is not None
                else None
            )
            job: TrustedLiveJob[Any] = TrustedLiveJob(
                helper.number,
                helper.session,
                generation,
                operation,
                self._owner_nonce,
                expected_result_count,
            )
            self._active_job = job
            try:
                _bounded_send_bytes(helper.command_send, frame, self._send_timeout)
            except BaseException as error:
                self._active_job = None
                self._discard_helper_locked(force=True)
                raise TrustedLiveHelperExitedError(
                    "The trusted helper did not accept the bounded job frame."
                ) from error
            return job

    def submit_query(
        self,
        sql: str,
        bindings: SqliteBindings = None,
        *,
        timeout: float | None = None,
    ) -> TrustedLiveJob[TrustedQueryResult]:
        """Submit one finite read-only statement without waiting for its reply."""

        try:
            query = normalize_query_specification(sql, bindings)
        except TrustedLiveProtocolValidationError as error:
            raise TrustedLiveProtocolError(
                "The database job exceeds the bounded IPC protocol."
            ) from error
        return cast(
            TrustedLiveJob[TrustedQueryResult],
            self._submit("query", (query,), timeout),
        )

    def submit_query_batch(
        self,
        queries: Sequence[TrustedQuery],
        *,
        timeout: float | None = None,
    ) -> TrustedLiveJob[tuple[TrustedQueryResult, ...]]:
        """Submit one finite repeatable-read query batch."""

        try:
            normalized_queries = normalize_query_batch(queries)
        except TrustedLiveProtocolValidationError as error:
            raise TrustedLiveProtocolError(
                "The database job exceeds the bounded IPC protocol."
            ) from error
        return cast(
            TrustedLiveJob[tuple[TrustedQueryResult, ...]],
            self._submit("query_batch", normalized_queries, timeout),
        )

    def submit_data_version(
        self,
        *,
        timeout: float | None = None,
    ) -> TrustedLiveJob[int]:
        """Submit a finite ``PRAGMA data_version`` operation."""

        return cast(
            TrustedLiveJob[int],
            self._submit("data_version", None, timeout),
        )

    @staticmethod
    def _complete_job(
        job: TrustedLiveJob[Any],
        *,
        value: object = _NO_VALUE,
        error: BaseException | None = None,
    ) -> None:
        if job.done:
            return
        job._value = value
        job._error = error
        job._done.set()

    def _protocol_failure_locked(
        self,
        job: TrustedLiveJob[Any],
        message: str,
        cause: BaseException | None = None,
    ) -> None:
        error = TrustedLiveProtocolError(message)
        if cause is not None:
            error.__cause__ = cause
        if self._active_job is job:
            self._active_job = None
        self._discard_helper_locked(force=True)
        self._complete_job(job, error=error)

    def _unexpected_exit_locked(
        self,
        job: TrustedLiveJob[Any],
        message: str,
    ) -> None:
        if self._active_job is job:
            self._active_job = None
        self._discard_helper_locked(force=False)
        self._complete_job(job, error=TrustedLiveHelperExitedError(message))

    def _handle_job_reply_locked(
        self,
        job: TrustedLiveJob[Any],
        envelope: ProtocolEnvelope,
    ) -> None:
        helper = self._helper
        if helper is None or self._active_job is not job:
            self._protocol_failure_locked(
                job,
                "A reply arrived without its exact active helper job.",
            )
            return
        if envelope.session != job.session or envelope.session != helper.session:
            self._protocol_failure_locked(
                job,
                "A stale reply from another process incarnation was rejected.",
            )
            return
        if envelope.generation != job.generation:
            self._protocol_failure_locked(
                job,
                "A duplicate, stale, or out-of-order job reply was rejected.",
            )
            return
        try:
            status, payload = decode_reply_payload(envelope)
            if envelope.operation == "protocol":
                if status != "error" or payload["code"] != "protocol_error":
                    raise TrustedLiveProtocolValidationError(
                        "A protocol failure reply has invalid fields."
                    )
                error = _error_from_payload(payload["code"], payload["message"])
                terminal = True
                value = _NO_VALUE
            else:
                if envelope.operation != job.operation:
                    raise TrustedLiveProtocolValidationError(
                        "The helper reply operation does not match its job."
                    )
                if status == "error":
                    code = payload["code"]
                    error = _error_from_payload(code, payload["message"])
                    terminal = error_code_is_terminal(code)
                    value = _NO_VALUE
                else:
                    value = validate_job_success(job.operation, payload)
                    if job.operation == "query_batch" and (
                        not isinstance(value, tuple)
                        or len(value) != job._expected_result_count
                    ):
                        raise TrustedLiveProtocolValidationError(
                            "A query-batch reply does not contain one result "
                            "for every submitted statement."
                        )
                    error = None
                    terminal = False
        except TrustedLiveProtocolValidationError as protocol_error:
            self._protocol_failure_locked(
                job,
                "The helper reply payload failed closed validation.",
                protocol_error,
            )
            return
        self._active_job = None
        if job._parent_timed_out:
            forced = self._discard_helper_locked(force=False)
            if forced:
                error = TrustedLiveHelperForcedTerminationError(
                    "The parent reply deadline expired and helper retirement "
                    "required forced termination."
                )
            else:
                error = TrustedLiveHelperReplyTimeoutError(
                    "The parent reply deadline expired; the completed helper "
                    "incarnation was retired without accepting its late reply."
                )
            value = _NO_VALUE
        elif terminal:
            self._discard_helper_locked(force=False)
        self._complete_job(job, value=value, error=error)

    def _poll_job_once(self, job: TrustedLiveJob[Any], timeout: float) -> None:
        with self._lock:
            if job.done:
                return
            helper = self._helper
            if (
                helper is None
                or self._active_job is not job
                or helper.session != job.session
            ):
                self._unexpected_exit_locked(
                    job,
                    "The job's helper incarnation is no longer available.",
                )
                return
            process = helper.process
        try:
            item = self._take_reply_item(helper, max(0.0, timeout))
        except Exception as error:
            with self._lock:
                if job.done:
                    return
                if self._helper is not helper or self._active_job is not job:
                    self._complete_job(
                        job,
                        error=TrustedLiveHelperExitedError(
                            "The job's helper was retired while polling its reply."
                        ),
                    )
                    return
                self._protocol_failure_locked(
                    job,
                    "The helper reply endpoint failed closed.",
                    error,
                )
            return
        if item is not None:
            if isinstance(item, _ReplyReceiveFailure):
                with self._lock:
                    if job.done:
                        return
                    if self._helper is not helper or self._active_job is not job:
                        self._complete_job(
                            job,
                            error=TrustedLiveHelperExitedError(
                                "The job's helper was retired while its sole "
                                "reply receiver was stopping."
                            ),
                        )
                        return
                    if item.kind == "eof":
                        try:
                            exit_code = process.exitcode
                        except (AssertionError, OSError, ValueError):
                            exit_code = None
                        self._unexpected_exit_locked(
                            job,
                            "The trusted helper closed its reply endpoint before "
                            f"replying (exit code {exit_code}).",
                        )
                    else:
                        self._protocol_failure_locked(job, item.description)
                return
            try:
                frame = item
                envelope = decode_reply_frame(frame)
            except TrustedLiveProtocolValidationError as error:
                with self._lock:
                    if job.done:
                        return
                    if self._helper is not helper or self._active_job is not job:
                        self._complete_job(
                            job,
                            error=TrustedLiveHelperExitedError(
                                "The job's helper was retired before its reply "
                                "could be validated."
                            ),
                        )
                        return
                    self._protocol_failure_locked(
                        job,
                        "The helper sent malformed or oversized reply IPC.",
                        error,
                    )
                return
            except EOFError:
                with self._lock:
                    if job.done:
                        return
                    if self._helper is not helper or self._active_job is not job:
                        self._complete_job(
                            job,
                            error=TrustedLiveHelperExitedError(
                                "The job's helper was retired while receiving "
                                "its reply."
                            ),
                        )
                        return
                    try:
                        exit_code = process.exitcode
                    except (AssertionError, OSError, ValueError):
                        exit_code = None
                    self._unexpected_exit_locked(
                        job,
                        "The trusted helper closed its reply endpoint before "
                        f"replying (exit code {exit_code}).",
                    )
                return
            except (OSError, ValueError) as error:
                with self._lock:
                    if job.done:
                        return
                    if self._helper is not helper or self._active_job is not job:
                        self._complete_job(
                            job,
                            error=TrustedLiveHelperExitedError(
                                "The job's helper was retired while receiving "
                                "its reply frame."
                            ),
                        )
                        return
                    self._protocol_failure_locked(
                        job,
                        "The helper reply frame was truncated or oversized.",
                        error,
                    )
                return
            with self._lock:
                if job.done:
                    return
                if self._helper is not helper or self._active_job is not job:
                    self._complete_job(
                        job,
                        error=TrustedLiveHelperExitedError(
                            "The job's helper was retired before its reply "
                            "could be accepted."
                        ),
                    )
                    return
                self._handle_job_reply_locked(job, envelope)
            return
        try:
            process_alive = process.is_alive()
            exit_code = None if process_alive else process.exitcode
        except (AssertionError, OSError, ValueError) as error:
            with self._lock:
                if job.done:
                    return
                if self._helper is not helper or self._active_job is not job:
                    self._complete_job(
                        job,
                        error=TrustedLiveHelperExitedError(
                            "The job's helper was retired while its process "
                            "state was inspected."
                        ),
                    )
                    return
                self._protocol_failure_locked(
                    job,
                    "The trusted helper process state failed closed.",
                    error,
                )
            return
        if not process_alive:
            with self._lock:
                if job.done:
                    return
                if self._helper is not helper or self._active_job is not job:
                    self._complete_job(
                        job,
                        error=TrustedLiveHelperExitedError(
                            "The job's helper was retired before process-exit "
                            "handling completed."
                        ),
                    )
                    return
                # A complete frame may have become readable with the process
                # sentinel.  The receiver publishes before marking itself
                # done, so classify the exit only after its inbox is drained.
                if helper.reply_receiver_done.is_set() and helper.reply_inbox.empty():
                    self._unexpected_exit_locked(
                        job,
                        f"The trusted helper exited before replying "
                        f"(exit code {exit_code}).",
                    )

    def _request_cancel_locked(
        self,
        job: TrustedLiveJob[Any],
        *,
        grace_timeout: float,
        parent_timeout: bool,
    ) -> bool:
        if job.done:
            return False
        helper = self._helper
        if self._active_job is not job or helper is None:
            self._unexpected_exit_locked(
                job,
                "The current helper disappeared before cancellation.",
            )
            return False
        if job._cancel_requested:
            if parent_timeout:
                job._parent_timed_out = True
            return True
        frame = encode_cancel(helper.session, job.generation)
        job._cancel_requested = True
        job._parent_timed_out = parent_timeout
        job._cancel_deadline = time.monotonic() + grace_timeout
        try:
            _bounded_send_bytes(
                helper.control_send,
                frame,
                min(self._send_timeout, max(0.05, grace_timeout)),
            )
        except BaseException:
            self._force_job_locked(
                job,
                "The cancellation frame could not reach the trusted helper.",
            )
        return True

    def _force_job_locked(self, job: TrustedLiveJob[Any], message: str) -> None:
        if job.done:
            return
        if self._active_job is job:
            self._active_job = None
        self._discard_helper_locked(force=True)
        self._complete_job(
            job,
            error=TrustedLiveHelperForcedTerminationError(message),
        )

    def _drive_job(
        self,
        job: TrustedLiveJob[Any],
        parent_deadline: float,
    ) -> None:
        while not job.done:
            now = time.monotonic()
            with self._lock:
                cancel_deadline = job._cancel_deadline
                if cancel_deadline is not None and now >= cancel_deadline:
                    self._force_job_locked(
                        job,
                        "Cooperative cancellation did not finish within its "
                        "bounded grace period; the helper was forcibly stopped.",
                    )
                    return
                if cancel_deadline is None and now >= parent_deadline:
                    self._request_cancel_locked(
                        job,
                        grace_timeout=self._cancellation_grace,
                        parent_timeout=True,
                    )
                    continue
                effective_deadline = (
                    parent_deadline
                    if cancel_deadline is None
                    else min(parent_deadline, cancel_deadline)
                )
                if job._parent_timed_out and cancel_deadline is not None:
                    effective_deadline = cancel_deadline
            quantum = max(
                0.0,
                min(_POLL_QUANTUM_SECONDS, effective_deadline - time.monotonic()),
            )
            self._poll_job_once(job, quantum)

    def _job_outcome(self, job: TrustedLiveJob[_T]) -> _T:
        error = job._error
        if error is not None:
            raise error
        if job._value is _NO_VALUE:
            raise TrustedLiveProtocolError(
                "The helper job completed without a result or error."
            )
        return cast(_T, job._value)

    def wait(
        self,
        job: TrustedLiveJob[_T],
        *,
        timeout: float | None = None,
    ) -> _T:
        """Wait no longer than the parent deadline, then cancel and retire."""

        wait_timeout = (
            self._reply_timeout
            if timeout is None
            else _duration(
                timeout,
                description="parent job wait timeout",
                allow_zero=True,
            )
        )
        with self._lock:
            self._validate_job_locked(job)
            if job.done:
                return self._job_outcome(job)
        parent_deadline = time.monotonic() + wait_timeout
        while not job.done:
            remaining = max(0.0, parent_deadline - time.monotonic())
            if job._cancel_deadline is not None:
                remaining = max(
                    0.0,
                    job._cancel_deadline - time.monotonic(),
                )
            acquired = self._reply_lock.acquire(
                timeout=min(_POLL_QUANTUM_SECONDS, remaining) if remaining > 0 else 0
            )
            if acquired:
                try:
                    self._drive_job(job, parent_deadline)
                finally:
                    self._reply_lock.release()
                break
            if time.monotonic() >= parent_deadline:
                with self._lock:
                    self._request_cancel_locked(
                        job,
                        grace_timeout=self._cancellation_grace,
                        parent_timeout=True,
                    )
                if job._done.wait(self._cancellation_grace):
                    break
                with self._lock:
                    self._force_job_locked(
                        job,
                        "The parent reply deadline and cancellation grace "
                        "expired; the helper was forcibly stopped.",
                    )
                break
        return self._job_outcome(job)

    def cancel(
        self,
        job: TrustedLiveJob[Any] | None = None,
        *,
        grace_timeout: float | None = None,
    ) -> bool:
        """Cancel one exact active generation and enforce bounded escalation."""

        grace = (
            self._cancellation_grace
            if grace_timeout is None
            else _duration(
                grace_timeout,
                description="cancellation grace period",
                allow_zero=True,
            )
        )
        with self._lock:
            self._require_not_closed_locked()
            selected = self._active_job if job is None else job
            if selected is None:
                return False
            self._validate_job_locked(selected)
            if selected.done or self._active_job is not selected:
                return False
            self._request_cancel_locked(
                selected,
                grace_timeout=grace,
                parent_timeout=False,
            )
        if selected.done:
            return True
        if self._reply_lock.acquire(blocking=False):
            try:
                self._drive_job(
                    selected,
                    selected._cancel_deadline or time.monotonic(),
                )
            finally:
                self._reply_lock.release()
        elif not selected._done.wait(grace):
            with self._lock:
                self._force_job_locked(
                    selected,
                    "Cooperative cancellation did not finish within its "
                    "bounded grace period; the helper was forcibly stopped.",
                )
        return True

    def query(
        self,
        sql: str,
        bindings: SqliteBindings = None,
        *,
        timeout: float | None = None,
        wait_timeout: float | None = None,
    ) -> TrustedQueryResult:
        """Submit and boundedly wait for one trusted query."""

        return self.wait(
            self.submit_query(sql, bindings, timeout=timeout),
            timeout=wait_timeout,
        )

    def query_batch(
        self,
        queries: Sequence[TrustedQuery],
        *,
        timeout: float | None = None,
        wait_timeout: float | None = None,
    ) -> tuple[TrustedQueryResult, ...]:
        """Submit and boundedly wait for one repeatable-read query batch."""

        return self.wait(
            self.submit_query_batch(queries, timeout=timeout),
            timeout=wait_timeout,
        )

    def data_version(
        self,
        *,
        timeout: float | None = None,
        wait_timeout: float | None = None,
    ) -> int:
        """Submit and boundedly wait for ``PRAGMA data_version``."""

        return self.wait(
            self.submit_data_version(timeout=timeout),
            timeout=wait_timeout,
        )

    def restart(self) -> None:
        """Explicitly retire any idle incarnation and spawn a fresh one."""

        with self._lock:
            self._require_open_locked()
            if self._active_job is not None:
                raise TrustedLiveTransactionError(
                    "Cancel and finish the active job before restarting its helper."
                )
            self._discard_helper_locked(force=False)
            self._spawn_helper_locked()

    def _shutdown_helper_locked(self, timeout: float) -> BaseException | None:
        helper = self._helper
        if helper is None:
            if self._force_quarantined_helper_locked():
                return TrustedLiveHelperForcedTerminationError(
                    "A quarantined trusted helper required forced retirement "
                    "during shutdown."
                )
            return None
        if not helper.process.is_alive():
            self._discard_helper_locked(force=False)
            return None
        generation = self._next_generation
        if generation > MAX_GENERATION:
            forced = self._discard_helper_locked(force=True)
            if forced:
                return TrustedLiveHelperForcedTerminationError(
                    "The helper generation space exhausted during shutdown."
                )
            return TrustedLiveProtocolError(
                "The helper generation space exhausted during shutdown."
            )
        self._next_generation += 1
        try:
            frame = encode_shutdown(helper.session, generation)
            _bounded_send_bytes(
                helper.command_send,
                frame,
                min(self._send_timeout, max(0.05, timeout)),
            )
        except BaseException as error:
            self._discard_helper_locked(force=True)
            shutdown_send_error = TrustedLiveHelperForcedTerminationError(
                "The helper shutdown request could not be delivered."
            )
            shutdown_send_error.__cause__ = error
            return shutdown_send_error
        deadline = time.monotonic() + timeout
        shutdown_error: BaseException | None = None
        acknowledged = False
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                item = self._take_reply_item(
                    helper,
                    min(_POLL_QUANTUM_SECONDS, remaining),
                )
                if item is None and not helper.process.is_alive():
                    # Process exit and a final buffered pipe frame can become
                    # observable in either order.  The sole receiver publishes
                    # before marking itself done, so wait for that transition.
                    if (
                        helper.reply_receiver_done.is_set()
                        and helper.reply_inbox.empty()
                    ):
                        shutdown_error = TrustedLiveHelperExitedError(
                            "The trusted helper exited without acknowledging "
                            "shutdown "
                            f"(exit code {helper.process.exitcode})."
                        )
                        break
                if item is not None:
                    if isinstance(item, _ReplyReceiveFailure):
                        if item.kind == "eof":
                            shutdown_error = TrustedLiveHelperExitedError(
                                "The helper exited while acknowledging shutdown."
                            )
                        else:
                            shutdown_error = TrustedLiveProtocolError(item.description)
                        break
                    envelope = decode_reply_frame(item)
                    if (
                        envelope.session != helper.session
                        or envelope.generation != generation
                        or envelope.operation not in {"shutdown", "protocol"}
                    ):
                        raise TrustedLiveProtocolValidationError(
                            "The shutdown reply was stale or out of order."
                        )
                    status, payload = decode_reply_payload(envelope)
                    if envelope.operation == "protocol":
                        if status != "error" or payload["code"] != "protocol_error":
                            raise TrustedLiveProtocolValidationError(
                                "A protocol shutdown reply has invalid fields."
                            )
                        shutdown_error = _error_from_payload(
                            payload["code"], payload["message"]
                        )
                    else:
                        if status == "error":
                            shutdown_error = _error_from_payload(
                                payload["code"], payload["message"]
                            )
                        else:
                            validate_shutdown_success(payload)
                    acknowledged = True
                    break
            except TrustedLiveProtocolValidationError as error:
                shutdown_error = TrustedLiveProtocolError(
                    "The helper sent invalid shutdown IPC."
                )
                shutdown_error.__cause__ = error
                break
            except (EOFError, OSError, ValueError) as error:
                shutdown_error = TrustedLiveHelperExitedError(
                    "The helper exited while acknowledging shutdown."
                )
                shutdown_error.__cause__ = error
                break
        timed_out = not acknowledged and shutdown_error is None
        forced = self._discard_helper_locked(force=False)
        if forced or timed_out:
            return TrustedLiveHelperForcedTerminationError(
                "The trusted helper did not shut down within its bounded deadline."
            )
        return shutdown_error

    def close(self, *, timeout: float | None = None) -> None:
        """Cancel active work, then boundedly close, join, and release the helper."""

        shutdown_timeout = (
            self._shutdown_timeout
            if timeout is None
            else _duration(
                timeout,
                description="helper shutdown timeout",
                allow_zero=True,
            )
        )
        with self._lock:
            if self._closed:
                return
            if self._closing:
                raise TrustedLiveSupervisorClosedError(
                    "The trusted live-reader supervisor is already closing."
                )
            self._closing = True
            active = self._active_job
        cancel_failure: BaseException | None = None
        if active is not None and not active.done:
            try:
                self.cancel(active)
            except BaseException as error:
                cancel_failure = error
        if active is not None and not active.done:
            with self._lock:
                self._force_job_locked(
                    active,
                    "Supervisor close could not obtain a terminal job reply; "
                    "the helper was forcibly stopped before shutdown.",
                )
        active_forced_error = (
            active._error
            if active is not None
            and isinstance(active._error, TrustedLiveHelperForcedTerminationError)
            else None
        )
        with self._lock:
            try:
                shutdown_error = self._shutdown_helper_locked(shutdown_timeout)
            except BaseException as unexpected_shutdown_error:
                self._discard_helper_locked(force=True)
                shutdown_error = unexpected_shutdown_error
            finally:
                self._active_job = None
                self._closed = True
                self._closing = False
        self._unregister_atexit()
        if active_forced_error is not None:
            raise active_forced_error
        if cancel_failure is not None:
            raise cancel_failure
        if shutdown_error is not None:
            raise shutdown_error

    def _unregister_atexit(self) -> None:
        lock = getattr(self, "_lock", None)
        if lock is None:
            return
        with lock:
            self._unregister_atexit_locked()

    def _unregister_atexit_locked(self) -> None:
        if self._helper is not None or self._unreaped_helper is not None:
            return
        if not self._atexit_registered:
            return
        self._atexit_registered = False
        atexit.unregister(self._atexit_callback)

    def _cleanup_at_exit(self) -> None:
        """Best-effort bounded cleanup before multiprocessing's exit hook."""

        lock = getattr(self, "_lock", None)
        if lock is None:
            return
        acquired = False
        try:
            acquired = lock.acquire(timeout=_ATEXIT_LOCK_TIMEOUT_SECONDS)
            if not acquired:
                return
            self._atexit_registered = False
            active, self._active_job = self._active_job, None
            if active is not None and not active.done:
                self._complete_job(
                    active,
                    error=TrustedLiveHelperForcedTerminationError(
                        "The abandoned supervisor forcibly retired its active helper."
                    ),
                )
            self._closing = False
            self._closed = True
            self._discard_helper_locked(
                force=True,
                terminate_timeout=_ATEXIT_PROCESS_TIMEOUT_SECONDS,
                kill_timeout=_ATEXIT_PROCESS_TIMEOUT_SECONDS,
            )
            self._force_quarantined_helper_locked(
                terminate_timeout=_ATEXIT_PROCESS_TIMEOUT_SECONDS,
                kill_timeout=_ATEXIT_PROCESS_TIMEOUT_SECONDS,
            )
        except BaseException:
            # The daemon-process fallback remains in force.  Interpreter exit
            # must never be delayed by cleanup diagnostics.
            pass
        finally:
            if acquired:
                try:
                    lock.release()
                except BaseException:
                    pass

    def _wait_for_test_notification(self, expected: bytes, timeout: float) -> None:
        """Private deterministic barrier; not part of the production protocol."""

        wait_timeout = _duration(
            timeout,
            description="private test notification timeout",
            allow_zero=True,
        )
        with self._lock:
            helper = self._helper
            connection = None if helper is None else helper.test_notify_receive
        if connection is None or not connection.poll(wait_timeout):
            raise TimeoutError("The private helper test notification did not arrive.")
        value = connection.recv_bytes(256)
        if value != expected:
            raise AssertionError(
                f"Expected private helper notification {expected!r}, got {value!r}."
            )

    def _send_test_command_frame(self, frame: bytes) -> None:
        """Private raw-frame injection used only by protocol tests."""

        if not isinstance(frame, bytes):
            raise TypeError("A private test frame must be bytes.")
        with self._lock:
            helper = self._ensure_helper_locked()
            _bounded_send_bytes(helper.command_send, frame, self._send_timeout)

    def _send_test_control_frame(self, frame: bytes) -> None:
        """Private raw control injection used only by protocol tests."""

        if not isinstance(frame, bytes):
            raise TypeError("A private test frame must be bytes.")
        with self._lock:
            helper = self._ensure_helper_locked()
            _bounded_send_bytes(helper.control_send, frame, self._send_timeout)

    def __enter__(self) -> TrustedLiveReaderSupervisor:
        with self._lock:
            self._require_open_locked()
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exception_type, traceback
        if exception is None:
            self.close()
            return
        try:
            self.close()
        except BaseException as close_error:
            exception.add_note(
                "The trusted live-reader supervisor also failed during "
                f"context cleanup: {type(close_error).__name__}: {close_error}"
            )

    def __del__(self) -> None:
        try:
            if (
                getattr(self, "_closed", True)
                and getattr(self, "_helper", None) is None
                and getattr(self, "_unreaped_helper", None) is None
            ):
                return
            self._cleanup_at_exit()
            callback = getattr(self, "_atexit_callback", None)
            if callback is not None:
                atexit.unregister(callback)
        except BaseException:
            pass


__all__ = [
    "TrustedLiveHelperExitedError",
    "TrustedLiveHelperForcedTerminationError",
    "TrustedLiveHelperReplyTimeoutError",
    "TrustedLiveHelperStartupError",
    "TrustedLiveJob",
    "TrustedLiveProtocolError",
    "TrustedLiveReaderSupervisor",
    "TrustedLiveSupervisorClosedError",
    "TrustedLiveSupervisorError",
    "TrustedLiveSupervisorLiveness",
]
