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
    subprocess.run(
        [str(executable), "-I", "-c",
         "import sys, qplot; from pathlib import Path; "
         "assert (sys.prefix, sys.executable, str(Path(qplot.__file__).absolute())) == tuple(sys.argv[1:])",
         sys.prefix, str(Path(sys.executable).absolute()), str(Path(qplot.__file__).absolute())],
        env=dict(os.environ, __PYVENV_LAUNCHER__=sys.executable),
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
        subprocess.run(
            [str(executable), "-I", "-c",
             "import sys, qplot; from pathlib import Path; "
             "assert (sys.prefix, sys.executable, str(Path(qplot.__file__).absolute())) == tuple(sys.argv[1:])",
             record["prefix"], record["python"], record["package"]],
            env=dict(os.environ, __PYVENV_LAUNCHER__=str(python)),
            check=True, capture_output=True, timeout=2,
        )
        try:
            staged.rename(bundle)
        except OSError:
            # Another simultaneous launch may have published the same bundle.
            if not bundle.is_dir():
                raise
        return _verify_bundle(bundle, record)
