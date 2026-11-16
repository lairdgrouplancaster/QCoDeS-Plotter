"""File-manager integration shared by diagnostics and generated-file actions."""

import os
import subprocess
import sys

from PyQt6 import QtCore
from PyQt6.QtGui import QDesktopServices


def reveal_file_in_file_manager(file_path):
    """Reveal a generated file in Finder, or open its folder elsewhere."""
    file_path = os.path.abspath(file_path)
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

    folder = os.path.dirname(file_path)
    return QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(folder))
