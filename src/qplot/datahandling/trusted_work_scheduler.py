"""Qt-independent scheduling and cache identity for trusted-live derived work.

Stage 5A deliberately stops at ownership and scheduling.  This module does not
open databases, render images, or access a disk cache.  An owner pulls one
bounded :class:`ScheduledWork` claim at a time and completes or abandons it.
"""

from __future__ import annotations

import math
import os
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import TypeAlias

from qplot.datahandling.file_identity import DatabaseInstance
from qplot.datahandling.trusted_live_queries import TrustedSourceRevision

RenderingOptionValue: TypeAlias = None | bool | int | float | str
CanonicalRenderingOptionValue: TypeAlias = tuple[str, RenderingOptionValue]
PriorityKey: TypeAlias = tuple[int, int, int]


class TrustedWorkKind(IntEnum):
    """Information order within every run-priority tier."""

    METADATA = 0
    THUMBNAIL = 1
    PREVIEW = 2


class TrustedRunTier(IntEnum):
    """Run order, from interactive foreground to stable background."""

    SELECTED = 0
    VISIBLE = 1
    REMAINING = 2


class TrustedWorkState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class SchedulerLifecycle(StrEnum):
    ACTIVE = "active"
    CLOSED = "closed"


class CompletionDisposition(StrEnum):
    ACCEPTED = "accepted"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class RenderingOptions:
    """Canonical, immutable rendering options that participate in identity."""

    values: tuple[tuple[str, RenderingOptionValue], ...] = field(
        default=(),
        compare=False,
        hash=False,
    )
    _canonical_values: tuple[tuple[str, CanonicalRenderingOptionValue], ...] = field(
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        names = tuple(name for name, _value in self.values)
        if any(not isinstance(name, str) or not name for name in names):
            raise ValueError("Rendering option names must be non-empty strings.")
        if names != tuple(sorted(names)) or len(names) != len(set(names)):
            raise ValueError("Rendering options must have unique, sorted names.")
        canonical = []
        for name, value in self.values:
            if value is None:
                tagged: CanonicalRenderingOptionValue = ("none", None)
            elif isinstance(value, bool):
                tagged = ("boolean", value)
            elif isinstance(value, int):
                tagged = ("integer", value)
            elif isinstance(value, float):
                if not math.isfinite(value):
                    raise ValueError("Floating rendering options must be finite.")
                tagged = ("float", value)
            elif isinstance(value, str):
                tagged = ("string", value)
            else:
                raise TypeError("A rendering option value is not cache-key safe.")
            canonical.append((name, tagged))
        object.__setattr__(self, "_canonical_values", tuple(canonical))

    @property
    def canonical_values(
        self,
    ) -> tuple[tuple[str, CanonicalRenderingOptionValue], ...]:
        """Type-tagged deterministic representation used by equality/hashing."""

        return self._canonical_values

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, RenderingOptionValue],
    ) -> RenderingOptions:
        return cls(tuple(sorted(values.items())))


@dataclass(frozen=True, slots=True)
class WorkFormat:
    """Renderer/format namespace for one kind of derived result."""

    renderer_version: str
    options: RenderingOptions = RenderingOptions()

    def __post_init__(self) -> None:
        if not isinstance(self.renderer_version, str) or not self.renderer_version:
            raise ValueError("renderer_version must be a non-empty string.")


@dataclass(frozen=True, slots=True)
class TrustedCacheWorkKey:
    """Complete immutable identity for one cached or in-flight result."""

    database_instance: DatabaseInstance
    run_guid: str
    kind: TrustedWorkKind
    source_revision: TrustedSourceRevision
    renderer_version: str
    rendering_options: RenderingOptions = RenderingOptions()

    def __post_init__(self) -> None:
        if self.database_instance.identity is None:
            raise ValueError("A cache key requires an exact database file identity.")
        if not isinstance(self.run_guid, str) or not self.run_guid:
            raise ValueError("run_guid must be a non-empty string.")
        if not isinstance(self.kind, TrustedWorkKind):
            raise TypeError("kind must be a TrustedWorkKind.")
        if not isinstance(self.source_revision, TrustedSourceRevision):
            raise TypeError("source_revision must be a TrustedSourceRevision.")
        if not isinstance(self.renderer_version, str) or not self.renderer_version:
            raise ValueError("renderer_version must be a non-empty string.")
        if not isinstance(self.rendering_options, RenderingOptions):
            raise TypeError("rendering_options must be RenderingOptions.")


@dataclass(frozen=True, slots=True)
class TrustedRunWorkSource:
    """Minimal table-order input; no derived task is allocated here."""

    run_guid: str
    source_revision: TrustedSourceRevision

    def __post_init__(self) -> None:
        if not isinstance(self.run_guid, str) or not self.run_guid:
            raise ValueError("run_guid must be a non-empty string.")
        if not isinstance(self.source_revision, TrustedSourceRevision):
            raise TypeError("source_revision must be a TrustedSourceRevision.")


class CancellationToken:
    """Thread-safe cooperative cancellation flag owned by one work claim."""

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise InterruptedError("The scheduled work was cancelled.")


@dataclass(frozen=True, slots=True)
class ScheduledWork:
    """One bounded owner claim, safe to pass to a controllable executor."""

    claim_id: int
    generation: int
    run_index: int
    priority_key: PriorityKey
    key: TrustedCacheWorkKey
    cancellation: CancellationToken


@dataclass(frozen=True, slots=True)
class WorkTransition:
    generation: int
    key: TrustedCacheWorkKey
    previous: TrustedWorkState
    current: TrustedWorkState
    timestamp: float


@dataclass(frozen=True, slots=True)
class WorkPublication:
    """A current-instance result, explicitly tagged for safe UI routing."""

    generation: int
    key: TrustedCacheWorkKey
    result: object
    is_current_selection: bool


@dataclass(frozen=True, slots=True)
class SchedulerSnapshot:
    lifecycle: SchedulerLifecycle
    generation: int
    database_instance: DatabaseInstance
    run_count: int
    selected_index: int | None
    visible_range: tuple[int, int]
    visible_indices: tuple[int, ...]
    pending_count: int
    running: tuple[ScheduledWork, ...]
    completed_count: int
    next_priority_key: PriorityKey | None


TransitionCallback: TypeAlias = Callable[[WorkTransition], None]
PublicationCallback: TypeAlias = Callable[[WorkPublication], None]

_WORK_KINDS = tuple(TrustedWorkKind)
_RUN_TIERS = tuple(TrustedRunTier)
_DEFAULT_FORMATS = MappingProxyType(
    {
        TrustedWorkKind.METADATA: WorkFormat("metadata-v1"),
        TrustedWorkKind.THUMBNAIL: WorkFormat("thumbnail-v1"),
        TrustedWorkKind.PREVIEW: WorkFormat("preview-v1"),
    }
)


def trusted_derived_cache_root(
    *,
    environment: Mapping[str, str] | None = None,
    home: str | PurePath | None = None,
    platform: str | None = None,
) -> PurePath:
    """Return the future application-cache namespace without creating it."""

    environment = os.environ if environment is None else environment
    platform = sys.platform if platform is None else platform
    path_type: type[PurePath]
    home_path: PurePath
    candidate: PurePath | None
    base: PurePath
    if platform == "win32":
        path_type = PureWindowsPath
        home_path = path_type(str(Path.home()) if home is None else str(home))
        if not home_path.is_absolute():
            raise ValueError("home must be absolute under Windows path rules.")
        configured = environment.get("LOCALAPPDATA", "")
        candidate = path_type(configured) if configured else None
        base = (
            candidate
            if candidate is not None and candidate.is_absolute()
            else home_path / "AppData" / "Local"
        )
    elif platform == "darwin":
        path_type = PurePosixPath
        home_path = path_type(str(Path.home()) if home is None else str(home))
        if not home_path.is_absolute():
            raise ValueError("home must be absolute under POSIX path rules.")
        base = home_path / "Library" / "Caches"
    else:
        path_type = PurePosixPath
        home_path = path_type(str(Path.home()) if home is None else str(home))
        if not home_path.is_absolute():
            raise ValueError("home must be absolute under POSIX path rules.")
        configured = environment.get("XDG_CACHE_HOME", "")
        candidate = path_type(configured) if configured else None
        base = (
            candidate
            if candidate is not None and candidate.is_absolute()
            else home_path / ".cache"
        )
    return base / "qplot" / "trusted-derived"


class TrustedWorkScheduler:
    """Deterministic, lazy owner for one exact database generation.

    The exact priority key is ``(run tier, work kind, stable table index)``.
    No ready queue is materialised.  Nine small lane cursors lazily find the
    next claim, while one completion byte per run records all three kinds.

    The constructing thread is the sole scheduler owner.  Every scheduler
    method and property must be used on that thread; only a claim's
    :class:`CancellationToken` is cross-thread safe.  Executors must marshal
    completion back to the owner thread.  Owner-thread callbacks may re-enter.
    """

    def __init__(
        self,
        database_instance: DatabaseInstance,
        runs: Sequence[TrustedRunWorkSource],
        *,
        formats: Mapping[TrustedWorkKind, WorkFormat] | None = None,
        clock: Callable[[], float] = time.monotonic,
        on_transition: TransitionCallback | None = None,
        on_publish: PublicationCallback | None = None,
        on_selected_publish: PublicationCallback | None = None,
    ) -> None:
        self._owner_thread_id = threading.get_ident()
        self._validate_database_instance(database_instance)
        run_table = self._validated_run_table(runs)
        self._database_instance = database_instance
        self._runs = run_table
        self._formats = self._validated_formats(formats or _DEFAULT_FORMATS)
        self._clock = clock
        self._on_transition = on_transition
        self._on_publish = on_publish
        self._on_selected_publish = on_selected_publish
        self._lifecycle = SchedulerLifecycle.ACTIVE
        self._generation = 1
        self._selected_index: int | None = None
        self._visible_start = 0
        self._visible_stop = 0
        self._visible_indices: tuple[int, ...] = ()
        self._visible_index_set: frozenset[int] = frozenset()
        self._completion_masks = bytearray(len(runs))
        self._revision_overrides: dict[int, TrustedSourceRevision] = {}
        self._completed_count = 0
        self._running: dict[int, ScheduledWork] = {}
        self._running_slots: set[tuple[int, TrustedWorkKind]] = set()
        self._next_claim_id = 1
        self._lane_cursors: dict[tuple[TrustedRunTier, TrustedWorkKind], int] = {}
        self._reset_cursors()

    @property
    def lifecycle(self) -> SchedulerLifecycle:
        self._require_owner()
        return self._lifecycle

    @property
    def generation(self) -> int:
        self._require_owner()
        return self._generation

    @property
    def database_instance(self) -> DatabaseInstance:
        self._require_owner()
        return self._database_instance

    @property
    def allocated_work_count(self) -> int:
        """Number of actual derived-work claims, bounded to zero or one."""

        self._require_owner()
        return len(self._running)

    def select_run(self, run_index: int | None) -> None:
        self._require_owner()
        self._require_active()
        if run_index is not None:
            self._validate_run_index(run_index)
        if run_index != self._selected_index:
            self._selected_index = run_index
            self._reset_cursors()

    def set_visible_range(self, start: int, stop: int) -> None:
        """Set the half-open stable table range currently visible."""

        self._require_owner()
        self._require_active()
        if (
            type(start) is not int
            or type(stop) is not int
            or start < 0
            or stop < start
            or stop > len(self._runs)
        ):
            raise ValueError("The visible range must be within the run table.")
        exact = tuple(range(start, stop))
        if (start, stop) != (
            self._visible_start,
            self._visible_stop,
        ) or exact != self._visible_indices:
            self._visible_start = start
            self._visible_stop = stop
            self._visible_indices = exact
            self._visible_index_set = frozenset(self._visible_indices)
            self._reset_cursors()

    def set_visible_indices(self, indices: Sequence[int]) -> None:
        """Set the exact stable run slots currently intersecting the viewport.

        Qt sorting and filtering can make visible rows non-contiguous in the
        scheduler's stable run order.  The original range API remains for
        simple consumers; Stage 5C uses this exact bounded set.
        """

        self._require_owner()
        self._require_active()
        if not isinstance(indices, Sequence):
            raise TypeError("visible indices must be a stable Sequence.")
        exact = tuple(indices)
        if any(type(index) is not int for index in exact):
            raise TypeError("visible indices must contain integers.")
        if any(not 0 <= index < len(self._runs) for index in exact):
            raise ValueError("A visible index is outside the run table.")
        if len(exact) != len(set(exact)):
            raise ValueError("visible indices must be unique.")
        exact = tuple(sorted(exact))
        if exact == self._visible_indices:
            return
        self._visible_indices = exact
        self._visible_index_set = frozenset(exact)
        self._visible_start = exact[0] if exact else 0
        self._visible_stop = exact[-1] + 1 if exact else 0
        self._reset_cursors()

    def claim_next(self) -> ScheduledWork | None:
        """Claim the deterministic next item, with at most one in flight."""

        self._require_owner()
        self._require_active()
        if self._running:
            return None
        candidate = self._next_candidate(advance=True)
        if candidate is None:
            return None
        tier, kind, run_index = candidate
        source = self._runs[run_index]
        source_revision = self._revision_overrides.get(
            run_index,
            source.source_revision,
        )
        work_format = self._formats[kind]
        key = TrustedCacheWorkKey(
            self._database_instance,
            source.run_guid,
            kind,
            source_revision,
            work_format.renderer_version,
            work_format.options,
        )
        work = ScheduledWork(
            claim_id=self._next_claim_id,
            generation=self._generation,
            run_index=run_index,
            priority_key=(int(tier), int(kind), run_index),
            key=key,
            cancellation=CancellationToken(),
        )
        self._next_claim_id += 1
        self._running[work.claim_id] = work
        self._running_slots.add((run_index, kind))
        self._transition(work, TrustedWorkState.PENDING, TrustedWorkState.RUNNING)
        return work

    def complete(
        self,
        work: ScheduledWork,
        result: object = None,
    ) -> CompletionDisposition:
        """Accept only the exact current claim and publish only a keyed result."""

        self._require_owner()
        current = self._running.get(work.claim_id)
        if current is not work:
            return CompletionDisposition.STALE
        if not self._is_current(work):
            transitions = self._remove_claims((work,))
            self._emit_transitions(transitions)
            return CompletionDisposition.STALE
        self._running.pop(work.claim_id)
        self._running_slots.remove((work.run_index, work.key.kind))
        bit = self._kind_bit(work.key.kind)
        if not self._completion_masks[work.run_index] & bit:
            self._completion_masks[work.run_index] |= bit
            self._completed_count += 1
        self._transition(work, TrustedWorkState.RUNNING, TrustedWorkState.COMPLETED)
        if not self._is_current(work):
            return CompletionDisposition.STALE
        publication = WorkPublication(
            generation=work.generation,
            key=work.key,
            result=result,
            is_current_selection=(work.run_index == self._selected_index),
        )
        if self._on_publish is not None:
            self._on_publish(publication)
        if (
            self._on_selected_publish is not None
            and self._is_current(work)
            and work.run_index == self._selected_index
        ):
            self._on_selected_publish(
                WorkPublication(
                    work.generation,
                    work.key,
                    result,
                    True,
                )
            )
        return CompletionDisposition.ACCEPTED

    def abandon(self, work: ScheduledWork) -> CompletionDisposition:
        """Return current work to pending; obsolete claims remain harmless."""

        self._require_owner()
        current = self._running.get(work.claim_id)
        if current is not work:
            return CompletionDisposition.STALE
        if not self._is_current(work):
            transitions = self._remove_claims((work,))
            self._emit_transitions(transitions)
            return CompletionDisposition.STALE
        self._running.pop(work.claim_id)
        self._running_slots.remove((work.run_index, work.key.kind))
        tier = self._tier_for_index(work.run_index)
        lane = (tier, work.key.kind)
        self._lane_cursors[lane] = min(self._lane_cursors[lane], work.run_index)
        self._transition(work, TrustedWorkState.RUNNING, TrustedWorkState.PENDING)
        return CompletionDisposition.ACCEPTED

    def is_current_claim(self, work: ScheduledWork) -> bool:
        """Prove an exact claim token still owns its complete scheduler identity."""

        self._require_owner()
        return self._running.get(work.claim_id) is work and self._is_current(work)

    def refine_claim_source_revision(
        self,
        work: ScheduledWork,
        source_revision: TrustedSourceRevision,
    ) -> CompletionDisposition:
        """Atomically adopt an authoritative revision from one current claim.

        No state is changed until the exact claim token, generation, database,
        stable run slot/GUID, kind, prior source identity, and render format are
        all proven current.  The refining claim is retired without publication;
        its exact-key payload may then be reused by the owner for the replacement
        claim.
        """

        self._require_owner()
        if not isinstance(source_revision, TrustedSourceRevision):
            raise TypeError("source_revision must be a TrustedSourceRevision.")
        if not self.is_current_claim(work):
            return CompletionDisposition.STALE
        if work.key.source_revision == source_revision:
            return CompletionDisposition.ACCEPTED
        self._running.pop(work.claim_id)
        self._running_slots.remove((work.run_index, work.key.kind))
        self._revision_overrides[work.run_index] = source_revision
        self._completed_count -= self._completion_masks[work.run_index].bit_count()
        self._completion_masks[work.run_index] = 0
        self._reset_cursors()
        self._transition(work, TrustedWorkState.RUNNING, TrustedWorkState.CANCELLED)
        return CompletionDisposition.ACCEPTED

    def update_source_revision(
        self,
        run_index: int,
        source_revision: TrustedSourceRevision,
    ) -> None:
        """Invalidate all derived kinds for a live run's new source revision."""

        self._require_owner()
        self._require_active()
        self._validate_run_index(run_index)
        if not isinstance(source_revision, TrustedSourceRevision):
            raise TypeError("source_revision must be a TrustedSourceRevision.")
        prior = self._revision_overrides.get(
            run_index,
            self._runs[run_index].source_revision,
        )
        if prior == source_revision:
            return
        affected = tuple(
            work for work in self._running.values() if work.run_index == run_index
        )
        self._revision_overrides[run_index] = source_revision
        self._completed_count -= self._completion_masks[run_index].bit_count()
        self._completion_masks[run_index] = 0
        transitions = self._remove_claims(affected)
        self._reset_cursors()
        self._emit_transitions(transitions)

    def update_format(self, kind: TrustedWorkKind, work_format: WorkFormat) -> None:
        """Invalidate one output kind after renderer/options identity changes."""

        self._require_owner()
        self._require_active()
        if not isinstance(kind, TrustedWorkKind):
            raise TypeError("kind must be a TrustedWorkKind.")
        if not isinstance(work_format, WorkFormat):
            raise TypeError("work_format must be a WorkFormat.")
        if self._formats[kind] == work_format:
            return
        affected = tuple(
            work for work in self._running.values() if work.key.kind is kind
        )
        self._formats[kind] = work_format
        bit = self._kind_bit(kind)
        for index, mask in enumerate(self._completion_masks):
            if mask & bit:
                self._completion_masks[index] &= ~bit
                self._completed_count -= 1
        transitions = self._remove_claims(affected)
        self._reset_cursors()
        self._emit_transitions(transitions)

    def request_completed_work(
        self,
        run_index: int,
        kind: TrustedWorkKind,
        *,
        database_instance: DatabaseInstance,
        generation: int,
        run_guid: str,
    ) -> bool:
        """Return one exact completed item to pending for bounded replay.

        This is intentionally narrower than source or format invalidation: it
        retains the exact cache identity and resets only one completion bit.
        Stale GUI requests are harmless because the complete database,
        generation, stable run slot/GUID, and work-kind identity must still
        match. Repeated requests coalesce while that item is pending/running.
        """

        self._require_owner()
        self._require_active()
        if not isinstance(kind, TrustedWorkKind):
            raise TypeError("kind must be a TrustedWorkKind.")
        if type(run_index) is not int:
            raise TypeError("run_index must be an integer.")
        if not isinstance(database_instance, DatabaseInstance):
            raise TypeError("database_instance must be a DatabaseInstance.")
        if type(generation) is not int:
            raise TypeError("generation must be an integer.")
        if not isinstance(run_guid, str) or not run_guid:
            raise ValueError("run_guid must be a non-empty string.")
        if (
            database_instance != self._database_instance
            or generation != self._generation
            or not 0 <= run_index < len(self._runs)
            or self._runs[run_index].run_guid != run_guid
            or (run_index, kind) in self._running_slots
        ):
            return False
        bit = self._kind_bit(kind)
        if not self._completion_masks[run_index] & bit:
            return False
        self._completion_masks[run_index] &= ~bit
        self._completed_count -= 1
        self._reset_cursors()
        return True

    def reconcile_runs(self, runs: Sequence[TrustedRunWorkSource]) -> None:
        """Adopt an append-only same-database run table without replaying work.

        Existing entries must be an exact stable prefix.  Reordering, removal,
        GUID replacement, source-revision replacement, and duplicate GUIDs are
        rejected; source changes use :meth:`update_source_revision` instead.
        """

        self._require_owner()
        self._require_active()
        run_table = self._validated_run_table(runs)
        old_count = len(self._runs)
        if len(run_table) < old_count:
            raise ValueError("Same-instance run reconciliation may not remove runs.")
        effective_prefix = tuple(
            TrustedRunWorkSource(
                run.run_guid,
                self._revision_overrides.get(index, run.source_revision),
            )
            for index, run in enumerate(self._runs)
        )
        prefix_is_stable = all(
            incoming.run_guid == original.run_guid
            and incoming.source_revision
            in {original.source_revision, effective.source_revision}
            for incoming, original, effective in zip(
                run_table[:old_count],
                self._runs,
                effective_prefix,
                strict=True,
            )
        )
        if not prefix_is_stable:
            raise ValueError(
                "Existing runs must remain an exact stable prefix; use "
                "update_source_revision for source changes."
            )
        if len(run_table) == old_count:
            return
        # Fold refined revisions into the stable prefix.  Callers may present
        # either their original baseline or the coordinator's already-refined
        # view, but an append must never regress effective source identity.
        self._runs = (*effective_prefix, *run_table[old_count:])
        self._revision_overrides = {
            index: revision
            for index, revision in self._revision_overrides.items()
            if index >= old_count
        }
        self._completion_masks.extend(b"\0" * (len(run_table) - old_count))
        self._reset_cursors()

    def switch_database(
        self,
        database_instance: DatabaseInstance,
        runs: Sequence[TrustedRunWorkSource],
    ) -> None:
        """Cancel and invalidate the complete prior exact-instance namespace."""

        self._require_owner()
        self._require_active()
        self._validate_database_instance(database_instance)
        run_table = self._validated_run_table(runs)
        affected = tuple(self._running.values())
        self._generation += 1
        self._database_instance = database_instance
        self._runs = run_table
        self._selected_index = None
        self._visible_start = 0
        self._visible_stop = 0
        self._visible_indices = ()
        self._visible_index_set = frozenset()
        self._completion_masks = bytearray(len(runs))
        self._revision_overrides = {}
        self._completed_count = 0
        transitions = self._remove_claims(affected)
        self._reset_cursors()
        self._emit_transitions(transitions)

    def close(self) -> None:
        self._require_owner()
        if self._lifecycle is SchedulerLifecycle.CLOSED:
            return
        affected = tuple(self._running.values())
        self._lifecycle = SchedulerLifecycle.CLOSED
        transitions = self._remove_claims(affected)
        self._emit_transitions(transitions)

    def state_for(self, run_index: int, kind: TrustedWorkKind) -> TrustedWorkState:
        self._require_owner()
        self._validate_run_index(run_index)
        if (run_index, kind) in self._running_slots:
            return TrustedWorkState.RUNNING
        if self._completion_masks[run_index] & self._kind_bit(kind):
            return TrustedWorkState.COMPLETED
        return TrustedWorkState.PENDING

    def snapshot(self) -> SchedulerSnapshot:
        self._require_owner()
        next_candidate = None
        if self._lifecycle is SchedulerLifecycle.ACTIVE and not self._running:
            candidate = self._next_candidate(advance=False)
            if candidate is not None:
                tier, kind, run_index = candidate
                next_candidate = (int(tier), int(kind), run_index)
        total = len(self._runs) * len(_WORK_KINDS)
        return SchedulerSnapshot(
            lifecycle=self._lifecycle,
            generation=self._generation,
            database_instance=self._database_instance,
            run_count=len(self._runs),
            selected_index=self._selected_index,
            visible_range=(self._visible_start, self._visible_stop),
            visible_indices=self._visible_indices,
            pending_count=total - self._completed_count - len(self._running),
            running=tuple(self._running.values()),
            completed_count=self._completed_count,
            next_priority_key=next_candidate,
        )

    def _next_candidate(
        self,
        *,
        advance: bool,
    ) -> tuple[TrustedRunTier, TrustedWorkKind, int] | None:
        for tier in _RUN_TIERS:
            for kind in _WORK_KINDS:
                run_index = self._lane_candidate(tier, kind, advance=advance)
                if run_index is not None:
                    return tier, kind, run_index
        return None

    def _lane_candidate(
        self,
        tier: TrustedRunTier,
        kind: TrustedWorkKind,
        *,
        advance: bool,
    ) -> int | None:
        cursor = self._lane_cursors[(tier, kind)]
        if tier is TrustedRunTier.SELECTED:
            candidates: range | tuple[int, ...] = (
                ()
                if self._selected_index is None or self._selected_index < cursor
                else (self._selected_index,)
            )
        elif tier is TrustedRunTier.VISIBLE:
            candidates = tuple(
                index for index in self._visible_indices if index >= cursor
            )
        else:
            candidates = range(cursor, len(self._runs))

        bit = self._kind_bit(kind)
        for index in candidates:
            if self._tier_for_index(index) is not tier:
                continue
            if self._completion_masks[index] & bit:
                continue
            if (index, kind) in self._running_slots:
                continue
            if advance:
                self._lane_cursors[(tier, kind)] = index + 1
            return index
        if advance:
            self._lane_cursors[(tier, kind)] = len(self._runs)
        return None

    def _tier_for_index(self, run_index: int) -> TrustedRunTier:
        if run_index == self._selected_index:
            return TrustedRunTier.SELECTED
        if run_index in self._visible_index_set:
            return TrustedRunTier.VISIBLE
        return TrustedRunTier.REMAINING

    def _remove_claims(
        self,
        claims: Sequence[ScheduledWork],
    ) -> tuple[tuple[ScheduledWork, TrustedWorkState, TrustedWorkState], ...]:
        transitions = []
        for work in claims:
            if self._running.get(work.claim_id) is not work:
                continue
            work.cancellation.cancel()
            self._running.pop(work.claim_id)
            self._running_slots.discard((work.run_index, work.key.kind))
            transitions.append(
                (
                    work,
                    TrustedWorkState.RUNNING,
                    TrustedWorkState.CANCELLED,
                )
            )
        return tuple(transitions)

    def _emit_transitions(
        self,
        transitions: Sequence[tuple[ScheduledWork, TrustedWorkState, TrustedWorkState]],
    ) -> None:
        for work, previous, current in transitions:
            self._transition(work, previous, current)

    def _transition(
        self,
        work: ScheduledWork,
        previous: TrustedWorkState,
        current: TrustedWorkState,
    ) -> None:
        if self._on_transition is not None:
            self._on_transition(
                WorkTransition(
                    work.generation,
                    work.key,
                    previous,
                    current,
                    self._clock(),
                )
            )

    def _is_current(self, work: ScheduledWork) -> bool:
        if (
            self._lifecycle is not SchedulerLifecycle.ACTIVE
            or work.generation != self._generation
            or work.key.database_instance != self._database_instance
            or not 0 <= work.run_index < len(self._runs)
        ):
            return False
        source = self._runs[work.run_index]
        source_revision = self._revision_overrides.get(
            work.run_index,
            source.source_revision,
        )
        work_format = self._formats[work.key.kind]
        return work.key == TrustedCacheWorkKey(
            self._database_instance,
            source.run_guid,
            work.key.kind,
            source_revision,
            work_format.renderer_version,
            work_format.options,
        )

    def _reset_cursors(self) -> None:
        self._lane_cursors = {
            (tier, kind): 0 for tier in _RUN_TIERS for kind in _WORK_KINDS
        }

    @staticmethod
    def _kind_bit(kind: TrustedWorkKind) -> int:
        return 1 << int(kind)

    def _validate_run_index(self, run_index: int) -> None:
        if type(run_index) is not int or not 0 <= run_index < len(self._runs):
            raise IndexError("run_index is outside the stable run table.")

    def _require_active(self) -> None:
        if self._lifecycle is SchedulerLifecycle.CLOSED:
            raise RuntimeError("The trusted work scheduler is closed.")

    def _require_owner(self) -> None:
        if threading.get_ident() != self._owner_thread_id:
            raise RuntimeError(
                "TrustedWorkScheduler must be accessed on its owner thread; "
                "marshal executor completions back to the constructing thread."
            )

    @staticmethod
    def _validate_database_instance(instance: DatabaseInstance) -> None:
        if not isinstance(instance, DatabaseInstance) or instance.identity is None:
            raise ValueError("The scheduler requires an exact DatabaseInstance.")

    @staticmethod
    def _validated_run_table(
        runs: Sequence[TrustedRunWorkSource],
    ) -> tuple[TrustedRunWorkSource, ...]:
        if not isinstance(runs, Sequence):
            raise TypeError("runs must be a stable Sequence.")
        run_table = tuple(runs)
        if any(not isinstance(item, TrustedRunWorkSource) for item in run_table):
            raise TypeError("runs must contain TrustedRunWorkSource values.")
        guids = tuple(item.run_guid for item in run_table)
        if len(guids) != len(set(guids)):
            raise ValueError("run GUIDs must be unique within a scheduler table.")
        return run_table

    @staticmethod
    def _validated_formats(
        formats: Mapping[TrustedWorkKind, WorkFormat],
    ) -> dict[TrustedWorkKind, WorkFormat]:
        if set(formats) != set(_WORK_KINDS):
            raise ValueError("formats must define metadata, thumbnail, and preview.")
        output = dict(formats)
        if any(not isinstance(value, WorkFormat) for value in output.values()):
            raise TypeError("Every format must be a WorkFormat.")
        return output
