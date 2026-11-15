"""Shared fixtures and assertions for native averaging."""

from PyQt6 import QtCore, QtTest, QtWidgets

from tests.support.windows.native_fft_coordinates import (
    assert_spectrum,
)

__all__ = [
    'assert_average',
    'click_average',
]


def click_average(host):
    control = host.plot.ctrl.averageGroup
    control.show()
    option = QtWidgets.QStyleOptionGroupBox()
    control.initStyleOption(option)
    rect = control.style().subControlRect(
        QtWidgets.QStyle.ComplexControl.CC_GroupBox, option,
        QtWidgets.QStyle.SubControl.SC_GroupBoxCheckBox, control,
    )
    QtTest.QTest.mouseClick(control, QtCore.Qt.MouseButton.LeftButton, pos=rect.center())


def assert_average(host, expected_x, expected_y, count):
    assert len(host.plot.avgCurves) == 1
    actual_count, average = next(iter(host.plot.avgCurves.values()))
    assert actual_count == count
    assert average not in host.lines.values()
    assert_spectrum(average, expected_x, expected_y)
    for key in ("fftMode", "derivativeMode", "phasemapMode", "subtractMeanMode"):
        assert not average.opts[key]
    return average
