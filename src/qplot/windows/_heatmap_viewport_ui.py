"""Qt timer and window access for heatmap viewport reloads."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from PyQt6 import QtCore

from ._heatmap_viewport import RELOAD_DEBOUNCE_MS, AxisRanges, ViewportRequest

if TYPE_CHECKING:
    from .plot2d import plot2d


class HeatmapViewportUi:
    def __init__(self, window: plot2d) -> None:
        self.window = window
        self.timer = QtCore.QTimer(window)
        self.timer.setSingleShot(True)

    def available(self) -> bool:
        return self.window._can_process_refresh()

    def busy(self) -> bool:
        return self.window._refresh_controller().busy

    def visible_ranges(self) -> AxisRanges | None:
        return AxisRanges.normalise(self.window._visible_heatmap_axis_ranges())

    def stop_timer(self) -> None:
        self.timer.stop()

    def schedule(self, callback: Callable[[], None]) -> None:
        self.timer.stop()
        self.timer.deleteLater()
        # Each timer retains its own fenced callback. A queued old timeout
        # cannot consume a new request after cancellation or source replacement.
        self.timer = QtCore.QTimer(self.window)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(callback)
        self.timer.start(RELOAD_DEBOUNCE_MS)

    def load(self, request: ViewportRequest) -> bool:
        extent = "full heatmap" if request.ranges is None else "visible data"
        return self.window.load_data(
            force_sql_heatmap=True,
            heatmap_axis_ranges=request.ranges.as_dict() if request.ranges is not None else None,
            heatmap_full_axis_ranges=request.full.as_dict(),
            status_message=f"Loading {extent} for {self.window.param.name}...",
        )
