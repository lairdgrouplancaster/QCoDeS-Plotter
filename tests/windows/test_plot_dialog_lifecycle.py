"""Closing real QCoDeS plots also closes their owned editors and panels."""

from types import SimpleNamespace

import numpy as np
import pytest
from PyQt6 import QtCore, QtTest, sip
from PyQt6 import QtWidgets as qtw

from qplot.datahandling.plot_progress import PlotProgress
from qplot.windows._widgets.treeWidgets import RUN_TABLE_COLUMN_WIDTHS_KEY
from tests.support.windows.plot_dialog_lifecycle import (
    dialog_plot as dialog_plot,
)
from tests.support.windows.plot_integration import wait_for


@pytest.mark.parametrize("dialog_plot", ["line"], indirect=True)
@pytest.mark.parametrize("mode", ["automatic", "manual", "unwritable"])
def test_quit_preserves_the_run_table_width_choice(dialog_plot, monkeypatch, mode):
    window, _plot, _app, _guids = dialog_plot
    table = window.RunList
    column = table.cols.index("ID")
    assert window.config.get(RUN_TABLE_COLUMN_WIDTHS_KEY) == []
    attempts = []
    modal_errors = []
    update = window.config.update

    def record_update(key, value):
        if key == RUN_TABLE_COLUMN_WIDTHS_KEY:
            attempts.append(value[column])
            if mode == "unwritable":
                raise PermissionError("Read-only test settings")
        return update(key, value)

    monkeypatch.setattr(window.config, "update", record_update)
    monkeypatch.setattr(qtw.QMessageBox, "exec", lambda box: modal_errors.append(box.windowTitle()) or 0)
    width = table.columnWidth(column) + 53
    if mode != "automatic":
        # Keep the edit pending through shutdown even on slower CI workers.
        table._column_width_save_timer.setInterval(60_000)
        table.header().resizeSection(column, width)
        assert table.columnWidth(column) == width
        assert table._column_width_save_timer.isActive()
    window.quit_application()
    wait_for(lambda: window._shutdown_ready)
    saved = type(window.config)().get(RUN_TABLE_COLUMN_WIDTHS_KEY)
    assert attempts == ([] if mode == "automatic" else [width])
    assert not modal_errors
    if mode == "manual":
        assert saved and saved[column] == width
    else:
        assert saved == []


@pytest.mark.parametrize("dialog_plot", ["line"], indirect=True)
def test_hidden_source_deletion_releases_canvas_and_progress_timer(dialog_plot):
    window, _plot, app, guids = dialog_plot
    count = len(window.windows)
    window.openPlot(guid=guids[1], show=False)
    wait_for(lambda: len(window.windows) > count)
    source = window.windows[-1]
    wait_for(lambda: hasattr(source, "axis_data") and not source.worker.running)
    source.monitor.stop()
    assert not source.visible
    overlay = source.plot_state_overlay
    overlay.track(SimpleNamespace(
        progress=PlotProgress("Reading hidden source", 1, 2),
        is_cancelled=lambda: False,
    ))
    assert overlay.progress_timer.isActive()
    owned = (source, source.widget, overlay, overlay.progress_timer,
             source.plot_status, overlay.elapsed_label)
    source.close()
    source.deleteLater()
    app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()
    assert all(sip.isdeleted(item) for item in owned)


@pytest.mark.parametrize("dialog_plot,editor", [
    ("heatmap", "appearance"),
    ("heatmap", "color_scale"),
    ("heatmap", "axis_scale"),
    ("line", "trace_appearance"),
], indirect=["dialog_plot"])
def test_plot_close_closes_owned_editor(dialog_plot, editor):
    window, plot, app, _guids = dialog_plot
    if editor == "appearance":
        plot.appearance_action.trigger()
        dialog = plot._plot_appearance_dialog
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
    assert sip.isdeleted(plot) or not plot.isVisible()
    assert sip.isdeleted(dialog) or not dialog.isVisible()


def _detach_panel(panel, app):
    panel.window().raise_()
    panel.window().activateWindow()
    panel.show()
    app.processEvents()
    if isinstance(panel, qtw.QDockWidget):
        panel.setFloating(True)
    else:
        # QToolBar has no setFloating API: exercise the actual handle drag.
        option = qtw.QStyleOptionToolBar()
        panel.initStyleOption(option)
        start = panel.style().subElementRect(
            qtw.QStyle.SubElement.SE_ToolBarHandle, option, panel,
        ).center()
        direction = (
            -1 if panel.parentWidget().toolBarArea(panel)
            == QtCore.Qt.ToolBarArea.BottomToolBarArea else 1
        )
        QtTest.QTest.mouseMove(panel, start)
        QtTest.QTest.mousePress(panel, QtCore.Qt.MouseButton.LeftButton, pos=start)
        QtTest.QTest.mouseMove(panel, start + QtCore.QPoint(20, direction * 50), delay=30)
        QtTest.QTest.mouseMove(panel, start + QtCore.QPoint(30, direction * 150), delay=30)
        QtTest.QTest.mouseRelease(
            panel, QtCore.Qt.MouseButton.LeftButton,
            pos=start + QtCore.QPoint(30, direction * 150),
        )
    app.processEvents()
    assert panel.isFloating() and panel.isVisible()


@pytest.mark.parametrize("dialog_plot", ["line", "heatmap"], indirect=True)
@pytest.mark.parametrize("panel_name", [
    "axes_dock", "oper_dock", "toolbarCo_ord",
])
def test_plot_close_hides_detached_panels(dialog_plot, panel_name):
    window, plot, app, _guids = dialog_plot
    panel = getattr(plot, panel_name)
    _detach_panel(panel, app)

    plot.close()
    app.processEvents()

    assert window.windows == []
    assert sip.isdeleted(plot) or not plot.isVisible()
    assert sip.isdeleted(panel) or not panel.isVisible()


@pytest.mark.parametrize("dialog_plot", ["downsample"], indirect=True)
def test_plot_close_rejects_active_downsample_dialog(dialog_plot):
    window, plot, app, _guids = dialog_plot
    assert plot._heatmap_resolution_report is not None
    observations = []

    def close_plot_from_active_dialog():
        dialog = next(
            dialog for dialog in plot.findChildren(qtw.QDialog)
            if dialog.windowTitle() == "Heatmap displayed at reduced resolution"
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
    plot.appearance_action.trigger()
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
    assert all(sip.isdeleted(dialog) or not dialog.isVisible() for dialog in dialogs)


@pytest.mark.parametrize("dialog_plot", ["line"], indirect=True)
@pytest.mark.parametrize("panel_name", ["oper_dock", "toolbarCo_ord"])
def test_closing_retained_source_closes_editors_and_preserves_refresh(
        dialog_plot, monkeypatch, panel_name):
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
    source_panel = getattr(source, panel_name)
    host_panel = getattr(host, panel_name)
    _detach_panel(source_panel, app)
    _detach_panel(host_panel, app)
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
    assert not source_panel.isVisible()
    assert host_panel.isVisible()
    assert not sip.isdeleted(source.oper_widget)
    assert window.windows == [host]
    assert source._can_process_refresh()
    assert source.widget.scene() is not None
    assert cancelled == []
    wait_for(lambda: not source.worker.running)
    assert len(host.lines) == 2
    np.testing.assert_array_equal(source.axis_data["y"], np.arange(4.) * 3)
