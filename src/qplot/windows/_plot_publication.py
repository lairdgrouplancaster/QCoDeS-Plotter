"""Own plot publication snapshots without widgets, signals or database access."""

from __future__ import annotations

from collections.abc import MutableMapping
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any

from qplot.datahandling.qcodes_cache import (
    CacheParameterPublicationState,
    restore_cache_parameter_publication_state,
    snapshot_cache_parameter_publication_state,
)


class PublicationOutcome(Enum):
    PENDING = auto()
    COMMITTED = auto()
    ROLLED_BACK = auto()
    RENDER_FAILED = auto()
    DISCARDED = auto()


@dataclass(frozen=True)
class SavedPlotField:
    name: str
    present: bool
    value: Any


@dataclass(frozen=True)
class PlotSnapshot:
    """Shallow references preserve exact arrays and distinguish absent fields."""

    fields: tuple[SavedPlotField, ...]

    @classmethod
    def capture(cls, state: MutableMapping[str, Any], extra: tuple[str, ...]) -> PlotSnapshot:
        names = (
            "_live", "_qplot_display_synchronized", "_qplot_display_uses_direct_sql",
            "_heatmap_full_resolution", "axis_data", "axis_param", "dataGrid",
            "display_param", "complex_samples", "last_ds_len",
        ) + extra
        return cls(tuple(SavedPlotField(name, name in state, state.get(name)) for name in names))

    def restore(self, state: MutableMapping[str, Any]) -> None:
        for field in self.fields:
            if field.present:
                state[field.name] = field.value
            else:
                state.pop(field.name, None)


@dataclass(eq=False)
class RefreshPublication:
    worker: Any
    generation: int
    cache: Any
    parameter_name: str
    prior_cache: CacheParameterPublicationState | None
    plot: PlotSnapshot
    published_cache: CacheParameterPublicationState | None = None
    outcome: PublicationOutcome = PublicationOutcome.PENDING
    completing: bool = False


class PlotPublicationOwner:
    """Only the current transaction may commit, restore or retire plot state.

    Source rejection restores the cache only if it still contains this
    transaction's writes. Rendering failure retains valid shared cache data,
    restores plot fields and leaves the display retryable instead.
    """

    def __init__(self) -> None:
        self.generation = 0
        self.current: RefreshPublication | None = None

    def begin(
        self, worker: Any, state: MutableMapping[str, Any], *,
        cache: Any, parameter_name: str, extra_fields: tuple[str, ...] = (),
    ) -> RefreshPublication:
        plot = PlotSnapshot.capture(state, extra_fields)
        try:
            prior_cache = (snapshot_cache_parameter_publication_state(cache, parameter_name)
                           if cache is not None else None)
        except AttributeError:
            # Partial test caches cannot receive the shared-cache mutation,
            # but their plot fields still need the same publication lifetime.
            prior_cache = None
        if self.current is not None:
            self.current.outcome = PublicationOutcome.DISCARDED
        self.generation += 1
        publication = RefreshPublication(worker, self.generation, cache, parameter_name, prior_cache, plot)
        self.current = publication
        return publication

    def for_worker(self, worker: Any) -> RefreshPublication | None:
        publication = self.current
        return publication if publication is not None and publication.worker is worker else None

    def owns(self, publication: RefreshPublication, current_worker: Any) -> bool:
        return (self.current is publication and publication.generation == self.generation
                and publication.worker is current_worker
                and publication.outcome is PublicationOutcome.PENDING)

    def record_cache(self, worker: Any) -> None:
        publication = self.for_worker(worker)
        if (publication is not None and publication.prior_cache is not None
                and publication.published_cache is None):
            # Later presentation callbacks can publish another window's cache
            # data. Do not claim those writes during final bookkeeping.
            publication.published_cache = snapshot_cache_parameter_publication_state(
                publication.cache, publication.parameter_name,
            )

    def discard(self, worker: Any) -> None:
        publication = self.for_worker(worker)
        if publication is not None:
            publication.outcome = PublicationOutcome.DISCARDED
            self.current = None

    def commit(self, publication: RefreshPublication, current_worker: Any) -> bool:
        if not self.owns(publication, current_worker):
            return False
        publication.outcome = PublicationOutcome.COMMITTED
        self.current = None
        return True

    def rollback(self, worker: Any, current_worker: Any, state: MutableMapping[str, Any]) -> bool:
        publication = self.for_worker(worker)
        if publication is None:
            return False
        if not self.owns(publication, current_worker):
            self.discard(worker)
            return False
        if publication.published_cache is not None:
            restore_cache_parameter_publication_state(
                publication.cache, publication.parameter_name,
                publication.prior_cache, publication.published_cache,
            )
        publication.plot.restore(state)
        publication.outcome = PublicationOutcome.ROLLED_BACK
        self.current = None
        return True

    def render_failed(self, worker: Any, current_worker: Any, state: MutableMapping[str, Any]) -> bool:
        publication = self.for_worker(worker)
        if publication is None or not self.owns(publication, current_worker):
            return False
        publication.plot.restore(state)
        state["_qplot_display_synchronized"] = False
        publication.outcome = PublicationOutcome.RENDER_FAILED
        self.current = None
        return True
