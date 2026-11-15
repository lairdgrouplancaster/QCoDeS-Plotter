"""Identify and link real QCoDeS plot axes through the scaling dialog."""

from weakref import ref

from PyQt6 import QtWidgets as qtw

from tests.support.windows.axis_limit_precision import AxisLimitWindow
from tests.support.windows.mixed_plot_drops import _added, _drop
from tests.support.windows.mixed_plot_drops import mixed_plot as mixed_plot


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
