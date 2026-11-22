"""A held Centre supplies zero-centred colour limits in both modes."""

import numpy as np
import pytest

from tests.support.windows.color_scale_locale import enter_colour_value
from tests.support.windows.color_scale_locale import heatmap as heatmap


def hold_zero(plot):
    plot.colorbar_range_entry.locks["centre"].click()
    enter_colour_value(plot, "centre", 0)


def test_held_centre_tracks_data_and_can_be_released(heatmap):
    hold_zero(heatmap)
    assert heatmap.bar.levels() == (-3., 3.)
    assert heatmap._colorbar_manual_levels is None
    heatmap.dataGrid = np.array([[-8., 2.]])
    heatmap.scaleColorbar()
    assert heatmap.bar.levels() == (-8., 8.)
    heatmap.dataGrid = np.array([[5., 5.]])
    heatmap.scaleColorbar()
    assert heatmap.bar.levels() == (-5., 5.)
    heatmap.dataGrid = np.array([[-8., 2.]])
    heatmap.colorbar_range_entry.locks["centre"].click()
    assert heatmap.bar.levels() == (-8., 2.)


@pytest.mark.parametrize("field, number", [("min", -1), ("max", 2), ("min", -6)])
def test_manual_endpoint_mirrors_held_centre(heatmap, field, number):
    hold_zero(heatmap)
    enter_colour_value(heatmap, field, number)
    limits = -abs(number), abs(number)
    assert heatmap._colorbar_manual_levels == heatmap.bar.levels() == limits


def test_held_centre_colorbar_drag_mirrors_and_enters_manual(heatmap):
    hold_zero(heatmap)
    heatmap._install_colorbar_level_sync_handlers(heatmap.bar)
    heatmap.bar.setLevels((-1., 3.))
    heatmap.bar.sigLevelsChanged.emit(heatmap.bar)
    assert heatmap.bar.levels() == (-1., 1.)
    heatmap.bar.sigLevelsChangeFinished.emit(heatmap.bar)
    assert heatmap._colorbar_manual_levels == (-1., 1.)
    assert heatmap.colorbar_range_entry.state.lock == "centre"


def test_held_centre_retains_levels_on_invalid_edits(heatmap):
    hold_zero(heatmap)
    levels = heatmap.bar.levels()
    enter_colour_value(heatmap, "centre", 1.7e308)
    assert heatmap.bar.levels() == levels
    assert heatmap.colorbar_range_entry.error_label.text()
