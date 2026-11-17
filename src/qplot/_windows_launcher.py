"""Build a windowed Python launcher with qPlot's own executable icon."""

from __future__ import annotations

import ctypes
import struct
import tempfile
from pathlib import Path

from qplot._auxiliary_paths import ensure_safe_auxiliary_path
from qplot._icons import icon_path

# This payload must work before importing qPlot, including broken installations.
_BOOTSTRAP = """import ctypes, subprocess, sys
from pathlib import Path
try:
    subprocess.Popen(
        [str(Path(sys.executable).with_name('python.exe')), '-I',
         str(Path(sys.argv[0]).with_name('bootstrap.py')), *sys.argv[1:]],
        creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True,
    )
except OSError as error:
    ctypes.windll.user32.MessageBoxW(None, str(error), 'qPlot could not start', 16)
    raise SystemExit(79)
"""


def _embed_icon(executable: Path, icon: bytes) -> None:
    """Add an icon group to a private launcher stub before its ZIP is appended."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    kernel.BeginUpdateResourceW.argtypes = [ctypes.c_wchar_p, ctypes.c_int]
    kernel.BeginUpdateResourceW.restype = pointer
    kernel.UpdateResourceW.argtypes = [
        pointer, pointer, pointer, ctypes.c_ushort, pointer, ctypes.c_uint,
    ]
    kernel.UpdateResourceW.restype = ctypes.c_int
    kernel.EndUpdateResourceW.argtypes = [pointer, ctypes.c_int]
    kernel.EndUpdateResourceW.restype = ctypes.c_int

    reserved, kind, count = struct.unpack_from("<HHH", icon)
    if reserved or kind != 1 or not count or len(icon) < 6 + count * 16:
        raise ValueError("Invalid qPlot launcher icon")
    group = bytearray(icon[:6])
    resources = []
    for index in range(count):
        entry = icon[6 + index * 16:22 + index * 16]
        size, offset = struct.unpack_from("<II", entry, 8)
        if not size or offset < 6 + count * 16 or offset + size > len(icon):
            raise ValueError("Invalid qPlot launcher icon frame")
        resources.append(icon[offset:offset + size])
        group.extend(entry[:12] + struct.pack("<H", index + 1))

    handle = kernel.BeginUpdateResourceW(str(executable), False)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        for index, frame in enumerate(resources, 1):
            if not kernel.UpdateResourceW(handle, 3, index, 0, frame, len(frame)):
                raise ctypes.WinError(ctypes.get_last_error())
        data = bytes(group)
        if not kernel.UpdateResourceW(handle, 14, 1, 0, data, len(data)):
            raise ctypes.WinError(ctypes.get_last_error())
    except BaseException:
        kernel.EndUpdateResourceW(handle, True)
        raise
    if not kernel.EndUpdateResourceW(handle, False):
        raise ctypes.WinError(ctypes.get_last_error())


def create_windows_executable(destination: Path, python: Path) -> None:
    """Create qPlot.exe using distlib's architecture-matched GUI launcher.

    Resource updates precede distlib's shebang/ZIP append: updating the complete
    executable can discard that overlay and make the launcher unstartable.
    The payload starts the existing supervised bootstrap and exits, releasing
    qPlot.exe so desktop integration can be removed while the viewer runs.
    """
    from distlib.scripts import ScriptMaker

    output = destination / "qPlot.exe"
    ensure_safe_auxiliary_path(output)
    if output.exists():
        raise FileExistsError(output)
    with tempfile.TemporaryDirectory(prefix="qplot-launcher-") as temporary:
        scratch = Path(temporary)

        class IconScriptMaker(ScriptMaker):
            # distlib is pinned: this hook customises only its native stub.
            def _get_launcher(self, kind):
                stub = scratch / "stub.exe"
                stub.write_bytes(super()._get_launcher(kind))
                _embed_icon(stub, icon_path("qplot.ico").read_bytes())
                return stub.read_bytes()

        maker = IconScriptMaker(None, str(destination))
        maker.variants = {""}
        # Explicit executable paths must already be quoted. GUI mode selects
        # pythonw.exe; normalise callers that are themselves running pythonw.
        maker.executable = f'"{python.with_name("python.exe")}"'
        maker.script_template = _BOOTSTRAP
        maker.make("qPlot = runpy:run_path", options={"gui": True, "interpreter_args": ["-I"]})
