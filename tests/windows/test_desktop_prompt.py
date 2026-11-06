from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PyQt6 import QtCore, QtWidgets

from qplot.windows import _desktop_prompt as prompt_ui


@pytest.fixture
def invitation(qapplication, monkeypatch, tmp_path):
    settings_path = str(tmp_path / "invitation.ini")
    monkeypatch.setattr(prompt_ui, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(prompt_ui, "_already_installed", lambda: False)
    monkeypatch.setattr(prompt_ui, "_prompt_settings", lambda: QtCore.QSettings(
        settings_path, QtCore.QSettings.Format.IniFormat,
    ))
    parent = QtWidgets.QWidget()
    yield parent
    parent.close()
    parent.deleteLater()


@pytest.mark.parametrize("choice", ["Set up…", "Don’t ask again", "Later", None])
def test_choice_persists_only_when_answered(invitation, monkeypatch, choice):
    from qplot.windows import _help

    show_setup = Mock()
    monkeypatch.setattr(_help, "show_desktop_integration", show_setup)
    prompt = prompt_ui.show_first_launch_invitation(invitation)
    assert prompt is not None
    if choice is None:
        prompt.close()
    else:
        next(button for button in prompt.buttons() if button.text() == choice).click()
    assert show_setup.call_count == (1 if choice == "Set up…" else 0)
    next_prompt = prompt_ui.show_first_launch_invitation(invitation)
    if choice in ("Set up…", "Don’t ask again"):
        assert next_prompt is None
    else:
        assert next_prompt is not None
        next_prompt.close()


def test_existing_installation_suppresses_future_invitations(invitation, monkeypatch):
    monkeypatch.setattr(prompt_ui, "_already_installed", lambda: True)
    assert prompt_ui.show_first_launch_invitation(invitation) is None
    monkeypatch.setattr(prompt_ui, "_already_installed", lambda: False)
    assert prompt_ui.show_first_launch_invitation(invitation) is None


def test_unsupported_platform_has_no_prompt_or_settings(invitation, monkeypatch):
    monkeypatch.setattr(prompt_ui, "sys", SimpleNamespace(platform="linux"))
    settings = Mock()
    monkeypatch.setattr(prompt_ui, "_prompt_settings", settings)
    assert prompt_ui.show_first_launch_invitation(invitation) is None
    settings.assert_not_called()


def test_headless_startup_does_not_schedule_prompt(invitation, monkeypatch):
    show = Mock()
    monkeypatch.setattr(prompt_ui, "show_first_launch_invitation", show)
    prompt_ui.schedule_first_launch_invitation(invitation)
    QtWidgets.QApplication.processEvents()
    show.assert_not_called()


def test_interactive_startup_schedules_one_invitation(invitation, monkeypatch):
    show = Mock()
    monkeypatch.setattr(prompt_ui, "show_first_launch_invitation", show)
    monkeypatch.setattr(QtWidgets.QApplication, "platformName", lambda: "cocoa")
    prompt_ui.schedule_first_launch_invitation(invitation)
    show.assert_not_called()
    QtWidgets.QApplication.processEvents()
    QtWidgets.QApplication.processEvents()
    show.assert_called_once_with(invitation)


@pytest.mark.parametrize("contents, expected", [
    ('{"marker": "not-qplot"}', False), ('[]', False), ('invalid', False),
])
def test_unowned_or_invalid_installation_does_not_suppress(monkeypatch, tmp_path, contents, expected):
    monkeypatch.setattr(prompt_ui, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(prompt_ui, "default_destination", lambda: tmp_path)
    (tmp_path / "installation.json").write_text(contents)
    assert prompt_ui._already_installed() is expected
