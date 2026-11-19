"""Commit typed run selection before asynchronous keyboard plot requests."""


import numpy as np
import pytest
from PyQt6 import QtCore

from tests.support.windows.plot_integration import wait_for
from tests.support.windows.typed_run_keyboard_launch import (
    _drain as _drain,
)
from tests.support.windows.typed_run_keyboard_launch import (
    _launch as _launch,
)
from tests.support.windows.typed_run_keyboard_launch import (
    _type_run as _type_run,
)
from tests.support.windows.typed_run_keyboard_launch import (
    typed_run_window as typed_run_window,
)


@pytest.mark.parametrize("route", ["button", "Key_Return"])
def test_plot_action_preserves_selected_run_metadata(typed_run_window, route):
    window, runs = typed_run_window
    bridge = window._trusted_derived_bridge
    tree = window.infoBox.metadata
    window.infoBox.setCurrentWidget(tree)
    window.measurementBox.setText("2")

    def displayed_calibration():
        items = tree.findItems("calibration", QtCore.Qt.MatchFlag.MatchRecursive)
        return [item.text(1) for item in items]

    def check_selected_run(run_id, guid, offset):
        window.RunList.setCurrentItem(window.RunList._item_for_guid(guid))
        wait_for(lambda: bridge._detail_display_guid == guid
                 and not bridge.coordinator.active
                 and not bridge.coordinator.snapshot().pending_count
                 and not bridge.background_active())
        expected = [f"Reference offset {offset}"]
        assert displayed_calibration() == expected

        window.run_idBox.setFocus()
        _launch(window, route)
        _drain(window)
        assert [plot.ds.guid for plot in window.windows] == [guid]
        assert displayed_calibration() == expected
        assert window.infoBox._trusted_detail_identity == (run_id, guid)
        window.windows[0].close()
        wait_for(lambda: not window.windows)
        assert displayed_calibration() == expected

    for run in reversed(runs):
        check_selected_run(*run)


@pytest.mark.parametrize("route", ["Key_Return", "Key_Enter", "shortcut", "button"])
@pytest.mark.parametrize("same_run", [False, True], ids=["different-run", "same-run"])
def test_typed_run_launch_survives_edit_completion(typed_run_window, route, same_run):
    window, runs = typed_run_window
    run_id, guid, offset = runs[-1 if same_run else 0]
    _type_run(window, run_id)
    _launch(window, route)
    _drain(window)
    assert len(window.windows) == 1
    plot = window.windows[0]
    plot.monitor.stop()
    assert plot.ds.guid == guid
    assert plot.param.name == "reference"
    np.testing.assert_array_equal(plot.axis_data["x"], [-3, -1, 2, 4, 9])
    np.testing.assert_array_equal(plot.axis_data["y"],
                                  offset + np.array([9, 1, 4, 16, 81]))
    assert window.RunList.currentItem().guid == guid


@pytest.mark.parametrize("change", ["selection", "typing", "close-all", "close-database"])
def test_typed_run_pending_launch_still_obeys_lifecycle(typed_run_window, change):
    window, runs = typed_run_window
    _type_run(window, runs[0][0])
    _launch(window, "Key_Return")
    assert window._trusted_plot_pending
    if change == "selection":
        window.RunList.setCurrentItem(window.RunList._item_for_guid(runs[1][1]))
    elif change == "typing":
        _type_run(window, runs[1][0])
    elif change == "close-all":
        window.closeAllPlotsButton.click()
    else:
        window.close_current_database()
    _drain(window)
    assert not window.windows
