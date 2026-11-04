"""Range-format interactions through real axis and color-scale controls."""

import sys
from copy import deepcopy

import numpy as np
import pytest
from PyQt6 import QtCore, QtTest

from tests.windows.test_axis_limit_precision import AxisLimitWindow
from tests.windows.test_color_scale_locale import heatmap as _heatmap_fixture

heatmap_source = _heatmap_fixture


@pytest.fixture
def heatmap(heatmap_source, monkeypatch):
    # The minimal renderer fixture has no plot status widgets. Keep validation
    # observable without depending on unrelated window/toolbar construction.
    heatmap_source.statuses = []
    monkeypatch.setattr(
        heatmap_source, "show_status",
        lambda message, *_args: heatmap_source.statuses.append(message),
    )
    return heatmap_source


@pytest.fixture(autouse=True)
def no_callback_errors(monkeypatch):
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *error: errors.append(error))
    yield
    assert not errors


@pytest.fixture
def axis_window(qapplication):
    window = AxisLimitWindow()
    window.plot.plot([1, 2, 3], [2, 4, 6])
    window.resize(800, 600)
    window.show()
    window.open_axis_scale_dialog("x")
    qapplication.processEvents()
    yield window
    window._axis_scale_dialog.close()
    window.close()
    window.deleteLater()
    qapplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


def click(button):
    QtTest.QTest.mouseClick(button, QtCore.Qt.MouseButton.LeftButton)


def accept(first, second, values):
    first.setFocus()
    first.setText(str(values[0]))
    second.setText(str(values[1]))
    QtTest.QTest.keyClick(first, QtCore.Qt.Key.Key_Return)


@pytest.mark.parametrize("axis", ["x", "y"])
@pytest.mark.parametrize("automatic", [False, True])
def test_axis_switch_preserves_range_auto_and_per_axis_format(
    axis_window, qapplication, axis, automatic,
):
    window = axis_window
    window.open_axis_scale_dialog(axis)
    ui = window._axis_scale_controls[axis]
    number = window._axis_scale_axis_number(axis)
    if not automatic:
        click(ui.copyAutoLimitsButton)
    qapplication.processEvents()
    original = deepcopy(window.vb.getState())
    assert ui.rangeEntry.min_max_button.isChecked()
    assert ui.minimumLabel.text() == "Minimum"
    ui.minText.setFocus()
    click(ui.rangeEntry.center_span_button)
    qapplication.processEvents()
    assert window.vb.getState()["targetRange"] == original["targetRange"]
    assert window.vb.getState()["autoRange"] == original["autoRange"]
    low, high = original["targetRange"][number]
    np.testing.assert_allclose(
        [float(ui.minText.text()), float(ui.maxText.text())],
        [(low + high) / 2, high - low],
    )
    assert ui.minimumLabel.text() == "Center"
    assert ui.maximumLabel.text() == "Span"
    assert ui.maxText.accessibleName() == f"{axis.upper()} axis span"
    other = window._axis_scale_controls["y" if axis == "x" else "x"]
    assert not other.rangeEntry.center_span
    window._axis_scale_dialog.close()
    window.open_axis_scale_dialog(axis)
    assert ui.rangeEntry.center_span_button.isChecked()
    click(ui.rangeEntry.min_max_button)
    assert window.vb.getState()["targetRange"] == original["targetRange"]
    assert window.vb.getState()["autoRange"] == original["autoRange"]


@pytest.mark.parametrize("axis", ["x", "y"])
def test_axis_center_span_edits_pan_and_copy_auto(axis_window, axis):
    window = axis_window
    window.open_axis_scale_dialog(axis)
    ui = window._axis_scale_controls[axis]
    number = window._axis_scale_axis_number(axis)
    click(ui.rangeEntry.center_span_button)
    accept(ui.minText, ui.maxText, (3, 8))
    assert window.vb.viewRange()[number] == [-1, 7]
    assert ui.manualRadio.isChecked()
    assert ui.rangeEntry.summary.text() == "Minimum -1   ·   Maximum 7"
    # A programmatic pan/zoom is mirrored in the selected representation.
    setter = window.vb.setXRange if axis == "x" else window.vb.setYRange
    setter(2, 6, padding=0)
    assert (float(ui.minText.text()), float(ui.maxText.text())) == (4, 4)
    expected = window._axis_scale_auto_limits(axis)
    click(ui.copyAutoLimitsButton)
    np.testing.assert_array_equal(window.vb.viewRange()[number], expected)
    assert ui.rangeEntry.center_span
    assert ui.manualRadio.isChecked()


@pytest.mark.parametrize("values", [(0, 0), (0, -2), ("nan", 2), (0, "inf"),
                                         (1.7e308, 1e308), (1e20, 1)])
def test_invalid_axis_center_span_preserves_auto_and_fields(axis_window, values):
    window = axis_window
    ui = window._axis_scale_controls["x"]
    click(ui.rangeEntry.center_span_button)
    original = deepcopy(window.vb.getState())
    texts = ui.minText.text(), ui.maxText.text()
    accept(ui.minText, ui.maxText, values)
    assert window.vb.getState()["targetRange"] == original["targetRange"]
    assert window.vb.getState()["autoRange"] == original["autoRange"]
    assert (ui.minText.text(), ui.maxText.text()) == texts
    assert window.statuses


def test_log_center_span_uses_physical_values_and_preserves_exact_view(axis_window):
    window = axis_window
    ui = window._axis_scale_controls["x"]
    ui.logCheck.click()
    click(ui.rangeEntry.center_span_button)
    accept(ui.minText, ui.maxText, (55, 90))
    np.testing.assert_allclose(window.vb.viewRange()[0], [1, 2])
    before = deepcopy(window.vb.getState())
    for _ in range(3):
        click(ui.rangeEntry.min_max_button)
        click(ui.rangeEntry.center_span_button)
        QtTest.QTest.keyClick(ui.minText, QtCore.Qt.Key.Key_Return)
    assert window.vb.getState()["targetRange"] == before["targetRange"]
    accept(ui.minText, ui.maxText, (1, 4))
    assert window.vb.getState()["targetRange"] == before["targetRange"]
    assert "greater than zero" in window.statuses[-1]


def test_color_switch_preserves_auto_and_unitless_summary(heatmap, qapplication):
    entry = heatmap.colorbar_range_entry
    assert entry.min_max_button.isChecked()
    heatmap.colorbar_min_text.setFocus()
    click(entry.center_span_button)
    qapplication.processEvents()
    assert heatmap._colorbar_manual_levels is None
    assert heatmap.relevel_refresh.isChecked()
    assert tuple(heatmap.bar.levels()) == (0, 3)
    assert entry.summary.text() == "Minimum 0   ·   Maximum 3"
    assert heatmap.colorbar_min_label.text() == "Center"
    assert heatmap.colorbar_max_text.accessibleName() == "Color scale span"
    # Auto updates and reopening both retain the independent selection.
    heatmap.dataGrid = np.array([[-10, 70]])
    heatmap.scaleColorbar()
    heatmap.colorbar_scale_dialog.close()
    heatmap.open_colorbar_scale_dialog()
    assert entry.center_span_button.isChecked()
    assert float(heatmap.colorbar_min_text.text()) == 30
    assert float(heatmap.colorbar_max_text.text()) == 80
    assert heatmap._colorbar_manual_levels is None
    click(heatmap.colorbar_copy_auto_limits_button)
    assert heatmap._colorbar_manual_levels == (-10, 70)
    assert entry.center_span


def test_color_center_span_edits_and_auto(heatmap):
    entry = heatmap.colorbar_range_entry
    click(entry.center_span_button)
    accept(heatmap.colorbar_min_text, heatmap.colorbar_max_text, (30, 80))
    assert heatmap._colorbar_manual_levels == (-10, 70)
    assert tuple(heatmap.bar.levels()) == (-10, 70)
    assert not heatmap.relevel_refresh.isChecked()
    assert entry.summary.text() == "Minimum -10   ·   Maximum 70"
    heatmap.colorbar_auto_radio.click()
    assert heatmap._colorbar_manual_levels is None
    assert tuple(heatmap.bar.levels()) == (0, 3)
    assert float(heatmap.colorbar_min_text.text()) == 1.5
    assert float(heatmap.colorbar_max_text.text()) == 3
    assert entry.center_span


@pytest.mark.parametrize("values", [(0, 0), (0, -1), ("nan", 2), (0, "1,5"),
                                         (1.7e308, 1e308), (1e20, 1)])
def test_invalid_color_center_span_preserves_auto(heatmap, values):
    entry = heatmap.colorbar_range_entry
    click(entry.center_span_button)
    heatmap.colorbar_min_text.setText(str(values[0]))
    heatmap.colorbar_max_text.setText(str(values[1]))
    heatmap.colorbar_manual_radio.click()
    assert heatmap._colorbar_manual_levels is None
    assert tuple(heatmap.bar.levels()) == (0, 3)
    assert heatmap.relevel_refresh.isChecked()
    assert float(heatmap.colorbar_min_text.text()) == 1.5
    assert float(heatmap.colorbar_max_text.text()) == 3
    assert heatmap.statuses


@pytest.mark.parametrize("limits", [
    (1.2345678901234567, 1.2345678901234569),
    (1e308, 1.1e308),
    (0, float(np.nextafter(0.0, 1.0))),
])
def test_color_format_roundtrip_never_reconstructs_unchanged_endpoints(heatmap, limits):
    assert heatmap.setColorbarManualRange(*limits)
    entry = heatmap.colorbar_range_entry
    for _ in range(3):
        click(entry.center_span_button)
        QtTest.QTest.keyClick(heatmap.colorbar_min_text, QtCore.Qt.Key.Key_Return)
        heatmap.colorbar_manual_radio.click()
        assert tuple(heatmap.bar.levels()) == limits
        click(entry.min_max_button)
        assert (float(heatmap.colorbar_min_text.text()),
                float(heatmap.colorbar_max_text.text())) == limits
    assert heatmap._colorbar_manual_levels == limits
