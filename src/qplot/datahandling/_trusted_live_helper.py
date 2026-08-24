"""Spawn-safe child entry point for isolated trusted live-database reads."""

from __future__ import annotations

import json
import os
import struct
import threading
from dataclasses import dataclass, field
from multiprocessing.connection import Connection
from typing import Any, NoReturn

from qplot.datahandling._trusted_live_protocol import (
    MAX_CONTROL_BYTES,
    MAX_REPLY_BYTES,
    MAX_REQUEST_BYTES,
    PROTOCOL_VERSION,
    TrustedLiveProtocolValidationError,
    decode_control_frame,
    decode_job_request,
    decode_request_frame,
    decode_startup_request,
    encode_error_reply,
    encode_query_results,
    encode_source_identity,
    encode_success_reply,
    error_code_is_terminal,
    validate_cancel,
    validate_shutdown,
)
from qplot.datahandling.trusted_live import (
    TrustedLiveBusyTimeoutError,
    TrustedLiveCancelledError,
    TrustedLiveCleanupError,
    TrustedLiveDeadlineExceededError,
    TrustedLiveInvalidDatabaseError,
    TrustedLiveQueryError,
    TrustedLiveReader,
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
)

_CONTROL_POLL_SECONDS = 0.05
_COMMAND_POLL_SECONDS = 0.05
_EXIT_PARENT_ENDPOINT_LOST = 86
_EXIT_TEST_CRASH = 87
_TEST_FAULTS = frozenset(
    {
        "crash_before_reply",
        "hang_before_operation",
        "hang_startup",
        "hang_close",
        "cleanup_quarantine",
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


@dataclass(slots=True)
class _ChildControlState:
    session: str
    orphan_grace_seconds: float
    expected_generation: int
    lock: threading.Lock = field(default_factory=threading.Lock)
    stop_event: threading.Event = field(default_factory=threading.Event)
    main_done: threading.Event = field(default_factory=threading.Event)
    parent_lost: bool = False
    fatal_protocol_error: str | None = None
    active_generation: int | None = None
    last_completed_generation: int | None = None
    last_cancelled_generation: int | None = None
    pending_cancel_generation: int | None = None
    cancel_event: threading.Event | None = None
    reader: TrustedLiveReader | None = None


def _notify_test(test_notify: Connection | None, value: bytes) -> None:
    if test_notify is None:
        return
    try:
        test_notify.send_bytes(value)
    except (BrokenPipeError, EOFError, OSError):
        pass


def _hard_exit_unless_main_finishes(
    state: _ChildControlState,
    exit_code: int,
) -> NoReturn | None:
    if state.main_done.wait(state.orphan_grace_seconds):
        return None
    os._exit(exit_code)


def _interrupt_active_locked(state: _ChildControlState) -> None:
    cancel_event = state.cancel_event
    reader = state.reader
    if cancel_event is not None:
        cancel_event.set()
    if reader is not None and state.active_generation is not None:
        # TrustedLiveReader.interrupt() is the sole Stage-2 method documented
        # as cross-thread safe.  Keeping the state lock held prevents this
        # generation from being cleared and replaced before the call.
        reader.interrupt()


def _mark_parent_lost(state: _ChildControlState) -> None:
    with state.lock:
        state.parent_lost = True
        _interrupt_active_locked(state)


def _mark_protocol_failure(state: _ChildControlState, message: str) -> None:
    with state.lock:
        if state.fatal_protocol_error is None:
            state.fatal_protocol_error = message
        _interrupt_active_locked(state)


def _control_loop(control_receive: Connection, state: _ChildControlState) -> None:
    """Own the cancellation endpoint and only interrupt the reader."""

    hard_exit_code: int | None = None
    try:
        while not state.stop_event.is_set():
            try:
                ready = control_receive.poll(_CONTROL_POLL_SECONDS)
            except (OSError, ValueError):
                _mark_parent_lost(state)
                hard_exit_code = _EXIT_PARENT_ENDPOINT_LOST
                break
            if not ready:
                continue
            try:
                frame = control_receive.recv_bytes(MAX_CONTROL_BYTES)
            except EOFError:
                _mark_parent_lost(state)
                hard_exit_code = _EXIT_PARENT_ENDPOINT_LOST
                break
            except (OSError, ValueError):
                _mark_protocol_failure(
                    state,
                    "The helper received malformed or oversized control IPC.",
                )
                hard_exit_code = _EXIT_PARENT_ENDPOINT_LOST
                break
            try:
                envelope = decode_control_frame(frame)
                validate_cancel(envelope)
            except TrustedLiveProtocolValidationError as error:
                _mark_protocol_failure(state, str(error))
                hard_exit_code = _EXIT_PARENT_ENDPOINT_LOST
                break
            with state.lock:
                if envelope.session != state.session:
                    state.fatal_protocol_error = (
                        "A cancellation targeted the wrong process incarnation."
                    )
                    _interrupt_active_locked(state)
                    hard_exit_code = _EXIT_PARENT_ENDPOINT_LOST
                    break
                cancellation_is_new = (
                    state.last_cancelled_generation is None
                    or envelope.generation > state.last_cancelled_generation
                )
                if (
                    state.active_generation == envelope.generation
                    and state.cancel_event is not None
                    and cancellation_is_new
                ):
                    state.last_cancelled_generation = envelope.generation
                    _interrupt_active_locked(state)
                    continue
                if (
                    state.last_completed_generation == envelope.generation
                    and cancellation_is_new
                ):
                    # The operation may finish after the parent decides to
                    # cancel but before this control thread receives that
                    # exact-generation frame.  It may even arrive after the
                    # following operation starts because commands and control
                    # use separate pipes.  Accept it once as a no-op; never
                    # interrupt the newer generation.
                    state.last_cancelled_generation = envelope.generation
                    continue
                if (
                    state.active_generation is None
                    and envelope.generation == state.expected_generation
                    and state.pending_cancel_generation is None
                    and cancellation_is_new
                ):
                    # Command and control pipes are intentionally separate.  A
                    # cancel sent immediately after submit may reach this
                    # thread before the main thread receives that exact job.
                    state.pending_cancel_generation = envelope.generation
                    state.last_cancelled_generation = envelope.generation
                    continue
                else:
                    state.fatal_protocol_error = (
                        "A stale, duplicate, or out-of-order cancellation was rejected."
                    )
                    _interrupt_active_locked(state)
                    hard_exit_code = _EXIT_PARENT_ENDPOINT_LOST
                    break
    finally:
        try:
            control_receive.close()
        except OSError:
            pass
    if hard_exit_code is not None:
        _hard_exit_unless_main_finishes(state, hard_exit_code)


def _error_code(error: BaseException) -> str:
    if isinstance(error, TrustedLiveUnsupportedSourceError):
        return "unsupported_source"
    if isinstance(error, TrustedLiveSourceChangedError):
        return "source_changed"
    if isinstance(error, TrustedLiveSourceIOError):
        return "source_io"
    if isinstance(error, TrustedLiveSqlRejectedError):
        return "sql_rejected"
    if isinstance(error, TrustedLiveResultLimitError):
        return "result_limit"
    if isinstance(error, TrustedLiveBusyTimeoutError):
        return "busy_timeout"
    if isinstance(error, TrustedLiveCancelledError):
        return "cancelled"
    if isinstance(error, TrustedLiveDeadlineExceededError):
        return "operation_deadline"
    if isinstance(error, TrustedLiveInvalidDatabaseError):
        return "invalid_database"
    if isinstance(error, TrustedLiveCleanupError):
        return "cleanup_quarantine"
    if isinstance(error, TrustedLiveReaderClosedError):
        return "reader_closed"
    if isinstance(error, TrustedLiveReaderThreadError):
        return "reader_thread"
    if isinstance(error, TrustedLiveTransactionError):
        return "transaction"
    if isinstance(error, TrustedLiveReaderUnavailableError):
        return "reader_unavailable"
    if isinstance(error, TrustedLiveQueryError):
        return "query_failed"
    if isinstance(error, TrustedLiveReaderError):
        return "reader_error"
    if isinstance(error, TrustedLiveProtocolValidationError):
        return "protocol_error"
    return "internal_error"


def _error_message(error: BaseException) -> str:
    if isinstance(error, (TrustedLiveReaderError, TrustedLiveProtocolValidationError)):
        return str(error) or type(error).__name__
    return f"The trusted helper failed unexpectedly ({type(error).__name__})."


def _send_reply(connection: Connection, frame: bytes) -> bool:
    try:
        connection.send_bytes(frame)
    except (BrokenPipeError, EOFError, OSError, ValueError):
        return False
    return True


def _send_partial_reply_and_hang(
    connection: Connection,
    frame: bytes,
    *,
    partial_body: bool,
    test_notify: Connection | None,
    marker: bytes,
) -> NoReturn:
    """Private deterministic stream-frame fault used only by regressions.

    POSIX ``multiprocessing.Connection`` pipes use a four-byte length header,
    so writing below that layer can strand ``recv_bytes`` in either its header
    or body read.  Windows anonymous multiprocessing pipes are message based
    and expose no separate length header; holding their reply endpoint open
    without sending supplies the equivalent permanently incomplete receive for
    the production lifecycle test.
    """

    if os.name != "nt":
        header = struct.pack("!i", len(frame))
        fragment = (
            header + frame[: max(1, len(frame) // 2)] if partial_body else header[:2]
        )
        raw_send = getattr(connection, "_send", None)
        if not callable(raw_send):
            raise RuntimeError("The private partial-frame injector is unavailable.")
        raw_send(fragment)
    _hang_forever(test_notify, marker)


def _faulted_reply(
    frame: bytes,
    *,
    fault: str | None,
    session: str,
    generation: int,
) -> bytes:
    if fault == "malformed_reply":
        return b"{"
    if fault == "oversized_reply":
        return b"x" * (MAX_REPLY_BYTES + 1)
    if fault in {
        "stale_generation_reply",
        "stale_session_reply",
        "wrong_version_reply",
    }:
        parsed = json.loads(frame)
        if fault == "stale_generation_reply":
            parsed["generation"] = max(0, generation - 1)
        elif fault == "stale_session_reply":
            parsed["session"] = "0" * 32 if session != "0" * 32 else "1" * 32
        else:
            parsed["protocol_version"] = PROTOCOL_VERSION + 1
        return json.dumps(
            parsed,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    return frame


def _fatal_state(state: _ChildControlState) -> tuple[bool, str | None]:
    with state.lock:
        return state.parent_lost, state.fatal_protocol_error


def _send_protocol_failure(
    reply_send: Connection,
    state: _ChildControlState,
    message: str,
    generation: int,
) -> None:
    frame = encode_error_reply(
        state.session,
        max(0, generation),
        "protocol",
        "protocol_error",
        message,
    )
    _send_reply(reply_send, frame)


def _hang_forever(test_notify: Connection | None, marker: bytes) -> NoReturn:
    _notify_test(test_notify, marker)
    blocker = threading.Event()
    while True:
        blocker.wait(3_600.0)


def trusted_live_helper_main(
    command_receive: Connection,
    reply_send: Connection,
    control_receive: Connection,
    startup_frame: bytes,
    test_fault: str | None = None,
    test_notify: Connection | None = None,
) -> None:
    """Own one Stage-2 reader entirely on this spawned process' main thread."""

    try:
        startup_request = decode_request_frame(startup_frame)
    except TrustedLiveProtocolValidationError:
        for connection in (
            command_receive,
            reply_send,
            control_receive,
            test_notify,
        ):
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass
        return
    try:
        (
            first_generation,
            database_path,
            expected_instance,
            busy_timeout_ms,
            operation_timeout_ms,
            orphan_grace_ms,
        ) = decode_startup_request(startup_request)
    except TrustedLiveProtocolValidationError as error:
        frame = encode_error_reply(
            startup_request.session,
            0,
            "protocol",
            "protocol_error",
            str(error),
        )
        _send_reply(reply_send, frame)
        for connection in (
            command_receive,
            reply_send,
            control_receive,
            test_notify,
        ):
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass
        return
    session = startup_request.session
    state = _ChildControlState(
        session,
        orphan_grace_ms / 1_000.0,
        first_generation,
    )
    reader: TrustedLiveReader | None = None
    control_thread = threading.Thread(
        target=_control_loop,
        args=(control_receive, state),
        name="qplot-trusted-reader-control",
        daemon=True,
    )
    control_thread.start()
    next_generation = first_generation
    last_generation = max(0, first_generation - 1)
    try:
        if test_fault is not None and test_fault not in _TEST_FAULTS:
            raise TrustedLiveProtocolValidationError(
                "The private helper test bootstrap option is invalid."
            )
        if test_fault == "hang_startup":
            _hang_forever(test_notify, b"startup_hang")
        cleanup_fault = "base_close" if test_fault == "cleanup_quarantine" else None
        statement_limit_fault = (
            test_fault
            if test_fault is not None and test_fault.startswith("statement_limit_")
            else None
        )
        try:
            reader = TrustedLiveReader.open(
                database_path,
                expected_database_instance=expected_instance,
                busy_timeout_ms=busy_timeout_ms,
                operation_timeout_seconds=operation_timeout_ms / 1_000.0,
                _test_cleanup_fault=cleanup_fault,
                _test_statement_limit_fault=statement_limit_fault,
            )
        except BaseException as error:
            code = _error_code(error)
            frame = encode_error_reply(
                session,
                0,
                "startup",
                code,
                _error_message(error),
            )
            _send_reply(reply_send, frame)
            return
        with state.lock:
            state.reader = reader
        parent_lost, fatal_error = _fatal_state(state)
        if parent_lost:
            return
        if fatal_error is not None:
            _send_protocol_failure(reply_send, state, fatal_error, 0)
            return
        startup_frame = encode_success_reply(
            session,
            0,
            "startup",
            {"source": encode_source_identity(reader.source_identity)},
        )
        if test_fault in {"partial_startup_header", "partial_startup_body"}:
            _send_partial_reply_and_hang(
                reply_send,
                startup_frame,
                partial_body=test_fault == "partial_startup_body",
                test_notify=test_notify,
                marker=(
                    b"partial_startup_body_sent"
                    if test_fault == "partial_startup_body"
                    else b"partial_startup_header_sent"
                ),
            )
        if not _send_reply(reply_send, startup_frame):
            _mark_parent_lost(state)
            return

        while True:
            parent_lost, fatal_error = _fatal_state(state)
            if parent_lost:
                break
            if fatal_error is not None:
                _send_protocol_failure(
                    reply_send,
                    state,
                    fatal_error,
                    last_generation,
                )
                break
            try:
                ready = command_receive.poll(_COMMAND_POLL_SECONDS)
            except (OSError, ValueError):
                _mark_parent_lost(state)
                break
            if not ready:
                continue
            try:
                request_frame = command_receive.recv_bytes(MAX_REQUEST_BYTES)
            except EOFError:
                _mark_parent_lost(state)
                break
            except (OSError, ValueError):
                _send_protocol_failure(
                    reply_send,
                    state,
                    "The helper received malformed or oversized command IPC.",
                    last_generation,
                )
                break
            try:
                request = decode_request_frame(request_frame)
                if request.session != session:
                    raise TrustedLiveProtocolValidationError(
                        "A command targeted the wrong process incarnation."
                    )
                if request.generation != next_generation:
                    raise TrustedLiveProtocolValidationError(
                        "A duplicate or out-of-order job generation was rejected."
                    )
                with state.lock:
                    if state.expected_generation != request.generation:
                        raise TrustedLiveProtocolValidationError(
                            "Command and control generations became inconsistent."
                        )
                next_generation += 1
                last_generation = request.generation
            except TrustedLiveProtocolValidationError as error:
                _send_protocol_failure(reply_send, state, str(error), last_generation)
                break

            if request.operation == "shutdown":
                try:
                    validate_shutdown(request)
                    with state.lock:
                        if state.pending_cancel_generation == request.generation:
                            raise TrustedLiveProtocolValidationError(
                                "Cancellation cannot target a shutdown command."
                            )
                        state.expected_generation = request.generation + 1
                    if test_fault == "hang_close":
                        _hang_forever(test_notify, b"shutdown_hang")
                    closing_reader = reader
                    if closing_reader is None:
                        raise TrustedLiveReaderClosedError(
                            "The helper reader was already closed before shutdown."
                        )
                    with state.lock:
                        state.reader = None
                    closing_reader.close()
                    reader = None
                    shutdown_frame = encode_success_reply(
                        session,
                        request.generation,
                        "shutdown",
                    )
                except BaseException as error:
                    code = _error_code(error)
                    shutdown_frame = encode_error_reply(
                        session,
                        request.generation,
                        "shutdown",
                        code,
                        _error_message(error),
                    )
                    reader = None
                if test_fault in {
                    "partial_shutdown_header",
                    "partial_shutdown_body",
                }:
                    _send_partial_reply_and_hang(
                        reply_send,
                        shutdown_frame,
                        partial_body=test_fault == "partial_shutdown_body",
                        test_notify=test_notify,
                        marker=(
                            b"partial_shutdown_body_sent"
                            if test_fault == "partial_shutdown_body"
                            else b"partial_shutdown_header_sent"
                        ),
                    )
                _send_reply(reply_send, shutdown_frame)
                break

            try:
                queries, timeout_ms = decode_job_request(request)
            except TrustedLiveProtocolValidationError as error:
                _send_protocol_failure(reply_send, state, str(error), last_generation)
                break
            cancel_event = threading.Event()
            with state.lock:
                if state.parent_lost or state.fatal_protocol_error is not None:
                    continue
                if state.expected_generation != request.generation:
                    state.fatal_protocol_error = (
                        "The active command generation changed before publication."
                    )
                    continue
                state.expected_generation = request.generation + 1
                state.active_generation = request.generation
                if state.pending_cancel_generation == request.generation:
                    state.pending_cancel_generation = None
                    cancel_event.set()
                state.cancel_event = cancel_event
            _notify_test(test_notify, b"operation_started")
            if test_fault == "crash_before_reply":
                os._exit(_EXIT_TEST_CRASH)
            if test_fault == "hang_before_operation":
                _hang_forever(test_notify, b"operation_hang")

            result: Any = None
            operation_error: BaseException | None = None
            response_payload: dict[str, Any] | None = None
            try:
                active_reader = reader
                if active_reader is None:
                    raise TrustedLiveReaderClosedError(
                        "The helper reader closed before its database job."
                    )
                timeout_seconds = timeout_ms / 1_000.0
                if request.operation == "query":
                    if queries is None:
                        raise TrustedLiveProtocolValidationError(
                            "A query request omitted its statement."
                        )
                    result = active_reader.query(
                        queries[0].sql,
                        queries[0].bindings,
                        timeout=timeout_seconds,
                        cancel_event=cancel_event,
                    )
                elif request.operation == "query_batch":
                    if queries is None:
                        raise TrustedLiveProtocolValidationError(
                            "A query batch omitted its statements."
                        )
                    result = active_reader.query_batch(
                        queries,
                        timeout=timeout_seconds,
                        cancel_event=cancel_event,
                    )
                elif request.operation == "data_version":
                    result = active_reader.data_version(
                        timeout=timeout_seconds,
                        cancel_event=cancel_event,
                    )
                else:
                    raise TrustedLiveProtocolValidationError(
                        "The helper received an unknown database operation."
                    )
                if test_fault == "cleanup_quarantine":
                    with state.lock:
                        state.reader = None
                    active_reader.close()
                    reader = None
            except BaseException as error:
                operation_error = error
            finally:
                with state.lock:
                    # Publish completion under the same lock before clearing
                    # the active generation so a racing exact-generation
                    # cancellation can be recognised as a harmless late
                    # control frame instead of poisoning the incarnation.
                    state.last_completed_generation = request.generation
                    state.active_generation = None
                    state.cancel_event = None

            parent_lost, fatal_error = _fatal_state(state)
            if parent_lost:
                break
            if fatal_error is not None:
                _send_protocol_failure(
                    reply_send,
                    state,
                    fatal_error,
                    request.generation,
                )
                break
            if operation_error is not None:
                code = _error_code(operation_error)
                response_frame = encode_error_reply(
                    session,
                    request.generation,
                    request.operation,
                    code,
                    _error_message(operation_error),
                )
            else:
                try:
                    if request.operation == "data_version":
                        response_payload = {"value": result}
                    elif request.operation == "query":
                        response_payload = {"results": encode_query_results((result,))}
                    else:
                        response_payload = {"results": encode_query_results(result)}
                    response_frame = encode_success_reply(
                        session,
                        request.generation,
                        request.operation,
                        response_payload,
                    )
                except BaseException as error:
                    code = _error_code(error)
                    response_frame = encode_error_reply(
                        session,
                        request.generation,
                        request.operation,
                        code,
                        _error_message(error),
                    )
            response_frame = _faulted_reply(
                response_frame,
                fault=test_fault,
                session=session,
                generation=request.generation,
            )
            if test_fault in {"partial_job_header", "partial_job_body"}:
                _send_partial_reply_and_hang(
                    reply_send,
                    response_frame,
                    partial_body=test_fault == "partial_job_body",
                    test_notify=test_notify,
                    marker=(
                        b"partial_job_body_sent"
                        if test_fault == "partial_job_body"
                        else b"partial_job_header_sent"
                    ),
                )
            if not _send_reply(reply_send, response_frame):
                _mark_parent_lost(state)
                break
            fault_is_terminal = test_fault in {
                "malformed_reply",
                "oversized_reply",
                "stale_generation_reply",
                "stale_session_reply",
                "wrong_version_reply",
            }
            error_is_terminal = operation_error is not None and error_code_is_terminal(
                _error_code(operation_error)
            )
            # A persistent helper must not retain the materialised result, its
            # amplified tagged/base64 tree, or the encoded reply while idle.
            result = None
            response_payload = None
            response_frame = b""
            operation_error = None
            if fault_is_terminal:
                break
            if error_is_terminal:
                break
    except BaseException as error:
        parent_lost, _fatal_error = _fatal_state(state)
        if not parent_lost:
            try:
                _send_protocol_failure(
                    reply_send,
                    state,
                    _error_message(error),
                    last_generation,
                )
            except BaseException:
                pass
    finally:
        if reader is not None:
            try:
                with state.lock:
                    state.reader = None
                reader.close()
            except BaseException:
                pass
        state.stop_event.set()
        state.main_done.set()
        for connection in (command_receive, reply_send, test_notify):
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass
        control_thread.join(timeout=min(0.25, state.orphan_grace_seconds))
