"""Stable identities for database files that may be atomically replaced."""

import os
from typing import TypeAlias

DatabaseFileIdentity: TypeAlias = tuple[int, int] | tuple[str, str, int]


def canonical_database_path(database_path: str | os.PathLike[str]) -> str:
    """Return a stable, platform-normalised identity for a database file."""
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(database_path))))


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
