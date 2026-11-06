"""A per-user invitation to configure optional desktop integration."""
import json
import sys

from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from qplot._icons import application_icon
from qplot.desktop import _MARKER, default_destination
from qplot.diagnostics import log_exception


def _prompt_settings() -> QtCore.QSettings:
    # Keep this per-user choice separate from viewer preferences and their resets.
    return QtCore.QSettings(
        QtCore.QSettings.Format.IniFormat, QtCore.QSettings.Scope.UserScope,
        "qPlot", "DesktopIntegration",
    )


def _remember_choice(settings: QtCore.QSettings) -> None:
    settings.setValue("invitationAnswered", True)
    settings.sync()
    if settings.status() != QtCore.QSettings.Status.NoError:
        log_exception("Could not save desktop integration invitation choice",
                      OSError("Desktop integration settings could not be saved"))


def _already_installed() -> bool:
    destination = default_destination()
    resources = destination / "Contents/Resources" if sys.platform == "darwin" else destination
    try:
        record = json.loads((resources / "installation.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(record, dict) and record.get("marker") == _MARKER


def show_first_launch_invitation(parent: qtw.QWidget) -> qtw.QMessageBox | None:
    if sys.platform not in {"darwin", "win32"}:
        return None
    settings = _prompt_settings()
    if settings.value("invitationAnswered", False, type=bool):
        return None
    if _already_installed():
        _remember_choice(settings)
        return None

    prompt = qtw.QMessageBox(parent)
    prompt.setObjectName("desktopIntegrationInvitation")
    prompt.setWindowTitle("Set up qPlot on your desktop")
    prompt.setText("Add qPlot to your desktop and register database icons?")
    prompt.setInformativeText(
        "Choose which shortcuts and file icons to install. Your default file opener "
        "will stay unchanged. You can also do this later from Help → Desktop integration."
    )
    icon = application_icon()
    prompt.setWindowIcon(icon)
    prompt.setIconPixmap(icon.pixmap(QtCore.QSize(64, 64), prompt.devicePixelRatioF()))
    setup = prompt.addButton("Set up…", qtw.QMessageBox.ButtonRole.AcceptRole)
    later = prompt.addButton("Later", qtw.QMessageBox.ButtonRole.RejectRole)
    never = prompt.addButton("Don’t ask again", qtw.QMessageBox.ButtonRole.NoRole)
    prompt.setDefaultButton(later)
    prompt.setEscapeButton(later)
    prompt.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, True)

    def finished(_result: int) -> None:
        choice = prompt.clickedButton()
        if choice in (setup, never):
            _remember_choice(settings)
        if choice is setup:
            from ._help import show_desktop_integration

            show_desktop_integration(parent)

    prompt.finished.connect(finished)
    prompt.open()
    return prompt


def schedule_first_launch_invitation(parent: qtw.QWidget) -> None:
    # Headless automated runs must never show or persist onboarding choices.
    if sys.platform not in {"darwin", "win32"} or qtw.QApplication.platformName() in {"offscreen", "minimal"}:
        return
    timer = QtCore.QTimer(parent)
    timer.setSingleShot(True)
    timer.timeout.connect(lambda: show_first_launch_invitation(parent))
    timer.timeout.connect(timer.deleteLater)
    timer.start(0)
