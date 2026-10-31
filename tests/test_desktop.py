"""Desktop setup contract; platform shell integration is also exercised natively."""

import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from qplot import desktop


@pytest.fixture
def fake_tools(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[0] == "/usr/bin/osacompile":
            destination = Path(command[2])
            (destination / "Contents/Resources").mkdir(parents=True)
            with (destination / "Contents/Info.plist").open("wb") as stream:
                plistlib.dump({"CFBundleDocumentTypes": [{"CFBundleTypeExtensions": ["*"]}]}, stream)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(desktop.subprocess, "run", run)
    monkeypatch.setattr(desktop, "_windows_shortcut", lambda destination: calls.append(
        (["IShellLinkW", str(destination)], {}),
    ))
    return calls


def test_mac_setup_and_remove(tmp_path, monkeypatch, fake_tools):
    monkeypatch.setattr(sys, "platform", "darwin")
    destination = tmp_path / "qPlot Ω space.app"
    desktop.setup(destination)
    resources = destination / "Contents/Resources"
    record = json.loads((resources / "installation.json").read_text())
    assert record["python"] == sys.executable
    assert record["prefix"] == sys.prefix
    assert record["associations"] is False
    with (destination / "Contents/Info.plist").open("rb") as stream:
        assert "CFBundleDocumentTypes" not in plistlib.load(stream)
    script = (resources / "launch.sh").read_text()
    assert '"$@"' in script
    assert " -I " in script
    assert str(Path(sys.executable).absolute()) in script
    assert fake_tools[-1][0][:4] == ["/usr/bin/codesign", "--force", "--sign", "-"]
    with pytest.raises(ValueError, match="already exists"):
        desktop.setup(destination)
    desktop.remove(destination)
    assert not destination.exists()


def test_setup_command(tmp_path, monkeypatch, fake_tools):
    monkeypatch.setattr(sys, "platform", "darwin")
    destination = tmp_path / "qPlot.app"
    assert desktop.main(["setup", "--destination", str(destination), "--file-associations"]) == 0
    record = json.loads((destination / "Contents/Resources/installation.json").read_text())
    assert record["associations"] is True
    assert desktop.main(["remove", "--destination", str(destination)]) == 0
    assert not destination.exists()


def test_associations_are_opt_in(tmp_path, monkeypatch, fake_tools):
    monkeypatch.setattr(sys, "platform", "darwin")
    destination = tmp_path / "qPlot.app"
    desktop.setup(destination, associations=True)
    with (destination / "Contents/Info.plist").open("rb") as stream:
        document_type, = plistlib.load(stream)["CFBundleDocumentTypes"]
    assert document_type["LSHandlerRank"] == "Alternate"
    assert document_type["CFBundleTypeRole"] == "Viewer"


def test_windows_shortcut_and_unicode(tmp_path, monkeypatch, fake_tools):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    destination = tmp_path / "qPlot Ω space"
    desktop.setup(destination)
    script = (destination / "launch.vbs").read_text(encoding="utf-16")
    assert str(destination) in script
    assert "WScript.Arguments" in script
    assert "shell.Run(command, 0, True)" in script
    assert "cmd.exe" not in script
    command, _options = fake_tools[0]
    assert command == ["IShellLinkW", str(destination)]
    assert desktop._windows_command(destination).endswith(' "%1"')


def test_unrecognized_removal_preserves_files(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    (tmp_path / "installation.json").write_text('{"marker": "other"}')
    with pytest.raises(ValueError, match="not a qPlot"):
        desktop.remove(tmp_path)
    assert tmp_path.exists()


def test_bootstrap_outside_repository(tmp_path):
    """An isolated interpreter must find the install and supervise exact argv."""
    from qplot import _desktop_bootstrap

    resources = tmp_path / "launcher Ω space"
    resources.mkdir()
    shutil.copyfile(_desktop_bootstrap.__file__, resources / "bootstrap.py")
    # A tiny installed-package stand-in records the production entry-point call.
    # -I ignores PYTHONPATH, so add this fixture's install through sitecustomize
    # in a temporary venv instead of relying on the repository working directory.
    import venv

    environment = tmp_path / "Python Ω space"
    venv.EnvBuilder(with_pip=False).create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    site = subprocess.check_output(
        [str(python), "-I", "-c", "import sys, sysconfig; sys.stdout.reconfigure(encoding='utf-8'); print(sysconfig.get_path('purelib'))"],
        text=True, encoding="utf-8",
    ).strip()
    package = Path(site) / "qplot"
    package.mkdir()
    (package / "__init__.py").write_text("")
    from qplot import _auxiliary_paths

    shutil.copyfile(_auxiliary_paths.__file__, package / "_auxiliary_paths.py")
    output = tmp_path / "result.json"
    (package / "__main__.py").write_text(
        "import json, os, sys\nfrom pathlib import Path\n"
        f"def run():\n Path({str(output)!r}).write_text(json.dumps([sys.argv[1:], os.environ['MPLCONFIGDIR']]))\n return 0\n"
    )
    (resources / "installation.json").write_text(json.dumps({
        "prefix": str(environment), "package": str(package / "__init__.py"),
    }))
    for args in ([], [str(tmp_path / "測定 O'Brien $data.db")]):
        subprocess.run([str(python), "-I", str(resources / "bootstrap.py"), *args], cwd=tmp_path,
                       env=dict(os.environ, HOME=str(tmp_path), LOCALAPPDATA=str(tmp_path)), check=True, timeout=15)
        actual, cache = json.loads(output.read_text())
        assert actual == args
        assert Path(cache).is_dir()

    # Capture native error text without opening modal dialogs in automation.
    error_file = tmp_path / "error.txt"
    driver = resources / "error_driver.py"
    driver.write_text(
        "from pathlib import Path\nimport sys\n"
        f"scope = {{'__name__': 'payload', '__file__': {str(resources / 'bootstrap.py')!r}}}\n"
        "exec(compile(Path(scope['__file__']).read_text(), scope['__file__'], 'exec'), scope)\n"
        f"scope['show_error'] = lambda text: Path({str(error_file)!r}).write_text(text)\n"
        "raise SystemExit(scope['main']())\n"
    )
    import sqlite3

    database = Path(cache) / "fontlist.json"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE measurement (value)")
    before = database.read_bytes()
    failure_env = dict(os.environ, HOME=str(tmp_path), LOCALAPPDATA=str(tmp_path))
    failure = subprocess.run([str(python), "-I", str(driver)], cwd=tmp_path, env=failure_env, timeout=15)
    assert failure.returncode == 79
    assert "SQLite database" in error_file.read_text()
    assert database.read_bytes() == before
    database.unlink()  # Explicit synthetic fixture cleanup.
    shutil.rmtree(package)
    failure = subprocess.run([str(python), "-I", str(driver)], cwd=tmp_path, env=failure_env, timeout=15)
    assert failure.returncode == 79
    assert "No module named 'qplot'" in error_file.read_text()


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS applet tools")
def test_native_mac_compilation(tmp_path):
    unassociated = tmp_path / "unassociated.app"
    desktop.setup(unassociated)
    with (unassociated / "Contents/Info.plist").open("rb") as stream:
        assert "CFBundleDocumentTypes" not in plistlib.load(stream)
    desktop.remove(unassociated)
    destination = tmp_path / "qPlot Ω.app"
    desktop.setup(destination, associations=True)
    subprocess.run(["/usr/bin/codesign", "--verify", str(destination)], check=True)
    # Exercise the real shell wrapper against an argv-recording interpreter.
    python = tmp_path / "fake python Ω"
    result = tmp_path / "arguments"
    python.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > ' + desktop.shlex.quote(str(result)) + '\n')
    python.chmod(0o755)
    shell = destination / "Contents/Resources/launch.sh"
    shell.write_text(shell.read_text().replace(desktop.shlex.quote(sys.executable), desktop.shlex.quote(str(python))))
    subprocess.run(["/bin/sh", str(shell), "測定 O'Brien $data.db"], cwd=tmp_path, check=True)
    assert result.read_text().splitlines()[-1] == "測定 O'Brien $data.db"
    # Re-sign after replacing the interpreter for native Launch Services tests.
    subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(destination)], check=True)
    database = tmp_path / "測定 O'Brien $data.db"
    database.touch()
    import time

    for args in ([], [str(database)]):
        result.unlink()
        subprocess.run(["/usr/bin/open", "-W", "-n", "-a", str(destination), *args],
                       cwd=tmp_path, check=True, timeout=20)
        deadline = time.monotonic() + 5
        while not result.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        actual = result.read_text().splitlines()
        assert actual[0] == "-I"
        assert Path(actual[1]).resolve() == (destination / "Contents/Resources/bootstrap.py").resolve()
        assert len(actual[2:]) == len(args)
        if args:
            assert Path(actual[2]).resolve() == database.resolve()


def test_bootstrap_reports_missing_environment(tmp_path, monkeypatch):
    from qplot import _desktop_bootstrap as bootstrap

    monkeypatch.setattr(bootstrap, "__file__", str(tmp_path / "bootstrap.py"))
    (tmp_path / "installation.json").write_text(json.dumps({"prefix": "missing environment"}))
    errors = []
    monkeypatch.setattr(bootstrap, "show_error", errors.append)
    assert bootstrap.main() == 79
    assert "environment has moved" in errors[0]
    (tmp_path / "installation.json").unlink()
    assert bootstrap.main() == 79
    assert "installation.json" in errors[1]


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows launcher tools")
def test_native_windows_shortcut_and_arguments(tmp_path):
    destination = tmp_path / "qPlot Ω space"
    desktop.setup(destination)
    output = tmp_path / "result.json"
    # Exercise WScript's real parsing without starting an interactive GUI in CI.
    (destination / "bootstrap.py").write_text(
        "import json, sys\nfrom pathlib import Path\n"
        f"Path({str(output)!r}).write_text(json.dumps(sys.argv[1:]))\n"
    )
    for args in ([], [str(tmp_path / "測定 O'Brien %data & x.db")]):
        subprocess.run(["wscript.exe", "//nologo", str(destination / "launch.vbs"), *args],
                       cwd=tmp_path, check=True, timeout=20)
        assert json.loads(output.read_text()) == args
    shortcut = destination / "qPlot.lnk"
    assert shortcut.is_file()
    # Also execute the shortcut itself: its saved arguments must preserve the
    # Unicode launcher path, not just the directly invoked VBS script above.
    import time

    output.unlink()
    os.startfile(shortcut)
    deadline = time.monotonic() + 10
    while not output.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert json.loads(output.read_text()) == []
    desktop.remove(destination)


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows registry")
def test_windows_registration_roundtrip(tmp_path, monkeypatch):
    import uuid
    import winreg

    # Avoid touching any real qPlot registration on a developer's machine.
    progid = "qPlot.Test." + uuid.uuid4().hex
    monkeypatch.setattr(desktop, "_PROGID", progid)
    destination = tmp_path / "qPlot Ω space"
    base = r"Software\Classes"
    extension = base + r"\.db"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, extension) as key:
            default_before = winreg.QueryValue(key, "")
    except FileNotFoundError:
        default_before = None
    try:
        desktop._windows_associations(destination)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base + "\\" + progid + r"\shell\open\command") as key:
            assert winreg.QueryValue(key, "") == desktop._windows_command(destination)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, extension + r"\OpenWithProgids") as key:
            assert winreg.QueryValueEx(key, progid)[0] == ""
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, extension) as key:
            assert winreg.QueryValue(key, "") == (default_before or "")
        with pytest.raises(ValueError, match="Another qPlot launcher"):
            desktop._windows_associations(tmp_path / "other launcher")
        desktop._windows_associations(tmp_path / "other launcher", remove=True)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base + "\\" + progid):
            pass  # Other installations cannot remove this launcher's entry.
    finally:
        desktop._windows_associations(destination, remove=True)
    with pytest.raises(FileNotFoundError):
        winreg.OpenKey(winreg.HKEY_CURRENT_USER, base + "\\" + progid)


def test_removal_refuses_database_inside_launcher(tmp_path, monkeypatch, fake_tools):
    import sqlite3

    monkeypatch.setattr(sys, "platform", "darwin")
    destination = tmp_path / "qPlot.app"
    desktop.setup(destination)
    database = destination / "measurement.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE measurement (value)")
    before = database.read_bytes()
    with pytest.raises(OSError, match="SQLite database"):
        desktop.remove(destination)
    assert database.read_bytes() == before
    assert (destination / "Contents/Resources/installation.json").exists()


@pytest.mark.parametrize("suffix", ["-wal", "-journal", "-shm"])
def test_removal_preserves_orphan_sidecars(tmp_path, monkeypatch, fake_tools, suffix):
    monkeypatch.setattr(sys, "platform", "win32")
    destination = tmp_path / "qPlot"
    desktop.setup(destination)
    sidecar = destination / ("measurement.db" + suffix)
    sidecar.write_bytes(b"coordination")
    with pytest.raises(ValueError, match="database sidecar"):
        desktop.remove(destination)
    assert sidecar.read_bytes() == b"coordination"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS startup shell tools")
@pytest.mark.parametrize("diagnostic", ["PermissionError: Operation not permitted", "Python runtime is missing"])
def test_mac_preserves_python_startup_errors(tmp_path, request, diagnostic):
    run_process = subprocess.run
    request.getfixturevalue("fake_tools")
    destination = tmp_path / "qPlot.app"
    desktop.setup(destination)
    shell = destination / "Contents/Resources/launch.sh"
    python = tmp_path / "failing Python Ω"
    python.write_text("#!/bin/sh\nprintf '%s\\n' " + desktop.shlex.quote(diagnostic) + " >&2\nexit 13\n")
    python.chmod(0o755)
    alert = tmp_path / "alert"
    recorder = tmp_path / "record alert"
    recorder.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > " + desktop.shlex.quote(str(alert)) + "\n")
    recorder.chmod(0o755)
    shell.write_text(shell.read_text().replace(desktop.shlex.quote(sys.executable), desktop.shlex.quote(str(python)))
                     .replace("/usr/bin/osascript", desktop.shlex.quote(str(recorder))))
    result = run_process(["/bin/sh", str(shell)], env=dict(os.environ, TMPDIR=str(tmp_path)), timeout=10)
    assert result.returncode == 13
    message = alert.read_text()
    assert diagnostic in message
    log = Path(message.splitlines()[-1])
    assert log.read_text().strip() == diagnostic
    if "Operation not permitted" in diagnostic:
        assert "Recreating the same launcher does not fix denied access" in message
        assert "Files and Folders" in message
    else:
        assert "repaired environment" in message
