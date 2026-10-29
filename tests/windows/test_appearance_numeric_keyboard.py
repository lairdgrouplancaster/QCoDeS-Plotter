"""Appearance numeric edits commit intact through real Qt keyboard events."""

import numpy as np
import pytest
from PyQt6 import QtCore, QtTest
from PyQt6 import QtWidgets as qtw

from tests.windows.test_plot_integration import wait_for
from tests.windows.test_refresh_interval_precision import live_view
from tests.windows.test_typed_run_keyboard_launch import (
    _drain,
    _launch,
    _type_run,
)
from tests.windows.test_typed_run_keyboard_launch import (
    typed_run_window as _typed_run_window,
)

typed_run_window = _typed_run_window


@pytest.fixture
def trace_editor(typed_run_window):
    window, runs = typed_run_window
    assert window._database_access_mode == "trusted_live"
    for run_id, _guid, _offset in runs:
        _type_run(window, run_id)
        _launch(window, "Key_Return")
        _drain(window)
    host, source = window.windows
    assert window.add_trace_to_plot(
        host, source._dataset_key, source.param.name, param=source.param,
    )
    host.appearance_action.trigger()
    dialog = host._plot_appearance_dialog
    assert dialog.isVisible()
    lines = [host.line, next(line for line in host.lines.values() if line is not host.line)]
    originals = [tuple(np.array(values, copy=True) for values in line.getOriginalDataset())
                 for line in lines]
    yield host, dialog, lines
    for line, original in zip(lines, originals, strict=True):
        for values, expected in zip(line.getOriginalDataset(), original, strict=True):
            np.testing.assert_array_equal(values, expected)


def _select_line(host, dialog, line, modifier=QtCore.Qt.KeyboardModifier.NoModifier):
    key = next(key for key, candidate in host.lines.items() if candidate is line)
    row = next(row for row in range(dialog.table.rowCount())
               if dialog._row_entry(row) == ("trace", key))
    item = dialog.table.item(row, 0)
    dialog.table.scrollToItem(item)
    qtw.QApplication.processEvents()
    QtTest.QTest.mouseClick(
        dialog.table.viewport(), QtCore.Qt.MouseButton.LeftButton, modifier,
        dialog.table.visualItemRect(item).center(),
    )
    qtw.QApplication.processEvents()
    return key


def _replace_text(spin, value):
    editor = spin.lineEdit()
    spin.window().activateWindow()
    qtw.QApplication.processEvents()
    editor.setFocus()
    qtw.QApplication.processEvents()
    assert editor.hasFocus()
    QtTest.QTest.keyClick(editor, QtCore.Qt.Key.Key_A,
                         QtCore.Qt.KeyboardModifier.ControlModifier)
    assert editor.selectedText() == editor.text()
    QtTest.QTest.keyClicks(editor, str(value))
    return editor


def _rendered_value(line, field):
    return line.opts["pen"].widthF() if field == "line_width" else line.opts["symbolSize"]


_FIELDS = [
    ("line_width", "line_width", None, 4.5),
    ("dots_size", "dots_size", "dots_enable", 7.5),
    ("marker_size", "markers_size", "marker_enable", 12.5),
]


@pytest.mark.parametrize("field,style_name,toggle,value", _FIELDS)
@pytest.mark.parametrize("target", [0, 1], ids=["primary", "secondary"])
@pytest.mark.parametrize("commit", ["return", "tab", "other-row"])
def test_fractional_edit_commits_to_original_selected_trace(
    trace_editor, field, style_name, toggle, value, target, commit,
):
    host, dialog, lines = trace_editor
    line, other = lines[target], lines[1 - target]
    key = _select_line(host, dialog, line)
    other_key = next(key for key, candidate in host.lines.items() if candidate is other)
    other_style_before = getattr(host._trace_styles[other_key], style_name)
    other_rendered_before = _rendered_value(other, field)
    if toggle is not None:
        QtTest.QTest.mouseClick(getattr(dialog, toggle), QtCore.Qt.MouseButton.LeftButton)
    editor = _replace_text(getattr(dialog, field), value)
    if commit == "other-row":
        _select_line(host, dialog, other)
    else:
        QtTest.QTest.keyClick(editor, QtCore.Qt.Key.Key_Return if commit == "return"
                             else QtCore.Qt.Key.Key_Tab)
    qtw.QApplication.processEvents()

    assert getattr(host._trace_styles[key], style_name) == value
    assert _rendered_value(line, field) == value
    assert getattr(host._trace_styles[other_key], style_name) == other_style_before
    assert _rendered_value(other, field) == other_rendered_before


def test_fractional_multi_selection_and_arrow_steps(trace_editor):
    host, dialog, lines = trace_editor
    _select_line(host, dialog, lines[0])
    _select_line(host, dialog, lines[1], QtCore.Qt.KeyboardModifier.ControlModifier)
    assert len(dialog._selected_entries()) == 2
    editor = _replace_text(dialog.line_width, 4.5)
    QtTest.QTest.keyClick(editor, QtCore.Qt.Key.Key_Return)
    assert [_rendered_value(line, "line_width") for line in lines] == [4.5, 4.5]

    QtTest.QTest.keyClick(dialog.line_width, QtCore.Qt.Key.Key_Up)
    assert [_rendered_value(line, "line_width") for line in lines] == [5., 5.]
    QtTest.QTest.keyClick(dialog.line_width, QtCore.Qt.Key.Key_Down)
    assert [_rendered_value(line, "line_width") for line in lines] == [4.5, 4.5]


@pytest.mark.parametrize("field,style_name,toggle,value", _FIELDS)
def test_uncommitted_fraction_survives_live_refresh(
    tmp_path, monkeypatch, field, style_name, toggle, value,
):
    with live_view(tmp_path, monkeypatch, .1) as (window, open_plot):
        assert window._database_access_mode == "trusted_live"
        plot = open_plot("signal_a")
        plot.appearance_action.trigger()
        dialog = plot._plot_appearance_dialog
        if toggle is not None:
            QtTest.QTest.mouseClick(getattr(dialog, toggle), QtCore.Qt.MouseButton.LeftButton)
        editor = _replace_text(getattr(dialog, field), int(value))
        worker = plot.worker
        wait_for(lambda: plot.worker is not worker)
        QtTest.QTest.qWait(150)
        assert editor.text() == str(int(value))
        QtTest.QTest.keyClicks(editor, ".5")
        QtTest.QTest.keyClick(editor, QtCore.Qt.Key.Key_Tab)
        assert getattr(plot._trace_styles[plot.label], style_name) == value
        assert _rendered_value(plot.line, field) == value
        np.testing.assert_array_equal(plot.line.getOriginalDataset()[1], [1., 2., 3., 4.])
