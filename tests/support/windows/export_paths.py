"""Shared fixtures and assertions for export paths."""

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from qplot.datahandling.file_identity import (
    SQLITE_SIDECAR_SUFFIXES,
    database_file_identity,
)

__all__ = [
    '_database_artifact_state',
    'windows_stat_timestamps',
]


_DATABASE_ARTIFACT_SUFFIXES = ("", *SQLITE_SIDECAR_SUFFIXES)


@pytest.fixture
def windows_stat_timestamps(monkeypatch):
    """Model Windows' distinct path CreationTime and descriptor ChangeTime."""
    if os.name != "nt":
        pytest.skip("Windows stat timestamp semantics")
    real_fstat = os.fstat

    def descriptor_stat(fd):
        observed = real_fstat(fd)
        fields = {name: getattr(observed, name) for name in dir(observed)
                  if name.startswith("st_")}
        fields["st_ctime_ns"] += 1_000_000_000
        return SimpleNamespace(**fields)

    monkeypatch.setattr(os, "fstat", descriptor_stat)


def _path_artifact_state(path: Path):
    """Capture an entry without losing final-link or platform identity details."""
    try:
        file_stat = path.lstat()
    except FileNotFoundError:
        return None

    is_regular = stat.S_ISREG(file_stat.st_mode)
    return {
        "lstat_identity": (int(file_stat.st_dev), int(file_stat.st_ino)),
        "database_identity": database_file_identity(path),
        "mode": int(file_stat.st_mode),
        "nlink": int(file_stat.st_nlink),
        "size": int(file_stat.st_size),
        "mtime_ns": int(file_stat.st_mtime_ns),
        "bytes": path.read_bytes() if is_regular else None,
        "link_target": os.readlink(path) if stat.S_ISLNK(file_stat.st_mode) else None,
    }


def _database_artifact_state(database_path: Path):
    """Capture bytes, metadata, identities, and absence for every DB artifact."""
    return {
        suffix: _path_artifact_state(Path(f"{database_path}{suffix}"))
        for suffix in _DATABASE_ARTIFACT_SUFFIXES
    }
