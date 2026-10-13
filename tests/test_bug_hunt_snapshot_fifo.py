"""Snapshot inspection never opens nonregular source artifacts as streams."""

import hashlib
import os
import subprocess
import sys

import pytest
from qcodes.dataset import initialise_or_create_database_at


def _entry_state(directory):
    return {
        entry.name: tuple(getattr(entry.lstat(), field) for field in (
            "st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns",
        ))
        for entry in directory.iterdir()
    }


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO required")
@pytest.mark.parametrize("operation", ["probe", "snapshot"])
@pytest.mark.parametrize("suffix", ["", "-wal", "-journal", "-shm"])
@pytest.mark.parametrize("symlink", [False, True])
def test_snapshot_fifo_artifacts_are_bounded_and_unchanged(tmp_path, operation, suffix, symlink):
    database = tmp_path / "source.db"
    if suffix:
        initialise_or_create_database_at(str(database), journal_mode="DELETE")
    artifact = tmp_path / f"source.db{suffix}"
    fifo = tmp_path / "pipe" if symlink else artifact
    os.mkfifo(fifo)
    if symlink:
        artifact.symlink_to(fifo)
    entries = _entry_state(tmp_path)
    before = hashlib.sha256(database.read_bytes()).digest() if suffix else None

    # The internal deadline cannot interrupt a blocked FIFO open; the parent
    # timeout makes that regression fail without hanging the test process.
    result = subprocess.run(
        [sys.executable, "-c", """
import sys, time
from qplot.datahandling.readonly import (
    ReadOnlyDatabaseAccessError, probe_read_only_database, sqlite_read_only_connection,
)
path, operation, suffix = sys.argv[1:]
try:
    if operation == 'probe':
        probe_read_only_database(path, deadline=time.monotonic() + 1.)
    else:
        connection = sqlite_read_only_connection(path, deadline=time.monotonic() + 1.)
        try:
            assert connection.execute('SELECT count(*) FROM sqlite_master').fetchone()[0] > 0
        finally:
            connection.close()
except (FileNotFoundError, ReadOnlyDatabaseAccessError):
    assert suffix != '-shm', 'Snapshots do not need to open or copy SHM'
else:
    assert suffix == '-shm', 'A nonregular main/WAL/journal was accepted'
""", str(database), operation, suffix],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _entry_state(tmp_path) == entries
    if suffix:
        assert hashlib.sha256(database.read_bytes()).digest() == before
    assert fifo.is_fifo()
    assert artifact.is_symlink() == symlink
