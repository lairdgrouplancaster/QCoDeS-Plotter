"""Removing a mixed plot's primary must handle retained cut traces."""

import numpy as np
import pytest
from PyQt6 import QtWidgets as qtw

from qplot.windows._plot_drop_axes import plan_drop_axes
from tests.windows import test_mixed_plot_drops as mixed_helpers

mixed_plot = mixed_helpers.mixed_plot
_wait_for = mixed_helpers._wait_for


def _add_cut(main, target):
    target.z_index = [1, 0]
    target.openSweep("h")
    cut = main.windows[-1]
    _wait_for(lambda: hasattr(cut, "axis_data") and not cut.worker.running)
    assert target.add_preview_trace(cut, plan_drop_axes(target, cut))
    return cut


@pytest.mark.parametrize("mixed_plot", ["heatmap"], indirect=True)
def test_primary_removal_with_only_surviving_cut_is_disabled(mixed_plot):
    main, target, _runs = mixed_plot
    cut = _add_cut(main, target)
    entries = list(target._plot_item_order)
    users = {key: handle.users for key, handle in main.dataset_holder.items()}
    selected = [("heatmap", target._primary_heatmap_key)]
    target.open_plot_appearance_dialog()
    dialog = target._plot_appearance_dialog
    dialog.table.selectRow(next(row for row in range(2) if dialog._row_entry(row) == selected[0]))
    assert not dialog.remove_item_button.isEnabled()
    assert "cuts cannot be its primary source" in dialog.remove_item_button.toolTip()
    dialog._remove_items()
    assert target.remove_plot_items(selected) is target
    assert target._plot_item_order == entries
    assert {key: handle.users for key, handle in main.dataset_holder.items()} == users
    assert not target._closed
    # The cut can still be removed, and its own window retains the dataset.
    target.remove_plot_items([("trace", cut._trace_key)])
    assert cut._merged_trace_users == 0
    assert target._plot_item_order == selected


@pytest.mark.parametrize("mixed_plot", ["heatmap"], indirect=True)
@pytest.mark.parametrize("remaining", ["other_line", "other_heatmap"])
def test_primary_removal_skips_cut_but_preserves_order_and_updates(mixed_plot, monkeypatch, remaining):
    main, target, runs = mixed_plot
    monkeypatch.setattr(qtw.QMessageBox, "exec", lambda box: qtw.QMessageBox.StandardButton.Ok)
    cut = _add_cut(main, target)
    mixed_helpers._drop(target, runs[remaining])
    survivor = mixed_helpers._added(target, runs[remaining]).from_win
    expected = mixed_helpers._data_identities(target)[:-1]
    original_key = target._dataset_key
    replacement = target.remove_plot_items([("heatmap", target._primary_heatmap_key)])
    assert replacement is not target
    _wait_for(lambda: not replacement.worker.running and not replacement.__dict__.get("_refresh_pending", False))
    assert replacement._trace_key == survivor._trace_key
    assert mixed_helpers._data_identities(replacement) == expected
    assert cut._merged_trace_users == 1
    assert main.dataset_holder[original_key].users == 2
    assert target not in main.windows
    cut.picker.slider.setValue(1)
    np.testing.assert_array_equal(replacement.lines[cut._trace_key].getOriginalDataset()[1], cut.axis_data["y"])
    cut.close()
    assert cut._merged_trace_users == 1
    cut.refreshWindow(force=True)
    _wait_for(lambda: not cut.worker.running)
    np.testing.assert_array_equal(replacement.lines[cut._trace_key].getOriginalDataset()[1], cut.axis_data["y"])
    replacement.remove_plot_items([("trace", cut._trace_key)])
    assert cut._merged_trace_users == 0
    assert main.dataset_holder[original_key].users == 0
