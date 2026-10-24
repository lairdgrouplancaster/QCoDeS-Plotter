"""Parent heatmap controls preserve committed cuts during axis requests."""

import numpy as np
import pytest

from qplot.windows._plot_drop_axes import plan_drop_axes
from tests.windows import test_cut_color_lifecycle as cut_helpers

heatmap = cut_helpers.heatmap
ready = cut_helpers.ready


def _assert_samples(line, expected):
    for actual, values in zip(line.getOriginalDataset(), expected, strict=True):
        np.testing.assert_array_equal(actual, values)


def _assert_marker(plot, cut):
    marker = plot.sweep_lines[cut.sweep_id]
    assert marker.isVisible()
    assert marker.value() == cut.fixed_value
    assert marker.sweep_index == cut.fixed_index
    assert marker.angle == (90 if plot.axis_options["x"] == cut.fixed_indep else 0)
    assert cut.picker.slider.value() == cut.fixed_index
    assert float(cut.picker.text_box.text()) == cut.fixed_value
    return marker


@pytest.mark.parametrize("side", ["h", "v"])
@pytest.mark.parametrize("outcome", ["commit", "cancel"])
@pytest.mark.parametrize("drag_position", [-10_000., 10_000.])
def test_parent_marker_cannot_move_committed_cut_during_axis_request(
    heatmap, side, outcome, drag_position,
):
    window, plot, errors = heatmap
    plot.z_index = [1, 1]
    plot.openSweep(side)
    cut = window.windows[-1]
    ready(cut)
    original_axis = cut.sweep_indep
    assert plot.add_preview_trace(cut, plan_drop_axes(plot, cut))
    overlay = plot.lines[cut._trace_key]
    before_cut = tuple(values.copy() for values in cut.line.getOriginalDataset())
    before_overlay = tuple(values.copy() for values in overlay.getOriginalDataset())
    before_fixed = cut.fixed_value
    marker = _assert_marker(plot, cut)
    publications = []
    cut.trace_updated.connect(lambda: publications.append(True))

    cut.axis_dropdown["x"].setCurrentText(cut.fixed_indep)
    assert cut._axis_change_transaction is not None
    assert not cut.picker.slider.isEnabled()
    # This follows the same signal path as dragging beyond a heatmap edge.
    # The parent first clamps its marker, then notifies the cut.
    marker.setPos(drag_position)
    marker.sigDragged.emit(marker)
    assert not publications
    assert cut.fixed_value == before_fixed
    _assert_marker(plot, cut)
    _assert_samples(cut.line, before_cut)
    _assert_samples(overlay, before_overlay)

    if outcome == "cancel":
        cut.worker.cancel()
    ready(cut)
    _assert_marker(plot, cut)
    if outcome == "cancel":
        assert cut.sweep_indep == original_axis
        _assert_samples(cut.line, before_cut)
        _assert_samples(overlay, before_overlay)
    else:
        assert cut.sweep_indep != original_axis
        assert overlay.getOriginalDataset() == (None, None)
        cut.axis_dropdown["x"].setCurrentText(original_axis)
        ready(cut)

    # Once the request settles, normal marker movement updates both consumers.
    marker.setPos(drag_position)
    marker.sigDragged.emit(marker)
    index = 0 if drag_position < 0 else len(cut.fixed_indep_data) - 1
    assert cut.fixed_index == index
    _assert_marker(plot, cut)
    x, y = cut.line.getOriginalDataset()
    np.testing.assert_array_equal(y, x + cut.fixed_value)
    order = overlay.source_axis_order()
    _assert_samples(overlay, tuple(cut.axis_data[axis] for axis in order))
    assert not errors
