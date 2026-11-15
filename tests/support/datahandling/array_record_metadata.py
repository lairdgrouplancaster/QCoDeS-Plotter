"""Shared fixtures and assertions for array record metadata."""

import builtins
import hashlib
import io
import os
import sqlite3
from pathlib import Path
from urllib.parse import unquote, urlparse

__all__ = [
    '_artifact_state',
    '_protect_source',
]


def _protect_source(monkeypatch, path):
    """Reject source writes before the viewer can issue them."""
    protected = {os.path.normcase(str(path.resolve()) + suffix)
                 for suffix in ("", "-wal", "-shm", "-journal")}

    def is_protected(value):
        if isinstance(value, int):
            return False
        return os.path.normcase(os.path.abspath(os.fsdecode(value))) in protected

    def guard_open(original):
        def guarded(file, mode="r", *args, **kwargs):
            assert not (is_protected(file) and any(flag in mode for flag in "wax+"))
            return original(file, mode, *args, **kwargs)
        return guarded

    monkeypatch.setattr(builtins, "open", guard_open(builtins.open))
    monkeypatch.setattr(io, "open", guard_open(io.open))
    original_open = os.open

    def guarded_os_open(file, flags, *args, **kwargs):
        assert not (is_protected(file) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
        return original_open(file, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded_os_open)
    for name in ("remove", "unlink", "rename", "replace", "truncate"):
        original = getattr(os, name)

        def guarded_mutation(path, *args, _original=original, _name=name, **kwargs):
            assert not is_protected(path), f"{_name} attempted on source"
            if _name in ("rename", "replace"):
                assert not is_protected(args[0]), f"{_name} attempted onto source"
            return _original(path, *args, **kwargs)

        monkeypatch.setattr(os, name, guarded_mutation)
    original_connect = sqlite3.connect

    def guarded_connect(database, *args, **kwargs):
        spelling = os.fspath(database)
        if spelling.startswith("file:"):
            spelling = unquote(urlparse(spelling).path).lstrip("/") if os.name == "nt" else unquote(urlparse(spelling).path)
        assert not is_protected(spelling), "SQLite must read only a private snapshot"
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", guarded_connect)


def _artifact_state(path, *, include_digest=True):
    result = {}
    for suffix in ("", "-wal", "-shm", "-journal"):
        artifact = Path(str(path) + suffix)
        if artifact.exists():
            stat = artifact.stat()
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest() if include_digest else None
            result[suffix] = (digest, stat.st_size, stat.st_mtime_ns)
    return result
