"""The Dock needs a real qPlot bundle, not just a renamed NSRunningApplication."""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from qplot import _macos_identity


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_other_platforms_do_not_prepare_a_bundle(monkeypatch, platform):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(_macos_identity, "_runtime_executable", lambda: pytest.fail("Prepared macOS bundle"))
    command, environment = [sys.executable, "-m", "qplot"], {}
    actual = _macos_identity.prepare_gui_launch(command, environment)
    assert actual[0] is command
    assert actual[1] is environment


def test_headless_launch_does_not_prepare_a_bundle(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(_macos_identity, "_runtime_executable", lambda: pytest.fail("Prepared GUI bundle"))
    command, environment = [sys.executable], {"QT_QPA_PLATFORM": "offscreen"}
    assert _macos_identity.prepare_gui_launch(command, environment) == (command, environment)


def test_bundle_launch_preserves_venv_arguments_and_supervisor_environment(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    executable = Path("/test/qPlot.app/Contents/MacOS/qPlot")
    monkeypatch.setattr(_macos_identity, "_runtime_executable", lambda: executable)
    command = [sys.executable, "-m", "qplot._shutdown_supervisor", "marker", "測定 data.db"]
    environment = {"QT_QPA_PLATFORM": "cocoa", "private_supervisor_token": "keep"}
    actual, child_environment = _macos_identity.prepare_gui_launch(command, environment)
    assert actual == [str(executable), *command[1:]]
    assert child_environment == {**environment, "__PYVENV_LAUNCHER__": sys.executable}
    assert "__PYVENV_LAUNCHER__" not in environment
    assert command[0] == sys.executable


def test_unavailable_bundle_does_not_prevent_startup(monkeypatch, caplog):
    monkeypatch.setattr(sys, "platform", "darwin")

    def unavailable():
        raise OSError("Cannot create cache")

    monkeypatch.setattr(_macos_identity, "_runtime_executable", unavailable)
    command, environment = [sys.executable], {"QT_QPA_PLATFORM": "cocoa"}
    assert _macos_identity.prepare_gui_launch(command, environment) == (command, environment)
    assert "Could not prepare the qPlot Dock application" in caplog.text


def test_desktop_launch_uses_installed_bundle(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    bundle = Path("/Applications/qPlot.app")
    executable = bundle / "Contents/MacOS/qPlot"
    monkeypatch.setattr(_macos_identity, "_runtime_executable", lambda: pytest.fail("Created a second app"))

    def installed(path):
        assert path == bundle
        return executable

    monkeypatch.setattr(_macos_identity, "_installed_executable", installed)
    command = [sys.executable, "-m", "qplot"]
    actual, environment = _macos_identity.prepare_gui_launch(command, {"QPLOT_DESKTOP_BUNDLE": str(bundle)})
    assert actual == [str(executable), *command[1:]]
    assert environment["__PYVENV_LAUNCHER__"] == sys.executable


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS application bundle")
@pytest.mark.parametrize("desktop_launch", [False, True])
def test_native_gui_uses_qplot_bundle_and_original_venv(tmp_path, desktop_launch):
    from qplot import desktop

    if desktop_launch:
        bundle = tmp_path / "qPlot.app"
        desktop.setup(bundle, desktop_shortcut=False)
        executable = _macos_identity._installed_executable(bundle)
    else:
        executable = _macos_identity._runtime_executable(tmp_path)
    bundle = executable.parents[2]
    before = executable.stat().st_mtime_ns
    assert (_macos_identity._installed_executable(bundle) if desktop_launch
            else _macos_identity._runtime_executable(tmp_path)) == executable
    assert executable.stat().st_mtime_ns == before
    # Read-only reuse is also important to other running instances of this app.
    source = textwrap.dedent('''\
        import json, os, subprocess, sys
        from PyQt6.QtCore import QTimer
        from PyQt6.QtWidgets import QApplication, QWidget
        from qplot import __main__ as entrypoint, windows

        entrypoint.configure_logging = lambda: None
        entrypoint.install_excepthook = lambda: None

        class Window(QWidget):
            def __init__(self, **kwargs):
                super().__init__()
                self.show()
                query = ('ObjC.import("AppKit"); var a='
                         '$.NSRunningApplication.runningApplicationWithProcessIdentifier('
                         + str(os.getpid()) + '); JSON.stringify({'
                         'name:ObjC.unwrap(a.localizedName),bundle:ObjC.unwrap(a.bundleURL.path)});')
                native = json.loads(subprocess.check_output(
                    ['/usr/bin/osascript', '-l', 'JavaScript', '-e', query],
                    text=True, timeout=10,
                ))
                print(json.dumps(dict(native, prefix=sys.prefix, executable=sys.executable)), flush=True)
                QTimer.singleShot(0, QApplication.instance().quit)

        windows.MainWindow = Window
        raise SystemExit(entrypoint._run_gui())
    ''')
    try:
        result = subprocess.run(
            [str(executable), "-I", "-c", source], cwd=tmp_path,
            env=dict(os.environ, QT_QPA_PLATFORM="cocoa", __PYVENV_LAUNCHER__=sys.executable),
            capture_output=True, text=True, timeout=25,
        )
    finally:
        if desktop_launch:
            desktop.remove(bundle)
        else:
            subprocess.run(
                ["/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister",
                 "-u", str(bundle)], capture_output=True, timeout=10, check=False,
            )
    assert result.returncode == 0, result.stdout + result.stderr
    native = json.loads(result.stdout.splitlines()[-1])
    assert native["name"] == "qPlot"
    # The old implementation passed the name check while still using Python.app.
    assert Path(native["bundle"]).resolve() == bundle.resolve()
    assert bundle.name == "qPlot.app"
    assert native["prefix"] == sys.prefix
    assert native["executable"] == sys.executable

    if desktop_launch:
        return

    # A foreign SQLite file in the cache must be preserved, never rebuilt over.
    record = bundle / "Contents/Resources/installation.json"
    database = b"SQLite format 3\x00" + b"synthetic measurement fixture"
    record.write_bytes(database)
    with pytest.raises(ValueError):
        _macos_identity._runtime_executable(tmp_path)
    assert record.read_bytes() == database
