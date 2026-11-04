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

import qplot._desktop_bootstrap
from qplot._auxiliary_paths import ensure_safe_auxiliary_path
from qplot._desktop_shortcut import create_desktop_shortcut, remove_desktop_shortcut
from qplot._desktop_sidecars import (
    install_sidecar_icons,
    prepare_sidecar_icons,
    remove_sidecar_icons,
)
from qplot._icons import icon_path

_MARKER = "qplot-desktop-launcher-v1"
_PROGID = "qPlot.Database"
_DOCUMENT_ICONS = ("qplot-database", "sqlite-wal", "sqlite-shm")
_LSREGISTER = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/"
               "LaunchServices.framework/Support/lsregister")


def _mac_registration(destination: Path, *, remove: bool = False) -> None:
    """Publish the completed bundle, or retire its declarations before deletion."""
    subprocess.run([_LSREGISTER, "-u" if remove else "-f", str(destination)],
                   check=True, timeout=30)


class WindowsAssociationRollbackError(OSError):
    """Registry restoration failed; retain the launcher for a later removal."""


def default_destination() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Applications/qPlot.app"
    if sys.platform == "win32":
        return Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/qPlot"
    raise ValueError("Desktop launcher setup supports macOS and Windows only.")


def _mac_launcher(destination: Path, python: Path, associations: bool, *,
                  compile_timeout: float | None = None) -> Path:
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
        subprocess.run(["/usr/bin/osacompile", "-o", str(destination), str(source_path)],
                       check=True, timeout=compile_timeout)
    resources = script.parent
    shutil.copyfile(icon_path("qplot.icns"), resources / "qplot.icns")
    if associations:
        for name in _DOCUMENT_ICONS:
            shutil.copyfile(icon_path(f"{name}.icns"), resources / f"{name}.icns")
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
    properties.update(CFBundleIdentifier="uk.ac.lancaster.qplot.launcher", CFBundleName="qPlot",
                      CFBundleIconFile="qplot.icns")
    # An asset-catalog name takes precedence over the standalone icon file.
    properties.pop("CFBundleIconName", None)
    # osacompile droplets default to accepting every file type. Remove that
    # template declaration even when registration was not requested.
    properties.pop("CFBundleDocumentTypes", None)
    properties.pop("UTImportedTypeDeclarations", None)
    if associations:
        properties["CFBundleDocumentTypes"] = [{
            "CFBundleTypeName": "QCoDeS database", "CFBundleTypeExtensions": ["db"],
            "CFBundleTypeRole": "Viewer", "LSHandlerRank": "Alternate",
            "CFBundleTypeIconFile": "qplot-database.icns",
        }]
        # Imported UTIs let Finder discover these icons. Role/rank None publish
        # type information without offering qPlot as an opener for sidecars.
        properties["UTImportedTypeDeclarations"] = []
        for extension, name, description in (
            ("db-wal", "sqlite-wal", "SQLite write-ahead log"),
            ("db-shm", "sqlite-shm", "SQLite shared-memory index"),
        ):
            identifier = f"uk.ac.lancaster.qplot.{name}"
            properties["UTImportedTypeDeclarations"].append({
                "UTTypeIdentifier": identifier,
                "UTTypeConformsTo": ["public.data"],
                "UTTypeDescription": description,
                "UTTypeIconFile": f"{name}.icns",
                "UTTypeTagSpecification": {"public.filename-extension": [extension]},
            })
            properties["CFBundleDocumentTypes"].append({
                "CFBundleTypeName": description,
                "CFBundleTypeExtensions": [extension],
                "LSItemContentTypes": [identifier],
                "CFBundleTypeRole": "None", "LSHandlerRank": "None",
                "CFBundleTypeIconFile": f"{name}.icns",
            })
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
    shutil.copyfile(icon_path("qplot.ico"), destination / "qplot.ico")
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
        # IShellLinkW::SetIconLocation takes a UTF-16 path and resource index.
        check(method(link, 17, hresult, ctypes.c_wchar_p, ctypes.c_int)(
            link, str(destination / "qplot.ico"), 0,
        ))
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
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, application + r"\DefaultIcon")
        except FileNotFoundError:
            # Launchers created before icon support have no DefaultIcon key.
            pass
        for suffix in (r"\Application", r"\shell\open\command", r"\shell\open", r"\shell", ""):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, application + suffix)
            except FileNotFoundError:
                # A failed setup can retain an owned but incomplete ProgID.
                pass
    else:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, command_key) as key:
                if winreg.QueryValueEx(key, "")[0] != _windows_command(destination):
                    raise ValueError("Another qPlot launcher owns Open With. Remove it before registering this one.")
        except FileNotFoundError:
            # No existing registry command means this launcher can create its own
            # association.
            pass
        # Write ownership first and restore it last, so a failed rollback can
        # still be recognized by remove() without touching another launcher.
        updates = (
            (command_key, "", _windows_command(destination)),
            (application, "", "qPlot QCoDeS viewer"),
            (application + r"\DefaultIcon", "", f'"{destination / "qplot-database.ico"}",0'),
            (application + r"\Application", "ApplicationName", "qPlot"),
            (application + r"\Application", "ApplicationDescription", "QCoDeS database viewer"),
            (application + r"\Application", "ApplicationIcon", f'"{destination / "qplot.ico"}",0'),
            (open_with, _PROGID, ""),
        )

        def read_value(path, name):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
                    return winreg.QueryValueEx(key, name)
            except FileNotFoundError:
                return None

        # Complete every read before the first mutation. Backups remain in
        # native registry types, including binary and expandable-string values.
        previous = {(path, name): read_value(path, name) for path, name, _value in updates}
        created_keys = set()
        for path, _name, _value in updates:
            while path != base:
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path):
                        pass
                except FileNotFoundError:
                    created_keys.add(path)
                path = path.rsplit("\\", 1)[0]
        attempted = []
        try:
            for path, name, value in updates:
                attempted.append((path, name, value))
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as key:
                    winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        except OSError as install_error:
            errors = []
            for path, name, value in reversed(attempted):
                if path == command_key and errors:
                    continue  # Retain ownership until failed restoration is repaired.
                try:
                    if read_value(path, name) != (value, winreg.REG_SZ):
                        continue  # A subsequent writer owns the value now.
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as key:
                        saved = previous[path, name]
                        if saved is None:
                            winreg.DeleteValue(key, name)
                        else:
                            data, kind = saved
                            winreg.SetValueEx(key, name, 0, kind, data)
                except OSError as error:
                    errors.append(error)
            for path in sorted(created_keys, key=lambda item: item.count("\\"), reverse=True):
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
                        children, values, _modified = winreg.QueryInfoKey(key)
                    if children == 0 and values == 0:
                        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
                except FileNotFoundError:
                    pass
                except OSError as error:
                    errors.append(error)
            if errors:
                raise WindowsAssociationRollbackError(
                    "Could not restore qPlot's file registration; keep the launcher and retry removal."
                ) from install_error
            raise


def _notify_windows_shell() -> None:
    """Refresh Explorer after the complete set of per-user icon changes."""
    if sys.platform != "win32":
        raise ValueError("Windows icon refresh requires Windows.")
    import ctypes

    ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)


def setup(destination: Path, *, associations: bool = False, desktop_shortcut: bool = True) -> None:
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
    sidecar_icons: list[dict] = []
    icons_attempted = False
    registered = False
    mac_registration_attempted = False
    shortcut_record = None
    try:
        if sys.platform == "darwin":
            resources = _mac_launcher(destination, python, associations)
            from qplot._macos_identity import install_interpreter

            install_interpreter(destination)
        else:
            destination.mkdir()
            resources = _windows_launcher(destination, python)
            if associations:
                for name in _DOCUMENT_ICONS:
                    shutil.copyfile(icon_path(f"{name}.ico"), resources / f"{name}.ico")
        shutil.copyfile(qplot._desktop_bootstrap.__file__, resources / "bootstrap.py")
        record = {"marker": _MARKER, "python": str(python), "prefix": sys.prefix,
                  "package": str(Path(qplot.__file__).absolute()), "associations": associations}
        if desktop_shortcut:
            shortcut_record = create_desktop_shortcut(destination)
            if shortcut_record is not None:
                record["desktop_shortcut"] = shortcut_record
        if associations and sys.platform == "win32":
            sidecar_icons = prepare_sidecar_icons(destination)
            record["sidecar_icons"] = sidecar_icons
        # Save prior icon values before changing the registry, so a failed
        # rollback still leaves a removable launcher with its recovery record.
        (resources / "installation.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        if sys.platform == "darwin":
            subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(destination)], check=True)
            mac_registration_attempted = True
            _mac_registration(destination)
        elif associations:
            _windows_associations(destination)
            registered = True
            icons_attempted = True
            install_sidecar_icons(destination, records=sidecar_icons)
            _notify_windows_shell()
    except Exception as error:
        # If restoration fails, retain the launcher and its record for removal.
        if mac_registration_attempted:
            _mac_registration(destination, remove=True)
        if icons_attempted:
            remove_sidecar_icons(sidecar_icons)
        if registered:
            _windows_associations(destination, remove=True)
        if icons_attempted or registered:
            _notify_windows_shell()
        remove_desktop_shortcut(shortcut_record)
        if isinstance(error, WindowsAssociationRollbackError):
            raise
        if destination.exists():
            shutil.rmtree(destination)
        raise
    print(f"Created {destination}")
    if shortcut_record is not None:
        print(f"Created desktop shortcut {shortcut_record['path']}")


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
    if sys.platform == "darwin":
        _mac_registration(destination, remove=True)
    remove_desktop_shortcut(record.get("desktop_shortcut"))
    if sys.platform == "win32" and record["associations"]:
        remove_sidecar_icons(record.get("sidecar_icons", []))
        _windows_associations(destination, remove=True)
        _notify_windows_shell()
    shutil.rmtree(destination)
    print(f"Removed {destination}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["setup", "remove"])
    parser.add_argument("--destination", type=Path, help="Custom .app path (macOS) or launcher directory (Windows)")
    parser.add_argument("--file-associations", action="store_true", help="Offer qPlot in Open With for .db files and add database/WAL/SHM icons; never change the default opener")
    parser.add_argument("--no-desktop-shortcut", action="store_true", help="Create the launcher without adding a desktop shortcut")
    options = parser.parse_args(argv)
    try:
        destination = options.destination or default_destination()
        if options.action == "setup":
            setup(destination, associations=options.file_associations,
                  desktop_shortcut=not options.no_desktop_shortcut)
        else:
            remove(destination)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"qPlot desktop setup: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
