"""Accepted manual axis edits preserve coherent real Qt/QCoDeS plots."""

import sys
from copy import deepcopy

import numpy as np
import pytest
from PyQt6 import QtCore, QtTest
from PyQt6 import QtWidgets as qtw

from tests.windows.test_merged_trace_metadata import merge
from tests.windows.test_native_fft_coordinates import fft_plots as _fft_plots_fixture
from tests.windows.test_native_transform_labels import (
    click_control,
)
from tests.windows.test_native_transform_labels import (
    no_callback_errors as _no_callback_errors_fixture,
)
from tests.windows.test_plot_integration import wait_for

fft_plots = _fft_plots_fixture
no_callback_errors = _no_callback_errors_fixture


@pytest.fixture(autouse=True)
def no_initial_callback_errors(monkeypatch):
    """Capture callbacks during real measurement-window fixture setup too."""
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *error: errors.append(error))
    yield
    assert errors == []


def _axis_controls(fft_plots, axis):
    window, (plot, source), _x, _y = fft_plots
    if axis in ("x2", "y2"):
        merge(window, plot, source, x_axis="Top", y_axis="Right")
    plot.open_axis_scale_dialog(axis)
    # Showing the dialog and merged axis labels posts Qt layout work. Complete
    # that setup before snapshots: orthogonal overlay coordinates follow the
    # main view's physical pixels even when no axis edit is accepted.
    qtw.QApplication.instance().processEvents()
    plot.plot.layout.activate()
    assert plot.plot.layout.isActivated()
    plot.updateViews(None)
    for name in ("right_vb", "top_vb", "top_right_vb"):
        linked_viewbox = plot.__dict__.get(name)
        if linked_viewbox is not None:
            assert linked_viewbox.geometry() == plot.vb.sceneBoundingRect()
            linked_viewbox.linkedXChanged()
            linked_viewbox.linkedYChanged()
    return plot, plot._axis_scale_controls[axis], plot._axis_scale_viewbox(axis)


def _accept_limits(controls, lower, upper):
    controls.minText.setFocus()
    controls.minText.setText(repr(lower))
    controls.maxText.setText(repr(upper))
    QtTest.QTest.keyClick(controls.minText, QtCore.Qt.Key.Key_Return)


@pytest.mark.parametrize("fft_plots", [{"x": np.array([1.0, 2.0, 3.0])}], indirect=True)
@pytest.mark.parametrize("axis", ["x2", "y2"])
def test_axis_snapshots_follow_completed_overlay_geometry(
    fft_plots, qapplication, axis, monkeypatch,
):
    _window, (plot, _source), _x, _y = fft_plots
    open_dialog = plot.open_axis_scale_dialog
    applications = []
    monkeypatch.setattr(plot, "_apply_axis_scale_manual_limits",
                        lambda *args: applications.append(args))

    def open_with_pending_geometry(requested_axis):
        open_dialog(requested_axis)
        viewbox = plot._axis_scale_viewbox(requested_axis)
        geometry = plot.vb.sceneBoundingRect()
        # Reproduce an overlay awaiting the next main-view resize sync.
        viewbox.setGeometry(geometry.adjusted(0, 0, 0, 1) if axis == "x2"
                            else geometry.adjusted(0, 0, 1, 0))
        QtCore.QTimer.singleShot(0, lambda: plot.updateViews(None))

    monkeypatch.setattr(plot, "open_axis_scale_dialog", open_with_pending_geometry)
    _plot, _controls, viewbox = _axis_controls(fft_plots, axis)
    assert plot.plot.layout.isActivated()
    for name in ("right_vb", "top_vb", "top_right_vb"):
        assert plot.__dict__[name].geometry() == plot.vb.sceneBoundingRect()
    before = deepcopy(viewbox.getState())
    before_view = deepcopy(viewbox.viewRange())
    qapplication.processEvents()
    plot.updateViews(None)
    np.testing.assert_array_equal(viewbox.viewRange(), before_view)
    assert viewbox.getState()["targetRange"] == before["targetRange"]
    assert viewbox.getState()["autoRange"] == before["autoRange"]
    assert applications == []


@pytest.mark.parametrize("fft_plots", [{"x": np.array([1.0, 2.0, 3.0])}], indirect=True)
@pytest.mark.parametrize("axis", ["x", "y", "x2", "y2"])
@pytest.mark.parametrize("automatic", [False, True])
def test_unsupported_accepted_limits_leave_range_and_mode_unchanged(
    fft_plots, no_callback_errors, qapplication, axis, automatic, monkeypatch,
):
    plot, controls, viewbox = _axis_controls(fft_plots, axis)
    statuses = []
    monkeypatch.setattr(plot, "show_status", lambda message, *_args: statuses.append(message))
    axis_number = plot._axis_scale_axis_number(axis)
    radio = controls.autoRadio if automatic else controls.manualRadio
    QtTest.QTest.mouseClick(radio, QtCore.Qt.MouseButton.LeftButton,
                          pos=QtCore.QPoint(8, radio.height() // 2))
    qapplication.processEvents()
    assert controls.autoRadio.isChecked() == automatic
    before = deepcopy(viewbox.getState())
    before_view = deepcopy(viewbox.viewRange())
    before_fields = (controls.minText.text(), controls.maxText.text())
    before_custom_auto = set(plot._axis_scale_custom_auto_axes)
    for lower, upper in (
        (-1e308, 1e308), (1e308, 1.1e308), (-1.1e308, -1e308),
        (float("nan"), 1.0), (2.0, 1.0),
    ):
        _accept_limits(controls, lower, upper)
        qapplication.processEvents()
        np.testing.assert_array_equal(viewbox.viewRange(), before_view)
        assert viewbox.getState()["targetRange"] == before["targetRange"]
        assert viewbox.autoRangeEnabled()[axis_number] == before["autoRange"][axis_number]
        assert controls.autoRadio.isChecked() == automatic
        assert controls.manualRadio.isChecked() != automatic
        assert (controls.minText.text(), controls.maxText.text()) == before_fields
        assert plot._axis_scale_custom_auto_axes == before_custom_auto
        expected_status = "finite numbers" if not lower < upper else "supported plot range"
        assert expected_status in statuses[-1]


@pytest.mark.parametrize("fft_plots", [{"x": np.array([1.0, 2.0, 3.0])}], indirect=True)
@pytest.mark.parametrize("axis", ["x", "y", "x2", "y2"])
def test_small_finite_manual_ranges_remain_available(
    fft_plots, no_callback_errors, qapplication, axis,
):
    plot, controls, viewbox = _axis_controls(fft_plots, axis)
    axis_number = plot._axis_scale_axis_number(axis)
    for lower, upper in ((0.0, 1e-320), (1e-320, 2e-320), (-1e306, 1e306)):
        _accept_limits(controls, lower, upper)
        qapplication.processEvents()
        np.testing.assert_array_equal(viewbox.viewRange()[axis_number], [lower, upper])
        assert controls.manualRadio.isChecked()


@pytest.mark.parametrize("fft_plots", [{"x": np.array([1.0, 2.0, 3.0])}], indirect=True)
@pytest.mark.parametrize("axis", ["x", "y", "x2", "y2"])
def test_large_physical_log_limits_use_representable_view_coordinates(
    fft_plots, no_callback_errors, qapplication, axis,
):
    plot, controls, viewbox = _axis_controls(fft_plots, axis)
    QtTest.QTest.mouseClick(controls.logCheck, QtCore.Qt.MouseButton.LeftButton,
                          pos=QtCore.QPoint(8, controls.logCheck.height() // 2))
    assert plot._axis_scale_log_mode(axis)
    _accept_limits(controls, 1e308, 1.1e308)
    qapplication.processEvents()
    np.testing.assert_array_equal(viewbox.viewRange()[plot._axis_scale_axis_number(axis)],
                                  np.log10([1e308, 1.1e308]))
    assert controls.manualRadio.isChecked()


@pytest.mark.parametrize("fft_plots", [{"x": np.array([1.0, 2.0, 3.0])}], indirect=True)
@pytest.mark.parametrize("axis", ["x", "y", "x2", "y2"])
@pytest.mark.parametrize("automatic", [False, True])
def test_rejected_nonpositive_log_edit_preserves_current_mode(
    fft_plots, no_callback_errors, qapplication, axis, automatic,
):
    plot, controls, viewbox = _axis_controls(fft_plots, axis)
    QtTest.QTest.mouseClick(controls.logCheck, QtCore.Qt.MouseButton.LeftButton,
                          pos=QtCore.QPoint(8, controls.logCheck.height() // 2))
    qapplication.processEvents()
    assert plot._axis_scale_log_mode(axis)
    radio = controls.autoRadio if automatic else controls.manualRadio
    QtTest.QTest.mouseClick(radio, QtCore.Qt.MouseButton.LeftButton,
                          pos=QtCore.QPoint(8, radio.height() // 2))
    qapplication.processEvents()
    assert controls.autoRadio.isChecked() == automatic
    before = deepcopy(viewbox.getState())
    fields = (controls.minText.text(), controls.maxText.text())
    for lower in (0.0, -1.0):
        _accept_limits(controls, lower, 10.0)
        qapplication.processEvents()
        assert viewbox.getState()["targetRange"] == before["targetRange"]
        assert viewbox.getState()["autoRange"] == before["autoRange"]
        assert controls.autoRadio.isChecked() == automatic
        assert (controls.minText.text(), controls.maxText.text()) == fields


@pytest.mark.parametrize("fft_plots", [{"x": np.array([1.0, 2.0, 3.0])}], indirect=True)
@pytest.mark.parametrize("axis", ["x", "y", "x2", "y2"])
def test_partial_axis_input_is_not_applied_before_acceptance(
    fft_plots, no_callback_errors, qapplication, axis,
):
    _plot, controls, viewbox = _axis_controls(fft_plots, axis)
    before = deepcopy(viewbox.getState())
    controls.minText.setFocus()
    controls.minText.selectAll()
    QtTest.QTest.keyClicks(controls.minText, "-1e")
    qapplication.processEvents()
    assert controls.minText.text() == "-1e"
    assert viewbox.getState()["targetRange"] == before["targetRange"]
    assert viewbox.getState()["autoRange"] == before["autoRange"]
    controls.minText.setText("1.0")


@pytest.mark.parametrize("fft_plots", [{"x": np.array([1.0, 2.0, 3.0])}], indirect=True)
@pytest.mark.parametrize("axis", ["x", "y", "x2", "y2"])
def test_copy_auto_rejects_unsupported_limits_without_changing_auto_state(
    fft_plots, no_callback_errors, qapplication, axis, monkeypatch,
):
    plot, controls, viewbox = _axis_controls(fft_plots, axis)
    QtTest.QTest.mouseClick(controls.autoRadio, QtCore.Qt.MouseButton.LeftButton,
                          pos=QtCore.QPoint(8, controls.autoRadio.height() // 2))
    qapplication.processEvents()
    before = deepcopy(viewbox.getState())
    before_fields = (controls.minText.text(), controls.maxText.text())
    before_custom_auto = set(plot._axis_scale_custom_auto_axes)
    statuses = []
    monkeypatch.setattr(plot, "show_status", lambda message, *_args: statuses.append(message))
    monkeypatch.setattr(plot, "_axis_scale_auto_limits", lambda _axis: (-1e308, 1e308))
    plot._update_axis_scale_auto_limits_tooltip(axis)
    assert controls.copyAutoLimitsButton.isEnabled()
    QtTest.QTest.mouseClick(controls.copyAutoLimitsButton, QtCore.Qt.MouseButton.LeftButton)
    qapplication.processEvents()
    assert viewbox.getState()["targetRange"] == before["targetRange"]
    assert viewbox.getState()["autoRange"] == before["autoRange"]
    assert plot._axis_scale_custom_auto_axes == before_custom_auto
    assert (controls.minText.text(), controls.maxText.text()) == before_fields
    assert controls.autoRadio.isChecked()
    assert "supported plot range" in statuses[-1]


@pytest.mark.parametrize("axis,fft_plots", [
    ("x", {"x": np.array([1e308, 1.1e308]), "y": np.array([1.0, 2.0])}),
    ("x2", {"x": np.array([1e308, 1.1e308]), "y": np.array([1.0, 2.0])}),
    ("y", {"x": np.array([1.0, 2.0]), "y": np.array([1e308, 1.1e308])}),
    ("y2", {"x": np.array([1.0, 2.0]), "y": np.array([1e308, 1.1e308])}),
], indirect=["fft_plots"])
def test_initial_unsupported_auto_preserves_samples_and_recovers_on_log_refresh(
    fft_plots, no_callback_errors, qapplication, axis, monkeypatch,
):
    plot, controls, viewbox = _axis_controls(fft_plots, axis)
    _window, _plots, raw_x, raw_y = fft_plots
    axis_number = plot._axis_scale_axis_number(axis)
    assert axis in plot._axis_scale_custom_auto_axes
    assert controls.autoRadio.isChecked()
    before = deepcopy(viewbox.viewRange())
    assert all(np.isfinite(before).ravel())
    original_x, original_y = plot.line.getOriginalDataset()
    np.testing.assert_array_equal(original_x, raw_x)
    np.testing.assert_array_equal(original_y, raw_y)
    other_number = 1 - axis_number
    other_auto = viewbox.autoRangeEnabled()[other_number]
    statuses = []
    monkeypatch.setattr(plot, "show_status", lambda message, *_args: statuses.append(message))
    QtTest.QTest.mouseClick(controls.copyAutoLimitsButton, QtCore.Qt.MouseButton.LeftButton)
    qapplication.processEvents()
    np.testing.assert_array_equal(viewbox.viewRange(), before)
    assert controls.autoRadio.isChecked()
    assert "supported plot range" in statuses[-1]
    assert viewbox.autoRangeEnabled()[other_number] == other_auto

    # Reject an Auto request from a manual, supported range as well.
    _accept_limits(controls, 0.1, 0.9)
    qapplication.processEvents()
    assert controls.manualRadio.isChecked()
    manual = deepcopy(viewbox.getState())
    QtTest.QTest.mouseClick(controls.autoRadio, QtCore.Qt.MouseButton.LeftButton,
                          pos=QtCore.QPoint(8, controls.autoRadio.height() // 2))
    qapplication.processEvents()
    assert controls.manualRadio.isChecked()
    assert viewbox.getState()["targetRange"] == manual["targetRange"]
    assert viewbox.getState()["autoRange"] == manual["autoRange"]
    assert "supported plot range" in statuses[-1]

    QtTest.QTest.mouseClick(controls.logCheck, QtCore.Qt.MouseButton.LeftButton,
                          pos=QtCore.QPoint(8, controls.logCheck.height() // 2))
    qapplication.processEvents()
    assert plot._axis_scale_log_mode(axis)
    assert controls.autoRadio.isChecked()
    assert axis in plot._axis_scale_custom_auto_axes
    log_range = viewbox.viewRange()[axis_number]
    assert 307.0 < log_range[0] < log_range[1] < 309.0
    plot.refreshWindow(force=True)
    wait_for(lambda: not plot.worker.running)
    plot.monitor.stop()
    assert controls.autoRadio.isChecked()
    assert 307.0 < viewbox.viewRange()[axis_number][0] < viewbox.viewRange()[axis_number][1] < 309.0
    np.testing.assert_array_equal(plot.line.getOriginalDataset()[0], raw_x)
    np.testing.assert_array_equal(plot.line.getOriginalDataset()[1], raw_y)


@pytest.mark.parametrize("axis,fft_plots", [
    ("x", {"x": np.array([1e308, 1e308]), "y": np.array([1.0, 2.0])}),
    ("x", {"x": np.array([-1e308, -1e308]), "y": np.array([1.0, 2.0])}),
    ("y", {"x": np.array([1.0, 2.0]), "y": np.array([1e308, 1e308])}),
    ("y", {"x": np.array([1.0, 2.0]), "y": np.array([-1e308, -1e308])}),
], indirect=["fft_plots"])
def test_constant_extreme_auto_is_explicitly_rejected_without_losing_samples(
    fft_plots, no_callback_errors, qapplication, axis, monkeypatch,
):
    plot, controls, viewbox = _axis_controls(fft_plots, axis)
    _window, _plots, raw_x, raw_y = fft_plots
    assert axis in plot._axis_scale_custom_auto_axes
    assert controls.autoRadio.isChecked()
    before = deepcopy(viewbox.viewRange())
    assert all(np.isfinite(before).ravel())
    statuses = []
    monkeypatch.setattr(plot, "show_status", lambda message, *_args: statuses.append(message))
    assert plot._axis_scale_auto_limits(axis) is not None
    QtTest.QTest.mouseClick(controls.autoRadio, QtCore.Qt.MouseButton.LeftButton,
                          pos=QtCore.QPoint(8, controls.autoRadio.height() // 2))
    qapplication.processEvents()
    np.testing.assert_array_equal(viewbox.viewRange(), before)
    assert "supported plot range" in statuses[-1]
    np.testing.assert_array_equal(plot.line.getOriginalDataset()[0], raw_x)
    np.testing.assert_array_equal(plot.line.getOriginalDataset()[1], raw_y)


@pytest.mark.parametrize("fft_plots", [
    {"x": np.arange(4), "y": np.array([2**60 + k for k in range(4)], dtype=np.int64)},
    {"x": np.arange(4), "y": np.array([2**64 - 4 + k for k in range(4)], dtype=np.uint64)},
], indirect=True)
def test_default_clipping_fft_auto_includes_ac_and_dc_bins(
    fft_plots, no_callback_errors, qapplication,
):
    _window, (plot, _source), raw_x, raw_y = fft_plots
    original_limit = plot.line.opts["dynamicRangeLimit"]
    assert original_limit is not None
    click_control(plot, "fftCheck")
    qapplication.processEvents()
    expected_ac = np.abs(np.fft.rfft(np.arange(4, dtype=float)) / 4)[1:]
    np.testing.assert_allclose(plot.line.getData()[1][1:], expected_ac, rtol=1e-14, atol=0)
    lower, upper = plot.vb.viewRange()[1]
    assert lower <= expected_ac.min() and upper >= float(raw_y[0])
    assert "y" in plot._axis_scale_custom_auto_axes
    assert plot.line.opts["dynamicRangeLimit"] == original_limit
    np.testing.assert_array_equal(plot.line.getOriginalDataset()[0], raw_x)
    np.testing.assert_array_equal(plot.line.getOriginalDataset()[1], raw_y)
