"""Bounded primitive-only IPC for the trusted live-reader helper.

The multiprocessing bootstrap necessarily transfers Python ``Connection``
handles to the spawned child.  After bootstrap, every application frame is
canonical UTF-8 JSON sent with ``send_bytes``/``recv_bytes``.  This module is
the sole encoder/decoder for that wire format.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, TypeAlias

from qplot.datahandling.file_identity import (
    MAX_DATABASE_SIDECAR_IDENTITIES,
    DatabaseFileIdentity,
    DatabaseInstance,
)
from qplot.datahandling.trusted_live import (
    TRUSTED_LIVE_MAX_BATCH_QUERIES,
    TRUSTED_LIVE_MAX_CELLS_PER_REPLY,
    TRUSTED_LIVE_MAX_COLUMN_NAME_BYTES,
    TRUSTED_LIVE_MAX_COLUMNS_PER_RESULT,
    TRUSTED_LIVE_MAX_REPLY_BYTES,
    TRUSTED_LIVE_MAX_ROWS_PER_RESULT,
    TRUSTED_LIVE_MAX_SCALAR_BYTES,
    SqliteBindings,
    TrustedLiveReaderError,
    TrustedLiveSourceIdentity,
    TrustedQuery,
    TrustedQueryResult,
    preflight_trusted_query_results,
)

PROTOCOL_VERSION: Final = 1
MAX_REQUEST_BYTES: Final = 1 * 1024 * 1024
MAX_CONTROL_BYTES: Final = 4 * 1024
MAX_REPLY_BYTES: Final = TRUSTED_LIVE_MAX_REPLY_BYTES
MAX_SQL_BYTES: Final = 256 * 1024
MAX_BATCH_QUERIES: Final = TRUSTED_LIVE_MAX_BATCH_QUERIES
MAX_BINDINGS_PER_QUERY: Final = 4_096
MAX_BINDING_NAME_BYTES: Final = TRUSTED_LIVE_MAX_COLUMN_NAME_BYTES
MAX_COLUMNS_PER_RESULT: Final = TRUSTED_LIVE_MAX_COLUMNS_PER_RESULT
MAX_ROWS_PER_RESULT: Final = TRUSTED_LIVE_MAX_ROWS_PER_RESULT
MAX_CELLS_PER_REPLY: Final = TRUSTED_LIVE_MAX_CELLS_PER_REPLY
MAX_SCALAR_BYTES: Final = TRUSTED_LIVE_MAX_SCALAR_BYTES
MAX_ERROR_MESSAGE_BYTES: Final = 16 * 1024
MAX_PATH_BYTES: Final = 32 * 1024
MAX_JSON_NESTING: Final = 12
MAX_JSON_COLLECTION_ITEMS: Final = 4_500_000
MAX_GENERATION: Final = (1 << 63) - 1
SESSION_HEX_CHARS: Final = 32
MIN_OPERATION_TIMEOUT_MS: Final = 0
MAX_OPERATION_TIMEOUT_MS: Final = 300_000

_INTEGER_MIN = -(1 << 63)
_INTEGER_MAX = (1 << 63) - 1
_IDENTITY_INTEGER_MAX = (1 << 64) - 1
_MAX_REAL_HEX_CHARS = 32
_MAX_BASE64_SCALAR_CHARS = 4 * ((MAX_SCALAR_BYTES + 2) // 3)
_MAX_BUSY_TIMEOUT_MS = (1 << 31) - 1
_MIN_STARTUP_TIMEOUT_MS = 1
_MIN_ORPHAN_GRACE_MS = 1
_FRAME_ENVELOPE_RESERVE_BYTES = 512
_WIRE_CONTAINER_BUDGET_BYTES = 32
_WIRE_FIELD_BUDGET_BYTES = 16
_LOWERCASE_HEX_DIGITS = frozenset("0123456789abcdef")
_REQUEST_OPERATIONS = frozenset(
    {"startup", "query", "query_batch", "data_version", "shutdown"}
)
_REPLY_OPERATIONS = _REQUEST_OPERATIONS | {"startup", "protocol"}
_CONTROL_OPERATIONS = frozenset({"cancel"})
_ERROR_CODES = frozenset(
    {
        "reader_unavailable",
        "unsupported_source",
        "source_changed",
        "source_io",
        "sql_rejected",
        "query_failed",
        "result_limit",
        "busy_timeout",
        "cancelled",
        "operation_deadline",
        "invalid_database",
        "cleanup_quarantine",
        "reader_closed",
        "reader_thread",
        "transaction",
        "reader_error",
        "protocol_error",
        "internal_error",
    }
)

JsonValue: TypeAlias = (
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
)


class TrustedLiveProtocolValidationError(ValueError):
    """Raised when an IPC frame or payload fails closed validation."""


@dataclass(frozen=True, slots=True)
class ProtocolEnvelope:
    """One validated protocol envelope with an operation-specific payload."""

    session: str
    generation: int
    operation: str
    payload: dict[str, Any]


@dataclass(slots=True)
class _WireSizeBudget:
    """Conservatively bound aggregate canonical-wire construction."""

    maximum: int
    used: int = 0

    def consume(self, amount: int, description: str) -> None:
        if amount < 0 or amount > self.maximum - self.used:
            raise TrustedLiveProtocolValidationError(
                f"{description} exceeds the aggregate {self.maximum}-byte "
                "protocol wire budget."
            )
        self.used += amount


def _new_role_budget(maximum: int) -> _WireSizeBudget:
    budget = _WireSizeBudget(maximum)
    budget.consume(_FRAME_ENVELOPE_RESERVE_BYTES, "The protocol envelope")
    return budget


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TrustedLiveProtocolValidationError(
                f"Duplicate JSON object key {key!r} was rejected."
            )
        result[key] = value
    return result


def _text_sizes(value: str, description: str, maximum: int) -> tuple[int, int]:
    """Return UTF-8 and canonical JSON-string sizes without allocating either."""

    utf8_size = 0
    json_size = 2  # Opening and closing quotes.
    for character in value:
        codepoint = ord(character)
        if 0xD800 <= codepoint <= 0xDFFF:
            raise TrustedLiveProtocolValidationError(
                f"{description} must contain valid Unicode text."
            )
        if codepoint <= 0x7F:
            character_bytes = 1
        elif codepoint <= 0x7FF:
            character_bytes = 2
        elif codepoint <= 0xFFFF:
            character_bytes = 3
        else:
            character_bytes = 4
        utf8_size += character_bytes
        if utf8_size > maximum:
            raise TrustedLiveProtocolValidationError(
                f"{description} exceeds the {maximum}-byte protocol limit."
            )
        if character in {'"', "\\"}:
            json_size += 2
        elif codepoint <= 0x1F:
            json_size += 2 if character in "\b\t\n\f\r" else 6
        else:
            json_size += character_bytes
    return utf8_size, json_size


def _utf8_size(value: str, description: str, maximum: int) -> int:
    return _text_sizes(value, description, maximum)[0]


def _json_string_wire_size(value: str, description: str, maximum: int) -> int:
    return _text_sizes(value, description, maximum)[1]


def validate_session(session: object) -> str:
    if (
        type(session) is not str
        or len(session) != SESSION_HEX_CHARS
        or any(character not in _LOWERCASE_HEX_DIGITS for character in session)
    ):
        raise TrustedLiveProtocolValidationError(
            "The protocol session must be exactly 32 lowercase hexadecimal characters."
        )
    return session


def validate_generation(generation: object, *, allow_zero: bool) -> int:
    minimum = 0 if allow_zero else 1
    if (
        type(generation) is not int
        or generation < minimum
        or generation > MAX_GENERATION
    ):
        raise TrustedLiveProtocolValidationError(
            f"The protocol generation must be an integer from {minimum} "
            f"through {MAX_GENERATION}."
        )
    return generation


def _preflight_json_value(root: object, budget: _WireSizeBudget) -> None:
    """Bound a generic primitive JSON tree before ``json.dumps`` allocates."""

    active_collections: set[int] = set()
    collection_items = 0

    def visit(value: object, depth: int) -> None:
        nonlocal collection_items
        if depth > MAX_JSON_NESTING:
            raise TrustedLiveProtocolValidationError(
                "The protocol value exceeds the JSON nesting limit."
            )
        if isinstance(value, Mapping):
            identity = id(value)
            if identity in active_collections:
                raise TrustedLiveProtocolValidationError(
                    "The protocol value contains a circular collection."
                )
            collection_items += len(value)
            if collection_items > MAX_JSON_COLLECTION_ITEMS:
                raise TrustedLiveProtocolValidationError(
                    "The protocol value contains too many collection items."
                )
            # Count exact JSON punctuation here.  Operation-specific encoders
            # have already applied their own conservative resource budgets;
            # charging another arbitrary per-container reserve would reject
            # live results that are safely within both that budget and the
            # exact frame cap.
            budget.consume(
                2 + len(value) + max(0, len(value) - 1),
                "The protocol frame",
            )
            active_collections.add(identity)
            try:
                for key, child in value.items():
                    if type(key) is not str:
                        raise TrustedLiveProtocolValidationError(
                            "A protocol object key is not text."
                        )
                    budget.consume(
                        _json_string_wire_size(
                            key,
                            "A protocol object key",
                            budget.maximum,
                        ),
                        "The protocol frame",
                    )
                    visit(child, depth + 1)
            finally:
                active_collections.remove(identity)
            return
        if isinstance(value, (list, tuple)):
            identity = id(value)
            if identity in active_collections:
                raise TrustedLiveProtocolValidationError(
                    "The protocol value contains a circular collection."
                )
            collection_items += len(value)
            if collection_items > MAX_JSON_COLLECTION_ITEMS:
                raise TrustedLiveProtocolValidationError(
                    "The protocol value contains too many collection items."
                )
            budget.consume(2 + max(0, len(value) - 1), "The protocol frame")
            active_collections.add(identity)
            try:
                for child in value:
                    visit(child, depth + 1)
            finally:
                active_collections.remove(identity)
            return
        if value is None:
            budget.consume(4, "The protocol frame")
            return
        if isinstance(value, bool):
            budget.consume(5, "The protocol frame")
            return
        if type(value) is int:
            try:
                integer_size = len(str(value))
            except ValueError as error:
                raise TrustedLiveProtocolValidationError(
                    "A protocol integer is too large to encode."
                ) from error
            budget.consume(integer_size, "The protocol frame")
            return
        if isinstance(value, float):
            budget.consume(32, "The protocol frame")
            return
        if isinstance(value, str):
            budget.consume(
                _json_string_wire_size(
                    value,
                    "A protocol text value",
                    budget.maximum,
                ),
                "The protocol frame",
            )
            return
        raise TrustedLiveProtocolValidationError(
            "The protocol payload is not canonical JSON primitive data."
        )

    # The eventual envelope object occupies nesting level one.
    visit(root, 2)


def encode_frame(
    session: str,
    generation: int,
    operation: str,
    payload: Mapping[str, Any],
    *,
    maximum_bytes: int,
) -> bytes:
    """Encode one exact envelope and enforce its role-specific frame cap."""

    validate_session(session)
    validate_generation(generation, allow_zero=True)
    if not isinstance(operation, str) or not operation:
        raise TrustedLiveProtocolValidationError(
            "The protocol operation must be non-empty text."
        )
    if not isinstance(payload, Mapping):
        raise TrustedLiveProtocolValidationError(
            "The protocol payload must be an object."
        )
    budget = _new_role_budget(maximum_bytes)
    budget.consume(
        _json_string_wire_size(
            operation,
            "The protocol operation",
            maximum_bytes,
        ),
        "The protocol envelope",
    )
    _preflight_json_value(payload, budget)
    envelope = {
        "generation": generation,
        "operation": operation,
        "payload": dict(payload),
        "protocol_version": PROTOCOL_VERSION,
        "session": session,
    }
    try:
        encoded = json.dumps(
            envelope,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise TrustedLiveProtocolValidationError(
            "The protocol payload is not canonical JSON primitive data."
        ) from error
    if not encoded or len(encoded) > maximum_bytes:
        raise TrustedLiveProtocolValidationError(
            f"The encoded protocol frame exceeds the {maximum_bytes}-byte limit."
        )
    return encoded


def decode_frame(
    frame: bytes,
    *,
    maximum_bytes: int,
    allowed_operations: frozenset[str],
) -> ProtocolEnvelope:
    """Decode and validate the common envelope without trusting pickle."""

    if type(frame) is not bytes or not frame or len(frame) > maximum_bytes:
        raise TrustedLiveProtocolValidationError(
            f"The protocol frame must contain 1 through {maximum_bytes} bytes."
        )
    try:
        decoded = json.loads(
            frame.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: _raise_invalid_constant(value),
            parse_float=_parse_finite_json_float,
        )
    except TrustedLiveProtocolValidationError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise TrustedLiveProtocolValidationError(
            "The protocol frame is not valid bounded UTF-8 JSON."
        ) from error
    try:
        _validate_json_tree(decoded)
    except RecursionError as error:
        raise TrustedLiveProtocolValidationError(
            "The protocol frame exceeds the JSON nesting limit."
        ) from error
    if not isinstance(decoded, dict) or set(decoded) != {
        "protocol_version",
        "session",
        "generation",
        "operation",
        "payload",
    }:
        raise TrustedLiveProtocolValidationError(
            "The protocol envelope has missing, extra, or invalid fields."
        )
    if type(decoded["protocol_version"]) is not int or (
        decoded["protocol_version"] != PROTOCOL_VERSION
    ):
        raise TrustedLiveProtocolValidationError("The protocol version is unsupported.")
    session = validate_session(decoded["session"])
    generation = validate_generation(decoded["generation"], allow_zero=True)
    operation = decoded["operation"]
    if not isinstance(operation, str) or operation not in allowed_operations:
        raise TrustedLiveProtocolValidationError(
            "The protocol operation is unknown for this channel."
        )
    payload = decoded["payload"]
    if not isinstance(payload, dict):
        raise TrustedLiveProtocolValidationError(
            "The protocol payload must be an object."
        )
    return ProtocolEnvelope(session, generation, operation, payload)


def _validate_json_tree(root: object) -> None:
    """Bound nesting and collection fan-out before shape-specific use."""

    collection_items = 0

    def visit(value: object, depth: int) -> None:
        nonlocal collection_items
        if isinstance(value, dict):
            if depth > MAX_JSON_NESTING:
                raise TrustedLiveProtocolValidationError(
                    "The protocol frame exceeds the JSON nesting limit."
                )
            collection_items += len(value)
            if collection_items > MAX_JSON_COLLECTION_ITEMS:
                raise TrustedLiveProtocolValidationError(
                    "The protocol frame contains too many collection items."
                )
            for key, child in value.items():
                if not isinstance(key, str):
                    raise TrustedLiveProtocolValidationError(
                        "A protocol object key is not text."
                    )
                visit(child, depth + 1)
        elif isinstance(value, list):
            if depth > MAX_JSON_NESTING:
                raise TrustedLiveProtocolValidationError(
                    "The protocol frame exceeds the JSON nesting limit."
                )
            collection_items += len(value)
            if collection_items > MAX_JSON_COLLECTION_ITEMS:
                raise TrustedLiveProtocolValidationError(
                    "The protocol frame contains too many collection items."
                )
            for child in value:
                visit(child, depth + 1)
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise TrustedLiveProtocolValidationError(
                    "The protocol contains a non-finite JSON number."
                )
        elif value is not None and not isinstance(value, (bool, int, str)):
            raise TrustedLiveProtocolValidationError(
                "The protocol contains a non-primitive JSON value."
            )

    visit(root, 1)


def _raise_invalid_constant(value: str) -> None:
    raise TrustedLiveProtocolValidationError(
        f"The non-finite JSON constant {value!r} was rejected."
    )


def _parse_finite_json_float(value: str) -> float:
    decoded = float(value)
    if not math.isfinite(decoded):
        raise TrustedLiveProtocolValidationError(
            f"The non-finite JSON number {value!r} was rejected."
        )
    return decoded


def decode_request_frame(frame: bytes) -> ProtocolEnvelope:
    return decode_frame(
        frame,
        maximum_bytes=MAX_REQUEST_BYTES,
        allowed_operations=_REQUEST_OPERATIONS,
    )


def decode_control_frame(frame: bytes) -> ProtocolEnvelope:
    return decode_frame(
        frame,
        maximum_bytes=MAX_CONTROL_BYTES,
        allowed_operations=_CONTROL_OPERATIONS,
    )


def decode_reply_frame(frame: bytes) -> ProtocolEnvelope:
    return decode_frame(
        frame,
        maximum_bytes=MAX_REPLY_BYTES,
        allowed_operations=_REPLY_OPERATIONS,
    )


def _scalar_wire_size(value: object) -> int:
    """Validate one scalar and conservatively size its tagged JSON form."""

    if value is None:
        return 8
    if isinstance(value, bool):
        return 16
    if isinstance(value, int):
        if value < _INTEGER_MIN or value > _INTEGER_MAX:
            raise TrustedLiveProtocolValidationError(
                "SQLite integer values must fit in a signed 64-bit integer."
            )
        return 16 + len(str(value))
    if isinstance(value, float):
        return 16 + len(value.hex())
    if isinstance(value, str):
        return 16 + _json_string_wire_size(
            value,
            "A SQLite text value",
            MAX_SCALAR_BYTES,
        )
    if isinstance(value, memoryview):
        blob_size = value.nbytes
    elif isinstance(value, (bytes, bytearray)):
        blob_size = len(value)
    else:
        raise TrustedLiveProtocolValidationError(
            "Only SQLite null, integer, real, text, and blob scalars may cross IPC."
        )
    if blob_size > MAX_SCALAR_BYTES:
        raise TrustedLiveProtocolValidationError(
            f"A SQLite blob exceeds the {MAX_SCALAR_BYTES}-byte limit."
        )
    base64_size = 4 * ((blob_size + 2) // 3)
    return 16 + base64_size


def _preflight_scalar(
    value: object,
    budget: _WireSizeBudget,
    description: str,
) -> None:
    budget.consume(_scalar_wire_size(value), description)


def _encode_scalar(value: object) -> list[JsonValue]:
    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["integer", int(value)]
    if isinstance(value, int):
        if value < _INTEGER_MIN or value > _INTEGER_MAX:
            raise TrustedLiveProtocolValidationError(
                "SQLite integer values must fit in a signed 64-bit integer."
            )
        return ["integer", value]
    if isinstance(value, float):
        return ["real", value.hex()]
    if isinstance(value, str):
        _utf8_size(value, "A SQLite text value", MAX_SCALAR_BYTES)
        return ["text", value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        blob = bytes(value)
        if len(blob) > MAX_SCALAR_BYTES:
            raise TrustedLiveProtocolValidationError(
                f"A SQLite blob exceeds the {MAX_SCALAR_BYTES}-byte limit."
            )
        return ["blob", base64.b64encode(blob).decode("ascii")]
    raise TrustedLiveProtocolValidationError(
        "Only SQLite null, integer, real, text, and blob scalars may cross IPC."
    )


def _decode_scalar(encoded: object) -> None | int | float | str | bytes:
    if not isinstance(encoded, list) or not encoded or not isinstance(encoded[0], str):
        raise TrustedLiveProtocolValidationError(
            "A SQLite scalar has an invalid tagged representation."
        )
    tag = encoded[0]
    if tag == "null" and len(encoded) == 1:
        return None
    if tag == "integer" and len(encoded) == 2:
        value = encoded[1]
        if type(value) is int and _INTEGER_MIN <= value <= _INTEGER_MAX:
            return value
    elif (
        tag == "real"
        and len(encoded) == 2
        and isinstance(encoded[1], str)
        and len(encoded[1]) <= _MAX_REAL_HEX_CHARS
    ):
        try:
            value = float.fromhex(encoded[1])
        except ValueError:
            pass
        else:
            if value.hex() == encoded[1]:
                return value
    elif tag == "text" and len(encoded) == 2 and isinstance(encoded[1], str):
        _utf8_size(encoded[1], "A SQLite text value", MAX_SCALAR_BYTES)
        return encoded[1]
    elif (
        tag == "blob"
        and len(encoded) == 2
        and isinstance(encoded[1], str)
        and len(encoded[1]) <= _MAX_BASE64_SCALAR_CHARS
    ):
        try:
            value = base64.b64decode(encoded[1], validate=True)
        except (ValueError, binascii.Error):
            pass
        else:
            if len(value) <= MAX_SCALAR_BYTES:
                return value
    raise TrustedLiveProtocolValidationError(
        "A SQLite scalar has an invalid or oversized tagged value."
    )


def _preflight_bindings(
    bindings: SqliteBindings,
    budget: _WireSizeBudget,
) -> None:
    budget.consume(_WIRE_CONTAINER_BUDGET_BYTES, "Query bindings")
    if bindings is None:
        return
    if isinstance(bindings, Mapping):
        if len(bindings) > MAX_BINDINGS_PER_QUERY:
            raise TrustedLiveProtocolValidationError(
                "A query has too many named bindings."
            )
        seen: set[str] = set()
        for key, value in bindings.items():
            if not isinstance(key, str) or not key or key in seen:
                raise TrustedLiveProtocolValidationError(
                    "Named binding keys must be unique non-empty text."
                )
            key_size = _json_string_wire_size(
                key,
                "A named binding key",
                MAX_BINDING_NAME_BYTES,
            )
            seen.add(key)
            budget.consume(
                _WIRE_CONTAINER_BUDGET_BYTES + _WIRE_FIELD_BUDGET_BYTES + key_size,
                "Named query bindings",
            )
            _preflight_scalar(value, budget, "Query binding values")
        return
    if isinstance(bindings, (str, bytes, bytearray, memoryview)):
        values: Sequence[object] = (bindings,)
    elif isinstance(bindings, Sequence):
        values = bindings
    else:
        raise TrustedLiveProtocolValidationError(
            "SQLite bindings must be a sequence, mapping, scalar text/blob, or None."
        )
    if len(values) > MAX_BINDINGS_PER_QUERY:
        raise TrustedLiveProtocolValidationError("A query has too many bindings.")
    for value in values:
        budget.consume(_WIRE_FIELD_BUDGET_BYTES, "Sequential query bindings")
        _preflight_scalar(value, budget, "Query binding values")


def _bounded_binding_references(bindings: SqliteBindings) -> SqliteBindings:
    """Materialise at most the binding-count cap without copying blob data."""

    if bindings is None:
        return None
    if isinstance(bindings, Mapping):
        try:
            binding_count = len(bindings)
        except Exception as error:
            raise TrustedLiveProtocolValidationError(
                "Named query bindings do not expose a finite size."
            ) from error
        if binding_count > MAX_BINDINGS_PER_QUERY:
            raise TrustedLiveProtocolValidationError(
                "A query has too many named bindings."
            )

        try:
            iterator = iter(bindings.items())
        except Exception as error:
            raise TrustedLiveProtocolValidationError(
                "Named query bindings cannot be inspected safely."
            ) from error
        referenced: dict[str, Any] = {}
        for _ in range(binding_count):
            try:
                item = next(iterator)
            except StopIteration as error:
                raise TrustedLiveProtocolValidationError(
                    "Named query bindings changed while being inspected."
                ) from error
            except Exception as error:
                raise TrustedLiveProtocolValidationError(
                    "Named query bindings cannot be inspected safely."
                ) from error
            if not isinstance(item, tuple) or len(item) != 2:
                raise TrustedLiveProtocolValidationError(
                    "Named query bindings contain a malformed item."
                )
            key, value = item
            if not isinstance(key, str) or not key or key in referenced:
                raise TrustedLiveProtocolValidationError(
                    "Named binding keys must be unique non-empty text."
                )
            referenced[key] = value
        try:
            next(iterator)
        except StopIteration:
            pass
        except Exception as error:
            raise TrustedLiveProtocolValidationError(
                "Named query bindings cannot be inspected safely."
            ) from error
        else:
            raise TrustedLiveProtocolValidationError(
                "Named query bindings changed while being inspected."
            )
        return referenced
    elif isinstance(bindings, (str, bytes, bytearray, memoryview)):
        # These are single SQLite scalars, never collections of characters or
        # integer bytes. In particular, Stage 2 otherwise expands memoryview
        # into a tuple of integers in TrustedQuery.__post_init__.
        return (bindings,)
    elif isinstance(bindings, Sequence):
        try:
            binding_count = len(bindings)
        except Exception as error:
            raise TrustedLiveProtocolValidationError(
                "Sequential query bindings do not expose a finite size."
            ) from error
        if binding_count > MAX_BINDINGS_PER_QUERY:
            raise TrustedLiveProtocolValidationError("A query has too many bindings.")
        referenced_values: list[Any] = []
        for index in range(binding_count):
            try:
                referenced_values.append(bindings[index])
            except Exception as error:
                raise TrustedLiveProtocolValidationError(
                    "Sequential query bindings changed while being inspected."
                ) from error
        return tuple(referenced_values)
    raise TrustedLiveProtocolValidationError(
        "SQLite bindings must be a sequence, mapping, scalar text/blob, or None."
    )


def _freeze_binding_scalar(
    value: Any,
    budget: _WireSizeBudget,
) -> Any:
    """Charge and snapshot one scalar without an unbounded mutable-blob copy."""

    if isinstance(value, bytearray):
        view: memoryview | None = None
        try:
            # Exporting a view prevents another thread from resizing the
            # bytearray between sizing and copying it.
            view = memoryview(value)
            _preflight_scalar(view, budget, "Query binding values")
            return view.tobytes()
        except TrustedLiveProtocolValidationError:
            raise
        except (BufferError, TypeError, ValueError) as error:
            raise TrustedLiveProtocolValidationError(
                "A mutable SQLite blob binding could not be snapshotted safely."
            ) from error
        finally:
            if view is not None:
                view.release()
    if isinstance(value, memoryview):
        try:
            _preflight_scalar(value, budget, "Query binding values")
            return value.tobytes()
        except TrustedLiveProtocolValidationError:
            raise
        except (BufferError, TypeError, ValueError) as error:
            raise TrustedLiveProtocolValidationError(
                "A SQLite blob view could not be snapshotted safely."
            ) from error
    _preflight_scalar(value, budget, "Query binding values")
    return value


def _freeze_bindings_with_budget(
    bindings: SqliteBindings,
    budget: _WireSizeBudget,
) -> SqliteBindings:
    """Validate and freeze bounded binding references into a shared budget."""

    budget.consume(_WIRE_CONTAINER_BUDGET_BYTES, "Query bindings")
    if bindings is None:
        return None
    if isinstance(bindings, Mapping):
        frozen_mapping: dict[str, Any] = {}
        for key, value in bindings.items():
            key_size = _json_string_wire_size(
                key,
                "A named binding key",
                MAX_BINDING_NAME_BYTES,
            )
            budget.consume(
                _WIRE_CONTAINER_BUDGET_BYTES + _WIRE_FIELD_BUDGET_BYTES + key_size,
                "Named query bindings",
            )
            frozen_mapping[key] = _freeze_binding_scalar(value, budget)
        return frozen_mapping
    frozen_values: list[Any] = []
    for value in bindings:
        budget.consume(_WIRE_FIELD_BUDGET_BYTES, "Sequential query bindings")
        frozen_values.append(_freeze_binding_scalar(value, budget))
    return tuple(frozen_values)


def _normalize_query_with_budget(
    sql: object,
    bindings: SqliteBindings,
    budget: _WireSizeBudget,
) -> TrustedQuery:
    if not isinstance(sql, str):
        raise TrustedLiveProtocolValidationError("Query SQL must be text.")
    sql_size = _json_string_wire_size(sql, "Query SQL", MAX_SQL_BYTES)
    budget.consume(
        _WIRE_CONTAINER_BUDGET_BYTES + _WIRE_FIELD_BUDGET_BYTES + sql_size,
        "Query specifications",
    )
    bounded = _bounded_binding_references(bindings)
    frozen = _freeze_bindings_with_budget(bounded, budget)
    return TrustedQuery(sql, frozen)


def normalize_query_specification(
    sql: object,
    bindings: SqliteBindings,
) -> TrustedQuery:
    """Build one bounded query before ``TrustedQuery`` can copy raw input."""

    budget = _new_role_budget(MAX_REQUEST_BYTES)
    budget.consume(_WIRE_CONTAINER_BUDGET_BYTES, "Query specifications")
    return _normalize_query_with_budget(sql, bindings, budget)


def normalize_query_batch(
    queries: Sequence[TrustedQuery],
) -> tuple[TrustedQuery, ...]:
    """Snapshot one finite batch within a single aggregate request budget."""

    if not isinstance(queries, Sequence) or isinstance(
        queries, (str, bytes, bytearray, memoryview)
    ):
        raise TrustedLiveProtocolValidationError(
            "Query specifications must be a bounded sequence."
        )
    try:
        query_count = len(queries)
    except Exception as error:
        raise TrustedLiveProtocolValidationError(
            "Query specifications do not expose a finite size."
        ) from error
    if not 1 <= query_count <= MAX_BATCH_QUERIES:
        raise TrustedLiveProtocolValidationError(
            f"A query batch must contain 1 through {MAX_BATCH_QUERIES} statements."
        )
    specifications: list[TrustedQuery] = []
    for index in range(query_count):
        try:
            query = queries[index]
        except Exception as error:
            raise TrustedLiveProtocolValidationError(
                "Query specifications changed while being inspected."
            ) from error
        if not isinstance(query, TrustedQuery):
            raise TrustedLiveProtocolValidationError(
                "Query batches accept only TrustedQuery specifications."
            )
        specifications.append(query)

    budget = _new_role_budget(MAX_REQUEST_BYTES)
    budget.consume(_WIRE_CONTAINER_BUDGET_BYTES, "Query specifications")
    return tuple(
        _normalize_query_with_budget(query.sql, query.bindings, budget)
        for query in specifications
    )


def _encode_bindings_prevalidated(bindings: SqliteBindings) -> dict[str, JsonValue]:
    if bindings is None:
        return {"kind": "none"}
    if isinstance(bindings, Mapping):
        return {
            "kind": "mapping",
            "items": [[key, _encode_scalar(value)] for key, value in bindings.items()],
        }
    if isinstance(bindings, (str, bytes, bytearray, memoryview)):
        values: Sequence[object] = (bindings,)
    else:
        values = bindings
    return {"kind": "sequence", "values": [_encode_scalar(value) for value in values]}


def encode_bindings(bindings: SqliteBindings) -> dict[str, JsonValue]:
    budget = _new_role_budget(MAX_REQUEST_BYTES)
    _preflight_bindings(bindings, budget)
    return _encode_bindings_prevalidated(bindings)


def decode_bindings(encoded: object) -> SqliteBindings:
    if not isinstance(encoded, dict) or "kind" not in encoded:
        raise TrustedLiveProtocolValidationError("Query bindings are malformed.")
    kind = encoded["kind"]
    if kind == "none" and set(encoded) == {"kind"}:
        return None
    if kind == "sequence" and set(encoded) == {"kind", "values"}:
        values = encoded["values"]
        if not isinstance(values, list) or len(values) > MAX_BINDINGS_PER_QUERY:
            raise TrustedLiveProtocolValidationError(
                "Sequential query bindings are malformed or oversized."
            )
        return tuple(_decode_scalar(value) for value in values)
    if kind == "mapping" and set(encoded) == {"kind", "items"}:
        items = encoded["items"]
        if not isinstance(items, list) or len(items) > MAX_BINDINGS_PER_QUERY:
            raise TrustedLiveProtocolValidationError(
                "Named query bindings are malformed or oversized."
            )
        result: dict[str, object] = {}
        for item in items:
            if (
                not isinstance(item, list)
                or len(item) != 2
                or not isinstance(item[0], str)
                or not item[0]
                or item[0] in result
            ):
                raise TrustedLiveProtocolValidationError(
                    "Named query bindings contain an invalid or duplicate key."
                )
            _utf8_size(item[0], "A named binding key", MAX_BINDING_NAME_BYTES)
            result[item[0]] = _decode_scalar(item[1])
        return result
    raise TrustedLiveProtocolValidationError("Query bindings are malformed.")


def _preflight_queries(
    specifications: tuple[TrustedQuery, ...],
    budget: _WireSizeBudget,
) -> None:
    budget.consume(_WIRE_CONTAINER_BUDGET_BYTES, "Query specifications")
    if not specifications or len(specifications) > MAX_BATCH_QUERIES:
        raise TrustedLiveProtocolValidationError(
            f"A query batch must contain 1 through {MAX_BATCH_QUERIES} statements."
        )
    for query in specifications:
        if not isinstance(query, TrustedQuery):
            raise TrustedLiveProtocolValidationError(
                "Query batches accept only TrustedQuery specifications."
            )
        if not isinstance(query.sql, str):
            raise TrustedLiveProtocolValidationError("Query SQL must be text.")
        sql_size = _json_string_wire_size(query.sql, "Query SQL", MAX_SQL_BYTES)
        budget.consume(
            _WIRE_CONTAINER_BUDGET_BYTES + _WIRE_FIELD_BUDGET_BYTES + sql_size,
            "Query specifications",
        )
        _preflight_bindings(query.bindings, budget)


def encode_queries(queries: Sequence[TrustedQuery]) -> list[JsonValue]:
    if not isinstance(queries, Sequence) or isinstance(
        queries, (str, bytes, bytearray, memoryview)
    ):
        raise TrustedLiveProtocolValidationError(
            "Query specifications must be a bounded sequence."
        )
    query_count = len(queries)
    if not 1 <= query_count <= MAX_BATCH_QUERIES:
        raise TrustedLiveProtocolValidationError(
            f"A query batch must contain 1 through {MAX_BATCH_QUERIES} statements."
        )
    specifications = tuple(queries[index] for index in range(query_count))
    budget = _new_role_budget(MAX_REQUEST_BYTES)
    _preflight_queries(specifications, budget)
    return [
        {
            "bindings": _encode_bindings_prevalidated(query.bindings),
            "sql": query.sql,
        }
        for query in specifications
    ]


def decode_queries(encoded: object) -> tuple[TrustedQuery, ...]:
    if not isinstance(encoded, list) or not encoded or len(encoded) > MAX_BATCH_QUERIES:
        raise TrustedLiveProtocolValidationError(
            "The query list is empty, malformed, or oversized."
        )
    queries: list[TrustedQuery] = []
    for item in encoded:
        if not isinstance(item, dict) or set(item) != {"sql", "bindings"}:
            raise TrustedLiveProtocolValidationError(
                "A query specification has invalid fields."
            )
        sql = item["sql"]
        if not isinstance(sql, str):
            raise TrustedLiveProtocolValidationError("Query SQL must be text.")
        _utf8_size(sql, "Query SQL", MAX_SQL_BYTES)
        queries.append(TrustedQuery(sql, decode_bindings(item["bindings"])))
    return tuple(queries)


def validate_timeout_ms(value: object) -> int:
    if (
        type(value) is not int
        or value < MIN_OPERATION_TIMEOUT_MS
        or value > MAX_OPERATION_TIMEOUT_MS
    ):
        raise TrustedLiveProtocolValidationError(
            "The child operation timeout is outside the bounded protocol range."
        )
    return value


def encode_job_request(
    session: str,
    generation: int,
    operation: str,
    queries: Sequence[TrustedQuery] | None,
    timeout_ms: int,
) -> bytes:
    validate_generation(generation, allow_zero=False)
    validate_timeout_ms(timeout_ms)
    if operation == "query":
        encoded_queries = encode_queries(queries or ())
        if len(encoded_queries) != 1:
            raise TrustedLiveProtocolValidationError(
                "A query job must contain exactly one statement."
            )
        payload: dict[str, Any] = {
            "queries": encoded_queries,
            "timeout_ms": timeout_ms,
        }
    elif operation == "query_batch":
        payload = {
            "queries": encode_queries(queries or ()),
            "timeout_ms": timeout_ms,
        }
    elif operation == "data_version" and queries is None:
        payload = {"timeout_ms": timeout_ms}
    else:
        raise TrustedLiveProtocolValidationError(
            "The requested job operation is invalid."
        )
    return encode_frame(
        session,
        generation,
        operation,
        payload,
        maximum_bytes=MAX_REQUEST_BYTES,
    )


def decode_job_request(
    envelope: ProtocolEnvelope,
) -> tuple[tuple[TrustedQuery, ...] | None, int]:
    payload = envelope.payload
    if envelope.operation in {"query", "query_batch"}:
        if set(payload) != {"queries", "timeout_ms"}:
            raise TrustedLiveProtocolValidationError(
                "The query request has invalid fields."
            )
        queries = decode_queries(payload["queries"])
        if envelope.operation == "query" and len(queries) != 1:
            raise TrustedLiveProtocolValidationError(
                "A query request must contain exactly one statement."
            )
        return queries, validate_timeout_ms(payload["timeout_ms"])
    if envelope.operation == "data_version":
        if set(payload) != {"timeout_ms"}:
            raise TrustedLiveProtocolValidationError(
                "The data-version request has invalid fields."
            )
        return None, validate_timeout_ms(payload["timeout_ms"])
    raise TrustedLiveProtocolValidationError("The envelope is not a database job.")


def encode_cancel(session: str, generation: int) -> bytes:
    validate_generation(generation, allow_zero=False)
    return encode_frame(
        session,
        generation,
        "cancel",
        {},
        maximum_bytes=MAX_CONTROL_BYTES,
    )


def validate_cancel(envelope: ProtocolEnvelope) -> None:
    if envelope.operation != "cancel" or envelope.payload:
        raise TrustedLiveProtocolValidationError(
            "A cancellation frame has invalid fields."
        )
    validate_generation(envelope.generation, allow_zero=False)


def encode_shutdown(session: str, generation: int) -> bytes:
    validate_generation(generation, allow_zero=False)
    return encode_frame(
        session,
        generation,
        "shutdown",
        {},
        maximum_bytes=MAX_REQUEST_BYTES,
    )


def validate_shutdown(envelope: ProtocolEnvelope) -> None:
    if envelope.operation != "shutdown" or envelope.payload:
        raise TrustedLiveProtocolValidationError("A shutdown frame has invalid fields.")


def _identity_field_wire_sizes(identity: object) -> tuple[int, ...]:
    if not isinstance(identity, tuple):
        raise TrustedLiveProtocolValidationError("A database file identity is invalid.")
    valid_shape = (
        len(identity) == 2 and type(identity[0]) is int and type(identity[1]) is int
    ) or (
        len(identity) == 3
        and type(identity[0]) is str
        and type(identity[2]) is int
        and type(identity[1]) in {str, int}
    )
    if not valid_shape:
        raise TrustedLiveProtocolValidationError(
            "A database file identity has an unsupported shape."
        )
    sizes: list[int] = []
    for index, value in enumerate(identity):
        if type(value) is str:
            maximum = (
                MAX_PATH_BYTES
                if len(identity) == 3 and identity[0] == "birthtime" and index == 1
                else 1_024
            )
            sizes.append(
                _json_string_wire_size(
                    value,
                    "A database identity field",
                    maximum,
                )
            )
        elif type(value) is int and 0 <= value <= _IDENTITY_INTEGER_MAX:
            sizes.append(len(str(value)))
        else:
            raise TrustedLiveProtocolValidationError(
                "Database identity integers must be non-negative unsigned "
                "64-bit values."
            )
    return tuple(sizes)


def _encode_identity_prevalidated(
    identity: DatabaseFileIdentity | None,
) -> JsonValue:
    return None if identity is None else list(identity)


def _preflight_identity(
    identity: DatabaseFileIdentity | None,
    budget: _WireSizeBudget,
    description: str,
) -> None:
    budget.consume(_WIRE_CONTAINER_BUDGET_BYTES, description)
    if identity is None:
        return
    for value_size in _identity_field_wire_sizes(identity):
        budget.consume(
            _WIRE_FIELD_BUDGET_BYTES + value_size,
            description,
        )


def _decode_identity(encoded: object) -> DatabaseFileIdentity | None:
    if encoded is None:
        return None
    if not isinstance(encoded, list) or len(encoded) not in {2, 3}:
        raise TrustedLiveProtocolValidationError("A database file identity is invalid.")
    identity = tuple(encoded)
    _identity_field_wire_sizes(identity)
    if len(identity) == 2 and all(type(value) is int for value in identity):
        return (identity[0], identity[1])
    if (
        len(identity) == 3
        and isinstance(identity[0], str)
        and type(identity[1]) is int
        and type(identity[2]) is int
    ):
        return (identity[0], identity[1], identity[2])
    if (
        len(identity) == 3
        and isinstance(identity[0], str)
        and isinstance(identity[1], str)
        and type(identity[2]) is int
    ):
        return (identity[0], identity[1], identity[2])
    raise TrustedLiveProtocolValidationError(
        "A database file identity has an unsupported shape."
    )


def _validate_database_instance_path(value: object, description: str) -> str:
    if type(value) is not str or not value or "\x00" in value:
        raise TrustedLiveProtocolValidationError(
            f"{description} must be non-empty text without NUL bytes."
        )
    _utf8_size(value, description, MAX_PATH_BYTES)
    return value


def _preflight_database_instance(
    instance: DatabaseInstance | None,
    budget: _WireSizeBudget,
) -> None:
    budget.consume(_WIRE_CONTAINER_BUDGET_BYTES, "Database instance")
    if instance is None:
        return
    if not isinstance(instance, DatabaseInstance):
        raise TrustedLiveProtocolValidationError(
            "The expected database instance has an invalid type."
        )
    logical_path = _validate_database_instance_path(
        instance.logical_path,
        "The logical database path",
    )
    resolved_path = _validate_database_instance_path(
        instance.resolved_path,
        "The resolved database path",
    )
    logical_path_size = _json_string_wire_size(
        logical_path,
        "The logical database path",
        MAX_PATH_BYTES,
    )
    resolved_path_size = _json_string_wire_size(
        resolved_path,
        "The resolved database path",
        MAX_PATH_BYTES,
    )
    budget.consume(
        (2 * _WIRE_FIELD_BUDGET_BYTES) + logical_path_size + resolved_path_size,
        "Database instance paths",
    )
    if len(instance.sidecar_identities) > MAX_DATABASE_SIDECAR_IDENTITIES:
        raise TrustedLiveProtocolValidationError(
            "A database instance has too many sidecar identities."
        )
    _preflight_identity(instance.identity, budget, "Database main-file identity")
    for identity in instance.sidecar_identities:
        _preflight_identity(identity, budget, "Database sidecar identities")


def encode_database_instance(instance: DatabaseInstance | None) -> JsonValue:
    budget = _new_role_budget(MAX_REQUEST_BYTES)
    _preflight_database_instance(instance, budget)
    if instance is None:
        return None
    sidecars = [
        _encode_identity_prevalidated(identity)
        for identity in instance.sidecar_identities
    ]
    sidecars.sort(key=lambda value: json.dumps(value, separators=(",", ":")))
    return {
        "identity": _encode_identity_prevalidated(instance.identity),
        "logical_path": instance.logical_path,
        "resolved_path": instance.resolved_path,
        "sidecar_identities": sidecars,
    }


def decode_database_instance(encoded: object) -> DatabaseInstance | None:
    if encoded is None:
        return None
    if not isinstance(encoded, dict) or set(encoded) != {
        "identity",
        "logical_path",
        "resolved_path",
        "sidecar_identities",
    }:
        raise TrustedLiveProtocolValidationError(
            "A database instance has invalid fields."
        )
    logical_path = encoded["logical_path"]
    resolved_path = encoded["resolved_path"]
    sidecars = encoded["sidecar_identities"]
    logical_path = _validate_database_instance_path(
        logical_path,
        "The logical database path",
    )
    resolved_path = _validate_database_instance_path(
        resolved_path,
        "The resolved database path",
    )
    if (
        not isinstance(sidecars, list)
        or len(sidecars) > MAX_DATABASE_SIDECAR_IDENTITIES
    ):
        raise TrustedLiveProtocolValidationError(
            "Database sidecar identities are malformed or oversized."
        )
    decoded_sidecars = [_decode_identity(identity) for identity in sidecars]
    if any(identity is None for identity in decoded_sidecars):
        raise TrustedLiveProtocolValidationError(
            "A sidecar identity must not be absent inside the identity list."
        )
    sidecar_set = frozenset(decoded_sidecars)
    if len(sidecar_set) != len(decoded_sidecars):
        raise TrustedLiveProtocolValidationError(
            "Database sidecar identities must be unique."
        )
    return DatabaseInstance(
        logical_path=logical_path,
        resolved_path=resolved_path,
        identity=_decode_identity(encoded["identity"]),
        sidecar_identities=sidecar_set,  # type: ignore[arg-type]
    )


def _validate_startup_database_path(value: object) -> str:
    if type(value) is not str or not value or "\x00" in value:
        raise TrustedLiveProtocolValidationError(
            "The startup database path must be non-empty text without NUL bytes."
        )
    _utf8_size(value, "The startup database path", MAX_PATH_BYTES)
    return value


def _validate_startup_integer(
    value: object,
    *,
    minimum: int,
    maximum: int,
    description: str,
) -> int:
    if type(value) is not int or value < minimum or value > maximum:
        raise TrustedLiveProtocolValidationError(
            f"{description} must be an integer from {minimum} through {maximum}."
        )
    return value


def encode_startup_request(
    session: str,
    first_generation: int,
    database_path: str,
    expected_database_instance: DatabaseInstance | None,
    busy_timeout_ms: int,
    operation_timeout_ms: int,
    orphan_grace_ms: int,
) -> bytes:
    """Encode all application startup configuration in one bounded frame."""

    validate_generation(first_generation, allow_zero=False)
    validated_path = _validate_startup_database_path(database_path)
    validated_busy_timeout = _validate_startup_integer(
        busy_timeout_ms,
        minimum=0,
        maximum=_MAX_BUSY_TIMEOUT_MS,
        description="The startup busy timeout",
    )
    validated_operation_timeout = _validate_startup_integer(
        operation_timeout_ms,
        minimum=_MIN_STARTUP_TIMEOUT_MS,
        maximum=MAX_OPERATION_TIMEOUT_MS,
        description="The startup operation timeout",
    )
    validated_orphan_grace = _validate_startup_integer(
        orphan_grace_ms,
        minimum=_MIN_ORPHAN_GRACE_MS,
        maximum=MAX_OPERATION_TIMEOUT_MS,
        description="The startup orphan grace period",
    )
    budget = _new_role_budget(MAX_REQUEST_BYTES)
    budget.consume(
        _WIRE_CONTAINER_BUDGET_BYTES
        + (6 * _WIRE_FIELD_BUDGET_BYTES)
        + _json_string_wire_size(
            validated_path,
            "The startup database path",
            MAX_PATH_BYTES,
        ),
        "The startup request",
    )
    _preflight_database_instance(expected_database_instance, budget)
    payload = {
        "busy_timeout_ms": validated_busy_timeout,
        "database_path": validated_path,
        "expected_database_instance": encode_database_instance(
            expected_database_instance
        ),
        "first_generation": first_generation,
        "operation_timeout_ms": validated_operation_timeout,
        "orphan_grace_ms": validated_orphan_grace,
    }
    return encode_frame(
        session,
        0,
        "startup",
        payload,
        maximum_bytes=MAX_REQUEST_BYTES,
    )


def decode_startup_request(
    envelope: ProtocolEnvelope,
) -> tuple[int, str, DatabaseInstance | None, int, int, int]:
    """Validate and decode one generation-zero startup request."""

    if envelope.operation != "startup" or envelope.generation != 0:
        raise TrustedLiveProtocolValidationError(
            "The startup request must use operation 'startup' and generation zero."
        )
    payload = envelope.payload
    if set(payload) != {
        "first_generation",
        "database_path",
        "expected_database_instance",
        "busy_timeout_ms",
        "operation_timeout_ms",
        "orphan_grace_ms",
    }:
        raise TrustedLiveProtocolValidationError(
            "The startup request has invalid fields."
        )
    first_generation = validate_generation(
        payload["first_generation"],
        allow_zero=False,
    )
    database_path = _validate_startup_database_path(payload["database_path"])
    expected_instance = decode_database_instance(payload["expected_database_instance"])
    busy_timeout_ms = _validate_startup_integer(
        payload["busy_timeout_ms"],
        minimum=0,
        maximum=_MAX_BUSY_TIMEOUT_MS,
        description="The startup busy timeout",
    )
    operation_timeout_ms = _validate_startup_integer(
        payload["operation_timeout_ms"],
        minimum=_MIN_STARTUP_TIMEOUT_MS,
        maximum=MAX_OPERATION_TIMEOUT_MS,
        description="The startup operation timeout",
    )
    orphan_grace_ms = _validate_startup_integer(
        payload["orphan_grace_ms"],
        minimum=_MIN_ORPHAN_GRACE_MS,
        maximum=MAX_OPERATION_TIMEOUT_MS,
        description="The startup orphan grace period",
    )
    return (
        first_generation,
        database_path,
        expected_instance,
        busy_timeout_ms,
        operation_timeout_ms,
        orphan_grace_ms,
    )


def encode_source_identity(source: TrustedLiveSourceIdentity) -> dict[str, JsonValue]:
    if not isinstance(source, TrustedLiveSourceIdentity):
        raise TrustedLiveProtocolValidationError("The source identity is invalid.")
    if source.journal_mode not in {"wal", "rollback"}:
        raise TrustedLiveProtocolValidationError(
            "The source journal mode is unsupported."
        )
    budget = _new_role_budget(MAX_REPLY_BYTES)
    _preflight_database_instance(source.database_instance, budget)
    _preflight_identity(source.journal_identity, budget, "Journal identity")
    _preflight_identity(source.shm_identity, budget, "SHM identity")
    _preflight_identity(source.wal_identity, budget, "WAL identity")
    return {
        "database_instance": encode_database_instance(source.database_instance),
        "journal_identity": _encode_identity_prevalidated(source.journal_identity),
        "journal_mode": source.journal_mode,
        "shm_identity": _encode_identity_prevalidated(source.shm_identity),
        "wal_identity": _encode_identity_prevalidated(source.wal_identity),
    }


def decode_source_identity(encoded: object) -> TrustedLiveSourceIdentity:
    if not isinstance(encoded, dict) or set(encoded) != {
        "database_instance",
        "wal_identity",
        "shm_identity",
        "journal_identity",
        "journal_mode",
    }:
        raise TrustedLiveProtocolValidationError(
            "The trusted source identity has invalid fields."
        )
    instance = decode_database_instance(encoded["database_instance"])
    if instance is None or encoded["journal_mode"] not in {"wal", "rollback"}:
        raise TrustedLiveProtocolValidationError(
            "The trusted source identity is incomplete."
        )
    return TrustedLiveSourceIdentity(
        database_instance=instance,
        wal_identity=_decode_identity(encoded["wal_identity"]),
        shm_identity=_decode_identity(encoded["shm_identity"]),
        journal_identity=_decode_identity(encoded["journal_identity"]),
        journal_mode=encoded["journal_mode"],
    )


def _preflight_query_results(
    materialised: tuple[TrustedQueryResult, ...],
) -> None:
    try:
        preflight_trusted_query_results(materialised)
    except TrustedLiveReaderError as error:
        raise TrustedLiveProtocolValidationError(str(error)) from error


def encode_query_results(results: Sequence[TrustedQueryResult]) -> list[JsonValue]:
    if not isinstance(results, Sequence) or isinstance(
        results, (str, bytes, bytearray, memoryview)
    ):
        raise TrustedLiveProtocolValidationError(
            "Query results must be a bounded sequence."
        )
    result_count = len(results)
    if not 1 <= result_count <= MAX_BATCH_QUERIES:
        raise TrustedLiveProtocolValidationError(
            "The helper returned an empty or oversized result batch."
        )
    materialised = tuple(results[index] for index in range(result_count))
    _preflight_query_results(materialised)
    encoded_results: list[JsonValue] = []
    for result in materialised:
        columns: list[JsonValue] = list(result.columns)
        rows: list[JsonValue] = []
        for row in result.rows:
            rows.append([_encode_scalar(value) for value in row])
        encoded_results.append({"columns": columns, "rows": rows})
    return encoded_results


def decode_query_results(encoded: object) -> tuple[TrustedQueryResult, ...]:
    if not isinstance(encoded, list) or not encoded or len(encoded) > MAX_BATCH_QUERIES:
        raise TrustedLiveProtocolValidationError(
            "The result batch is empty, malformed, or oversized."
        )
    results: list[TrustedQueryResult] = []
    total_cells = 0
    for item in encoded:
        if not isinstance(item, dict) or set(item) != {"columns", "rows"}:
            raise TrustedLiveProtocolValidationError("A result has invalid fields.")
        columns = item["columns"]
        rows = item["rows"]
        if (
            not isinstance(columns, list)
            or len(columns) > MAX_COLUMNS_PER_RESULT
            or not all(isinstance(column, str) for column in columns)
        ):
            raise TrustedLiveProtocolValidationError(
                "Result columns are malformed or oversized."
            )
        for column in columns:
            _utf8_size(column, "A result column name", MAX_BINDING_NAME_BYTES)
        if not isinstance(rows, list) or len(rows) > MAX_ROWS_PER_RESULT:
            raise TrustedLiveProtocolValidationError(
                "Result rows are malformed or oversized."
            )
        decoded_rows: list[tuple[object, ...]] = []
        for row in rows:
            if not isinstance(row, list) or len(row) != len(columns):
                raise TrustedLiveProtocolValidationError(
                    "A result row has the wrong shape."
                )
            total_cells += len(row)
            if total_cells > MAX_CELLS_PER_REPLY:
                raise TrustedLiveProtocolValidationError(
                    "The reply contains too many SQLite cells."
                )
            decoded_rows.append(tuple(_decode_scalar(value) for value in row))
        results.append(TrustedQueryResult(tuple(columns), tuple(decoded_rows)))
    return tuple(results)


def encode_success_reply(
    session: str,
    generation: int,
    operation: str,
    payload: Mapping[str, Any] | None = None,
) -> bytes:
    body: dict[str, Any] = {"status": "ok"}
    if payload:
        body.update(payload)
    return encode_frame(
        session,
        generation,
        operation,
        body,
        maximum_bytes=MAX_REPLY_BYTES,
    )


def encode_error_reply(
    session: str,
    generation: int,
    operation: str,
    code: str,
    message: str,
) -> bytes:
    if code not in _ERROR_CODES:
        raise TrustedLiveProtocolValidationError("The helper error code is unknown.")
    if not isinstance(message, str):
        message = "The trusted helper failed without a valid error message."
    encoded_message = message.encode("utf-8", errors="replace")
    if len(encoded_message) > MAX_ERROR_MESSAGE_BYTES:
        encoded_message = encoded_message[:MAX_ERROR_MESSAGE_BYTES]
        while True:
            try:
                message = encoded_message.decode("utf-8")
                break
            except UnicodeDecodeError as error:
                encoded_message = encoded_message[: error.start]
        message += "…"
    return encode_frame(
        session,
        generation,
        operation,
        {"status": "error", "code": code, "message": message},
        maximum_bytes=MAX_REPLY_BYTES,
    )


def decode_reply_payload(envelope: ProtocolEnvelope) -> tuple[str, dict[str, Any]]:
    payload = envelope.payload
    status = payload.get("status")
    if status == "error":
        if set(payload) != {"status", "code", "message"}:
            raise TrustedLiveProtocolValidationError(
                "An error reply has invalid fields."
            )
        code = payload["code"]
        message = payload["message"]
        if code not in _ERROR_CODES or not isinstance(message, str):
            raise TrustedLiveProtocolValidationError(
                "An error reply has an unknown code or invalid message."
            )
        _utf8_size(message, "A helper error message", MAX_ERROR_MESSAGE_BYTES + 4)
        return "error", {"code": code, "message": message}
    if status != "ok":
        raise TrustedLiveProtocolValidationError(
            "A helper reply has an invalid status."
        )
    return "ok", payload


def validate_startup_success(payload: Mapping[str, Any]) -> TrustedLiveSourceIdentity:
    if set(payload) != {"status", "source"}:
        raise TrustedLiveProtocolValidationError(
            "A startup success reply has invalid fields."
        )
    return decode_source_identity(payload["source"])


def validate_shutdown_success(payload: Mapping[str, Any]) -> None:
    if set(payload) != {"status"}:
        raise TrustedLiveProtocolValidationError(
            "A shutdown success reply has invalid fields."
        )


def validate_job_success(
    operation: str,
    payload: Mapping[str, Any],
) -> TrustedQueryResult | tuple[TrustedQueryResult, ...] | int:
    if operation in {"query", "query_batch"}:
        if set(payload) != {"status", "results"}:
            raise TrustedLiveProtocolValidationError(
                "A query success reply has invalid fields."
            )
        results = decode_query_results(payload["results"])
        if operation == "query":
            if len(results) != 1:
                raise TrustedLiveProtocolValidationError(
                    "A query reply must contain exactly one result."
                )
            return results[0]
        return results
    if operation == "data_version":
        if set(payload) != {"status", "value"}:
            raise TrustedLiveProtocolValidationError(
                "A data-version success reply has invalid fields."
            )
        value = payload["value"]
        if type(value) is not int or value < 0 or value > _INTEGER_MAX:
            raise TrustedLiveProtocolValidationError(
                "The data-version reply is invalid."
            )
        return value
    raise TrustedLiveProtocolValidationError(
        "A success reply used an invalid database operation."
    )


def error_code_is_terminal(code: str) -> bool:
    return code not in {
        "sql_rejected",
        "query_failed",
        "result_limit",
        "busy_timeout",
        "cancelled",
    }
