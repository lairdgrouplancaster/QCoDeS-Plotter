"""Committed cut axes publish fresh samples after invalidating old overlays."""

import numpy as np
import pytest

from tests.windows.test_mixed_plot_drops import (
    _wait_for,
    mixed_plot,  # noqa: F401
)


@pytest.mark.parametrize("mixed_plot", ["heatmap"], indirect=True)
def test_cut_overlay_restores_samples_after_swapping_cut_axes_twice(mixed_plot):  # noqa: F811
    main, target, _runs = mixed_plot
    target.z_index = [1, 0]
    target.openSweep("h")
    cut = main.windows[-1]
    _wait_for(lambda: hasattr(cut, "axis_data") and not cut.worker.running)
    main.post_admin()
    box = next(box for box in target.option_boxes if box.option_box.isEnabled())
    index = next(index for index in range(box.option_box.count())
                 if box.option_box.itemData(index) == cut._trace_key)
    box.option_box.setCurrentIndex(index)
    overlay = target.lines[cut._trace_key]
    np.testing.assert_array_equal(overlay.getOriginalDataset(), cut.line.getOriginalDataset())
    original = cut.sweep_indep
    cut.axis_dropdown["x"].setCurrentText(cut.fixed_indep)
    _wait_for(lambda: cut._axis_change_transaction is None and not cut.worker.running)
    np.testing.assert_array_equal(cut.line.getOriginalDataset(), [[10, 20], [10, 20]])
    assert overlay.getOriginalDataset() == (None, None)
    cut.axis_dropdown["x"].setCurrentText(original)
    _wait_for(lambda: cut._axis_change_transaction is None and not cut.worker.running)
    np.testing.assert_array_equal(overlay.getOriginalDataset(), cut.line.getOriginalDataset())
    np.testing.assert_array_equal(cut.line.getOriginalDataset(), [[0, 1, 2], [10, 11, 12]])
    cut.picker.slider.setValue(1)
    np.testing.assert_array_equal(cut.line.getOriginalDataset(), [[0, 1, 2], [20, 21, 22]])
    np.testing.assert_array_equal(overlay.getOriginalDataset(), cut.line.getOriginalDataset())
    for _ in range(2):
        cut.refreshWindow(force=True)
        _wait_for(lambda: not cut.worker.running)
        np.testing.assert_array_equal(overlay.getOriginalDataset(), [[0, 1, 2], [20, 21, 22]])
    assert target.sweep_lines[cut.sweep_id].value() == 20
    np.testing.assert_array_equal(target.dataGrid, [[10, 11, 12], [20, 21, 22]])
