"""Clip-to-view survives trace reparenting through the real appearance controls."""

import sys

import numpy as np
import pytest
from PyQt6 import QtWidgets as qtw

from tests.windows.test_native_transform_labels import click_control
from tests.windows.test_plot_overlay_integration import loaded_plot as loaded_plot


@pytest.mark.parametrize("loaded_plot", [True], indirect=True)
@pytest.mark.parametrize("secondary", [False, True])
@pytest.mark.parametrize("derivative", [False, True])
def test_clipped_trace_axis_changes_rebuild_display_without_callback_errors(
    loaded_plot, monkeypatch, secondary, derivative,
):
    window = loaded_plot
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *error: errors.append(error))
    key = (next(key for key, line in window.lines.items() if line is not window.line)
           if secondary else window.label)
    line = window.lines[key]
    raw_x, raw_y = (values.copy() for values in line.getOriginalDataset())
    window.plot.ctrl.clipToViewCheck.setChecked(True)
    if derivative:
        click_control(window, "derivativeCheck")
    window.plot.setDownsampling(ds=1, auto=False)

    for x_side, y_side in (
        ("Top", "Right"), ("Bottom", "Right"),
        ("Top", "Left"), ("Bottom", "Left"),
    ):
        window.open_trace_appearance_dialog(key)
        dialog = window._trace_appearance_dialog
        dialog.x_axis.setCurrentText(x_side)
        dialog.y_axis.setCurrentText(y_side)
        dialog.reject()
        qtw.QApplication.processEvents()
        assert not errors, [(kind.__name__, str(error)) for kind, error, _ in errors]
        assert line.opts["clipToView"]
        for dimension, bounds in (("x", (20.2, 29.8)), ("y", (-5, 100))):
            axis = window._axis_scale_axis_for_line(line, dimension)
            owner = window._axis_scale_viewbox(axis)
            setter = owner.setXRange if dimension == "x" else owner.setYRange
            setter(*bounds, padding=0)
        qtw.QApplication.processEvents()
        visible_x, visible_y = line.getData()
        # Linked viewboxes align by scene pixels; require the same physical
        # samples as their actual view, including the surrounding boundary pair.
        low, high = line.getViewBox().viewRange()[0]
        processed_x = raw_x[:-1] if derivative else raw_x
        start = max(0, np.searchsorted(processed_x, low, side="left") - 1)
        end = min(len(processed_x), np.searchsorted(processed_x, high, side="right") + 1)
        expected_x = processed_x[start:end]
        expected_y = (np.ones_like(raw_y[:-1]) if derivative else raw_y)[start:end]
        assert 0 < len(expected_x) < len(raw_x)
        np.testing.assert_array_equal(visible_x, expected_x)
        np.testing.assert_array_equal(visible_y, expected_y)
        np.testing.assert_array_equal(line.curve.getData(), [expected_x, expected_y])
        np.testing.assert_array_equal(line.getOriginalDataset(), [raw_x, raw_y])
        assert not errors, [(kind.__name__, str(error)) for kind, error, _ in errors]
