"""The Dock needs a real qPlot bundle, not just a renamed NSRunningApplication."""

import json
import os
import plistlib
import subprocess
import sys
import textwrap
import time
import uuid
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


def test_file_open_handler_validates_and_forwards_each_document(tmp_path, monkeypatch, qapplication):
    from types import SimpleNamespace

    from PyQt6.QtCore import QEvent, QUrl

    # PyQt exposes native QFileOpenEvent instances but does not allow creating
    # them in Python; the native test below supplies real Launch Services events.
    def event_for(url):
        return SimpleNamespace(type=lambda: QEvent.Type.FileOpen, url=lambda: url)

    def file_event(path):
        return event_for(QUrl.fromLocalFile(str(path)))

    monkeypatch.setattr(sys, "platform", "darwin")
    bundle = tmp_path / "qPlot.app"
    resources = bundle / "Contents/Resources"
    resources.mkdir(parents=True)
    (resources / "launch.sh").touch()
    monkeypatch.setattr(_macos_identity, "_interpreter_executable", lambda: bundle / "Contents/MacOS/qPlot")
    calls, errors = [], []

    def run(command, **options):
        calls.append((command, options))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(_macos_identity.subprocess, "run", run)
    monkeypatch.setenv("_QPLOT_SHUTDOWN_SUPERVISOR_V1", "private-token")
    monkeypatch.setenv("QPLOT_DESKTOP_BUNDLE", "previous-launch")
    monkeypatch.setenv("__PYVENV_LAUNCHER__", "previous-interpreter")
    first = tmp_path / "first.db"
    handler = _macos_identity.install_file_open_handler(
        qapplication, SimpleNamespace(show_error=lambda *args: errors.append(args)), str(first),
    )
    try:
        assert handler is qapplication._qplot_file_open_handler
        assert handler.eventFilter(qapplication, file_event(first))
        assert not calls  # A native repeat of the command-line path is already loaded.
        qapplication.processEvents()
        for path in (first, tmp_path / "測定 second.db"):
            assert handler.eventFilter(qapplication, file_event(path))
        assert [call[0] for call in calls] == [
            ["/usr/bin/open", "-n", "-a", str(bundle), str(path)]
            for path in (first, tmp_path / "測定 second.db")
        ]
        for _command, options in calls:
            assert options["timeout"] == 5
            assert options["check"] is True
            assert not any(key.startswith(("_QPLOT_", "QPLOT_")) for key in options["env"])
            assert "__PYVENV_LAUNCHER__" not in options["env"]
        for event in (event_for(QUrl("https://example.com/not-local.db")),
                      event_for(QUrl()), file_event(tmp_path / "first.db-wal")):
            assert not handler.eventFilter(qapplication, event)
        assert len(calls) == 2

        def fail(command, **options):
            raise subprocess.TimeoutExpired(command, options["timeout"])

        monkeypatch.setattr(_macos_identity.subprocess, "run", fail)
        assert handler.eventFilter(qapplication, file_event(first))
        assert len(errors) == 1
        assert errors[0][0] == "Database Open Failed"
    finally:
        qapplication.removeEventFilter(handler)
        handler.deleteLater()
        del qapplication._qplot_file_open_handler


def test_unavailable_interpreter_does_not_prevent_document_handler_startup(monkeypatch, caplog, qapplication):
    monkeypatch.setattr(sys, "platform", "darwin")

    def unavailable():
        raise OSError("Executable no longer exists")

    monkeypatch.setattr(_macos_identity, "_interpreter_executable", unavailable)
    assert _macos_identity.install_file_open_handler(qapplication, None) is None
    assert "Could not prepare macOS document handling" in caplog.text


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS document events")
def test_native_finder_open_reaches_launcher_while_gui_is_running(tmp_path):
    """A shared Dock identity must not swallow a subsequent Finder document open."""
    from qplot import desktop

    bundle = tmp_path / "qPlot Ω.app"
    resources = desktop._mac_launcher(bundle, Path(sys.executable), associations=False)
    _macos_identity.install_interpreter(bundle)
    # Keep the production AppleScript/shell forwarding, but record its final
    # arguments instead of opening a database or creating another real viewer.
    received = tmp_path / "received.json"
    (resources / "bootstrap.py").write_text(
        "import json, sys\nfrom pathlib import Path\n"
        f"Path({str(received)!r}).write_text(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    info = bundle / "Contents/Info.plist"
    properties = plistlib.loads(info.read_bytes())
    # A test identity cannot redirect events from an installed/running qPlot.
    properties["CFBundleIdentifier"] = "org.qplot.test." + uuid.uuid4().hex
    info.write_bytes(plistlib.dumps(properties))
    subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(bundle)],
                   check=True, capture_output=True, timeout=10)
    desktop._mac_registration(bundle)
    database = tmp_path / "測定 O'Brien $data.db"
    database.touch()  # Empty synthetic file; the recorder never reads it.
    ready = tmp_path / "ready"
    driver = tmp_path / "running_gui.py"
    driver.write_text(textwrap.dedent(f'''\
        import os, subprocess
        from pathlib import Path
        from PyQt6.QtCore import QTimer
        from PyQt6.QtWidgets import QApplication, QWidget
        from qplot._macos_identity import install_file_open_handler
        app = QApplication([__file__, {str(database)!r}])
        window = QWidget()
        window.show_error = lambda *args: print(args, flush=True)
        install_file_open_handler(app, window, {str(database)!r})
        window.show()
        app.processEvents()
        query = ('ObjC.import("AppKit"); '
                 '$.NSRunningApplication.runningApplicationWithProcessIdentifier('
                 + str(os.getpid()) + ').bundleURL.path.js')
        native_bundle = subprocess.check_output(
            ['/usr/bin/osascript', '-l', 'JavaScript', '-e', query],
            text=True, timeout=10,
        ).strip()
        Path({str(ready)!r}).write_text(native_bundle)
        QTimer.singleShot(15000, app.quit)
        app.exec()
    '''), encoding="utf-8")
    # This is the interpreter the production supervisor uses for the GUI. The
    # handler must forward warm Open Documents through a fresh applet instance.
    child = None
    try:
        child = subprocess.Popen(
            [str(bundle / "Contents/MacOS/qPlot"), "-I", str(driver)],
            env=dict(os.environ, QT_QPA_PLATFORM="cocoa", __PYVENV_LAUNCHER__=sys.executable),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        deadline = time.monotonic() + 10
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists(), "The synthetic GUI did not start"
        assert Path(ready.read_text()).resolve() == bundle.resolve()
        assert not received.exists(), "The command-line document was forwarded twice"
        subprocess.run(["/usr/bin/open", "-a", str(bundle), str(database)],
                       check=True, timeout=10)
        deadline = time.monotonic() + 5
        while not received.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert received.exists(), "Finder did not forward the document to the launcher"
        assert [Path(value).resolve() for value in json.loads(received.read_text())] == [database.resolve()]
        assert child.poll() is None, "Opening a document replaced the existing GUI"
    finally:
        if child is not None:
            child.terminate()
            try:
                child.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate(timeout=5)
        desktop._mac_registration(bundle, remove=True)


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
