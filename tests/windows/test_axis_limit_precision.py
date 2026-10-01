"""Exercise axis-limit precision through the production plot controls."""

import numpy as np
import pyqtgraph as pg
import pytest
from PyQt6 import QtCore, QtTest
from PyQt6 import QtWidgets as qtw

from qplot.windows._plot_axis_scaling import PlotAxisScalingMixin


class AxisLimitWindow(PlotAxisScalingMixin, qtw.QMainWindow):
    def __init__(self):
        super().__init__()
        self.widget = pg.GraphicsLayoutWidget()
        self.setCentralWidget(self.widget)
        self.plot = self.widget.addPlot()
        self.vb = self.plot.vb
        self.vbMenu = self.vb.menu
        self.statuses = []
        self._init_axis_scale_dialogs()

    def _context_menu_action(self, _text):
        return None

    def _view_range_changed_programmatically(self):
        pass

    def show_status(self, message, _timeout):
        self.statuses.append(message)


@pytest.mark.parametrize("axis", ["x", "y"])
@pytest.mark.parametrize("log_mode", [False, True])
def test_copy_auto_preserves_precise_limits_in_real_controls(qapplication, axis, log_mode):
    window = AxisLimitWindow()
    values = 5e9 + np.arange(64) * 100
    window.plot.plot(x=values, y=values)
    side = "bottom" if axis == "x" else "left"
    window.plot.setLabel(side, "Frequency", units="Hz")
    window.resize(800, 600)
    window.show()
    try:
        qapplication.processEvents()
        window.open_axis_scale_dialog(axis)
        controls = window._axis_scale_controls[axis]
        if log_mode:
            QtTest.QTest.mouseClick(
                controls.logCheck, QtCore.Qt.MouseButton.LeftButton,
                pos=QtCore.QPoint(8, controls.logCheck.height() // 2),
            )
            assert controls.logCheck.isChecked()
            assert window._axis_scale_log_mode(axis)
        qapplication.processEvents()
        expected = window._axis_scale_auto_limits(axis)
        expected_data = window.view_to_data(axis, expected)
        axis_number = window._axis_scale_axis_number(axis)

        QtTest.QTest.mouseClick(
            controls.copyAutoLimitsButton, QtCore.Qt.MouseButton.LeftButton
        )
        qapplication.processEvents()
        assert controls.minText.text() != controls.maxText.text()
        assert controls.manualRadio.isChecked()
        assert not controls.autoRadio.isChecked()
        assert window.vb.autoRangeEnabled()[axis_number] is False
        np.testing.assert_array_equal(window.vb.viewRange()[axis_number], expected)
        np.testing.assert_array_equal(
            [float(controls.minText.text()), float(controls.maxText.text())], expected_data
        )
        assert window.plot.getAxis(side).labelUnits == "Hz"

        window._axis_scale_dialog.close()
        window.open_axis_scale_dialog(axis)
        qapplication.processEvents()
        # Accept both unchanged fields using the actual editingFinished path.
        for field in (controls.minText, controls.maxText):
            field.setFocus()
            QtTest.QTest.keyClick(field, QtCore.Qt.Key.Key_Return)
        np.testing.assert_array_equal(window.vb.viewRange()[axis_number], expected)

        # Rejected edits must restore the same full-precision physical limits.
        for invalid in (["0", "nan"] if log_mode else ["nan", "5000007000"]):
            controls.minText.setFocus()
            controls.minText.setText(invalid)
            QtTest.QTest.keyClick(controls.minText, QtCore.Qt.Key.Key_Return)
            np.testing.assert_array_equal(
                [float(controls.minText.text()), float(controls.maxText.text())],
                expected_data,
            )
            np.testing.assert_array_equal(window.vb.viewRange()[axis_number], expected)
        assert window.statuses
    finally:
        if window._axis_scale_dialog is not None:
            window._axis_scale_dialog.close()
        window.close()
        window.deleteLater()
