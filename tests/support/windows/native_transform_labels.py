"""Shared fixtures and assertions for native transform labels."""

import sys

import numpy as np
import pytest
from PyQt6 import QtCore, QtTest

__all__ = [
    'SUPPORTED_CONTROLS',
    'assert_display',
    'click_control',
    'expected_mapping',
    'no_callback_errors',
    'set_controls',
]


def set_controls(plot, controls, enabled=True):
    for name in controls:
        getattr(plot.plot.ctrl, name).setChecked(enabled)


def assert_display(line, x, y):
    actual_x, actual_y = line.getData()
    assert len(actual_x) == len(actual_y)
    np.testing.assert_allclose(actual_x, x, equal_nan=True)
    np.testing.assert_allclose(actual_y, y, equal_nan=True, atol=1e-14)


def expected_mapping(x, y, controls):
    mapped_x, mapped_y = x, y
    if "subtractMeanCheck" in controls:
        mapped_y = y - np.mean(y)
    if "fftCheck" in controls:
        dx = np.diff(x)
        if np.any(np.abs(dx - dx[0]) > abs(dx[0]) / 1000):
            uniform_x = np.linspace(x[0], x[-1], len(x))
            mapped_y = np.interp(uniform_x, x, mapped_y)
        spacing = float(x[-1] - x[0]) / (len(x) - 1)
        mapped_x = np.fft.rfftfreq(len(x), spacing)
        mapped_y = np.abs(np.fft.rfft(mapped_y) / len(y))
    if "derivativeCheck" in controls:
        mapped_x = mapped_x[:-1]
        mapped_y = np.diff(y) / np.diff(x)
    if "phasemapCheck" in controls:
        mapped_x = y[:-1]
        mapped_y = np.diff(y) / np.diff(x)
    return mapped_x, mapped_y


SUPPORTED_CONTROLS = [
    # qPlot excludes FFT + derivative without phase map: native FFT shortens
    # X but the native derivative restores the longer input-sample Y.
    ("derivativeCheck",),
    ("fftCheck",),
    ("phasemapCheck",),
    ("phasemapCheck", "derivativeCheck"),
    ("phasemapCheck", "fftCheck"),
    ("phasemapCheck", "fftCheck", "derivativeCheck"),
]


@pytest.fixture
def no_callback_errors(monkeypatch, qapplication):
    """Qt slot failures go to excepthook instead of raising from mouseClick."""
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *error: errors.append(error))
    yield
    qapplication.processEvents()
    assert not errors, [(kind.__name__, str(error)) for kind, error, _tb in errors]


def click_control(plot, name):
    control = getattr(plot.plot.ctrl, name)
    # Exercise the actual checkbox and its synchronous Qt signal connections.
    control.show()
    QtTest.QTest.mouseClick(control, QtCore.Qt.MouseButton.LeftButton,
                           pos=QtCore.QPoint(8, control.height() // 2))
