"""Preferences stay usable with restricted space and larger interface text."""

import sys

import pytest
from PyQt6 import QtCore, QtGui, QtTest
from PyQt6 import QtWidgets as qtw

from qplot.windows._preferences import PreferencesDialog
from tests.support.windows.preferences import FakeConfig


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


@pytest.mark.parametrize("font_size", [9, 14])
def test_roomy_screen_fits_preferences_without_scrolling(preferences, monkeypatch, qapplication, font_size):
    dialog = preferences
    monkeypatch.setattr(dialog, "_available_geometry", lambda: QtCore.QRect(0, 0, 1280, 1000))
    dialog.setStyleSheet(f"QWidget {{ font-size: {font_size}pt; }}")
    dialog.show()
    qapplication.processEvents()
    initial_size = dialog.size()
    for index in range(dialog.tabs.count()):
        dialog.tabs.setCurrentIndex(index)
        qapplication.processEvents()
        assert dialog.size() == initial_size
        assert dialog.tabs.currentWidget().verticalScrollBar().maximum() == 0


def test_narrow_pages_scroll_and_keep_actions_visible(preferences, qapplication):
    dialog = preferences
    dialog.show()
    dialog.resize(390, 330)
    qapplication.processEvents()
    assert [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())] == [
        "Application", "Plots", "Files && export", "Performance",
    ]
    for index, field in enumerate((
        dialog.includePrereleasesCheck, dialog.defaultColourMapButton,
        dialog.copyPlotImageResolutionCombo, dialog.delGracePeriodSpin,
    )):
        dialog.tabs.setCurrentIndex(index)
        qapplication.processEvents()
        page = dialog.tabs.currentWidget()
        assert not page.horizontalScrollBar().isVisible()
        if field is dialog.defaultColourMapButton:
            assert page.verticalScrollBar().maximum() > 0
        assert page.widget().width() <= page.viewport().width(), (index, page.widget().minimumSizeHint())
        field.setFocus()
        qapplication.processEvents()
        rect = QtCore.QRect(field.mapTo(page.viewport(), QtCore.QPoint()), field.size())
        assert page.viewport().rect().contains(rect.center()), (index, rect, page.viewport().rect())
        assert_buttons_visible(dialog)
    assert dialog.tabs.tabBar().usesScrollButtons()
    assert not any(label.text().startswith("Changes are saved when") for label in dialog.findChildren(qtw.QLabel))


def test_plot_preferences_group_controls_by_when_they_apply(preferences):
    dialog = preferences
    form = dialog.tabs.widget(1).widget().layout()
    headings = {}
    for row in range(form.rowCount()):
        item = form.itemAt(row, qtw.QFormLayout.ItemRole.SpanningRole)
        if item and isinstance(label := item.widget(), qtw.QLabel) and label.font().bold():
            headings[label.text()] = row
    assert list(headings) == ["Plot display", "New plot defaults"]
    defaults = headings["New plot defaults"]
    for control in (dialog.fontSizeSpin, dialog.showSourceAnnotationCheck, dialog.axisTickWidthSpin,
                    dialog.axisMajorTickCountSpin, dialog.colorbarWidthSpin, dialog.cursorSignificantFiguresSpin,
                    dialog.confirmIncompatibleAxesCheck):
        assert headings["Plot display"] < form.getWidgetPosition(control)[0] < defaults
    for control in (dialog.plotTileRowsSpin.parentWidget(), dialog.complex1dCombo, dialog.complexNdCombo,
                    dialog.defaultTraceRow, dialog.defaultColourMapButton):
        assert form.getWidgetPosition(control)[0] > defaults


@pytest.mark.parametrize("width", [390, 600])
def test_plot_grid_keeps_each_label_next_to_its_spin_box(preferences, qapplication, width):
    dialog = preferences
    dialog.show()
    dialog.resize(width, 540)
    dialog.tabs.setCurrentIndex(1)
    qapplication.processEvents()
    page = dialog.tabs.currentWidget()
    viewport = page.viewport()
    labels = {label.buddy(): label for label in page.findChildren(qtw.QLabel) if label.buddy()}
    rows, columns = dialog.plotTileRowsSpin, dialog.plotTileColumnsSpin
    row_label, row_box, column_label, column_box = [
        QtCore.QRect(widget.mapTo(viewport, QtCore.QPoint()), widget.size())
        for widget in (labels[rows], rows, labels[columns], columns)
    ]
    assert 0 <= row_label.left() < column_box.right() < viewport.width()
    centres = [rect.center().y() for rect in (row_label, row_box, column_label, column_box)]
    assert max(centres) - min(centres) <= 1
    between_pairs = column_label.left() - row_box.right()
    assert 0 < row_box.left() - row_label.right() < between_pairs
    assert 0 < column_box.left() - column_label.right() < between_pairs
    assert row_box.width() == column_box.width()


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
