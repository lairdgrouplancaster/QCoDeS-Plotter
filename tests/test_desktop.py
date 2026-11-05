"""Desktop setup contract; platform shell integration is also exercised natively."""

import json
import os
import plistlib
import shutil
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from qplot import _desktop_associations as associations
from qplot import _desktop_shortcut, desktop


@pytest.fixture(autouse=True)
def isolated_desktop(tmp_path, monkeypatch):
    path = tmp_path / "Desktop Ω space"
    monkeypatch.setattr(_desktop_shortcut, "desktop_directory", lambda: path)
    return path


@pytest.fixture
def fake_tools(monkeypatch):
    from qplot import _macos_identity

    monkeypatch.setattr(_macos_identity, "install_interpreter", lambda destination: None)
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[0] == "/usr/bin/osacompile":
            destination = Path(command[2])
            (destination / "Contents/Resources").mkdir(parents=True)
            with (destination / "Contents/Info.plist").open("wb") as stream:
                plistlib.dump({
                    "CFBundleDocumentTypes": [{"CFBundleTypeExtensions": ["*"]}],
                    "CFBundleIconFile": "applet.icns", "CFBundleIconName": "applet",
                }, stream)
        elif command[0] == "/usr/bin/codesign":
            destination = Path(command[-1])
            assert (destination / "Contents/Resources/qplot.icns").read_bytes() == desktop.icon_path("qplot.icns").read_bytes()
            with (destination / "Contents/Info.plist").open("rb") as stream:
                document_types = plistlib.load(stream).get("CFBundleDocumentTypes", [])
            for document_type in document_types:
                name = document_type["CFBundleTypeIconFile"]
                assert (destination / "Contents/Resources" / name).read_bytes() == desktop.icon_path(name).read_bytes()
        return subprocess.CompletedProcess(command, 0)

    def shortcut(destination):
        assert (destination / "qplot.ico").read_bytes() == desktop.icon_path("qplot.ico").read_bytes()
        calls.append((["IShellLinkW", str(destination)], {}))
        (destination / "qPlot.lnk").write_bytes(b"Synthetic shell-link fixture")

    monkeypatch.setattr(desktop.subprocess, "run", run)
    monkeypatch.setattr(desktop, "_windows_shortcut", shortcut)
    monkeypatch.setattr(desktop, "_notify_windows_shell", lambda: None)
    return calls


def test_mac_setup_and_remove(tmp_path, monkeypatch, fake_tools, isolated_desktop):
    monkeypatch.setattr(sys, "platform", "darwin")
    destination = tmp_path / "qPlot Ω space.app"
    desktop.setup(destination)
    resources = destination / "Contents/Resources"
    record = json.loads((resources / "installation.json").read_text())
    assert record["python"] == sys.executable
    assert record["prefix"] == sys.prefix
    assert record["associations"] is False
    shortcut = isolated_desktop / "qPlot.app"
    assert shortcut.is_symlink()
    assert shortcut.resolve() == destination.resolve()
    assert record["desktop_shortcut"]["path"] == str(shortcut)
    assert record["desktop_shortcut"]["target"] == os.readlink(shortcut)
    with (destination / "Contents/Info.plist").open("rb") as stream:
        properties = plistlib.load(stream)
    assert "CFBundleDocumentTypes" not in properties
    assert "UTImportedTypeDeclarations" not in properties
    assert properties["CFBundleIconFile"] == "qplot.icns"
    assert "CFBundleIconName" not in properties
    assert not (resources / "sqlite-wal.icns").exists()
    script = (resources / "launch.sh").read_text()
    assert '"$@"' in script
    assert " -I " in script
    assert str(Path(sys.executable).absolute()) in script
    assert fake_tools[-2][0][:4] == ["/usr/bin/codesign", "--force", "--sign", "-"]
    assert fake_tools[-1][0] == [desktop._LSREGISTER, "-f", str(destination)]
    with pytest.raises(ValueError, match="already exists"):
        desktop.setup(destination)
    desktop.remove(destination)
    assert fake_tools[-1][0] == [desktop._LSREGISTER, "-u", str(destination)]
    assert not destination.exists()
    assert not shortcut.is_symlink()


def test_setup_command(tmp_path, monkeypatch, fake_tools):
    monkeypatch.setattr(sys, "platform", "darwin")
    destination = tmp_path / "qPlot.app"
    assert desktop.main(["setup", "--destination", str(destination), "--file-associations"]) == 0
    record = json.loads((destination / "Contents/Resources/installation.json").read_text())
    assert record["associations"] is True
    assert desktop.main(["remove", "--destination", str(destination)]) == 0
    assert not destination.exists()


def test_failed_mac_registration_rolls_back(tmp_path, monkeypatch, fake_tools, isolated_desktop):
    monkeypatch.setattr(sys, "platform", "darwin")
    original = desktop.subprocess.run

    def run(command, **kwargs):
        if command[:2] == [desktop._LSREGISTER, "-f"]:
            raise subprocess.CalledProcessError(1, command)
        return original(command, **kwargs)

    monkeypatch.setattr(desktop.subprocess, "run", run)
    destination = tmp_path / "qPlot.app"
    with pytest.raises(subprocess.CalledProcessError):
        desktop.setup(destination, associations=True)
    assert fake_tools[-1][0] == [desktop._LSREGISTER, "-u", str(destination)]
    assert not destination.exists()
    assert not (isolated_desktop / "qPlot.app").is_symlink()


def test_associations_are_opt_in(tmp_path, monkeypatch, fake_tools):
    monkeypatch.setattr(sys, "platform", "darwin")
    destination = tmp_path / "qPlot.app"
    desktop.setup(destination, associations=True)
    with (destination / "Contents/Info.plist").open("rb") as stream:
        properties = plistlib.load(stream)
    document_type, wal_type, shm_type = properties["CFBundleDocumentTypes"]
    assert document_type["LSHandlerRank"] == "Alternate"
    assert document_type["CFBundleTypeRole"] == "Viewer"
    assert document_type["CFBundleTypeIconFile"] == "qplot-database.icns"
    for index, declaration, extension, description, icon in (
        (0, wal_type, "db-wal", "SQLite write-ahead log", "sqlite-wal.icns"),
        (1, shm_type, "db-shm", "SQLite shared-memory index", "sqlite-shm.icns"),
    ):
        identifier = f"uk.ac.lancaster.qplot.{Path(icon).stem}"
        assert declaration == {
            "CFBundleTypeName": description, "CFBundleTypeExtensions": [extension],
            "LSItemContentTypes": [identifier],
            "CFBundleTypeRole": "None", "LSHandlerRank": "None",
            "CFBundleTypeIconFile": icon,
        }
        assert properties["UTImportedTypeDeclarations"][index] == {
            "UTTypeIdentifier": identifier, "UTTypeConformsTo": ["public.data"],
            "UTTypeDescription": description, "UTTypeIconFile": icon,
            "UTTypeTagSpecification": {"public.filename-extension": [extension]},
        }


def test_windows_shortcut_and_unicode(tmp_path, monkeypatch, fake_tools, isolated_desktop):
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
    assert associations._windows_command(destination).endswith(' "%1"')
    assert not (destination / "sqlite-wal.ico").exists()
    shortcut = isolated_desktop / "qPlot.lnk"
    assert shortcut.read_bytes() == (destination / "qPlot.lnk").read_bytes()
    desktop.remove(destination)
    assert not shortcut.exists()


def test_setup_can_omit_desktop_shortcut(tmp_path, monkeypatch, fake_tools, isolated_desktop):
    monkeypatch.setattr(sys, "platform", "darwin")
    destination = tmp_path / "qPlot.app"
    desktop.main(["setup", "--destination", str(destination), "--no-desktop-shortcut"])
    assert not isolated_desktop.exists()
    desktop.remove(destination)


@pytest.mark.parametrize("platform, name", [("darwin", "qPlot.app"), ("win32", "qPlot.lnk")])
def test_setup_preserves_existing_desktop_file(tmp_path, monkeypatch, fake_tools,
                                              isolated_desktop, platform, name):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    isolated_desktop.mkdir()
    existing = isolated_desktop / name
    original = b"SQLite format 3\x00" + b"synthetic database fixture"
    existing.write_bytes(original)
    destination = tmp_path / "qPlot.app"
    with pytest.raises(FileExistsError, match="no-desktop-shortcut"):
        desktop.setup(destination)
    assert existing.read_bytes() == original
    assert not destination.exists()


def test_setup_preserves_existing_broken_desktop_link(tmp_path, monkeypatch, fake_tools, isolated_desktop):
    monkeypatch.setattr(sys, "platform", "darwin")
    isolated_desktop.mkdir()
    shortcut = isolated_desktop / "qPlot.app"
    shortcut.symlink_to(tmp_path / "missing.app")
    with pytest.raises(FileExistsError):
        desktop.setup(tmp_path / "qPlot.app")
    assert shortcut.is_symlink()


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_removal_preserves_replaced_desktop_shortcut(tmp_path, monkeypatch, fake_tools,
                                                   isolated_desktop, platform):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    destination = tmp_path / "qPlot.app"
    desktop.setup(destination)
    shortcut = isolated_desktop / ("qPlot.app" if platform == "darwin" else "qPlot.lnk")
    shortcut.unlink()
    shortcut.write_bytes(b"A replacement owned by the user")
    desktop.remove(destination)
    assert shortcut.read_bytes() == b"A replacement owned by the user"


def test_mac_destination_already_on_desktop_needs_no_link(monkeypatch, fake_tools, isolated_desktop):
    monkeypatch.setattr(sys, "platform", "darwin")
    destination = isolated_desktop / "qPlot.app"
    desktop.setup(destination)
    assert destination.is_dir() and not destination.is_symlink()
    desktop.remove(destination)


def test_failed_signing_removes_created_desktop_link(tmp_path, monkeypatch, fake_tools, isolated_desktop):
    monkeypatch.setattr(sys, "platform", "darwin")
    run = desktop.subprocess.run

    def fail_signing(command, **kwargs):
        if command[0] == "/usr/bin/codesign":
            raise subprocess.CalledProcessError(1, command)
        return run(command, **kwargs)

    monkeypatch.setattr(desktop.subprocess, "run", fail_signing)
    destination = tmp_path / "qPlot.app"
    with pytest.raises(subprocess.CalledProcessError):
        desktop.setup(destination)
    assert not destination.exists()
    assert not (isolated_desktop / "qPlot.app").is_symlink()


def test_windows_setup_records_and_removes_sidecar_icons(tmp_path, monkeypatch, fake_tools):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    destination = tmp_path / "qPlot Ω space"
    state = [{"test_saved_state": "an existing icon"}]
    events = []

    def install(path, *, records):
        assert path == destination
        assert records == state
        assert json.loads((path / "installation.json").read_text())["sidecar_icons"] == state
        for name in desktop._DOCUMENT_ICONS:
            assert (path / f"{name}.ico").read_bytes() == desktop.icon_path(f"{name}.ico").read_bytes()
        events.append("sidecars installed")
        return state

    monkeypatch.setattr(desktop, "prepare_sidecar_icons", lambda path: state)
    monkeypatch.setattr(desktop, "install_sidecar_icons", install)
    monkeypatch.setattr(desktop, "remove_sidecar_icons", lambda saved: events.append(saved))
    monkeypatch.setattr(desktop, "_windows_associations", lambda path, remove=False: events.append((path, remove)))
    desktop.setup(destination, associations=True)
    record = json.loads((destination / "installation.json").read_text())
    assert record["sidecar_icons"] == state
    desktop.remove(destination)
    assert events == [(destination, False), "sidecars installed", state, (destination, True)]
    assert not destination.exists()


def test_windows_setup_saves_recovery_record_before_registry_changes(tmp_path, monkeypatch, fake_tools):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    destination = tmp_path / "qPlot"
    state = [{"test_saved_state": "an existing icon"}]
    events = []
    monkeypatch.setattr(desktop, "prepare_sidecar_icons", lambda path: state)
    monkeypatch.setattr(desktop, "install_sidecar_icons", lambda path, **kwargs: events.append("installed"))
    monkeypatch.setattr(desktop, "remove_sidecar_icons", lambda saved: events.append(saved))
    monkeypatch.setattr(desktop, "_windows_associations", lambda path, remove=False: events.append(remove))
    original_write = Path.write_text

    def fail_record(path, *args, **kwargs):
        if path.name == "installation.json":
            raise OSError("Cannot save launcher record")
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_record)
    with pytest.raises(OSError, match="Cannot save launcher record"):
        desktop.setup(destination, associations=True)
    assert events == []
    assert not destination.exists()


def test_windows_setup_preserves_other_launcher_registration_on_failure(tmp_path, monkeypatch, fake_tools):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    destination = tmp_path / "qPlot"
    state = [{"test_saved_state": "another launcher icon"}]
    restored = []
    monkeypatch.setattr(desktop, "prepare_sidecar_icons", lambda path: state)
    monkeypatch.setattr(desktop, "install_sidecar_icons", lambda path, **kwargs: restored.append("unexpected install"))
    monkeypatch.setattr(desktop, "remove_sidecar_icons", restored.append)

    def refuse_other_launcher(path, *, remove=False):
        assert not remove
        raise ValueError("Another qPlot launcher owns Open With")

    monkeypatch.setattr(desktop, "_windows_associations", refuse_other_launcher)
    with pytest.raises(ValueError, match="Another qPlot launcher"):
        desktop.setup(destination, associations=True)
    assert restored == []
    assert not destination.exists()


def test_windows_failed_icon_rollback_retains_removable_launcher(tmp_path, monkeypatch, fake_tools):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    destination = tmp_path / "qPlot"
    state = [{"test_saved_state": "existing companion icon"}]
    monkeypatch.setattr(desktop, "prepare_sidecar_icons", lambda path: state)
    monkeypatch.setattr(desktop, "_windows_associations", lambda path, **kwargs: None)

    def failed_install(path, **kwargs):
        raise PermissionError("Cannot install second icon")

    def failed_restore(records):
        raise PermissionError("Cannot restore first icon")

    monkeypatch.setattr(desktop, "install_sidecar_icons", failed_install)
    monkeypatch.setattr(desktop, "remove_sidecar_icons", failed_restore)
    with pytest.raises(PermissionError, match="Cannot restore first icon"):
        desktop.setup(destination, associations=True)
    assert json.loads((destination / "installation.json").read_text())["sidecar_icons"] == state
    assert (destination / "sqlite-wal.ico").is_file()
    restored = []
    monkeypatch.setattr(desktop, "remove_sidecar_icons", restored.append)
    desktop.remove(destination)
    assert restored == [state]
    assert not destination.exists()


def test_unrecognized_removal_preserves_files(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    (tmp_path / "installation.json").write_text('{"marker": "other"}')
    with pytest.raises(ValueError, match="not a qPlot"):
        desktop.remove(tmp_path)
    assert tmp_path.exists()


def test_bootstrap_outside_repository(tmp_path):
    """An isolated interpreter must find the install and supervise exact argv."""
    from qplot import _desktop_bootstrap

    resources = tmp_path / "launcher Ω space.app/Contents/Resources"
    resources.mkdir(parents=True)
    interpreter = resources.parent / "MacOS/qPlot"
    interpreter.parent.mkdir()
    interpreter.touch()
    shutil.copyfile(_desktop_bootstrap.__file__, resources / "bootstrap.py")
    # A tiny installed-package stand-in records the production entry-point call.
    # -I ignores PYTHONPATH, so put the fixture in a temporary venv's package
    # directory instead of relying on the repository working directory.
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
        f"def run():\n Path({str(output)!r}).write_text(json.dumps([sys.argv[1:], os.environ['MPLCONFIGDIR'], os.environ.get('QPLOT_DESKTOP_BUNDLE')]))\n return 0\n"
    )
    (resources / "installation.json").write_text(json.dumps({
        "prefix": str(environment), "package": str(package / "__init__.py"), "marker": desktop._MARKER,
    }))
    for args in ([], [str(tmp_path / "測定 O'Brien $data.db")]):
        subprocess.run([str(python), "-I", str(resources / "bootstrap.py"), *args], cwd=tmp_path,
                       env=dict(os.environ, HOME=str(tmp_path), LOCALAPPDATA=str(tmp_path)), check=True, timeout=15)
        actual, cache, bundle = json.loads(output.read_text())
        assert actual == args
        assert Path(cache).is_dir()
        if sys.platform == "darwin":
            assert Path(bundle) == resources.parents[1]

    # Capture native error text without opening modal dialogs in automation.
    error_file = tmp_path / "error.txt"
    driver = resources / "error_driver.py"
    driver.write_text(
        "from pathlib import Path\nimport sys\n"
        f"scope = {{'__name__': 'payload', '__file__': {str(resources / 'bootstrap.py')!r}}}\n"
        "exec(compile(Path(scope['__file__']).read_text(encoding='utf-8'), scope['__file__'], 'exec'), scope)\n"
        f"scope['show_error'] = lambda text: Path({str(error_file)!r}).write_text(text, encoding='utf-8')\n"
        "raise SystemExit(scope['main']())\n",
        encoding="utf-8",
    )
    import sqlite3

    database = Path(cache) / "fontlist.json"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE measurement (value)")
    before = database.read_bytes()
    failure_env = dict(os.environ, HOME=str(tmp_path), LOCALAPPDATA=str(tmp_path))
    failure = subprocess.run([str(python), "-I", str(driver)], cwd=tmp_path, env=failure_env, timeout=15)
    assert failure.returncode == 79
    assert "SQLite database" in error_file.read_text(encoding="utf-8")
    assert database.read_bytes() == before
    database.unlink()  # Explicit synthetic fixture cleanup.
    shutil.rmtree(package)
    failure = subprocess.run([str(python), "-I", str(driver)], cwd=tmp_path, env=failure_env, timeout=15)
    assert failure.returncode == 79
    assert "No module named 'qplot'" in error_file.read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS applet tools")
def test_native_mac_compilation(tmp_path, isolated_desktop):
    unassociated = tmp_path / "unassociated.app"
    desktop.setup(unassociated)
    with (unassociated / "Contents/Info.plist").open("rb") as stream:
        assert "CFBundleDocumentTypes" not in plistlib.load(stream)
    desktop.remove(unassociated)
    destination = tmp_path / "qPlot Ω.app"
    desktop.setup(destination, associations=True)
    subprocess.run(["/usr/bin/codesign", "--verify", str(destination)], check=True)
    with (destination / "Contents/Info.plist").open("rb") as stream:
        properties = plistlib.load(stream)
    assert properties["CFBundleIconFile"] == "qplot.icns"
    assert (destination / "Contents/Resources" / properties["CFBundleIconFile"]).read_bytes() == desktop.icon_path("qplot.icns").read_bytes()
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
        subprocess.run(["/usr/bin/open", "-W", "-n", "-a", str(isolated_desktop / "qPlot.app"), *args],
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
    desktop.remove(destination)
    assert not (isolated_desktop / "qPlot.app").is_symlink()


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
def test_native_windows_shortcut_and_arguments(tmp_path, isolated_desktop):
    destination = tmp_path / "qPlot Ω space"
    desktop.setup(destination)
    output = tmp_path / "result.json"
    # Batch mode prevents script errors from opening an invisible modal dialog
    # in the noninteractive Windows runner. Keep the real WScript parser.
    (destination / "bootstrap.py").write_text(
        "import json, sys\nfrom pathlib import Path\n"
        f"Path({str(output)!r}).write_text(json.dumps(sys.argv[1:]), encoding='utf-8')\n",
        encoding="utf-8",
    )
    for args in ([], [str(tmp_path / "測定 O'Brien %data & x.db")]):
        result = subprocess.run(
            ["wscript.exe", "//B", "//nologo", str(destination / "launch.vbs"), *args],
            cwd=tmp_path, capture_output=True, timeout=20,
        )
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert output.is_file(), (result.stdout, result.stderr)
        assert json.loads(output.read_text(encoding="utf-8")) == args
        output.unlink()
    shortcut = destination / "qPlot.lnk"
    assert shortcut.is_file()
    desktop_link = isolated_desktop / "qPlot.lnk"
    assert desktop_link.read_bytes() == shortcut.read_bytes()
    # Load the saved Desktop link through the Unicode shell interface used by
    # Explorer. Unlike WScript.Shell.CreateShortcut, this explicitly requires
    # loading an existing link and preserves Unicode throughout inspection.
    import ctypes
    import uuid

    pointer = ctypes.c_void_p
    hresult = ctypes.c_long
    ole = ctypes.WinDLL("ole32")
    ole.CoInitializeEx.argtypes = [pointer, ctypes.c_uint32]
    ole.CoInitializeEx.restype = hresult
    ole.CoCreateInstance.argtypes = [pointer, pointer, ctypes.c_uint32, pointer,
                                    ctypes.POINTER(pointer)]
    ole.CoCreateInstance.restype = hresult
    ole.CoUninitialize.argtypes = []
    ole.CoUninitialize.restype = None

    def guid(value):
        return ctypes.create_string_buffer(uuid.UUID(value).bytes_le)

    def check(result):
        assert result >= 0, f"Shell-link inspection failed: HRESULT 0x{result & 0xffffffff:08x}"

    def method(interface, slot, *arguments):
        vtable = ctypes.cast(interface, ctypes.POINTER(ctypes.POINTER(pointer))).contents
        return ctypes.WINFUNCTYPE(hresult, pointer, *arguments)(vtable[slot])

    initialized = ole.CoInitializeEx(None, 2)
    if initialized != -2147417850:  # RPC_E_CHANGED_MODE: reuse the caller's apartment.
        check(initialized)
    link, persist = pointer(), pointer()
    try:
        check(ole.CoCreateInstance(
            guid("00021401-0000-0000-c000-000000000046"), None, 1,
            guid("000214f9-0000-0000-c000-000000000046"), ctypes.byref(link),
        ))
        check(method(link, 0, pointer, ctypes.POINTER(pointer))(
            link, guid("0000010b-0000-0000-c000-000000000046"), ctypes.byref(persist),
        ))
        # IPersistFile::Load must succeed; unlike CreateShortcut it cannot
        # silently create an empty link when the existing filename is lost.
        check(method(persist, 5, ctypes.c_wchar_p, ctypes.c_uint32)(
            persist, str(desktop_link), 0,  # STGM_READ
        ))
        icon_file = ctypes.create_unicode_buffer(32768)
        icon_index = ctypes.c_int(-1)
        check(method(link, 16, ctypes.c_wchar_p, ctypes.c_int,
                     ctypes.POINTER(ctypes.c_int))(
            link, icon_file, len(icon_file), ctypes.byref(icon_index),
        ))
        assert Path(icon_file.value) == destination / "qplot.ico"
        assert icon_index.value == 0
    finally:
        for interface in (persist, link):
            if interface:
                method(interface, 2)(interface)  # IUnknown::Release
        if initialized in (0, 1):
            ole.CoUninitialize()
    # Also execute the shortcut itself: its saved arguments must preserve the
    # Unicode launcher path, not just the directly invoked VBS script above.
    import time

    os.startfile(desktop_link)
    deadline = time.monotonic() + 10
    while not output.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert json.loads(output.read_text(encoding="utf-8")) == []
    desktop.remove(destination)
    assert not desktop_link.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows registry")
@pytest.mark.parametrize("missing_icon_key", [False, True])
def test_windows_registration_roundtrip(tmp_path, monkeypatch, missing_icon_key):
    import uuid
    import winreg

    # Avoid touching any real qPlot registration on a developer's machine.
    progid = "qPlot.Test." + uuid.uuid4().hex
    monkeypatch.setattr(associations, "_PROGID", progid)
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
            assert winreg.QueryValue(key, "") == associations._windows_command(destination)
        icon_location = f'"{destination / "qplot-database.ico"}",0'
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base + "\\" + progid + r"\DefaultIcon") as key:
            assert winreg.QueryValue(key, "") == icon_location
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base + "\\" + progid + r"\Application") as key:
            assert winreg.QueryValueEx(key, "ApplicationIcon")[0] == f'"{destination / "qplot.ico"}",0'
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, extension + r"\OpenWithProgids") as key:
            assert winreg.QueryValueEx(key, progid)[0] == ""
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, extension) as key:
            assert winreg.QueryValue(key, "") == (default_before or "")
        with pytest.raises(ValueError, match="Another qPlot launcher"):
            desktop._windows_associations(tmp_path / "other launcher")
        desktop._windows_associations(tmp_path / "other launcher", remove=True)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base + "\\" + progid):
            pass  # Other installations cannot remove this launcher's entry.
        if missing_icon_key:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, base + "\\" + progid + r"\DefaultIcon")
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
    with closing(sqlite3.connect(database)) as connection, connection:
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
