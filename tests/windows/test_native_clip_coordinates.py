"""Native viewport clipping must retain acquired sweep/phase branches."""

import numpy as np
import pytest
from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from qplot.windows._plot1d_snap import _line_snap_data
from tests.windows.test_native_transform_labels import click_control
from tests.windows.test_native_transform_labels import (
    no_callback_errors as _no_callback_errors,
)
from tests.windows.test_numeric_sample_preservation import array_plot
from tests.windows.test_plot_csv_precision import _export_from_dialog, _open_csv_dialog
from tests.windows.test_plot_overlay_integration import _assign_trace_axes

no_callback_errors = _no_callback_errors


def _mapped_samples(x, y, phase, log_x):
    if phase:
        # Python integer subtraction supplies an independent secant oracle.
        mapped_x = y[:-1]
        mapped_y = np.array([
            (int(after) - int(before)) / (int(right) - int(left))
            for left, right, before, after in zip(x[:-1], x[1:], y[:-1], y[1:], strict=True)
        ])
    else:
        mapped_x, mapped_y = x, y
    return np.log10(mapped_x) if log_x else mapped_x, mapped_y


def _assert_visible_samples(line, x, y, bounds):
    """Compare every in-range pair, retaining its original acquisition order."""
    displayed_x, displayed_y = line.getData()
    wanted = (x >= bounds[0]) & (x <= bounds[1])
    visible = (displayed_x >= bounds[0]) & (displayed_x <= bounds[1])
    assert wanted.sum() > 1
    np.testing.assert_array_equal(displayed_x[visible], x[wanted])
    np.testing.assert_array_equal(displayed_y[visible], y[wanted])
    snap = _line_snap_data(line)
    assert snap is not None
    np.testing.assert_array_equal(snap.x_view, x)
    np.testing.assert_array_equal(snap.y_view, y)


@pytest.mark.parametrize("mode", ["ascending", "descending", "reversing", "phase"])
@pytest.mark.parametrize("secondary", [False, True], ids=["primary", "secondary"])
@pytest.mark.parametrize("log_x", [False, True], ids=["linear", "log-x"])
def test_real_sweep_clipping_keeps_all_visible_branches(
    tmp_path, monkeypatch, no_callback_errors, mode, secondary, log_x,
):
    x = np.arange(1, 65, dtype=np.int64)
    y = (x - 32) ** 2 + 3
    if mode == "descending":
        x, y = x[::-1], y[::-1]
    elif mode == "reversing":
        x = np.r_[x[:32], x[:32][::-1]]
        y = np.arange(1, 65, dtype=np.int64)
    phase = mode == "phase"
    expected_x, expected_y = _mapped_samples(x, y, phase, log_x)
    with array_plot(tmp_path, monkeypatch, [{"x": x, "signal": y}]) as (_window, plot):
        owner = plot.vb
        if secondary:
            key = next(key for key, line in plot.lines.items() if line is plot.line)
            owner = _assign_trace_axes(plot, key, "Top", "Right")
        if phase:
            click_control(plot, "phasemapCheck")
        plot._axis_scale_log_toggled("x2" if secondary else "x", log_x)
        assert not plot.plot.ctrl.clipToViewCheck.isChecked()
        np.testing.assert_array_equal(plot.line.getData(), [expected_x, expected_y])
        bounds_list = [(20., 120.), (40., 400.)] if phase else [(10., 20.), (22., 28.)]
        for limits in bounds_list:
            bounds = np.log10(limits) if log_x else limits
            owner.setRange(xRange=bounds, yRange=[-80., 1100.], padding=0)
            for clipped in (True, False, True):
                plot.plot.ctrl.clipToViewCheck.setChecked(clipped)
                qtw.QApplication.processEvents()
                assert plot.line.opts["clipToView"] == clipped
                _assert_visible_samples(plot.line, expected_x, expected_y, bounds)

        # Selection and the curve must describe the same physical samples;
        # viewport clipping must not alter the full-resolution CSV source.
        bounds = np.log10(bounds_list[0]) if log_x else bounds_list[0]
        plot.set_marquee_rect(QtCore.QRectF(bounds[0], -80., bounds[1] - bounds[0], 1180.))
        rect = plot.marquee.normalized()
        selected = expected_y[(expected_x >= rect.left()) & (expected_x <= rect.right())]
        np.testing.assert_array_equal(plot._marquee_line_values(), selected)
        dialog = _open_csv_dialog(plot)
        rows = _export_from_dialog(monkeypatch, plot, dialog, tmp_path / "sweep.csv", ",")
        np.testing.assert_array_equal(np.asarray(rows[1:], dtype=np.int64), np.column_stack((x, y)))


def test_clip_guard_tracks_native_mapping_and_preserves_ascending_optimization(
    tmp_path, monkeypatch, no_callback_errors,
):
    x = np.arange(1, 65, dtype=np.int64)
    y = (x - 32) ** 2 + 3
    with array_plot(tmp_path, monkeypatch, [{"x": x, "signal": y}]) as (_window, plot):
        plot.plot.ctrl.clipToViewCheck.setChecked(True)
        for phase in (False, True, False, True, False):
            plot.plot.ctrl.phasemapCheck.setChecked(phase)
            expected_x, expected_y = _mapped_samples(x, y, phase, False)
            bounds = (20., 120.) if phase else (10., 20.)
            plot.vb.setRange(xRange=bounds, yRange=[-80., 1100.], padding=0)
            qtw.QApplication.processEvents()
            _assert_visible_samples(plot.line, expected_x, expected_y, bounds)
            displayed = plot.line.getData()[0]
            if not phase:
                assert len(displayed) < len(x)
            assert plot.line.opts["clipToView"]
            assert plot.plot.ctrl.clipToViewCheck.isChecked()
