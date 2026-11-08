"""The refresh preference survives manual scaling and dialog reopening."""

import numpy as np
import pytest

from tests.windows.test_color_scale_locale import heatmap as heatmap


def test_refresh_autoscale_defaults_on_in_colour_dialog(heatmap):
    checkbox = heatmap.relevel_refresh
    assert checkbox.text() == "Autoscale on refresh"
    assert checkbox.isChecked()
    assert checkbox.isEnabled()
    assert checkbox.isVisibleTo(heatmap.colorbar_scale_dialog)
    assert heatmap.colorbar_scale_controls.isAncestorOf(checkbox)


@pytest.mark.parametrize("refresh_on", [False, True])
@pytest.mark.parametrize("manual_action", ["fields", "drag", "copy_auto"])
def test_manual_scaling_retains_refresh_choice(heatmap, refresh_on, manual_action):
    checkbox = heatmap.relevel_refresh
    checkbox.setChecked(refresh_on)
    if manual_action == "fields":
        heatmap.colorbar_min_text.setText("-2")
        heatmap.colorbar_max_text.setText("4")
        heatmap.colorbar_manual_radio.click()
    elif manual_action == "drag":
        heatmap._install_colorbar_level_sync_handlers(heatmap.bar)
        heatmap.bar.setLevels((-2., 4.))
        heatmap.bar.sigLevelsChangeFinished.emit(heatmap.bar)
    else:
        heatmap.colorbar_copy_auto_limits_button.click()

    assert heatmap._colorbar_manual_levels is not None
    assert not checkbox.isEnabled()
    assert checkbox.isChecked() == refresh_on
    heatmap.colorbar_scale_dialog.close()
    heatmap.open_colorbar_scale_dialog()
    assert not checkbox.isEnabled()
    assert checkbox.isChecked() == refresh_on

    heatmap.dataGrid = np.array([[-10., 20.]])
    heatmap.colorbar_auto_radio.click()
    assert heatmap._colorbar_manual_levels is None
    assert heatmap.bar.levels() == (-10., 20.)
    assert checkbox.isEnabled()
    assert checkbox.isChecked() == refresh_on


def test_explicit_autoscale_restores_option_without_changing_choice(heatmap):
    heatmap.relevel_refresh.setChecked(False)
    heatmap.setColorbarManualRange(-2., 4.)
    heatmap.scaleColorbar()
    assert heatmap._colorbar_manual_levels is None
    assert heatmap.colorbar_auto_radio.isChecked()
    assert heatmap.relevel_refresh.isEnabled()
    assert not heatmap.relevel_refresh.isChecked()
