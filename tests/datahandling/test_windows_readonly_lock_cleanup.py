"""Exercise Windows lock cleanup with real read-only files on every platform."""

import builtins
import ctypes
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from qplot.datahandling import readonly


class InjectedFailure(RuntimeError):
    pass


@pytest.mark.parametrize(
    "failure", ["dll", "handle", "first_lock", "second_lock", "body", "unlock", None],
)
def test_windows_lock_check_always_closes_readonly_handle(tmp_path, monkeypatch, failure):
    database = tmp_path / "source.db"
    writer = sqlite3.connect(database)
    try:
        writer.execute("CREATE TABLE measurement (value INTEGER)")
        writer.commit()
    finally:
        writer.close()
    original_bytes = database.read_bytes()
    original_mtime = database.stat().st_mtime_ns
    handles = []
    locked = []
    unlocked = []

    def open_readonly(path, mode):
        assert path == database
        assert mode == "rb"
        handle = builtins.open(path, mode)
        handles.append(handle)
        return handle

    def lock_file(handle, flags, reserved, low, high, overlapped):
        if failure == ("first_lock" if not locked else "second_lock"):
            return False
        locked.append(overlapped._obj.Offset)
        return True

    def unlock_file(handle, reserved, low, high, overlapped):
        unlocked.append(overlapped._obj.Offset)
        if failure == "unlock":
            raise InjectedFailure("unlock")
        return True

    def load_dll(*args, **kwargs):
        if failure == "dll":
            raise InjectedFailure("dll")
        return SimpleNamespace(LockFileEx=lock_file, UnlockFileEx=unlock_file)

    def get_osfhandle(descriptor):
        if failure == "handle":
            raise InjectedFailure("handle")
        return descriptor

    monkeypatch.setattr(readonly, "open", open_readonly, raising=False)
    monkeypatch.setattr(ctypes, "WinDLL", load_dll, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 33, raising=False)
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(get_osfhandle=get_osfhandle))

    def check():
        with readonly._windows_sqlite_shared_lock_bytes(database) as handle:
            assert not handle.closed
            assert handle.read(16) == b"SQLite format 3\x00"
            if failure == "body":
                raise InjectedFailure("body")

    if failure in {"first_lock", "second_lock"}:
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            check()
    elif failure is not None:
        with pytest.raises(InjectedFailure, match=failure):
            check()
    else:
        check()

    assert len(handles) == 1
    assert handles[0].closed
    if failure != "unlock":
        assert unlocked == list(reversed(locked))
    assert database.read_bytes() == original_bytes
    assert database.stat().st_mtime_ns == original_mtime
    assert sorted(path.name for path in tmp_path.iterdir()) == [database.name]
