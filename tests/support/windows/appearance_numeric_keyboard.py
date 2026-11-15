"""Shared fixtures and assertions for appearance numeric keyboard."""

import numpy as np
import pytest
from PyQt6 import QtCore, QtTest
from PyQt6 import QtWidgets as qtw

from tests.support.windows.plot_integration import wait_for
from tests.support.windows.typed_run_keyboard_launch import (
    _drain,
    _launch,
    _type_run,
)
from tests.support.windows.typed_run_keyboard_launch import (
    typed_run_window as _typed_run_window,
)

__all__ = [
    '_replace_text',
    '_select_line',
    'trace_editor',
    'typed_run_window',
]


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
    window = spin.window()
    # Window exposure/activation is asynchronous on the Windows CI runner.
    # Finish it before typing: a late focus transition commits a spin box's
    # partial text even when keyboard tracking is disabled.
    assert QtTest.QTest.qWaitForWindowExposed(window)
    window.activateWindow()
    assert QtTest.QTest.qWaitForWindowActive(window)
    editor.setFocus()
    wait_for(editor.hasFocus)
    QtTest.QTest.keyClick(editor, QtCore.Qt.Key.Key_A,
                         QtCore.Qt.KeyboardModifier.ControlModifier)
    assert editor.selectedText() == editor.text()
    QtTest.QTest.keyClicks(editor, str(value))
    return editor
