"""Selection statistics through real QCoDeS samples and the Stats action."""

import numpy as np
import pytest
from PyQt6 import QtCore

from tests.test_sample_statistics_accuracy import summary_oracle
from tests.windows.test_native_transform_labels import (
    no_callback_errors as _no_callback_errors,
)
from tests.windows.test_numerical_stability import measured_plots as _measured_plots

measured_plots = _measured_plots
no_callback_errors = _no_callback_errors

VALUES = (
    np.arange(2**60, 2**60 + 4, dtype=np.int64),
    np.array([1e200, -1e200]),
    np.array([1e-200, -1e-200]),
    np.array([1e16, 1., -1e16]),
)


@pytest.mark.parametrize('measured_plots', [
    (np.arange(len(values)), np.tile(values, (2, 1)) if heatmap else values, 1)
    for heatmap in (False, True) for values in VALUES
], indirect=True)
def test_stats_action_retains_recorded_variation(measured_plots, no_callback_errors):
    _window, (host,), x, values = measured_plots
    if values.ndim == 2:
        rect = QtCore.QRectF(-.5, -.5, len(x), 2.)
    else:
        low, high = float(values.min()), float(values.max())
        if low == high:
            low, high = low - 1024, high + 1024
        rect = QtCore.QRectF(-.5, low, len(x), high - low)
    host.set_marquee_rect(rect)
    text = host._marquee_stats_text()
    mean, deviation = summary_oracle(values)
    assert f'Average: {host.formatNum(mean)}' in text
    assert f'Standard deviation: {host.formatNum(deviation)}' in text
    menu = host._new_marquee_context_menu()
    next(action for action in menu.actions() if action.text() == 'Stats...').trigger()
    assert host._marquee_stats_dialog.isVisible()
    host._marquee_stats_dialog.close()
