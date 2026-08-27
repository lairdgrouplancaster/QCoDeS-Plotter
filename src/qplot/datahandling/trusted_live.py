"""Physically read-only access to a trusted live QCoDeS database.

This module is the finite Stage 2 backend boundary.  It is intentionally not
used by application workers yet: the existing private-snapshot reader remains
qPlot's default until the isolated helper-process and scheduling stages.
"""

from __future__ import annotations

import importlib
import json
import math
import os
import secrets
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType, TracebackType
from typing import Any, TypeAlias
from urllib.parse import quote

import apsw
import apsw.ext

from qplot.datahandling.file_identity import (
    DatabaseFileIdentity,
    DatabaseInstance,
    FileIdentityHandleCloseError,
    canonical_database_path,
    checked_path_bound_file_identity,
    database_instances_differ,
    logical_database_path,
)

TRUSTED_READER_APSW_VERSION = "3.53.4.0"
TRUSTED_READER_SQLITE_VERSION = "3.53.4"
TRUSTED_READER_SQLITE_SOURCE_ID = (
    "2026-07-24 19:02:57 "
    "bf7c7f30031888f4e796e429ab3978879485813aaca6f641c7b33e4e09459bcc"
)
TRUSTED_READER_VFS_NAME = "qplot-trusted-live-v2"
_NATIVE_ENTRYPOINT = "sqlite3_qplot_trusted_vfs_init"
_SQLITE_HEADER_READ_VERSION_OFFSET = 18
_SQLITE_HEADER_WRITE_VERSION_OFFSET = 19
_SQLITE_HEADER_PREFIX_BYTES = 100
_SQLITE_ROLLBACK_FORMAT = 1
_SQLITE_WAL_FORMAT = 2
DEFAULT_TRUSTED_OPERATION_TIMEOUT_SECONDS = 5.0
TRUSTED_LIVE_MAX_REPLY_BYTES = 32 * 1024 * 1024
TRUSTED_LIVE_MAX_BATCH_QUERIES = 128
TRUSTED_LIVE_MAX_COLUMNS_PER_RESULT = 4_096
TRUSTED_LIVE_MAX_ROWS_PER_RESULT = 250_000
TRUSTED_LIVE_MAX_CELLS_PER_REPLY = 1_000_000
TRUSTED_LIVE_MAX_SCALAR_BYTES = 4 * 1024 * 1024
TRUSTED_LIVE_MAX_COLUMN_NAME_BYTES = 1_024
TRUSTED_LIVE_MAX_TRANSIENT_RAW_ROW_BYTES = 8 * 1024 * 1024
# APSW returns ordinary SQLite values in a Python tuple.  Four is the maximum
# UTF-8-to-PEP-393 payload expansion.  The per-column allowance covers the
# scalar object/header/terminator, a tuple reference, and fixed-size SQLite
# integer/float/NULL objects on supported 64-bit CPython; the fixed allowance
# covers the tuple's logical object size.  This is a conservative logical
# Python-object/payload envelope, not a bound on allocator-reserved bytes,
# size-class rounding, process RSS, arenas, fragmentation, or SQLite VM memory.
# The statement limit also satisfies the independent raw-payload bound.
_TRUSTED_LIVE_PYTHON_TEXT_EXPANSION = 4
_TRUSTED_LIVE_PYTHON_FIXED_BYTES_PER_COLUMN = 512
_TRUSTED_LIVE_PYTHON_ROW_FIXED_BYTES = 4 * 1024
TRUSTED_LIVE_MAX_TRANSIENT_PYTHON_ROW_BYTES = 32 * 1024 * 1024
_BUSY_RETRY_QUANTUM_SECONDS = 0.01
_PROGRESS_HANDLER_STEPS = 1_000
_RESULT_FRAME_ENVELOPE_RESERVE_BYTES = 512
_RESULT_WIRE_CONTAINER_BUDGET_BYTES = 32
_RESULT_WIRE_FIELD_BUDGET_BYTES = 16
_SQLITE_INTEGER_MIN = -(1 << 63)
_SQLITE_INTEGER_MAX = (1 << 63) - 1
_PROCESS_SESSION_MUTEX = threading.Lock()
_PROCESS_SESSION_OWNER: object | None = None
_PROCESS_SESSION_QUARANTINE_REASON: str | None = None
SqliteBinding: TypeAlias = Any
SqliteBindings: TypeAlias = Sequence[SqliteBinding] | Mapping[str, SqliteBinding] | None


class TrustedLiveReaderError(RuntimeError):
    """Base error for the trusted live-reader backend."""


class TrustedLiveReaderUnavailableError(TrustedLiveReaderError):
    """Raised when the pinned native reader boundary is unavailable."""


class TrustedLiveSourceError(TrustedLiveReaderError):
    """Raised when the selected source is not a safe direct-read target."""


class TrustedLiveUnsupportedSourceError(TrustedLiveSourceError):
    """Raised for an unsupported platform, path, filesystem, or file type."""


class TrustedLiveSourceChangedError(TrustedLiveSourceError):
    """Raised only when checked OS identities prove a source change."""


class TrustedLiveSourceIOError(TrustedLiveSourceError):
    """Raised for source I/O failures not proven to be identity changes."""


class TrustedLiveSqlRejectedError(TrustedLiveReaderError):
    """Raised when SQL is outside the backend's read-only query surface."""


class TrustedLiveQueryError(TrustedLiveReaderError):
    """Raised when an allowed query cannot be prepared or executed."""


class TrustedLiveResultLimitError(TrustedLiveReaderError):
    """Raised before a query result can exceed its materialisation budget."""


class TrustedLiveBusyTimeoutError(TrustedLiveReaderError):
    """Raised when the reader's bounded SQLite busy budget expires."""


class TrustedLiveCancelledError(InterruptedError, TrustedLiveReaderError):
    """Raised when an external event or :meth:`interrupt` cancels an operation."""


class TrustedLiveDeadlineExceededError(TimeoutError, TrustedLiveReaderError):
    """Raised when a finite reader operation reaches its monotonic deadline."""


class TrustedLiveInvalidDatabaseError(TrustedLiveReaderError):
    """Raised for a corrupt, malformed, or non-SQLite database."""


class TrustedLiveCleanupError(TrustedLiveReaderError):
    """Raised when rollback or handle release cannot be verified cleanly."""


class TrustedLiveReaderClosedError(TrustedLiveReaderError):
    """Raised when an operation targets a closed reader."""


class TrustedLiveReaderThreadError(TrustedLiveReaderError):
    """Raised when a reader is used from a thread other than its owner."""


class TrustedLiveTransactionError(TrustedLiveReaderError):
    """Raised when an operation is re-entered on the same reader."""


class _PreflightFileDescriptorCloseError(OSError):
    """An internal signal that preflight descriptor cleanup is uncertain."""


@dataclass(frozen=True, slots=True)
class TrustedLiveSourceIdentity:
    """Main binding plus source sidecars observed before the native open."""

    database_instance: DatabaseInstance
    wal_identity: DatabaseFileIdentity | None
    shm_identity: DatabaseFileIdentity | None
    journal_identity: DatabaseFileIdentity | None
    journal_mode: str


@dataclass(frozen=True, slots=True)
class TrustedLiveReaderToken:
    """Opaque incarnation token used to reject stale Stage 2 callbacks."""

    source: TrustedLiveSourceIdentity
    nonce: str


@dataclass(frozen=True, slots=True)
class TrustedQuery:
    """One immutable statement specification for a finite reader operation.

    Sequence and mapping bindings are shallow-copied when the specification is
    made.  A caller therefore cannot replace bindings while a batch transaction
    is running.
    """

    sql: str
    bindings: SqliteBindings = None

    def __post_init__(self) -> None:
        bindings = self.bindings
        if isinstance(bindings, Mapping):
            frozen_bindings: SqliteBindings = MappingProxyType(dict(bindings))
        elif bindings is None:
            frozen_bindings = None
        elif isinstance(bindings, bytearray):
            frozen_bindings = (bytes(bindings),)
        elif isinstance(bindings, (str, bytes)):
            frozen_bindings = (bindings,)
        else:
            frozen_bindings = tuple(bindings)
        object.__setattr__(self, "bindings", frozen_bindings)


@dataclass(frozen=True, slots=True)
class TrustedQueryResult:
    """One fully materialised result with no cursor or connection escape."""

    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]


@dataclass(frozen=True, slots=True)
class TrustedVfsAudit:
    """Native VFS counters attributed only to this reader incarnation."""

    counters: Mapping[str, int]

    def __getitem__(self, name: str) -> int:
        return self.counters[name]


@dataclass(frozen=True, slots=True)
class _TrustedNativeStatus:
    sequence: int
    kind: str
    artifact: str
    operation: str
    sqlite_code: int


@dataclass(slots=True)
class _TrustedOperationControl:
    generation: int
    started_at: float
    deadline: float
    busy_deadline: float
    cancel_event: threading.Event | None
    native_sequence: int = 0
    abort_reason: str | None = None
    policy_denied: bool = False
    operation_result_length_limit: int | None = None
    result_length_limit: int | None = None


@dataclass(frozen=True, slots=True)
class _TrustedSqliteLengthLimit:
    """One temporary, verified SQLite length-limit installation."""

    previous: int
    effective: int


def _trusted_statement_length_limit(
    operation_baseline: int,
    column_count: int,
) -> int:
    """Return the per-value cap for the raw and logical Python-row envelopes."""

    width = max(1, column_count)
    raw_payload_limit = TRUSTED_LIVE_MAX_TRANSIENT_RAW_ROW_BYTES // width
    python_payload_room = (
        TRUSTED_LIVE_MAX_TRANSIENT_PYTHON_ROW_BYTES
        - _TRUSTED_LIVE_PYTHON_ROW_FIXED_BYTES
        - (_TRUSTED_LIVE_PYTHON_FIXED_BYTES_PER_COLUMN * width)
    )
    if python_payload_room <= 0:
        raise TrustedLiveResultLimitError(
            "The trusted query result is too wide for the logical Python-row "
            "materialisation budget."
        )
    python_payload_limit = python_payload_room // (
        _TRUSTED_LIVE_PYTHON_TEXT_EXPANSION * width
    )
    effective = min(
        operation_baseline,
        TRUSTED_LIVE_MAX_SCALAR_BYTES,
        raw_payload_limit,
        python_payload_limit,
    )
    if effective < 1:
        raise TrustedLiveResultLimitError(
            "The trusted query result is too wide for a positive SQLite "
            "result-length limit."
        )
    return effective


@dataclass(slots=True)
class _TrustedResultBudget:
    """Incrementally bound one batch before retaining each SQLite row."""

    maximum_wire_bytes: int
    used_wire_bytes: int
    total_cells: int = 0
    abort_check: Callable[[], None] | None = None

    @classmethod
    def for_query_batch(cls) -> _TrustedResultBudget:
        budget = cls(
            maximum_wire_bytes=TRUSTED_LIVE_MAX_REPLY_BYTES,
            used_wire_bytes=0,
        )
        budget.consume(
            _RESULT_FRAME_ENVELOPE_RESERVE_BYTES,
            "The protocol envelope",
        )
        budget.consume(
            _RESULT_WIRE_CONTAINER_BUDGET_BYTES,
            "Query results",
        )
        return budget

    def consume(self, amount: int, description: str) -> None:
        if amount < 0 or amount > self.maximum_wire_bytes - self.used_wire_bytes:
            raise TrustedLiveResultLimitError(
                f"{description} exceeds the aggregate "
                f"{self.maximum_wire_bytes}-byte result wire budget."
            )
        self.used_wire_bytes += amount

    def start_result(self, description: Sequence[Sequence[Any]]) -> tuple[str, ...]:
        if len(description) > TRUSTED_LIVE_MAX_COLUMNS_PER_RESULT:
            raise TrustedLiveResultLimitError(
                "A trusted query result has more than "
                f"{TRUSTED_LIVE_MAX_COLUMNS_PER_RESULT} columns."
            )
        self.consume(_RESULT_WIRE_CONTAINER_BUDGET_BYTES, "Query results")
        columns: list[str] = []
        for column_description in description:
            if self.abort_check is not None:
                self.abort_check()
            if not column_description or not isinstance(column_description[0], str):
                raise TrustedLiveQueryError(
                    "SQLite returned a result column without a valid text name."
                )
            column = column_description[0]
            _utf8_bytes, json_bytes = _trusted_text_sizes(
                column,
                description="A trusted query result column name",
                maximum_utf8_bytes=TRUSTED_LIVE_MAX_COLUMN_NAME_BYTES,
                abort_check=self.abort_check,
            )
            self.consume(
                _RESULT_WIRE_FIELD_BUDGET_BYTES + json_bytes,
                "Query result columns",
            )
            columns.append(column)
        return tuple(columns)

    def retain_row(
        self,
        row: Sequence[Any],
        *,
        column_count: int,
        retained_row_count: int,
    ) -> tuple[Any, ...]:
        if retained_row_count >= TRUSTED_LIVE_MAX_ROWS_PER_RESULT:
            raise TrustedLiveResultLimitError(
                "A trusted query result has more than "
                f"{TRUSTED_LIVE_MAX_ROWS_PER_RESULT} rows."
            )
        if len(row) != column_count:
            raise TrustedLiveQueryError(
                "SQLite returned a result row with the wrong number of columns."
            )
        if column_count > TRUSTED_LIVE_MAX_CELLS_PER_REPLY - self.total_cells:
            raise TrustedLiveResultLimitError(
                "A trusted query batch has more than "
                f"{TRUSTED_LIVE_MAX_CELLS_PER_REPLY} result cells."
            )

        self.consume(
            _RESULT_WIRE_CONTAINER_BUDGET_BYTES
            + (_RESULT_WIRE_FIELD_BUDGET_BYTES * column_count),
            "Query result rows",
        )
        retained_values: list[Any] = []
        for value in row:
            if self.abort_check is not None:
                self.abort_check()
            scalar, wire_bytes = _trusted_result_scalar(
                value,
                abort_check=self.abort_check,
            )
            self.consume(wire_bytes, "Query result values")
            retained_values.append(scalar)
        self.total_cells += column_count
        return tuple(retained_values)


def _trusted_text_sizes(
    value: str,
    *,
    description: str,
    maximum_utf8_bytes: int,
    abort_check: Callable[[], None] | None = None,
) -> tuple[int, int]:
    """Size UTF-8 and canonical JSON text without allocating either form."""

    utf8_bytes = 0
    json_bytes = 2  # Opening and closing JSON quotes.
    for character_index, character in enumerate(value):
        if character_index % 4_096 == 0 and abort_check is not None:
            abort_check()
        codepoint = ord(character)
        if 0xD800 <= codepoint <= 0xDFFF:
            raise TrustedLiveQueryError(
                f"{description} contains text that is not valid Unicode."
            )
        if codepoint <= 0x7F:
            character_bytes = 1
        elif codepoint <= 0x7FF:
            character_bytes = 2
        elif codepoint <= 0xFFFF:
            character_bytes = 3
        else:
            character_bytes = 4
        utf8_bytes += character_bytes
        if utf8_bytes > maximum_utf8_bytes:
            raise TrustedLiveResultLimitError(
                f"{description} exceeds the {maximum_utf8_bytes}-byte limit."
            )
        if character in {'"', "\\"}:
            json_bytes += 2
        elif codepoint <= 0x1F:
            json_bytes += 2 if character in "\b\t\n\f\r" else 6
        else:
            json_bytes += character_bytes
    return utf8_bytes, json_bytes


def _trusted_result_scalar(
    value: Any,
    *,
    abort_check: Callable[[], None] | None = None,
) -> tuple[Any, int]:
    """Validate, normalise, and conservatively size one live SQLite scalar."""

    if value is None:
        return None, 8
    if isinstance(value, bool):
        return value, 16
    if isinstance(value, int):
        if value < _SQLITE_INTEGER_MIN or value > _SQLITE_INTEGER_MAX:
            raise TrustedLiveQueryError(
                "SQLite returned an integer outside its signed 64-bit range."
            )
        return value, 16 + len(str(value))
    if isinstance(value, float):
        # SQLite can deliberately return infinities.  They are represented by
        # a canonical tagged hexadecimal string at the IPC layer rather than
        # as a non-finite JSON number.
        return value, 16 + len(value.hex())
    if isinstance(value, str):
        _utf8_bytes, json_bytes = _trusted_text_sizes(
            value,
            description="A SQLite text result",
            maximum_utf8_bytes=TRUSTED_LIVE_MAX_SCALAR_BYTES,
            abort_check=abort_check,
        )
        return value, 16 + json_bytes
    if isinstance(value, memoryview):
        blob_size = value.nbytes
    elif isinstance(value, (bytes, bytearray)):
        blob_size = len(value)
    else:
        raise TrustedLiveQueryError(
            "SQLite returned a value outside its null, integer, real, text, "
            "and blob scalar types."
        )
    if blob_size > TRUSTED_LIVE_MAX_SCALAR_BYTES:
        raise TrustedLiveResultLimitError(
            "A SQLite blob result exceeds the "
            f"{TRUSTED_LIVE_MAX_SCALAR_BYTES}-byte limit."
        )
    blob = value if isinstance(value, bytes) else bytes(value)
    base64_bytes = 4 * ((blob_size + 2) // 3)
    return blob, 16 + base64_bytes


def preflight_trusted_query_results(
    results: Sequence[TrustedQueryResult],
) -> None:
    """Apply the live reader's exact aggregate result budget to existing rows.

    The helper protocol calls this as a defensive second check.  Normal live
    queries use the same budget incrementally, before each row is retained.
    """

    if isinstance(results, (str, bytes, bytearray, memoryview)):
        raise TrustedLiveQueryError("Trusted query results must be a sequence.")
    result_count = len(results)
    if not 1 <= result_count <= TRUSTED_LIVE_MAX_BATCH_QUERIES:
        raise TrustedLiveResultLimitError(
            "A trusted query batch must contain between 1 and "
            f"{TRUSTED_LIVE_MAX_BATCH_QUERIES} results."
        )
    budget = _TrustedResultBudget.for_query_batch()
    for index in range(result_count):
        result = results[index]
        if not isinstance(result, TrustedQueryResult):
            raise TrustedLiveQueryError(
                "A trusted query batch contains a non-result object."
            )
        description = tuple((column,) for column in result.columns)
        columns = budget.start_result(description)
        if columns != result.columns:
            raise TrustedLiveQueryError(
                "A trusted query result has invalid column metadata."
            )
        for row_index, row in enumerate(result.rows):
            budget.retain_row(
                row,
                column_count=len(columns),
                retained_row_count=row_index,
            )


def _claim_process_session(owner: object) -> None:
    global _PROCESS_SESSION_OWNER
    with _PROCESS_SESSION_MUTEX:
        if _PROCESS_SESSION_QUARANTINE_REASON is not None:
            raise TrustedLiveReaderUnavailableError(
                "A prior trusted-reader cleanup failure quarantined this process. "
                "Terminate the process before opening another trusted reader. "
                "Recorded cleanup uncertainty: "
                f"{_PROCESS_SESSION_QUARANTINE_REASON}"
            )
        if _PROCESS_SESSION_OWNER is not None:
            raise TrustedLiveReaderUnavailableError(
                "Close the existing trusted reader in this process before "
                "opening another one."
            )
        _PROCESS_SESSION_OWNER = owner


def _quarantine_process_session(owner: object, error: BaseException) -> None:
    global _PROCESS_SESSION_QUARANTINE_REASON
    with _PROCESS_SESSION_MUTEX:
        if _PROCESS_SESSION_OWNER is owner:
            _PROCESS_SESSION_QUARANTINE_REASON = f"{type(error).__name__}: {error}"


def _release_process_session(owner: object) -> None:
    global _PROCESS_SESSION_OWNER, _PROCESS_SESSION_QUARANTINE_REASON
    with _PROCESS_SESSION_MUTEX:
        # Identity ownership makes constructor/close cleanup idempotent and
        # prevents one interrupted reader from clearing another reader's claim.
        if _PROCESS_SESSION_OWNER is owner:
            _PROCESS_SESSION_OWNER = None
            _PROCESS_SESSION_QUARANTINE_REASON = None


def _native_extension_path() -> Path:
    try:
        module = importlib.import_module("qplot.datahandling._trusted_vfs_native")
    except ImportError as error:
        raise TrustedLiveReaderUnavailableError(
            "qPlot's trusted live-reader native VFS is not installed."
        ) from error
    module_file = getattr(module, "__file__", None)
    if not module_file:
        raise TrustedLiveReaderUnavailableError(
            "qPlot could not locate its trusted live-reader native VFS."
        )
    return Path(module_file).resolve()


def _check_pinned_sqlite_runtime() -> None:
    apsw_version = apsw.apsw_version()
    sqlite_version = apsw.sqlite_lib_version()
    sqlite_source_id = apsw.sqlite3_sourceid()
    if (
        apsw_version != TRUSTED_READER_APSW_VERSION
        or sqlite_version != TRUSTED_READER_SQLITE_VERSION
        or sqlite_source_id != TRUSTED_READER_SQLITE_SOURCE_ID
    ):
        raise TrustedLiveReaderUnavailableError(
            "The trusted live reader requires APSW "
            f"{TRUSTED_READER_APSW_VERSION} with SQLite "
            f"{TRUSTED_READER_SQLITE_VERSION}; found APSW {apsw_version} "
            f"with SQLite {sqlite_version} ({sqlite_source_id}). qPlot did "
            "not open the source."
        )
    get_effective_user_id = getattr(os, "geteuid", None)
    if get_effective_user_id is not None and get_effective_user_id() == 0:
        raise TrustedLiveReaderUnavailableError(
            "The trusted live reader refuses to run as the POSIX root user. "
            "SQLite's Unix VFS may issue fchown() while opening a WAL "
            "shared-memory file as root."
        )


def _reject_symlink_path_components(path: Path) -> None:
    """Reject a selected path containing any currently visible symlink.

    This records only the observed trusted hierarchy at preflight.  The native
    VFS separately compares expected identities with SQLite's actual handles;
    neither check claims to defend a hostile parent namespace.
    """

    absolute_path = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute_path.anchor)
    components = (
        absolute_path.parts[1:] if absolute_path.anchor else absolute_path.parts
    )
    for index, component in enumerate(components):
        current /= component
        try:
            status = current.lstat()
        except FileNotFoundError:
            if index == len(components) - 1:
                return
            raise TrustedLiveUnsupportedSourceError(
                f"A parent of the selected trusted database does not exist: {current}"
            ) from None
        except OSError as error:
            raise TrustedLiveSourceIOError(
                f"Could not inspect a selected database path component "
                f"{current}: {error}"
            ) from error
        file_attributes = int(getattr(status, "st_file_attributes", 0))
        reparse_point = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        if stat.S_ISLNK(status.st_mode) or (
            os.name == "nt" and file_attributes & reparse_point
        ):
            raise TrustedLiveUnsupportedSourceError(
                "Trusted live reading does not accept symbolic-link or "
                f"reparse-point path components: {current}"
            )
        if index != len(components) - 1 and not stat.S_ISDIR(status.st_mode):
            raise TrustedLiveUnsupportedSourceError(
                f"A parent of the selected trusted database is not a directory: {current}"
            )


def _reject_windows_alternate_data_stream(path: Path) -> None:
    """Reject NTFS stream syntax before opening any source-family handle."""

    if os.name != "nt":
        return
    _drive, tail = os.path.splitdrive(os.fspath(path))
    if ":" in tail:
        raise TrustedLiveUnsupportedSourceError(
            "Trusted live reading does not accept Windows alternate data "
            f"stream paths: {path}"
        )


def _require_regular_file(path: Path, description: str) -> os.stat_result:
    try:
        status = path.lstat()
    except FileNotFoundError:
        raise TrustedLiveUnsupportedSourceError(
            f"The trusted database {description} does not exist: {path}"
        ) from None
    except OSError as error:
        raise TrustedLiveSourceIOError(
            f"Could not inspect the trusted database {description} {path}: {error}"
        ) from error
    file_attributes = int(getattr(status, "st_file_attributes", 0))
    reparse_point = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    if stat.S_ISLNK(status.st_mode) or (
        os.name == "nt" and file_attributes & reparse_point
    ):
        raise TrustedLiveUnsupportedSourceError(
            f"The trusted database {description} must not be a symbolic link "
            f"or reparse point: {path}"
        )
    if not stat.S_ISREG(status.st_mode):
        raise TrustedLiveUnsupportedSourceError(
            f"The trusted database {description} is not a regular file: {path}"
        )
    return status


def _required_file_identity(
    path: Path,
    description: str,
) -> DatabaseFileIdentity:
    _require_regular_file(path, description)
    try:
        identity = checked_path_bound_file_identity(path)
    except FileIdentityHandleCloseError:
        # The constructor handles this separately: an uncertain Windows HANDLE
        # close must quarantine the process session, not look like ordinary I/O.
        raise
    except OSError as error:
        raise TrustedLiveSourceIOError(
            f"Could not identify the trusted database {description} {path}: {error}"
        ) from error
    if identity is None:
        raise TrustedLiveUnsupportedSourceError(
            f"Could not establish a stable identity for the trusted database "
            f"{description} {path}."
        )
    return identity


def _optional_file_identity(
    path: Path,
    description: str,
) -> DatabaseFileIdentity | None:
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise TrustedLiveSourceIOError(
            f"Could not inspect the trusted database {description} {path}: {error}"
        ) from error
    return _required_file_identity(path, description)


def _capture_database_instance_for_trusted_open(
    database_path: str | os.PathLike[str],
) -> tuple[DatabaseInstance, DatabaseFileIdentity]:
    """Capture UI-comparable and native identities without unchecked handles."""

    selected_path = Path(os.path.abspath(os.fspath(database_path)))
    _reject_windows_alternate_data_stream(selected_path)
    _reject_symlink_path_components(selected_path)
    logical_path = logical_database_path(database_path)
    resolved_path = canonical_database_path(logical_path)
    status = _require_regular_file(Path(resolved_path), "main file")
    native_identity = _required_file_identity(Path(resolved_path), "main file")

    inode = int(getattr(status, "st_ino", 0) or 0)
    if inode:
        comparable_identity: DatabaseFileIdentity = (
            int(getattr(status, "st_dev", 0) or 0),
            inode,
        )
    else:
        # On Windows the ordinary UI identity helper falls back to the same
        # volume/file-index identity.  Using the checked observation here keeps
        # expected-instance comparisons compatible without an unchecked HANDLE.
        comparable_identity = native_identity
    return (
        DatabaseInstance(
            logical_path=logical_path,
            resolved_path=resolved_path,
            identity=comparable_identity,
        ),
        native_identity,
    )


def _database_header_journal_mode(database_path: Path) -> str:
    open_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    open_flags |= getattr(os, "O_CLOEXEC", 0)
    open_flags |= getattr(os, "O_NOINHERIT", 0)
    open_flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        file_descriptor = os.open(database_path, open_flags)
        try:
            header_parts: list[bytes] = []
            remaining = _SQLITE_HEADER_PREFIX_BYTES
            while remaining:
                part = os.read(file_descriptor, remaining)
                if not part:
                    break
                header_parts.append(part)
                remaining -= len(part)
            header = b"".join(header_parts)
        finally:
            _close_preflight_file_descriptor(file_descriptor)
    except _PreflightFileDescriptorCloseError:
        raise
    except OSError as error:
        raise TrustedLiveSourceIOError(
            f"Could not read the SQLite header of {database_path}: {error}"
        ) from error
    if len(header) < _SQLITE_HEADER_PREFIX_BYTES or not header.startswith(
        b"SQLite format 3\x00"
    ):
        raise TrustedLiveInvalidDatabaseError(
            f"The selected file is not a complete SQLite 3 database: {database_path}"
        )
    write_version = header[_SQLITE_HEADER_WRITE_VERSION_OFFSET]
    read_version = header[_SQLITE_HEADER_READ_VERSION_OFFSET]
    if (write_version, read_version) == (
        _SQLITE_WAL_FORMAT,
        _SQLITE_WAL_FORMAT,
    ):
        return "wal"
    if (write_version, read_version) == (
        _SQLITE_ROLLBACK_FORMAT,
        _SQLITE_ROLLBACK_FORMAT,
    ):
        return "rollback"
    raise TrustedLiveUnsupportedSourceError(
        "The selected database uses unsupported SQLite header read/write "
        f"versions {read_version}/{write_version}."
    )


def _close_preflight_file_descriptor(file_descriptor: int) -> None:
    """Close one preflight fd exactly once and surface uncertain cleanup."""

    try:
        os.close(file_descriptor)
    except OSError as error:
        # In particular, never retry close(2) after EINTR: supported kernels
        # may already have consumed and reassigned the descriptor number.
        raise _PreflightFileDescriptorCloseError(*error.args) from error


def _capture_source_identity(
    database_path: str | os.PathLike[str],
) -> tuple[TrustedLiveSourceIdentity, DatabaseFileIdentity]:
    instance, main_identity = _capture_database_instance_for_trusted_open(database_path)
    resolved = Path(instance.resolved_path)
    # This is the sole source-content read before SQLite opens the database.
    # All validation after xOpen goes through the token-gated native proof.
    journal_mode = _database_header_journal_mode(resolved)
    wal_path = Path(f"{resolved}-wal")
    shm_path = Path(f"{resolved}-shm")
    journal_path = Path(f"{resolved}-journal")
    wal_identity = _optional_file_identity(wal_path, "WAL file")
    shm_identity = _optional_file_identity(shm_path, "SHM file")
    journal_identity = _optional_file_identity(journal_path, "rollback journal")
    instance = DatabaseInstance(
        logical_path=instance.logical_path,
        resolved_path=instance.resolved_path,
        identity=instance.identity,
        sidecar_identities=frozenset(
            identity
            for identity in (wal_identity, shm_identity, journal_identity)
            if identity is not None
        ),
    )

    if journal_mode == "wal":
        if journal_identity is not None:
            raise TrustedLiveUnsupportedSourceError(
                "The trusted WAL database also has a rollback journal; the "
                "ambiguous source family was rejected."
            )
    else:
        if any(
            identity is not None
            for identity in (wal_identity, shm_identity, journal_identity)
        ):
            raise TrustedLiveUnsupportedSourceError(
                "Direct rollback-mode reads require a sidecar-free database. "
                "Use qPlot's existing private-snapshot path for retained or "
                "hot journals."
            )

    return (
        TrustedLiveSourceIdentity(
            database_instance=instance,
            wal_identity=wal_identity,
            shm_identity=shm_identity,
            journal_identity=journal_identity,
            journal_mode=journal_mode,
        ),
        main_identity,
    )


def _native_expected_identity_parameters(
    parameter_prefix: str,
    identity: DatabaseFileIdentity | None,
    *,
    allow_absent: bool,
) -> str:
    """Encode one expected source identity for the pinned native VFS."""

    if identity is None:
        if not allow_absent:
            raise TrustedLiveUnsupportedSourceError(
                "The selected database has no stable main-file identity."
            )
        return f"{parameter_prefix}_kind=absent"

    if len(identity) == 2 and type(identity[0]) is int and type(identity[1]) is int:
        identity_kind = "posix"
        identity_a, identity_b = identity
    elif (
        len(identity) == 3
        and identity[0] == "windows-file-id"
        and type(identity[1]) is int
        and type(identity[2]) is int
    ):
        identity_kind = "windows"
        identity_a, identity_b = identity[1:]
    else:
        raise TrustedLiveUnsupportedSourceError(
            "The selected database filesystem does not expose an identity "
            "supported by qPlot's native trusted reader."
        )
    if identity_a < 0 or identity_b <= 0:
        raise TrustedLiveUnsupportedSourceError(
            "The selected database returned an invalid operating-system identity."
        )
    return (
        f"{parameter_prefix}_kind={identity_kind}"
        f"&{parameter_prefix}_a={identity_a:x}"
        f"&{parameter_prefix}_b={identity_b:x}"
    )


def _sqlite_uri_path(path: str) -> str:
    # Preserve separators and the Windows drive colon; quote URI delimiters,
    # whitespace, and non-ASCII bytes so native URI parameters stay distinct.
    return quote(path, safe="/:\\")


class TrustedLiveReader:
    """One owner-thread-bound connection offering only finite read jobs.

    ``query`` and ``query_batch`` own their complete transaction lifetime and
    always materialise results before returning.  Their default timeout is
    finite.  Callers may supply a shorter or longer timeout, an absolute
    monotonic deadline, and a :class:`threading.Event` cancellation signal.

    Python/SQLite progress and busy handlers provide cooperative in-process
    interruption.  :meth:`interrupt` additionally calls ``sqlite3_interrupt``
    and is safe from a different thread.  These mechanisms do not forcibly
    pre-empt arbitrary stalled Python or operating-system code; the later
    helper-process stage provides that hard failure boundary.
    """

    def __init__(
        self,
        database_path: str | os.PathLike[str],
        *,
        expected_database_instance: DatabaseInstance | None = None,
        busy_timeout_ms: int = 5_000,
        operation_timeout_seconds: float = (DEFAULT_TRUSTED_OPERATION_TIMEOUT_SECONDS),
        _test_race_artifact: str | None = None,
        _test_cleanup_fault: str | None = None,
        _test_statement_limit_fault: str | None = None,
        _test_pre_open_callback: (
            Callable[[TrustedLiveReaderToken, Path], None] | None
        ) = None,
    ) -> None:
        self._owner_thread = threading.get_ident()
        self._closed = False
        self._invalidated = False
        self._internal_transaction_control = False
        self._connection: apsw.Connection | None = None
        self._bootstrap: apsw.Connection | None = None
        self._final_audit: TrustedVfsAudit | None = None
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = None
        self._native_temporary_path: Path | None = None
        self._token_string = secrets.token_hex(16)
        self._process_session_owner = object()
        self._operation_mutex = threading.Lock()
        self._active_operation: _TrustedOperationControl | None = None
        self._next_operation_generation = 1
        self._native_extension_loaded = False
        self._native_session_attempted = False
        self._native_release_confirmed = False
        self._resource_cleanup_complete = False
        self._resource_cleanup_error: TrustedLiveCleanupError | None = None
        self._preflight_cleanup_error: OSError | None = None
        # Arm the private statement-limit fault only after construction's
        # bootstrap operations have completed.
        self._test_statement_limit_fault: str | None = None
        self._test_statement_limit_fault_consumed = False
        self._default_operation_timeout_seconds = self._validated_duration(
            operation_timeout_seconds,
            description="default trusted-reader operation timeout",
            allow_zero=False,
        )
        if (
            type(busy_timeout_ms) is not int
            or busy_timeout_ms < 0
            or busy_timeout_ms > 2_147_483_647
        ):
            raise ValueError(
                "busy_timeout_ms must be an integer from 0 through 2147483647."
            )
        self._busy_timeout_seconds = busy_timeout_ms / 1_000
        if _test_race_artifact not in {None, "main", "wal", "shm"}:
            raise ValueError(
                "_test_race_artifact must be None, 'main', 'wal', or 'shm'."
            )
        if _test_cleanup_fault not in {
            None,
            "proof_close",
            "shm_unmap",
            "base_close",
        }:
            raise ValueError(
                "_test_cleanup_fault must be None, 'proof_close', "
                "'shm_unmap', or 'base_close'."
            )
        if _test_statement_limit_fault not in {
            None,
            "statement_limit_install",
            "statement_limit_verify",
            "statement_limit_restore",
        }:
            raise ValueError(
                "_test_statement_limit_fault must be None, "
                "'statement_limit_install', 'statement_limit_verify', or "
                "'statement_limit_restore'."
            )
        if _test_pre_open_callback is not None and not callable(
            _test_pre_open_callback
        ):
            raise TypeError("_test_pre_open_callback must be callable or None.")

        try:
            # Claim before any source stat/open.  On POSIX, even opening and
            # closing a second fd for a database inode would release fcntl
            # locks held by an existing reader in this process.
            _claim_process_session(self._process_session_owner)
            _check_pinned_sqlite_runtime()
            source, main_identity = _capture_source_identity(database_path)
            if expected_database_instance is not None and database_instances_differ(
                expected_database_instance,
                source.database_instance,
            ):
                raise TrustedLiveSourceChangedError(
                    "A checked identity observation proved that the selected "
                    "database differs from the instance approved before preflight."
                )
            self._source = source
            self._token = TrustedLiveReaderToken(source, self._token_string)

            self._temporary_directory = tempfile.TemporaryDirectory(
                prefix="qplot-trusted-live-"
            )
            temporary_path = Path(self._temporary_directory.name)
            # macOS commonly spells its private temporary root through the
            # `/var -> /private/var` compatibility symlink.  Canonicalise only
            # this application-created directory so native proof handles never
            # need to weaken source-path symlink rejection.
            if os.name != "nt":
                temporary_path = Path(os.path.realpath(temporary_path))
            self._native_temporary_path = temporary_path
            try:
                self._bootstrap = apsw.Connection(":memory:")
                self._bootstrap.config(
                    apsw.SQLITE_DBCONFIG_ENABLE_LOAD_EXTENSION,
                    1,
                )
                try:
                    self._bootstrap.load_extension(
                        str(_native_extension_path()),
                        _NATIVE_ENTRYPOINT,
                    )
                    self._native_extension_loaded = True
                finally:
                    self._bootstrap.config(
                        apsw.SQLITE_DBCONFIG_ENABLE_LOAD_EXTENSION,
                        0,
                    )
            except apsw.Error as error:
                raise TrustedLiveReaderUnavailableError(
                    "qPlot's pinned native trusted-reader boundary could not "
                    "be initialised."
                ) from error

            resolved_path = source.database_instance.resolved_path
            final_preopen_instance, final_main_identity = (
                _capture_database_instance_for_trusted_open(
                    source.database_instance.logical_path
                )
            )
            if database_instances_differ(
                source.database_instance,
                final_preopen_instance,
            ) or (
                expected_database_instance is not None
                and database_instances_differ(
                    expected_database_instance,
                    final_preopen_instance,
                )
            ):
                raise TrustedLiveSourceChangedError(
                    "Checked identity observations proved that the selected "
                    "database changed during trusted-reader preflight."
                )
            if final_main_identity != main_identity:
                raise TrustedLiveSourceChangedError(
                    "Checked native identities proved that the selected database "
                    "changed during trusted-reader preflight."
                )
            expected_identity_query = "&".join(
                (
                    _native_expected_identity_parameters(
                        "qplot_expected",
                        main_identity,
                        allow_absent=False,
                    ),
                    _native_expected_identity_parameters(
                        "qplot_expected_wal",
                        source.wal_identity,
                        allow_absent=True,
                    ),
                    _native_expected_identity_parameters(
                        "qplot_expected_shm",
                        source.shm_identity,
                        allow_absent=True,
                    ),
                )
            )
            query = (
                f"qplot_token={quote(self._token_string, safe='')}"
                "&qplot_temp="
                f"{_sqlite_uri_path(os.fspath(temporary_path))}"
                f"&{expected_identity_query}"
            )
            if _test_race_artifact is not None:
                query += f"&qplot_test_race={_test_race_artifact}"
            if _test_cleanup_fault is not None:
                query += f"&qplot_test_cleanup_fault={_test_cleanup_fault}"
            uri = f"{Path(resolved_path).as_uri()}?{query}"
            if _test_pre_open_callback is not None:
                _test_pre_open_callback(
                    self._token,
                    temporary_path,
                )
            if (
                _optional_file_identity(
                    Path(f"{resolved_path}-journal"),
                    "rollback journal",
                )
                is not None
            ):
                raise TrustedLiveUnsupportedSourceError(
                    "A rollback journal appeared during trusted-reader preflight; "
                    "the ambiguous source family was rejected."
                )
            self._native_session_attempted = True
            self._connection = apsw.Connection(
                uri,
                flags=apsw.SQLITE_OPEN_READONLY | apsw.SQLITE_OPEN_URI,
                vfs=TRUSTED_READER_VFS_NAME,
                statementcachesize=0,
            )
            self._connection.config(
                apsw.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE,
                1,
            )
            self._connection.config(apsw.SQLITE_DBCONFIG_DEFENSIVE, 1)
            self._connection.config(
                apsw.SQLITE_DBCONFIG_TRUSTED_SCHEMA,
                0,
            )
            # The raw/logical pre-yield row proof owns APSW's standard
            # tuple/scalar conversion path.  A row trace or JSONB converter
            # could replace a bounded SQLite value with an arbitrary object.
            self._connection.row_trace = None
            self._connection.convert_jsonb = None
            # Each finite operation installs its own deadline-aware busy
            # handler.  There must be no independent unbounded SQLite wait.
            self._connection.set_busy_timeout(0)
            self._connection.pragma("query_only", True)
            self._connection.pragma("temp_store", "MEMORY")
            self._connection.pragma("mmap_size", 0)
            self._connection.authorizer = self._authorize
            if not self._connection.readonly("main"):
                raise TrustedLiveReaderUnavailableError(
                    "The native VFS did not report a read-only main database."
                )
            self._validate_native_source()
            # Force schema and any currently present WAL/SHM opening while the
            # native expected-main proof is still part of construction.
            self.data_version()
            self.query("SELECT name FROM sqlite_schema LIMIT 1")
            self._test_statement_limit_fault = _test_statement_limit_fault
        except apsw.Error as error:
            translated = self._translate_sqlite_error(error, native_sequence=None)
            self._closed = True
            cleanup_error = self._close_resources(force=True)
            if cleanup_error is not None:
                translated.__cause__ = error
                raise cleanup_error from translated
            raise translated from error
        except (
            FileIdentityHandleCloseError,
            _PreflightFileDescriptorCloseError,
        ) as error:
            self._preflight_cleanup_error = error
            self._closed = True
            cleanup_error = self._close_resources(force=True)
            if cleanup_error is None:
                cleanup_error = TrustedLiveCleanupError(
                    "The trusted reader could not prove release of a preflight "
                    "source handle; this process is quarantined."
                )
            raise cleanup_error from error
        except BaseException as error:
            self._closed = True
            cleanup_error = self._close_resources(force=True)
            if cleanup_error is not None:
                if cleanup_error is error:
                    raise
                raise cleanup_error from error
            raise

    @classmethod
    def open(
        cls,
        database_path: str | os.PathLike[str],
        *,
        expected_database_instance: DatabaseInstance | None = None,
        busy_timeout_ms: int = 5_000,
        operation_timeout_seconds: float = (DEFAULT_TRUSTED_OPERATION_TIMEOUT_SECONDS),
        _test_race_artifact: str | None = None,
        _test_cleanup_fault: str | None = None,
        _test_statement_limit_fault: str | None = None,
        _test_pre_open_callback: (
            Callable[[TrustedLiveReaderToken, Path], None] | None
        ) = None,
    ) -> TrustedLiveReader:
        """Open the selected source directly, with no snapshot fallback.

        ``operation_timeout_seconds`` is the finite default for later calls
        which do not provide their own ``timeout`` or ``deadline``.
        """
        return cls(
            database_path,
            expected_database_instance=expected_database_instance,
            busy_timeout_ms=busy_timeout_ms,
            operation_timeout_seconds=operation_timeout_seconds,
            _test_race_artifact=_test_race_artifact,
            _test_cleanup_fault=_test_cleanup_fault,
            _test_statement_limit_fault=_test_statement_limit_fault,
            _test_pre_open_callback=_test_pre_open_callback,
        )

    @property
    def source_identity(self) -> TrustedLiveSourceIdentity:
        return self._source

    @property
    def token(self) -> TrustedLiveReaderToken:
        return self._token

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def temporary_directory(self) -> Path | None:
        temporary_directory = self._temporary_directory
        if temporary_directory is None:
            return None
        return self._native_temporary_path

    def _check_thread(self) -> None:
        if threading.get_ident() != self._owner_thread:
            raise TrustedLiveReaderThreadError(
                "Trusted live readers may only be used by their owner thread."
            )

    def _require_open(self) -> apsw.Connection:
        self._check_thread()
        if self._invalidated:
            raise TrustedLiveSourceChangedError(
                "The trusted database instance changed; close this reader and "
                "open a new one explicitly."
            )
        if self._closed or self._connection is None:
            raise TrustedLiveReaderClosedError("The trusted live reader is closed.")
        return self._connection

    @staticmethod
    def _validated_duration(
        value: float,
        *,
        description: str,
        allow_zero: bool,
    ) -> float:
        if isinstance(value, bool):
            raise ValueError(f"{description} must be a finite number of seconds.")
        try:
            duration = float(value)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"{description} must be a finite number of seconds."
            ) from error
        if (
            not math.isfinite(duration)
            or duration < 0
            or (duration == 0 and not allow_zero)
        ):
            qualifier = "non-negative" if allow_zero else "positive"
            raise ValueError(
                f"{description} must be a finite {qualifier} number of seconds."
            )
        return duration

    def _operation_deadline(
        self,
        *,
        timeout: float | None,
        deadline: float | None,
    ) -> tuple[float, float]:
        started_at = time.monotonic()
        candidates: list[float] = []
        if timeout is not None:
            candidates.append(
                started_at
                + self._validated_duration(
                    timeout,
                    description="trusted-reader operation timeout",
                    allow_zero=True,
                )
            )
        if deadline is not None:
            if isinstance(deadline, bool):
                raise ValueError("deadline must be a finite monotonic timestamp.")
            try:
                absolute_deadline = float(deadline)
            except (TypeError, ValueError) as error:
                raise ValueError(
                    "deadline must be a finite monotonic timestamp."
                ) from error
            if not math.isfinite(absolute_deadline):
                raise ValueError("deadline must be a finite monotonic timestamp.")
            candidates.append(absolute_deadline)
        if not candidates:
            candidates.append(started_at + self._default_operation_timeout_seconds)
        return started_at, min(candidates)

    def interrupt(self) -> bool:
        """Cancel the currently active finite operation from any thread.

        The request is generation-bound: calling this method while the reader
        is idle does not poison the next operation.  ``True`` means an active
        operation was marked and ``sqlite3_interrupt`` was invoked.
        """

        with self._operation_mutex:
            control = self._active_operation
            connection = self._connection
            if control is None or connection is None:
                return False
            if control.abort_reason is None:
                control.abort_reason = "cancelled"
            # Keep publication and sqlite3_interrupt() in one generation lock.
            # Otherwise the old operation could finish and a new one could be
            # published between these two actions, receiving a stale interrupt.
            connection.interrupt()
            return True

    def _mark_operation_abort(
        self,
        control: _TrustedOperationControl,
        reason: str,
    ) -> None:
        with self._operation_mutex:
            # Cleanup clears the published active generation before consulting
            # the native control channel.  It may still need to record a
            # deadline/cancellation against that same, now unpublished control.
            if (
                self._active_operation is None or self._active_operation is control
            ) and control.abort_reason is None:
                control.abort_reason = reason

    def _operation_abort_reason(
        self,
        control: _TrustedOperationControl,
    ) -> str | None:
        with self._operation_mutex:
            return control.abort_reason

    def _raise_if_operation_aborted(
        self,
        control: _TrustedOperationControl,
    ) -> None:
        cancel_event = control.cancel_event
        if cancel_event is not None and cancel_event.is_set():
            self._mark_operation_abort(control, "cancelled")
        if time.monotonic() >= control.deadline:
            self._mark_operation_abort(control, "deadline")
        reason = self._operation_abort_reason(control)
        if reason == "cancelled":
            raise TrustedLiveCancelledError("The trusted database read was cancelled.")
        if reason == "deadline":
            raise TrustedLiveDeadlineExceededError(
                "The trusted database read deadline was exceeded."
            )

    def _progress_handler(self, control: _TrustedOperationControl) -> bool:
        try:
            self._raise_if_operation_aborted(control)
        except (TrustedLiveCancelledError, TrustedLiveDeadlineExceededError):
            return True
        return False

    def _busy_handler(
        self,
        control: _TrustedOperationControl,
        prior_calls: int,
    ) -> bool:
        del prior_calls
        try:
            self._raise_if_operation_aborted(control)
        except (TrustedLiveCancelledError, TrustedLiveDeadlineExceededError):
            return False
        now = time.monotonic()
        if now >= control.busy_deadline:
            return False
        delay = min(_BUSY_RETRY_QUANTUM_SECONDS, control.busy_deadline - now)
        cancel_event = control.cancel_event
        if cancel_event is None:
            time.sleep(delay)
        else:
            cancel_event.wait(delay)
        try:
            self._raise_if_operation_aborted(control)
        except (TrustedLiveCancelledError, TrustedLiveDeadlineExceededError):
            return False
        return time.monotonic() < control.busy_deadline

    def _start_operation(
        self,
        *,
        timeout: float | None,
        deadline: float | None,
        cancel_event: threading.Event | None,
    ) -> tuple[apsw.Connection, _TrustedOperationControl]:
        connection = self._require_open()
        if cancel_event is not None and not isinstance(cancel_event, threading.Event):
            raise TypeError("cancel_event must be a threading.Event or None.")
        started_at, operation_deadline = self._operation_deadline(
            timeout=timeout,
            deadline=deadline,
        )
        with self._operation_mutex:
            if self._active_operation is not None:
                raise TrustedLiveTransactionError(
                    "Nested trusted-reader operations are not supported."
                )
            generation = self._next_operation_generation
            self._next_operation_generation += 1
            control = _TrustedOperationControl(
                generation=generation,
                started_at=started_at,
                deadline=operation_deadline,
                busy_deadline=min(
                    operation_deadline,
                    started_at + self._busy_timeout_seconds,
                ),
                cancel_event=cancel_event,
            )
            self._active_operation = control
        return connection, control

    @staticmethod
    def _progress_handler_id(control: _TrustedOperationControl) -> tuple[str, int]:
        return ("qplot-trusted-operation", control.generation)

    def _install_operation_handlers(
        self,
        connection: apsw.Connection,
        control: _TrustedOperationControl,
    ) -> None:
        connection.set_progress_handler(
            lambda: self._progress_handler(control),
            _PROGRESS_HANDLER_STEPS,
            id=self._progress_handler_id(control),
        )
        connection.set_busy_handler(
            lambda prior_calls: self._busy_handler(control, prior_calls)
        )

    def _remove_operation_handlers(
        self,
        connection: apsw.Connection,
        control: _TrustedOperationControl,
    ) -> list[BaseException]:
        errors: list[BaseException] = []
        try:
            connection.set_progress_handler(
                None,
                id=self._progress_handler_id(control),
            )
        except BaseException as error:
            errors.append(error)
        try:
            connection.set_busy_handler(None)
        except BaseException as error:
            errors.append(error)
        return errors

    def _record_policy_denial(self) -> int:
        """Mark the active operation and return SQLite's deny result."""

        with self._operation_mutex:
            control = self._active_operation
            if control is not None:
                control.policy_denied = True
        return apsw.SQLITE_DENY

    def _authorize(
        self,
        operation: int,
        parameter_one: str | None,
        parameter_two: str | None,
        database_name: str | None,
        trigger_or_view: str | None,
    ) -> int:
        del trigger_or_view
        if operation in {
            apsw.SQLITE_SELECT,
            apsw.SQLITE_READ,
            apsw.SQLITE_RECURSIVE,
        }:
            return apsw.SQLITE_OK
        if operation == apsw.SQLITE_FUNCTION:
            function_name = (parameter_two or "").casefold()
            if function_name in {
                "eval",
                "fts3_tokenizer",
                "load_extension",
                "readfile",
                "writefile",
            }:
                return self._record_policy_denial()
            return apsw.SQLITE_OK
        if operation == apsw.SQLITE_PRAGMA:
            pragma_name = (parameter_one or "").casefold()
            if (
                pragma_name == "data_version"
                and parameter_two is None
                and database_name in {None, "main"}
            ):
                return apsw.SQLITE_OK
            return self._record_policy_denial()
        if operation == apsw.SQLITE_TRANSACTION and self._internal_transaction_control:
            return apsw.SQLITE_OK
        return self._record_policy_denial()

    def _execute_transaction_control(
        self,
        sql: str,
    ) -> None:
        self._check_thread()
        connection = self._connection
        if self._closed or connection is None:
            raise TrustedLiveReaderClosedError("The trusted live reader is closed.")
        self._internal_transaction_control = True
        cursor: apsw.Cursor | None = None
        body_error: BaseException | None = None
        body_traceback: TracebackType | None = None
        try:
            cursor = connection.cursor()
            cursor.execute(sql).fetchall()
        except BaseException as error:
            body_error = error
            body_traceback = error.__traceback__
        finally:
            self._internal_transaction_control = False
            if cursor is not None:
                try:
                    cursor.close()
                except BaseException as close_error:
                    if body_error is None:
                        body_error = close_error
                        body_traceback = close_error.__traceback__
                    else:
                        body_error.add_note(
                            f"SQLite cursor cleanup also failed: {close_error!r}"
                        )
        if body_error is not None:
            raise body_error.with_traceback(body_traceback)

    def _install_result_length_limit(
        self,
        connection: apsw.Connection,
        control: _TrustedOperationControl,
    ) -> _TrustedSqliteLengthLimit:
        """Install and verify the operation's absolute SQLite scalar cap."""

        self._raise_if_operation_aborted(control)
        try:
            previous = connection.limit(apsw.SQLITE_LIMIT_LENGTH)
        except BaseException as error:
            raise TrustedLiveCleanupError(
                "The trusted reader could not inspect SQLite's prior "
                "result-length limit; the connection must be retired."
            ) from error
        effective = min(previous, TRUSTED_LIVE_MAX_SCALAR_BYTES)
        try:
            observed_previous = connection.limit(
                apsw.SQLITE_LIMIT_LENGTH,
                effective,
            )
            observed_effective = connection.limit(apsw.SQLITE_LIMIT_LENGTH)
        except BaseException as error:
            setup_error = TrustedLiveCleanupError(
                "The trusted reader could not install its SQLite result-length "
                "limit; the connection must be retired."
            )
            try:
                connection.limit(apsw.SQLITE_LIMIT_LENGTH, previous)
                restored = connection.limit(apsw.SQLITE_LIMIT_LENGTH)
            except BaseException as restore_error:
                setup_error.add_note(
                    f"SQLite limit restoration also failed: {restore_error!r}"
                )
            else:
                if restored != previous:
                    setup_error.add_note(
                        "SQLite did not confirm restoration of its prior length limit."
                    )
            raise setup_error from error
        if observed_previous != previous or observed_effective != effective:
            try:
                connection.limit(apsw.SQLITE_LIMIT_LENGTH, previous)
            except BaseException as restore_error:
                cleanup_failure = TrustedLiveCleanupError(
                    "The trusted reader could neither verify nor restore its "
                    "SQLite result-length limit."
                )
                cleanup_failure.add_note(
                    f"SQLite limit restoration also failed: {restore_error!r}"
                )
                raise cleanup_failure from restore_error
            raise TrustedLiveCleanupError(
                "The trusted reader could not verify its SQLite result-length "
                "limit; the connection must be retired."
            )
        control.operation_result_length_limit = effective
        control.result_length_limit = effective
        return _TrustedSqliteLengthLimit(previous=previous, effective=effective)

    def _consume_statement_limit_fault(self, phase: str) -> bool:
        if (
            not self._test_statement_limit_fault_consumed
            and self._test_statement_limit_fault == phase
        ):
            self._test_statement_limit_fault_consumed = True
            return True
        return False

    @staticmethod
    def _note_best_effort_limit_restore(
        connection: apsw.Connection,
        target: int,
        setup_error: TrustedLiveCleanupError,
    ) -> None:
        """Try to regain a known limit after uncertain statement setup."""

        try:
            connection.limit(apsw.SQLITE_LIMIT_LENGTH, target)
            restored = connection.limit(apsw.SQLITE_LIMIT_LENGTH)
        except BaseException as restore_error:
            setup_error.add_note(
                f"SQLite limit restoration also failed: {restore_error!r}"
            )
        else:
            if restored != target:
                setup_error.add_note(
                    "SQLite did not confirm restoration of the operation-wide "
                    "length limit."
                )

    def _install_statement_result_length_limit(
        self,
        connection: apsw.Connection,
        control: _TrustedOperationControl,
        column_count: int,
    ) -> _TrustedSqliteLengthLimit:
        """Install the width-derived cap before APSW can produce one row."""

        self._raise_if_operation_aborted(control)
        baseline = control.operation_result_length_limit
        if baseline is None:
            raise TrustedLiveCleanupError(
                "The trusted reader lost its operation-wide SQLite result-length "
                "baseline; the connection must be retired."
            )
        try:
            observed_baseline = connection.limit(apsw.SQLITE_LIMIT_LENGTH)
            row_trace = connection.row_trace
            convert_jsonb = connection.convert_jsonb
        except BaseException as error:
            raise TrustedLiveCleanupError(
                "The trusted reader could not inspect the SQLite row-conversion "
                "and result-length state; the connection must be retired."
            ) from error
        if observed_baseline != baseline:
            raise TrustedLiveCleanupError(
                "SQLite did not retain the verified operation-wide result-length "
                "baseline; the connection must be retired."
            )
        if row_trace is not None or convert_jsonb is not None:
            raise TrustedLiveCleanupError(
                "APSW's standard tuple/scalar row conversion was not active; "
                "the raw/logical pre-yield row bounds could not be proved."
            )

        effective = _trusted_statement_length_limit(baseline, column_count)
        self._raise_if_operation_aborted(control)
        if self._consume_statement_limit_fault("statement_limit_install"):
            raise TrustedLiveCleanupError(
                "Injected per-statement SQLite result-length installation "
                "failure; the connection must be retired."
            )
        try:
            observed_previous = connection.limit(
                apsw.SQLITE_LIMIT_LENGTH,
                effective,
            )
            observed_effective = connection.limit(apsw.SQLITE_LIMIT_LENGTH)
        except BaseException as error:
            setup_error = TrustedLiveCleanupError(
                "The trusted reader could not install its per-statement SQLite "
                "result-length limit; the connection must be retired."
            )
            self._note_best_effort_limit_restore(connection, baseline, setup_error)
            raise setup_error from error

        verification_fault = self._consume_statement_limit_fault(
            "statement_limit_verify"
        )
        if (
            verification_fault
            or observed_previous != baseline
            or observed_effective != effective
        ):
            setup_error = TrustedLiveCleanupError(
                "The trusted reader could not verify its per-statement SQLite "
                "result-length limit; the connection must be retired."
            )
            self._note_best_effort_limit_restore(connection, baseline, setup_error)
            raise setup_error

        control.result_length_limit = effective
        return _TrustedSqliteLengthLimit(previous=baseline, effective=effective)

    def _restore_statement_result_length_limit(
        self,
        connection: apsw.Connection,
        control: _TrustedOperationControl,
        installed: _TrustedSqliteLengthLimit,
    ) -> TrustedLiveCleanupError | None:
        """Restore and verify the operation baseline after one statement."""

        if self._consume_statement_limit_fault("statement_limit_restore"):
            return TrustedLiveCleanupError(
                "Injected per-statement SQLite result-length restoration "
                "failure; the connection must be retired."
            )
        try:
            observed_effective = connection.limit(
                apsw.SQLITE_LIMIT_LENGTH,
                installed.previous,
            )
            observed_baseline = connection.limit(apsw.SQLITE_LIMIT_LENGTH)
        except BaseException as error:
            return TrustedLiveCleanupError(
                "The trusted reader could not restore the operation-wide SQLite "
                f"result-length baseline: {error}"
            )
        if (
            observed_effective != installed.effective
            or observed_baseline != installed.previous
        ):
            return TrustedLiveCleanupError(
                "The trusted reader could not verify restoration of the "
                "operation-wide SQLite result-length baseline."
            )
        control.result_length_limit = installed.previous
        return None

    def _restore_result_length_limit(
        self,
        connection: apsw.Connection,
        control: _TrustedOperationControl,
        installed: _TrustedSqliteLengthLimit,
    ) -> TrustedLiveCleanupError | None:
        """Restore and verify SQLite's prior runtime length limit."""

        try:
            observed_effective = connection.limit(
                apsw.SQLITE_LIMIT_LENGTH,
                installed.previous,
            )
            observed_previous = connection.limit(apsw.SQLITE_LIMIT_LENGTH)
        except BaseException as error:
            return TrustedLiveCleanupError(
                "The trusted reader could not restore its prior SQLite "
                f"result-length limit: {error}"
            )
        if (
            observed_effective != installed.effective
            or observed_previous != installed.previous
        ):
            return TrustedLiveCleanupError(
                "The trusted reader could not verify restoration of its prior "
                "SQLite result-length limit."
            )
        control.operation_result_length_limit = None
        control.result_length_limit = None
        return None

    def _query_spec_in_transaction(
        self,
        connection: apsw.Connection,
        query: TrustedQuery,
        result_budget: _TrustedResultBudget,
        control: _TrustedOperationControl,
        operation_cleanup_errors: list[BaseException],
    ) -> TrustedQueryResult:
        sql = query.sql
        bindings = query.bindings
        if not isinstance(sql, str) or not sql.strip() or "\x00" in sql:
            raise TrustedLiveSqlRejectedError(
                "A trusted query must be one non-empty SQLite statement."
            )
        details = apsw.ext.query_info(connection, sql, bindings)
        if (
            (details.query_remaining is not None and details.query_remaining.strip())
            or not details.is_readonly
            or not details.has_vdbe
            or details.is_explain
        ):
            raise TrustedLiveSqlRejectedError(
                "The trusted reader accepts exactly one ordinary read-only "
                "SELECT or PRAGMA data_version statement."
            )
        columns = result_budget.start_result(details.description)
        cursor: apsw.Cursor | None = None
        statement_length_limit: _TrustedSqliteLengthLimit | None = None
        body_error: BaseException | None = None
        body_cause: BaseException | None = None
        body_traceback: TracebackType | None = None
        result: TrustedQueryResult | None = None
        try:
            try:
                statement_length_limit = self._install_statement_result_length_limit(
                    connection,
                    control,
                    len(columns),
                )
            except TrustedLiveCleanupError:
                operation_cleanup_errors.append(
                    TrustedLiveCleanupError(
                        "Per-statement SQLite result-limit setup was not proved "
                        "safe; the reader connection must be retired."
                    )
                )
                raise
            self._raise_if_operation_aborted(control)
            cursor = connection.cursor()
            cursor.row_trace = None
            cursor.execute(details.first_query, bindings)
            try:
                execution_description = cursor.get_description()
            except apsw.ExecutionCompleteError:
                # APSW releases the completed statement before exposing its
                # description when a valid SELECT produces zero rows.  The
                # exact same statement was prepared, authorised, and bounded
                # immediately above by query_info(), so its validated
                # description remains the authoritative zero-row metadata.
                execution_description = details.description
            if execution_description != details.description:
                raise TrustedLiveQueryError(
                    "SQLite result metadata changed between validation and execution."
                )
            retained_rows: list[tuple[Any, ...]] = []
            for row in cursor:
                self._raise_if_operation_aborted(control)
                retained_row = result_budget.retain_row(
                    row,
                    column_count=len(columns),
                    retained_row_count=len(retained_rows),
                )
                retained_rows.append(retained_row)
            result = TrustedQueryResult(columns=columns, rows=tuple(retained_rows))
        except apsw.Error as error:
            body_error = self._translate_sqlite_error(
                error,
                native_sequence=control.native_sequence,
                control=control,
            )
            body_cause = error
            body_traceback = body_error.__traceback__
        except BaseException as error:
            body_error = error
            body_traceback = error.__traceback__
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except BaseException as close_error:
                    operation_cleanup_errors.append(close_error)
                    if body_error is None:
                        body_error = close_error
                        body_traceback = close_error.__traceback__
                    else:
                        body_error.add_note(
                            f"SQLite cursor cleanup also failed: {close_error!r}"
                        )
            if statement_length_limit is not None:
                restore_error = self._restore_statement_result_length_limit(
                    connection,
                    control,
                    statement_length_limit,
                )
                if restore_error is not None:
                    operation_cleanup_errors.append(
                        TrustedLiveCleanupError(
                            "Per-statement SQLite result-limit restoration was "
                            "not proved safe; the reader connection must be retired."
                        )
                    )
                    if body_error is None:
                        body_error = restore_error
                        body_traceback = restore_error.__traceback__
                    else:
                        body_error.add_note(
                            "SQLite statement-limit cleanup also failed: "
                            f"{restore_error!r}"
                        )
            if body_error is None:
                try:
                    self._raise_if_operation_aborted(control)
                except BaseException as error:
                    body_error = error
                    body_traceback = error.__traceback__
        if body_error is not None:
            if body_cause is not None:
                raise body_error.with_traceback(body_traceback) from body_cause
            raise body_error.with_traceback(body_traceback)
        if result is None:
            raise TrustedLiveQueryError(
                "The trusted query completed without a materialised result."
            )
        return result

    def _bootstrap_scalar(
        self,
        sql: str,
        *,
        bootstrap: apsw.Connection | None = None,
    ) -> Any:
        if bootstrap is None:
            bootstrap = self._bootstrap
        if bootstrap is None:
            raise TrustedLiveReaderUnavailableError(
                "The trusted live reader's native control channel is closed."
            )
        cursor: apsw.Cursor | None = None
        body_error: BaseException | None = None
        body_traceback: TracebackType | None = None
        value: Any = None
        try:
            cursor = bootstrap.cursor()
            row = cursor.execute(sql, (self._token_string,)).fetchone()
            if row is None or len(row) != 1:
                raise TrustedLiveReaderUnavailableError(
                    "The native trusted VFS returned an invalid control result."
                )
            value = row[0]
        except BaseException as error:
            body_error = error
            body_traceback = error.__traceback__
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except BaseException as close_error:
                    if body_error is None:
                        body_error = close_error
                        body_traceback = close_error.__traceback__
                    else:
                        body_error.add_note(
                            f"Native control cursor cleanup also failed: "
                            f"{close_error!r}"
                        )
        if body_error is not None:
            raise body_error.with_traceback(body_traceback)
        return value

    def _read_native_status(self) -> _TrustedNativeStatus:
        raw_status = self._bootstrap_scalar("SELECT qplot_trusted_vfs_status(?)")
        if not isinstance(raw_status, str):
            raise TrustedLiveReaderUnavailableError(
                "The native trusted VFS returned a non-text status."
            )
        try:
            parsed = json.loads(raw_status)
        except json.JSONDecodeError as error:
            raise TrustedLiveReaderUnavailableError(
                "The native trusted VFS returned malformed status JSON."
            ) from error
        expected_keys = {
            "sequence",
            "kind",
            "artifact",
            "operation",
            "sqlite_code",
        }
        if (
            not isinstance(parsed, dict)
            or set(parsed) != expected_keys
            or type(parsed["sequence"]) is not int
            or parsed["sequence"] < 0
            or parsed["kind"]
            not in {"none", "source_changed", "unsupported", "policy", "io"}
            or parsed["artifact"]
            not in {"none", "main", "wal", "shm", "journal", "temp"}
            or not isinstance(parsed["operation"], str)
            or type(parsed["sqlite_code"]) is not int
            or parsed["sqlite_code"] < 0
        ):
            raise TrustedLiveReaderUnavailableError(
                "The native trusted VFS returned an invalid status record."
            )
        return _TrustedNativeStatus(
            sequence=parsed["sequence"],
            kind=parsed["kind"],
            artifact=parsed["artifact"],
            operation=parsed["operation"],
            sqlite_code=parsed["sqlite_code"],
        )

    def _native_status_if_available(self) -> _TrustedNativeStatus | None:
        if not self._native_extension_loaded or self._bootstrap is None:
            return None
        try:
            return self._read_native_status()
        except (apsw.Error, TrustedLiveReaderError):
            return None

    def _validate_native_source(self) -> None:
        result = self._bootstrap_scalar("SELECT qplot_trusted_vfs_validate(?)")
        if result != 1:
            raise TrustedLiveReaderUnavailableError(
                "The native trusted VFS returned an invalid validation result."
            )

    def _error_from_native_status(
        self,
        status: _TrustedNativeStatus,
    ) -> TrustedLiveReaderError | None:
        location = (
            f" ({status.artifact}/{status.operation}, SQLite code {status.sqlite_code})"
        )
        if status.kind == "source_changed":
            self._invalidated = True
            return TrustedLiveSourceChangedError(
                "The native trusted VFS proved that a source file was "
                f"replaced or changed identity{location}."
            )
        if status.kind == "unsupported":
            return TrustedLiveUnsupportedSourceError(
                f"The native trusted VFS rejected the source{location}."
            )
        if status.kind == "policy":
            return TrustedLiveSqlRejectedError(
                f"The native trusted VFS rejected a prohibited operation{location}."
            )
        if status.kind == "io":
            return TrustedLiveSourceIOError(
                f"The native trusted VFS reported a source I/O failure{location}."
            )
        return None

    def _translate_sqlite_error(
        self,
        error: apsw.Error,
        *,
        native_sequence: int | None,
        control: _TrustedOperationControl | None = None,
    ) -> TrustedLiveReaderError:
        status = self._native_status_if_available()
        status_is_new = status is not None and (
            native_sequence is None or status.sequence != native_sequence
        )
        if status_is_new and status is not None:
            status_error = self._error_from_native_status(status)
            if status_error is not None:
                return status_error

        if control is not None:
            cancel_event = control.cancel_event
            if cancel_event is not None and cancel_event.is_set():
                self._mark_operation_abort(control, "cancelled")
            if time.monotonic() >= control.deadline:
                self._mark_operation_abort(control, "deadline")
            reason = self._operation_abort_reason(control)
            if reason == "cancelled":
                return TrustedLiveCancelledError(
                    "The trusted database read was cancelled."
                )
            if reason == "deadline":
                return TrustedLiveDeadlineExceededError(
                    "The trusted database read deadline was exceeded."
                )
            policy_denied = control.policy_denied
        else:
            policy_denied = False

        if isinstance(error, (apsw.BusyError, apsw.LockedError)):
            return TrustedLiveBusyTimeoutError(
                f"The trusted database remained busy beyond its bounded wait: {error}"
            )
        if isinstance(error, apsw.InterruptError):
            return TrustedLiveQueryError(
                "SQLite interrupted the trusted query without a matching "
                "cancellation or deadline request."
            )
        if isinstance(error, (apsw.CorruptError, apsw.NotADBError, apsw.FormatError)):
            return TrustedLiveInvalidDatabaseError(
                f"The selected database is corrupt or invalid: {error}"
            )
        if isinstance(error, apsw.TooBigError) and (
            control is not None and control.result_length_limit is not None
        ):
            return TrustedLiveResultLimitError(
                "SQLite stopped producing a text or blob result at qPlot's "
                f"{control.result_length_limit}-byte runtime length limit."
            )
        if policy_denied:
            return TrustedLiveSqlRejectedError(
                f"The trusted reader rejected the SQL statement: {error}"
            )
        if isinstance(error, apsw.ReadOnlyError):
            return TrustedLiveSourceIOError(
                "SQLite's physically read-only source boundary could not "
                f"complete required coordination: {error}"
            )
        if isinstance(error, apsw.PermissionsError):
            return TrustedLiveSourceIOError(
                f"SQLite could not access the trusted source: {error}"
            )
        if isinstance(
            error,
            (
                apsw.CantOpenError,
                apsw.NoLFSError,
                apsw.VFSNotImplementedError,
            ),
        ):
            return TrustedLiveUnsupportedSourceError(
                f"The selected source or platform is unsupported: {error}"
            )
        if isinstance(error, (apsw.IOError, apsw.FullError)):
            return TrustedLiveSourceIOError(
                f"SQLite could not read the trusted source: {error}"
            )
        if isinstance(
            error,
            (
                apsw.BindingsError,
                apsw.MismatchError,
                apsw.RangeError,
                apsw.SchemaChangeError,
                apsw.SQLError,
            ),
        ):
            return TrustedLiveQueryError(f"The trusted read-only query failed: {error}")
        if control is not None:
            return TrustedLiveQueryError(
                f"SQLite failed while executing the trusted query: {error}"
            )
        return TrustedLiveReaderUnavailableError(
            f"SQLite failed while establishing the trusted reader boundary: {error}"
        )

    def _finish_operation(
        self,
        connection: apsw.Connection,
        control: _TrustedOperationControl,
        *,
        transaction_started: bool,
        handlers_installed: bool,
        publication_pending: bool,
        prior_cleanup_errors: Sequence[BaseException] = (),
    ) -> TrustedLiveReaderError | None:
        with self._operation_mutex:
            if self._active_operation is control:
                self._active_operation = None

        cleanup_errors = list(prior_cleanup_errors)
        if handlers_installed:
            cleanup_errors.extend(self._remove_operation_handlers(connection, control))
        if transaction_started:
            try:
                # A fatal VFS/SQLite error can roll the transaction back before
                # Python reaches this cleanup path.  Treat that already-clean
                # state as success; issuing ROLLBACK then would create a false
                # "no transaction is active" cleanup failure and hide the
                # identity error that caused the automatic rollback.
                if connection.in_transaction:
                    self._execute_transaction_control("ROLLBACK")
            except BaseException as error:
                cleanup_errors.append(error)
        try:
            transaction_state = connection.txn_state("main")
        except BaseException as error:
            cleanup_errors.append(error)
        else:
            if transaction_state != apsw.SQLITE_TXN_NONE:
                cleanup_errors.append(
                    TrustedLiveCleanupError(
                        "SQLite retained a transaction after trusted-reader rollback."
                    )
                )
        try:
            self._validate_native_source()
            post_status = self._read_native_status()
            if publication_pending and post_status.sequence != control.native_sequence:
                status_error = self._error_from_native_status(post_status)
                if status_error is not None:
                    cleanup_errors.append(status_error)
        except apsw.Error as error:
            cleanup_errors.append(
                self._translate_sqlite_error(
                    error,
                    native_sequence=control.native_sequence,
                    control=control,
                )
            )
        except BaseException as error:
            cleanup_errors.append(error)

        if not cleanup_errors and publication_pending:
            if control.cancel_event is not None and control.cancel_event.is_set():
                return TrustedLiveCancelledError(
                    "The trusted database read was cancelled before publication."
                )
            if time.monotonic() >= control.deadline:
                return TrustedLiveDeadlineExceededError(
                    "The trusted database read deadline expired before publication."
                )
        if not cleanup_errors:
            return None

        if any(
            isinstance(error, TrustedLiveSourceChangedError) for error in cleanup_errors
        ):
            self._invalidated = True
        self._closed = True
        close_cleanup_error = self._close_resources(force=True)
        if close_cleanup_error is not None:
            for preceding_error in cleanup_errors:
                close_cleanup_error.add_note(
                    "Failure that required forced reader cleanup: "
                    f"{type(preceding_error).__name__}: {preceding_error}"
                )
            return close_cleanup_error
        primary = cleanup_errors[0]
        if isinstance(primary, TrustedLiveReaderError):
            cleanup_error = primary
        else:
            cleanup_error = TrustedLiveCleanupError(
                f"The trusted reader could not prove transaction cleanup: {primary}"
            )
        for additional_error in cleanup_errors[1:]:
            cleanup_error.add_note(f"Additional cleanup failure: {additional_error!r}")
        return cleanup_error

    def query_batch(
        self,
        queries: Sequence[TrustedQuery],
        *,
        timeout: float | None = None,
        deadline: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> tuple[TrustedQueryResult, ...]:
        """Run a finite query batch in one repeatable-read transaction.

        The supplied sequence is copied before ``BEGIN``.  Every item must be a
        :class:`TrustedQuery`; no callback or cursor escapes while SQLite holds
        read locks.  The effective deadline is the earliest of ``timeout`` and
        ``deadline``.  If neither is supplied, the reader's finite default is
        used.
        """

        specifications = tuple(queries)
        if not specifications:
            raise TrustedLiveSqlRejectedError(
                "A trusted query batch must contain at least one statement."
            )
        if len(specifications) > TRUSTED_LIVE_MAX_BATCH_QUERIES:
            raise TrustedLiveResultLimitError(
                "A trusted query batch contains more than "
                f"{TRUSTED_LIVE_MAX_BATCH_QUERIES} statements."
            )
        if not all(isinstance(query, TrustedQuery) for query in specifications):
            raise TypeError("query_batch accepts only TrustedQuery specifications.")

        result_budget = _TrustedResultBudget.for_query_batch()
        connection, control = self._start_operation(
            timeout=timeout,
            deadline=deadline,
            cancel_event=cancel_event,
        )
        result_budget.abort_check = lambda: self._raise_if_operation_aborted(control)
        transaction_started = False
        handlers_installed = False
        body_error: BaseException | None = None
        body_cause: BaseException | None = None
        body_traceback: TracebackType | None = None
        results: tuple[TrustedQueryResult, ...] | None = None
        result_length_limit: _TrustedSqliteLengthLimit | None = None
        operation_cleanup_errors: list[BaseException] = []
        try:
            self._raise_if_operation_aborted(control)
            status = self._read_native_status()
            control.native_sequence = status.sequence
            self._validate_native_source()
            self._raise_if_operation_aborted(control)
            handlers_installed = True
            self._install_operation_handlers(connection, control)
            try:
                result_length_limit = self._install_result_length_limit(
                    connection,
                    control,
                )
            except TrustedLiveCleanupError as error:
                operation_cleanup_errors.append(
                    TrustedLiveCleanupError(
                        "SQLite result-limit setup was not proved safe; the "
                        "reader connection must be retired."
                    )
                )
                raise error
            self._raise_if_operation_aborted(control)
            try:
                self._execute_transaction_control("BEGIN")
            finally:
                # If BEGIN itself completed but explicit cursor cleanup failed,
                # this still forces rollback on the exceptional path.
                transaction_started = connection.in_transaction
            materialised: list[TrustedQueryResult] = []
            for query in specifications:
                self._raise_if_operation_aborted(control)
                materialised.append(
                    self._query_spec_in_transaction(
                        connection,
                        query,
                        result_budget,
                        control,
                        operation_cleanup_errors,
                    )
                )
            self._raise_if_operation_aborted(control)
            results = tuple(materialised)
        except apsw.Error as error:
            body_error = self._translate_sqlite_error(
                error,
                native_sequence=control.native_sequence,
                control=control,
            )
            body_cause = error
            body_traceback = body_error.__traceback__
        except BaseException as error:
            body_error = error
            body_traceback = error.__traceback__
        finally:
            if result_length_limit is not None:
                restore_error = self._restore_result_length_limit(
                    connection,
                    control,
                    result_length_limit,
                )
                if restore_error is not None:
                    operation_cleanup_errors.append(restore_error)

        cleanup_error = self._finish_operation(
            connection,
            control,
            transaction_started=transaction_started,
            handlers_installed=handlers_installed,
            publication_pending=body_error is None,
            prior_cleanup_errors=operation_cleanup_errors,
        )
        if body_error is not None:
            if cleanup_error is not None:
                if isinstance(cleanup_error, TrustedLiveCleanupError) or self._closed:
                    preserved_body_error = body_error.with_traceback(body_traceback)
                    if body_cause is not None:
                        preserved_body_error.__cause__ = body_cause
                    raise cleanup_error from preserved_body_error
                body_error.add_note(
                    f"Trusted-reader cleanup also failed: {cleanup_error!r}"
                )
            if body_cause is not None:
                raise body_error.with_traceback(body_traceback) from body_cause
            raise body_error.with_traceback(body_traceback)
        if cleanup_error is not None:
            raise cleanup_error
        if results is None:
            raise TrustedLiveQueryError(
                "The trusted query batch completed without results."
            )
        return results

    def query(
        self,
        sql: str,
        bindings: SqliteBindings = None,
        *,
        timeout: float | None = None,
        deadline: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> TrustedQueryResult:
        """Run one materialised statement in a bounded read transaction."""

        return self.query_batch(
            (TrustedQuery(sql, bindings),),
            timeout=timeout,
            deadline=deadline,
            cancel_event=cancel_event,
        )[0]

    def data_version(
        self,
        *,
        timeout: float | None = None,
        deadline: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> int:
        """Return ``PRAGMA data_version`` in its own finite transaction."""

        result = self.query(
            "PRAGMA data_version",
            timeout=timeout,
            deadline=deadline,
            cancel_event=cancel_event,
        )
        if (
            result.columns != ("data_version",)
            or len(result.rows) != 1
            or len(result.rows[0]) != 1
            or not isinstance(result.rows[0][0], int)
        ):
            raise TrustedLiveReaderError(
                "SQLite returned an invalid PRAGMA data_version result."
            )
        return result.rows[0][0]

    def _read_native_audit(
        self,
        bootstrap: apsw.Connection,
    ) -> TrustedVfsAudit:
        raw_audit = self._bootstrap_scalar(
            "SELECT qplot_trusted_vfs_audit(?)",
            bootstrap=bootstrap,
        )
        if not isinstance(raw_audit, str):
            raise TrustedLiveReaderError(
                "The native VFS returned an invalid audit result."
            )
        try:
            parsed = json.loads(raw_audit)
        except json.JSONDecodeError as error:
            raise TrustedLiveReaderError(
                "The native VFS returned malformed audit counters."
            ) from error
        if not isinstance(parsed, dict) or not all(
            isinstance(key, str) and type(value) is int for key, value in parsed.items()
        ):
            raise TrustedLiveReaderError(
                "The native VFS returned malformed audit counters."
            )
        return TrustedVfsAudit(MappingProxyType(parsed))

    def audit(self) -> TrustedVfsAudit:
        """Return native counters, including the final close-time snapshot."""
        self._check_thread()
        bootstrap = self._bootstrap
        if bootstrap is not None:
            return self._read_native_audit(bootstrap)
        if self._final_audit is not None:
            return self._final_audit
        raise TrustedLiveReaderClosedError(
            "The trusted live reader audit channel is closed."
        )

    @staticmethod
    def _verify_final_audit(audit: TrustedVfsAudit) -> None:
        """Require native proof and SHM state to be fully released."""

        counters = audit.counters
        required = {
            "base_close_error",
            "proof_open",
            "proof_close",
            "proof_close_error",
            "proof_active",
            "shm_unmap_error",
            "shm_unmap_delete_forwarded",
        }
        missing = required.difference(counters)
        if missing:
            missing_names = ", ".join(sorted(missing))
            raise TrustedLiveCleanupError(
                f"The native VFS omitted required cleanup counters: {missing_names}."
            )
        failures: list[str] = []
        if counters["proof_active"] != 0:
            failures.append(f"proof_active={counters['proof_active']}")
        if counters["proof_open"] != counters["proof_close"]:
            failures.append(
                f"proof_open={counters['proof_open']} but "
                f"proof_close={counters['proof_close']}"
            )
        for counter_name in (
            "base_close_error",
            "proof_close_error",
            "shm_unmap_error",
            "shm_unmap_delete_forwarded",
        ):
            if counters[counter_name] != 0:
                failures.append(f"{counter_name}={counters[counter_name]}")
        if failures:
            raise TrustedLiveCleanupError(
                "The native VFS did not release trusted proof/SHM state: "
                + ", ".join(failures)
                + "."
            )

    def _close_resources(self, *, force: bool) -> TrustedLiveCleanupError | None:
        if self._resource_cleanup_complete:
            cleanup_error = self._resource_cleanup_error
            if cleanup_error is not None and not force:
                raise cleanup_error
            return cleanup_error

        errors: list[BaseException] = []
        if self._preflight_cleanup_error is not None:
            errors.append(self._preflight_cleanup_error)
        native_release_confirmed = (
            self._native_release_confirmed or not self._native_extension_loaded
        )
        try:
            connection, self._connection = self._connection, None
            if connection is not None:
                try:
                    connection.close(False)
                except BaseException as error:
                    errors.append(error)
                    try:
                        connection.close(True)
                    except BaseException as force_close_error:
                        errors.append(force_close_error)

            bootstrap, self._bootstrap = self._bootstrap, None
            if bootstrap is not None:
                final_audit_captured = False
                final_audit_verified = False
                final_audit_error: BaseException | None = None
                if self._native_session_attempted:
                    try:
                        final_audit = self._read_native_audit(bootstrap)
                        self._final_audit = final_audit
                        final_audit_captured = True
                        self._verify_final_audit(final_audit)
                        final_audit_verified = True
                    except BaseException as error:
                        # xOpen can reject input before the native session is
                        # configured.  Defer this error until release reports
                        # whether a matching session actually existed.
                        final_audit_error = error
                if self._native_extension_loaded:
                    cursor: apsw.Cursor | None = None
                    release_result: int | None = None
                    release_error: BaseException | None = None
                    try:
                        cursor = bootstrap.cursor()
                        row = cursor.execute(
                            "SELECT qplot_trusted_vfs_release(?)",
                            (self._token_string,),
                        ).fetchone()
                        if (
                            row is None
                            or len(row) != 1
                            or type(row[0]) is not int
                            or row[0] not in {0, 1}
                        ):
                            raise TrustedLiveCleanupError(
                                "The native VFS returned an invalid release result."
                            )
                        release_result = row[0]
                    except BaseException as error:
                        release_error = error
                    finally:
                        if cursor is not None:
                            try:
                                cursor.close()
                            except BaseException as error:
                                errors.append(error)
                    if release_error is not None:
                        if final_audit_error is not None:
                            errors.append(final_audit_error)
                        errors.append(release_error)
                    elif release_result == 0:
                        if final_audit_captured:
                            errors.append(
                                TrustedLiveCleanupError(
                                    "The native VFS reported no session after "
                                    "returning its audit."
                                )
                            )
                        else:
                            # A zero result authoritatively proves that xOpen
                            # rejected the request before configuring native
                            # state.  Its expected unknown-token audit error is
                            # therefore not cleanup uncertainty.
                            native_release_confirmed = True
                            self._native_release_confirmed = True
                    elif release_result == 1:
                        native_release_confirmed = True
                        self._native_release_confirmed = True
                        if not self._native_session_attempted:
                            errors.append(
                                TrustedLiveCleanupError(
                                    "The native VFS released an unexpected session."
                                )
                            )
                        elif final_audit_error is not None:
                            errors.append(final_audit_error)
                        elif not final_audit_verified:
                            errors.append(
                                TrustedLiveCleanupError(
                                    "The native VFS session was released without "
                                    "a verified final audit."
                                )
                            )
                try:
                    bootstrap.close(True)
                except BaseException as error:
                    errors.append(error)

            temporary_directory, self._temporary_directory = (
                self._temporary_directory,
                None,
            )
            self._native_temporary_path = None
            if temporary_directory is not None:
                try:
                    temporary_directory.cleanup()
                except BaseException as error:
                    errors.append(error)
        finally:
            if not native_release_confirmed and not errors:
                errors.append(
                    TrustedLiveCleanupError(
                        "The native trusted VFS release could not be confirmed."
                    )
                )

        if errors:
            cleanup_error = TrustedLiveCleanupError(
                "The trusted reader did not close cleanly; cleanup could not be "
                f"proved and this process is quarantined: {errors[0]}"
            )
            for additional_error in errors[1:]:
                cleanup_error.add_note(
                    f"Additional close failure: {additional_error!r}"
                )
            _quarantine_process_session(
                self._process_session_owner,
                cleanup_error,
            )
            self._resource_cleanup_error = cleanup_error
            self._resource_cleanup_complete = True
            if force:
                return cleanup_error
            raise cleanup_error
        _release_process_session(self._process_session_owner)
        self._native_release_confirmed = native_release_confirmed
        self._resource_cleanup_complete = True
        return None

    def close(self) -> None:
        """Close the SQLite handles and remove the private temp directory."""
        self._check_thread()
        if self._closed:
            return
        with self._operation_mutex:
            if self._active_operation is not None:
                raise TrustedLiveTransactionError(
                    "Interrupt and finish the active operation before closing "
                    "its trusted reader."
                )
        self._closed = True
        self._close_resources(force=False)

    def __enter__(self) -> TrustedLiveReader:
        self._require_open()
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exception_type, exception, traceback
        self.close()

    def __del__(self) -> None:
        """Best-effort release if an owner abandons the reader without closing."""
        try:
            if getattr(self, "_closed", True):
                return
            self._closed = True
            operation_mutex = getattr(self, "_operation_mutex", None)
            if operation_mutex is not None:
                with operation_mutex:
                    self._active_operation = None
            self._close_resources(force=True)
        except BaseException:
            # Finalizers may run during interpreter teardown or after a partial
            # constructor failure.  They must never make that teardown noisy.
            pass
