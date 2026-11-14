"""Preferences stay usable with restricted space and larger interface text."""

import sys

import pytest
from PyQt6 import QtCore, QtGui, QtTest
from PyQt6 import QtWidgets as qtw

from qplot.windows._preferences import PreferencesDialog
from tests.windows.test_preferences import FakeConfig


@pytest.fixture(autouse=True)
def no_callback_errors(monkeypatch):
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *error: errors.append(error))
    yield
    assert not errors


@pytest.fixture
def preferences(qapplication):
    dialog = PreferencesDialog(FakeConfig())
    yield dialog
    dialog.close()
    dialog.deleteLater()
    qapplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


def assert_buttons_visible(dialog):
    for button in dialog.buttonBox.buttons():
        assert button.isVisible()
        rect = QtCore.QRect(button.mapTo(dialog, QtCore.QPoint()), button.size())
        assert dialog.rect().contains(rect), (dialog.size(), rect, button.text())


@pytest.mark.parametrize("font_size", [9, 14])
@pytest.mark.parametrize("screen_size", [(760, 500), (480, 360)])
def test_initial_size_fits_available_screen_and_tabs_keep_size(
    preferences, monkeypatch, qapplication, font_size, screen_size,
):
    dialog = preferences
    screen = QtCore.QRect(50, 40, *screen_size)
    monkeypatch.setattr(dialog, "_available_geometry", lambda: screen)
    font = QtGui.QFont(dialog.font())
    font.setPointSize(font_size)
    dialog.setFont(font)
    dialog.show()
    qapplication.processEvents()
    assert screen.contains(dialog.frameGeometry()), (screen, dialog.frameGeometry())
    initial_size = dialog.size()
    for index in range(dialog.tabs.count()):
        dialog.tabs.setCurrentIndex(index)
        qapplication.processEvents()
        assert dialog.size() == initial_size
        assert_buttons_visible(dialog)
        page = dialog.tabs.currentWidget()
        assert page.widget().width() <= page.viewport().width(), (index, page.widget().minimumSizeHint())
        assert not page.horizontalScrollBar().isVisible()
        for child in page.widget().findChildren(qtw.QWidget, options=QtCore.Qt.FindChildOption.FindDirectChildrenOnly):
            assert child.geometry().left() >= 0
            assert child.geometry().right() < page.widget().width(), (index, child.objectName(), child.geometry())


def test_narrow_pages_scroll_and_keep_actions_visible(preferences, qapplication):
    dialog = preferences
    dialog.show()
    dialog.resize(390, 330)
    qapplication.processEvents()
    for index, field in enumerate((
        dialog.confirmQuitCheck, dialog.defaultColourMapButton,
        dialog.confirmIncompatibleAxesCheck, dialog.copyPlotImageResolutionCombo,
        dialog.delGracePeriodSpin, dialog.includePrereleasesCheck,
    )):
        dialog.tabs.setCurrentIndex(index)
        qapplication.processEvents()
        page = dialog.tabs.currentWidget()
        assert not page.horizontalScrollBar().isVisible()
        assert page.widget().width() <= page.viewport().width(), (index, page.widget().minimumSizeHint())
        field.setFocus()
        qapplication.processEvents()
        rect = QtCore.QRect(field.mapTo(page.viewport(), QtCore.QPoint()), field.size())
        assert page.viewport().rect().contains(rect.center()), (index, rect, page.viewport().rect())
        assert_buttons_visible(dialog)
    assert dialog.tabs.tabBar().usesScrollButtons()
    assert any(button.isVisible() for button in dialog.tabs.tabBar().findChildren(qtw.QToolButton))
    assert not any(label.text().startswith("Changes are saved when") for label in dialog.findChildren(qtw.QLabel))


def test_wrapped_checkbox_remains_clickable_and_keyboard_accessible(preferences, qapplication):
    dialog = preferences
    dialog.show()
    dialog.resize(360, 380)
    dialog.tabs.setCurrentIndex(1)
    qapplication.processEvents()
    checkbox = dialog.showSourceAnnotationCheck
    page = dialog.tabs.currentWidget()
    page.ensureWidgetVisible(checkbox)
    qapplication.processEvents()
    assert checkbox.height() >= checkbox.heightForWidth(checkbox.width())
    before = checkbox.isChecked()
    QtTest.QTest.mouseClick(checkbox, QtCore.Qt.MouseButton.LeftButton,
                           pos=QtCore.QPoint(checkbox.width() - 4, checkbox.height() // 2))
    assert checkbox.isChecked() != before
    checkbox.setFocus()
    QtTest.QTest.keyClick(checkbox, QtCore.Qt.Key.Key_Space)
    assert checkbox.isChecked() == before
    dialog.resize(780, 500)
    qapplication.processEvents()
    label = page.widget().layout().labelForField(dialog.fontSizeSpin)
    assert label.geometry().right() < dialog.fontSizeSpin.geometry().left()
