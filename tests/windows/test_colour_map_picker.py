"""Browse maps without changing the active map until a result is selected."""

import sys

import pytest
from PyQt6 import QtCore, QtGui, QtTest

from tests.windows.test_color_scale_locale import heatmap

__all__ = ["heatmap"]


@pytest.fixture(autouse=True)
def no_callback_errors(monkeypatch):
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *error: errors.append(error))
    yield
    assert not errors


def visible_names(window):
    table = window.colorbar_colormap_table
    return {
        table.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole)
        for row in range(table.rowCount()) if not table.isRowHidden(row)
    }


def choose(combo, value):
    index = combo.findData(value)
    assert index >= 0
    combo.setCurrentIndex(index)


def test_dialog_order_and_compact_header(heatmap, qapplication):
    qapplication.processEvents()
    dialog = heatmap.colorbar_scale_dialog
    widgets = (
        heatmap.colorbar_current_heading,
        heatmap.colorbar_current_name,
        heatmap.colorbar_scaling_heading,
        heatmap.colorbar_range_entry,
        heatmap.symmetric_color,
        heatmap.colorbar_picker_heading,
        heatmap.colorbar_search,
        heatmap.colorbar_colormap_table,
    )
    for first, second in zip(widgets[:-1], widgets[1:], strict=True):
        assert first.mapTo(dialog, first.rect().bottomLeft()).y() < second.mapTo(dialog, second.rect().topLeft()).y()
    assert heatmap.colorbar_scaling_heading.text() == "Set scaling"
    assert heatmap.colorbar_current_heading.text() == "Colour map"
    assert heatmap.colorbar_picker_heading.text() == "Choose colour map"
    assert heatmap.colorbar_search.placeholderText() == "Search names…"
    assert "click again to reverse" in heatmap.colorbar_colormap_table.horizontalHeaderItem(0).toolTip()
    assert "click again to reverse" in heatmap.colorbar_colormap_table.horizontalHeaderItem(2).toolTip()
    header = heatmap.colorbar_colormap_table.horizontalHeader()
    assert header.height() >= header.fontMetrics().height() + 2
    assert header.height() <= heatmap.colorbar_colormap_table.rowHeight(0) + 2
    assert all(heading.font().bold() for heading in (
        heatmap.colorbar_current_heading, heatmap.colorbar_scaling_heading,
        heatmap.colorbar_picker_heading,
    ))


def test_scaling_options_share_a_row_and_symmetry_remains_available_in_manual(heatmap):
    dialog = heatmap.colorbar_scale_dialog
    refresh = heatmap.relevel_refresh
    symmetry = heatmap.symmetric_color
    assert refresh.mapTo(dialog, refresh.rect().topRight()).x() < symmetry.mapTo(dialog, symmetry.rect().topLeft()).x()
    assert abs(refresh.mapTo(dialog, refresh.rect().center()).y() - symmetry.mapTo(dialog, symmetry.rect().center()).y()) <= 1
    heatmap.colorbar_manual_radio.click()
    assert not refresh.isEnabled()
    assert symmetry.isEnabled()


def test_default_table_shows_six_complete_rows(heatmap):
    table = heatmap.colorbar_colormap_table
    fully_visible = []
    for row in range(table.rowCount()):
        rect = table.visualItemRect(table.item(row, 0))
        if rect.intersects(table.viewport().rect()):
            assert table.viewport().rect().contains(rect)
            fully_visible.append(row)
    assert len(fully_visible) == 6


def test_source_offers_only_present_categories_and_resets_invalid_choice(heatmap):
    combo = heatmap.colorbar_category_combo
    choose(heatmap.colorbar_source_combo, "cet")
    assert combo.findData("linear") >= 0
    assert combo.findData("qualitative") == -1
    choose(combo, "linear")
    choose(heatmap.colorbar_source_combo, "matplotlib")
    assert combo.currentData() == "all"
    assert combo.findData("linear") == -1
    assert combo.findData("qualitative") >= 0
    assert visible_names(heatmap)
    choose(heatmap.colorbar_source_combo, "custom")
    assert [combo.itemData(index) for index in range(combo.count())] == ["all", "sequential"]
    assert visible_names(heatmap)
    assert heatmap._current_colorbar_colormap_name() == "viridis"


def test_source_retains_supported_category_and_search_does_not_change_choices(heatmap):
    combo = heatmap.colorbar_category_combo
    choose(heatmap.colorbar_source_combo, "cet")
    choose(combo, "cyclic")
    choose(heatmap.colorbar_source_combo, "matplotlib")
    assert combo.currentData() == "cyclic"
    assert "phase or angle" in combo.itemData(combo.currentIndex(), QtCore.Qt.ItemDataRole.ToolTipRole)
    assert "phase or angle" in combo.toolTip()
    assert "twilight" in visible_names(heatmap)
    categories = [combo.itemData(index) for index in range(combo.count())]
    heatmap.colorbar_search.setText("no such map")
    assert [combo.itemData(index) for index in range(combo.count())] == categories
    assert combo.currentData() == "cyclic"
    assert heatmap._current_colorbar_colormap_name() == "viridis"


def test_show_in_list_clears_hiding_filters_and_preserves_plot_and_sort(heatmap, qapplication):
    assert heatmap.setColorbarManualRange(-1.2345678901234567, 2.345678901234567)
    levels = tuple(heatmap.bar.levels())
    table = heatmap.colorbar_colormap_table
    table.sortItems(2, QtCore.Qt.SortOrder.DescendingOrder)
    choose(heatmap.colorbar_source_combo, "cet")
    choose(heatmap.colorbar_category_combo, "linear")
    heatmap.colorbar_search.setText("no match")
    QtTest.QTest.mouseClick(heatmap.colorbar_show_current_button, QtCore.Qt.MouseButton.LeftButton)
    qapplication.processEvents()
    assert heatmap.colorbar_search.text() == ""
    assert heatmap.colorbar_source_combo.currentData() == "all"
    assert heatmap.colorbar_category_combo.currentData() == "all"
    row = heatmap._colorbar_colormap_row("viridis")
    assert not table.isRowHidden(row)
    assert table.currentRow() == row
    assert table.viewport().rect().contains(table.visualItemRect(table.item(row, 0)).center())
    assert table.horizontalHeader().sortIndicatorSection() == 2
    assert table.horizontalHeader().sortIndicatorOrder() == QtCore.Qt.SortOrder.DescendingOrder
    assert heatmap._current_colorbar_colormap_name() == "viridis"
    assert tuple(heatmap.bar.levels()) == levels
    assert heatmap._colorbar_manual_levels == levels


def test_show_in_list_keeps_filters_that_already_include_current_map(heatmap, qapplication):
    choose(heatmap.colorbar_source_combo, "matplotlib")
    choose(heatmap.colorbar_category_combo, "perceptual")
    heatmap.colorbar_search.setText("vir")
    heatmap.colorbar_show_current_button.click()
    qapplication.processEvents()
    assert heatmap.colorbar_source_combo.currentData() == "matplotlib"
    assert heatmap.colorbar_category_combo.currentData() == "perceptual"
    assert heatmap.colorbar_search.text() == "vir"
    assert heatmap.colorbar_colormap_table.currentRow() == heatmap._colorbar_colormap_row("viridis")


def test_current_name_can_be_selected_copied_and_stays_selected_during_sync(heatmap, qapplication):
    label = heatmap.colorbar_current_name
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


def test_search_and_filters_combine_without_changing_plot(heatmap):
    window = heatmap
    initial_levels = tuple(window.bar.levels())
    initial_map = window.bar.colorMap()
    window.colorbar_search.setText("  cEt-c  ")
    assert "CET-C1" in visible_names(window)
    assert "CET-L1" not in visible_names(window)
    choose(window.colorbar_source_combo, "cet")
    choose(window.colorbar_category_combo, "cyclic")
    assert visible_names(window)
    assert all(name.startswith("CET-C") for name in visible_names(window))
    assert window.colorbar_current_name.text() == "Viridis"
    assert not window.colorbar_current_preview.pixmap().isNull()
    assert not window.colorbar_colormap_table.selectedItems()
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
def test_source_and_category_find_expected_maps(heatmap, source, category, expected):
    choose(heatmap.colorbar_source_combo, source)
    choose(heatmap.colorbar_category_combo, category)
    assert expected in visible_names(heatmap)
    assert heatmap._current_colorbar_colormap_name() == "viridis"


def test_empty_results_and_clearing_search_preserve_active_selection(heatmap):
    heatmap.colorbar_search.setText("no such map")
    assert not visible_names(heatmap)
    assert heatmap.colorbar_no_matches.isVisible()
    assert not heatmap.colorbar_colormap_table.selectedItems()
    assert heatmap.colorbar_current_name.text() == "Viridis"
    heatmap.colorbar_search.clear()
    assert visible_names(heatmap)
    assert not heatmap.colorbar_no_matches.isVisible()
    assert heatmap.colorbar_colormap_table.currentRow() == heatmap._colorbar_colormap_row("viridis")


def test_selecting_search_result_updates_persistent_preview(heatmap, qapplication):
    table = heatmap.colorbar_colormap_table
    original_preview = heatmap.colorbar_current_preview.pixmap().toImage()
    heatmap.colorbar_search.setFocus()
    QtTest.QTest.keyClicks(heatmap.colorbar_search, "plasma")
    table.sortItems(0, QtCore.Qt.SortOrder.DescendingOrder)
    row = heatmap._colorbar_colormap_row("plasma")
    assert not table.isRowHidden(row)
    table.scrollToItem(table.item(row, 0))
    qapplication.processEvents()
    QtTest.QTest.mouseClick(table.viewport(), QtCore.Qt.MouseButton.LeftButton,
                           pos=table.visualItemRect(table.item(row, 0)).center())
    assert heatmap._current_colorbar_colormap_name() == "plasma"
    assert heatmap.colorbar_current_name.text() == "Plasma"
    assert heatmap.colorbar_current_preview.pixmap().toImage() != original_preview
    heatmap.colorbar_search.setText("viridis")
    assert heatmap.colorbar_current_name.text() == "Plasma"
    assert not table.selectedItems()


def test_external_map_change_updates_preview_and_keeps_filters(heatmap):
    heatmap.colorbar_search.setText("viridis")
    assert heatmap.setColorbarColorMap("CET-C1")
    assert heatmap.colorbar_current_name.text() == "CET-C1"
    assert heatmap.colorbar_search.text() == "viridis"
    assert not heatmap.colorbar_colormap_table.selectedItems()


def test_reopening_reveals_and_scrolls_to_active_map(heatmap, qapplication):
    assert heatmap.setColorbarColorMap("twilight_shifted")
    choose(heatmap.colorbar_source_combo, "cet")
    choose(heatmap.colorbar_category_combo, "linear")
    heatmap.colorbar_search.setText("CET-L")
    heatmap.colorbar_scale_dialog.close()
    heatmap.open_colorbar_scale_dialog()
    qapplication.processEvents()
    table = heatmap.colorbar_colormap_table
    row = heatmap._colorbar_colormap_row("twilight_shifted")
    assert "twilight_shifted" in visible_names(heatmap)
    assert table.currentRow() == row
    rect = table.visualItemRect(table.item(row, 0))
    assert table.viewport().rect().contains(rect.center())
    assert heatmap.colorbar_current_name.text() == "twilight_shifted"
