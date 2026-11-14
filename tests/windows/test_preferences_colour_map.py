"""The global map chooser previews pending changes and honours both dialogs."""

import sys

import pytest
from PyQt6 import QtCore, QtTest
from PyQt6 import QtWidgets as qtw

from qplot.windows._colour_map_picker import ColourMapButton, ColourMapChooser
from qplot.windows._preferences import PreferencesDialog
from tests.windows.test_preferences import FakeConfig

MAP_KEY = "user_preference.bar_colour"


@pytest.fixture
def preferences(qapplication, monkeypatch):
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *error: errors.append(error))
    cfg = FakeConfig()
    cfg.values[MAP_KEY] = "twilight_shifted"
    dialog = PreferencesDialog(cfg)
    dialog.tabs.setCurrentIndex(1)
    dialog.show()
    qapplication.processEvents()
    yield dialog, cfg
    dialog.close()
    dialog.deleteLater()
    qapplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    assert not errors


@pytest.mark.parametrize("exit_action", ["ok", "cancel", "escape", "close"])
def test_chooser_previews_and_only_accepts_confirmed_selection(preferences, qapplication, exit_action):
    preferences, cfg = preferences
    button = preferences.defaultColourMapButton
    assert isinstance(button, ColourMapButton)
    assert button.text() == "twilight_shifted"
    original_image = button.grab().toImage()
    results = []
    destroyed = []

    def choose():
        chooser = qapplication.activeModalWidget()
        assert isinstance(chooser, ColourMapChooser)
        chooser.destroyed.connect(lambda: destroyed.append(True))
        assert chooser.picker.current_name.text() == "twilight_shifted"
        chooser.picker.search.setText("plasma")
        picker = chooser.picker
        row = picker.row_for_name("plasma")
        picker.table.scrollToItem(picker.table.item(row, 0))
        qapplication.processEvents()
        QtTest.QTest.mouseClick(
            picker.table.viewport(), QtCore.Qt.MouseButton.LeftButton,
            pos=picker.table.visualItemRect(picker.table.item(row, 0)).center(),
        )
        results.append((button.text(), preferences.preference_values()[MAP_KEY],
                        cfg.get(MAP_KEY), button.grab().toImage() != original_image))
        if exit_action in ("ok", "cancel"):
            standard = (qtw.QDialogButtonBox.StandardButton.Ok if exit_action == "ok"
                        else qtw.QDialogButtonBox.StandardButton.Cancel)
            chooser.buttons.button(standard).click()
        elif exit_action == "escape":
            QtTest.QTest.keyClick(chooser, QtCore.Qt.Key.Key_Escape)
        else:
            chooser.close()

    def run_choice():
        try:
            choose()
        except BaseException:
            chooser = qapplication.activeModalWidget()
            if isinstance(chooser, ColourMapChooser):
                chooser.reject()
            raise

    QtCore.QTimer.singleShot(0, run_choice)
    button.click()
    assert results == [("Plasma", "plasma", "twilight_shifted", True)]
    expected = "plasma" if exit_action == "ok" else "twilight_shifted"
    assert preferences.preference_values()[MAP_KEY] == expected
    assert cfg.get(MAP_KEY) == "twilight_shifted"
    assert "Default colour map for new plots" in button.accessibleName()
    assert "Existing plots keep their own colour maps" in button.toolTip()
    qapplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    assert destroyed == [True]
    assert preferences.findChildren(ColourMapChooser) == []
    assert preferences.apply_preferences()
    assert cfg.get(MAP_KEY) == expected


def test_defaults_reload_and_preferences_cancel_keep_button_in_sync(preferences, monkeypatch):
    preferences, cfg = preferences
    button = preferences.defaultColourMapButton
    button.setValue("plasma")
    preferences.reject()
    assert cfg.get(MAP_KEY) == "twilight_shifted"
    preferences.load_from_config()
    assert button.value() == "twilight_shifted"
    assert button.text() == "twilight_shifted"
    monkeypatch.setattr(qtw.QMessageBox, "question", lambda *args: qtw.QMessageBox.StandardButton.Yes)
    assert preferences.restore_defaults()
    assert button.value() == "viridis"
    assert button.text() == "Viridis"
    assert cfg.get(MAP_KEY) == "twilight_shifted"
    assert preferences.apply_preferences()
    assert cfg.get(MAP_KEY) == "viridis"
