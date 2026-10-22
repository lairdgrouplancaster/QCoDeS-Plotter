"""Observe reader artifacts without opening SQLite's mutable shared memory."""

import os
from pathlib import Path

import pytest

from tests.windows.test_heatmap_layer_integration import (
    _assert_reader_artifact_invariant,
    _database_artifact_state,
)


@pytest.fixture
def database_artifacts(tmp_path):
    # These are disposable byte fixtures, never a user's measurement database.
    database = tmp_path / "artifacts.db"
    for suffix in ("", "-wal", "-journal", "-shm"):
        Path(f"{database}{suffix}").write_bytes(b"original")
    return database


def test_locked_shm_does_not_interrupt_writer_baseline(database_artifacts, monkeypatch):
    database = database_artifacts
    shm = Path(f"{database}-shm")
    read_bytes = Path.read_bytes

    def read_protected_only(path):
        if path == shm:
            raise PermissionError("SQLite's Windows SHM sharing/locking mode")
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", read_protected_only)
    before = _database_artifact_state(database)
    _assert_reader_artifact_invariant(database, before)

    # An explicit fixture writer advances the WAL. The following observation
    # must succeed, rather than leave the pre-append baseline in the caller.
    Path(f"{database}-wal").write_bytes(b"writer committed additional rows")
    after_writer = _database_artifact_state(database)
    assert after_writer["-wal"] != before["-wal"]
    _assert_reader_artifact_invariant(database, after_writer)
    with pytest.raises(AssertionError):
        _assert_reader_artifact_invariant(database, before)


@pytest.mark.parametrize("suffix", ["", "-wal", "-journal"])
def test_protected_content_is_still_checked(database_artifacts, suffix):
    before = _database_artifact_state(database_artifacts)
    path = Path(f"{database_artifacts}{suffix}")
    original = path.stat()
    path.write_bytes(b"replaced")
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert path.stat().st_size == original.st_size
    with pytest.raises(AssertionError):
        _assert_reader_artifact_invariant(database_artifacts, before)


@pytest.mark.parametrize("suffix", ["", "-wal", "-journal"])
def test_protected_read_denial_is_not_ignored(database_artifacts, monkeypatch, suffix):
    denied = Path(f"{database_artifacts}{suffix}")
    read_bytes = Path.read_bytes

    def read_except_protected(path):
        if path == denied:
            raise PermissionError("Protected artifact cannot be verified")
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", read_except_protected)
    with pytest.raises(PermissionError, match="Protected artifact"):
        _database_artifact_state(database_artifacts)


def test_shm_creation_and_in_place_growth_are_permitted(database_artifacts):
    shm = Path(f"{database_artifacts}-shm")
    shm.unlink()
    without_shm = _database_artifact_state(database_artifacts)
    shm.write_bytes(b"initial shared memory")
    _assert_reader_artifact_invariant(database_artifacts, without_shm)
    with_shm = _database_artifact_state(database_artifacts)
    shm.write_bytes(b"updated shared memory" * 10)
    _assert_reader_artifact_invariant(database_artifacts, with_shm)


@pytest.mark.parametrize("action", ["remove", "replace"])
def test_existing_shm_cannot_disappear_or_be_replaced(database_artifacts, action):
    shm = Path(f"{database_artifacts}-shm")
    before = _database_artifact_state(database_artifacts)
    # Keep the original inode alive so the replacement cannot reuse it.
    shm.rename(shm.with_name("detached-shm"))
    if action == "replace":
        shm.write_bytes(b"original")
    with pytest.raises(AssertionError):
        _assert_reader_artifact_invariant(database_artifacts, before)


def test_shm_symlink_is_rejected(database_artifacts):
    shm = Path(f"{database_artifacts}-shm")
    before = _database_artifact_state(database_artifacts)
    target = shm.with_name("symlink-target")
    shm.rename(target)
    try:
        shm.symlink_to(target.name)
    except OSError as exc:
        pytest.skip(f"Symlink creation unavailable: {exc}")
    with pytest.raises(AssertionError):
        _assert_reader_artifact_invariant(database_artifacts, before)
