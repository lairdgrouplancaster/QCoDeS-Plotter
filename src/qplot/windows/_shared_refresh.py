"""Apply the main window's refresh choices to every retained plot source."""

from collections.abc import Iterator
from typing import Any

from ._refresh_interval import set_refresh_interval


def iter_plot_sources(owner: Any) -> Iterator[Any]:
    """Include closed overlay sources once, even when hosts form cycles."""
    pending = list(getattr(owner, "windows", ()))
    seen: set[int] = set()
    while pending:
        plot = pending.pop()
        if id(plot) in seen:
            continue
        seen.add(id(plot))
        state = plot.__dict__
        for registry_name in ("lines", "heatmaps"):
            registry = state.get(registry_name, {})
            if not isinstance(registry, dict):
                continue
            for item in tuple(registry.values()):
                source = getattr(item, "from_win", None)
                if source is not None:
                    pending.append(source)
        yield plot


def apply_shared_refresh_interval(owner: Any, interval: float) -> None:
    """Synchronise interval storage and timers without changing plot data."""
    for plot in iter_plot_sources(owner):
        spin_box = plot.__dict__.get("spinBox")
        update = getattr(plot, "monitorIntervalChanged", None)
        if spin_box is None or not callable(update):
            continue
        set_refresh_interval(spin_box, interval)
        if interval <= 0 and not plot.__dict__.get("_refresh_pending_force", False):
            plot._refresh_pending = False
        update(interval)


def refresh_plot_sources(owner: Any) -> None:
    """Request one manual refresh per live source, preserving frozen data."""
    for plot in iter_plot_sources(owner):
        if "_frozen_result" in plot.__dict__:
            continue
        refresh = getattr(plot, "refreshWindow", None)
        if callable(refresh):
            refresh(force=True)
