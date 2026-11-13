from unittest.mock import Mock

import pytest
from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from qplot.windows import _desktop_integration as desktop_ui
from qplot.windows._help import add_help_menu, show_about


@pytest.fixture
def dialog(qapplication, monkeypatch, tmp_path):
    monkeypatch.setattr(desktop_ui, "default_destination", lambda: tmp_path / "qPlot.app")
    widget = desktop_ui.DesktopIntegrationDialog()
    monkeypatch.setattr(widget.process, "start", Mock())
    yield widget
    widget._set_busy(False)
    widget.close()
    widget.deleteLater()


def test_install_options_and_environment(dialog):
    dialog.shortcut.setChecked(False)
    dialog._start("setup")
    args = dialog.process.start.call_args.args[1]
    assert args[:3] == ["-m", "qplot.desktop", "setup"]
    assert "--file-associations" in args
    assert "--no-desktop-shortcut" in args
    assert not dialog.close_button.isEnabled()
    assert not any(key.startswith(("QPLOT_", "_QPLOT_"))
                   for key in dialog.process.processEnvironment().keys())
    dialog._finished(0, QtCore.QProcess.ExitStatus.NormalExit)
    assert dialog.close_button.isEnabled()
    assert "installed" in dialog.output.toPlainText()


@pytest.mark.parametrize("remove_exit", [0, 1])
def test_update_only_installs_after_successful_removal(dialog, remove_exit):
    desktop_ui.Path(dialog.destination.text()).mkdir()
    dialog._start("setup")
    assert dialog.process.start.call_args.args[1][2] == "remove"
    dialog._finished(remove_exit, QtCore.QProcess.ExitStatus.NormalExit)
    if remove_exit:
        assert dialog.process.start.call_count == 1
        assert not dialog._busy
        assert "failed" in dialog.output.toPlainText()
    else:
        assert dialog.process.start.call_count == 2
        assert dialog.process.start.call_args.args[1][2] == "setup"
        dialog._finished(0, QtCore.QProcess.ExitStatus.NormalExit)


def test_failed_start_allows_retry(dialog):
    dialog._start("remove")
    dialog._error(QtCore.QProcess.ProcessError.FailedToStart)
    assert not dialog._busy
    assert dialog.install_button.isEnabled()
    assert "Could not start" in dialog.output.toPlainText()


def test_settings_and_about_offer_dialog(qapplication, monkeypatch):
    from qplot.windows import _help

    opened = Mock()
    monkeypatch.setattr(_help, "show_desktop_integration", opened)
    window = qtw.QMainWindow()
    add_help_menu(window, include_desktop_integration=True)
    menu = next(a.menu() for a in window.menuBar().actions()
                if a.text().replace("&", "") == "Settings")
    next(a for a in menu.actions() if a.objectName() == "desktopIntegrationAction").trigger()
    opened.assert_called_once_with(window)
    about = show_about(window)
    about.findChild(qtw.QPushButton, "desktopIntegrationButton").click()
    assert opened.call_args.args == (about,)
    about.close()
    window.close()
    window.deleteLater()
