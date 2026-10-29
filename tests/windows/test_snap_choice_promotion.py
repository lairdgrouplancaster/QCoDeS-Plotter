"""Removing a plot's source must retain the selected cursor interaction."""

import hashlib
from pathlib import Path

import numpy as np
import pytest
from PyQt6 import QtCore, QtGui, QtTest, sip
from PyQt6 import QtWidgets as qtw

from qplot.windows._plotWin import plotWidget
from tests.windows.test_mixed_plot_drops import (
    _added,
    _drop,
    _wait_for,
)
from tests.windows.test_mixed_plot_drops import (
    mixed_plot as _mixed_plot_fixture,
)
from tests.windows.test_plot_integration import csv_rows, export_real_plot_csv


def _protected_state(database):
    result = {}
    for suffix in ("", "-wal", "-journal"):
        path = Path(str(database) + suffix)
        if not path.exists():
            result[suffix] = None
            continue
        stat = path.stat()
        result[suffix] = (
            stat.st_dev, stat.st_ino, stat.st_mode, stat.st_nlink, stat.st_size,
            stat.st_mtime_ns, stat.st_ctime_ns, hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    return result


@pytest.fixture(params=["line", "heatmap"])
def promotion_plot(request, tmp_path, monkeypatch):
    generator = _mixed_plot_fixture.__wrapped__(request, tmp_path, monkeypatch)
    main, host, runs = next(generator)
    service = main._trusted_read_service
    protected = _protected_state(tmp_path / "mixed.db")
    try:
        main.config.config["user_preference"]["confirm_incompatible_axes"] = False
        main.config.config["runtime_settings"]["del_grace_period"] = 0
        yield main, host, runs
    finally:
        plots = [
            widget for widget in qtw.QApplication.topLevelWidgets()
            if isinstance(widget, plotWidget)
            and getattr(widget, "threadPool", None) is main.threadPool
        ]
        with pytest.raises(StopIteration):
            next(generator)
        assert service.closed
        assert sip.isdeleted(main)
        assert all(sip.isdeleted(plot) for plot in plots)
        assert _protected_state(tmp_path / "mixed.db") == protected


def _assert_source_values(host):
    for line in host.lines.values():
        np.testing.assert_array_equal(line.getOriginalDataset(), [[0, 1, 2], [0, 1, 2]])
    for layer in host.heatmaps.values():
        grid = layer.dataGrid if layer is host else layer.data_grid
        np.testing.assert_array_equal(grid, [[10, 11, 12], [20, 21, 22]])


def _assert_pointer_mode(host, snapping):
    host.resize(1500, 800)
    host.show()
    # Separate trace and heatmap axes so each mode has a distinct measured
    # readout at the same viewport pixel, including after source promotion.
    host.appearance_action.trigger()
    dialog = host._plot_appearance_dialog
    for key in host.lines:
        dialog.select_trace(key)
        dialog.x_axis.setCurrentText("Top")
        dialog.y_axis.setCurrentText("Right")
    dialog.hide()
    line = next(iter(host.lines.values()))
    owner = line.getViewBox()
    owner.setRange(xRange=(-0.5, 2.5), yRange=(-0.5, 2.5), padding=0)
    if host.operation_kind == "plot2d":
        host._primary_heatmap_viewbox().setRange(
            xRange=(-0.5, 2.5), yRange=(5, 25), padding=0,
        )
    qtw.QApplication.processEvents()
    scene_point = owner.mapViewToScene(QtCore.QPointF(0.75, 1.25))
    pixel = host.widget.mapFromScene(scene_point)
    viewport = host.widget.viewport()
    event = QtGui.QMouseEvent(
        QtCore.QEvent.Type.MouseMove,
        QtCore.QPointF(pixel), QtCore.QPointF(viewport.mapToGlobal(pixel)),
        QtCore.Qt.MouseButton.NoButton, QtCore.Qt.MouseButton.NoButton,
        QtCore.Qt.KeyboardModifier.NoModifier,
    )
    qtw.QApplication.sendEvent(viewport, event)
    qtw.QApplication.processEvents()
    if snapping:
        assert float(host.pos_labels["x"].text()) == 1
        assert float(host.pos_labels["y"].text()) == 1
        assert host.pos_labels["index"].text() == "[1]"
        assert "snapped to point 1" in host.trace_label.text()
        assert host._snap_marker_view is not None
        np.testing.assert_array_equal(host.snap_marker.getData(), [[1], [1]])
    else:
        if host.operation_kind == "plot2d":
            assert float(host.pos_labels["x"].text()) == 1
            assert float(host.pos_labels["y"].text()) == 20
            assert float(host.pos_labels["z"].text()) == 21
            assert host.pos_labels["index"].text() == "[1,1]"
            assert not host.pos_labels["z"].isHidden()
        else:
            assert float(host.pos_labels["x"].text()) == pytest.approx(0.75, abs=0.02)
            assert float(host.pos_labels["y"].text()) == pytest.approx(1.25, abs=0.02)
        assert "snapped to point" not in host.trace_label.text()
        assert host._snap_marker_view is None
    assert host.snap_to_trace_action.isChecked() is snapping


@pytest.mark.parametrize("snapping", [False, True])
@pytest.mark.parametrize("remove_primary", [True, False])
def test_appearance_removal_preserves_cursor_mode(
        promotion_plot, snapping, remove_primary, monkeypatch, tmp_path):
    main, host, runs = promotion_plot
    other = "heatmap" if host.operation_kind == "plot1d" else "line"
    _drop(host, runs[other])
    _added(host, runs[other])
    _drop(host, runs["other_line"])
    overlay = _added(host, runs["other_line"])
    if not snapping:
        host.snap_to_trace_action.trigger()
    _assert_source_values(host)
    _assert_pointer_mode(host, snapping)

    host.appearance_action.trigger()
    dialog = host._plot_appearance_dialog
    if remove_primary:
        if host.operation_kind == "plot1d":
            dialog.select_trace(host.label)
        else:
            dialog.select_heatmap(host._primary_heatmap_key)
    else:
        dialog.select_trace(overlay.from_win._trace_key)
    previous = set(main.windows)
    QtTest.QTest.mouseClick(dialog.remove_item_button, QtCore.Qt.MouseButton.LeftButton)
    if remove_primary:
        _wait_for(lambda: any(window not in previous for window in main.windows))
        target = next(window for window in main.windows if window not in previous)
        expected_kind = "plot2d" if other == "heatmap" else "plot1d"
        assert target.operation_kind == expected_kind
    else:
        target = host
        assert len(target.lines) == 1
    _wait_for(lambda: not target.worker.running and not getattr(target, "_refresh_pending", False))
    target.monitor.stop()
    _assert_source_values(target)
    _assert_pointer_mode(target, snapping)

    target.refreshPlotButton.click()
    _wait_for(lambda: not target.worker.running and not getattr(target, "_refresh_pending", False))
    _assert_source_values(target)
    _assert_pointer_mode(target, snapping)
    destination = tmp_path / "remaining.csv"
    assert export_real_plot_csv(monkeypatch, target, destination)
    actual = sorted(tuple(map(float, row)) for row in csv_rows(destination)[1:])
    expected = (
        [tuple([float(x), float(x)] * len(target.lines)) for x in range(3)]
        if target.operation_kind == "plot1d"
        else sorted((float(x), float(y), float(x + y)) for y in (10, 20) for x in range(3))
    )
    assert actual == expected
