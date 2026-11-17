"""Own accepted heatmap extents and coalesced viewport reloads."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from math import isfinite
from sys import float_info
from typing import Protocol

from ._plot_publication import PublicationOutcome, RefreshPublication

RELOAD_DEBOUNCE_MS = 450
_MIN_VIEW_FRACTION = 0.95


@dataclass(frozen=True)
class AxisRanges:
    x: tuple[float, float]
    y: tuple[float, float]

    @classmethod
    def normalise(cls, ranges: Mapping[str, tuple[float, float]] | None) -> AxisRanges | None:
        if ranges is None:
            return None
        axes = []
        for axis in ("x", "y"):
            try:
                low, high = sorted(float(value) for value in ranges[axis])
            except (KeyError, TypeError, ValueError, OverflowError):
                return None
            if not (isfinite(low) and isfinite(high)) or low == high:
                return None
            axes.append((low, high))
        return cls(*axes)

    def as_dict(self) -> dict[str, tuple[float, float]]:
        return {"x": self.x, "y": self.y}

    def matches(self, other: AxisRanges | None) -> bool:
        if other is None:
            return False
        for (low, high), (other_low, other_high) in zip((self.x, self.y), (other.x, other.y), strict=True):
            width = max(abs(high - low), abs(other_high - other_low))
            scale = max(abs(low), abs(high), abs(other_low), abs(other_high), 1.)
            tolerance = max(width * 0.01, float_info.epsilon * scale * 16)
            if abs(low - other_low) > tolerance or abs(high - other_high) > tolerance:
                return False
        return True

    def visible_within(self, full: AxisRanges) -> AxisRanges | None:
        clamped = []
        zoomed = False
        for (low, high), (full_low, full_high) in zip((self.x, self.y), (full.x, full.y), strict=True):
            low = max(full_low, min(full_high, low))
            high = max(full_low, min(full_high, high))
            if high <= low:
                return None
            zoomed |= high - low < (full_high - full_low) * _MIN_VIEW_FRACTION
            clamped.append((low, high))
        return AxisRanges(*clamped) if zoomed else None


@dataclass(frozen=True)
class HeatmapViewportState:
    enabled: bool = False
    full: AxisRanges | None = None
    full_view: AxisRanges | None = None
    displayed: AxisRanges | None = None


@dataclass(frozen=True)
class ViewportRequest:
    ranges: AxisRanges | None
    full: AxisRanges


class HeatmapViewportHost(Protocol):
    def available(self) -> bool: ...
    def busy(self) -> bool: ...
    def visible_ranges(self) -> AxisRanges | None: ...
    def schedule(self, callback: Callable[[], None]) -> None: ...
    def stop_timer(self) -> None: ...
    def load(self, request: ViewportRequest) -> bool: ...


class HeatmapViewportController:
    def __init__(self, host: HeatmapViewportHost) -> None:
        self.host = host
        self._accepted = HeatmapViewportState()
        self._staged: tuple[RefreshPublication, HeatmapViewportState] | None = None
        self._settled_generation: int | None = None
        self._ticket: object | None = None
        self._intent = object()
        self._full_requested = False
        self._retry_required = False
        self.requested: ViewportRequest | None = None

    @property
    def accepted(self) -> HeatmapViewportState:
        # Preview consumers may observe the plot immediately after commit,
        # before the concrete renderer's finally block runs.
        self._settle_publication()
        return self._accepted

    @property
    def pending(self) -> bool:
        return self._ticket is not None

    def stage(self, publication: RefreshPublication, state: HeatmapViewportState) -> None:
        self._settle_publication()
        self._staged = (publication, state)

    def _settle_publication(self) -> None:
        if self._staged is None:
            return
        publication, state = self._staged
        if publication.outcome is PublicationOutcome.PENDING:
            return
        self._staged = None
        self._settled_generation = publication.generation
        if publication.outcome is PublicationOutcome.COMMITTED:
            self._accepted = state
            self._retry_required = False
        elif publication.outcome in (PublicationOutcome.RENDER_FAILED, PublicationOutcome.ROLLED_BACK):
            # A cleared renderer needs a retry even if the requested range is
            # identical to the last successful image's range.
            self._retry_required = True

    def finish(self, publication: RefreshPublication | None) -> None:
        if self._staged is not None and self._staged[0] is publication:
            self._settle_publication()
        if (publication is not None and self._staged is None
                and self._settled_generation == publication.generation and not self._accepted.enabled):
            self.cancel_pending()

    def cancel_pending(self) -> None:
        self._intent = object()
        self._ticket = None
        self._full_requested = False
        self.host.stop_timer()

    def reset(self) -> None:
        self.cancel_pending()
        self._staged = None
        self._settled_generation = None
        self._accepted = HeatmapViewportState()
        self.requested = None
        self._retry_required = False

    def _arm(self) -> None:
        ticket = self._intent = self._ticket = object()
        def flush() -> None:
            self._flush(ticket)
        self.host.schedule(flush)

    def schedule_visible(self) -> None:
        if self.accepted.enabled and self.host.available():
            self._full_requested = False
            self._arm()

    def reload(self, *, full: bool = False) -> bool:
        self.cancel_pending()
        self._full_requested = full
        ticket = self._intent = self._ticket = object()
        return self._flush(ticket)

    def _flush(self, ticket: object) -> bool:
        if ticket is not self._ticket:
            return False
        self._ticket = None
        state = self.accepted
        if not state.enabled or state.full is None or not self.host.available():
            return False
        if self.host.busy():
            self._arm()
            return False
        ranges = None if self._full_requested else self.host.visible_ranges()
        # Reading Qt ranges can pump callbacks that cancel or replace a request.
        if self._intent is not ticket or self.accepted is not state or not self.host.available():
            return False
        if self.host.busy():
            self._arm()
            return False
        self.requested = ViewportRequest(ranges, state.full)
        same = (ranges.matches(state.displayed) if ranges is not None else state.displayed is None)
        if same and not self._retry_required:
            return False
        return self.host.load(self.requested)
