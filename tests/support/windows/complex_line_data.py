"""Shared fixtures and assertions for complex line data."""

import hashlib

__all__ = [
    'database_state',
]


def database_state(path):
    return {
        suffix: (hashlib.sha256(artifact.read_bytes()).digest(), artifact.stat().st_mtime_ns)
        if artifact.exists() else None
        for suffix in ("", "-wal", "-journal")
        for artifact in (path.with_name(path.name + suffix),)
    }
