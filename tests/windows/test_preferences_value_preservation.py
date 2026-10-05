"""Preferences edits must not overwrite untouched, schema-valid settings."""

import json
from pathlib import Path

import pytest
from PyQt6 import QtCore, QtTest
from PyQt6 import QtWidgets as qtw

from qplot.configuration.config import config
from qplot.windows._preferences import PreferencesDialog


@pytest.fixture
def precise_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "default_path", str(tmp_path))
    monkeypatch.setattr(config, "default_file", str(tmp_path / "config.json"))
    cfg = config()
    cfg.update_many({
        "user_preference.axis_tick_width": 1.25,
        "user_preference.default_refresh_rate": 0.049,
        "runtime_settings.del_grace_period": 10.125,
        "runtime_settings.cloud_sync_timeout": 120.125,
    })
    return cfg


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
    monkeypatch.setattr(qtw.QMessageBox, "question", lambda *args: qtw.QMessageBox.StandardButton.Yes)
    try:
        assert dialog.refreshRateSpin.value() == 1.0
        assert dialog.restore_defaults()
        assert dialog.apply_preferences()
        assert precise_config.get("user_preference.default_refresh_rate") == 1.0
        assert precise_config.get("user_preference.axis_tick_width") == 2.0
    finally:
        dialog.close()


def test_cancel_discards_pending_edits_and_reset(precise_config, monkeypatch):
    before = json.loads(Path(precise_config.default_file).read_text(encoding="utf-8"))
    dialog = PreferencesDialog(precise_config)
    monkeypatch.setattr(qtw.QMessageBox, "question", lambda *args: qtw.QMessageBox.StandardButton.Yes)
    try:
        assert dialog.restore_defaults()
        dialog.refreshRateSpin.setValue(0.2)
        dialog.buttonBox.button(qtw.QDialogButtonBox.StandardButton.Cancel).click()
        assert precise_config.config == before
        assert json.loads(Path(precise_config.default_file).read_text(encoding="utf-8")) == before
    finally:
        dialog.close()
