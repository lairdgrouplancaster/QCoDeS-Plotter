"""Cuts remain linked to the exact heatmap from which they were opened."""

import numpy as np
import pytest
from PyQt6 import QtWidgets as qtw

from tests.windows.test_mixed_plot_drops import (
    _added,
    _drop,
    _wait_for,
    mixed_plot,  # noqa: F401
)


def _open_cut(window, source, side):
    source.z_index = [1, 1]
    source.openSweep(side)
    cut = window.windows[-1]
    _wait_for(lambda: hasattr(cut, "axis_data") and not cut.worker.running)
    qtw.QApplication.processEvents()
    return cut


@pytest.mark.parametrize("side", ["h", "v"])
def test_cut_from_promoted_heatmap_uses_its_exact_source(mixed_plot, monkeypatch, side):  # noqa: F811
    window, target, runs = mixed_plot
    monkeypatch.setattr(qtw.QMessageBox, "exec", lambda box: qtw.QMessageBox.StandardButton.Ok)
    original_entry = target._plot_item_order[0]
    previous_count = len(window.windows)
    window.openPlot(guid=runs["other_heatmap"].guid, show=True)
    _wait_for(lambda: len(window.windows) > previous_count)
    source = window.windows[-1]
    _wait_for(lambda: hasattr(source, "dataGrid") and not source.worker.running)
    original_cut = _open_cut(window, source, side)
    assert original_cut.sweep_id in source.sweep_lines

    # Both surviving heatmap windows legitimately refer to the same QCoDeS
    # dataset after removing the host's primary measurement.
    _drop(target, runs["other_heatmap"])
    assert _added(target, runs["other_heatmap"]).from_win is source
    promoted = target.remove_plot_items([original_entry])
    assert promoted is not target and promoted is not source
    _wait_for(lambda: hasattr(promoted, "dataGrid") and not promoted.worker.running
              and not promoted._refresh_pending)
    promoted_cut = _open_cut(window, promoted, side)
    assert promoted_cut.sweep_id in promoted.sweep_lines
    assert promoted_cut.sweep_id not in source.sweep_lines
    assert original_cut.sweep_id not in promoted.sweep_lines

    original_cut.picker.slider.setValue(0)
    promoted_cut.picker.slider.setValue(promoted_cut.picker.slider.maximum())
    for heatmap, cut in ((source, original_cut), (promoted, promoted_cut)):
        marker = heatmap.sweep_lines[cut.sweep_id]
        assert marker.value() == cut.fixed_value
        x, y = cut.line.getOriginalDataset()
        np.testing.assert_array_equal(y, x + cut.fixed_value)
        marker.setPos(float(cut.fixed_indep_data[1]))
        marker.sigDragged.emit(marker)
        assert cut.fixed_index == 1

    # Closing one source's cut must remove only its marker. The other source
    # and its bidirectional slider/marker connection remain usable.
    promoted.close_sweeps_requested.emit(promoted, [promoted_cut.sweep_id])
    assert promoted_cut not in window.windows
    assert not promoted.sweep_lines
    assert original_cut in window.windows
    assert original_cut.sweep_id in source.sweep_lines
    original_cut.picker.slider.setValue(0)
    assert source.sweep_lines[original_cut.sweep_id].value() == original_cut.fixed_value
