"""Windows must extract qPlot's own icon from the registered executable."""

import ctypes
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

from qplot._icons import icon_path
from qplot._windows_launcher import create_windows_executable

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Windows resources")


def test_native_launcher_embeds_every_icon_frame(tmp_path):
    create_windows_executable(tmp_path, Path(sys.executable))
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    kernel.LoadLibraryExW.argtypes = [ctypes.c_wchar_p, pointer, ctypes.c_uint]
    kernel.LoadLibraryExW.restype = pointer
    kernel.FindResourceW.argtypes = [pointer, pointer, pointer]
    kernel.FindResourceW.restype = pointer
    kernel.LoadResource.argtypes = [pointer, pointer]
    kernel.LoadResource.restype = pointer
    kernel.LockResource.argtypes = [pointer]
    kernel.LockResource.restype = pointer
    kernel.SizeofResource.argtypes = [pointer, pointer]
    kernel.SizeofResource.restype = ctypes.c_uint
    kernel.FreeLibrary.argtypes = [pointer]
    kernel.FreeLibrary.restype = ctypes.c_int
    module = kernel.LoadLibraryExW(str(tmp_path / "qPlot.exe"), None, 2)
    assert module

    def resource(kind, identifier):
        found = kernel.FindResourceW(module, identifier, kind)
        assert found, ctypes.get_last_error()
        loaded = kernel.LoadResource(module, found)
        assert loaded
        return ctypes.string_at(kernel.LockResource(loaded), kernel.SizeofResource(module, found))

    try:
        icon = icon_path("qplot.ico").read_bytes()
        group = resource(14, 1)
        assert group[:6] == icon[:6]
        for index in range(struct.unpack_from("<H", icon, 4)[0]):
            entry = icon[6 + index * 16:22 + index * 16]
            size, offset = struct.unpack_from("<II", entry, 8)
            assert group[6 + index * 14:20 + index * 14] == entry[:12] + struct.pack("<H", index + 1)
            assert resource(3, index + 1) == icon[offset:offset + size]
    finally:
        kernel.FreeLibrary(module)


def test_launcher_creation_preserves_existing_database(tmp_path):
    output = tmp_path / "qPlot.exe"
    original = b"SQLite format 3\0" + bytes(100)
    output.write_bytes(original)
    with pytest.raises(OSError, match="SQLite database"):
        create_windows_executable(tmp_path, Path(sys.executable))
    assert output.read_bytes() == original


def test_launcher_releases_executable_while_bootstrap_runs(tmp_path):
    create_windows_executable(tmp_path, Path(sys.executable))
    started = tmp_path / "started"
    stop = tmp_path / "stop"
    (tmp_path / "bootstrap.py").write_text(
        "from pathlib import Path\nimport sys, time\n"
        f"Path({str(started)!r}).write_text(str(sys.flags.isolated))\n"
        "deadline = time.monotonic() + 15\n"
        f"while not Path({str(stop)!r}).exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.02)\n",
        encoding="utf-8",
    )
    try:
        result = subprocess.run([str(tmp_path / "qPlot.exe")], timeout=5)
        assert result.returncode == 0
        deadline = time.monotonic() + 5
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert started.read_text() == "1"
        # Windows forbids deleting an executable while it is still running.
        (tmp_path / "qPlot.exe").unlink()
    finally:
        stop.touch()
