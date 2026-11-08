"""Thumbnail selection stays attached to visible measurement identities."""

import pytest
from PyQt6 import QtCore, QtGui, QtTest
from PyQt6 import QtWidgets as qtw

from qplot.windows._dragdrop import run_preview_payloads_from_mime
from qplot.windows._widgets._preview_selection import (
    active_preview_labels,
    preview_key,
    selected_preview_targets,
)
from qplot.windows._widgets.preview import PreviewTab
from qplot.windows._widgets.treeWidgets import RunList
from tests.widgets.test_preview_placeholders import _white_preview


@pytest.fixture
def table():
    view = RunList()
    view.setRuns({index: {
        "guid": f"run-{index}",
        "measure_parameters": ["a", "b"],
        "sweep_parameters": ["x"],
        "preview_dimensions": [1, 3],
        "is_completed": True,
    } for index in (1, 2, 3)})
    view.resize(800, 260)
    view.show()
    qtw.QApplication.processEvents()
    try:
        yield view
    finally:
        view.close()
        view.deleteLater()


def _click(label, modifiers=QtCore.Qt.KeyboardModifier.NoModifier):
    QtTest.QTest.mouseClick(label, QtCore.Qt.MouseButton.LeftButton, modifiers)
    qtw.QApplication.processEvents()


def _context(label, monkeypatch, callback):
    monkeypatch.setattr(qtw.QMenu, "exec", callback)
    event = QtGui.QContextMenuEvent(
        QtGui.QContextMenuEvent.Reason.Mouse, QtCore.QPoint(2, 2),
        label.mapToGlobal(QtCore.QPoint(2, 2)),
    )
    qtw.QApplication.sendEvent(label, event)


@pytest.mark.parametrize("modifier", [QtCore.Qt.KeyboardModifier.ControlModifier,
                                     QtCore.Qt.KeyboardModifier.MetaModifier])
def test_toggle_multiple_thumbnails_does_not_toggle_table_rows(table, modifier):
    labels = active_preview_labels(table)
    _click(labels[0])
    selected_rows = table.selected_run_ids()
    _click(labels[1], modifier)
    _click(labels[4], modifier)
    assert selected_preview_targets(table) == tuple(map(preview_key, (labels[0], labels[1], labels[4])))
    assert table.selected_run_ids() == selected_rows
    _click(labels[1], modifier)
    assert selected_preview_targets(table) == tuple(map(preview_key, (labels[0], labels[4])))
    _click(labels[2])
    assert selected_preview_targets(table) == (preview_key(labels[2]),)


def test_shift_range_uses_sorted_visible_rows(table):
    table.sortItems(0, QtCore.Qt.SortOrder.DescendingOrder)
    qtw.QApplication.processEvents()
    labels = active_preview_labels(table)
    _click(labels[1])
    _click(labels[4], QtCore.Qt.KeyboardModifier.ShiftModifier)
    assert selected_preview_targets(table) == tuple(map(preview_key, labels[1:5]))
    table.topLevelItem(1).setHidden(True)
    assert selected_preview_targets(table) == tuple(map(preview_key, (labels[1], labels[4])))


def _drag(label, monkeypatch):
    captured = []

    class Drag:
        def __init__(self, _source):
            pass

        def setMimeData(self, mime):
            captured.append(run_preview_payloads_from_mime(mime))

        def setPixmap(self, pixmap):
            assert not pixmap.isNull()

        def setHotSpot(self, _point):
            pass

        def exec(self, action):
            assert action == QtCore.Qt.DropAction.CopyAction

    monkeypatch.setattr(QtGui, "QDrag", Drag)
    start = label.rect().center()
    end = start + QtCore.QPoint(qtw.QApplication.startDragDistance() + 1, 0)
    QtTest.QTest.mousePress(label, QtCore.Qt.MouseButton.LeftButton, pos=start)
    move = QtGui.QMouseEvent(
        QtCore.QEvent.Type.MouseMove, QtCore.QPointF(end), QtCore.QPointF(label.mapToGlobal(end)),
        QtCore.Qt.MouseButton.NoButton, QtCore.Qt.MouseButton.LeftButton,
        QtCore.Qt.KeyboardModifier.NoModifier,
    )
    qtw.QApplication.sendEvent(label, move)
    QtTest.QTest.mouseRelease(label, QtCore.Qt.MouseButton.LeftButton, pos=end)
    assert len(captured) == 1
    return captured[0]


def test_drag_selected_thumbnail_keeps_entire_group_in_display_order(table, monkeypatch):
    labels = active_preview_labels(table)
    _click(labels[0])
    _click(labels[1], QtCore.Qt.KeyboardModifier.ControlModifier)
    _click(labels[4], QtCore.Qt.KeyboardModifier.ControlModifier)
    selected = selected_preview_targets(table)
    previews = _drag(labels[1], monkeypatch)
    assert tuple((preview["guid"], preview["parameter"]) for preview in previews) == selected
    assert selected_preview_targets(table) == selected
    assert all(preview.get("axes_pending") for preview in previews)


def test_drag_unselected_thumbnail_carries_only_that_measurement(table, monkeypatch):
    labels = active_preview_labels(table)
    _click(labels[0])
    _click(labels[1], QtCore.Qt.KeyboardModifier.ControlModifier)
    previews = _drag(labels[4], monkeypatch)
    assert [(preview["guid"], preview["parameter"]) for preview in previews] == [preview_key(labels[4])]
    assert selected_preview_targets(table) == (preview_key(labels[4]),)


def test_plain_click_selected_thumbnail_collapses_group_on_release(table):
    labels = active_preview_labels(table)
    _click(labels[0])
    _click(labels[1], QtCore.Qt.KeyboardModifier.ControlModifier)
    QtTest.QTest.mousePress(labels[0], QtCore.Qt.MouseButton.LeftButton)
    assert len(selected_preview_targets(table)) == 2
    QtTest.QTest.mouseRelease(labels[0], QtCore.Qt.MouseButton.LeftButton)
    assert selected_preview_targets(table) == (preview_key(labels[0]),)


def test_preview_pane_drags_all_selected_rendered_measurements(monkeypatch):
    tab = PreviewTab(preview_size=64)
    tab.current_guid = "run"
    tab._show_previews([_white_preview("a"), _white_preview("b"), _white_preview("c")])
    tab.show()
    qtw.QApplication.processEvents()
    try:
        labels = active_preview_labels(tab)
        _click(labels[0])
        _click(labels[2], QtCore.Qt.KeyboardModifier.ShiftModifier)
        previews = _drag(labels[1], monkeypatch)
        assert [preview["parameter"] for preview in previews] == ["a", "b", "c"]
        assert all(preview["axes"] == ["x"] for preview in previews)
        assert len(selected_preview_targets(tab)) == 3
    finally:
        tab.close()
        tab.deleteLater()


def test_context_and_keyboard_menu_preserve_group(table, monkeypatch):
    labels = active_preview_labels(table)
    _click(labels[0])
    _click(labels[3], QtCore.Qt.KeyboardModifier.ControlModifier)
    expected = tuple(map(preview_key, (labels[0], labels[3])))
    emitted = []
    table.batchExportRequested.connect(emitted.append)

    def export(menu, *_args):
        assert [action.text() for action in menu.actions()] == ["Export Selected Thumbnails as CSV…"]
        menu.actions()[0].trigger()

    _context(labels[0], monkeypatch, export)
    assert emitted == [expected]
    assert selected_preview_targets(table) == expected
    labels[3].setFocus()
    QtTest.QTest.keyClick(labels[3], QtCore.Qt.Key.Key_Menu)
    assert emitted == [expected, expected]
    assert selected_preview_targets(table) == expected


def test_right_click_unselected_thumbnail_uses_single_action(table, monkeypatch):
    labels = active_preview_labels(table)
    _click(labels[0])
    _click(labels[1], QtCore.Qt.KeyboardModifier.ControlModifier)
    captured = []
    _context(labels[4], monkeypatch, lambda menu, *_: captured.extend(action.text() for action in menu.actions()))
    assert captured == ["&Plot", "&Export CSV...", "Generate &stats..."]
    assert selected_preview_targets(table) == (preview_key(labels[4]),)


def test_refresh_preserves_all_selected_parameters_and_retires_old_labels(table):
    cell = table.preview_cells["run-1"]
    first, second = cell.preview_selection_labels()
    _click(first)
    _click(second, QtCore.Qt.KeyboardModifier.ControlModifier)
    assert selected_preview_targets(table) == (("run-1", "a"), ("run-1", "b"))
    cell.show_previews([_white_preview("a"), {"parameter": "b", "unsupported": True, "axes": ["x", "y", "z"]}])
    assert selected_preview_targets(table) == (("run-1", "a"), ("run-1", "b"))
    assert first not in active_preview_labels(table)
    assert second not in active_preview_labels(table)
    assert all(label._selected for label in cell.preview_selection_labels())
    cell.update_placeholder_metadata({"measure_parameters": ["b"], "preview_dimensions": [3]})
    assert selected_preview_targets(table) == (("run-1", "b"),)


@pytest.mark.parametrize("change", ["selection", "refresh", "reset", "hidden", "hidden_label"])
def test_stale_thumbnail_menu_cannot_export(table, monkeypatch, change):
    labels = active_preview_labels(table)
    _click(labels[0])
    _click(labels[1], QtCore.Qt.KeyboardModifier.ControlModifier)
    emitted = []
    table.batchExportRequested.connect(emitted.append)

    def change_then_export(menu, *_args):
        if change == "selection":
            labels[2].select_preview()
        elif change == "refresh":
            table.preview_cells[labels[0].guid].show_previews([_white_preview("a")])
        elif change == "reset":
            table.setRuns({})
        elif change == "hidden_label":
            labels[1].hide()
        else:
            table._item_for_guid(labels[0].guid).setHidden(True)
        menu.actions()[0].trigger()

    _context(labels[0], monkeypatch, change_then_export)
    assert emitted == []


def test_preview_tab_multiselect_refresh_and_run_switch(monkeypatch):
    tab = PreviewTab(preview_size=64)
    tab.current_guid = "first"
    tab._show_previews([_white_preview("a"), _white_preview("b"), _white_preview("c")])
    tab.show()
    qtw.QApplication.processEvents()
    try:
        labels = active_preview_labels(tab)
        _click(labels[0])
        _click(labels[2], QtCore.Qt.KeyboardModifier.ShiftModifier)
        expected = (("first", "a"), ("first", "b"), ("first", "c"))
        assert selected_preview_targets(tab) == expected
        emitted = []
        tab.batchExportRequested.connect(emitted.append)
        _context(labels[1], monkeypatch, lambda menu, *_: menu.actions()[0].trigger())
        assert emitted == [expected]
        tab._show_previews([_white_preview("c"), _white_preview("a")])
        assert selected_preview_targets(tab) == (("first", "c"), ("first", "a"))
        assert all(label._preview_retired for label in labels)
        tab.current_guid = "second"
        tab._show_previews([_white_preview("c"), _white_preview("a")])
        assert selected_preview_targets(tab) == ()
    finally:
        tab.close()
        tab.deleteLater()
