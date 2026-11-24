"""Colour fits use finite displayed samples and respect the current range mode."""

from types import SimpleNamespace

import numpy as np
import pytest

from qplot.tools.heatmap_geometry import HeatmapGeometry
from tests.support.windows.color_scale_locale import enter_colour_value
from tests.support.windows.color_scale_locale import heatmap as heatmap


def test_percentile_ignores_nonfinite_values_and_tracks_new_data(heatmap):
    entry = heatmap.colorbar_range_entry
    heatmap.dataGrid = np.array([[0., 10., np.nan], [20., 30., np.inf]])
    entry.percent.setValue(50)
    assert heatmap.bar.levels() == (7.5, 22.5)
    heatmap.dataGrid *= 2
    heatmap.scaleColorbar()
    assert heatmap.bar.levels() == (15., 45.)
    assert entry.state.automatic


def test_manual_percentage_only_applies_on_fit_or_field_action(heatmap):
    entry = heatmap.colorbar_range_entry
    heatmap.setColorbarManualRange(-10., 10.)
    entry.percent.setValue(50)
    assert heatmap.bar.levels() == (-10., 10.)
    entry.use_buttons["min"].click()
    assert heatmap.bar.levels() == (.75, 10.)
    entry.fit_button.click()
    assert heatmap.bar.levels() == (.75, 2.25)
    assert not entry.state.automatic


def test_visible_colour_fit_uses_assigned_view_and_recovers_after_empty_view(heatmap, qapplication):
    entry = heatmap.colorbar_range_entry
    heatmap.heatmap_geometry = HeatmapGeometry.from_centres([0, 1], [0, 1])
    heatmap._watch_heatmap_view_transform(heatmap.plot.vb)
    heatmap.plot.vb.setRange(xRange=(-.2, .2), yRange=(-.2, 1.2), padding=0)
    entry.visible_only.setChecked(True)
    assert heatmap.bar.levels() == (0., 2.)
    heatmap.plot.vb.setXRange(4, 5, padding=0)
    qapplication.processEvents()
    assert heatmap.bar.levels() == (0., 2.)
    assert entry.error_label.text()
    heatmap.plot.vb.setXRange(.8, 1.2, padding=0)
    qapplication.processEvents()
    assert heatmap.bar.levels() == (1., 3.)
    assert entry.state.automatic
    assert not entry.error_label.text()


def test_visible_colour_fit_combines_only_samples_in_each_layers_view(heatmap, monkeypatch):
    geometry = HeatmapGeometry.from_centres([0, 1], [0, 1])
    heatmap.heatmap_geometry = geometry
    overlay = SimpleNamespace(visible=True, geometry=geometry, data_grid=np.array([[90., 100.], [110., 120.]]))
    heatmap.heatmaps = {"host": heatmap, "overlay": overlay}
    first = SimpleNamespace(viewRange=lambda: ((-.1, .1), (-.1, 1.1)))
    second = SimpleNamespace(viewRange=lambda: ((.9, 1.1), (.9, 1.1)))
    monkeypatch.setattr(heatmap, "_heatmap_axis_viewbox", lambda layer: first if layer is heatmap else second)
    entry = heatmap.colorbar_range_entry
    entry.visible_only.setChecked(True)
    entry.percent.setValue(50)
    # The three eligible values are 0, 2 and 120.
    assert heatmap.bar.levels() == (1., 61.)
    overlay.visible = False
    heatmap.scaleColorbar()
    assert heatmap.bar.levels() == (.5, 1.5)


def test_hiding_a_layer_refits_automatic_and_changes_manual_fit_source(heatmap):
    overlay = SimpleNamespace(visible=True, geometry=HeatmapGeometry.from_centres([0, 1], [0, 1]),
                              data_grid=np.array([[10., 20.], [30., 40.]]))
    overlay.set_visible = lambda visible: setattr(overlay, "visible", visible)
    heatmap.heatmaps = {"host": heatmap, "overlay": overlay}
    entry = heatmap.colorbar_range_entry
    assert not entry.visible_only.isChecked()
    heatmap.scaleColorbar()
    assert heatmap.bar.levels() == (0., 40.)
    heatmap._set_layer_visibility(overlay, False)
    assert heatmap.bar.levels() == (0., 3.)
    entry.manual_radio.click()
    heatmap._set_layer_visibility(overlay, True)
    assert heatmap.bar.levels() == (0., 3.)
    entry.fit_button.click()
    assert heatmap.bar.levels() == (0., 40.)


def test_per_field_and_whole_data_fit_differ_for_held_centre(heatmap):
    entry = heatmap.colorbar_range_entry
    entry.locks["centre"].click()
    enter_colour_value(heatmap, "centre", 0)
    assert heatmap.bar.levels() == (-3., 3.)
    entry.use_buttons["span"].click()
    assert heatmap.bar.levels() == (-1.5, 1.5)
    assert not entry.state.automatic
    entry.fit_button.click()
    assert heatmap.bar.levels() == (-3., 3.)
    assert entry.state.lock == "centre"


def test_colour_options_and_lock_restore_across_session(heatmap):
    entry = heatmap.colorbar_range_entry
    entry.percent.setValue(50)
    entry.locks["span"].click()
    before = heatmap._capture_colour_range_settings()
    entry.locks["max"].click()
    entry.percent.setValue(100)
    entry.manual_radio.click()
    heatmap._restore_colour_range_settings(before)
    assert entry.state == before[0]
    assert entry.percent.value() == 50
    assert heatmap.bar.levels() == pytest.approx(before[0].limits)
