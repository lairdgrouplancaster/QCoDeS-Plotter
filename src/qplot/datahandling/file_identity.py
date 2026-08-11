"""Stable paths and identities for databases that may be replaced."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

DatabaseFileIdentity: TypeAlias = (
    tuple[int, int]
    | tuple[str, int, int]
    | tuple[str, str, int]
)
DATABASE_PUBLICATION_GUARD_SUFFIX = ".qplot-publishing"
QPLOT_GENERATED_DATABASE_APPLICATION_ID = 0x51504C54
_SQLITE_APPLICATION_ID_OFFSET = 68


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

    return Path(f"{logical_database_path(database_path)}{DATABASE_PUBLICATION_GUARD_SUFFIX}")


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
    )


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


def _windows_file_identity(canonical_path: str) -> DatabaseFileIdentity | None:
    """Return Windows' volume/file-index identity without opening for writes."""

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

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
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
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ByHandleFileInformation),
    ]
    get_information.restype = wintypes.BOOL
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
        information = ByHandleFileInformation()
        if not get_information(handle, ctypes.byref(information)):
            return None
        file_index = (
            int(information.file_index_high) << 32
            | int(information.file_index_low)
        )
        if file_index == 0:
            return None
        return (
            "windows-file-id",
            int(information.volume_serial_number),
            file_index,
        )
    finally:
        close_handle(handle)
