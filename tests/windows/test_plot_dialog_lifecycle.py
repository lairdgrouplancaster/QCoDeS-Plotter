"""Closing real QCoDeS plots also closes their owned editors."""

import os

import numpy as np
import pytest
from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from qplot.diagnostics import configure_logging
from qplot.windows.main import MainWindow
from tests._window_lifecycle import close_main_window
from tests.windows.test_heatmap_layer_integration import (
    _assert_reader_artifact_invariant,
    _build_two_heatmap_database,
    _database_artifact_state,
)
from tests.windows.test_plot_csv_precision import _create_measurements
from tests.windows.test_plot_integration import configure_temp_qplot, wait_for


@pytest.fixture
def dialog_plot(tmp_path, monkeypatch, request, qapplication):
    configure_logging(tmp_path / "qplot.log", force=True)
    configure_temp_qplot(monkeypatch, tmp_path)
    database = tmp_path / "dialog-lifecycle.db"
    mode = getattr(request, "param", "heatmap")
    if mode == "line":
        measurements = _create_measurements(
            database,
            [(np.arange(4.), np.arange(4.) * factor) for factor in (2, 3)],
        )
        guids = [guid for guid, _expected in measurements]
    else:
        _run_id, guid, _primary, _secondary = _build_two_heatmap_database(database)
        guids = [guid]
    original = _database_artifact_state(database)
    protected = {
        os.path.normcase(os.path.abspath(f"{database}{suffix}"))
        for suffix in ("", "-wal", "-shm", "-journal")
    }

    def guard_mutation(operation):
        def guarded(*args, **kwargs):
            for candidate in args[:2]:
                if isinstance(candidate, (str, bytes, os.PathLike)):
                    assert os.path.normcase(os.path.abspath(candidate)) not in protected
            return operation(*args, **kwargs)
        return guarded

    for name in ("remove", "unlink", "replace", "rename"):
        monkeypatch.setattr(os, name, guard_mutation(getattr(os, name)))
    window = MainWindow()
    try:
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.config.config["runtime_settings"]["del_grace_period"] = 0
        if mode == "downsample":
            window.config.config["runtime_settings"]["max_full_heatmap_points"] = 1
            window.config.config["runtime_settings"]["max_heatmap_grid_cells"] = 4
            window.config.config["runtime_settings"]["max_heatmap_grid_side"] = 2
        window.close_database(status=False)
        assert window.load_file(str(database))
        wait_for(lambda: not window._database_load_active and not window._database_detail_active)
        if mode == "line":
            window.openPlot(guid=guids[0])
        else:
            window.open_preview_plot("signal_a")
        wait_for(lambda: bool(window.windows))
        plot = window.windows[-1]
        wait_for(lambda: hasattr(plot, "axis_data") and not plot.worker.running)
        plot.monitor.stop()
        yield window, plot, qapplication, guids
    finally:
        close_main_window(window)
        _assert_reader_artifact_invariant(database, original)


@pytest.mark.parametrize("dialog_plot,editor", [
    ("heatmap", "appearance"),
    ("heatmap", "color_scale"),
    ("heatmap", "axis_scale"),
    ("line", "trace_appearance"),
], indirect=["dialog_plot"])
def test_plot_close_closes_owned_editor(dialog_plot, editor):
    window, plot, app, _guids = dialog_plot
    if editor == "appearance":
        plot.heatmap_appearance_action.trigger()
        dialog = plot._heatmap_appearance_dialog
    elif editor == "color_scale":
        plot.colorbar_scale_action.trigger()
        dialog = plot.colorbar_scale_dialog
    elif editor == "trace_appearance":
        plot.open_trace_appearance_dialog()
        dialog = plot._trace_appearance_dialog
    else:
        plot.open_axis_scale_dialog("Bottom")
        dialog = plot._axis_scale_dialog
    assert dialog.isVisible()
    plot.close()
    app.processEvents()
    assert window.windows == []
    assert not plot.isVisible()
    assert not dialog.isVisible()


@pytest.mark.parametrize("dialog_plot", ["downsample"], indirect=True)
def test_plot_close_rejects_active_downsample_dialog(dialog_plot):
    window, plot, app, _guids = dialog_plot
    assert plot._heatmap_downsample_info is not None
    observations = []

    def close_plot_from_active_dialog():
        dialog = next(
            dialog for dialog in plot.findChildren(qtw.QDialog)
            if dialog.windowTitle() == "Warning: Heatmap downsampled"
        )
        assert dialog.isVisible()
        plot.close()
        observations.append((dialog.isVisible(), dialog.result()))
        dialog.reject()  # Keep a broken close from blocking the test event loop.

    QtCore.QTimer.singleShot(0, close_plot_from_active_dialog)
    plot.heatmap_downsample_button.click()
    app.processEvents()
    assert window.windows == []
    assert observations == [(False, qtw.QDialog.DialogCode.Rejected)]


def test_cut_close_closes_axis_scale_editor(dialog_plot):
    window, heatmap, app, _guids = dialog_plot
    heatmap.z_index = [0, 0]
    heatmap.openSweep("h")
    cut = window.windows[-1]
    wait_for(lambda: hasattr(cut, "axis_data") and not cut.worker.running)
    cut.monitor.stop()
    cut.open_axis_scale_dialog("Bottom")
    dialog = cut._axis_scale_dialog
    assert dialog.isVisible()
    cut.close()
    app.processEvents()
    assert window.windows == [heatmap]
    assert not dialog.isVisible()


@pytest.mark.parametrize("close_action", ["database", "application"])
def test_database_or_application_close_closes_plot_editors(dialog_plot, close_action):
    window, plot, app, _guids = dialog_plot
    plot.heatmap_appearance_action.trigger()
    plot.colorbar_scale_action.trigger()
    plot.open_axis_scale_dialog("Bottom")
    dialogs = [plot._heatmap_appearance_dialog, plot.colorbar_scale_dialog, plot._axis_scale_dialog]
    assert all(dialog.isVisible() for dialog in dialogs)
    if close_action == "database":
        window.closeDatabaseAction.trigger()
    else:
        window.quit_application()
        wait_for(lambda: window._shutdown_ready)
    app.processEvents()
    assert window.windows == []
    assert all(not dialog.isVisible() for dialog in dialogs)


@pytest.mark.parametrize("dialog_plot", ["line"], indirect=True)
def test_closing_retained_source_closes_editors_and_preserves_refresh(dialog_plot, monkeypatch):
    window, host, app, guids = dialog_plot
    window.openPlot(guid=guids[1])
    wait_for(lambda: len(window.windows) == 2)
    source = window.windows[-1]
    wait_for(lambda: hasattr(source, "axis_data") and not source.worker.running)
    source.monitor.stop()
    assert window.add_trace_to_plot(host, source._dataset_key, source.param.name, param=source.param)
    assert source._merged_trace_users == 1
    source.open_trace_appearance_dialog()
    dialog = source._trace_appearance_dialog
    cancelled = []
    original_cancel = source.worker.cancel

    def record_cancel():
        cancelled.append(True)
        original_cancel()

    monkeypatch.setattr(source.worker, "cancel", record_cancel)
    source.refreshWindow(force=True)
    source.close()
    app.processEvents()
    assert not dialog.isVisible()
    assert window.windows == [host]
    assert source._can_process_refresh()
    assert source.widget.scene() is not None
    assert cancelled == []
    wait_for(lambda: not source.worker.running)
    assert len(host.lines) == 2
    np.testing.assert_array_equal(source.axis_data["y"], np.arange(4.) * 3)
