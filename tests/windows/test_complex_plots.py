"""Real complex measurements, persisted projections and original cursor values."""

import hashlib

import numpy as np
import pytest
from PyQt6 import QtCore, QtTest
from qcodes.dataset import Measurement, initialise_or_create_database_at, new_experiment

from qplot.configuration.config import config
from qplot.datahandling.complex_data import COMPLEX_1D_KEY, COMPLEX_ND_KEY
from qplot.windows._complex_plots import format_complex_cursor
from qplot.windows._preferences import PreferencesDialog
from qplot.windows._widgets.preview import PreviewImageLabel
from qplot.windows.main import MainWindow
from tests._window_lifecycle import close_main_window
from tests.windows.test_plot_integration import configure_temp_qplot, wait_for


def component(values, mode):
    return {"magnitude": np.abs, "phase": lambda z: np.angle(z, deg=True),
            "real": np.real, "imag": np.imag}[mode](values)


@pytest.fixture
def complex_window(tmp_path, monkeypatch):
    configure_temp_qplot(monkeypatch, tmp_path)
    path = tmp_path / "complex.db"
    initialise_or_create_database_at(path, journal_mode="DELETE")
    experiment = new_experiment("Complex signals", "Synthetic")
    measurement = Measurement(exp=experiment)
    for name in ("frequency", "gate", "field"):
        measurement.register_custom_parameter(name, unit="GHz" if name == "frequency" else "V")
    for name, axes in (("S11", ("frequency",)), ("S21", ("gate", "frequency")),
                       ("S12", ("field", "gate", "frequency"))):
        measurement.register_custom_parameter(name, setpoints=axes, paramtype="complex")
    values = np.array([1 + 2j, -3 + 4j, -2 - 3j, 4 - 1j])
    with measurement.run(write_in_background=False) as saver:
        for i, frequency in enumerate(range(4, 8)):
            saver.add_result(("frequency", frequency), ("S11", values[i]))
        for gate in (0, 1):
            for i, frequency in enumerate(range(4, 8)):
                saver.add_result(("frequency", frequency), ("gate", gate), ("S21", values[i] + 10 * gate))
        for field in (0, 1):
            for gate in (0, 1):
                for i, frequency in enumerate(range(4, 8)):
                    saver.add_result(("frequency", frequency), ("gate", gate), ("field", field),
                                     ("S12", values[i] + 10 * gate + 100 * field))
    guid = saver.dataset.guid
    params = {param.name: param for param in saver.dataset.get_parameters()}
    saver.dataset.conn.close()
    experiment.conn.close()
    before = hashlib.sha256(path.read_bytes()).digest()
    window = MainWindow()
    window.startupDatabaseTimer.stop()
    window.monitor.stop()
    window.config.update_many({"user_preference.confirm_close": False,
                               "user_preference.confirm_close_all": False})
    window.close_database(status=False)
    window.autoPlotBox.setChecked(False)
    assert window.load_file(str(path))
    wait_for(lambda: not window._database_load_active)
    window.monitor.stop()
    try:
        yield window, guid, params, values
    finally:
        close_main_window(window)
    assert hashlib.sha256(path.read_bytes()).digest() == before
    assert not path.with_name(path.name + "-wal").exists()
    assert not path.with_name(path.name + "-journal").exists()


def open_plot(window, guid, param):
    previous = len(window.windows)
    window.openPlot(guid, params=[param], show=True)
    wait_for(lambda: len(window.windows) > previous)
    plot = window.windows[-1]
    wait_for(lambda: hasattr(plot, "worker") and not plot.worker.running)
    assert plot.worker.result is not None, plot._last_error_text
    plot.monitor.stop()
    return plot


def test_complex_previews_refresh_with_persistent_preferences(complex_window):
    window, guid, _, _ = complex_window
    window.show()
    item = next(window.RunList.topLevelItem(index) for index in range(window.RunList.topLevelItemCount())
                if window.RunList.topLevelItem(index).guid == guid)
    window.RunList.setCurrentItem(item)
    window.RunList.scrollToItem(item)
    window.infoBox.setCurrentWidget(window.infoBox.preview)
    cell = window.RunList.preview_cells[guid]
    preview = window.infoBox.preview

    def rendered_modes():
        if guid not in preview.cache or not cell._has_rendered_previews:
            return None
        small = {image["parameter"]: image.get("complex_mode") for image in cell._previews}
        large = {image["parameter"]: image.get("complex_mode") for image in preview.cache[guid]}
        return small, large

    for line_mode, map_mode in (("magnitude", "magnitude"), ("phase", "real"),
                               ("real", "imag"), ("imag", "phase"),
                               ("imag_vs_real", "magnitude")):
        window.config.update_many({COMPLEX_1D_KEY: line_mode, COMPLEX_ND_KEY: map_mode})
        window.apply_current_settings()
        expected = {"S11": line_mode, "S21": map_mode, "S12": None}
        wait_for(lambda expected=expected: rendered_modes() == (expected, expected))
        for host in (cell, preview):
            labels = host.findChildren(PreviewImageLabel)
            assert {label.parameter for label in labels} == {"S11", "S21", "S12"}
            assert all(label.property("complexDataset") for label in labels)
            assert all("Complex-valued measurement" in label.accessibleDescription() for label in labels)
            assert all(not label.pixmap().isNull() for label in labels if label.parameter != "S12")
    # The preferences alone generate these images, without opening a plot.
    assert not window.windows


@pytest.mark.parametrize("mode", ["magnitude", "phase", "real", "imag", "imag_vs_real"])
def test_complex_line_modes_keep_original_cursor_and_setpoint(complex_window, mode):
    window, guid, params, values = complex_window
    plot = open_plot(window, guid, params["S11"])
    np.testing.assert_allclose(plot.axis_data["y"], np.abs(values))
    old_worker = plot.worker
    window.config.update(COMPLEX_1D_KEY, mode)
    window.apply_current_settings()
    if mode != "magnitude":
        wait_for(lambda: plot.worker is not old_worker and not plot.worker.running)
    assert plot.worker.result is not None, plot._last_error_text
    x = values.real if mode == "imag_vs_real" else np.arange(4, 8)
    y = values.imag if mode == "imag_vs_real" else component(values, mode)
    np.testing.assert_allclose(plot.axis_data["x"], x)
    np.testing.assert_allclose(plot.axis_data["y"], y)
    if mode == "imag_vs_real":
        assert plot.axis_param["x"].label == "Re(S11)"
        assert plot.axis_param["y"].label == "Im(S11)"
        assert not plot.can_swap_plot_axes()
        assert not plot.axis_dropdown["x"].isEnabled()
    elif mode == "phase":
        assert plot.axis_param["y"].unit == "°"
    plot.force_all_axes_autoscale()
    point = plot.vb.mapViewToScene(QtCore.QPointF(float(x[2]), float(y[2])))
    for snap in (False, True):
        plot.snap_to_trace_action.setChecked(snap)
        plot.mouseMoved(point)
        assert float(plot.pos_labels["x"].text()) == 6.
        assert plot.pos_labels["y"].text() == format_complex_cursor(plot, values[2])
        assert plot.copy_coordinates()


@pytest.mark.parametrize("name", ["S21", "S12"], ids=["2D", "3D"])
@pytest.mark.parametrize("mode", ["magnitude", "phase", "real", "imag"])
def test_complex_heatmaps_and_slices_keep_original_cursor(complex_window, mode, name):
    window, guid, params, values = complex_window
    window.config.update(COMPLEX_ND_KEY, mode)
    plot = open_plot(window, guid, params[name])
    raw = plot.complex_samples
    assert raw is not None
    x, y = plot.axis_data["x"], plot.axis_data["y"]
    for j, y_value in enumerate(y):
        for i, x_value in enumerate(x):
            coordinates = {plot.axis_param["x"].name: x_value, plot.axis_param["y"].name: y_value}
            if name == "S12":
                coordinates[plot._depth_name()] = plot.slice_frame.depth
            expected = values[int(coordinates["frequency"]) - 4] + 10 * coordinates["gate"]
            if name == "S12":
                expected += 100 * coordinates["field"]
            np.testing.assert_allclose(plot.dataGrid[j, i], component(expected, mode))
    sample = plot.heatmap_sample_at(float(x[1]), float(y[1]))
    assert sample is not None and isinstance(sample[-1], complex)
    plot.mouseMoved(plot.vb.mapViewToScene(QtCore.QPointF(float(x[1]), float(y[1]))))
    assert plot.pos_labels["z"].text() == format_complex_cursor(plot, sample[-1])
    if mode == "phase":
        assert plot.display_param.unit == "°"


def test_complex_preferences_persist_and_restore(tmp_path, monkeypatch):
    configure_temp_qplot(monkeypatch, tmp_path)
    cfg = config()
    dialog = PreferencesDialog(cfg)
    try:
        assert dialog.complex1dCombo.currentData() == "magnitude"
        assert dialog.complexNdCombo.currentData() == "magnitude"
        dialog.complex1dCombo.setCurrentIndex(dialog.complex1dCombo.findData("imag_vs_real"))
        dialog.complexNdCombo.setCurrentIndex(dialog.complexNdCombo.findData("phase"))
        assert dialog.apply_preferences()
        restored = config()
        assert restored.get(COMPLEX_1D_KEY) == "imag_vs_real"
        assert restored.get(COMPLEX_ND_KEY) == "phase"
        assert dialog.default_preference_values()[COMPLEX_1D_KEY] == "magnitude"
        assert dialog.complexNdCombo.findData("imag_vs_real") == -1
    finally:
        dialog.close()


@pytest.mark.parametrize("name", ["S21", "S12"], ids=["2D", "3D"])
def test_changing_higher_dimensional_preference_refreshes_open_plots(complex_window, name):
    window, guid, params, _ = complex_window
    plot = open_plot(window, guid, params[name])
    before = plot.worker
    window.config.update(COMPLEX_ND_KEY, "phase")
    window.apply_current_settings()
    wait_for(lambda: plot.worker is not before and not plot.worker.running)
    assert plot.worker.result is not None, plot._last_error_text
    assert plot.complex_samples.mode == "phase"
    assert plot.display_param.unit == "°"
    sample = plot.heatmap_sample_at(float(plot.axis_data["x"][1]), float(plot.axis_data["y"][1]))
    np.testing.assert_allclose(plot.dataGrid[1, 1], np.angle(sample[-1], deg=True))


@pytest.mark.parametrize("name", ["S21", "S12"], ids=["2D", "3D"])
def test_complex_cuts_keep_original_values(complex_window, name):
    window, guid, params, _ = complex_window
    plot = open_plot(window, guid, params[name])
    previous = len(window.windows)
    plot.show_hover_pixel_outline(1, 1)
    plot.openSweep("h")
    wait_for(lambda: len(window.windows) > previous)
    cut = window.windows[-1]
    wait_for(lambda: hasattr(cut, "worker") and not cut.worker.running)
    assert cut.worker.result is not None, cut._last_error_text
    point = cut.vb.mapViewToScene(QtCore.QPointF(float(cut.axis_data["x"][1]), float(cut.axis_data["y"][1])))
    for snap in (False, True):
        cut.snap_to_trace_action.setChecked(snap)
        cut.mouseMoved(point)
        assert "i" in cut.pos_labels["y"].text()


@pytest.mark.parametrize("run_name", ["complex_response", "complex_gate_map"])
@pytest.mark.parametrize("surface", ["thumbnail", "preview"])
def test_bundled_complex_preview_double_click_opens_plot(tmp_path, monkeypatch, run_name, surface):
    from qplot.testdata import (
        copy_instruction_collection,
        generate_database_from_csv,
    )

    configure_temp_qplot(monkeypatch, tmp_path)
    source = copy_instruction_collection(tmp_path / "instructions")[0]
    database, _ = generate_database_from_csv(source, tmp_path / "synthetic.db")
    before = hashlib.sha256(database.read_bytes()).digest()
    window = MainWindow()
    try:
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.update_many({"user_preference.confirm_close": False,
                                   "user_preference.confirm_close_all": False})
        window.close_database(status=False)
        window.autoPlotBox.setChecked(False)
        assert window.load_file(str(database))
        wait_for(lambda: not window._database_load_active)
        window.show()
        item = next(window.RunList.topLevelItem(index) for index in range(window.RunList.topLevelItemCount())
                    if window.RunList.topLevelItem(index).run_metadata.get("name") == run_name)
        window.RunList.setCurrentItem(item)
        window.RunList.scrollToItem(item)
        cell = window.RunList.preview_cells[item.guid]
        wait_for(lambda: cell._has_rendered_previews and len(cell._previews) == 3)
        expected_count = "21" if run_name == "complex_response" else "231 = 11 × 21"
        column = window.RunList.cols.index("Setpoints")
        wait_for(lambda: item.text(column) == expected_count)
        if surface == "thumbnail":
            host = cell
        else:
            host = window.infoBox.preview
            wait_for(lambda: len(host.findChildren(PreviewImageLabel)) == 3)
        labels = host.findChildren(PreviewImageLabel)
        assert {label.parameter for label in labels} == {"S11", "S21", "S12"}
        assert all(label.plot_supported for label in labels)
        assert all(label.property("complexDataset") for label in labels)
        assert all(label.pixmap() is not None and not label.pixmap().isNull() for label in labels)
        assert all("ℂ" in label.toolTip() and "Magnitude" in label.toolTip() for label in labels)
        label = next(label for label in labels if label.parameter == "S11")
        previous = len(window.windows)
        QtTest.QTest.mouseClick(label, QtCore.Qt.MouseButton.LeftButton)
        QtTest.QTest.mouseDClick(label, QtCore.Qt.MouseButton.LeftButton)
        wait_for(lambda: len(window.windows) > previous)
        plot = window.windows[-1]
        wait_for(lambda: hasattr(plot, "worker") and not plot.worker.running)
        assert plot.worker.result is not None, plot._last_error_text
        assert plot.param.name == "S11"
        assert plot.complex_samples is not None
        assert np.iscomplexobj(plot.complex_samples.values)
        assert np.isfinite(plot.axis_data["y"]).any()
    finally:
        close_main_window(window)
    assert hashlib.sha256(database.read_bytes()).digest() == before
