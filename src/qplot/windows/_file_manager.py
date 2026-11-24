"""File-manager integration for exports, generated files and diagnostic logs."""

import os
import subprocess
import sys

from PyQt6 import QtCore
from PyQt6.QtGui import QDesktopServices


def reveal_file_in_file_manager(file_path):
    """Select the file in the system file manager when supported."""
    file_path = os.path.abspath(file_path)
    if sys.platform == "win32":
        try:
            explorer = os.path.join(os.environ["SystemRoot"], "explorer.exe")
            os.startfile(explorer, arguments=f'/select,"{file_path}"')
        except OSError:
            return False
        return True

    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["open", "-R", file_path],
                capture_output=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    if sys.platform.startswith("linux"):
        uri = QtCore.QUrl.fromLocalFile(file_path).toString(QtCore.QUrl.ComponentFormattingOption.FullyEncoded)
        try:
            result = subprocess.run(
                ["busctl", "--user", "--timeout=2s", "call", "org.freedesktop.FileManager1",
                 "/org/freedesktop/FileManager1", "org.freedesktop.FileManager1", "ShowItems",
                 "ass", "1", uri, ""],
                capture_output=True, check=False, timeout=3,
            )
            if result.returncode == 0:
                return True
        except (OSError, subprocess.TimeoutExpired):
            pass

    folder = os.path.dirname(file_path)
    return QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(folder))
