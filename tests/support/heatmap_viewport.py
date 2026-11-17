"""Arrange accepted viewport state in focused window tests without Qt timers."""

from dataclasses import replace
from types import SimpleNamespace

from qplot.windows._heatmap_viewport import AxisRanges, HeatmapViewportController
from qplot.windows._heatmap_viewport_ui import HeatmapViewportUi


class _Timer:
    def start(self, _interval):
        pass

    def stop(self):
        pass


class _ViewportHost(HeatmapViewportUi):
    def __init__(self, window):
        self.window = window
        self.timer = _Timer()
        self.callback = None

    def schedule(self, callback):
        self.callback = callback
        self.timer.start(450)


def set_viewport(window, **fields):
    if not hasattr(type(window), "_viewport_state"):
        # Lightweight layer-source doubles expose only this observation.
        for name, value in fields.items():
            setattr(window, name, value)
        return
    controller = window.__dict__.get("_heatmap_viewport_controller")
    if controller is None:
        host = _ViewportHost(window)
        window.__dict__["_heatmap_viewport_ui"] = host
        controller = window.__dict__["_heatmap_viewport_controller"] = HeatmapViewportController(host)
    names = {"_large_heatmap_sql_mode": "enabled", "_heatmap_full_axis_ranges": "full",
             "_heatmap_full_view_ranges": "full_view", "_heatmap_last_view_ranges": "displayed"}
    changes = {}
    for name, value in fields.items():
        if name == "_heatmap_view_reload_timer":
            window.__dict__["_heatmap_viewport_ui"].timer = value
        else:
            changes[names[name]] = value if name == "_large_heatmap_sql_mode" else AxisRanges.normalise(value)
    controller._accepted = replace(controller.accepted, **changes)


def stage_viewport(window, result):
    set_viewport(window)
    worker = SimpleNamespace()
    owner = window._publication_owner()
    record = owner.begin(worker, window.__dict__, cache=None, parameter_name="signal")
    window._update_large_heatmap_state(result)
    assert owner.commit(record, worker)
    window._viewport_controller().finish(record)
