"""Appearance swaps retain the semantic position of existing real heatmap cuts."""

from contextlib import closing

import numpy as np
import pytest
from PyQt6 import QtWidgets as qtw
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.dataset.sqlite.database import connect
from qcodes.parameters import ManualParameter

from qplot.windows.main import MainWindow
from tests._window_lifecycle import close_main_window
from tests.windows.test_plot_integration import (
    configure_temp_qplot,
    database_artifact_state,
    wait_for,
)


@pytest.fixture(params=[
    ((10., 20., 30.), (1., 2., 3., 4.)),
    ((1., 1.2, 1.4), (0., 1., 2., 3.)),
], ids=["disjoint", "overlapping"])
def heatmap_with_cuts(tmp_path, monkeypatch, request):
    configure_temp_qplot(monkeypatch, tmp_path)
    database = tmp_path / "appearance-cuts.db"
    initialise_or_create_database_at(str(database), journal_mode="DELETE")
    with closing(connect(database)) as connection:
        experiment = load_or_create_experiment(
            "appearance_swap", sample_name="cuts", conn=connection,
        )
        x, y, z = (ManualParameter(name) for name in ("x", "y", "z"))
        measurement = Measurement(exp=experiment)
        measurement.register_parameter(x)
        measurement.register_parameter(y)
        measurement.register_parameter(z, setpoints=(x, y))
        with measurement.run(write_in_background=False) as saver:
            for x_value in request.param[0]:
                for y_value in request.param[1]:
                    saver.add_result((x, x_value), (y, y_value), (z, x_value + y_value))
        guid = saver.dataset.guid
    protected = database_artifact_state(database)
    window = MainWindow()
    try:
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.close_database(status=False)
        assert window.load_file(str(database))
        wait_for(lambda: not window._database_load_active)
        window.openPlot(guid=guid, show=True)
        heatmap = window.windows[-1]
        wait_for(lambda: hasattr(heatmap, "dataGrid") and not heatmap.worker.running)
        heatmap.monitor.stop()
        cuts = []
        for orientation, indices in (("h", [1, 1]), ("h", [1, 2]),
                                     ("v", [1, 1]), ("v", [2, 1])):
            heatmap.z_index = indices
            heatmap.openSweep(orientation)
            cut = window.windows[-1]
            wait_for(lambda cut=cut: hasattr(cut, "axis_data") and not cut.worker.running)
            cut.monitor.stop()
            cuts.append(cut)
        yield heatmap, cuts
    finally:
        close_main_window(window)
        assert database_artifact_state(database) == protected


def _assert_cut_marker(heatmap, cut):
    line = heatmap.sweep_lines[cut.sweep_id]
    expected_angle = 90 if heatmap.axis_options["x"] == cut.fixed_indep else 0
    assert line.angle == expected_angle
    assert line.value() == cut.fixed_value
    assert line.isVisible()
    assert line.getViewBox() is heatmap._primary_heatmap_viewbox()
    assert line.sweep_index == cut.fixed_index
    assert cut.picker.slider.value() == cut.fixed_index
    expected_x, expected_y = cut.line.getOriginalDataset()
    np.testing.assert_array_equal(expected_y, expected_x + cut.fixed_value)


def _wait_for_axes(heatmap, axes):
    wait_for(lambda: (
        not heatmap.worker.running
        and not heatmap.__dict__.get("_refresh_pending", False)
        and not heatmap.__dict__.get("_refresh_pending_scheduled", False)
        and heatmap.axis_options == axes
        and all(heatmap.axis_param[axis].name == name for axis, name in axes.items())
    ))


@pytest.mark.parametrize("axes", [
    ("Bottom", "Left"), ("Top", "Left"), ("Bottom", "Right"), ("Top", "Right"),
])
def test_appearance_swap_keeps_multiple_cuts_linked(heatmap_with_cuts, axes):
    heatmap, cuts = heatmap_with_cuts
    heatmap.open_heatmap_appearance_dialog()
    dialog = heatmap._heatmap_appearance_dialog
    dialog.x_axis.setCurrentText(axes[0])
    dialog.y_axis.setCurrentText(axes[1])
    swap = next(
        checkbox for checkbox in dialog.findChildren(qtw.QCheckBox)
        if checkbox.text() == "Swap X/Y"
    )
    original_axes = dict(heatmap.axis_options)
    original_cuts = {
        cut.sweep_id: (cut.fixed_indep, cut.fixed_value,
                       tuple(np.array(values, copy=True) for values in cut.line.getOriginalDataset()))
        for cut in cuts
    }
    for swapped in (True, False):
        swap.click()
        expected_axes = (
            {"x": original_axes["y"], "y": original_axes["x"]}
            if swapped else original_axes
        )
        _wait_for_axes(heatmap, expected_axes)
        for cut in cuts:
            fixed_parameter, fixed_value, original_data = original_cuts[cut.sweep_id]
            assert cut.fixed_indep == fixed_parameter
            assert cut.fixed_value == fixed_value
            for actual, expected in zip(cut.line.getOriginalDataset(), original_data, strict=True):
                np.testing.assert_array_equal(actual, expected)
            _assert_cut_marker(heatmap, cut)

    # Two clicks can coalesce while the first refresh is still in flight.
    # The committed axes and each marker must return to their original state.
    swap.click()
    swap.click()
    _wait_for_axes(heatmap, original_axes)
    for cut in cuts:
        fixed_parameter, fixed_value, original_data = original_cuts[cut.sweep_id]
        assert cut.fixed_indep == fixed_parameter
        assert cut.fixed_value == fixed_value
        for actual, expected in zip(cut.line.getOriginalDataset(), original_data, strict=True):
            np.testing.assert_array_equal(actual, expected)
        _assert_cut_marker(heatmap, cut)

    # The ordinary dropdown path continues to share the same marker semantics.
    heatmap.axis_dropdown["x"].setCurrentText(original_axes["y"])
    _wait_for_axes(heatmap, {"x": original_axes["y"], "y": original_axes["x"]})
    for cut in cuts:
        _assert_cut_marker(heatmap, cut)
    heatmap.axis_dropdown["x"].setCurrentText(original_axes["x"])
    _wait_for_axes(heatmap, original_axes)
    for cut in cuts:
        _assert_cut_marker(heatmap, cut)

    # Both directions still propagate actual Qt slider and marker drag signals
    # after swapping twice, and retain their physical fixed-parameter values.
    for cut in cuts:
        cut.picker.slider.setValue(0)
        qtw.QApplication.processEvents()
        _assert_cut_marker(heatmap, cut)
        line = heatmap.sweep_lines[cut.sweep_id]
        line.setPos(float(cut.fixed_indep_data[-1]))
        line.sigDragged.emit(line)
        qtw.QApplication.processEvents()
        assert cut.fixed_value == cut.fixed_indep_data[-1]
        _assert_cut_marker(heatmap, cut)
