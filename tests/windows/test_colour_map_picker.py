"""Browse maps without changing the active map until a result is selected."""

import sys

import pytest
from PyQt6 import QtCore, QtGui, QtTest

from tests.support.windows.color_scale_locale import heatmap

__all__ = ["heatmap"]


@pytest.fixture(autouse=True)
def no_callback_errors(monkeypatch):
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *error: errors.append(error))
    yield
    assert not errors


@pytest.fixture
def picker(heatmap, qapplication):
    heatmap.colorbar_choose_map.click()
    qapplication.processEvents()
    dialog = heatmap.colorbar_map_dialog
    yield dialog.picker
    if heatmap.colorbar_map_dialog is not None:
        heatmap.colorbar_map_dialog.reject()


def visible_names(picker):
    table = picker.table
    return {
        table.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole)
        for row in range(table.rowCount()) if not table.isRowHidden(row)
    }


def choose(combo, value):
    index = combo.findData(value)
    assert index >= 0
    combo.setCurrentIndex(index)


def test_scale_shows_only_limits_and_current_map(heatmap):
    dialog = heatmap.colorbar_scale_dialog
    entry = heatmap.colorbar_range_entry
    current = heatmap.colorbar_current_map
    button = heatmap.colorbar_choose_map
    assert entry.range_heading.text() == "Range"
    assert heatmap.colorbar_map_dialog is None
    assert current.name.text() == "Viridis" and not current.preview.pixmap().isNull()
    assert button.isVisible() and button.text() == "Choose colour map…"
    assert entry.mapTo(dialog, entry.rect().bottomLeft()).y() < button.mapTo(dialog, button.rect().topLeft()).y()


def test_chooser_has_search_and_sortable_headers(picker):
    assert picker.current_heading.text() == "Preview"
    assert picker.picker_heading.text() == "Available colour maps"
    assert picker.current_heading.font().bold() and picker.picker_heading.font().bold()
    assert picker.current_heading.x() == picker.picker_heading.x()
    assert picker.search.placeholderText() == "Search names…"
    for column in (0, 2):
        assert "click again to reverse" in picker.table.horizontalHeaderItem(column).toolTip()
    header = picker.table.horizontalHeader()
    assert header.fontMetrics().height() + 2 <= header.height() <= picker.table.rowHeight(0) + 2


def test_search_enter_requires_a_match_and_applies_the_only_result(heatmap, picker):
    dialog = heatmap.colorbar_map_dialog
    assert dialog.focusWidget() is picker.search
    picker.search.setText("zzzzzz")
    QtTest.QTest.keyClick(picker.search, QtCore.Qt.Key.Key_Return)
    assert dialog.isVisible()
    picker.search.setText("plasma")
    assert heatmap._current_colorbar_colormap_name() == "viridis"
    QtTest.QTest.keyClick(picker.search, QtCore.Qt.Key.Key_Return)
    assert not dialog.isVisible() and heatmap._current_colorbar_colormap_name() == "plasma"


def test_down_moves_from_search_to_filtered_results(heatmap, picker):
    picker.search.setText("blue")
    QtTest.QTest.keyClick(picker.search, QtCore.Qt.Key.Key_Down)
    dialog = heatmap.colorbar_map_dialog
    assert dialog.focusWidget() is picker.table
    item = picker.table.item(picker.table.currentRow(), 0)
    assert item.data(QtCore.Qt.ItemDataRole.UserRole) == heatmap._current_colorbar_colormap_name()
    QtTest.QTest.keyClick(picker.table, QtCore.Qt.Key.Key_Return)
    assert not dialog.isVisible()


def test_colour_fit_options_available_in_manual(heatmap):
    entry = heatmap.colorbar_range_entry
    entry.manual_radio.click()
    assert entry.visible_only.isEnabled()
    assert entry.percent.isEnabled()
    assert entry.fit_button.isEnabled()
    assert all(button.isEnabled() for button in entry.locks.values())


def test_default_chooser_shows_at_least_six_complete_rows(picker):
    table = picker.table
    fully_visible = []
    for row in range(table.rowCount()):
        rect = table.visualItemRect(table.item(row, 0))
        if table.viewport().rect().contains(rect):
            fully_visible.append(row)
    assert len(fully_visible) >= 6


def test_source_offers_only_present_categories_and_resets_invalid_choice(heatmap, picker):
    combo = picker.category_combo
    choose(picker.source_combo, "cet")
    assert combo.findData("linear") >= 0
    assert combo.findData("qualitative") == -1
    choose(combo, "linear")
    choose(picker.source_combo, "matplotlib")
    assert combo.currentData() == "all"
    assert combo.findData("linear") == -1
    assert combo.findData("qualitative") >= 0
    assert visible_names(picker)
    choose(picker.source_combo, "custom")
    assert [combo.itemData(index) for index in range(combo.count())] == ["all", "sequential"]
    assert visible_names(picker)
    assert heatmap._current_colorbar_colormap_name() == "viridis"


def test_source_retains_supported_category_and_search_does_not_change_choices(heatmap, picker):
    combo = picker.category_combo
    choose(picker.source_combo, "cet")
    choose(combo, "cyclic")
    choose(picker.source_combo, "matplotlib")
    assert combo.currentData() == "cyclic"
    assert "phase or angle" in combo.itemData(combo.currentIndex(), QtCore.Qt.ItemDataRole.ToolTipRole)
    assert "phase or angle" in combo.toolTip()
    assert "twilight" in visible_names(picker)
    categories = [combo.itemData(index) for index in range(combo.count())]
    picker.search.setText("no such map")
    assert [combo.itemData(index) for index in range(combo.count())] == categories
    assert combo.currentData() == "cyclic"
    assert heatmap._current_colorbar_colormap_name() == "viridis"


def test_show_in_list_clears_hiding_filters_and_preserves_plot_and_sort(heatmap, qapplication, picker):
    assert heatmap.setColorbarManualRange(-1.2345678901234567, 2.345678901234567)
    levels = tuple(heatmap.bar.levels())
    table = picker.table
    table.sortItems(2, QtCore.Qt.SortOrder.DescendingOrder)
    choose(picker.source_combo, "cet")
    choose(picker.category_combo, "linear")
    picker.search.setText("no match")
    QtTest.QTest.mouseClick(picker.show_current_button, QtCore.Qt.MouseButton.LeftButton)
    qapplication.processEvents()
    assert picker.search.text() == ""
    assert picker.source_combo.currentData() == "all"
    assert picker.category_combo.currentData() == "all"
    row = picker.row_for_name("viridis")
    assert not table.isRowHidden(row)
    assert table.currentRow() == row
    assert table.viewport().rect().contains(table.visualItemRect(table.item(row, 0)).center())
    assert table.horizontalHeader().sortIndicatorSection() == 2
    assert table.horizontalHeader().sortIndicatorOrder() == QtCore.Qt.SortOrder.DescendingOrder
    assert heatmap._current_colorbar_colormap_name() == "viridis"
    assert tuple(heatmap.bar.levels()) == levels
    assert heatmap._colorbar_manual_levels == levels


def test_show_in_list_keeps_filters_that_already_include_current_map(qapplication, picker):
    choose(picker.source_combo, "matplotlib")
    choose(picker.category_combo, "perceptual")
    picker.search.setText("vir")
    picker.show_current_button.click()
    qapplication.processEvents()
    assert picker.source_combo.currentData() == "matplotlib"
    assert picker.category_combo.currentData() == "perceptual"
    assert picker.search.text() == "vir"
    assert picker.table.currentRow() == picker.row_for_name("viridis")


def test_current_name_can_be_selected_copied_and_stays_selected_during_sync(heatmap, qapplication):
    label = heatmap.colorbar_current_map.name
    label.setFocus()
    QtTest.QTest.keySequence(label, QtGui.QKeySequence(QtGui.QKeySequence.StandardKey.SelectAll))
    assert label.selectedText() == "Viridis"
    heatmap._sync_colorbar_scale_controls()
    assert label.selectedText() == "Viridis"
    clipboard = qapplication.clipboard()
    backup = QtCore.QMimeData()
    original = clipboard.mimeData()
    if original is not None:
        for mime_type in original.formats():
            backup.setData(mime_type, original.data(mime_type))
    try:
        QtTest.QTest.keySequence(label, QtGui.QKeySequence(QtGui.QKeySequence.StandardKey.Copy))
        assert clipboard.text() == "Viridis"
    finally:
        clipboard.setMimeData(backup)
    assert heatmap.setColorbarColorMap("plasma")
    assert label.text() == "Plasma"
    QtTest.QTest.keySequence(label, QtGui.QKeySequence(QtGui.QKeySequence.StandardKey.SelectAll))
    assert label.selectedText() == "Plasma"


def test_search_and_filters_combine_without_changing_plot(heatmap, picker):
    window = heatmap
    initial_levels = tuple(window.bar.levels())
    initial_map = window.bar.colorMap()
    picker.search.setText("  cEt-c  ")
    assert "CET-C1" in visible_names(picker)
    assert "CET-L1" not in visible_names(picker)
    choose(picker.source_combo, "cet")
    choose(picker.category_combo, "cyclic")
    assert visible_names(picker)
    assert all(name.startswith("CET-C") for name in visible_names(picker))
    assert window.colorbar_current_map.name.text() == "Viridis"
    assert not window.colorbar_current_map.preview.pixmap().isNull()
    assert not picker.table.selectedItems()
    assert window._current_colorbar_colormap_name() == "viridis"
    assert window.bar.colorMap() is initial_map
    assert tuple(window.bar.levels()) == initial_levels
    assert window._colorbar_manual_levels is None


@pytest.mark.parametrize("source, category, expected", [
    ("cet", "linear", "CET-L1"),
    ("matplotlib", "divergent", "RdBu"),
    ("custom", "sequential", "Greys"),
    ("pyqtgraph", "other", "PAL-relaxed"),
])
def test_source_and_category_find_expected_maps(heatmap, source, category, expected, picker):
    choose(picker.source_combo, source)
    choose(picker.category_combo, category)
    assert expected in visible_names(picker)
    assert heatmap._current_colorbar_colormap_name() == "viridis"


def test_empty_results_and_clearing_search_preserve_active_selection(heatmap, picker):
    picker.search.setText("no such map")
    assert not visible_names(picker)
    assert picker.no_matches.isVisible()
    assert not picker.table.selectedItems()
    assert heatmap.colorbar_current_map.name.text() == "Viridis"
    picker.search.clear()
    assert visible_names(picker)
    assert not picker.no_matches.isVisible()
    assert picker.table.currentRow() == picker.row_for_name("viridis")


def test_empty_list_centres_clear_filters_and_restores_all_results(picker, qapplication):
    names = visible_names(picker)
    table = picker.table
    table.sortItems(2, QtCore.Qt.SortOrder.DescendingOrder)
    selected = QtTest.QSignalSpy(picker.mapSelected)
    choose(picker.source_combo, "cet")
    choose(picker.category_combo, "linear")
    heading = picker.picker_heading.geometry()
    picker.search.setText("no such map")
    qapplication.processEvents()
    assert picker.picker_heading.isVisible() and picker.picker_heading.geometry() == heading
    button = picker.clear_filters_button
    assert button.isVisible() and button.text() == "Clear filters"
    message_rect = picker.no_matches.geometry()
    button_rect = button.geometry()
    assert message_rect.width() >= picker.no_matches.fontMetrics().horizontalAdvance(picker.no_matches.text())
    assert message_rect.right() < button_rect.left()
    assert abs(message_rect.center().y() - button_rect.center().y()) <= 1
    centre = message_rect.united(button_rect).center()
    assert (centre - table.viewport().rect().center()).manhattanLength() <= 2

    button.setFocus()
    QtTest.QTest.keyClick(button, QtCore.Qt.Key.Key_Space)
    qapplication.processEvents()
    assert picker.search.text() == ""
    assert picker.source_combo.currentData() == picker.category_combo.currentData() == "all"
    assert visible_names(picker) == names
    assert not button.isVisible() and not picker.no_matches.isVisible()
    assert table.currentRow() == picker.row_for_name("viridis")
    assert table.horizontalHeader().sortIndicatorSection() == 2
    assert table.horizontalHeader().sortIndicatorOrder() == QtCore.Qt.SortOrder.DescendingOrder
    assert not selected


def test_selecting_search_result_updates_persistent_preview(heatmap, qapplication, picker):
    table = picker.table
    original_preview = heatmap.colorbar_current_map.preview.pixmap().toImage()
    picker.search.setFocus()
    QtTest.QTest.keyClicks(picker.search, "plasma")
    table.sortItems(0, QtCore.Qt.SortOrder.DescendingOrder)
    row = picker.row_for_name("plasma")
    assert not table.isRowHidden(row)
    table.scrollToItem(table.item(row, 0))
    qapplication.processEvents()
    QtTest.QTest.mouseClick(table.viewport(), QtCore.Qt.MouseButton.LeftButton,
                           pos=table.visualItemRect(table.item(row, 0)).center())
    assert heatmap._current_colorbar_colormap_name() == "plasma"
    assert heatmap.colorbar_current_map.name.text() == "Plasma"
    assert heatmap.colorbar_current_map.preview.pixmap().toImage() != original_preview
    picker.search.setText("viridis")
    assert heatmap.colorbar_current_map.name.text() == "Plasma"
    assert not table.selectedItems()


def test_external_map_change_updates_preview_and_keeps_filters(heatmap, picker):
    picker.search.setText("viridis")
    assert heatmap.setColorbarColorMap("CET-C1")
    assert heatmap.colorbar_current_map.name.text() == "CET-C1"
    assert picker.search.text() == "viridis"
    assert not picker.table.selectedItems()


def test_reopening_reveals_and_scrolls_to_active_map(heatmap, qapplication, picker):
    assert heatmap.setColorbarColorMap("twilight_shifted")
    choose(picker.source_combo, "cet")
    choose(picker.category_combo, "linear")
    picker.search.setText("CET-L")
    heatmap.colorbar_map_dialog.accept()
    heatmap.colorbar_choose_map.click()
    picker = heatmap.colorbar_map_dialog.picker
    qapplication.processEvents()
    table = picker.table
    row = picker.row_for_name("twilight_shifted")
    assert "twilight_shifted" in visible_names(picker)
    assert table.currentRow() == row
    rect = table.visualItemRect(table.item(row, 0))
    assert table.viewport().rect().contains(rect.center())
    assert heatmap.colorbar_current_map.name.text() == "twilight_shifted"
