"""Per-user desktop setup for an existing installation; no database access."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from qplot import _desktop_bootstrap
from qplot._auxiliary_paths import ensure_safe_auxiliary_path

_MARKER = "qplot-desktop-launcher-v1"
_PROGID = "qPlot.Database"


def default_destination() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Applications/qPlot.app"
    if sys.platform == "win32":
        return Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/qPlot"
    raise ValueError("Desktop launcher setup supports macOS and Windows only.")


def _mac_launcher(destination: Path, python: Path, associations: bool) -> Path:
    script = destination / "Contents/Resources/launch.sh"
    # AppleScript receives Finder Open Documents events, including on an already
    # running applet. Each file gets its own supervised qPlot process tree.
    source = '''on launchQPlot(arguments)
    set commandText to "/bin/sh " & quoted form of ((POSIX path of (path to me)) & "/Contents/Resources/launch.sh")
    repeat with argument in arguments
        set commandText to commandText & " " & quoted form of (argument as text)
    end repeat
    do shell script (commandText & " >/dev/null 2>&1 &")
end launchQPlot
on run
    launchQPlot({})
end run
on open fileList
    repeat with selectedFile in fileList
        launchQPlot({POSIX path of selectedFile})
    end repeat
end open
'''
    with tempfile.TemporaryDirectory(prefix="qplot-applet-") as temporary:
        source_path = Path(temporary) / "launcher.applescript"
        source_path.write_text(source, encoding="utf-8")
        subprocess.run(["/usr/bin/osacompile", "-o", str(destination), str(source_path)], check=True)
    resources = script.parent
    error = "Python could not start. Check the diagnostic below. If the installation has moved or is missing, remove the launcher and run python -m qplot.desktop setup from the repaired environment."
    access_error = "Access to the Python installation was denied. If it is inside OneDrive or another protected folder, check qPlot in System Settings > Privacy & Security > Files and Folders, or create a fresh installation in a local folder outside OneDrive, Documents, and Desktop. Recreating the same launcher does not fix denied access."
    script.write_text(
        "#!/bin/sh\n"
        'startup_log=$(/usr/bin/mktemp -t qplot-startup) || exit 1\n'
        'exec 2>"$startup_log"\n'
        'show_startup_error() {\n'
        '  detail=$(/usr/bin/tail -c 2000 "$startup_log")\n'
        '  /usr/bin/osascript -e "on run argv" '
        "-e 'display alert \"qPlot could not start\" message ((item 1 of argv) & return & return & (item 2 of argv) & return & return & \"Diagnostic file: \" & (item 3 of argv))' "
        '-e "end run" "$1" "$detail" "$startup_log"\n'
        '}\n'
        f"if [ ! -x {shlex.quote(str(python))} ]; then\n"
        f"  printf '%s\\n' {shlex.quote('Cannot access Python executable: ' + str(python))} >&2\n"
        f"  show_startup_error {shlex.quote(error)}\n"
        "  exit 1\nfi\n"
        f'{shlex.quote(str(python))} -I "$(/usr/bin/dirname "$0")/bootstrap.py" "$@"\n'
        "status=$?\n"
        'if [ "$status" -ne 0 ] && [ "$status" -ne 79 ]; then\n'
        "  if /usr/bin/grep -Eq 'Operation not permitted|Permission denied' \"$startup_log\"; then\n"
        f"    show_startup_error {shlex.quote(access_error)}\n"
        "  else\n"
        f"    show_startup_error {shlex.quote(error)}\n"
        "  fi\n"
        'fi\nexit "$status"\n', encoding="utf-8",
    )
    info = destination / "Contents/Info.plist"
    with info.open("rb") as stream:
        properties = plistlib.load(stream)
    properties.update(CFBundleIdentifier="uk.ac.lancaster.qplot.launcher", CFBundleName="qPlot")
    # osacompile droplets default to accepting every file type. Remove that
    # template declaration even when registration was not requested.
    properties.pop("CFBundleDocumentTypes", None)
    if associations:
        properties["CFBundleDocumentTypes"] = [{
            "CFBundleTypeName": "QCoDeS database", "CFBundleTypeExtensions": ["db"],
            "CFBundleTypeRole": "Viewer", "LSHandlerRank": "Alternate",
        }]
    with info.open("wb") as stream:
        plistlib.dump(properties, stream)
    return resources


def _vbs_string(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _windows_launcher(destination: Path, python: Path) -> Path:
    # WScript supplies a native error even when Python no longer exists. It
    # passes arguments directly to CreateProcess, with no cmd.exe expansion.
    bootstrap = destination / "bootstrap.py"
    command = subprocess.list2cmdline([str(python), "-I", str(bootstrap)])
    script = f'''Option Explicit
Dim shell, fs, command, argument, status
Set shell = CreateObject("WScript.Shell")
Set fs = CreateObject("Scripting.FileSystemObject")
If Not fs.FileExists({_vbs_string(str(python))}) Then
    MsgBox "Python is missing or moved. Remove the old launcher, then run python -m qplot.desktop setup from the installed environment.", 16, "qPlot could not start"
    WScript.Quit 1
End If
command = {_vbs_string(command)}
For Each argument In WScript.Arguments
    command = command & " " & Chr(34) & argument & Chr(34)
Next
On Error Resume Next
status = shell.Run(command, 0, True)
If Err.Number <> 0 Or (status <> 0 And status <> 79) Then
    MsgBox "Could not start Python. Remove the old launcher, then run python -m qplot.desktop setup from the installed environment.", 16, "qPlot could not start"
End If
'''
    # Windows Script Host requires a BOM for Unicode source text.
    (destination / "launch.vbs").write_text(script, encoding="utf-16")
    _windows_shortcut(destination)
    return destination


def _windows_shortcut(destination: Path) -> None:
    """Use the Unicode shell-link interfaces, independent of the ANSI code page.

    https://learn.microsoft.com/windows/win32/api/shobjidl_core/nn-shobjidl_core-ishelllinkw
    https://learn.microsoft.com/windows/win32/api/objidl/nf-objidl-ipersistfile-save
    """
    import ctypes
    import uuid

    # These ctypes APIs exist only on Windows; analysis also runs on Linux.
    ole = ctypes.WinDLL("ole32")  # type: ignore[attr-defined]
    hresult = ctypes.c_long
    pointer = ctypes.c_void_p
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
        if result < 0:
            raise OSError(f"Windows shortcut creation failed (HRESULT 0x{result & 0xffffffff:08x})")

    def method(interface, slot, result_type, *arguments):
        vtable = ctypes.cast(interface, ctypes.POINTER(ctypes.POINTER(pointer))).contents
        return ctypes.WINFUNCTYPE(result_type, pointer, *arguments)(vtable[slot])  # type: ignore[attr-defined]

    initialized = ole.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED
    if initialized != -2147417850:  # RPC_E_CHANGED_MODE: use the caller's apartment.
        check(initialized)
    link, persist = pointer(), pointer()
    try:
        check(ole.CoCreateInstance(
            guid("00021401-0000-0000-c000-000000000046"), None, 1,
            guid("000214f9-0000-0000-c000-000000000046"), ctypes.byref(link),
        ))
        # IShellLinkW slots: SetPath, SetArguments, SetWorkingDirectory,
        # SetDescription. Every string crosses COM as UTF-16, never ANSI.
        for slot, value in (
            (20, str(Path(os.environ["SystemRoot"]) / "System32/wscript.exe")),
            (11, subprocess.list2cmdline(["//nologo", str(destination / "launch.vbs")])),
            (9, os.environ["USERPROFILE"]),
            (7, "qPlot QCoDeS viewer"),
        ):
            check(method(link, slot, hresult, ctypes.c_wchar_p)(link, value))
        check(method(link, 0, hresult, pointer, ctypes.POINTER(pointer))(
            link, guid("0000010b-0000-0000-c000-000000000046"), ctypes.byref(persist),
        ))
        shortcut = destination / "qPlot.lnk"
        ensure_safe_auxiliary_path(shortcut)
        check(method(persist, 6, hresult, ctypes.c_wchar_p, ctypes.c_int)(
            persist, str(shortcut), 1,
        ))
    finally:
        for interface in (persist, link):
            if interface:
                method(interface, 2, ctypes.c_ulong)(interface)  # IUnknown::Release
        if initialized in (0, 1):
            ole.CoUninitialize()


def _windows_command(destination: Path) -> str:
    return subprocess.list2cmdline([
        str(Path(os.environ["SystemRoot"]) / "System32/wscript.exe"),
        "//nologo", str(destination / "launch.vbs"),
    ]) + ' "%1"'


def _windows_associations(destination: Path, *, remove: bool = False) -> None:
    if sys.platform != "win32":
        raise ValueError("Windows file registration requires Windows.")
    import winreg

    base = r"Software\Classes"
    application = base + "\\" + _PROGID
    command_key = application + r"\shell\open\command"
    open_with = base + r"\.db\OpenWithProgids"
    if remove:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, command_key) as key:
                if winreg.QueryValueEx(key, "")[0] != _windows_command(destination):
                    return  # A different launcher owns this registration now.
        except FileNotFoundError:
            return
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, open_with, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, _PROGID)
        except FileNotFoundError:
            # Removing an already absent per-user association is idempotent.
            pass
        for suffix in (r"\shell\open\command", r"\shell\open", r"\shell", r"\Application", ""):
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, application + suffix)
    else:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, command_key) as key:
                if winreg.QueryValueEx(key, "")[0] != _windows_command(destination):
                    raise ValueError("Another qPlot launcher owns Open With. Remove it before registering this one.")
        except FileNotFoundError:
            # No existing registry command means this launcher can create its own
            # association.
            pass
        for path, name, value in (
            (application, "", "qPlot QCoDeS viewer"),
            (application + r"\Application", "ApplicationName", "qPlot"),
            (application + r"\Application", "ApplicationDescription", "QCoDeS database viewer"),
            (command_key, "", _windows_command(destination)),
            (open_with, _PROGID, ""),
        ):
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as key:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    # Notify Explorer of the per-user association change, without setting a default.
    import ctypes

    ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)


def setup(destination: Path, *, associations: bool = False) -> None:
    import qplot

    destination = destination.expanduser().absolute()
    if sys.platform not in {"darwin", "win32"}:
        raise ValueError("Desktop launcher setup supports macOS and Windows only.")
    if sys.platform == "darwin" and destination.suffix != ".app":
        raise ValueError("The macOS destination must end in .app")
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"{destination} already exists. Remove the old launcher first.")
    # Do not resolve the venv executable symlink to the base interpreter.
    python = Path(sys.executable).absolute()
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        if sys.platform == "darwin":
            resources = _mac_launcher(destination, python, associations)
        else:
            destination.mkdir()
            resources = _windows_launcher(destination, python)
        shutil.copyfile(_desktop_bootstrap.__file__, resources / "bootstrap.py")
        record = {"marker": _MARKER, "python": str(python), "prefix": sys.prefix,
                  "package": str(Path(qplot.__file__).absolute()), "associations": associations}
        (resources / "installation.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        if sys.platform == "darwin":
            subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(destination)], check=True)
        if associations and sys.platform == "win32":
            _windows_associations(destination)
    except Exception:
        if destination.exists():
            shutil.rmtree(destination)
        raise
    print(f"Created {destination}")


def remove(destination: Path) -> None:
    destination = destination.expanduser().absolute()
    resources = destination / "Contents/Resources" if sys.platform == "darwin" else destination
    if destination.is_symlink():
        raise ValueError("Refusing to remove a symlink launcher.")
    record = json.loads((resources / "installation.json").read_text(encoding="utf-8"))
    if record.get("marker") != _MARKER:
        raise ValueError("This directory is not a qPlot desktop launcher.")
    # A user may have placed measurement files inside the launcher since setup.
    # Preflight the entire removal before deleting anything or changing registry.
    for entry in destination.rglob("*"):
        if entry.is_symlink():
            raise ValueError("Refusing to remove a launcher containing symlinks.")
        if entry.name.casefold().endswith(("-wal", "-journal", "-shm")):
            raise ValueError("Refusing to remove a launcher containing a database sidecar.")
        if entry.is_file():
            ensure_safe_auxiliary_path(entry)
    if sys.platform == "win32" and record["associations"]:
        _windows_associations(destination, remove=True)
    shutil.rmtree(destination)
    print(f"Removed {destination}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["setup", "remove"])
    parser.add_argument("--destination", type=Path, help="Custom .app path (macOS) or launcher directory (Windows)")
    parser.add_argument("--file-associations", action="store_true", help="Offer qPlot in Open With for .db files; never change the default")
    options = parser.parse_args(argv)
    try:
        destination = options.destination or default_destination()
        if options.action == "setup":
            setup(destination, associations=options.file_associations)
        else:
            remove(destination)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"qPlot desktop setup: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
