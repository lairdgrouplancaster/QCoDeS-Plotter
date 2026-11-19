from pathlib import Path

import numpy as np
import pytest
import qcodes
from PyQt6 import QtWidgets as qtw

from qplot.windows import main as main_window
from qplot.windows._database_detail_state import detail_channel
from qplot.windows._dataset_handle import TraceKey
from tests._window_lifecycle import close_main_window
from tests.support.windows.differentiation_integration import (
    apply_operations,
    operation_option,
)
from tests.support.windows.heatmap_layer_integration import (
    _assert_reader_artifact_invariant as _assert_reader_artifact_invariant,
)
from tests.support.windows.heatmap_layer_integration import (
    _build_two_heatmap_database as _build_two_heatmap_database,
)
from tests.support.windows.heatmap_layer_integration import (
    _configure_temp_qplot as _configure_temp_qplot,
)
from tests.support.windows.heatmap_layer_integration import (
    _database_artifact_state as _database_artifact_state,
)
from tests.support.windows.heatmap_layer_integration import (
    _drop_heatmap as _drop_heatmap,
)
from tests.support.windows.heatmap_layer_integration import (
    _PreviewDropEvent as _PreviewDropEvent,
)
from tests.support.windows.heatmap_layer_integration import (
    _wait_for as _wait_for,
)
from tests.support.windows.plot3d import main as main


@pytest.mark.parametrize("changed_plot", ["host", "source"])
def test_dropped_heatmap_tracks_processed_colour_units(main, tmp_path, monkeypatch, changed_plot):
    window, errors = main
    monkeypatch.setitem(qcodes.config.core, "db_location", qcodes.config.core.db_location)
    path = tmp_path / "processed-layers.db"
    run_id, guid, primary, secondary = _build_two_heatmap_database(path)
    window.show()
    assert window.load_file(str(path))
    _wait_for(lambda: (
        not window._database_load_active
        and window._selected_run_guid == guid
        and window.run_idBox.text() == str(run_id)
        and window.plotRunButton.isEnabled()
    ))
    assert window.measurementBox.text() == "*"
    window.plotRunButton.click()
    try:
        _wait_for(lambda: len(window.windows) == 2 and all(
            hasattr(plot, "dataGrid") and plot._heatmap_geometry() is not None
            and not plot.worker.running and plot.image.isVisible()
            for plot in window.windows
        ))
    except AssertionError:
        frames = [
            (plot.param.name, plot._dataset_key.guid, np.shape(getattr(plot, "dataGrid", None)),
             getattr(getattr(plot, "worker", None), "running", None))
            for plot in window.windows
        ]
        pytest.fail(f"Expected signal_a and signal_b for {guid}. "
                    f"Published (parameter, GUID, grid shape, worker running): {frames}. "
                    f"Window errors: {errors}")
    by_parameter = {plot.param.name: plot for plot in window.windows}
    host, source = by_parameter["signal_a"], by_parameter["signal_b"]
    plots = {"host": host, "source": source}
    assert host._dataset_key.guid == source._dataset_key.guid == guid
    np.testing.assert_array_equal(host.image.image, primary)
    np.testing.assert_array_equal(source.image.image, secondary)
    _drop_heatmap(host, guid, source.param)
    _wait_for(lambda: source._trace_key in host.heatmaps)
    layer = host.heatmaps[source._trace_key]
    np.testing.assert_array_equal(layer.image.image, secondary)
    assert layer.from_win is source and source._merged_trace_users == 1
    opacity = layer.opacity

    changed = plots[changed_plot]
    differentiate = operation_option(changed, "dz/dx").input
    differentiate.click()
    assert apply_operations(changed)[1:] == ([True], [])
    assert changed.display_param.unit == "nA/mV"
    assert host.image.isVisible()
    np.testing.assert_allclose(host.image.image, 1 if changed is host else primary)
    assert not layer.image.isVisible() and not layer.heatmap_mesh.isVisible()
    assert len(host._heatmap_colorbar_data_arrays()) == 1
    assert "Signal A" in host.source_annotation.text and "Signal B" not in host.source_annotation.text
    host.open_colorbar_scale_dialog()
    host.colorbar_auto_radio.click()
    expected_limits = (0.999999, 1.000001) if changed is host else (primary.min(), primary.max())
    np.testing.assert_allclose(host.bar.levels(), expected_limits)
    host.colorbar_scale_dialog.close()
    assert layer.visible and layer.opacity == opacity and source._merged_trace_users == 1

    differentiate.click()
    assert apply_operations(changed)[1:] == ([True], [])
    assert changed.display_param.unit == "nA"
    assert layer.image.isVisible() and not layer.heatmap_mesh.isVisible()
    np.testing.assert_array_equal(layer.image.image, secondary)
    np.testing.assert_array_equal(host.image.image, primary)
    host.open_colorbar_scale_dialog()
    host.colorbar_auto_radio.click()
    np.testing.assert_allclose(host.bar.levels(), (primary.min(), secondary.max()))
    host.colorbar_scale_dialog.close()
    assert "Signal A" in host.source_annotation.text and "Signal B" in host.source_annotation.text
    assert host.heatmaps[source._trace_key] is layer and layer.opacity == opacity
    assert host.remove_heatmap(trace_key=source._trace_key)
    assert source._merged_trace_users == 0 and not errors


@pytest.mark.parametrize("sql_source", [False, True], ids=["array", "sql"])
def test_preview_drop_adds_and_removes_real_secondary_heatmap(
    tmp_path,
    monkeypatch,
    sql_source,
):
    _configure_temp_qplot(monkeypatch, tmp_path)
    original_database_path = qcodes.config.core.db_location
    database_path = Path(tmp_path) / "heatmap-layers.db"
    _run_id, guid, expected_primary, expected_secondary = (
        _build_two_heatmap_database(database_path)
    )
    original_artifacts = _database_artifact_state(database_path)
    window = None

    try:
        window = main_window.MainWindow()
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.config.config["runtime_settings"]["del_grace_period"] = 0
        if sql_source:
            window.config.config["runtime_settings"]["max_full_heatmap_points"] = 4
        window.close_database(status=False)

        assert window.load_file(str(database_path))
        _wait_for(
            lambda: (
                not window._database_load_active
                and not detail_channel(window).active
                and not detail_channel(window, True).active
            )
        )
        window.monitor.stop()
        assert window.ds is None
        assert window._selected_run_guid == guid

        # Ordinary trusted selection stays DB-free. The explicit plot
        # action materialises only the requested trace against the accepted
        # database instance.
        prior_plot_count = len(window.windows)
        window.open_preview_plot("signal_a")
        _wait_for(lambda prior_plot_count=prior_plot_count: len(window.windows) > prior_plot_count)
        target = window.windows[-1]
        _wait_for(
            lambda: (
                hasattr(target, "dataGrid")
                and target._heatmap_geometry() is not None
                and not getattr(target.worker, "running", False)
            )
        )

        parameters = {
            parameter.name: parameter
            for parameter in target.ds.get_parameters()
            if parameter.depends_on
        }
        primary_parameter = parameters["signal_a"]
        secondary_parameter = parameters["signal_b"]
        assert target.param == primary_parameter

        np.testing.assert_allclose(target.dataGrid, expected_primary)
        primary_grid_object = target.dataGrid
        primary_grid = np.asarray(target.dataGrid).copy()
        primary_geometry = target._heatmap_geometry()
        primary_key = target._trace_key
        dataset_key = target._dataset_key
        dataset_handle = window.dataset_holder[dataset_key]
        primary_owner_count = dataset_handle.users
        secondary_key = TraceKey(dataset_key, secondary_parameter.name)

        _drop_heatmap(target, guid, secondary_parameter, axes_pending=True)
        _wait_for(
            lambda: (
                secondary_key in target.heatmaps
                and target.heatmaps[secondary_key].geometry is not None
                and target.heatmaps[secondary_key].image.isVisible()
                and not getattr(
                    target.heatmaps[secondary_key].from_win.worker,
                    "running",
                    False,
                )
            )
        )

        assert set(target.heatmaps) == {primary_key, secondary_key}
        assert target.heatmaps[primary_key] is target
        layer = target.heatmaps[secondary_key]
        source = layer.from_win
        assert layer.image.isVisible()
        assert not layer.heatmap_mesh.isVisible()
        np.testing.assert_allclose(layer.data_grid, expected_secondary)
        np.testing.assert_allclose(layer.image.image, expected_secondary)
        assert np.min(layer.data_grid) > np.max(primary_grid)

        assert target.dataGrid is primary_grid_object
        np.testing.assert_array_equal(target.dataGrid, primary_grid)
        assert target._heatmap_geometry() is primary_geometry
        assert source._closed
        assert source not in window.windows
        assert window.windows == [target]
        assert source._merged_trace_users == 1
        assert dataset_handle.users == primary_owner_count + 1

        if sql_source:
            # A hidden source remains live while its overlay owns a consumer.
            assert source.load_data(force_sql_heatmap=True)
            _wait_for(lambda: source._large_heatmap_sql_mode
                      and not source._refresh_controller().session.active
                      and not source._viewport_controller().pending)
            assert source._large_heatmap_sql_mode
            np.testing.assert_allclose(layer.data_grid, expected_secondary)

        _drop_heatmap(target, guid, secondary_parameter)
        qtw.QApplication.processEvents()
        assert set(target.heatmaps) == {primary_key, secondary_key}
        assert target.heatmaps[secondary_key] is layer
        assert layer.from_win is source
        assert window.windows == [target]
        assert source._merged_trace_users == 1
        assert dataset_handle.users == primary_owner_count + 1

        if sql_source:
            source._schedule_visible_heatmap_reload()
            assert source._viewport_controller().pending
            assert source._heatmap_view_reload_timer.isActive()

        assert target.remove_heatmap(trace_key=secondary_key)
        assert set(target.heatmaps) == {primary_key}
        assert source._merged_trace_users == 0
        assert not source.monitor.isActive()
        assert not source._viewport_controller().pending
        assert not source._heatmap_view_reload_timer.isActive()
        assert not layer.image.isVisible()
        assert not layer.heatmap_mesh.isVisible()
        assert dataset_handle.users == primary_owner_count
        np.testing.assert_array_equal(target.dataGrid, primary_grid)
        assert target._heatmap_geometry() is primary_geometry
        _assert_reader_artifact_invariant(database_path, original_artifacts)
    finally:
        if window is not None:
            close_main_window(window)
        qcodes.config.core.db_location = original_database_path

    _assert_reader_artifact_invariant(database_path, original_artifacts)
