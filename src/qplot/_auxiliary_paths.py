"""Keep settings and diagnostics away from existing SQLite artifacts."""

import os
import stat

_SQLITE_HEADER = b"SQLite format 3\x00"
_SQLITE_HEADERS = (
    _SQLITE_HEADER,
    b"\x37\x7f\x06\x82",  # WAL, little-endian checksums
    b"\x37\x7f\x06\x83",  # WAL, big-endian checksums
    b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7",  # rollback journal
)
_SIDECAR_SUFFIXES = ("-wal", "-journal", "-shm")


def _signature(file_stat):
    return tuple(int(getattr(file_stat, field)) for field in (
        "st_dev", "st_ino", "st_mode", "st_nlink", "st_size",
        "st_mtime_ns", "st_ctime_ns",
    ))


def _read_header(filename):
    """Inspect a bounded regular file without SQLite or writable handles."""
    try:
        before = os.lstat(filename)
    except FileNotFoundError:
        return b""
    if not stat.S_ISREG(before.st_mode):
        raise OSError("The auxiliary destination is not a regular file.")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(filename, flags)
    try:
        opened = os.fstat(descriptor)
        # Windows lstat and fstat expose different ctime definitions.
        common_fields = 6 if os.name == "nt" else 7
        if _signature(before)[:common_fields] != _signature(opened)[:common_fields]:
            raise OSError("The auxiliary destination changed during inspection.")
        header = os.read(descriptor, len(_SQLITE_HEADER))
        if _signature(os.fstat(descriptor)) != _signature(opened):
            raise OSError("The auxiliary destination changed during inspection.")
    finally:
        os.close(descriptor)
    if _signature(os.lstat(filename)) != _signature(before):
        raise OSError("The auxiliary destination changed during inspection.")
    return header


def ensure_safe_auxiliary_path(filename):
    """Reject database files and reserved sidecars before any auxiliary write.

    These fixed application paths can contain measurement data even when the
    database has never been selected in qPlot. Unreadable files fail closed.
    """
    filename = os.path.abspath(os.fspath(filename))
    if _read_header(filename).startswith(_SQLITE_HEADERS):
        raise OSError("The auxiliary destination contains a SQLite database or journal.")
    for suffix in _SIDECAR_SUFFIXES:
        if filename.casefold().endswith(suffix):
            if _read_header(filename[:-len(suffix)]).startswith(_SQLITE_HEADER):
                raise OSError("The auxiliary destination is reserved for a SQLite sidecar.")
