"""Lightweight spawn targets for read-only source-artifact audits.

Keep this module limited to the standard library so each audit process avoids
importing QCoDeS and the test suite. Audits must stay in a separate process:
closing source-file descriptors in the reader process can release POSIX locks.
"""

from __future__ import annotations

import hashlib
import os
import stat
import traceback
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

_ARTIFACT_SUFFIXES = ("", "-wal", "-shm", "-journal")


def _file_descriptor_digest(file_descriptor: int) -> str:
    """Hash the exact object already bound to ``file_descriptor``."""

    digest = hashlib.sha256()
    while chunk := os.read(file_descriptor, 1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def _artifact_state_in_audit_process(
    database_path: str,
    control: Connection,
) -> None:
    """Capture source state without touching reader-process POSIX descriptors."""
    state: dict[str, tuple[Any, ...] | None] = {}
    try:
        for suffix in _ARTIFACT_SUFFIXES:
            artifact = Path(f"{database_path}{suffix}")
            open_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
            open_flags |= getattr(os, "O_CLOEXEC", 0)
            open_flags |= getattr(os, "O_NOINHERIT", 0)
            open_flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                file_descriptor = os.open(artifact, open_flags)
            except FileNotFoundError:
                state[suffix] = None
                continue
            try:
                status = os.fstat(file_descriptor)
                digest = (
                    _file_descriptor_digest(file_descriptor)
                    if stat.S_ISREG(status.st_mode)
                    else None
                )
                state[suffix] = (
                    digest,
                    status.st_dev,
                    status.st_ino,
                    status.st_mode,
                    status.st_nlink,
                    status.st_uid,
                    status.st_gid,
                    status.st_size,
                    status.st_mtime_ns,
                    status.st_ctime_ns,
                )
            finally:
                os.close(file_descriptor)
        control.send(("ok", state))
    except BaseException:
        control.send(("error", traceback.format_exc()))
    finally:
        control.close()


def _path_stat_in_audit_process(file_path: str, control: Connection) -> None:
    try:
        try:
            status = os.stat(file_path, follow_symlinks=False)
        except FileNotFoundError:
            status = None
        control.send(("ok", status))
    except BaseException:
        control.send(("error", traceback.format_exc()))
    finally:
        control.close()
