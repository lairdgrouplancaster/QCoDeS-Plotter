"""Pure authenticated wire format for qPlot shutdown supervision.

This module owns byte layouts, authentication, and result validation. It must
remain independent of sockets, process ownership, signals, threads, and Qt so
both launcher endpoints use exactly the same protocol without importing their
lifecycle machinery.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import struct
from dataclasses import dataclass
from typing import Any

_API_RESULT_MAX_BYTES = 256 * 1024
_TERMINATION_DIAGNOSTIC_LIMIT = 64

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
_LOOPBACK_HOST = "127.0.0.1"

# Control records have one strict, fixed size. The eight-byte payload is
# an unsigned PID for HELLO/READY and an IEEE-754 deadline for ARM/ARM_ACK.
_FRAME_BODY = struct.Struct("!8sBB6x32s32s8s")
_FRAME_SIZE = _FRAME_BODY.size + _AUTHENTICATION_BYTES
_PID_PAYLOAD = struct.Struct("!Q")
_DEADLINE_PAYLOAD = struct.Struct("!d")

# The public API result has a fixed header followed by bounded JSON and a MAC.
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
    """The immutable outcome shared by a launcher and its API caller."""

    return_code: int
    forced: bool = False
    signal_number: int | None = None
    diagnostics: tuple[str, ...] = ()


def _exact_error(context: str, error: BaseException) -> str:
    """Preserve exception type and detail in launcher diagnostics."""

    return f"{context} raised {type(error).__name__}: {error}"


_BOOTSTRAP_COMMON_FIELDS = frozenset({
    "version", "host", "port", "authentication_key", "session_nonce", "database_path",
})


def _decode_bootstrap_object(
    encoded: str, extra_fields: set[str], context: str,
) -> dict[str, Any]:
    try:
        raw = json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise ShutdownSupervisorError(
            _exact_error(f"{context} bootstrap decoding", error)
        ) from error
    if not isinstance(raw, dict) or set(raw) != _BOOTSTRAP_COMMON_FIELDS | extra_fields:
        raise ShutdownSupervisorError(f"{context} bootstrap fields are invalid")
    if raw["version"] != _PROTOCOL_VERSION:
        raise ShutdownSupervisorError(f"{context} bootstrap version is unsupported")
    return raw


def _bootstrap_endpoint(raw: dict[str, Any], context: str) -> tuple[str, int]:
    host, port = raw["host"], raw["port"]
    if host != _LOOPBACK_HOST:
        raise ShutdownSupervisorError(f"{context} bootstrap endpoint is not loopback")
    if type(port) is not int or not 0 < port < 65536:
        raise ShutdownSupervisorError(f"{context} bootstrap port is invalid")
    return host, port


def _bootstrap_database_path(value: Any, context: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ShutdownSupervisorError(f"{context} database-path override is invalid")
    return value


def _bootstrap_credentials(
    raw: dict[str, Any], context: str, *, length_context: str,
) -> tuple[bytes, bytes]:
    try:
        authentication_key = bytes.fromhex(raw["authentication_key"])
        session_nonce = bytes.fromhex(raw["session_nonce"])
    except (TypeError, ValueError) as error:
        raise ShutdownSupervisorError(
            _exact_error(f"{context} bootstrap nonce decoding", error)
        ) from error
    if len(authentication_key) != _AUTHENTICATION_BYTES:
        raise ShutdownSupervisorError(
            f"{length_context} authentication key has the wrong length"
        )
    if len(session_nonce) != _NONCE_BYTES:
        raise ShutdownSupervisorError(
            f"{length_context} session nonce has the wrong length"
        )
    return authentication_key, session_nonce


def _decode_bootstrap(encoded: str) -> _Bootstrap:
    """Decode the GUI bootstrap while retaining version-one validation order."""
    context = "shutdown-launcher"
    raw = _decode_bootstrap_object(encoded, {"startup_timeout"}, context)
    host, port = _bootstrap_endpoint(raw, context)
    timeout = raw["startup_timeout"]
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
        raise ShutdownSupervisorError("shutdown-launcher bootstrap timeout is invalid")
    timeout = float(timeout)
    if not math.isfinite(timeout) or timeout <= 0.0:
        raise ShutdownSupervisorError("shutdown-launcher bootstrap timeout is invalid")
    database_path = _bootstrap_database_path(raw["database_path"], context)
    authentication_key, session_nonce = _bootstrap_credentials(
        raw, context, length_context="shutdown-launcher bootstrap",
    )
    return _Bootstrap(host, port, authentication_key, session_nonce, timeout, database_path)


def _decode_api_launcher_bootstrap(encoded: str) -> _ApiLauncherBootstrap:
    """Decode the caller bootstrap; its deadline is absolute, not a timeout."""
    context = "public-API launcher"
    raw = _decode_bootstrap_object(
        encoded, {"startup_deadline", "caller_pid", "child_argv"}, context,
    )
    host, port = _bootstrap_endpoint(raw, context)
    deadline = raw["startup_deadline"]
    if not isinstance(deadline, (int, float)) or isinstance(deadline, bool):
        raise ShutdownSupervisorError("public-API launcher bootstrap deadline is invalid")
    deadline = float(deadline)
    if not math.isfinite(deadline):
        raise ShutdownSupervisorError("public-API launcher bootstrap deadline is invalid")
    caller_pid, child_argv = raw["caller_pid"], raw["child_argv"]
    if type(caller_pid) is not int or caller_pid <= 0:
        raise ShutdownSupervisorError("public-API launcher caller PID is invalid")
    if (
        not isinstance(child_argv, list)
        or not child_argv
        or not all(isinstance(argument, str) for argument in child_argv)
    ):
        raise ShutdownSupervisorError("public-API launcher child arguments are invalid")
    database_path = _bootstrap_database_path(raw["database_path"], context)
    authentication_key, session_nonce = _bootstrap_credentials(
        raw, context, length_context=context,
    )
    return _ApiLauncherBootstrap(
        host, port, authentication_key, session_nonce, deadline, caller_pid,
        tuple(child_argv), database_path,
    )


def _encode_bootstrap_fields(
    bootstrap: _Bootstrap | _ApiLauncherBootstrap, extra_fields: dict[str, Any],
) -> str:
    return json.dumps(
        {
            "version": _PROTOCOL_VERSION,
            "host": bootstrap.host,
            "port": bootstrap.port,
            "authentication_key": bootstrap.authentication_key.hex(),
            "session_nonce": bootstrap.session_nonce.hex(),
            "database_path": bootstrap.database_path,
            **extra_fields,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _encode_bootstrap(bootstrap: _Bootstrap) -> str:
    return _encode_bootstrap_fields(bootstrap, {"startup_timeout": bootstrap.startup_timeout})


def _encode_api_launcher_bootstrap(bootstrap: _ApiLauncherBootstrap) -> str:
    return _encode_bootstrap_fields(bootstrap, {
        "startup_deadline": bootstrap.startup_deadline,
        "caller_pid": bootstrap.caller_pid,
        "child_argv": list(bootstrap.child_argv),
    })


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
