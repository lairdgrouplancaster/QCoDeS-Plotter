"""Zero-centred colour limits through the shared dialog and colourbar."""

import numpy as np
import pytest

from tests.windows.test_color_scale_locale import heatmap as heatmap


def test_symmetric_auto_tracks_data_and_can_be_disabled(heatmap):
    assert not heatmap.symmetric_color.isChecked()
    heatmap.symmetric_color.setChecked(True)
    assert heatmap.bar.levels() == (-3., 3.)
    assert heatmap._colorbar_manual_levels is None
    heatmap.dataGrid = np.array([[-8., 2.]])
    heatmap.scaleColorbar()
    assert heatmap.bar.levels() == (-8., 8.)
    heatmap.dataGrid = np.array([[5., 5.]])
    heatmap.scaleColorbar()
    assert heatmap.bar.levels() == (-5., 5.)
    heatmap.dataGrid = np.array([[-8., 2.]])
    heatmap.symmetric_color.setChecked(False)
    assert heatmap.bar.levels() == (-8., 2.)


@pytest.mark.parametrize("field_name, text, expected", [
    ("colorbar_min_text", "-1", (-1., 1.)),
    ("colorbar_max_text", "2", (-2., 2.)),
    ("colorbar_min_text", "-6", (-6., 6.)),
])
def test_manual_endpoint_edits_mirror_even_when_shrinking(heatmap, field_name, text, expected):
    heatmap.setColorbarManualRange(-2., 5.)
    heatmap.symmetric_color.setChecked(True)
    assert heatmap._colorbar_manual_levels == (-5., 5.)
    field = getattr(heatmap, field_name)
    field.setText(text)
    field.editingFinished.emit()
    assert heatmap.bar.levels() == expected
    assert heatmap._colorbar_manual_levels == expected
    heatmap.symmetric_color.setChecked(False)
    assert heatmap._colorbar_manual_levels == expected


def test_symmetric_center_span_keeps_center_zero(heatmap):
    heatmap.symmetric_color.setChecked(True)
    heatmap.colorbar_range_entry.center_span_button.click()
    assert not heatmap.colorbar_min_text.isEnabled()
    heatmap.colorbar_max_text.setText("4")
    heatmap.colorbar_max_text.editingFinished.emit()
    assert heatmap.bar.levels() == (-2., 2.)
    assert float(heatmap.colorbar_min_text.text()) == 0
    heatmap.symmetric_color.setChecked(False)
    assert heatmap.colorbar_min_text.isEnabled()


def test_symmetric_colorbar_drag_mirrors_and_locks(heatmap):
    heatmap.symmetric_color.setChecked(True)
    heatmap._install_colorbar_level_sync_handlers(heatmap.bar)
    heatmap.bar.setLevels((-1., 3.))
    heatmap.bar.sigLevelsChanged.emit(heatmap.bar)
    assert heatmap.bar.levels() == (-1., 1.)
    heatmap.bar.sigLevelsChangeFinished.emit(heatmap.bar)
    assert heatmap._colorbar_manual_levels == (-1., 1.)
    assert heatmap.relevel_refresh.isChecked()
    assert not heatmap.relevel_refresh.isEnabled()


def test_zero_and_nonfinite_data_have_safe_symmetric_limits(heatmap):
    heatmap.dataGrid = np.array([[0., np.nan]])
    heatmap.symmetric_color.setChecked(True)
    low, high = heatmap.bar.levels()
    assert low == -high and high > 0
    heatmap.dataGrid = np.array([[np.nan, np.inf]])
    heatmap.scaleColorbar()
    assert heatmap.bar.levels() == (low, high)


def test_symmetry_rejects_overflow_without_changing_manual_range(heatmap, monkeypatch):
    messages = []
    monkeypatch.setattr(heatmap, "show_status", lambda text, *_: messages.append(text))
    levels = (1e308, 1.1e308)
    assert heatmap.setColorbarManualRange(*levels)
    heatmap.symmetric_color.setChecked(True)
    assert not heatmap.symmetric_color.isChecked()
    assert heatmap._colorbar_manual_levels == levels
    assert heatmap.bar.levels() == levels
    assert messages == ["The symmetric colour scale range is too wide to display safely."]
