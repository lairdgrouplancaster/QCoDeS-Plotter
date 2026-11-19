"""Identify and link real QCoDeS plot axes through the scaling dialog."""

from weakref import ref

import pytest
from PyQt6 import QtCore, sip
from PyQt6 import QtWidgets as qtw

from tests.support.windows.axis_limit_precision import AxisLimitWindow
from tests.support.windows.mixed_plot_drops import _added, _drop
from tests.support.windows.mixed_plot_drops import mixed_plot as mixed_plot
from tests.support.windows.plot_dialog_lifecycle import dialog_plot as dialog_plot
from tests.support.windows.plot_integration import wait_for


def test_link_targets_include_primary_and_secondary_plot_quantities(mixed_plot, qapplication):
    _main, target, runs = mixed_plot
    _drop(target, runs["other_line"])
    _added(target, runs["other_line"])
    source = AxisLimitWindow()
    source.plot.plot([1, 2, 3], [2, 4, 6])
    source.resize(800, 600)
    source.show()
    try:
        qapplication.processEvents()
        for axis, secondary, side, quantity in (
            ("x", "x2", "Top", "u (K)"),
            ("y", "y2", "Right", "w (Hz)"),
        ):
            source.open_axis_scale_dialog(axis)
            combo = source._axis_scale_controls[axis].linkCombo
            assert combo.findData(ref(target.vb)) > 0
            target_view = target._axis_scale_viewbox(secondary)
            index = combo.findData(ref(target_view))
            assert index > 0
            assert combo.itemText(index) == f"{target.windowTitle()} — {side}: {quantity}"
            combo.setCurrentIndex(index)
            number = source._axis_scale_axis_number(axis)
            assert source.vb.linkedView(number) is target_view
            target.open_axis_scale_dialog(secondary)
            assert target._axis_scale_controls[secondary].quantityLabel.text() == quantity
            assert target._axis_scale_tabs.tabToolTip(2 if axis == "x" else 3) == (
                "Top horizontal axis" if axis == "x" else "Right vertical axis"
            )
            source._sync_axis_scale_controls(axis)
            assert combo.currentData()() is target_view
            combo.setCurrentIndex(0)
            assert source.vb.linkedView(number) is None
    finally:
        source._axis_scale_dialog.close()
        source.close()
        source.deleteLater()
        qtw.QApplication.processEvents()


@pytest.mark.parametrize("dialog_plot", ["line"], indirect=True)
@pytest.mark.parametrize(("axis", "linked", "retain"), [
    pytest.param("x", True, False, id="linked-target-retires"),
    pytest.param("y", True, True, id="target-retained-as-overlay"),
    pytest.param("x", False, False, id="unselected-target-retires"),
])
def test_closed_target_leaves_axis_independent_and_updates_open_chooser(
    dialog_plot, axis, linked, retain,
):
    main, receiver, app, guids = dialog_plot
    main.openPlot(guid=guids[1])
    wait_for(lambda: len(main.windows) == 2)
    target = main.windows[-1]
    wait_for(lambda: hasattr(target, "axis_data") and not target.worker.running)
    if retain:
        assert main.add_trace_to_plot(
            receiver, target._dataset_key, target.param.name, param=target.param,
        )
        wait_for(lambda: len(receiver.lines) == 2)
    receiver.open_axis_scale_dialog(axis)
    controls = receiver._axis_scale_controls[axis]
    combo = controls.linkCombo
    index = combo.findData(ref(target.vb))
    assert index > 0
    label = combo.itemText(index)
    dimension = receiver._axis_scale_axis_number(axis)
    if linked:
        combo.setCurrentIndex(index)
        assert receiver.vb.linkedView(dimension) is target.vb
    controls.minText.setText("-20.25")  # An uncommitted edit in the open dialogue.
    target.close()
    assert target not in main.windows
    assert receiver._axis_scale_dialog.isVisible()
    assert combo.currentText() == "None"
    assert combo.findText(label) == -1
    assert receiver.vb.linkedView(dimension) is None
    assert controls.minText.text() == "-20.25"
    if retain:
        assert not sip.isdeleted(target) and target._merged_trace_users == 1
    else:
        def retired():
            app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
            return sip.isdeleted(target)

        wait_for(retired)
