"""A per-user invitation to configure optional desktop integration."""
import sys

from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from qplot.desktop import default_destination, is_owned_launcher
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
    return is_owned_launcher(default_destination())


def show_first_launch_invitation(parent: qtw.QMainWindow) -> qtw.QWidget | None:
    if sys.platform not in {"darwin", "win32"} or not parent.isVisible():
        return None
    settings = _prompt_settings()
    if settings.value("invitationAnswered", False, type=bool):
        return None
    if _already_installed():
        _remember_choice(settings)
        return None

    bar = parent.statusBar()
    if bar is None:
        return None
    banner = qtw.QWidget(bar)
    banner.setObjectName("desktopIntegrationInvitation")
    row = qtw.QHBoxLayout(banner)
    row.setContentsMargins(2, 0, 2, 0)
    row.addWidget(qtw.QLabel("Add a qPlot desktop launcher?", banner))
    setup = qtw.QPushButton("Set up…", banner)
    dismiss = qtw.QPushButton("Dismiss", banner)
    for button in (setup, dismiss):
        row.addWidget(button)

    def finish(open_setup: bool) -> None:
        _remember_choice(settings)
        bar.removeWidget(banner)
        banner.deleteLater()
        if open_setup:
            from ._help import show_desktop_integration

            show_desktop_integration(parent)

    setup.clicked.connect(lambda: finish(True))
    dismiss.clicked.connect(lambda: finish(False))
    bar.addPermanentWidget(banner)
    return banner


def schedule_first_launch_invitation(parent: qtw.QMainWindow) -> None:
    # Headless automated runs must never show or persist onboarding choices.
    if sys.platform not in {"darwin", "win32"} or qtw.QApplication.platformName() in {"offscreen", "minimal"}:
        return
    if parent.property("qplotDesktopInvitationScheduled"):
        return
    parent.setProperty("qplotDesktopInvitationScheduled", True)
    timer = QtCore.QTimer(parent)
    timer.setSingleShot(True)
    timer.timeout.connect(lambda: show_first_launch_invitation(parent))
    timer.timeout.connect(timer.deleteLater)
    timer.start(0)
