"""Window-menu commands cannot revive plots whose data ownership has ended."""

import os
from pathlib import Path

import numpy as np
import pytest
from PyQt6 import QtCore, QtTest, sip
from PyQt6 import QtWidgets as qtw

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


def _window_menu(owner):
    return next(action.menu() for action in owner.menuBar().actions()
                if action.text().replace("&", "") == "Window")


def _target_action(menu, target, route):
    if route == "focus":
        return next(action for action in menu.actions()
                    if action.property("qplotWindowListEntry")
                    and target.label in action.text()
                    and ("Cut" in action.text()) == (target.operation_kind == "sweeper"))
    title = {"minimize": "Minimize", "maximize": "Maximize", "fullscreen": "Full Screen"}[route]
    return next(action for action in menu.actions()
                if title.casefold() in action.text().replace("&", "").casefold())


def _choose(menu, action, keyboard):
    if keyboard:
        menu.setActiveAction(action)
        QtTest.QTest.keyClick(menu, QtCore.Qt.Key.Key_Return)
    else:
        QtTest.QTest.mouseClick(menu, QtCore.Qt.MouseButton.LeftButton,
                               pos=menu.actionGeometry(action).center())


def _popup(owner, callback):
    menu = _window_menu(owner)
    state = {}
    poll, watchdog = QtCore.QTimer(), QtCore.QTimer()
    poll.setInterval(10)
    watchdog.setSingleShot(True)

    def progress():
        try:
            callback(menu, state)
        except Exception as error:
            state["error"] = error
            poll.stop()
            menu.close()

    def abort():
        state["timed_out"] = True
        menu.close()

    poll.timeout.connect(progress)
    watchdog.timeout.connect(abort)
    poll.start()
    watchdog.start(15_000)
    try:
        menu.exec(owner.mapToGlobal(QtCore.QPoint(20, 20)))
    finally:
        poll.stop()
        watchdog.stop()
    assert not state.get("timed_out")
    if "error" in state:
        raise state["error"]
    return menu, state


@pytest.mark.skipif(os.name == "nt", reason="Windows locks an open SQLite main file against replacement.")
@pytest.mark.parametrize("target_kind", ["heatmap", "cut"])
@pytest.mark.parametrize("route", ["focus", "minimize", "maximize", "fullscreen"])
@pytest.mark.parametrize("keyboard", [False, True], ids=["mouse", "keyboard"])
def test_automatic_replacement_retires_window_menu_commands(
    color_cut, tmp_path, target_kind, route, keyboard,
):
    main, heatmap, cut, protected = color_cut
    wait_for(lambda: hasattr(cut, "axis_data") and not cut.worker.running)
    target = heatmap if target_kind == "heatmap" else cut
    owner = main if route == "focus" else target
    source = Path(heatmap._dataset_key.database_path)
    replacement = tmp_path / "window-menu-replacement.db"
    _run_id, new_guid, _a, _b = _build_two_heatmap_database(replacement)
    replacement_state = _database_artifact_state(replacement)
    retired = tmp_path / "window-menu-retired.db"
    main.spinBox.setValue(.1)
    assert main.monitor.isActive()

    def replace(menu, state):
        if "action" not in state:
            state["action"] = _target_action(menu, target, route)
            # Only the independent fixture writer replaces its synthetic files.
            # qPlot notices through the actual automatic refresh timer.
            source.rename(retired)
            protected[retired] = protected.pop(source)
            replacement.rename(source)
            protected[source] = replacement_state
        elif main._selected_run_guid == new_guid and not main._database_load_active:
            assert target._closed and target not in main.windows
            assert not target.isVisible()
            _choose(menu, state["action"], keyboard)

    menu, state = _popup(owner, replace)
    wait_for(lambda: not main._database_load_active and main._selected_run_guid == new_guid)
    if owner is target:
        assert not menu.isVisible()
        # An action dispatched just before owner retirement is harmless too.
        state["action"].trigger()
    assert target._closed and target not in main.windows
    assert not target.isVisible()
    assert not target.monitor.isActive()


@pytest.mark.parametrize("route", ["focus", "minimize", "maximize", "fullscreen"])
def test_public_close_database_invalidates_window_commands(color_cut, route):
    main, heatmap, cut, _protected = color_cut
    wait_for(lambda: hasattr(cut, "axis_data") and not cut.worker.running)
    owner = main if route == "focus" else cut

    def close(menu, state):
        action = _target_action(menu, cut, route)
        main.close_current_database()
        state["owner_popup_visible"] = menu.isVisible()
        action.trigger()
        menu.close()

    _menu, state = _popup(owner, close)
    assert not main.windows and not cut.isVisible() and not heatmap.isVisible()
    if owner is cut:
        assert not state["owner_popup_visible"]
    assert cut._closed


@pytest.mark.parametrize("route", ["focus", "minimize", "maximize", "fullscreen"])
def test_active_window_commands_keep_measurements_and_allow_later_plots(color_cut, route):
    main, heatmap, cut, _protected = color_cut
    wait_for(lambda: hasattr(cut, "axis_data") and not cut.worker.running)
    original = tuple(values.copy() for values in cut.line.getOriginalDataset())
    owner = main if route == "focus" else cut
    if route == "focus":
        cut.showMinimized()

    def choose(menu, _state):
        _choose(menu, _target_action(menu, cut, route), keyboard=True)

    _popup(owner, choose)
    assert cut in main.windows and not cut._closed
    assert cut.isVisible()
    if route == "focus":
        assert not cut.isMinimized()
    elif route == "minimize":
        assert cut.isMinimized()
    elif route == "maximize":
        assert cut.isMaximized()
    else:
        assert cut.isFullScreen()
    cut.showNormal()
    for actual, expected in zip(cut.line.getOriginalDataset(), original, strict=True):
        np.testing.assert_array_equal(actual, expected)

    # A fresh plot with the same source remains a normal Window-menu target.
    heatmap.z_index = [0, 0]
    heatmap.openSweep("h")
    later = main.windows[-1]
    wait_for(lambda: hasattr(later, "axis_data") and not later.worker.running)
    later.showMinimized()
    _popup(main, lambda menu, state: _choose(menu, _target_action(menu, later, "focus"), False))
    assert later.isVisible() and not later.isMinimized() and not later._closed


@pytest.mark.parametrize("route", ["focus", "minimize", "maximize", "fullscreen"])
def test_deleted_window_target_ignores_an_already_dispatched_action(color_cut, route):
    main, _heatmap, cut, _protected = color_cut
    wait_for(lambda: hasattr(cut, "axis_data") and not cut.worker.running)
    owner = main if route == "focus" else cut
    menu = _window_menu(owner)
    menu.aboutToShow.emit()
    action = _target_action(menu, cut, route)
    cut.close()
    cut.deleteLater()
    qtw.QApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    assert sip.isdeleted(cut)
    # The main-owned Window list can retain an action after its target dies.
    # Plot-owned actions are automatically deleted with their Qt owner.
    if not sip.isdeleted(action):
        action.trigger()


@pytest.mark.parametrize("keyboard", [False, True], ids=["mouse", "keyboard"])
def test_closed_retained_source_stays_hidden_while_its_overlay_remains(
    merged_plots, keyboard,
):
    main, (host, source, _other), x, y = merged_plots
    _key, line = merge(main, host, source, x_axis="Bottom", y_axis="Left")
    assert source._merged_trace_users > 0

    def close_source(menu, _state):
        action = _target_action(menu, source, "focus")
        source.close()
        assert source._merged_trace_users > 0 and not source.isVisible()
        _choose(menu, action, keyboard)

    _popup(main, close_source)
    assert source._closed and source not in main.windows and not source.isVisible()
    assert host in main.windows and host.isVisible()
    np.testing.assert_array_equal(line.getOriginalDataset(), [x, y])
