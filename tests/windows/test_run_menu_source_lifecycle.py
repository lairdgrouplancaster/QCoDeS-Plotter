"""Run popup commands cannot silently switch to another run or database."""

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
from tests.windows.test_plot_integration import wait_for
from tests.windows.test_typed_run_keyboard_launch import (
    typed_run_window as _typed_run_window,
)

color_cut = _color_cut
typed_run_window = _typed_run_window


def _run_menu(main, guid, callback):
    state = {}
    poll = QtCore.QTimer()
    poll.setInterval(10)
    watchdog = QtCore.QTimer()
    watchdog.setSingleShot(True)

    def progress():
        menu = qtw.QApplication.activePopupWidget()
        if not isinstance(menu, qtw.QMenu):
            return
        state["menu"] = menu
        try:
            callback(menu, state)
        except Exception as error:
            state["error"] = error
            poll.stop()
            menu.close()

    def abort():
        state["timed_out"] = True
        menu = qtw.QApplication.activePopupWidget()
        if menu is not None:
            menu.close()

    poll.timeout.connect(progress)
    watchdog.timeout.connect(abort)
    poll.start()
    watchdog.start(15_000)
    try:
        item = main.RunList._item_for_guid(guid)
        main.RunList.scrollToItem(item)
        main.RunList.prepareMenu(main.RunList.visualItemRect(item).center())
    finally:
        poll.stop()
        watchdog.stop()
    assert not state.get("timed_out")
    if "error" in state:
        raise state["error"]
    assert "menu" in state
    return state


def _choose(menu, parameter, keyboard):
    action = next(action for action in menu.actions()
                  if action.text() == ("&Plot all" if parameter is None else f"  - {parameter}"))
    if keyboard:
        menu.setActiveAction(action)
        QtTest.QTest.keyClick(menu, QtCore.Qt.Key.Key_Return)
    else:
        QtTest.QTest.mouseClick(menu, QtCore.Qt.MouseButton.LeftButton,
                               pos=menu.actionGeometry(action).center())


def _drain(main):
    wait_for(lambda: not getattr(main, "_trusted_plot_pending", ())
             and all(hasattr(plot, "axis_data") and not plot.worker.running
                     for plot in main.windows))


def _assert_menu_retired(menu):
    qtw.QApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    qtw.QApplication.processEvents()
    assert sip.isdeleted(menu)


@pytest.mark.parametrize("keyboard", [False, True], ids=["mouse", "keyboard"])
@pytest.mark.parametrize("parameter", [None, "reference"], ids=["all", "parameter"])
def test_run_menu_survives_ordinary_refresh(typed_run_window, keyboard, parameter):
    main, runs = typed_run_window
    guid = runs[0][1]

    def refresh(menu, state):
        if not state.get("refresh_started"):
            main.refreshDatabaseButton.click()
            state["refresh_started"] = True
        elif not main._database_refresh_active:
            state["still_visible"] = menu.isVisible()
            _choose(menu, parameter, keyboard)

    state = _run_menu(main, guid, refresh)
    _drain(main)
    assert state["still_visible"]
    assert len(main.windows) == (2 if parameter is None else 1)
    assert all(plot.ds.guid == guid for plot in main.windows)
    for plot in main.windows:
        x = np.array([-3, -1, 2, 4, 9])
        expected = runs[0][2] + (x * x if plot.param.name == "reference" else x)
        np.testing.assert_array_equal(plot.axis_data["x"], x)
        np.testing.assert_array_equal(plot.axis_data["y"], expected)


@pytest.mark.parametrize("change", ["selection", "close-database", "blocked-clear"])
@pytest.mark.parametrize("parameter", [None, "reference"], ids=["all", "parameter"])
def test_retired_run_menu_closes_and_rejects_queued_action(typed_run_window, change, parameter):
    main, runs = typed_run_window

    def retire(menu, state):
        action = next(action for action in menu.actions()
                      if action.text() == ("&Plot all" if parameter is None else "  - reference"))
        if change == "selection":
            main.RunList.setCurrentItem(main.RunList._item_for_guid(runs[1][1]))
        elif change == "close-database":
            main.close_current_database()
        else:
            # A model reset must invalidate the popup even when the widget's
            # own selection signals are blocked during source publication.
            blocker = QtCore.QSignalBlocker(main.RunList)
            main.RunList.clear()
            del blocker
        state["visible_after_retirement"] = menu.isVisible()
        action.trigger()  # An input already dispatched before popup closure.
        menu.close()

    state = _run_menu(main, runs[0][1], retire)
    _drain(main)
    assert not state["visible_after_retirement"]
    assert not main.windows
    _assert_menu_retired(state["menu"])


@pytest.mark.skipif(os.name == "nt", reason="Windows locks an open SQLite main file against replacement.")
@pytest.mark.parametrize("keyboard", [False, True], ids=["mouse", "keyboard"])
@pytest.mark.parametrize("parameter", [None, "signal_a"], ids=["all", "parameter"])
def test_replacement_cannot_retarget_open_run_menu(color_cut, tmp_path, keyboard, parameter):
    main, heatmap, _cut, protected = color_cut
    old_guid = heatmap.ds.guid
    source = Path(heatmap._dataset_key.database_path)
    replacement = tmp_path / "run-menu-replacement.db"
    _run_id, new_guid, _primary, _secondary = _build_two_heatmap_database(replacement)
    assert new_guid != old_guid
    replacement_state = _database_artifact_state(replacement)
    retired = tmp_path / "retired.db"
    main.spinBox.setValue(.1)
    assert main.monitor.isActive()

    def replace(menu, state):
        if not state.get("replaced"):
            # External fixture writer changes only its owned synthetic files.
            source.rename(retired)
            protected[retired] = protected.pop(source)
            replacement.rename(source)
            protected[source] = replacement_state
            state["replaced"] = True
        elif not main._database_load_active and main._selected_run_guid == new_guid:
            state["survived_replacement"] = menu.isVisible()
            _choose(menu, parameter, keyboard)

    state = _run_menu(main, old_guid, replace)
    wait_for(lambda: not main._database_load_active and main._selected_run_guid == new_guid)
    _drain(main)
    assert state["replaced"]
    assert not state.get("survived_replacement", False)
    assert not main.windows
    _assert_menu_retired(state["menu"])
