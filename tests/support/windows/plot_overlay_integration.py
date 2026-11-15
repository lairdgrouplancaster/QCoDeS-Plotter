"""Shared fixtures and assertions for plot overlay integration."""

import pytest
from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw
from PyQt6.QtTest import QTest

from qplot.windows import main as main_window
from tests._window_lifecycle import close_main_window
from tests.support.windows.plot_integration import (
    build_line_database,
    configure_temp_qplot,
    wait_for,
)

__all__ = [
    '_assign_trace_axes',
    '_drag_in_view',
    'loaded_plot',
]


@pytest.fixture
def loaded_plot(tmp_path, monkeypatch, request):
    configure_temp_qplot(monkeypatch, tmp_path)
    database_path = tmp_path / "overlays.db"
    _run_id, guid, _table = build_line_database(database_path, 64)
    merged_guid = None
    if getattr(request, "param", False):
        _run_id, merged_guid, _table = build_line_database(database_path, 48)
    protected = {
        suffix: (
            path.read_bytes(), path.stat().st_mtime_ns
        ) if (path := database_path.with_name(database_path.name + suffix)).exists()
        else None
        for suffix in ("", "-wal", "-journal")
    }
    window = main_window.MainWindow()
    try:
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.close_database(status=False)
        assert window.load_file(str(database_path))
        wait_for(lambda: not window._database_load_active)
        window.openPlot(guid=guid, show=True)
        wait_for(lambda: not getattr(window, "_trusted_plot_pending", ()))
        plot = window.windows[-1]
        wait_for(lambda: hasattr(plot, "axis_data") and not plot.worker.running)
        plot.monitor.stop()
        if merged_guid is not None:
            window.openPlot(guid=merged_guid, show=False)
            wait_for(lambda: not getattr(window, "_trusted_plot_pending", ()))
            source = window.windows[-1]
            wait_for(lambda: hasattr(source, "axis_data") and not source.worker.running)
            source.monitor.stop()
            assert window.add_trace_to_plot(
                plot, source._dataset_key, source.param.name, param=source.param,
            )
            wait_for(lambda: len(plot.lines) == 2)
        yield plot
    finally:
        close_main_window(window)
        for suffix, original in protected.items():
            path = database_path.with_name(database_path.name + suffix)
            current = (path.read_bytes(), path.stat().st_mtime_ns) if path.exists() else None
            assert current == original


def _drag_in_view(window, start, end, modifiers, viewbox=None, *, button=QtCore.Qt.MouseButton.LeftButton):
    viewbox = window.vb if viewbox is None else viewbox
    viewport = window.widget.viewport()
    start = window.widget.mapFromScene(viewbox.mapViewToScene(start))
    end = window.widget.mapFromScene(viewbox.mapViewToScene(end))
    QTest.mousePress(viewport, button, modifiers, start)
    # Supply pressed-button state explicitly; offscreen QTest.mouseMove does
    # not retain it on every Qt platform.
    midpoint = QtCore.QPoint((start.x() + end.x()) // 2, (start.y() + end.y()) // 2)
    for position in (midpoint, end):
        event = QtGui.QMouseEvent(
            QtCore.QEvent.Type.MouseMove,
            QtCore.QPointF(position), QtCore.QPointF(viewport.mapToGlobal(position)),
            QtCore.Qt.MouseButton.NoButton, button, modifiers,
        )
        qtw.QApplication.sendEvent(viewport, event)
    QTest.mouseRelease(viewport, button, modifiers, end)
    qtw.QApplication.processEvents()


def _assign_trace_axes(window, key, x_axis, y_axis):
    style = window._trace_styles[key]
    style.x_axis, style.y_axis = x_axis, y_axis
    window._apply_trace_style(key, window.lines[key])
    qtw.QApplication.processEvents()
    return window._trace_axis_viewbox(style)
