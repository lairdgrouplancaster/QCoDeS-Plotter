"""Stable paths and identities for databases that may be replaced."""

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeAlias

DatabaseFileIdentity: TypeAlias = (
    tuple[int, int] | tuple[str, int, int] | tuple[str, str, int]
)


class FileIdentityHandleCloseError(OSError):
    """Raised when a trusted Windows identity handle cannot be closed."""


DATABASE_PUBLICATION_GUARD_SUFFIX = ".qplot-publishing"
QPLOT_GENERATED_DATABASE_APPLICATION_ID = 0x51504C54
QPLOT_GENERATION_PROVENANCE_TABLE = "qplot_generation_provenance"
QPLOT_GENERATION_PROVENANCE_TOKEN_BYTES = 32
QPLOT_GENERATION_LINEAGE_STATE_TABLE = "qplot_generation_lineage_state"
QPLOT_GENERATION_LINEAGE_RING_TABLE = "qplot_generation_lineage_ring"
QPLOT_GENERATION_LINEAGE_FORMAT_VERSION = 2
QPLOT_GENERATION_LINEAGE_NONCE_BYTES = 32
QPLOT_GENERATION_LINEAGE_WINDOW = 65_536
QPLOT_GENERATION_PROVENANCE_TRIGGER_PREFIX = "qplot_provenance_v2_"
_SQLITE_APPLICATION_ID_OFFSET = 68
SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
MAX_DATABASE_SIDECAR_IDENTITIES = 2 * len(SQLITE_SIDECAR_SUFFIXES)


def _quote_sqlite_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def generation_lineage_transition_statements() -> tuple[str, str, str]:
    """Return the statements for one bounded, nonce-linked lineage event."""
    provenance = _quote_sqlite_identifier(QPLOT_GENERATION_PROVENANCE_TABLE)
    state = _quote_sqlite_identifier(QPLOT_GENERATION_LINEAGE_STATE_TABLE)
    ring = _quote_sqlite_identifier(QPLOT_GENERATION_LINEAGE_RING_TABLE)
    insert_event = (
        f"INSERT OR REPLACE INTO {ring} "
        "(slot, sequence, parent_nonce, nonce) "
        f"SELECT (head_sequence + 1) % {QPLOT_GENERATION_LINEAGE_WINDOW}, "
        f"head_sequence + 1, head_nonce, "
        f"randomblob({QPLOT_GENERATION_LINEAGE_NONCE_BYTES}) "
        f"FROM {state} WHERE singleton = 1"
    )
    update_head = (
        f"UPDATE {state} SET "
        "head_sequence = head_sequence + 1, "
        f"head_nonce = (SELECT nonce FROM {ring} "
        f"WHERE sequence = {state}.head_sequence + 1) "
        "WHERE singleton = 1"
    )
    update_legacy_epoch = (
        f"UPDATE {provenance} SET write_epoch = "
        f"(SELECT head_sequence FROM {state} WHERE singleton = 1) "
        "WHERE singleton = 1"
    )
    return insert_event, update_head, update_legacy_epoch


def generation_provenance_trigger(
    table_name: str,
    operation: str,
) -> tuple[str, str]:
    """Return the stable name and exact SQL for one lineage trigger."""
    table_digest = hashlib.sha256(
        table_name.encode("utf-8", errors="surrogatepass")
    ).hexdigest()
    trigger_name = (
        f"{QPLOT_GENERATION_PROVENANCE_TRIGGER_PREFIX}"
        f"{table_digest}_{operation.lower()}"
    )
    statements = "; ".join(generation_lineage_transition_statements())
    statement = (
        f"CREATE TRIGGER {_quote_sqlite_identifier(trigger_name)} "
        f"AFTER {operation} ON {_quote_sqlite_identifier(table_name)} "
        f"BEGIN {statements}; END"
    )
    return trigger_name, statement


def logical_database_path(database_path: str | os.PathLike[str]) -> str:
    """Return the normalised path selected by the user without resolving links."""

    return os.path.normcase(os.path.abspath(os.fspath(database_path)))


def canonical_database_path(database_path: str | os.PathLike[str]) -> str:
    """Return the currently resolved, platform-normalised database path."""
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(database_path))))


def database_publication_guard_path(
    database_path: str | os.PathLike[str],
) -> Path:
    """Return qPlot's path-local guard for an in-flight main-file replacement."""

    return Path(
        f"{logical_database_path(database_path)}{DATABASE_PUBLICATION_GUARD_SUFFIX}"
    )


def database_has_qplot_generation_marker(
    database_path: str | os.PathLike[str],
) -> bool:
    """Return whether the main header marks a qPlot-generated database."""
    with open(database_path, "rb") as database_file:
        database_file.seek(_SQLITE_APPLICATION_ID_OFFSET)
        application_id = database_file.read(4)
    return application_id == QPLOT_GENERATED_DATABASE_APPLICATION_ID.to_bytes(
        4,
        "big",
    )


@dataclass(frozen=True, slots=True)
class DatabaseInstance:
    """One database instance accepted through a stable logical path."""

    logical_path: str
    resolved_path: str
    identity: DatabaseFileIdentity | None
    sidecar_identities: frozenset[DatabaseFileIdentity] = field(
        default_factory=frozenset,
        compare=False,
        hash=False,
        repr=False,
    )


def database_instance(
    database_path: str | os.PathLike[str],
) -> DatabaseInstance:
    """Capture the logical path, its resolved target, and that target's identity."""

    logical_path = logical_database_path(database_path)
    resolved_path = canonical_database_path(logical_path)
    return DatabaseInstance(
        logical_path=logical_path,
        resolved_path=resolved_path,
        identity=database_file_identity(resolved_path),
        sidecar_identities=database_sidecar_identities(
            logical_path,
            resolved_path,
        ),
    )


def database_sidecar_identities(
    database_path: str | os.PathLike[str],
    resolved_database_path: str | os.PathLike[str] | None = None,
) -> frozenset[DatabaseFileIdentity]:
    """Capture existing SQLite sidecars without opening the database.

    Both the user-selected path and its resolved target are checked because
    SQLite sidecars may be associated with either spelling when the input path
    contains symbolic links. Only filesystem metadata is inspected.
    """

    logical_path = logical_database_path(database_path)
    resolved_path = canonical_database_path(
        resolved_database_path if resolved_database_path is not None else logical_path
    )
    identities: set[DatabaseFileIdentity] = set()
    for base_path in {logical_path, resolved_path}:
        for suffix in SQLITE_SIDECAR_SUFFIXES:
            identity = database_file_identity(f"{base_path}{suffix}")
            if identity is not None:
                identities.add(identity)
    return frozenset(identities)


def database_instances_differ(
    first: DatabaseInstance | None,
    second: DatabaseInstance | None,
) -> bool:
    """Return whether two observations prove that the file instance changed."""

    if first is None or second is None:
        return first is not second
    if first.logical_path != second.logical_path:
        return True
    if first.resolved_path != second.resolved_path:
        return True
    if first.identity is None or second.identity is None:
        return first.identity != second.identity
    return first.identity != second.identity


def database_file_identity(
    database_path: str | os.PathLike[str],
) -> DatabaseFileIdentity | None:
    """Return an identity that changes only when the file instance changes.

    Device and inode are the preferred cross-platform identity. Some Windows
    and network filesystems do not expose a useful inode, so a creation-time
    identity is used only when the platform exposes one. Size, modification
    time, and POSIX change time are deliberately excluded because normal
    database and WAL activity can change them without replacing the file.
    """

    canonical_path = canonical_database_path(database_path)
    try:
        stat_result = os.stat(canonical_path)
    except OSError:
        return None

    inode = int(getattr(stat_result, "st_ino", 0) or 0)
    if inode:
        device = int(getattr(stat_result, "st_dev", 0) or 0)
        return (device, inode)

    if os.name == "nt":
        windows_identity = _windows_file_identity(canonical_path)
        if windows_identity is not None:
            return windows_identity

    birthtime_ns = getattr(stat_result, "st_birthtime_ns", None)
    if birthtime_ns is None:
        birthtime = getattr(stat_result, "st_birthtime", None)
        if birthtime is not None:
            birthtime_ns = round(float(birthtime) * 1_000_000_000)
    if birthtime_ns is None and os.name == "nt":
        # On supported Python versions Windows st_ctime is file creation time,
        # unlike POSIX st_ctime. Prefer st_birthtime above when it is available.
        birthtime_ns = getattr(stat_result, "st_ctime_ns", None)
    if birthtime_ns is None:
        return None
    return ("birthtime", canonical_path, int(birthtime_ns))


def path_bound_file_identity(
    database_path: str | os.PathLike[str],
) -> DatabaseFileIdentity | None:
    """Return an identity comparable with an already-open file descriptor."""

    canonical_path = canonical_database_path(database_path)
    if os.name == "nt":
        windows_identity = _windows_file_identity(canonical_path)
        if windows_identity is not None:
            return windows_identity
    return database_file_identity(canonical_path)


def checked_path_bound_file_identity(
    database_path: str | os.PathLike[str],
) -> DatabaseFileIdentity | None:
    """Return a path-bound identity and prove temporary handle cleanup.

    The ordinary identity helpers deliberately remain best-effort because UI
    refresh and export code treats an unavailable identity as a normal race.
    The trusted live-reader boundary has a stronger lifecycle requirement: on
    Windows it must surface both identity-inspection failures and an uncertain
    ``CloseHandle`` result so the process can quarantine that reader session.
    """

    canonical_path = canonical_database_path(database_path)
    if os.name == "nt":
        return _checked_windows_file_identity(canonical_path)
    return path_bound_file_identity(canonical_path)


def open_file_identity(file_descriptor: int) -> DatabaseFileIdentity | None:
    """Return the stable identity of the exact file held by a descriptor."""

    if os.name == "nt":
        try:
            import msvcrt

            get_osfhandle = getattr(msvcrt, "get_osfhandle", None)
            if get_osfhandle is None:
                return None
            handle = get_osfhandle(file_descriptor)
        except (ImportError, OSError):
            return None
        return _windows_handle_identity(handle)

    try:
        stat_result = os.fstat(file_descriptor)
    except OSError:
        return None
    inode = int(getattr(stat_result, "st_ino", 0) or 0)
    if not inode:
        return None
    device = int(getattr(stat_result, "st_dev", 0) or 0)
    return (device, inode)


def _windows_file_identity(canonical_path: str) -> DatabaseFileIdentity | None:
    """Return Windows' volume/file-index identity without opening for writes."""

    try:
        import ctypes
        from ctypes import wintypes
    except (ImportError, AttributeError):
        return None

    win_dll = getattr(ctypes, "WinDLL", None)
    if win_dll is None:
        return None
    kernel32 = win_dll("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    file_read_attributes = 0x0080
    share_read_write_delete = 0x0001 | 0x0002 | 0x0004
    open_existing = 3
    handle = create_file(
        canonical_path,
        file_read_attributes,
        share_read_write_delete,
        None,
        open_existing,
        0,
        None,
    )
    invalid_handle = wintypes.HANDLE(-1).value
    if handle in (None, invalid_handle):
        return None
    try:
        return _windows_handle_identity(handle)
    finally:
        close_handle(handle)


def _checked_windows_file_identity(
    canonical_path: str,
) -> DatabaseFileIdentity | None:
    """Return Windows identity while treating every HANDLE close as fallible."""

    try:
        import ctypes
        from ctypes import wintypes
    except (ImportError, AttributeError):
        return None

    win_dll = getattr(ctypes, "WinDLL", None)
    get_last_error = getattr(ctypes, "get_last_error", None)
    if win_dll is None or get_last_error is None:
        return None
    kernel32 = win_dll("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    file_read_attributes = 0x0080
    share_read_write_delete = 0x0001 | 0x0002 | 0x0004
    open_existing = 3
    handle = create_file(
        canonical_path,
        file_read_attributes,
        share_read_write_delete,
        None,
        open_existing,
        0,
        None,
    )
    invalid_handle = wintypes.HANDLE(-1).value
    if handle in (None, invalid_handle):
        error_code = get_last_error()
        raise OSError(
            error_code,
            f"CreateFileW could not inspect {canonical_path}",
        )

    identity: DatabaseFileIdentity | None = None
    identity_error_code = 0
    try:
        identity = _windows_handle_identity(handle)
        if identity is None:
            identity_error_code = get_last_error()
    finally:
        if not close_handle(handle):
            error_code = get_last_error()
            raise FileIdentityHandleCloseError(
                error_code,
                f"CloseHandle could not release the identity handle for "
                f"{canonical_path}",
            )

    if identity is None:
        raise OSError(
            identity_error_code,
            f"GetFileInformationByHandle could not identify {canonical_path}",
        )
    return identity


def _windows_handle_identity(handle) -> DatabaseFileIdentity | None:
    """Return Windows' volume/file-index identity for an existing handle."""

    try:
        import ctypes
        from ctypes import wintypes
    except (ImportError, AttributeError):
        return None

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("file_attributes", wintypes.DWORD),
            ("creation_time", wintypes.FILETIME),
            ("last_access_time", wintypes.FILETIME),
            ("last_write_time", wintypes.FILETIME),
            ("volume_serial_number", wintypes.DWORD),
            ("file_size_high", wintypes.DWORD),
            ("file_size_low", wintypes.DWORD),
            ("number_of_links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        ]

    win_dll = getattr(ctypes, "WinDLL", None)
    if win_dll is None:
        return None
    kernel32 = win_dll("kernel32", use_last_error=True)
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ByHandleFileInformation),
    ]
    get_information.restype = wintypes.BOOL
    information = ByHandleFileInformation()
    if not get_information(handle, ctypes.byref(information)):
        return None
    file_index = int(information.file_index_high) << 32 | int(
        information.file_index_low
    )
    if file_index == 0:
        return None
    return (
        "windows-file-id",
        int(information.volume_serial_number),
        file_index,
    )
