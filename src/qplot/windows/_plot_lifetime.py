"""Retire a closed Qt plot after its last consumer and refresh callback."""

from typing import Any

from PyQt6 import QtCore


def close_superseded_plot(window: Any) -> None:
    """Retire a replaced source or temporary preview during internal cleanup."""
    from ._export_tasks import cancel_owner_exports

    cancel_owner_exports(window)
    window.close()


def retire_closed_plot(window: Any) -> None:
    """Queue native deletion; retained overlay sources stay available."""
    if not isinstance(window, QtCore.QObject):
        return
    state = window.__dict__
    if (not state.get("_closed", False) or state.get("_merged_trace_users", 0) > 0
            or state.get("_plot_retirement_timer") is not None):
        return
    timer = QtCore.QTimer(window)
    timer.setInterval(25)
    state["_plot_retirement_timer"] = timer

    def retire() -> None:
        if not state.get("_closed", False) or state.get("_merged_trace_users", 0) > 0:
            timer.stop()
            state.pop("_plot_retirement_timer", None)
            timer.deleteLater()
            return
        controller = state.get("_plot_refresh_controller")
        if controller is not None:
            session = controller.session
            if session is not None and (session.starting or session.publishing):
                return
            if getattr(controller.host, "has_pending_workers", False):
                return
        timer.stop()
        window.deleteLater()

    timer.timeout.connect(retire)
    timer.start()
