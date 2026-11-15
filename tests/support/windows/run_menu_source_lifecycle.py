"""Shared fixtures and assertions for run menu source lifecycle."""

from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

__all__ = [
    '_run_menu',
]


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
