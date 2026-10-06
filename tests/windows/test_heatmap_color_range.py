"""Unrepresentable heatmap color spans fail visibly without Qt callbacks."""
from contextlib import contextmanager

import numpy as np
import pytest
from PyQt6 import QtCore
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.dataset.sqlite.database import connect

from qplot.testdata import enable_generation_provenance_for_writer
from qplot.windows.main import MainWindow
from tests._window_lifecycle import close_main_window
from tests.windows.test_complex_line_data import database_state
from tests.windows.test_differentiation_integration import (
    apply_operations,
    operation_option,
)
from tests.windows.test_native_transform_labels import (
    no_callback_errors as _no_callback_errors_fixture,
)
from tests.windows.test_plot_csv_precision import _export_from_dialog, _open_csv_dialog
from tests.windows.test_plot_integration import (
    configure_temp_qplot,
    prepare_generated_database_for_live_writes,
    trusted_database_artifact_state,
    wait_for,
)

no_callback_errors = _no_callback_errors_fixture


def _create_color_database(path, values):
    assert not path.exists()
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("color range audit", "synthetic")
    measurement = Measurement(exp=experiment)
    for name in ("x", "slow"):
        measurement.register_custom_parameter(name, paramtype="array")
    measurement.register_custom_parameter("signal", paramtype="array", setpoints=("slow", "x"))
    x = np.arange(len(values), dtype=float) / 10
    grid = np.tile(values, (2, 1))
    try:
        with measurement.run(write_in_background=False) as saver:
            saver.add_result(("x", np.tile(x, (2, 1))),
                             ("slow", np.repeat(np.arange(2)[:, None], len(x), axis=1)),
                             ("signal", grid))
        guid = saver.dataset.guid
        np.testing.assert_array_equal(saver.dataset.get_parameter_data("signal")["signal"]["signal"].ravel(), grid.ravel())
    finally:
        saver.dataset.conn.close()
        experiment.conn.close()
    return guid, grid


@pytest.fixture
def color_plot(tmp_path, monkeypatch, request, no_callback_errors):
    configure_temp_qplot(monkeypatch, tmp_path)
    path = tmp_path / "color-range.db"
    guid, grid = _create_color_database(path, request.param)
    before = database_state(path)
    window = MainWindow()
    window.startupDatabaseTimer.stop()
    window.monitor.stop()
    window.config.config["user_preference"]["confirm_close"] = False
    window.config.config["user_preference"]["confirm_close_all"] = False
    try:
        assert window.load_file(str(path))
        wait_for(lambda: not window._database_load_active)
        prior_plot_count = len(window.windows)
        window.openPlot(guid=guid, show=True)
        wait_for(lambda prior_plot_count=prior_plot_count: len(window.windows) > prior_plot_count)
        plot = window.windows[-1]
        wait_for(lambda: not plot.worker.running and plot._qplot_display_synchronized)
        plot.monitor.stop()
        yield plot, grid
    finally:
        close_main_window(window)
        assert database_state(path) == before


def assert_color_error(plot):
    assert not plot.image.isVisible()
    assert not plot.heatmap_mesh.isVisible()
    assert not getattr(plot, "bar", plot.image).isVisible()
    assert plot.plot_state_overlay.frame.isVisible()
    assert "color" in plot.plot_state_overlay.title_label.text().lower()
    assert "range" in plot.plot_state_overlay.detail_label.text().lower()


@pytest.mark.parametrize("color_plot", [np.array([0., 1e308, 1., -1e308])], indirect=True)
def test_initial_unrepresentable_color_span_preserves_raw_csv(color_plot, monkeypatch, tmp_path):
    plot, grid = color_plot
    assert_color_error(plot)
    np.testing.assert_array_equal(plot.dataGrid, grid)
    rows = _export_from_dialog(monkeypatch, plot, _open_csv_dialog(plot), tmp_path / "extreme.csv", ",")
    np.testing.assert_array_equal(np.asarray(rows[1:], dtype=float)[:, 2], grid.ravel())


@pytest.mark.parametrize("color_plot", [np.array([0., 1e308, 1., -1e308])], indirect=True)
def test_completed_color_rejection_is_stable_after_due_refresh(color_plot, qapplication):
    plot, grid = color_plot
    assert_color_error(plot)
    assert not plot.ds.running
    assert plot._qplot_display_synchronized
    assert not plot._refresh_monitor_required()
    finished_worker = plot.worker
    # Deliver the timeout in a subsequent Qt event drain, as can happen in the
    # fixture wait's final processEvents call. No replacement read is necessary.
    plot.monitor.start(1)
    QtCore.QTimer.singleShot(0, plot.monitor.timeout.emit)
    qapplication.processEvents()
    assert plot.worker is finished_worker
    assert not plot.monitor.isActive()
    assert_color_error(plot)
    np.testing.assert_array_equal(plot.dataGrid, grid)
    plot.refreshWindow(force=True)
    assert plot.worker is not finished_worker
    wait_for(lambda: not plot.worker.running and plot._qplot_display_synchronized)
    assert plot._qplot_display_synchronized
    assert_color_error(plot)
    np.testing.assert_array_equal(plot.dataGrid, grid)


def test_live_color_rejection_keeps_polling_and_manual_span_recovers_on_completion(
    tmp_path, monkeypatch, no_callback_errors,
):
    configure_temp_qplot(monkeypatch, tmp_path)
    path = tmp_path / "live-color-range.db"
    assert not path.exists()
    prepare_generated_database_for_live_writes(path)
    initialise_or_create_database_at(str(path), journal_mode="WAL")
    writer_connection = connect(path)
    enable_generation_provenance_for_writer(writer_connection)
    experiment = load_or_create_experiment(
        "live color range", "owned", conn=writer_connection,
    )
    measurement = Measurement(exp=experiment)
    for name in ("x", "slow"):
        measurement.register_custom_parameter(name, paramtype="array")
    measurement.register_custom_parameter("signal", paramtype="array", setpoints=("slow", "x"))
    grid = np.array([[0., 1e308, 1., -1e308], [0., 1e308, 1., -1e308]])
    window = None
    try:
        with measurement.run(write_in_background=False) as saver:
            saver.add_result(("x", np.tile(np.arange(4) / 10, (2, 1))),
                             ("slow", np.repeat(np.arange(2)[:, None], 4, axis=1)),
                             ("signal", grid))
            saver.flush_data_to_database()
            protected = trusted_database_artifact_state(path)
            window = MainWindow()
            window.startupDatabaseTimer.stop()
            window.monitor.stop()
            window.config.config["user_preference"]["confirm_close"] = False
            window.config.config["user_preference"]["confirm_close_all"] = False
            assert window.load_file(str(path))
            wait_for(lambda: not window._database_load_active)
            prior_plot_count = len(window.windows)
            window.openPlot(guid=saver.dataset.guid, show=True)
            wait_for(lambda prior_plot_count=prior_plot_count: len(window.windows) > prior_plot_count)
            plot = window.windows[-1]

            @contextmanager
            def finite_live_publication():
                # Live polling is intentionally active. Fence only its signals
                # during a finite publication/oracle so the final event drain
                # cannot replace the state being checked with a new load.
                interval = plot.monitor.interval()
                assert plot.monitor.isActive()
                with QtCore.QSignalBlocker(plot.monitor):
                    QtCore.QTimer.singleShot(0, plot.monitor.timeout.emit)
                    yield
                    assert plot.monitor.isActive()
                    assert plot.monitor.interval() == interval
                assert not plot.monitor.signalsBlocked()
                assert plot.monitor.isActive()
                assert plot.monitor.interval() == interval

            with finite_live_publication():
                wait_for(lambda: not plot.worker.running and hasattr(plot, "dataGrid"))
                assert_color_error(plot)
            assert plot.ds.running
            assert not plot._qplot_display_synchronized
            assert plot._refresh_monitor_required()
            assert plot.monitor.isActive()
            first_worker = plot.worker
            plot.monitor.timeout.emit()
            assert plot.worker is not first_worker
            with finite_live_publication():
                wait_for(lambda: not plot.worker.running)
                assert_color_error(plot)
            assert plot.monitor.isActive()
            np.testing.assert_array_equal(plot.dataGrid, grid)
            with finite_live_publication():
                plot.open_colorbar_scale_dialog()
                plot.colorbar_min_text.setText("-1e307")
                plot.colorbar_max_text.setText("1e307")
                plot.colorbar_max_text.editingFinished.emit()
                wait_for(lambda: not plot.worker.running and plot.image.isVisible())
            assert plot.ds.running
            assert plot.monitor.isActive()
            assert plot.bar.isVisible()
            assert not plot.plot_state_overlay.frame.isVisible()
            assert tuple(plot.image.getLevels()) == (-1e307, 1e307)
            np.testing.assert_array_equal(plot.dataGrid, grid)
            assert trusted_database_artifact_state(path) == protected
        # Only the owning writer changes completion metadata. The viewer then
        # loads that terminal state while preserving the explicit valid span.
        protected = trusted_database_artifact_state(path)
        plot.refreshWindow(force=True)
        wait_for(
            lambda: not plot.worker.running
            and not plot.ds.running
            and plot._qplot_display_synchronized
        )
        assert plot._qplot_display_synchronized
        assert not plot._refresh_monitor_required()
        plot.monitor.timeout.emit()
        assert not plot.monitor.isActive()
        assert plot.image.isVisible() and plot.bar.isVisible()
        assert tuple(plot.image.getLevels()) == (-1e307, 1e307)
        assert not plot.plot_state_overlay.frame.isVisible()
        np.testing.assert_array_equal(plot.dataGrid, grid)
        assert trusted_database_artifact_state(path) == protected
    finally:
        protected = trusted_database_artifact_state(path)
        if window is not None:
            close_main_window(window)
        assert trusted_database_artifact_state(path) == protected
        if "saver" in locals():
            saver.dataset.conn.close()
        experiment.conn.close()
        writer_connection.close()


@pytest.mark.parametrize("color_plot", [np.array([0., 1e307, 0.])], indirect=True)
def test_refresh_unrepresentable_color_span_hides_previous_display_and_recovers(color_plot):
    plot, grid = color_plot
    assert plot.image.isVisible()
    assert plot.bar.isVisible()
    operation = operation_option(plot, "dz/dx")
    operation.input.setChecked(True)
    apply_operations(plot)
    assert_color_error(plot)
    assert np.all(np.isfinite(plot.dataGrid))
    assert np.max(plot.dataGrid) > 9e307
    assert np.min(plot.dataGrid) < -9e307
    operation.input.setChecked(False)
    apply_operations(plot)
    np.testing.assert_array_equal(plot.dataGrid, grid)
    assert plot.image.isVisible()
    assert plot.bar.isVisible()
    assert not plot.plot_state_overlay.frame.isVisible()


@pytest.mark.parametrize("color_plot", [np.array([0., 1., 2.])], indirect=True)
def test_manual_unrepresentable_color_span_keeps_previous_display(color_plot):
    plot, grid = color_plot
    previous = plot.bar.levels()
    plot.open_colorbar_scale_dialog()
    plot.colorbar_min_text.setText("-1e308")
    plot.colorbar_max_text.setText("1e308")
    plot.colorbar_max_text.editingFinished.emit()
    assert plot.bar.levels() == previous
    assert plot._colorbar_manual_levels is None
    assert plot.image.isVisible()
    assert plot.bar.isVisible()
    np.testing.assert_array_equal(plot.dataGrid, grid)


@pytest.mark.parametrize("color_plot", [np.array([0., 1e308, 1., -1e308])], indirect=True)
def test_manual_finite_span_recovers_initial_rejection(color_plot):
    plot, grid = color_plot
    assert_color_error(plot)
    plot.open_colorbar_scale_dialog()
    plot.colorbar_min_text.setText("-1e307")
    plot.colorbar_max_text.setText("1e307")
    plot.colorbar_max_text.editingFinished.emit()
    wait_for(lambda: not plot.worker.running and plot.image.isVisible())
    plot.monitor.stop()
    assert plot.bar.isVisible()
    assert plot._colorbar_manual_levels == (-1e307, 1e307)
    assert tuple(plot.image.getLevels()) == (-1e307, 1e307)
    assert not plot.plot_state_overlay.frame.isVisible()
    np.testing.assert_array_equal(plot.dataGrid, grid)


@pytest.mark.parametrize("color_plot", [np.array([0., 1e307, 0.])], indirect=True)
def test_manual_finite_span_is_installed_before_extreme_refresh(color_plot):
    plot, _grid = color_plot
    plot.open_colorbar_scale_dialog()
    plot.colorbar_min_text.setText("-1e307")
    plot.colorbar_max_text.setText("1e307")
    plot.colorbar_max_text.editingFinished.emit()
    operation_option(plot, "dz/dx").input.setChecked(True)
    apply_operations(plot)
    assert plot.image.isVisible()
    assert plot.bar.isVisible()
    assert tuple(plot.image.getLevels()) == (-1e307, 1e307)
    assert not plot.plot_state_overlay.frame.isVisible()
    assert np.max(plot.dataGrid) > 9e307
    assert np.min(plot.dataGrid) < -9e307


@pytest.mark.parametrize("color_plot", [np.array([0., 1., 2.])], indirect=True)
def test_layer_visibility_preserves_current_manual_color_levels(color_plot):
    plot, _grid = color_plot
    plot.open_colorbar_scale_dialog()
    plot.colorbar_min_text.setText("0.25")
    plot.colorbar_max_text.setText("1.25")
    plot.colorbar_max_text.editingFinished.emit()
    assert tuple(plot.image.getLevels()) == (0.25, 1.25)
    plot.open_heatmap_appearance_dialog(plot._primary_heatmap_key)
    dialog = plot._heatmap_appearance_dialog
    dialog.visible.setChecked(False)
    assert not plot.image.isVisible()
    dialog.visible.setChecked(True)
    assert plot.image.isVisible()
    assert tuple(plot.image.getLevels()) == plot.bar.levels() == (0.25, 1.25)
