"""Promotion must retain a cut's coordinate and measured-signal identities."""

import numpy as np
import pytest
from PyQt6 import QtWidgets as qtw

from tests.windows.test_mixed_plot_drops import (
    _added,
    _drop,
    _wait_for,
    mixed_plot,  # noqa: F401
)


@pytest.mark.parametrize("mixed_plot", ["heatmap"], indirect=True)
@pytest.mark.parametrize("promote, swapped", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("remaining", ["other_line", "other_heatmap"])
def test_cut_compatibility_is_preserved_by_source_promotion(mixed_plot, monkeypatch, promote, swapped, remaining):  # noqa: F811
    main, target, runs = mixed_plot
    monkeypatch.setattr(qtw.QMessageBox, "exec", lambda box: qtw.QMessageBox.StandardButton.Ok)
    target.z_index = [1, 0]
    target.openSweep("h")
    cut = main.windows[-1]
    _wait_for(lambda: hasattr(cut, "axis_data") and not cut.worker.running)
    main.post_admin()
    box = next(box for box in target.option_boxes if box.option_box.isEnabled())
    index = next(index for index in range(box.option_box.count())
                 if box.option_box.itemData(index) == cut._trace_key)
    box.option_box.setCurrentIndex(index)
    if promote:
        _drop(target, runs[remaining])
        _added(target, runs[remaining])
        target = target.remove_plot_items([("heatmap", target._primary_heatmap_key)])
        _wait_for(lambda: not target.worker.running and not target._refresh_pending)
    overlay = target.lines[cut._trace_key]
    np.testing.assert_array_equal(overlay.getOriginalDataset(), [[0, 1, 2], [10, 11, 12]])
    if swapped:
        target.set_plot_axes_swapped(True)
        _wait_for(lambda: not target.worker.running)
    def expected(values):
        data = np.array([[0, 1, 2], values])
        return data[::-1] if swapped else data
    np.testing.assert_array_equal(overlay.getOriginalDataset(), expected([10, 11, 12]))
    style = target._trace_styles[cut._trace_key]
    sides = style.x_axis, style.y_axis
    original = cut.sweep_indep
    cut.axis_dropdown["x"].setCurrentText(cut.fixed_indep)
    _wait_for(lambda: cut._axis_change_transaction is None and not cut.worker.running)
    cut.picker.slider.setValue(1)
    np.testing.assert_array_equal(cut.line.getOriginalDataset(), [[10, 20], [11, 21]])
    assert overlay.getOriginalDataset()[0] is None
    cut.refreshWindow(force=True)
    _wait_for(lambda: not cut.worker.running)
    assert overlay.getOriginalDataset()[0] is None
    cut.picker.option_box.setCurrentText(cut.sweep_indep)
    _wait_for(lambda: cut._axis_change_transaction is None and not cut.worker.running)
    assert cut.sweep_indep == original
    np.testing.assert_array_equal(overlay.getOriginalDataset(), expected([10, 11, 12]))
    cut.picker.slider.setValue(1)
    np.testing.assert_array_equal(overlay.getOriginalDataset(), expected([20, 21, 22]))
    target.refreshWindow(force=True)
    _wait_for(lambda: not target.worker.running)
    np.testing.assert_array_equal(overlay.getOriginalDataset(), expected([20, 21, 22]))
    assert (style.x_axis, style.y_axis) == sides
