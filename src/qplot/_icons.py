"""Packaged desktop artwork; importing paths does not initialise Qt."""

from pathlib import Path


def icon_path(filename: str) -> Path:
    """Locate an icon in an installed wheel or editable checkout."""
    return Path(__file__).parent / "assets" / filename


def application_icon():
    """Use native small-size artwork plus a high-resolution Dock image."""
    from PyQt6.QtCore import QSize
    from PyQt6.QtGui import QIcon

    icon = QIcon(str(icon_path("qplot.ico")))
    icon.addFile(str(icon_path("qplot.png")), QSize(1024, 1024))
    return icon
