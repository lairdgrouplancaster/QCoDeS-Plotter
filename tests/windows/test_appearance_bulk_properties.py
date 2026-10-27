"""Editing one Appearance property preserves each selected dataset's others."""

from dataclasses import asdict

import numpy as np
import pytest
from PyQt6 import QtCore, QtGui, QtTest
from PyQt6 import QtWidgets as qtw

from qplot.windows._plot_data_viewbox import coordinate_viewbox
from tests.windows.test_appearance_numeric_keyboard import (
    _replace_text,
    _select_line,
)
from tests.windows.test_appearance_numeric_keyboard import (
    trace_editor as _trace_editor,
)
from tests.windows.test_appearance_numeric_keyboard import (
    typed_run_window as _typed_run_window,
)
from tests.windows.test_mixed_plot_drops import _added, _drop, _wait_for
from tests.windows.test_mixed_plot_drops import mixed_plot as _mixed_plot
from tests.windows.test_plot_integration import export_real_plot_csv

trace_editor = _trace_editor
typed_run_window = _typed_run_window
mixed_plot = _mixed_plot


_TRACE_EDITS = [
    ("line_enable", "line_enabled", False),
    ("line_color", "line_color", "#2ca02c"),
    ("line_width", "line_width", 4.5),
    ("line_style", "line_style", "Dot"),
    ("dots_enable", "dots_enabled", True),
    ("dots_color", "dots_color", "#2ca02c"),
    ("dots_size", "dots_size", 9.5),
    ("marker_enable", "markers_enabled", True),
    ("marker_color", "markers_color", "#2ca02c"),
    ("marker_symbol", "markers_symbol", "s"),
    ("marker_size", "markers_size", 14.5),
    ("x_axis", "x_axis", "Bottom"),
    ("y_axis", "y_axis", "Left"),
    ("visible", "visible", False),
    ("opacity", "opacity", .61),
]


def _prepare_traces(host, dialog, lines, field, multiple):
    keys = [next(key for key, item in host.lines.items() if item is line) for line in lines]
    first, second = (host._trace_styles[key] for key in keys)
    first.line_color, second.line_color = "#1f77b4", "#ff7f0e"
    first.line_width, second.line_width = 2., 7.5
    first.line_style, second.line_style = "Solid", "Dash"
    first.dots_color, second.dots_color = "#1f77b4", "#d62728"
    first.dots_size, second.dots_size = 6., 8.5
    first.markers_color, second.markers_color = "#1f77b4", "#d62728"
    first.markers_size, second.markers_size = 10., 12.5
    first.markers_symbol, second.markers_symbol = "o", "+"
    first.x_axis, first.y_axis = "Bottom", "Left"
    second.x_axis, second.y_axis = "Top", "Right"
    first.opacity, second.opacity = 1., .37
    first.visible = second.visible = True
    for style in (first, second):
        style.dots_enabled = field.startswith("dots_") and field != "dots_enable"
        style.markers_enabled = field.startswith("marker_") and field != "marker_enable"
    # Enabling one renderer intentionally disables the other, but preserves its
    # independent color, symbol and size for later reuse.
    if field == "dots_enable":
        second.markers_enabled = True
    if field == "marker_enable":
        second.dots_enabled = True
    for key, line in zip(keys, lines, strict=True):
        host._apply_trace_style(key, line)
    dialog.refresh_rows()
    if multiple:
        _select_line(host, dialog, lines[0])
        _select_line(host, dialog, lines[1], QtCore.Qt.KeyboardModifier.ControlModifier)
    else:
        _select_line(host, dialog, lines[1])
    assert len(dialog._selected_entries()) == (2 if multiple else 1)
    return keys


def _edit_widget(widget, value):
    if isinstance(widget, qtw.QCheckBox):
        assert widget.isChecked() != value
        QtTest.QTest.mouseClick(widget, QtCore.Qt.MouseButton.LeftButton)
    elif isinstance(widget, qtw.QComboBox):
        index = widget.findData(value)
        if index < 0:
            index = widget.findText(value)
        assert index >= 0
        assert widget.currentIndex() != index
        widget.setFocus()
        QtTest.QTest.keyClick(widget, QtCore.Qt.Key.Key_Home)
        for _ in range(index):
            QtTest.QTest.keyClick(widget, QtCore.Qt.Key.Key_Down)
    else:
        editor = _replace_text(widget, value)
        QtTest.QTest.keyClick(editor, QtCore.Qt.Key.Key_Return)
    qtw.QApplication.processEvents()


@pytest.mark.parametrize("multiple", [False, True], ids=["single", "multiple"])
@pytest.mark.parametrize("field,property_name,value", _TRACE_EDITS)
def test_trace_edit_preserves_unedited_properties(
    trace_editor, field, property_name, value, multiple,
):
    host, dialog, lines = trace_editor
    keys = _prepare_traces(host, dialog, lines, field, multiple)
    before = [asdict(host._trace_styles[key]) for key in keys]
    _edit_widget(getattr(dialog, field), round(value * 100) if field == "opacity" else value)
    for index, (key, original) in enumerate(zip(keys, before, strict=True)):
        expected = dict(original)
        if multiple or index == 1:
            expected[property_name] = value
            if property_name == "dots_enabled" and value:
                expected["markers_enabled"] = False
            elif property_name == "markers_enabled" and value:
                expected["dots_enabled"] = False
        style = host._trace_styles[key]
        assert asdict(style) == expected
        assert coordinate_viewbox(lines[index].getViewBox()) is host._trace_axis_viewbox(style)


@pytest.mark.parametrize("accept", [False, True], ids=["cancel", "accept"])
@pytest.mark.parametrize("multiple", [False, True], ids=["single", "multiple"])
def test_custom_trace_color_preserves_other_properties_and_csv(
    trace_editor, multiple, accept, tmp_path, monkeypatch,
):
    host, dialog, lines = trace_editor
    keys = _prepare_traces(host, dialog, lines, "line_color", multiple)
    before = [asdict(host._trace_styles[key]) for key in keys]
    before_csv = tmp_path / "before.csv"
    after_csv = tmp_path / "after.csv"
    assert export_real_plot_csv(monkeypatch, host, before_csv)
    state = {}
    timer = QtCore.QTimer()
    deadline = QtCore.QTimer()

    def choose():
        chooser = qtw.QApplication.activeModalWidget()
        if not isinstance(chooser, qtw.QColorDialog):
            return
        timer.stop()
        state["opened"] = True
        chooser.setCurrentColor(QtGui.QColor("#123456"))
        button = chooser.findChild(qtw.QDialogButtonBox).button(
            qtw.QDialogButtonBox.StandardButton.Ok if accept
            else qtw.QDialogButtonBox.StandardButton.Cancel
        )
        QtTest.QTest.mouseClick(button, QtCore.Qt.MouseButton.LeftButton)

    def abort():
        state["timeout"] = True
        chooser = qtw.QApplication.activeModalWidget()
        if isinstance(chooser, qtw.QDialog):
            chooser.reject()

    timer.timeout.connect(choose)
    deadline.setSingleShot(True)
    deadline.timeout.connect(abort)
    try:
        timer.start(10)
        deadline.start(10_000)
        dialog.line_color.setCurrentIndex(dialog.line_color.findText("Custom"))
    finally:
        timer.stop()
        deadline.stop()
    assert state == {"opened": True}
    for index, (key, original) in enumerate(zip(keys, before, strict=True)):
        expected = dict(original)
        if accept and (multiple or index == 1):
            expected["line_color"] = "#123456"
        assert asdict(host._trace_styles[key]) == expected
    assert export_real_plot_csv(monkeypatch, host, after_csv)
    assert before_csv.read_bytes() == after_csv.read_bytes()


def _select_heatmap(dialog, key, modifier=QtCore.Qt.KeyboardModifier.NoModifier):
    row = next(row for row in range(dialog.table.rowCount())
               if dialog._row_entry(row) == ("heatmap", key))
    item = dialog.table.item(row, 0)
    dialog.table.scrollToItem(item)
    qtw.QApplication.processEvents()
    QtTest.QTest.mouseClick(dialog.table.viewport(), QtCore.Qt.MouseButton.LeftButton,
                           modifier, dialog.table.visualItemRect(item).center())
    qtw.QApplication.processEvents()


def _heatmap_state(host, layer):
    x_axis, y_axis = host._heatmap_axis_sides(layer)
    return {"visible": host._heatmap_is_visible(layer),
            "opacity": host._heatmap_opacity(layer),
            "x_axis": x_axis, "y_axis": y_axis}


@pytest.mark.parametrize("field,value", [("visible", False), ("opacity", .61),
                                        ("x_axis", "Bottom"), ("y_axis", "Left")])
@pytest.mark.parametrize("multiple", [False, True], ids=["single", "multiple"])
def test_heatmap_edit_preserves_unedited_properties(
    mixed_plot, monkeypatch, field, value, multiple,
):
    window, host, runs = mixed_plot
    assert window._database_access_mode == "trusted_live"
    monkeypatch.setattr(qtw.QMessageBox, "exec", lambda box: qtw.QMessageBox.StandardButton.Ok)
    if host.operation_kind == "plot1d":
        _drop(host, runs["heatmap"])
        first = _added(host, runs["heatmap"])
    else:
        first = host
    _drop(host, runs["other_heatmap"])
    second = _added(host, runs["other_heatmap"])
    _wait_for(lambda: not second.from_win.worker.running)
    layers = [first, second]
    keys = [host._heatmap_key_for_layer(layer) for layer in layers]
    grids = [np.array(layer.dataGrid if layer is host else layer.data_grid, copy=True)
             for layer in layers]
    for layer, axes, opacity in zip(layers, [("Bottom", "Left"), ("Top", "Right")],
                                    [1., .37], strict=True):
        host._set_layer_axes(layer, *axes)
        host._set_layer_opacity(layer, opacity)
        host._set_layer_visibility(layer, True)
    # Opacity edits must not also reveal a separately hidden layer.
    if field == "opacity":
        host._set_layer_visibility(first, False)
    host.appearance_action.trigger()
    dialog = host._plot_appearance_dialog
    if multiple:
        _select_heatmap(dialog, keys[0])
        _select_heatmap(dialog, keys[1], QtCore.Qt.KeyboardModifier.ControlModifier)
    else:
        _select_heatmap(dialog, keys[1])
    before = [_heatmap_state(host, layer) for layer in layers]
    _edit_widget(getattr(dialog.heatmap_controls, field),
                 round(value * 100) if field == "opacity" else value)
    for index, (layer, original) in enumerate(zip(layers, before, strict=True)):
        expected = dict(original)
        if multiple or index == 1:
            expected[field] = value
        assert _heatmap_state(host, layer) == expected
        actual = layer.dataGrid if layer is host else layer.data_grid
        np.testing.assert_array_equal(actual, grids[index])
