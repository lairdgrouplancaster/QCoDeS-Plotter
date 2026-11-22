"""Preferences edits must not overwrite untouched, schema-valid settings."""

import json
from pathlib import Path

import pytest
from PyQt6 import QtCore, QtTest
from PyQt6 import QtWidgets as qtw

from qplot.windows._preferences import PreferencesDialog
from qplot.windows._window_controls import (
    CONFIRM_CLOSE_ALL_KEY,
    CONFIRM_INCOMPATIBLE_AXES_KEY,
    CONFIRM_QUIT_KEY,
)
from tests.support.windows.preferences_value_preservation import (
    precise_config as precise_config,
)


@pytest.mark.parametrize("control_name, key", [
    ("confirmCloseAllCheck", CONFIRM_CLOSE_ALL_KEY),
    ("confirmQuitCheck", CONFIRM_QUIT_KEY),
    ("confirmIncompatibleAxesCheck", CONFIRM_INCOMPATIBLE_AXES_KEY),
])
def test_confirmation_edits_are_recorded_for_their_own_key(precise_config, control_name, key):
    dialog = PreferencesDialog(precise_config)
    try:
        control = getattr(dialog, control_name)
        control.setChecked(not control.isChecked())
        assert dialog._edited_preference_keys == {key}
        assert dialog.apply_preferences()
        assert precise_config.get(key) == control.isChecked()
        assert dialog._edited_preference_keys == set()
        dialog.load_from_config()
        assert dialog._edited_preference_keys == set()
    finally:
        dialog.close()


@pytest.mark.parametrize("button", ["Apply", "Ok"])
def test_untouched_preferences_preserve_exact_persisted_values(precise_config, button):
    cfg = precise_config
    before = json.loads(Path(cfg.default_file).read_text(encoding="utf-8"))
    dialog = PreferencesDialog(cfg)
    try:
        dialog.buttonBox.button(getattr(qtw.QDialogButtonBox.StandardButton, button)).click()
        assert json.loads(Path(cfg.default_file).read_text(encoding="utf-8")) == before
        assert cfg.config == before
    finally:
        dialog.close()


def test_unrelated_and_repeated_apply_preserve_untouched_values(precise_config):
    cfg = precise_config
    before = dict(cfg.config["runtime_settings"])
    dialog = PreferencesDialog(cfg)
    try:
        dialog.themeCombo.setCurrentIndex(dialog.themeCombo.findData("dark"))
        assert dialog.apply_preferences()
        assert cfg.get("user_preference.theme") == "dark"
        assert cfg.get("user_preference.default_refresh_rate") == 0.049
        assert cfg.get("user_preference.axis_tick_width") == 1.25
        assert cfg.config["runtime_settings"] == before
        dialog.refreshRateSpin.setValue(0.2)
        assert dialog.apply_preferences()
        assert cfg.get("user_preference.default_refresh_rate") == 0.2
        assert dialog.apply_preferences()
        assert cfg.config["runtime_settings"] == before
    finally:
        dialog.close()


def test_explicit_edit_back_to_rounded_value_is_saved(precise_config):
    dialog = PreferencesDialog(precise_config)
    try:
        assert dialog.refreshRateSpin.value() == 0.0
        dialog.refreshRateSpin.setValue(0.1)
        dialog.refreshRateSpin.setValue(0.0)
        assert dialog.apply_preferences()
        assert precise_config.get("user_preference.default_refresh_rate") == 0.0
        assert precise_config.get("user_preference.axis_tick_width") == 1.25
    finally:
        dialog.close()


def test_keyboard_edit_to_same_rounded_value_is_saved(precise_config):
    dialog = PreferencesDialog(precise_config)
    try:
        dialog.show()
        line_edit = dialog.refreshRateSpin.lineEdit()
        assert line_edit is not None
        line_edit.setFocus()
        qtw.QApplication.processEvents()
        QtTest.QTest.keyClick(line_edit, QtCore.Qt.Key.Key_A,
                            QtCore.Qt.KeyboardModifier.ControlModifier)
        QtTest.QTest.keyClicks(line_edit, "0.0")
        dialog.refreshRateSpin.interpretText()
        dialog.buttonBox.button(qtw.QDialogButtonBox.StandardButton.Apply).click()
        assert precise_config.get("user_preference.default_refresh_rate") == 0.0
        assert precise_config.get("user_preference.axis_tick_width") == 1.25
    finally:
        dialog.close()


def test_restore_defaults_saves_even_matching_rounded_value(precise_config, monkeypatch):
    precise_config.update("user_preference.default_refresh_rate", 1.049)
    dialog = PreferencesDialog(precise_config)
    monkeypatch.setattr("qplot.windows._preferences.confirm_action", lambda *args, **kwargs: True)
    try:
        assert dialog.refreshRateSpin.value() == 1.0
        assert dialog.restore_defaults()
        assert dialog.apply_preferences()
        assert precise_config.get("user_preference.default_refresh_rate") == 1.0
        assert precise_config.get("user_preference.axis_tick_width") == 1.25
    finally:
        dialog.close()


def test_cancel_discards_pending_edits_and_reset(precise_config, monkeypatch):
    before = json.loads(Path(precise_config.default_file).read_text(encoding="utf-8"))
    dialog = PreferencesDialog(precise_config)
    monkeypatch.setattr("qplot.windows._preferences.confirm_action", lambda *args, **kwargs: True)
    try:
        assert dialog.restore_defaults()
        dialog.refreshRateSpin.setValue(0.2)
        dialog.buttonBox.button(qtw.QDialogButtonBox.StandardButton.Cancel).click()
        assert precise_config.config == before
        assert json.loads(Path(precise_config.default_file).read_text(encoding="utf-8")) == before
    finally:
        dialog.close()


def test_cancel_keeps_previous_apply_and_untouched_ok_is_quiet(precise_config):
    from qplot.configuration.fonts import FONT_SIZE_KEY

    dialog = PreferencesDialog(precise_config)
    applied = []
    dialog.preferencesApplied.connect(lambda: applied.append(True))
    dialog._accept_preferences()
    assert dialog.result() == qtw.QDialog.DialogCode.Accepted
    assert applied == []
    dialog.show()
    original = dialog.fontSizeSpin.value()
    dialog.fontSizeSpin.setValue(original + 1)
    dialog.apply_button.click()
    dialog.fontSizeSpin.setValue(original + 2)
    dialog.buttonBox.button(qtw.QDialogButtonBox.StandardButton.Cancel).click()
    assert precise_config.get(FONT_SIZE_KEY) == original + 1
    assert applied == [True]
    dialog.deleteLater()
