"""Run the macOS GUI from its own app bundle, rather than Python.app."""

from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from qplot._icons import icon_path


def prepare_gui_launch(command, environment):
    """Keep the venv and argv while giving the GUI a real qPlot.app identity."""
    if sys.platform != "darwin" or environment.get("QT_QPA_PLATFORM", "").split(":")[0] in {
        "offscreen", "minimal",
    }:
        return command, environment
    try:
        bundle = environment.get("QPLOT_DESKTOP_BUNDLE")
        executable = _installed_executable(Path(bundle)) if bundle else _runtime_executable()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        # A cosmetic integration failure must not prevent opening the viewer.
        logging.getLogger("qplot").warning("Could not prepare the qPlot Dock application: %s", error)
        return command, environment
    child_environment = dict(environment)
    # This is the same CPython launcher mechanism used by framework Python.
    # It preserves sys.executable, pyvenv.cfg discovery and helper interpreters.
    child_environment["__PYVENV_LAUNCHER__"] = sys.executable
    return [str(executable), *command[1:]], child_environment


def _interpreter_executable() -> Path:
    """Find the actual Mach-O image, not the venv symlink or Python wrapper."""
    library = ctypes.CDLL(None)
    get_path = library._NSGetExecutablePath
    get_path.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint32)]
    get_path.restype = ctypes.c_int
    size = ctypes.c_uint32(4096)
    buffer = ctypes.create_string_buffer(size.value)
    if get_path(buffer, ctypes.byref(size)):
        buffer = ctypes.create_string_buffer(size.value)
        if get_path(buffer, ctypes.byref(size)):
            raise OSError("Could not locate the running Python executable")
    return Path(os.fsdecode(buffer.value)).resolve(strict=True)


def install_file_open_handler(app, window, startup_database_path=None):
    """Forward warm Finder opens through a new, independently supervised applet."""
    if sys.platform != "darwin":
        return None
    from PyQt6 import QtCore

    try:
        executable = _interpreter_executable()
    except (OSError, ValueError) as error:
        logging.getLogger("qplot").warning("Could not prepare macOS document handling: %s", error)
        return None
    bundle = executable.parents[2]
    if bundle.suffix != ".app" or not (bundle / "Contents/Resources/launch.sh").is_file():
        return None  # The cosmetic-bundle fallback is running in Python.app.

    class FileOpenHandler(QtCore.QObject):
        def __init__(self):
            super().__init__(app)
            self.starting = True
            # Qt may repeat the command-line document as a native FileOpen
            # event during startup. It is already being loaded by MainWindow.
            self.startup_path = (os.path.abspath(startup_database_path)
                                 if startup_database_path else None)
            QtCore.QTimer.singleShot(0, self.finish_startup)

        def finish_startup(self):
            self.starting = False

        def eventFilter(self, watched, event):
            if event.type() != QtCore.QEvent.Type.FileOpen:
                return False
            url = event.url()
            if not url.isLocalFile():
                return False
            filename = url.toLocalFile()
            if not filename or not filename.lower().endswith(".db"):
                return False
            if self.starting and os.path.abspath(filename) == self.startup_path:
                return True
            try:
                # Without -n, Launch Services sends the event back to this GUI.
                # The applet retains the normal bootstrap and shutdown owner;
                # no Python process is spawned as a child of the existing GUI.
                environment = {key: value for key, value in os.environ.items()
                               if not key.startswith(("_QPLOT_", "QPLOT_"))
                               and key != "__PYVENV_LAUNCHER__"}
                subprocess.run(["/usr/bin/open", "-n", "-a", str(bundle), filename],
                               check=True, capture_output=True, timeout=5, env=environment)
            except (OSError, subprocess.SubprocessError) as error:
                window.show_error("Database Open Failed", "qPlot could not start another viewer.",
                                  str(error))
            return True

    handler = FileOpenHandler()
    app.installEventFilter(handler)
    app._qplot_file_open_handler = handler
    return handler


def _installed_executable(bundle: Path) -> Path:
    """Use the desktop app itself, so its shortcut and GUI share a Dock tile."""
    import qplot
    from qplot.desktop import _MARKER

    record = json.loads((bundle / "Contents/Resources/installation.json").read_text())
    expected = {"marker": _MARKER, "python": str(Path(sys.executable).absolute()),
                "prefix": sys.prefix, "package": str(Path(qplot.__file__).absolute())}
    if any(record.get(key) != value for key, value in expected.items()):
        raise ValueError("The desktop bundle belongs to a different installation")
    return _verify_bundle(bundle, record)


def install_interpreter(bundle: Path) -> None:
    """Install and preflight an interpreter copy inside a newly built bundle."""
    import qplot

    executable = bundle / "Contents/MacOS/qPlot"
    shutil.copy2(_interpreter_executable(), executable)
    subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(executable)],
                   check=True, capture_output=True, timeout=2)
    _verify_interpreter(
        executable, prefix=sys.prefix, python=str(Path(sys.executable).absolute()),
        package=str(Path(qplot.__file__).absolute()), launcher_python=sys.executable,
    )


def _verify_interpreter(
    executable: Path, *, prefix: str, python: str, package: str, launcher_python: str,
) -> None:
    """Require a signed interpreter copy to start the expected installation."""
    subprocess.run(
        [str(executable), "-I", "-c",
         "import sys, qplot; from pathlib import Path; "
         "assert (sys.prefix, sys.executable, str(Path(qplot.__file__).absolute())) == tuple(sys.argv[1:])",
         prefix, python, package],
        env=dict(os.environ, __PYVENV_LAUNCHER__=launcher_python),
        check=True, capture_output=True, timeout=2,
    )


def _verify_bundle(bundle: Path, record: dict) -> Path:
    if bundle.is_symlink() or any(path.is_symlink() for path in bundle.rglob("*")):
        raise OSError("The qPlot runtime bundle contains a symlink")
    saved = json.loads((bundle / "Contents/Resources/installation.json").read_text(encoding="utf-8"))
    if saved != record:
        raise ValueError("The qPlot runtime bundle belongs to a different installation")
    if not (bundle / "Contents/MacOS/qPlot").is_file():
        raise OSError("The qPlot bundle has no GUI interpreter")
    subprocess.run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(bundle)],
        check=True, capture_output=True, timeout=2,
    )
    return bundle / "Contents/MacOS/qPlot"


def _runtime_executable(cache_root: Path | None = None) -> Path:
    """Build once in a fresh directory; never replace an existing cache entry.

    A content-derived directory refreshes the bundle when Python, launcher code
    or artwork changes. Existing entries are validated read-only. The applet
    remains its launch entry point, so a Dock shortcut can reopen the viewer.
    """
    import qplot
    from qplot import _desktop_bootstrap, desktop

    python = Path(sys.executable).absolute()
    source = _interpreter_executable()
    record = {
        "marker": "qplot-macos-runtime-v1", "python": str(python),
        "prefix": sys.prefix, "package": str(Path(qplot.__file__).absolute()),
    }
    digest = hashlib.sha256(json.dumps(record, sort_keys=True).encode("utf-8"))
    for path in (source, Path(__file__), Path(desktop.__file__),
                 Path(_desktop_bootstrap.__file__), icon_path("qplot.icns")):
        digest.update(str(path).encode("utf-8"))
        digest.update(path.read_bytes())
    root = cache_root if cache_root is not None else Path.home() / "Library/Caches/qPlot/launchers"
    version = root / digest.hexdigest()[:24]
    bundle = version / "qPlot.app"
    if bundle.exists() or bundle.is_symlink():
        return _verify_bundle(bundle, record)
    version.mkdir(parents=True, exist_ok=True)
    # All writes, compilation and signing target our new staging directory.
    # A foreign file at the final path is never overwritten or cleaned up.
    with tempfile.TemporaryDirectory(prefix=".building-", dir=version) as temporary:
        staged = Path(temporary) / "qPlot.app"
        resources = desktop._mac_launcher(staged, python, associations=False, compile_timeout=2)
        shutil.copyfile(_desktop_bootstrap.__file__, resources / "bootstrap.py")
        (resources / "installation.json").write_text(json.dumps(record), encoding="utf-8")
        info_path = staged / "Contents/Info.plist"
        properties = plistlib.loads(info_path.read_bytes())
        properties.update(CFBundleIdentifier="uk.ac.lancaster.qplot.runtime",
                          CFBundleName="qPlot", CFBundleDisplayName="qPlot")
        info_path.write_bytes(plistlib.dumps(properties))
        executable = staged / "Contents/MacOS/qPlot"
        shutil.copy2(source, executable)
        # The original executable signature is tied to Python.app's Info.plist.
        # Sign only our copy, then seal the surrounding qPlot bundle.
        for target in (executable, staged):
            subprocess.run(
                ["/usr/bin/codesign", "--force", "--sign", "-", str(target)],
                check=True, capture_output=True, timeout=2,
            )
        # Some Python distributions use relative library paths. Verify that
        # this copy can still start the exact installation before publishing it.
        _verify_interpreter(
            executable, prefix=record["prefix"], python=record["python"],
            package=record["package"], launcher_python=str(python),
        )
        try:
            staged.rename(bundle)
        except OSError:
            # Another simultaneous launch may have published the same bundle.
            if not bundle.is_dir():
                raise
        return _verify_bundle(bundle, record)
