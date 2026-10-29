"""Native plot popups cannot export a source after its ownership has ended."""

import os
from pathlib import Path

import numpy as np
import pytest
from PyQt6 import QtCore, QtTest
from PyQt6 import QtWidgets as qtw
from pyqtgraph.exporters import ImageExporter

from tests.windows.test_custom_color_dialog_lifecycle import color_cut as _color_cut
from tests.windows.test_heatmap_layer_integration import (
    _build_two_heatmap_database,
    _database_artifact_state,
)
from tests.windows.test_merged_trace_metadata import merge
from tests.windows.test_merged_trace_metadata import merged_plots as _merged_plots
from tests.windows.test_plot_integration import wait_for

color_cut = _color_cut
merged_plots = _merged_plots


def _native_menu(plot, level):
    """Use the viewport and Qt keyboard navigation, including native widgets."""
    plot.raise_()
    qtw.QApplication.processEvents()
    rect = plot.vb.sceneBoundingRect()
    scene_pos = QtCore.QPointF(rect.left() + rect.width() * .27,
                              rect.top() + rect.height() * .31)
    point = plot.widget.mapFromScene(scene_pos)
    QtTest.QTest.mouseClick(plot.widget.viewport(), QtCore.Qt.MouseButton.RightButton,
                           pos=point)
    menu = plot.vbMenu
    wait_for(menu.isVisible)
    for title in (() if level == "root" else ("Options", level)):
        action = next(action for action in menu.actions() if action.text() == title)
        menu.setActiveAction(action)
        QtTest.QTest.keyClick(menu, QtCore.Qt.Key.Key_Right)
        menu = action.menu()
        wait_for(menu.isVisible)
    return menu


def _close_native_menus(plot):
    for root in (plot.vbMenu, plot.plot.ctrlMenu):
        for menu in root.findChildren(qtw.QMenu):
            menu.close()
        root.close()


@pytest.mark.skipif(os.name == "nt", reason="Windows locks an open SQLite main file against replacement.")
@pytest.mark.parametrize("target_kind", ["heatmap", "cut"])
@pytest.mark.parametrize("level", ["root", "Transforms", "Downsample", "Average"])
def test_automatic_replacement_closes_native_menu_levels(color_cut, tmp_path, target_kind, level):
    main, heatmap, cut, protected = color_cut
    wait_for(lambda: hasattr(cut, "axis_data") and not cut.worker.running)
    target = heatmap if target_kind == "heatmap" else cut
    menu = _native_menu(target, level)
    source = Path(heatmap._dataset_key.database_path)
    replacement = tmp_path / "native-menu-replacement.db"
    _run_id, guid, _a, _b = _build_two_heatmap_database(replacement)
    replacement_state = _database_artifact_state(replacement)
    retired = tmp_path / "native-menu-retired.db"
    try:
        # Only the external fixture writer replaces these owned files. The
        # real automatic refresh discovers and retires the old plot session.
        source.rename(retired)
        protected[retired] = protected.pop(source)
        replacement.rename(source)
        protected[source] = replacement_state
        main.spinBox.setValue(.1)
        wait_for(lambda: not main._database_load_active and main._selected_run_guid == guid)
        assert target._closed and target not in main.windows and not target.isVisible()
        assert not menu.isVisible()
        assert not target.vbMenu.isVisible()
        assert not target.plot.ctrlMenu.isVisible()

        # Queued actions must remain harmless even after the popup disappears.
        clipboard = qtw.QApplication.clipboard()
        clipboard.setText("current clipboard survives retired plot")
        target.copyPlotImageAction.trigger()
        target.savePlotPdfAction.trigger()
        target.exportPlotAction.trigger()
        assert clipboard.text() == "current clipboard survives retired plot"
        assert not any(isinstance(widget, qtw.QDialog) and widget.isVisible()
                       for widget in qtw.QApplication.topLevelWidgets())
    finally:
        _close_native_menus(target)


@pytest.mark.parametrize("level", ["root", "Transforms", "Downsample", "Average"])
def test_retained_source_closes_native_menu_without_changing_overlay(merged_plots, level):
    main, (host, source, _other), x, y = merged_plots
    _key, overlay = merge(main, host, source)
    menu = _native_menu(source, level)
    try:
        source.close()
        assert source._closed and source not in main.windows and source._merged_trace_users > 0
        assert not menu.isVisible()
        assert not source.vbMenu.isVisible()
        assert not source.plot.ctrlMenu.isVisible()
        assert host.isVisible()
        for actual, expected in zip(overlay.getData(), (x, y), strict=True):
            np.testing.assert_array_equal(actual, expected)
    finally:
        _close_native_menus(source)


@pytest.mark.parametrize("route", ["copy", "pdf", "export", "context"])
def test_delayed_native_actions_do_not_reopen_closed_plot_ui(color_cut, route):
    _main, _heatmap, cut, _protected = color_cut
    wait_for(lambda: hasattr(cut, "axis_data") and not cut.worker.running)
    callback = {"copy": cut.copy_plot_image, "pdf": cut.save_plot_pdf,
                "export": cut.open_export_dialog, "context": cut.open_context_menu}[route]
    cut.close()
    clipboard = qtw.QApplication.clipboard()
    clipboard.setText("unchanged clipboard")
    observed = []
    timer = QtCore.QTimer()

    def dismiss_stale_ui():
        for widget in qtw.QApplication.topLevelWidgets():
            if isinstance(widget, (qtw.QDialog, qtw.QMenu)) and widget.isVisible():
                observed.append(type(widget).__name__)
                widget.close()

    timer.timeout.connect(dismiss_stale_ui)
    timer.start(20)
    try:
        callback()
        qtw.QApplication.processEvents()
        dismiss_stale_ui()
    finally:
        timer.stop()
    assert not observed
    assert clipboard.text() == "unchanged clipboard"
    assert not cut.isVisible()


def test_current_native_menu_copy_and_escape_remain_usable(color_cut):
    _main, _heatmap, cut, _protected = color_cut
    wait_for(lambda: hasattr(cut, "axis_data") and not cut.worker.running)
    menu = _native_menu(cut, "root")
    action = cut.copyPlotImageAction
    QtTest.QTest.mouseClick(menu, QtCore.Qt.MouseButton.LeftButton,
                           pos=menu.actionGeometry(action).center())
    assert not qtw.QApplication.clipboard().image().isNull()
    assert not menu.isVisible()
    assert cut.isVisible() and not cut._closed
    for level in ("Transforms", "Downsample", "Average"):
        submenu = _native_menu(cut, level)
        QtTest.QTest.keyClick(submenu, QtCore.Qt.Key.Key_Escape)
        assert not submenu.isVisible()
        _close_native_menus(cut)


@pytest.mark.parametrize("route", ["file", "copy"])
def test_cached_exporter_cannot_publish_after_owner_close(color_cut, tmp_path, monkeypatch, route):
    _main, _heatmap, cut, _protected = color_cut
    wait_for(lambda: hasattr(cut, "axis_data") and not cut.worker.running)
    exporter = ImageExporter(cut.plot)
    cut.close()
    clipboard = qtw.QApplication.clipboard()
    clipboard.setText("keep current clipboard")
    destination = tmp_path / "retired-plot.png"
    choices = []

    def select_path(*_args, **_kwargs):
        choices.append(destination)
        return str(destination), "PNG files (*.png)"

    monkeypatch.setattr(qtw.QFileDialog, "getSaveFileName", select_path)
    callback = cut._export_pyqtgraph_exporter if route == "file" else cut._copy_pyqtgraph_exporter
    assert callback(exporter) is False
    assert not choices and not destination.exists()
    assert clipboard.text() == "keep current clipboard"
