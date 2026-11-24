"""Selection statistics through real QCoDeS samples and the selection menu."""

from fractions import Fraction

import numpy as np
import pytest
from PyQt6 import QtCore

from tests.support.tools.sample_statistics_accuracy import summary_oracle
from tests.support.windows.native_transform_labels import (
    no_callback_errors as _no_callback_errors,
)
from tests.support.windows.numerical_stability import measured_plots as _measured_plots

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
    if values.ndim == 1 and values.dtype.kind in "iu":
        # No line selection exists while absolute coordinates are rejected.
        # Centering restores its visible variation and usable statistics.
        assert host.line._qplot_precision_error
        assert host._marquee_stats_text() is None
        raw = values.copy()
        exact_mean = sum(Fraction(int(value)) for value in raw) / len(raw)
        values = np.array([float(Fraction(int(value)) - exact_mean) for value in raw])
        host.plot.ctrl.subtractMeanCheck.setChecked(True)
        assert host.line._qplot_precision_error is None
        np.testing.assert_array_equal(host.line.getOriginalDataset()[1], raw)
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
    next(action for action in menu.actions() if action.text() == 'Selection statistics…').trigger()
    assert host._marquee_stats_dialog.isVisible()
    host._marquee_stats_dialog.close()


@pytest.mark.parametrize("measured_plots", [
    (np.arange(10), np.where(np.arange(100) < 17, np.nan, np.arange(100)).reshape(10, 10), 1),
], indirect=True)
def test_heatmap_report_identifies_the_primary_grid_and_finite_cells(measured_plots):
    from PyQt6 import QtWidgets as qtw

    _, (plot,), _, _ = measured_plots
    plot.set_marquee_rect(QtCore.QRectF(-.5, -.5, 10, 10))
    assert plot.show_marquee_stats_dialog()
    dialog = plot._marquee_stats_dialog
    assert dialog.findChild(qtw.QLabel).text() == f"Signal (signal) [A] · Run {plot.ds.run_id}"
    table = dialog.findChild(qtw.QTableWidget)
    fields = [table.item(row, 0).text() for row in range(table.rowCount())]
    assert fields[:5] == ["Average", "Standard deviation", "Max", "Min", "Selection and source"]
    assert table.item(fields.index("Run GUID"), 1).text() == plot.ds.guid
    next(b for b in dialog.findChild(qtw.QDialogButtonBox).buttons() if b.text() == "Copy full report").click()
    text = qtw.QApplication.clipboard().text()
    assert f"Run: {plot.ds.run_id} ({plot.ds.guid})" in text
    assert "Measurement: Signal (signal) [A]" in text
    assert "X quantity: Coordinate (x) [V]" in text
    assert "Statistics quantity: Signal (signal) [A]" in text
    assert "Selected grid: 10 × 10" in text
    summary = dialog.findChild(qtw.QLabel, "statisticsContribution")
    assert summary.isVisible() and summary.y() < table.y()
    assert "83 of 100 selected cells contributed" in summary.text()
    assert summary.text() in text
    assert "Average: 58.0" in text
    dialog.close()
