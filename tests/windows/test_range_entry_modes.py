"""Range editing through the plot's native axis and colour-scale controls."""

import sys
from weakref import ref

import numpy as np
import pyqtgraph as pg
import pytest
from PyQt6 import QtCore, QtTest, sip
from PyQt6 import QtWidgets as qtw

from tests.support.windows.axis_limit_precision import AxisLimitWindow
from tests.support.windows.color_scale_locale import heatmap as _heatmap_fixture

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


def enter(field, value, key=QtCore.Qt.Key.Key_Return):
    field.setFocus()
    field.selectAll()
    QtTest.QTest.keyClicks(field, str(value))
    QtTest.QTest.keyClick(field, key)


def test_colour_and_spatial_editors_offer_the_same_four_fields(heatmap, axis_window):
    colour = heatmap.colorbar_range_entry
    spatial = axis_window._axis_scale_controls["x"].rangeEntry
    for editor in (colour, spatial):
        assert editor.auto_radio.isChecked()
        assert not editor.fit_button.isEnabled()
        for key in ("min", "max", "centre", "span"):
            assert editor.fields[key].isVisible()
            assert editor.fields[key].isEnabled()
            assert editor.locks[key].isVisible()
            assert not editor.locks[key].isChecked()
        assert not editor.error_label.isVisible()
    assert colour.percent.isVisible()
    assert not spatial.percent.isVisible()
    assert heatmap.colorbar_choose_map.isVisible()


def test_compact_colour_limits_reveal_and_accept_exact_values(heatmap, qapplication):
    limits = (-1.64340716715477, 1.64343277133006)
    assert heatmap.setColorbarManualRange(*limits)
    first, second = heatmap.colorbar_min_text, heatmap.colorbar_max_text
    heatmap.colorbar_choose_map.setFocus()
    qapplication.processEvents()
    assert (first.presentation_text(), second.presentation_text()) == ("-1.64341", "1.64343")
    assert (float(first.text()), float(second.text())) == limits
    first.setFocus()
    qapplication.processEvents()
    assert first.presentation_text() == repr(limits[0])
    QtTest.QTest.keyClick(first, QtCore.Qt.Key.Key_Return)
    assert tuple(heatmap.bar.levels()) == limits
    enter(first, "-2")
    assert tuple(heatmap.bar.levels()) == (-2.0, limits[1])


@pytest.mark.parametrize("limits", [
    (1.2345678901234567, 1.2345678901234569),
    (1e-120, 2e-120),
    (1e250, 2e250),
])
def test_compact_colour_limits_stay_distinct_without_rounding_data(heatmap, qapplication, limits):
    assert heatmap.setColorbarManualRange(*limits)
    heatmap.colorbar_choose_map.setFocus()
    qapplication.processEvents()
    first, second = heatmap.colorbar_min_text, heatmap.colorbar_max_text
    assert first.presentation_text() != second.presentation_text()
    heatmap.colorbar_manual_radio.click()
    assert heatmap._colorbar_manual_levels == limits
    assert tuple(heatmap.bar.levels()) == limits


def check_keyboard_range_edit(dialog, field, entry, current_limits, key):
    dialog.activateWindow()
    field.setFocus()
    qtw.QApplication.processEvents()
    original = tuple(current_limits())
    enter(field, "1000", key)
    assert tuple(current_limits()) == pytest.approx(original)
    assert dialog.isVisible() and field.hasFocus()
    assert field.text() == "1000"
    assert entry.error_label.isVisible()
    enter(field, "-7", key)
    assert tuple(current_limits()) == pytest.approx((-7, original[1]))
    assert dialog.isVisible() and field.hasFocus()
    assert not entry.error_label.isVisible()
    QtTest.QTest.keyClick(field, QtCore.Qt.Key.Key_Tab)
    assert dialog.isVisible() and not field.hasFocus()
    close = dialog.findChild(qtw.QDialogButtonBox).button(qtw.QDialogButtonBox.StandardButton.Close)
    close.setFocus()
    QtTest.QTest.keyClick(close, QtCore.Qt.Key.Key_Return)
    assert not dialog.isVisible()


@pytest.mark.parametrize("key", [QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter])
def test_axis_range_enter_preserves_the_editor(axis_window, key):
    ui = axis_window._axis_scale_controls["x"]
    click(ui.manualRadio)
    check_keyboard_range_edit(
        axis_window._axis_scale_dialog, ui.minText, ui.rangeEntry,
        lambda: axis_window.vb.viewRange()[0], key,
    )


@pytest.mark.parametrize("heatmap_source", ["C"], indirect=True)
@pytest.mark.parametrize("key", [QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter])
def test_colour_range_enter_preserves_the_editor(heatmap, key):
    check_keyboard_range_edit(
        heatmap.colorbar_scale_dialog, heatmap.colorbar_min_text,
        heatmap.colorbar_range_entry, heatmap.bar.levels, key,
    )


@pytest.mark.parametrize("field", ["min", "centre"])
def test_typing_switches_mode_and_escape_restores_tracking(axis_window, field):
    ui = axis_window._axis_scale_controls["x"].rangeEntry
    target = ui.fields[field]
    original = tuple(axis_window.vb.viewRange()[0])
    assert ui.auto_radio.isChecked()
    target.setFocus()
    target.selectAll()
    QtTest.QTest.keyClicks(target, "-")
    assert target.text() == "-"
    assert ui.manual_radio.isChecked()
    assert tuple(axis_window.vb.viewRange()[0]) == original
    QtTest.QTest.keyClick(target, QtCore.Qt.Key.Key_Escape)
    assert ui.auto_radio.isChecked()
    enter(target, "-1.5")
    assert ui.manual_radio.isChecked()
    assert float(target.text()) == -1.5


@pytest.mark.parametrize("axis, number", [("x", 0), ("y", 1)])
def test_axis_link_none_unlinks_without_confusing_a_view_name(axis_window, axis, number):
    target = pg.ViewBox(name="None")
    try:
        axis_window.open_axis_scale_dialog(axis)
        combo = axis_window._axis_scale_controls[axis].linkCombo
        assert combo.currentText() == "None"
        assert combo.currentData() == ""
        combo.setCurrentIndex(combo.findData("None"))
        assert axis_window.vb.linkedView(number) is target
        axis_window._sync_axis_scale_controls(axis)
        assert combo.currentData() == "None"
        combo.setCurrentIndex(combo.findData(""))
        assert axis_window.vb.linkedView(number) is None
    finally:
        target.unregister()
        target.deleteLater()


def test_axis_tabs_track_quantity_without_changing_range_units(axis_window):
    axis_window.plot.setLabel("bottom", "Gate <b>voltage</b>", units="V")
    axis_window.vb.setXRange(0.001, 0.002, padding=0)
    axis_window.open_axis_scale_dialog("bottom")
    ui = axis_window._axis_scale_controls["x"]
    tabs = axis_window._axis_scale_tabs
    assert tabs.tabText(tabs.currentIndex()) == "Bottom"
    assert axis_window._axis_scale_quantity.isVisible()
    assert axis_window._axis_scale_quantity.text() == "Gate voltage (V)"
    assert float(ui.minText.text()) == 0.001
    assert float(ui.maxText.text()) == 0.002
    axis_window.plot.setLabel("bottom", "Frequency", units="Hz")
    axis_window._sync_axis_scale_controls("x")
    assert axis_window._axis_scale_quantity.text() == "Frequency (Hz)"
    assert "Frequency (Hz)" in tabs.tabToolTip(tabs.currentIndex())


def test_axis_tabs_support_one_click_and_keyboard_selection(axis_window):
    tabs = axis_window._axis_scale_tabs
    stack = axis_window._axis_scale_stack
    assert isinstance(tabs, qtw.QTabBar)
    assert tabs.accessibleName()
    assert tabs.count() == 4
    QtTest.QTest.mouseClick(tabs, QtCore.Qt.MouseButton.LeftButton, pos=tabs.tabRect(1).center())
    assert tabs.tabText(tabs.currentIndex()) == "Left"
    assert stack.currentWidget() is axis_window._axis_scale_controls["y"]
    assert not tabs.isTabEnabled(2)
    QtTest.QTest.mouseClick(tabs, QtCore.Qt.MouseButton.LeftButton, pos=tabs.tabRect(2).center())
    assert tabs.tabText(tabs.currentIndex()) == "Left"
    tabs.setFocus()
    QtTest.QTest.keyClick(tabs, QtCore.Qt.Key.Key_Left)
    assert stack.currentWidget() is axis_window._axis_scale_controls["x"]
    QtTest.QTest.keyClick(tabs, QtCore.Qt.Key.Key_Right)
    assert stack.currentWidget() is axis_window._axis_scale_controls["y"]
    QtTest.QTest.keyClick(tabs, QtCore.Qt.Key.Key_Right)
    assert tabs.isTabEnabled(tabs.currentIndex())


def test_automatic_fields_track_exact_updated_data_limits(axis_window, qapplication):
    ui = axis_window._axis_scale_controls["x"].rangeEntry
    axis_window.plot.listDataItems()[0].setData([0.01234567890123, 0.98765432109876], [2, 6])
    axis_window.vb.updateAutoRange()
    qapplication.processEvents()
    assert ui.auto_radio.isChecked()
    assert (float(ui.fields["min"].text()), float(ui.fields["max"].text())) == tuple(axis_window.vb.viewRange()[0])


@pytest.mark.parametrize("axis, side, number", [("x", "bottom", 0), ("y", "left", 1)])
def test_axis_link_choices_identify_plot_and_quantity(axis_window, qapplication, axis, side, number):
    target = AxisLimitWindow()
    target.setWindowTitle("Plot 42")
    target.plot.plot([1, 2, 3], [2, 4, 6])
    target.plot.setLabel(side, "Gate voltage", units="V")
    target.resize(800, 600)
    target.show()
    try:
        qapplication.processEvents()
        axis_window.open_axis_scale_dialog(axis)
        ui = axis_window._axis_scale_controls[axis]
        index = ui.linkCombo.findData(ref(target.vb))
        assert index > 0
        assert ui.linkCombo.itemText(index) == f"Plot 42 — {side.title()}: Gate voltage (V)"
        ui.linkCombo.setCurrentIndex(index)
        assert axis_window.vb.linkedView(number) is target.vb
        setter = target.vb.setXRange if number == 0 else target.vb.setYRange
        setter(10, 20, padding=0)
        qapplication.processEvents()
        assert axis_window.vb.viewRange()[number] == pytest.approx([10, 20])
        axis_window._sync_axis_scale_controls(axis)
        assert ui.linkCombo.currentData()() is target.vb
        ui.linkCombo.setCurrentIndex(0)
        assert axis_window.vb.linkedView(number) is None
    finally:
        target.close()
        target.deleteLater()
        qapplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


def test_axis_link_ignores_target_destroyed_while_dialog_is_open(axis_window, qapplication):
    target = AxisLimitWindow()
    target.show()
    qapplication.processEvents()
    axis_window.open_axis_scale_dialog("x")
    combo = axis_window._axis_scale_controls["x"].linkCombo
    index = combo.findData(ref(target.vb))
    assert index > 0
    sip.delete(target)
    combo.setCurrentIndex(index)
    assert axis_window.vb.linkedView(0) is None
    assert combo.currentIndex() == 0


@pytest.mark.parametrize("texts", [
    ("9.9", "10.2"),
    ("0.010012718532581035", "0.010017029084329264"),
    ("1.2345678901234567", "2.234567890123457"),
])
@pytest.mark.parametrize("target", ["coordinate", "colour"])
def test_edits_apply_exact_endpoints(axis_window, heatmap, texts, target):
    if target == "coordinate":
        entry = axis_window._axis_scale_controls["x"].rangeEntry
        limits = lambda: axis_window.vb.viewRange()[0]
    else:
        entry = heatmap.colorbar_range_entry
        limits = heatmap.bar.levels
    low, high = map(float, texts)
    ordered = [("min", low), ("max", high)]
    if low >= limits()[1]:
        ordered.reverse()
    for field, value in ordered:
        enter(entry.fields[field], value)
    assert tuple(limits()) == (low, high)
    assert (entry.fields["min"].text(), entry.fields["max"].text()) == texts


@pytest.mark.parametrize("axis", ["x", "y"])
def test_axis_centre_span_edits_pan_and_fit_once(axis_window, axis):
    axis_window.open_axis_scale_dialog(axis)
    entry = axis_window._axis_scale_controls[axis].rangeEntry
    number = axis_window._axis_scale_axis_number(axis)
    enter(entry.fields["centre"], 3)
    enter(entry.fields["span"], 8)
    assert axis_window.vb.viewRange()[number] == [-1, 7]
    setter = axis_window.vb.setXRange if axis == "x" else axis_window.vb.setYRange
    setter(2, 6, padding=0)
    assert (float(entry.fields["centre"].text()), float(entry.fields["span"].text())) == (4, 4)
    expected = axis_window._axis_scale_auto_limits(axis)
    click(entry.fit_button)
    np.testing.assert_array_equal(axis_window.vb.viewRange()[number], expected)
    assert entry.manual_radio.isChecked()


def test_field_action_and_whole_fit_obey_a_held_centre(heatmap):
    entry = heatmap.colorbar_range_entry
    click(entry.locks["centre"])
    enter(entry.fields["centre"], 0)
    assert entry.auto_radio.isChecked()
    np.testing.assert_allclose(heatmap.bar.levels(), [-3, 3])
    entry.use_buttons["span"].click()
    assert entry.manual_radio.isChecked()
    np.testing.assert_allclose(heatmap.bar.levels(), [-1.5, 1.5])
    click(entry.fit_button)
    np.testing.assert_allclose(heatmap.bar.levels(), [-3, 3])
    assert entry.manual_radio.isChecked()


@pytest.mark.parametrize("value", ["0", "-2", "nan", "inf", "1e-30"])
def test_invalid_span_leaves_plot_range_intact(axis_window, value):
    entry = axis_window._axis_scale_controls["x"].rangeEntry
    before = list(axis_window.vb.viewRange()[0])
    enter(entry.fields["span"], value)
    np.testing.assert_array_equal(axis_window.vb.viewRange()[0], before)
    assert entry.error_label.isVisible()


def test_log_centre_span_uses_physical_values(axis_window, qapplication):
    ui = axis_window._axis_scale_controls["x"]
    ui.logCheck.click()
    axis_window.vb.updateAutoRange()
    qapplication.processEvents()
    entry = ui.rangeEntry
    enter(entry.fields["centre"], 55)
    enter(entry.fields["span"], 90)
    np.testing.assert_allclose(axis_window.vb.viewRange()[0], [1, 2])
    enter(entry.fields["centre"], 1)
    np.testing.assert_allclose(axis_window.vb.viewRange()[0], [1, 2])
    assert entry.error_label.isVisible()


@pytest.mark.parametrize("limits", [
    (1.2345678901234567, 1.2345678901234569),
    (1e308, 1.1e308),
    (0, float(np.nextafter(0.0, 1.0))),
])
def test_focusing_derived_fields_preserves_exact_endpoints(heatmap, limits):
    assert heatmap.setColorbarManualRange(*limits)
    entry = heatmap.colorbar_range_entry
    for field in ("centre", "span", "min", "max"):
        entry.fields[field].setFocus()
        QtTest.QTest.keyClick(entry.fields[field], QtCore.Qt.Key.Key_Return)
        assert tuple(heatmap.bar.levels()) == limits
    assert heatmap._colorbar_manual_levels == limits
