"""Owned per-user Windows Open With registration and transactional rollback."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_PROGID = "qPlot.Database"


class WindowsAssociationRollbackError(OSError):
    """Registry restoration failed; retain the launcher for a later removal."""


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


