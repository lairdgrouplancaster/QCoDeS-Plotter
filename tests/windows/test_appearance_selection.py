"""Programmatic appearance selection ignores held Ctrl and Shift keys."""

import pytest
from PyQt6 import QtCore, QtTest
from PyQt6 import QtWidgets as qtw

from qplot.windows._plot2d_layers import _HeatmapAppearanceDialog
from tests.windows.test_appearance_numeric_keyboard import (
    trace_editor as _trace_editor,
)
from tests.windows.test_appearance_numeric_keyboard import (
    typed_run_window as _typed_run_window,
)
from tests.windows.test_heatmap_appearance import (
    _Host,
    _key,
    _Layer,
    _retire_appearance_widgets,
)

trace_editor = _trace_editor
typed_run_window = _typed_run_window


@pytest.fixture(params=[
    QtCore.Qt.KeyboardModifier.NoModifier,
    QtCore.Qt.KeyboardModifier.ControlModifier,
    QtCore.Qt.KeyboardModifier.ShiftModifier,
], ids=["none", "ctrl", "shift"])
def selection_modifier(request, qapplication):
    receiver = qtw.QWidget()
    receiver.show()
    qapplication.processEvents()

    def hold_modifier():
        # QWindow input updates Qt's process-wide modifier state, as a real
        # Ctrl/Shift mouse click does; QWidget-only key events do not.
        QtTest.QTest.mouseClick(
            receiver.windowHandle(), QtCore.Qt.MouseButton.LeftButton, request.param,
        )
        assert qapplication.keyboardModifiers() == request.param

    try:
        yield hold_modifier
    finally:
        QtTest.QTest.mouseClick(receiver.windowHandle(), QtCore.Qt.MouseButton.LeftButton)
        receiver.deleteLater()
        qapplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


def test_select_heatmap_replaces_selection_and_edits_only_that_layer(selection_modifier):
    host = _Host()
    key = _key("overlay-guid", "overlay")
    layer = _Layer(key)
    host.heatmaps[key] = layer
    dialog = _HeatmapAppearanceDialog(host)
    try:
        dialog.refresh_rows()
        selection_modifier()
        assert dialog.select_heatmap(key)
        assert dialog._selected_keys() == [key]
        assert dialog.table.currentRow() == 1
        dialog.opacity_slider.setValue(37)
        assert layer.opacity == 0.37
        assert host._heatmap_opacity(host) == 1.0
    finally:
        _retire_appearance_widgets(host, dialog)


def test_added_heatmap_is_selected_and_removable(selection_modifier):
    host = _Host()
    key = _key("candidate-guid", "candidate")
    host._heatmap_candidate_provider = lambda: [("ID:3 candidate", key)]

    def add_heatmap(label, key):
        host.heatmaps[key] = _Layer(key, label)
        return True

    host.add_heatmap_from_dialog = add_heatmap
    dialog = _HeatmapAppearanceDialog(host)
    try:
        dialog.refresh_rows()
        dialog.add_heatmap_combo.setCurrentIndex(dialog.add_heatmap_combo.findData(key))
        selection_modifier()
        dialog.add_heatmap_button.click()
        assert dialog._selected_keys() == [key]
        assert dialog.remove_heatmap_button.isEnabled()
    finally:
        _retire_appearance_widgets(host, dialog)


def test_select_trace_replaces_selection_and_edits_only_that_trace(trace_editor, selection_modifier):
    host, dialog, lines = trace_editor
    key = next(key for key, line in host.lines.items() if line is lines[1])
    other_key = next(key for key, line in host.lines.items() if line is lines[0])
    original_opacity = host._trace_styles[other_key].opacity
    assert dialog.select_trace(other_key)
    selection_modifier()
    assert dialog.select_trace(key)
    assert dialog._selected_entries() == [("trace", key)]
    assert dialog._row_entry(dialog.table.currentRow()) == ("trace", key)
    dialog.opacity.setValue(37)
    assert host._trace_styles[key].opacity == 0.37
    assert host._trace_styles[other_key].opacity == original_opacity
