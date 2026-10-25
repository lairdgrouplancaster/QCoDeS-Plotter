"""Appearance removal retains native controls and surviving measured curves."""

from fractions import Fraction

import numpy as np
import pytest
from PyQt6 import QtWidgets as qtw

from tests.windows.test_merged_trace_metadata import merge
from tests.windows.test_merged_trace_metadata import (
    merged_plots as _merged_plots,
)
from tests.windows.test_native_averaging import click_average
from tests.windows.test_native_transform_labels import (
    no_callback_errors as _no_callback_errors,
)
from tests.windows.test_plot_csv_precision import _export_from_dialog, _open_csv_dialog
from tests.windows.test_plot_integration import wait_for

merged_plots = _merged_plots
no_callback_errors = _no_callback_errors


def _control_state(plot):
    controls = plot.plot.ctrl
    state = {
        name: getattr(controls, name).isChecked()
        for name in (
            "averageGroup", "subtractMeanCheck", "downsampleCheck",
            "autoDownsampleCheck", "subsampleRadio", "meanRadio", "peakRadio",
            "clipToViewCheck", "alphaGroup", "autoAlphaCheck", "xGridCheck", "yGridCheck",
            "maxTracesCheck", "forgetTracesCheck",
        )
    }
    state["factor"] = controls.downsampleSpin.value()
    state["alpha"] = controls.alphaSlider.value()
    state["grid_alpha"] = controls.gridAlphaSlider.value()
    state["maximum_traces"] = controls.maxTracesSpin.value()
    return state


def _display_oracle(x, y, mode, enabled):
    mean = sum(Fraction(int(value)) for value in y) / len(y)
    centered = np.array([float(Fraction(int(value)) - mean) for value in y])
    if not enabled:
        return x, centered
    groups = [centered[index:index + 4] for index in range(0, len(y), 4)]
    if mode == "subsample":
        return x[::4], centered[::4]
    if mode == "mean":
        return x[2::4], np.array([sum(map(float, group)) / 4 for group in groups])
    return np.repeat(x[2::4], 2), np.array([
        value for group in groups for value in (max(group), min(group))
    ])


@pytest.mark.parametrize("merged_plots", [np.arange(5, 69, dtype=float)], indirect=True)
@pytest.mark.parametrize("primary", [False, True], ids=["remove-secondary", "remove-primary"])
@pytest.mark.parametrize("enabled", [False, True], ids=["downsample-disabled", "downsample-enabled"])
@pytest.mark.parametrize("mode", ["subsample", "mean", "peak"])
def test_appearance_removal_preserves_native_display_and_average(
    merged_plots, tmp_path, monkeypatch, no_callback_errors, primary, enabled, mode,
):
    window, (host, source, other), x, y = merged_plots
    errors = []
    monkeypatch.setattr(qtw.QMessageBox, "critical", lambda *error: errors.append(error))
    merge(window, host, source, x_axis="Bottom", y_axis="Left")
    secondary_key, _line = merge(window, host, other, x_axis="Bottom", y_axis="Left")
    controls = host.plot.ctrl
    controls.subtractMeanCheck.setChecked(True)
    controls.downsampleSpin.setValue(4)
    getattr(controls, f"{mode}Radio").setChecked(True)
    # Preserve the chosen auto mode even while its enclosing control is off.
    controls.autoDownsampleCheck.setChecked(not enabled)
    controls.downsampleCheck.setChecked(enabled)
    controls.clipToViewCheck.setChecked(True)
    controls.alphaGroup.setChecked(True)
    controls.autoAlphaCheck.setChecked(False)
    controls.alphaSlider.setValue(333)
    controls.xGridCheck.setChecked(True)
    controls.yGridCheck.setChecked(True)
    controls.gridAlphaSlider.setValue(85)
    controls.forgetTracesCheck.setChecked(not enabled)
    host.vb.setRange(xRange=[4., 70.], yRange=[-3000., 5000.], padding=0)
    click_average(host)
    qtw.QApplication.processEvents()
    expected_state = _control_state(host)
    expected_x, expected_y = _display_oracle(x, y, mode, enabled)
    assert host.plot.ctrl.avgParamList.count() == 0
    assert all(not host.plot.itemMeta.get(line, {}) for line in host.lines.values())
    before_count, before_average = next(iter(host.plot.avgCurves.values()))
    assert before_count == 3
    np.testing.assert_array_equal(before_average.getData(), [expected_x, expected_y])

    host.appearance_action.trigger()
    dialog = host._plot_appearance_dialog
    key = (next(key for key, line in host.lines.items() if line is host.line)
           if primary else secondary_key)
    dialog.select_trace(key)
    assert dialog.remove_item_button.isEnabled()
    previous_windows = set(window.windows)
    dialog.remove_item_button.click()
    assert not errors
    if primary:
        wait_for(lambda: any(plot not in previous_windows for plot in window.windows))
        target = next(plot for plot in window.windows if plot not in previous_windows)
    else:
        target = host
    wait_for(lambda: hasattr(target, "axis_data") and not target.worker.running)
    target.monitor.stop()
    qtw.QApplication.processEvents()
    assert _control_state(target) == expected_state
    assert len(target.lines) == 2
    assert len(target.plot.avgCurves) == 1
    count, average = next(iter(target.plot.avgCurves.values()))
    assert count == 2
    assert average.isVisible()
    np.testing.assert_array_equal(average.getData(), [expected_x, expected_y])
    for line in target.lines.values():
        np.testing.assert_array_equal(line.getOriginalDataset(), [x, y])
        np.testing.assert_array_equal(line.getData(), [expected_x, expected_y])
        assert line.opacity() == pytest.approx(.333 ** 2)
    for side in ("bottom", "left"):
        assert target.plot.getAxis(side).grid == 85

    export = _open_csv_dialog(target)
    rows = _export_from_dialog(monkeypatch, target, export, tmp_path / "remaining.csv", ",")
    assert len(rows[0]) == 6  # Two exact measurements and their displayed average.
    expected_raw = np.column_stack((x, y, x, y))
    np.testing.assert_array_equal(np.asarray([row[:4] for row in rows[1:]], dtype=float), expected_raw)
    np.testing.assert_array_equal(
        np.asarray([row[4:] for row in rows[1:len(expected_x) + 1]], dtype=float),
        np.column_stack((expected_x, expected_y)),
    )


@pytest.mark.parametrize("merged_plots", [np.arange(5, 21, dtype=float)], indirect=True)
@pytest.mark.parametrize("primary", [False, True], ids=["remove-secondary", "remove-primary"])
@pytest.mark.parametrize("averaging", [False, True], ids=["source-curves", "generated-average"])
def test_removal_keeps_maximum_visible_traces(
    merged_plots, monkeypatch, no_callback_errors, primary, averaging,
):
    window, (host, source, other), x, y = merged_plots
    errors = []
    monkeypatch.setattr(qtw.QMessageBox, "critical", lambda *error: errors.append(error))
    merge(window, host, source, x_axis="Bottom", y_axis="Left")
    other_key, _line = merge(window, host, other, x_axis="Bottom", y_axis="Left")
    if averaging:
        click_average(host)
    host.plot.ctrl.maxTracesSpin.setValue(1)
    host.plot.ctrl.forgetTracesCheck.setChecked(False)
    host.plot.ctrl.maxTracesCheck.setChecked(True)
    assert sum(line.isVisible() for line in host.plot.curves) == 1
    host.appearance_action.trigger()
    dialog = host._plot_appearance_dialog
    key = (next(key for key, line in host.lines.items() if line is host.line)
           if primary else other_key)
    dialog.select_trace(key)
    previous = set(window.windows)
    dialog.remove_item_button.click()
    assert not errors
    if primary:
        wait_for(lambda: any(plot not in previous for plot in window.windows))
        target = next(plot for plot in window.windows if plot not in previous)
    else:
        target = host
    wait_for(lambda: hasattr(target, "axis_data") and not target.worker.running)
    target.monitor.stop()
    qtw.QApplication.processEvents()
    assert target.plot.ctrl.maxTracesCheck.isChecked()
    assert target.plot.ctrl.maxTracesSpin.value() == 1
    assert not target.plot.ctrl.forgetTracesCheck.isChecked()
    assert sum(line.isVisible() for line in target.plot.curves) == 1
    assert len(target.lines) == 2
    if averaging:
        count, average = next(iter(target.plot.avgCurves.values()))
        assert count == 2 and average.isVisible()
        np.testing.assert_array_equal(average.getData(), [x, y])
    for line in target.lines.values():
        np.testing.assert_array_equal(line.getOriginalDataset(), [x, y])
