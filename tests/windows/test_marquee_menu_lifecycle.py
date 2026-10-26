"""A live marquee popup cannot outlast the plot whose data it targets."""

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
from tests.windows.test_typed_run_keyboard_launch import (
    typed_run_window as _typed_run_window,
)

color_cut = _color_cut
typed_run_window = _typed_run_window
merged_plots = _merged_plots


@pytest.fixture
def menu_plot(request):
    kind = getattr(request, "param", "line")
    if kind == "line":
        main, runs = request.getfixturevalue("typed_run_window")
        main.open_run_preview_plot(runs[0][1], "signal")
        wait_for(lambda: len(main.windows) == 1)
        plot = main.windows[0]
    else:
        main, heatmap, cut, _protected = request.getfixturevalue("color_cut")
        plot = heatmap if kind == "heatmap" else cut
    wait_for(lambda: hasattr(plot, "axis_data") and not plot.worker.running)
    plot.monitor.stop()
    yield main, plot
    # Also retire the erroneous post-close dialog on a red regression run.
    if not sip.isdeleted(plot):
        for dialog in plot.findChildren(qtw.QDialog):
            dialog.close()


def _marquee_popup(plot, callback):
    """Exercise the real nested popup loop with bounded cleanup."""
    plot.set_marquee_rect(QtCore.QRectF(-1000., -1000., 2000., 2000.))
    state = {"rect": QtCore.QRectF(plot.marquee)}
    timer = QtCore.QTimer()
    watchdog = QtCore.QTimer()
    timer.setInterval(10)
    watchdog.setSingleShot(True)

    def poll():
        menu = qtw.QApplication.activePopupWidget()
        if not isinstance(menu, qtw.QMenu):
            return
        state["menu"] = menu
        try:
            callback(menu, state)
        except Exception as error:
            state["error"] = error
            timer.stop()
            menu.close()

    def abort():
        state["timed_out"] = True
        menu = qtw.QApplication.activePopupWidget()
        if menu is not None:
            menu.close()

    timer.timeout.connect(poll)
    watchdog.timeout.connect(abort)
    timer.start()
    watchdog.start(15_000)
    try:
        scene = plot._marquee_viewbox().mapViewToScene(QtCore.QPointF(0., 0.))
        assert plot.open_marquee_context_menu(scene, plot.mapToGlobal(QtCore.QPoint(100, 100)))
    finally:
        timer.stop()
        watchdog.stop()
    assert not state.get("timed_out")
    if "error" in state:
        raise state["error"]
    assert "menu" in state
    return state


def _choose(menu, text, keyboard=False):
    action = next(action for action in menu.actions() if action.text() == text)
    assert action.isEnabled()
    if keyboard:
        menu.setActiveAction(action)
        QtTest.QTest.keyClick(menu, QtCore.Qt.Key.Key_Return)
    else:
        QtTest.QTest.mouseClick(menu, QtCore.Qt.MouseButton.LeftButton,
                               pos=menu.actionGeometry(action).center())


def _retired(menu):
    qtw.QApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    qtw.QApplication.processEvents()
    return sip.isdeleted(menu)


@pytest.mark.parametrize("menu_plot", ["line", "heatmap", "cut"], indirect=True)
@pytest.mark.parametrize("close_all", [False, True], ids=["close-owner", "close-all"])
def test_closing_plot_dismisses_live_marquee_popup(menu_plot, close_all):
    main, plot = menu_plot

    def close(menu, state):
        (main.closeAllPlotsButton.click if close_all else plot.close)()
        state["visible_after_close"] = menu.isVisible()
        if menu.isVisible():
            _choose(menu, "Zoom X")

    state = _marquee_popup(plot, close)
    assert plot._closed and plot not in main.windows
    assert not state["visible_after_close"]
    assert getattr(plot, "_marquee_stats_dialog", None) is None
    assert _retired(state["menu"])


@pytest.mark.parametrize("keyboard", [False, True], ids=["mouse", "keyboard"])
@pytest.mark.parametrize("action", ["Stats...", "Zoom X", "dismiss"])
def test_live_menu_accept_and_dismiss_retire_popup(menu_plot, keyboard, action):
    _main, plot = menu_plot
    before = tuple(values.copy() for values in plot.line.getOriginalDataset())

    def choose(menu, _state):
        if action == "dismiss":
            QtTest.QTest.keyClick(menu, QtCore.Qt.Key.Key_Escape)
        else:
            _choose(menu, action, keyboard)

    state = _marquee_popup(plot, choose)
    assert not plot._closed and plot.isVisible()
    for actual, expected in zip(plot.line.getOriginalDataset(), before, strict=True):
        np.testing.assert_array_equal(actual, expected)
    if action == "Stats...":
        assert plot._marquee_stats_dialog.isVisible()
        plot._marquee_stats_dialog.close()
    elif action == "Zoom X":
        assert plot._marquee_zoom_viewbox("x").viewRange()[0] == pytest.approx(
            [state["rect"].left(), state["rect"].right()])
    assert (plot.marquee is not None) == (action == "dismiss")



def test_queued_marquee_action_cannot_open_dialog_after_close(menu_plot):
    _main, plot = menu_plot
    plot.set_marquee_rect(QtCore.QRectF(-1000., -1000., 2000., 2000.))
    menu = plot._new_marquee_context_menu()
    try:
        action = next(action for action in menu.actions() if action.text() == "Stats...")
        plot.close()
        action.trigger()
        assert getattr(plot, "_marquee_stats_dialog", None) is None
    finally:
        menu.deleteLater()


def test_closing_retained_source_dismisses_menu_without_discarding_overlay(merged_plots):
    main, (host, source, _other), x, y = merged_plots
    state = _marquee_popup(source, lambda menu, state: (
        merge(main, host, source),
        source.close(),
        state.update(visible_after_close=menu.isVisible()),
        menu.close(),
    ))
    assert source._closed and source._merged_trace_users == 1
    assert not state["visible_after_close"]
    for line in host.lines.values():
        np.testing.assert_array_equal(line.getOriginalDataset(), [x, y])
    assert _retired(state["menu"])


@pytest.mark.skipif(os.name == "nt", reason="Windows locks an open SQLite main file against replacement.")
def test_automatic_replacement_closes_marquee_popup(color_cut, tmp_path):
    main, heatmap, cut, protected = color_cut
    wait_for(lambda: hasattr(cut, "axis_data") and not cut.worker.running)
    source = Path(heatmap._dataset_key.database_path)
    replacement = tmp_path / "replacement.db"
    _build_two_heatmap_database(replacement)
    replacement_state = _database_artifact_state(replacement)
    retired = tmp_path / "retired.db"
    main.spinBox.setValue(.1)
    assert main.monitor.isActive()

    def replace(menu, state):
        if not state.get("replaced"):
            # Only this external fixture writer redirects its owned files.
            source.rename(retired)
            protected[retired] = protected.pop(source)
            replacement.rename(source)
            protected[source] = replacement_state
            state["replaced"] = True
        elif heatmap._closed:
            state["visible_after_close"] = menu.isVisible()
            _choose(menu, "Stats...", keyboard=True)

    state = _marquee_popup(heatmap, replace)
    wait_for(lambda: not main.windows and not main._database_load_active)
    assert state["replaced"]
    assert not state.get("visible_after_close", False)
    assert getattr(heatmap, "_marquee_stats_dialog", None) is None
    assert _retired(state["menu"])


def test_cancelled_close_all_keeps_plot_and_later_menu_usable(menu_plot):
    main, plot = menu_plot
    main.config.config["user_preference"]["confirm_close_all"] = True
    responder = QtCore.QTimer()
    responder.setInterval(10)
    answered = []

    def reject_confirmation():
        box = qtw.QApplication.activeModalWidget()
        if isinstance(box, qtw.QMessageBox):
            answered.append(box.text())
            responder.stop()
            QtTest.QTest.mouseClick(box.button(qtw.QMessageBox.StandardButton.No),
                                   QtCore.Qt.MouseButton.LeftButton)

    def request_close(menu, state):
        if state.get("requested"):
            return
        state["requested"] = True
        responder.start()
        main.closeAllPlotsButton.click()
        state["still_live"] = not plot._closed and plot in main.windows
        menu.close()

    responder.timeout.connect(reject_confirmation)
    try:
        state = _marquee_popup(plot, request_close)
    finally:
        responder.stop()
        main.config.config["user_preference"]["confirm_close_all"] = False
    assert answered == ["Close 1 plot window?"]
    assert state["still_live"]
    next_state = _marquee_popup(plot, lambda menu, _state: _choose(menu, "Stats...", True))
    assert plot._marquee_stats_dialog.isVisible()
    plot._marquee_stats_dialog.close()
    assert _retired(state["menu"])
    assert _retired(next_state["menu"])
