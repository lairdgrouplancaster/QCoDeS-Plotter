from __future__ import annotations

import queue
import sqlite3
import threading
import time
from collections import deque
from concurrent.futures import Future
from dataclasses import fields, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import qplot.datahandling.trusted_work_coordinator as coordinator_module
from qplot.datahandling.file_identity import DatabaseInstance
from qplot.datahandling.trusted_derived_cache import (
    TrustedDerivedDiskCache,
    trusted_cache_filename,
)
from qplot.datahandling.trusted_live_queries import (
    Trusted2DGridLayout,
    TrustedDerivedSourceObservation,
    TrustedParameterView,
    TrustedSourceRevision,
    TrustedSourceRevisionNamespace,
    trusted_derived_source_revision,
)
from qplot.datahandling.trusted_live_service import (
    TrustedLiveReadService,
    TrustedReadQueueFullError,
)
from qplot.datahandling.trusted_work_coordinator import (
    TRUSTED_DERIVED_MAX_REUSED_SOURCE_BYTES,
    TRUSTED_DERIVED_MAX_REUSED_SOURCES,
    TrustedDerivedRun,
    TrustedWorkCoordinator,
)
from qplot.datahandling.trusted_work_scheduler import (
    RenderingOptions,
    TrustedWorkKind,
    WorkFormat,
)


def _instance(value: int = 11) -> DatabaseInstance:
    return DatabaseInstance("/data/live.db", "/data/live.db", (7, value))


def _seed_corrupt_cache_index(root: Path, row_name: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(root / ".qplot-derived-cache-index.sqlite3")
    try:
        connection.execute(
            "CREATE TABLE cache_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) "
            "WITHOUT ROWID"
        )
        connection.execute(
            "CREATE TABLE entries (name TEXT PRIMARY KEY, modified INTEGER NOT NULL, "
            "size INTEGER NOT NULL, ready INTEGER NOT NULL) WITHOUT ROWID"
        )
        connection.execute("CREATE INDEX entries_oldest ON entries(modified, name)")
        connection.execute("INSERT INTO cache_meta VALUES('schema', '1')")
        connection.execute("INSERT INTO cache_meta VALUES('inventory_complete', '1')")
        connection.execute(
            "INSERT INTO entries VALUES(?, 0, 4096, 1)",
            (row_name,),
        )
        connection.commit()
    finally:
        connection.close()


def _observation(run_id: int, instance: DatabaseInstance, watermark: int = 8):
    return TrustedDerivedSourceObservation(
        1,
        instance,
        run_id,
        f"guid-{run_id}",
        b"fake-service",
        1,
        watermark,
        f"results-{run_id}",
        ("id", "x", "signal"),
        f"schema-{run_id}".encode(),
        watermark,
        (
            TrustedParameterView("x", "X", "V", (), "numeric"),
            TrustedParameterView("signal", "Signal", "A", ("x",), "numeric"),
        ),
        ("signal",),
        (watermark,),
        ("id", "x", "signal"),
        tuple(
            (index, float(index), float(index * 2)) for index in range(1, watermark + 1)
        ),
    )


class _Request:
    def __init__(
        self,
        result: TrustedDerivedSourceObservation,
        release: threading.Event | None = None,
    ) -> None:
        self._result = result
        self._release = release
        self._cancelled = False

    @property
    def done(self) -> bool:
        return self._cancelled or self._release is None or self._release.is_set()

    def cancel(self) -> bool:
        self._cancelled = True
        return True

    def wait(self, _timeout: float | None = None) -> TrustedDerivedSourceObservation:
        if self._cancelled:
            raise InterruptedError("fake request cancelled")
        if not self.done:
            raise TimeoutError("fake request still blocked")
        return self._result


class _Service(TrustedLiveReadService):
    def __init__(
        self,
        instance: DatabaseInstance,
        observations: dict[int, TrustedDerivedSourceObservation],
        *,
        release: threading.Event | None = None,
    ) -> None:
        self.fake_instance = instance
        self.observations = observations
        self.release = release
        self.submissions: list[int] = []
        self.namespace = TrustedSourceRevisionNamespace(b"fake-service")

    @property
    def database_instance(self) -> DatabaseInstance:
        return self.fake_instance

    @property
    def source_revision_namespace(self) -> TrustedSourceRevisionNamespace:
        return self.namespace

    def submit_derived_source(self, run_id: int, **_kwargs: Any) -> _Request:  # type: ignore[override]
        self.submissions.append(run_id)
        return _Request(self.observations[run_id], self.release)

    def close_async(self) -> bool:
        return True

    def wait_closed(self, _timeout: float | None = None) -> bool:
        return True


def _runs(observations: dict[int, TrustedDerivedSourceObservation]):
    return tuple(
        TrustedDerivedRun(
            run_id,
            observation.run_guid,
            trusted_derived_source_revision(observation),
        )
        for run_id, observation in sorted(observations.items())
    )


def _drain(coordinator: TrustedWorkCoordinator, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        coordinator.poll()
        snapshot = coordinator.snapshot()
        if (
            snapshot.pending_count == 0
            and not coordinator.active
            and coordinator._progressive_active_count == 0
            and not coordinator._progressive_restart_deferred
        ):
            return
        time.sleep(0.005)
    raise AssertionError("The trusted coordinator did not drain")


def _wait_for(event: threading.Event, timeout: float = 2.0) -> None:
    assert event.wait(timeout), "timed out waiting for the worker phase"


def _provisional_runs(observations: dict[int, TrustedDerivedSourceObservation]):
    return tuple(
        TrustedDerivedRun(run_id, observation.run_guid, TrustedSourceRevision(b"old"))
        for run_id, observation in sorted(observations.items())
    )


class _ImmediateExecutor:
    """Complete a claim synchronously while preserving owner poll boundaries."""

    def __init__(self, **_kwargs: Any) -> None:
        self.closed = False

    def submit(self, function: Any, *args: Any) -> Future[Any]:
        future: Future[Any] = Future()
        try:
            future.set_result(function(*args))
        except BaseException as error:
            future.set_exception(error)
        return future

    def shutdown(
        self,
        wait: bool = True,
        *,
        cancel_futures: bool = False,
    ) -> None:
        del wait, cancel_futures
        self.closed = True


def _install_progressive_test_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[float], list[Any]]:
    """Install one mutable clock and observable one-token progressive notifier."""

    now = [100.0]
    notifiers: list[Any] = []

    class _FakeTime:
        @staticmethod
        def monotonic() -> float:
            return now[0]

        @staticmethod
        def sleep(seconds: float) -> None:
            now[0] += seconds

    class _RecordingProgressiveNotifier:
        def __init__(self, wakeup: Any) -> None:
            self.wakeup = wakeup
            self.deadline: float | None = None
            self.created_deadlines: list[float] = []
            notifiers.append(self)

        def schedule(self, deadline: float) -> None:
            if self.deadline == deadline:
                return
            self.deadline = deadline
            self.created_deadlines.append(deadline)

        def cancel(self) -> None:
            self.deadline = None

        def fire_due(self) -> None:
            if self.deadline is None or now[0] < self.deadline:
                return
            self.deadline = None
            if self.wakeup is not None:
                self.wakeup()

    monkeypatch.setattr(coordinator_module, "time", _FakeTime)
    monkeypatch.setattr(
        coordinator_module,
        "_ProgressiveWakeupNotifier",
        _RecordingProgressiveNotifier,
    )
    return now, notifiers


def _install_recording_retry_notifier(
    monkeypatch: pytest.MonkeyPatch,
) -> list[Any]:
    notifiers: list[Any] = []

    class _RecordingRetryNotifier:
        def __init__(self, _wakeup: Any) -> None:
            self.deadline: float | None = None
            notifiers.append(self)

        def schedule(self, deadline: float) -> None:
            self.deadline = deadline

        def cancel(self) -> None:
            self.deadline = None

    monkeypatch.setattr(
        coordinator_module,
        "_RetryWakeupNotifier",
        _RecordingRetryNotifier,
    )
    return notifiers


def _grid_observation(
    run_id: int,
    instance: DatabaseInstance,
    *,
    slow_count: int = 108,
    fast_count: int = 7,
) -> TrustedDerivedSourceObservation:
    rows = tuple(
        (
            slow_index * fast_count + fast_index + 1,
            float(slow_index),
            float(fast_index),
            float(slow_index + fast_index),
        )
        for slow_index in range(slow_count)
        for fast_index in range(fast_count)
    )
    return TrustedDerivedSourceObservation(
        1,
        instance,
        run_id,
        f"guid-{run_id}",
        b"fake-service",
        1,
        1,
        f"results-{run_id}",
        ("id", "slow", "fast", "signal"),
        f"schema-{run_id}".encode(),
        len(rows),
        (
            TrustedParameterView("slow", "Slow", "V", (), "numeric"),
            TrustedParameterView("fast", "Fast", "V", (), "numeric"),
            TrustedParameterView(
                "signal",
                "Signal",
                "A",
                ("slow", "fast"),
                "numeric",
            ),
        ),
        ("signal",),
        (slow_count, fast_count),
        ("id", "slow", "fast", "signal"),
        rows,
        validated_2d_layouts=(
            Trusted2DGridLayout(
                dependent="signal",
                dependencies=("slow", "fast"),
                shape=(slow_count, fast_count),
                fast_axis_index=1,
                first_row_id=1,
                fast_id_stride=1,
                slow_id_stride=fast_count,
                sample_slow_indexes=tuple(range(slow_count)),
                sample_fast_indexes=tuple(range(fast_count)),
                slow_reversed=False,
                fast_reversed=False,
                serpentine=False,
                complete=True,
                source="observed",
            ),
        ),
    )


def _progressive_observation(
    run_id: int,
    instance: DatabaseInstance,
    *,
    page: int = 1,
) -> TrustedDerivedSourceObservation:
    return replace(
        _observation(run_id, instance, watermark=8),
        result_watermark=4096 * 300,
        planned_shape=(1_200, 1_024),
        progressive_layout_pending=True,
        progressive_layout_cursor=4096 * page,
    )


def _poll_completed_claims(
    coordinator: TrustedWorkCoordinator,
    count: int,
    *,
    maximum_owner_turns: int = 128,
) -> None:
    completed = 0
    for _turn in range(maximum_owner_turns):
        completed += coordinator.poll()
        if completed == count:
            return
        assert completed < count
    raise AssertionError(f"Only {completed} of {count} claims completed")


def test_real_coordinator_preserves_tier_first_priority_and_eventual_drain(
    tmp_path: Path,
) -> None:
    instance = _instance()
    observations = {index: _observation(index, instance) for index in range(1, 5)}
    service = _Service(instance, observations)
    publications = []
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs(observations),
        service,
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
        on_publish=publications.append,
    )
    coordinator.select_run(2)
    coordinator.set_visible_range(1, 3)
    coordinator.start()

    _drain(coordinator)
    coordinator.close()

    assert [(item.key.run_guid, item.key.kind) for item in publications] == [
        ("guid-3", TrustedWorkKind.METADATA),
        ("guid-3", TrustedWorkKind.THUMBNAIL),
        ("guid-3", TrustedWorkKind.PREVIEW),
        ("guid-2", TrustedWorkKind.METADATA),
        ("guid-2", TrustedWorkKind.THUMBNAIL),
        ("guid-2", TrustedWorkKind.PREVIEW),
        ("guid-1", TrustedWorkKind.METADATA),
        ("guid-4", TrustedWorkKind.METADATA),
        ("guid-1", TrustedWorkKind.THUMBNAIL),
        ("guid-4", TrustedWorkKind.THUMBNAIL),
        ("guid-1", TrustedWorkKind.PREVIEW),
        ("guid-4", TrustedWorkKind.PREVIEW),
    ]
    assert service.submissions == [3, 2, 1, 4]


def test_atomic_priority_cannot_claim_from_the_previous_viewport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coordinator_module, "ThreadPoolExecutor", _ImmediateExecutor)
    monkeypatch.setattr(
        coordinator_module,
        "render_trusted_derived_payload",
        _render_empty_retry_payload,
    )
    instance = _instance(52)
    observations = {run_id: _observation(run_id, instance) for run_id in range(1, 4)}
    service = _Service(instance, observations)
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs(observations),
        service,
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
    )
    try:
        # The selected run is already complete and the old viewport still
        # points at A. A two-call update would pump A in between; the atomic
        # API must make newly visible C the first claim.
        coordinator.scheduler.set_priority(1, (0,))
        coordinator._selected_index = 1
        for expected_kind in TrustedWorkKind:
            work = coordinator.scheduler.claim_next()
            assert work is not None
            assert (work.run_index, work.key.kind) == (1, expected_kind)
            assert (
                coordinator.scheduler.complete(work)
                is coordinator_module.CompletionDisposition.ACCEPTED
            )

        coordinator.set_priority(1, (2,))

        running = coordinator.snapshot().running
        assert len(running) == 1
        assert (running[0].run_index, running[0].key.kind) == (
            2,
            TrustedWorkKind.METADATA,
        )
        assert service.submissions == [3]
    finally:
        coordinator.close()


def test_selected_108_by_7_images_preempt_hundreds_of_background_pages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coordinator_module, "ThreadPoolExecutor", _ImmediateExecutor)
    instance = _instance(40)
    huge = _progressive_observation(1, instance)
    selected = _grid_observation(2, instance)

    class _HugeFirstService(_Service):
        def __init__(self) -> None:
            super().__init__(instance, {1: huge, 2: selected})
            self.huge_pages = 0

        def submit_derived_source(  # type: ignore[override]
            self,
            run_id: int,
            **_kwargs: Any,
        ) -> _Request:
            self.submissions.append(run_id)
            if run_id == 1:
                self.huge_pages += 1
                return _Request(
                    _progressive_observation(1, instance, page=self.huge_pages)
                )
            return _Request(selected)

    service = _HugeFirstService()
    publications = []
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs({1: huge, 2: selected}),
        service,
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
        on_publish=publications.append,
    )
    try:
        coordinator.start()
        assert service.submissions == [1]

        # The huge run's first 4,096-row page is already complete in the worker
        # queue.  Selection must take effect immediately after that one bounded
        # claim, rather than after the other 299 possible continuation pages.
        coordinator.select_run(1)
        _poll_completed_claims(coordinator, 4)

        selected_publications = [
            item for item in publications if item.key.run_guid == "guid-2"
        ]
        assert [item.key.kind for item in selected_publications] == list(
            TrustedWorkKind
        )
        assert service.huge_pages == 1
        for publication in selected_publications[1:]:
            images = publication.result["images"]
            assert isinstance(images, tuple) and len(images) == 1
            encoded = dict(images[0])["bytes"]
            assert isinstance(encoded, bytes)
            assert encoded.startswith(b"\x89PNG\r\n\x1a\n")
            assert len(encoded) > 100
    finally:
        coordinator.close()


def test_remaining_continuation_cannot_starve_or_republish_sibling_images(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coordinator_module, "ThreadPoolExecutor", _ImmediateExecutor)
    instance = _instance(41)
    huge = _progressive_observation(1, instance)
    visible = _observation(2, instance)

    class _NeverConclusiveService(_Service):
        def __init__(self) -> None:
            super().__init__(instance, {1: huge, 2: visible})
            self.huge_pages = 0

        def submit_derived_source(  # type: ignore[override]
            self,
            run_id: int,
            **_kwargs: Any,
        ) -> _Request:
            self.submissions.append(run_id)
            if run_id == 1:
                self.huge_pages += 1
                return _Request(
                    _progressive_observation(1, instance, page=self.huge_pages)
                )
            return _Request(visible)

    service = _NeverConclusiveService()
    publications = []
    wakeups: queue.Queue[None] = queue.Queue()
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs({1: huge, 2: visible}),
        service,
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
        wakeup=lambda: wakeups.put(None),
        on_publish=publications.append,
    )
    try:
        coordinator.start()
        _poll_completed_claims(coordinator, 2)
        assert [item.key.kind for item in publications] == [TrustedWorkKind.METADATA]

        # Admit exactly one remaining-tier continuation, then promote the
        # conclusive run while that bounded page is already in flight.
        while not wakeups.empty():
            wakeups.get_nowait()
        deadline = time.monotonic() + 1.0
        while service.huge_pages < 2 and time.monotonic() < deadline:
            coordinator.poll()
            if service.huge_pages < 2:
                wakeups.get(timeout=max(0.001, deadline - time.monotonic()))
        assert service.huge_pages == 2
        visible_publications = [
            item for item in publications if item.key.run_guid == "guid-2"
        ]
        assert [item.key.kind for item in visible_publications] == list(TrustedWorkKind)

        # Same-tier ordinary work already completed before A's continuation was
        # admitted.  Reclassifying B while that bounded page is in flight must
        # neither replay B nor allow another continuation into the same boundary.
        coordinator.set_visible_indices((1,))
        _poll_completed_claims(coordinator, 1)
        assert [item.key.kind for item in visible_publications] == list(TrustedWorkKind)
        assert (
            len([item for item in publications if item.key.run_guid == "guid-2"]) == 3
        )
        assert service.huge_pages == 2
    finally:
        coordinator.close()


def test_progressive_visible_run_cannot_gate_conclusive_visible_sibling_images(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coordinator_module, "ThreadPoolExecutor", _ImmediateExecutor)
    monkeypatch.setattr(
        coordinator_module,
        "render_trusted_derived_payload",
        _render_empty_retry_payload,
    )
    instance = _instance(45)
    progressive = _progressive_observation(1, instance)
    conclusive = _observation(2, instance)

    class _ProgressiveSiblingService(_Service):
        def __init__(self) -> None:
            super().__init__(instance, {1: progressive, 2: conclusive})
            self.progressive_pages = 0

        def submit_derived_source(  # type: ignore[override]
            self,
            run_id: int,
            **_kwargs: Any,
        ) -> _Request:
            self.submissions.append(run_id)
            if run_id == 1:
                self.progressive_pages += 1
                return _Request(
                    _progressive_observation(
                        1,
                        instance,
                        page=self.progressive_pages,
                    )
                )
            return _Request(conclusive)

    service = _ProgressiveSiblingService()
    publications = []
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs({1: progressive, 2: conclusive}),
        service,
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
        on_publish=publications.append,
    )
    try:
        coordinator.set_visible_range(0, 2)
        coordinator.start()

        # A's first page and B's conclusive metadata are the only prerequisites.
        # B's ready outputs are ordinary work in the same tier and therefore run
        # before any continuation page for indefinitely progressive A.
        _poll_completed_claims(coordinator, 4)
        sibling_publications = [
            publication
            for publication in publications
            if publication.key.run_guid == "guid-2"
        ]
        assert [publication.key.kind for publication in sibling_publications] == list(
            TrustedWorkKind
        )
        assert service.progressive_pages == 1
    finally:
        coordinator.close()


def test_remaining_append_stream_cannot_starve_same_tier_progressive_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coordinator_module, "ThreadPoolExecutor", _ImmediateExecutor)
    monkeypatch.setattr(
        coordinator_module,
        "render_trusted_derived_payload",
        _render_empty_retry_payload,
    )
    now, _notifiers = _install_progressive_test_clock(monkeypatch)
    interval = coordinator_module.TRUSTED_DERIVED_PROGRESSIVE_MIN_INTERVAL_SECONDS
    claim_bound = coordinator_module.TRUSTED_DERIVED_SAME_TIER_PROGRESSIVE_CLAIM_BOUND
    assert claim_bound == 3
    assert claim_bound <= 8
    instance = _instance(64)
    observations = {
        1: _progressive_observation(1, instance),
        **{run_id: _observation(run_id, instance) for run_id in range(2, 6)},
    }

    class _AppendStreamService(_Service):
        def __init__(self) -> None:
            super().__init__(instance, observations)
            self.progressive_pages = 0

        def submit_derived_source(  # type: ignore[override]
            self,
            run_id: int,
            **_kwargs: Any,
        ) -> _Request:
            self.submissions.append(run_id)
            if run_id == 1:
                self.progressive_pages += 1
                return _Request(
                    _progressive_observation(
                        1,
                        instance,
                        page=self.progressive_pages,
                    )
                )
            return _Request(observations[run_id])

    service = _AppendStreamService()
    publications = []
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs({1: observations[1]}),
        service,
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
        on_publish=publications.append,
    )

    def append_run(run_id: int) -> None:
        coordinator.reconcile_runs(
            (*coordinator.runs, _runs({run_id: observations[run_id]})[0])
        )

    try:
        coordinator.start()
        assert service.submissions == [1]
        assert coordinator.poll() == 1
        assert not coordinator.active
        assert coordinator._progressive_restart_deferred
        deadline = coordinator._progressive_not_before
        assert deadline == pytest.approx(now[0] + interval)

        # B wins the same-tier ordinary quantum and drains its complete M/T/P
        # sequence promptly.  More ordinary siblings arrive while B's final
        # claim is active, so work remains available to expose starvation.
        append_run(2)
        ordinary_claims = []
        for expected_kind in TrustedWorkKind:
            running = coordinator.snapshot().running
            assert len(running) == 1
            assert (running[0].run_index, running[0].key.kind) == (
                1,
                expected_kind,
            )
            ordinary_claims.append(running[0])
            if expected_kind is TrustedWorkKind.PREVIEW:
                append_run(3)
                append_run(4)
            assert coordinator.poll() == 1

        sibling_publications = [
            publication
            for publication in publications
            if publication.key.run_guid == "guid-2"
        ]
        assert [publication.key.kind for publication in sibling_publications] == list(
            TrustedWorkKind
        )
        assert len(ordinary_claims) == claim_bound
        assert coordinator._ordinary_claims_since_progressive_page == claim_bound
        assert not coordinator.active

        # Even an append at the idle paced boundary cannot reset the quantum
        # or sneak another ordinary claim ahead of A's already-due page.
        append_run(5)
        assert not coordinator.active
        assert service.submissions == [1, 2]
        now[0] = deadline
        assert coordinator.poll() == 0

        running = coordinator.snapshot().running
        assert len(running) == 1
        assert (running[0].run_index, running[0].key.kind) == (
            0,
            TrustedWorkKind.METADATA,
        )
        assert service.submissions == [1, 2, 1]
        assert len(ordinary_claims) <= claim_bound
    finally:
        coordinator.close()


def test_newly_progressive_selected_guid_enters_existing_round_next(
    tmp_path: Path,
) -> None:
    instance = _instance(46)
    observations = {
        run_id: _progressive_observation(run_id, instance) for run_id in range(1, 4)
    }
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs(observations),
        _Service(instance, observations),
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
    )
    try:
        # Establish an older remaining-tier round without starting the worker.
        coordinator.scheduler.select_run(2)
        coordinator._selected_index = 2
        for run_id in (1, 2):
            observation = observations[run_id]
            coordinator._adopt_observation(
                observation,
                trusted_derived_source_revision(observation),
                coordinator._observation_size(observation),
            )
        snapshot = coordinator.snapshot()
        assert coordinator._next_progressive_run_index(snapshot) == 0
        assert not coordinator._progressive_is_unserved(0)
        assert coordinator._progressive_is_unserved(1)

        # The selected run becomes progressive after the round was built.  Its
        # dependency must enter at the front at the next bounded page boundary.
        selected = observations[3]
        coordinator._adopt_observation(
            selected,
            trusted_derived_source_revision(selected),
            coordinator._observation_size(selected),
        )
        assert coordinator._next_progressive_run_index(snapshot) == 2
        assert coordinator._progressive_is_unserved(1)
        assert coordinator._progressive_active_count == 3
    finally:
        coordinator.close()


def test_newly_visible_active_progressive_run_is_next_at_page_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coordinator_module, "ThreadPoolExecutor", _ImmediateExecutor)
    now, notifiers = _install_progressive_test_clock(monkeypatch)
    interval = coordinator_module.TRUSTED_DERIVED_PROGRESSIVE_MIN_INTERVAL_SECONDS
    instance = _instance(59)
    first_pages = {
        run_id: _progressive_observation(run_id, instance) for run_id in (1, 2)
    }

    class _VisiblePromotionService(_Service):
        def __init__(self) -> None:
            super().__init__(instance, first_pages)
            self.page_counts = {1: 0, 2: 0}

        def submit_derived_source(  # type: ignore[override]
            self,
            run_id: int,
            **_kwargs: Any,
        ) -> _Request:
            self.submissions.append(run_id)
            self.page_counts[run_id] += 1
            return _Request(
                _progressive_observation(
                    run_id,
                    instance,
                    page=self.page_counts[run_id],
                )
            )

    service = _VisiblePromotionService()
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs(first_pages),
        service,
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
    )
    try:
        coordinator.start()
        assert coordinator.poll() == 1
        assert coordinator.poll() == 1
        assert service.submissions == [1, 2]
        assert len(notifiers) == 1

        # A consumes its current remaining-round quantum.  Promote it while
        # that page is already in flight; B is otherwise the next unserved
        # remaining run, so the following boundary exposes whether viewport
        # promotion was adopted or merely recorded for a later full round.
        first_deadline = coordinator._progressive_not_before
        assert first_deadline == pytest.approx(now[0] + interval)
        now[0] = first_deadline
        assert coordinator.poll() == 0
        assert service.submissions == [1, 2, 1]
        coordinator.set_visible_indices((0,))
        assert service.submissions == [1, 2, 1]
        assert coordinator.poll() == 1
        assert not coordinator.active

        second_deadline = coordinator._progressive_not_before
        assert second_deadline == pytest.approx(now[0] + interval)
        now[0] = second_deadline
        assert coordinator.poll() == 0
        assert service.submissions == [1, 2, 1, 1]
        running = coordinator.snapshot().running
        assert len(running) == 1
        assert running[0].run_index == 0
        assert running[0].key.kind is TrustedWorkKind.METADATA
    finally:
        coordinator.close()


def test_continuous_selection_churn_admits_background_metadata_within_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coordinator_module, "ThreadPoolExecutor", _ImmediateExecutor)
    monkeypatch.setattr(
        coordinator_module,
        "render_trusted_derived_payload",
        _render_empty_retry_payload,
    )
    # The production diagnostic is part of the acceptance contract.  The
    # fallback lets this regression exercise the broken pre-repair behavior
    # before the named constant exists.
    claim_bound = getattr(
        coordinator_module,
        "TRUSTED_DERIVED_BACKGROUND_AGING_CLAIM_BOUND",
        8,
    )
    assert type(claim_bound) is int and 1 <= claim_bound <= 8
    instance = _instance(42)
    observations = {
        1: _observation(1, instance),
        2: _observation(2, instance),
    }
    service = _Service(instance, observations)
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs(observations),
        service,
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
    )
    try:
        # Complete the foreground run's whole output sequence first.  Churn
        # below therefore creates metadata-only detail replays; no selected
        # thumbnail/preview is ready to invoke the stronger image protection.
        coordinator.scheduler.select_run(0)
        for expected_kind in TrustedWorkKind:
            work = coordinator.scheduler.claim_next()
            assert work is not None
            assert work.run_index == 0
            assert work.key.kind is expected_kind
            coordinator.scheduler.complete(work, {"preseeded": expected_kind.name})
        foreground = observations[1]
        coordinator._adopt_observation(
            foreground,
            trusted_derived_source_revision(foreground),
            coordinator._observation_size(foreground),
        )
        coordinator._selected_index = 0
        assert coordinator.scheduler.request_completed_work(
            0,
            TrustedWorkKind.METADATA,
            database_instance=coordinator.scheduler.database_instance,
            generation=coordinator.scheduler.generation,
            run_guid="guid-1",
        )
        coordinator.start()
        assert coordinator.snapshot().running[0].run_index == 0
        assert service.submissions == []

        admitted_after: int | None = None
        for completed_claims in range(1, claim_bound + 1):
            # Re-select before every queued selected-metadata completion.  This
            # invalidates its ephemeral detail attempt and makes more selected
            # work ready without introducing another executor or timer.
            coordinator.select_run(None)
            coordinator.select_run(0)
            assert coordinator.poll() == 1
            if 2 in service.submissions:
                admitted_after = completed_claims
                break

        assert admitted_after is not None
        assert admitted_after <= claim_bound
        assert service.submissions == [2]
        assert (
            coordinator_module.TRUSTED_DERIVED_BACKGROUND_AGING_CLAIM_BOUND
            == claim_bound
        )
    finally:
        coordinator.close()


def test_aged_progressive_and_ordinary_background_metadata_rotate_under_churn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coordinator_module, "ThreadPoolExecutor", _ImmediateExecutor)
    monkeypatch.setattr(
        coordinator_module,
        "render_trusted_derived_payload",
        _render_empty_retry_payload,
    )
    now = [100.0]

    class _FakeTime:
        @staticmethod
        def monotonic() -> float:
            return now[0]

        @staticmethod
        def sleep(seconds: float) -> None:
            now[0] += seconds

    class _RecordingProgressiveNotifier:
        def __init__(self, _wakeup: Any) -> None:
            self.deadline: float | None = None

        def schedule(self, deadline: float) -> None:
            self.deadline = deadline

        def cancel(self) -> None:
            self.deadline = None

    monkeypatch.setattr(coordinator_module, "time", _FakeTime)
    monkeypatch.setattr(
        coordinator_module,
        "_ProgressiveWakeupNotifier",
        _RecordingProgressiveNotifier,
    )
    instance = _instance(48)
    progressive = _progressive_observation(1, instance)
    ordinary = _observation(2, instance)
    foreground = _observation(3, instance)
    observations = {1: progressive, 2: ordinary, 3: foreground}

    class _RotatingBackgroundService(_Service):
        def __init__(self) -> None:
            super().__init__(instance, observations)
            self.progressive_pages = 0

        def submit_derived_source(  # type: ignore[override]
            self,
            run_id: int,
            **_kwargs: Any,
        ) -> _Request:
            self.submissions.append(run_id)
            if run_id == 1:
                self.progressive_pages += 1
                return _Request(
                    _progressive_observation(
                        1,
                        instance,
                        page=self.progressive_pages + 1,
                    )
                )
            return _Request(self.observations[run_id])

    service = _RotatingBackgroundService()
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs(observations),
        service,
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
    )
    try:
        # Park A and precomplete every foreground output without consuming the
        # worker.  B remains ordinary pending metadata in the same background
        # tier as A's continuation.
        coordinator.scheduler.select_run(0)
        parked = coordinator.scheduler.claim_next()
        assert parked is not None
        progressive_revision = trusted_derived_source_revision(progressive)
        assert (
            coordinator.scheduler.park_progressive_metadata(
                parked,
                progressive_revision,
            )
            is coordinator_module.CompletionDisposition.ACCEPTED
        )
        coordinator._adopt_observation(
            progressive,
            progressive_revision,
            coordinator._observation_size(progressive),
        )
        coordinator.scheduler.select_run(2)
        for expected_kind in TrustedWorkKind:
            work = coordinator.scheduler.claim_next()
            assert work is not None
            assert work.run_index == 2
            assert work.key.kind is expected_kind
            coordinator.scheduler.complete(work, {"preseeded": expected_kind.name})
        foreground_revision = trusted_derived_source_revision(foreground)
        coordinator._adopt_observation(
            foreground,
            foreground_revision,
            coordinator._observation_size(foreground),
        )
        coordinator._selected_index = 2

        # The first aged quantum is A's bounded continuation.
        coordinator._foreground_claims_since_background_metadata = (
            coordinator_module.TRUSTED_DERIVED_BACKGROUND_AGING_CLAIM_BOUND
        )
        coordinator.start()
        assert service.submissions == [1]
        assert coordinator.scheduler.request_completed_work(
            2,
            TrustedWorkKind.METADATA,
            database_instance=coordinator.scheduler.database_instance,
            generation=coordinator.scheduler.generation,
            run_guid="guid-3",
        )
        assert coordinator.poll() == 1
        assert coordinator.snapshot().running[0].run_index == 2

        # Sustained selected metadata churn reaches the fixed bound again.  The
        # next aged quantum must rotate to ordinary B rather than reopening A
        # for a second consecutive page.
        claim_bound = coordinator_module.TRUSTED_DERIVED_BACKGROUND_AGING_CLAIM_BOUND
        for completed_claims in range(1, claim_bound + 1):
            coordinator.select_run(None)
            coordinator.select_run(2)
            assert coordinator.poll() == 1
            if completed_claims < claim_bound:
                assert coordinator.snapshot().running[0].run_index == 2

        assert service.submissions == [1, 2]
        assert service.progressive_pages == 1
        assert coordinator.snapshot().running[0].run_index == 1
        assert coordinator.snapshot().running[0].key.kind is TrustedWorkKind.METADATA
        assert coordinator._foreground_claims_since_background_metadata == 0

        # Complete B's sole metadata quantum, then sustain the same foreground
        # pressure for one more bound.  A must become the next aged source, so
        # the bounded background sequence is A, B, A rather than either source
        # monopolising every admission opportunity.
        assert coordinator.poll() == 1
        assert coordinator.snapshot().running[0].run_index == 2
        for completed_claims in range(1, claim_bound + 1):
            coordinator.select_run(None)
            coordinator.select_run(2)
            assert coordinator.poll() == 1
            if completed_claims < claim_bound:
                assert coordinator.snapshot().running[0].run_index == 2
        assert not coordinator.active
        assert coordinator._progressive_restart_deferred
        now[0] += coordinator_module.TRUSTED_DERIVED_PROGRESSIVE_MIN_INTERVAL_SECONDS
        assert coordinator.poll() == 0
        assert service.submissions == [1, 2, 1]
        assert service.progressive_pages == 2
        assert coordinator.snapshot().running[0].run_index == 0
        assert coordinator.snapshot().running[0].key.kind is TrustedWorkKind.METADATA
    finally:
        coordinator.close()


def test_selected_metadata_replay_and_images_precede_aged_background_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coordinator_module, "ThreadPoolExecutor", _ImmediateExecutor)
    rendered: list[tuple[str, TrustedWorkKind]] = []

    def render(
        observation: TrustedDerivedSourceObservation,
        kind: TrustedWorkKind,
        options: object,
        *,
        cancel_check: Any,
    ) -> object:
        rendered.append((observation.run_guid, kind))
        return _render_empty_retry_payload(
            observation,
            kind,
            options,
            cancel_check=cancel_check,
        )

    monkeypatch.setattr(coordinator_module, "render_trusted_derived_payload", render)
    instance = _instance(49)
    background = _observation(1, instance)
    selected = _observation(2, instance)
    observations = {1: background, 2: selected}
    service = _Service(instance, observations)
    publications = []
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs(observations),
        service,
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
        on_publish=publications.append,
    )
    try:
        # Metadata is conclusive and retained, but the selected run's images are
        # still pending.  Reopen metadata exactly as selected-detail replay does.
        coordinator.scheduler.select_run(1)
        metadata = coordinator.scheduler.claim_next()
        assert metadata is not None
        assert metadata.run_index == 1
        assert metadata.key.kind is TrustedWorkKind.METADATA
        coordinator.scheduler.complete(metadata, {"preseeded": "metadata"})
        publications.clear()
        selected_revision = trusted_derived_source_revision(selected)
        coordinator._adopt_observation(
            selected,
            selected_revision,
            coordinator._observation_size(selected),
        )
        coordinator._selected_index = 1
        assert coordinator.scheduler.request_completed_work(
            1,
            TrustedWorkKind.METADATA,
            database_instance=coordinator.scheduler.database_instance,
            generation=coordinator.scheduler.generation,
            run_guid="guid-2",
        )
        coordinator._foreground_claims_since_background_metadata = (
            coordinator_module.TRUSTED_DERIVED_BACKGROUND_AGING_CLAIM_BOUND
        )

        coordinator.start()
        assert coordinator.snapshot().running[0].run_index == 1
        assert coordinator.snapshot().running[0].key.kind is TrustedWorkKind.METADATA
        assert service.submissions == []

        # Aging cannot split the selected M replay -> T -> P sequence.  Only
        # after preview completes may the pending background metadata run.
        assert coordinator.poll() == 1
        assert coordinator.snapshot().running[0].key.kind is TrustedWorkKind.THUMBNAIL
        assert coordinator.poll() == 1
        assert coordinator.snapshot().running[0].key.kind is TrustedWorkKind.PREVIEW
        assert coordinator.poll() == 1
        assert service.submissions == [1]
        assert coordinator.snapshot().running[0].run_index == 0
        assert coordinator.snapshot().running[0].key.kind is TrustedWorkKind.METADATA
        assert rendered == [
            ("guid-2", TrustedWorkKind.METADATA),
            ("guid-2", TrustedWorkKind.THUMBNAIL),
            ("guid-2", TrustedWorkKind.PREVIEW),
            ("guid-1", TrustedWorkKind.METADATA),
        ]
        assert [publication.key.kind for publication in publications] == list(
            TrustedWorkKind
        )
    finally:
        coordinator.close()


def test_selected_progressive_continuation_survives_aging_bound_without_background(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coordinator_module, "ThreadPoolExecutor", _ImmediateExecutor)
    now, notifiers = _install_progressive_test_clock(monkeypatch)
    interval = coordinator_module.TRUSTED_DERIVED_PROGRESSIVE_MIN_INTERVAL_SECONDS
    claim_bound = coordinator_module.TRUSTED_DERIVED_BACKGROUND_AGING_CLAIM_BOUND
    instance = _instance(51)
    first_page = _progressive_observation(1, instance)

    class _SelectedProgressiveService(_Service):
        def __init__(self) -> None:
            super().__init__(instance, {1: first_page})
            self.page_count = 0

        def submit_derived_source(  # type: ignore[override]
            self,
            run_id: int,
            **_kwargs: Any,
        ) -> _Request:
            self.submissions.append(run_id)
            self.page_count += 1
            return _Request(
                _progressive_observation(run_id, instance, page=self.page_count)
            )

    service = _SelectedProgressiveService()
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs({1: first_page}),
        service,
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
    )
    try:
        coordinator.select_run(0)
        assert service.page_count == 1
        assert len(notifiers) == 1
        notifier = notifiers[0]

        # The initial selected claim plus seven continuations saturate the
        # foreground aging counter.  With no background source, the next page
        # remains eligible; it must not be filtered out and orphaned merely
        # because the counter reached its bound.
        for completed_claims in range(1, claim_bound + 1):
            assert coordinator.poll() == 1
            assert not coordinator.active
            assert coordinator._progressive_restart_deferred
            deadline = coordinator._progressive_not_before
            assert deadline == pytest.approx(now[0] + interval)
            assert notifier.deadline == deadline

            scheduled = tuple(notifier.created_deadlines)
            for _owner_turn in range(3):
                assert coordinator.poll() == 0
                assert service.page_count == completed_claims
                assert tuple(notifier.created_deadlines) == scheduled

            now[0] = deadline
            assert coordinator.poll() == 0
            assert service.page_count == completed_claims + 1
            assert coordinator.active

        assert service.page_count == claim_bound + 1
        assert coordinator._foreground_claims_since_background_metadata == claim_bound
    finally:
        coordinator.close()


def test_selected_progressive_yields_remaining_metadata_by_aging_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coordinator_module, "ThreadPoolExecutor", _ImmediateExecutor)
    now, notifiers = _install_progressive_test_clock(monkeypatch)
    interval = coordinator_module.TRUSTED_DERIVED_PROGRESSIVE_MIN_INTERVAL_SECONDS
    claim_bound = coordinator_module.TRUSTED_DERIVED_BACKGROUND_AGING_CLAIM_BOUND
    instance = _instance(52)
    remaining = _observation(1, instance)
    selected = _progressive_observation(2, instance)

    class _SelectedAndRemainingService(_Service):
        def __init__(self) -> None:
            super().__init__(instance, {1: remaining, 2: selected})
            self.selected_pages = 0

        def submit_derived_source(  # type: ignore[override]
            self,
            run_id: int,
            **_kwargs: Any,
        ) -> _Request:
            self.submissions.append(run_id)
            if run_id == 2:
                self.selected_pages += 1
                return _Request(
                    _progressive_observation(
                        2,
                        instance,
                        page=self.selected_pages,
                    )
                )
            return _Request(remaining)

    service = _SelectedAndRemainingService()
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs({1: remaining, 2: selected}),
        service,
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
    )
    try:
        coordinator.select_run(1)
        assert service.submissions == [2]
        assert len(notifiers) == 1

        admitted_after: int | None = None
        for completed_claims in range(1, claim_bound + 1):
            assert coordinator.poll() == 1
            if service.submissions[-1] == 1:
                admitted_after = completed_claims
                break
            assert not coordinator.active
            deadline = coordinator._progressive_not_before
            assert deadline == pytest.approx(now[0] + interval)
            now[0] = deadline
            assert coordinator.poll() == 0
            assert service.submissions == [2] * (completed_claims + 1)

        assert admitted_after is not None
        assert admitted_after <= claim_bound
        assert service.submissions == [2] * claim_bound + [1]
        running = coordinator.snapshot().running
        assert len(running) == 1
        assert running[0].run_index == 0
        assert running[0].key.kind is TrustedWorkKind.METADATA
    finally:
        coordinator.close()


def test_retry_blocked_remaining_metadata_does_not_hide_ordinary_aging_peer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coordinator_module, "ThreadPoolExecutor", _ImmediateExecutor)
    monkeypatch.setattr(
        coordinator_module,
        "render_trusted_derived_payload",
        _render_empty_retry_payload,
    )
    now, _progressive_notifiers = _install_progressive_test_clock(monkeypatch)
    retry_notifiers = _install_recording_retry_notifier(monkeypatch)
    instance = _instance(57)
    observations = {run_id: _observation(run_id, instance) for run_id in range(1, 4)}

    class _FirstRemainingRetryService(_Service):
        def __init__(self) -> None:
            super().__init__(instance, observations)
            self.fail_a_once = True

        def submit_derived_source(  # type: ignore[override]
            self,
            run_id: int,
            **_kwargs: Any,
        ) -> _Request:
            self.submissions.append(run_id)
            if run_id == 1 and self.fail_a_once:
                self.fail_a_once = False
                raise TrustedReadQueueFullError("A is retry delayed")
            return _Request(self.observations[run_id])

    service = _FirstRemainingRetryService()
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs(observations),
        service,
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
    )
    try:
        # Precomplete C so its later selected replay is metadata-only churn and
        # cannot invoke the stronger pending-image protection.
        coordinator.scheduler.select_run(2)
        for expected_kind in TrustedWorkKind:
            work = coordinator.scheduler.claim_next()
            assert work is not None
            assert work.run_index == 2
            assert work.key.kind is expected_kind
            coordinator.scheduler.complete(work, {"preseeded": expected_kind.name})

        # Put A in flight, then select/reopen C before A's queued failure is
        # handled.  At the aging boundary A is the first stable remaining M but
        # is in backoff; the lookup must continue to eligible peer B.
        coordinator.scheduler.select_run(0)
        coordinator._selected_index = 0
        coordinator.start()
        assert service.submissions == [1]
        coordinator.select_run(2)
        coordinator._foreground_claims_since_background_metadata = (
            coordinator_module.TRUSTED_DERIVED_BACKGROUND_AGING_CLAIM_BOUND
        )

        assert coordinator.poll() == 1
        assert len(retry_notifiers) == 1
        assert retry_notifiers[0].deadline == pytest.approx(now[0] + 0.025)
        assert service.submissions == [1, 2]
        running = coordinator.snapshot().running
        assert len(running) == 1
        assert running[0].run_index == 1
        assert running[0].key.kind is TrustedWorkKind.METADATA
    finally:
        coordinator.close()


def test_retry_blocked_remaining_metadata_does_not_hide_progressive_aging_peer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coordinator_module, "ThreadPoolExecutor", _ImmediateExecutor)
    monkeypatch.setattr(
        coordinator_module,
        "render_trusted_derived_payload",
        _render_empty_retry_payload,
    )
    now, progressive_notifiers = _install_progressive_test_clock(monkeypatch)
    retry_notifiers = _install_recording_retry_notifier(monkeypatch)
    instance = _instance(58)
    blocked = _observation(1, instance)
    progressive = _progressive_observation(2, instance)
    terminal = replace(
        progressive,
        progressive_layout_pending=False,
        progressive_layout_cursor=0,
    )
    foreground = _observation(3, instance)
    observations = {1: blocked, 2: progressive, 3: foreground}

    class _BlockedThenProgressiveService(_Service):
        def __init__(self) -> None:
            super().__init__(instance, observations)
            self.fail_a_once = True

        def submit_derived_source(  # type: ignore[override]
            self,
            run_id: int,
            **_kwargs: Any,
        ) -> _Request:
            self.submissions.append(run_id)
            if run_id == 1 and self.fail_a_once:
                self.fail_a_once = False
                raise TrustedReadQueueFullError("A is retry delayed")
            return _Request(terminal if run_id == 2 else foreground)

    service = _BlockedThenProgressiveService()
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs(observations),
        service,
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
    )
    try:
        # Park B as an active remaining continuation and precomplete C for
        # metadata-only selected churn, all without consuming a worker turn.
        coordinator.scheduler.select_run(1)
        parked = coordinator.scheduler.claim_next()
        assert parked is not None
        progressive_revision = trusted_derived_source_revision(progressive)
        assert (
            coordinator.scheduler.park_progressive_metadata(
                parked,
                progressive_revision,
            )
            is coordinator_module.CompletionDisposition.ACCEPTED
        )
        coordinator._adopt_observation(
            progressive,
            progressive_revision,
            coordinator._observation_size(progressive),
        )
        coordinator.scheduler.select_run(2)
        for expected_kind in TrustedWorkKind:
            work = coordinator.scheduler.claim_next()
            assert work is not None
            assert work.run_index == 2
            assert work.key.kind is expected_kind
            coordinator.scheduler.complete(work, {"preseeded": expected_kind.name})

        coordinator.scheduler.select_run(0)
        coordinator._selected_index = 0
        coordinator.start()
        assert service.submissions == [1]
        coordinator.select_run(2)
        coordinator._foreground_claims_since_background_metadata = (
            coordinator_module.TRUSTED_DERIVED_BACKGROUND_AGING_CLAIM_BOUND
        )

        # A's retry deadline is 25 ms.  B remains independently discoverable
        # and receives the aged quantum at the earlier progressive boundary.
        assert coordinator.poll() == 1
        assert len(retry_notifiers) == 1
        retry_deadline = retry_notifiers[0].deadline
        assert retry_deadline == pytest.approx(now[0] + 0.025)
        assert len(progressive_notifiers) == 1
        progressive_deadline = progressive_notifiers[0].deadline
        assert progressive_deadline == pytest.approx(
            now[0] + coordinator_module.TRUSTED_DERIVED_PROGRESSIVE_MIN_INTERVAL_SECONDS
        )
        assert progressive_deadline < retry_deadline
        assert service.submissions == [1]

        now[0] = progressive_deadline
        assert coordinator.poll() == 0
        assert service.submissions == [1, 2]
        assert now[0] < retry_deadline
        running = coordinator.snapshot().running
        assert len(running) == 1
        assert running[0].run_index == 1
        assert running[0].key.kind is TrustedWorkKind.METADATA
    finally:
        coordinator.close()


def test_ordinary_aging_skips_served_stale_progressive_slot_for_unserved_peer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coordinator_module, "ThreadPoolExecutor", _ImmediateExecutor)
    instance = _instance(60)
    progressive_a = _progressive_observation(1, instance)
    progressive_b = _progressive_observation(2, instance)
    foreground = _observation(3, instance)
    observations = {1: progressive_a, 2: progressive_b, 3: foreground}
    service = _Service(instance, observations)
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs(observations),
        service,
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
    )
    try:
        # A and B are active progressive sources in one round.  A has already
        # consumed its quantum; abandoning its reopened M claim reproduces the
        # exact scheduler state left by a STALE continuation outcome.
        coordinator.scheduler.select_run(0)
        parked_a = coordinator.scheduler.claim_next()
        assert parked_a is not None
        revision_a = trusted_derived_source_revision(progressive_a)
        assert (
            coordinator.scheduler.park_progressive_metadata(parked_a, revision_a)
            is coordinator_module.CompletionDisposition.ACCEPTED
        )
        coordinator._adopt_observation(
            progressive_a,
            revision_a,
            coordinator._observation_size(progressive_a),
        )
        coordinator._mark_progressive_served(0, coordinator.snapshot())
        assert coordinator.scheduler.request_completed_work(
            0,
            TrustedWorkKind.METADATA,
            database_instance=instance,
            generation=coordinator.scheduler.generation,
            run_guid="guid-1",
        )
        stale_a = coordinator.scheduler.claim_pending(0, TrustedWorkKind.METADATA)
        assert stale_a is not None
        assert (
            coordinator.scheduler.abandon(stale_a)
            is coordinator_module.CompletionDisposition.ACCEPTED
        )

        coordinator.scheduler.select_run(1)
        parked_b = coordinator.scheduler.claim_next()
        assert parked_b is not None
        revision_b = trusted_derived_source_revision(progressive_b)
        assert (
            coordinator.scheduler.park_progressive_metadata(parked_b, revision_b)
            is coordinator_module.CompletionDisposition.ACCEPTED
        )
        coordinator._adopt_observation(
            progressive_b,
            revision_b,
            coordinator._observation_size(progressive_b),
        )
        assert not coordinator._progressive_is_unserved(0)
        assert coordinator._progressive_is_unserved(1)

        # Precomplete C, reopen only selected metadata, and arrange for the
        # ordinary half of aging rotation.  A is pending in the ordinary lane
        # but remains progressive and therefore inadmissible there; B must win.
        coordinator.scheduler.select_run(2)
        for expected_kind in TrustedWorkKind:
            work = coordinator.scheduler.claim_next()
            assert work is not None
            assert work.run_index == 2
            assert work.key.kind is expected_kind
            coordinator.scheduler.complete(work, {"preseeded": expected_kind.name})
        assert coordinator.scheduler.request_completed_work(
            2,
            TrustedWorkKind.METADATA,
            database_instance=instance,
            generation=coordinator.scheduler.generation,
            run_guid="guid-3",
        )
        coordinator._selected_index = 2
        coordinator._foreground_claims_since_background_metadata = (
            coordinator_module.TRUSTED_DERIVED_BACKGROUND_AGING_CLAIM_BOUND
        )
        coordinator._age_progressive_background_next = False

        coordinator.start()
        assert service.submissions == [2]
        running = coordinator.snapshot().running
        assert len(running) == 1
        assert running[0].run_index == 1
        assert running[0].key.kind is TrustedWorkKind.METADATA
        assert coordinator._progressive_is_active(0)
        assert coordinator._progressive_is_active(1)
    finally:
        coordinator.close()


def test_two_transient_slots_keep_independent_retry_deadlines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coordinator_module, "ThreadPoolExecutor", _ImmediateExecutor)
    monkeypatch.setattr(
        coordinator_module,
        "render_trusted_derived_payload",
        _render_empty_retry_payload,
    )
    now, _progressive_notifiers = _install_progressive_test_clock(monkeypatch)
    retry_notifiers = _install_recording_retry_notifier(monkeypatch)
    instance = _instance(61)
    observations = {run_id: _observation(run_id, instance) for run_id in (1, 2)}

    class _EachSlotFailsOnceService(_Service):
        def __init__(self) -> None:
            super().__init__(instance, observations)
            self.failed: set[int] = set()
            self.submission_times: list[tuple[int, float]] = []

        def submit_derived_source(  # type: ignore[override]
            self,
            run_id: int,
            **_kwargs: Any,
        ) -> _Request:
            self.submissions.append(run_id)
            self.submission_times.append((run_id, now[0]))
            if run_id not in self.failed:
                self.failed.add(run_id)
                raise TrustedReadQueueFullError(f"run {run_id} is transient")
            return _Request(self.observations[run_id])

    service = _EachSlotFailsOnceService()
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs(observations),
        service,
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
    )
    try:
        # Precomplete both image lanes so successful metadata retries do not
        # introduce unrelated T/P priority between the two exact deadlines.
        for run_index in (0, 1):
            coordinator.scheduler.select_run(run_index)
            for expected_kind in TrustedWorkKind:
                work = coordinator.scheduler.claim_next()
                assert work is not None
                assert work.run_index == run_index
                assert work.key.kind is expected_kind
                coordinator.scheduler.complete(
                    work,
                    {"preseeded": expected_kind.name},
                )
        coordinator.scheduler.select_run(None)
        assert coordinator.scheduler.request_completed_work(
            0,
            TrustedWorkKind.METADATA,
            database_instance=instance,
            generation=coordinator.scheduler.generation,
            run_guid="guid-1",
        )

        coordinator.select_run(0)
        assert service.submissions == [1]
        coordinator.select_run(1)
        assert coordinator.poll() == 1
        assert service.submissions == [1, 2]
        assert coordinator.poll() == 1
        assert service.submissions == [1, 2]
        assert len(retry_notifiers) == 1
        assert retry_notifiers[0].deadline == pytest.approx(100.025)

        # B becoming the newest failed slot must not erase A's own deadline.
        # Re-selecting either slot, plus arbitrary owner polls, remains inert
        # while the frozen clock is still before 100.025.
        coordinator.select_run(0)
        assert service.submissions == [1, 2]
        coordinator.select_run(1)
        assert service.submissions == [1, 2]
        coordinator.select_run(0)
        for _owner_turn in range(5):
            assert coordinator.poll() == 0
            assert service.submissions == [1, 2]
        now[0] = 100.024
        assert coordinator.poll() == 0
        assert service.submissions == [1, 2]

        # At their exact common boundary A succeeds first.  Clearing A's state
        # cannot clear B's: B is admitted immediately afterwards at the same
        # monotonic instant, proving both bounded retry records survived.
        now[0] = 100.025
        assert coordinator.poll() == 0
        assert service.submissions == [1, 2, 1]
        assert coordinator.poll() == 1
        assert service.submissions == [1, 2, 1, 2]
        assert service.submission_times == [
            (1, 100.0),
            (2, 100.0),
            (1, 100.025),
            (2, 100.025),
        ]
    finally:
        coordinator.close()


def test_deferred_changes_between_progressive_pages_remain_paced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coordinator_module, "ThreadPoolExecutor", _ImmediateExecutor)
    now, notifiers = _install_progressive_test_clock(monkeypatch)
    interval = coordinator_module.TRUSTED_DERIVED_PROGRESSIVE_MIN_INTERVAL_SECONDS
    instance = _instance(62)
    first_page = _progressive_observation(1, instance)

    class _ChangingProgressiveService(_Service):
        def __init__(self) -> None:
            super().__init__(instance, {1: first_page})
            self.returned: list[TrustedDerivedSourceObservation] = []
            self.submission_times: list[float] = []

        def submit_derived_source(  # type: ignore[override]
            self,
            run_id: int,
            **_kwargs: Any,
        ) -> _Request:
            self.submissions.append(run_id)
            self.submission_times.append(now[0])
            observation = _progressive_observation(
                run_id,
                instance,
                page=len(self.submissions),
            )
            self.returned.append(observation)
            return _Request(observation)

    service = _ChangingProgressiveService()
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs({1: first_page}),
        service,
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
    )
    try:
        coordinator.start()
        assert service.submission_times == [100.0]
        assert len(notifiers) == 1
        notifier = notifiers[0]

        for completed_pages in range(1, 4):
            stale_observation = service.returned[-1]
            stale_revision = trusted_derived_source_revision(stale_observation)
            coordinator.source_changed(0)
            assert coordinator.poll() == 1

            # The just-finished prefix was superseded before owner adoption.
            # It must neither survive in the retained LRU nor become the run's
            # current revision while the replacement waits for its boundary.
            assert "guid-1" not in coordinator._reused_observations
            assert coordinator.runs[0].source_revision != stale_revision
            assert len(service.submissions) == completed_pages
            assert not coordinator.active
            assert coordinator._progressive_restart_deferred
            deadline = coordinator._progressive_not_before
            assert deadline == pytest.approx(now[0] + interval)
            assert notifier.deadline == deadline

            scheduled = tuple(notifier.created_deadlines)
            for _notification in range(4):
                revision_before = coordinator.runs[0].source_revision
                coordinator.source_changed(0)
                assert coordinator.runs[0].source_revision != revision_before
                assert len(service.submissions) == completed_pages
                assert not coordinator.active
                assert coordinator._progressive_restart_deferred
                assert coordinator._progressive_not_before == deadline
                assert notifier.deadline == deadline
                assert tuple(notifier.created_deadlines) == scheduled

            for _owner_turn in range(4):
                assert coordinator.poll() == 0
                assert len(service.submissions) == completed_pages
                assert tuple(notifier.created_deadlines) == scheduled

            now[0] = deadline
            assert coordinator.poll() == 0
            assert len(service.submissions) == completed_pages + 1
            assert coordinator.active

        assert service.submission_times == pytest.approx(
            [100.0 + interval * index for index in range(4)]
        )
    finally:
        coordinator.close()


def test_warm_cached_selected_metadata_without_observation_protects_images(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coordinator_module, "ThreadPoolExecutor", _ImmediateExecutor)
    monkeypatch.setattr(
        coordinator_module,
        "render_trusted_derived_payload",
        _render_empty_retry_payload,
    )
    instance = _instance(53)
    remaining = _observation(1, instance)
    selected = _observation(2, instance)
    observations = {1: remaining, 2: selected}

    class _WarmSelectedCache:
        def __init__(self) -> None:
            self.hits: list[TrustedWorkKind] = []

        def get(self, key: Any, *, cancel_check: Any) -> object:
            cancel_check()
            if key.run_guid != "guid-2":
                return None
            self.hits.append(key.kind)
            return {
                "format": "qplot-trusted-derived-payload-v1",
                "kind": key.kind.name.lower(),
                "status": "empty",
                "description": "warm selected cache hit",
                "source": (),
                "images": (),
            }

        def put(self, _key: Any, _payload: object, *, cancel_check: Any) -> bool:
            cancel_check()
            return False

    cache = _WarmSelectedCache()
    service = _Service(instance, observations)
    publications = []
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs(observations),
        service,
        cache=cache,  # type: ignore[arg-type]
        on_publish=publications.append,
    )
    try:
        # Establish conclusive selected metadata without retaining a source
        # observation, leave T/P pending, then reopen M as a warm-cache replay.
        coordinator.scheduler.select_run(1)
        metadata = coordinator.scheduler.claim_next()
        assert metadata is not None
        assert metadata.run_index == 1
        assert metadata.key.kind is TrustedWorkKind.METADATA
        coordinator.scheduler.complete(metadata, {"preseeded": "metadata"})
        publications.clear()
        coordinator._selected_index = 1
        coordinator._accepted_selected_detail = (
            0,
            0,
            1,
            2,
            "guid-2",
            trusted_derived_source_revision(selected),
        )
        assert coordinator.scheduler.request_completed_work(
            1,
            TrustedWorkKind.METADATA,
            database_instance=instance,
            generation=coordinator.scheduler.generation,
            run_guid="guid-2",
        )
        assert coordinator._reused_observations == {}
        coordinator._foreground_claims_since_background_metadata = (
            coordinator_module.TRUSTED_DERIVED_BACKGROUND_AGING_CLAIM_BOUND
        )

        coordinator.start()
        queued = coordinator._completions.queue[0]  # type: ignore[attr-defined]
        assert queued.work.key.kind is TrustedWorkKind.METADATA
        assert queued.observation is None
        assert service.submissions == []

        # Scheduler conclusive state, not the optional cached observation,
        # protects the selected M replay -> T -> P sequence from aged work.
        assert coordinator.poll() == 1
        assert coordinator.snapshot().running[0].key.kind is TrustedWorkKind.THUMBNAIL
        assert coordinator.poll() == 1
        assert coordinator.snapshot().running[0].key.kind is TrustedWorkKind.PREVIEW
        assert coordinator.poll() == 1
        assert service.submissions == [1]
        assert coordinator.snapshot().running[0].run_index == 0
        assert coordinator.snapshot().running[0].key.kind is TrustedWorkKind.METADATA
        assert cache.hits == list(TrustedWorkKind)
        assert [publication.key.kind for publication in publications] == list(
            TrustedWorkKind
        )
        assert all(publication.key.run_guid == "guid-2" for publication in publications)
    finally:
        coordinator.close()


def test_terminal_metadata_revision_replays_one_fresh_selected_detail(
    tmp_path: Path,
) -> None:
    """Refresh detail once at the terminal revision, not on progressive pages."""

    instance = _instance(71)
    observation = _observation(1, instance)
    final_revision = trusted_derived_source_revision(observation)
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs({1: observation}),
        _Service(instance, {1: observation}),
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
    )
    try:
        coordinator.scheduler.select_run(0)
        coordinator._selected_index = 0
        coordinator._selection_generation = 7
        accepted = (
            7,
            0,
            0,
            1,
            "guid-1",
            TrustedSourceRevision(b"accepted-progressive-page"),
        )
        coordinator._accepted_selected_detail = accepted

        metadata = coordinator.scheduler.claim_next()
        assert metadata is not None
        assert metadata.run_index == 0
        assert metadata.key.kind is TrustedWorkKind.METADATA
        # A newer progressive claim must not trigger another Snapshot read.
        assert (
            coordinator._selected_detail_attempt_for(metadata, coordinator.runs[0])
            is None
        )
        terminal_result: Any = SimpleNamespace(
            work=metadata,
            selected_detail_attempt=None,
        )
        coordinator._replay_selected_metadata_for_detail(terminal_result)
        assert coordinator._accepted_selected_detail == accepted

        coordinator.scheduler.complete(metadata, {"preseeded": "metadata"})
        coordinator._replay_selected_metadata_for_detail(terminal_result)
        assert coordinator._accepted_selected_detail is None

        replay = coordinator.scheduler.claim_next()
        assert replay is not None
        assert replay.run_index == 0
        assert replay.key.kind is TrustedWorkKind.METADATA
        attempt = coordinator._selected_detail_attempt_for(
            replay,
            coordinator.runs[0],
        )
        assert attempt is not None
        assert attempt.source_revision == final_revision
    finally:
        coordinator.close()


def test_reused_observation_lru_has_strict_entry_and_byte_bounds(
    tmp_path: Path,
) -> None:
    instance = _instance()
    observations = {
        index: _observation(index, instance)
        for index in range(1, TRUSTED_DERIVED_MAX_REUSED_SOURCES + 2)
    }
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs(observations),
        _Service(instance, observations),
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
    )

    for observation in observations.values():
        coordinator._adopt_observation(
            observation,
            trusted_derived_source_revision(observation),
            coordinator._observation_size(observation),
        )

    assert len(coordinator._reused_observations) == (TRUSTED_DERIVED_MAX_REUSED_SOURCES)
    assert "guid-1" not in coordinator._reused_observations
    assert (
        coordinator._reused_observation_bytes <= TRUSTED_DERIVED_MAX_REUSED_SOURCE_BYTES
    )

    retained_size = TRUSTED_DERIVED_MAX_REUSED_SOURCE_BYTES // 2 + 1
    coordinator._clear_reused_observations()
    coordinator._adopt_observation(
        observations[1],
        trusted_derived_source_revision(observations[1]),
        retained_size,
    )
    coordinator._adopt_observation(
        observations[2],
        trusted_derived_source_revision(observations[2]),
        retained_size,
    )

    assert tuple(coordinator._reused_observations) == ("guid-2",)
    assert coordinator._reused_observation_bytes == retained_size
    coordinator.close()


def test_observation_identity_and_size_are_computed_once_off_owner_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = _instance()
    observations = {1: _observation(1, instance)}
    service = _Service(instance, observations)
    publications = []
    coordinator = TrustedWorkCoordinator(
        instance,
        _provisional_runs(observations),
        service,
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
        on_publish=publications.append,
    )
    owner_thread = threading.get_ident()
    revision_threads: list[int] = []
    size_threads: list[int] = []
    real_revision = coordinator_module.trusted_derived_source_revision
    real_size = TrustedWorkCoordinator._observation_size

    def revision_off_owner(observation: TrustedDerivedSourceObservation):
        thread_id = threading.get_ident()
        revision_threads.append(thread_id)
        assert thread_id != owner_thread
        return real_revision(observation)

    def size_off_owner(observation: TrustedDerivedSourceObservation) -> int:
        thread_id = threading.get_ident()
        size_threads.append(thread_id)
        assert thread_id != owner_thread
        return real_size(observation)

    monkeypatch.setattr(
        coordinator_module,
        "trusted_derived_source_revision",
        revision_off_owner,
    )
    monkeypatch.setattr(
        TrustedWorkCoordinator,
        "_observation_size",
        staticmethod(size_off_owner),
    )

    coordinator.start()
    _drain(coordinator)
    coordinator.close()

    assert service.submissions == [1]
    assert [item.key.kind for item in publications] == list(TrustedWorkKind)
    assert len(revision_threads) == 1
    assert len(size_threads) == 1


def test_progressive_layout_pages_restart_only_after_the_priority_pass_drains(
    tmp_path: Path,
) -> None:
    instance = _instance()
    first_page = replace(
        _observation(1, instance),
        progressive_layout_pending=True,
        progressive_layout_cursor=4_096,
    )
    completed = replace(
        first_page,
        progressive_layout_pending=False,
        progressive_layout_cursor=0,
    )
    ordinary = _observation(2, instance)

    class _ProgressiveService(_Service):
        def __init__(self) -> None:
            super().__init__(instance, {1: first_page, 2: ordinary})
            self._sequences = {1: [first_page, completed], 2: [ordinary]}

        def submit_derived_source(self, run_id: int, **_kwargs: Any) -> _Request:  # type: ignore[override]
            self.submissions.append(run_id)
            sequence = self._sequences[run_id]
            observation = sequence.pop(0) if len(sequence) > 1 else sequence[0]
            return _Request(observation)

    service = _ProgressiveService()
    publications = []
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs({1: first_page, 2: ordinary}),
        service,
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
        on_publish=publications.append,
    )
    coordinator.set_visible_range(0, 2)
    coordinator.start()

    _drain(coordinator)
    coordinator.close()

    assert service.submissions == [1, 2, 1]
    assert [(item.key.run_guid, item.key.kind) for item in publications] == [
        ("guid-2", TrustedWorkKind.METADATA),
        ("guid-2", TrustedWorkKind.THUMBNAIL),
        ("guid-2", TrustedWorkKind.PREVIEW),
        ("guid-1", TrustedWorkKind.METADATA),
        ("guid-1", TrustedWorkKind.THUMBNAIL),
        ("guid-1", TrustedWorkKind.PREVIEW),
    ]


def test_immediate_progressive_worker_yields_one_owner_turn_before_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ImmediateExecutor:
        def __init__(self, **_kwargs: Any) -> None:
            self.closed = False

        def submit(self, function: Any, *args: Any) -> Future[Any]:
            future: Future[Any] = Future()
            try:
                future.set_result(function(*args))
            except BaseException as error:
                future.set_exception(error)
            return future

        def shutdown(
            self,
            wait: bool = True,
            *,
            cancel_futures: bool = False,
        ) -> None:
            del wait, cancel_futures
            self.closed = True

    monkeypatch.setattr(coordinator_module, "ThreadPoolExecutor", _ImmediateExecutor)
    instance = _instance()
    first_pages = {
        run_id: replace(
            _observation(run_id, instance),
            progressive_layout_pending=True,
            progressive_layout_cursor=4_096,
        )
        for run_id in (1, 2)
    }
    completed = {
        run_id: replace(
            observation,
            progressive_layout_pending=False,
            progressive_layout_cursor=0,
        )
        for run_id, observation in first_pages.items()
    }

    class _ProgressiveService(_Service):
        def __init__(self) -> None:
            super().__init__(instance, first_pages)
            self._sequences = {
                run_id: [first_pages[run_id], completed[run_id]]
                for run_id in first_pages
            }

        def submit_derived_source(  # type: ignore[override]
            self,
            run_id: int,
            **_kwargs: Any,
        ) -> _Request:
            self.submissions.append(run_id)
            sequence = self._sequences[run_id]
            observation = sequence.pop(0) if len(sequence) > 1 else sequence[0]
            return _Request(observation)

    service = _ProgressiveService()
    publications = []
    wakeups: list[int] = []
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs(first_pages),
        service,
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
        wakeup=lambda: wakeups.append(len(service.submissions)),
        on_publish=publications.append,
    )
    coordinator.start()

    # A synchronously completed replacement claim is already queued before
    # every call returns.  Even so, one poll handles only one completion, and
    # the second progressive page is not submitted when the first pass drains.
    for _turn in range(2):
        submissions_before = len(service.submissions)
        assert coordinator.poll() == 1
        assert len(service.submissions) - submissions_before <= 1
    assert service.submissions == [1, 2]
    assert publications == []
    assert coordinator.snapshot().pending_count == 0
    assert not coordinator.active
    assert coordinator._progressive_restart_deferred
    assert wakeups

    # A priority change is a separate owner turn and therefore wins before the
    # next progressive page is admitted.  Both runs need another page, but the
    # newly selected second run is submitted first.
    coordinator.select_run(1)
    assert service.submissions == [1, 2, 2]

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        submissions_before = len(service.submissions)
        coordinator.poll()
        assert len(service.submissions) - submissions_before <= 1
        snapshot = coordinator.snapshot()
        if (
            snapshot.pending_count == 0
            and not coordinator.active
            and coordinator._progressive_active_count == 0
            and not coordinator._progressive_restart_deferred
        ):
            break
    else:
        raise AssertionError("The immediate progressive coordinator did not drain")

    assert service.submissions == [1, 2, 2, 1]
    assert [(item.key.run_guid, item.key.kind) for item in publications] == [
        ("guid-2", TrustedWorkKind.METADATA),
        ("guid-2", TrustedWorkKind.THUMBNAIL),
        ("guid-2", TrustedWorkKind.PREVIEW),
        ("guid-1", TrustedWorkKind.METADATA),
        ("guid-1", TrustedWorkKind.THUMBNAIL),
        ("guid-1", TrustedWorkKind.PREVIEW),
    ]
    coordinator.close()


def test_progressive_metadata_pages_are_tier_fair_and_owner_preemptible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ImmediateExecutor:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def submit(self, function: Any, *args: Any) -> Future[Any]:
            future: Future[Any] = Future()
            try:
                future.set_result(function(*args))
            except BaseException as error:
                future.set_exception(error)
            return future

        def shutdown(
            self,
            wait: bool = True,
            *,
            cancel_futures: bool = False,
        ) -> None:
            del wait, cancel_futures

    monkeypatch.setattr(coordinator_module, "ThreadPoolExecutor", _ImmediateExecutor)
    monkeypatch.setattr(
        coordinator_module,
        "render_trusted_derived_payload",
        _render_empty_retry_payload,
    )
    instance = _instance(30)
    first_pages = {
        run_id: replace(
            _observation(run_id, instance),
            progressive_layout_pending=True,
            progressive_layout_cursor=4_096,
        )
        for run_id in (1, 2, 3)
    }
    second_pages = {
        run_id: replace(
            observation,
            progressive_layout_cursor=8_192,
        )
        for run_id, observation in first_pages.items()
    }
    terminal_pages = {
        run_id: replace(
            observation,
            progressive_layout_pending=False,
            progressive_layout_cursor=0,
        )
        for run_id, observation in first_pages.items()
    }

    class _TieredProgressiveService(_Service):
        def __init__(self) -> None:
            super().__init__(instance, first_pages)
            self._sequences = {
                run_id: [
                    first_pages[run_id],
                    second_pages[run_id],
                    terminal_pages[run_id],
                ]
                for run_id in first_pages
            }

        def submit_derived_source(  # type: ignore[override]
            self,
            run_id: int,
            **_kwargs: Any,
        ) -> _Request:
            self.submissions.append(run_id)
            sequence = self._sequences[run_id]
            observation = sequence.pop(0) if len(sequence) > 1 else sequence[0]
            return _Request(observation)

    service = _TieredProgressiveService()
    publications = []
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs(first_pages),
        service,
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
        on_publish=publications.append,
    )
    coordinator.select_run(2)
    coordinator.set_visible_indices((1,))
    coordinator.start()
    assert service.submissions == [3]

    # Reprioritise while the old selected page's completion is queued.  The new
    # selection must preempt the visible and remaining lanes on the next owner
    # turn without starting a second 4,096-row page in that same turn.
    coordinator.select_run(0)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        submissions_before = len(service.submissions)
        coordinator.poll()
        assert len(service.submissions) - submissions_before <= 1
        snapshot = coordinator.snapshot()
        if (
            snapshot.pending_count == 0
            and not coordinator.active
            and coordinator._progressive_active_count == 0
            and not coordinator._progressive_restart_deferred
        ):
            break
    else:
        raise AssertionError("The tiered progressive coordinator did not drain")

    # The newly selected run's ordinary first page and one continuation precede
    # lower tiers.  Thereafter each progressive round gives selected, visible,
    # and remaining exactly one page.  A conclusive run completes its own M/T/P
    # sequence before the next lower-tier output sequence.
    assert service.submissions == [3, 1, 1, 2, 2, 3, 1, 2, 3]
    assert [
        (publication.key.run_guid, publication.key.kind) for publication in publications
    ] == [(f"guid-{run_id}", kind) for run_id in (1, 2, 3) for kind in TrustedWorkKind]
    coordinator.close()


def test_progressive_pages_do_not_amplify_outputs_or_cache_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = _instance(31)
    base = _observation(1, instance)
    observations = (
        replace(
            base,
            progressive_layout_pending=True,
            progressive_layout_cursor=4_096,
        ),
        replace(
            base,
            progressive_layout_pending=True,
            progressive_layout_cursor=8_192,
        ),
        base,
    )

    class _ThreePageService(_Service):
        def __init__(self) -> None:
            super().__init__(instance, {1: observations[0]})
            self._sequence = list(observations)

        def submit_derived_source(  # type: ignore[override]
            self,
            run_id: int,
            **_kwargs: Any,
        ) -> _Request:
            self.submissions.append(run_id)
            observation = (
                self._sequence.pop(0) if len(self._sequence) > 1 else self._sequence[0]
            )
            return _Request(observation)

    rendered: list[TrustedWorkKind] = []

    def render(
        observation: TrustedDerivedSourceObservation,
        kind: TrustedWorkKind,
        options: object,
        *,
        cancel_check: Any,
    ) -> object:
        rendered.append(kind)
        return _render_empty_retry_payload(
            observation,
            kind,
            options,
            cancel_check=cancel_check,
        )

    monkeypatch.setattr(coordinator_module, "render_trusted_derived_payload", render)
    cache = TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False)
    cache_writes: list[TrustedWorkKind] = []

    def cache_get(
        _key: object,
        *,
        cancel_check: Any,
    ) -> None:
        cancel_check()
        return None

    def cache_put(
        key: Any,
        _payload: object,
        *,
        cancel_check: Any,
    ) -> bool:
        cancel_check()
        cache_writes.append(key.kind)
        return False

    monkeypatch.setattr(cache, "get", cache_get)
    monkeypatch.setattr(cache, "put", cache_put)
    service = _ThreePageService()
    publications = []
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs({1: observations[0]}),
        service,
        cache=cache,
        on_publish=publications.append,
    )
    coordinator.start()
    _drain(coordinator)
    coordinator.close()

    published = [publication.key.kind for publication in publications]
    observed_counts = {
        "queries": len(service.submissions),
        "renders": tuple(rendered.count(kind) for kind in TrustedWorkKind),
        "publications": tuple(published.count(kind) for kind in TrustedWorkKind),
        "cache_writes": tuple(cache_writes.count(kind) for kind in TrustedWorkKind),
    }
    assert observed_counts == {
        "queries": 3,
        "renders": (1, 1, 1),
        "publications": (1, 1, 1),
        "cache_writes": (1, 1, 1),
    }


def test_identical_revision_terminal_page_unparks_one_complete_output_sequence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coordinator_module, "ThreadPoolExecutor", _ImmediateExecutor)
    instance = _instance(47)
    pending = _progressive_observation(1, instance)
    terminal = replace(
        pending,
        progressive_layout_pending=False,
        progressive_layout_cursor=0,
    )
    fixed_revision = TrustedSourceRevision(b"identical-progressive-revision")
    monkeypatch.setattr(
        coordinator_module,
        "trusted_derived_source_revision",
        lambda _observation: fixed_revision,
    )

    class _IdenticalRevisionService(_Service):
        def __init__(self) -> None:
            super().__init__(instance, {1: pending})
            self.sequence = [pending, terminal]

        def submit_derived_source(  # type: ignore[override]
            self,
            run_id: int,
            **_kwargs: Any,
        ) -> _Request:
            self.submissions.append(run_id)
            observation = self.sequence.pop(0) if self.sequence else terminal
            return _Request(observation)

    rendered: list[TrustedWorkKind] = []

    def render(
        observation: TrustedDerivedSourceObservation,
        kind: TrustedWorkKind,
        options: object,
        *,
        cancel_check: Any,
    ) -> object:
        rendered.append(kind)
        return _render_empty_retry_payload(
            observation,
            kind,
            options,
            cancel_check=cancel_check,
        )

    monkeypatch.setattr(coordinator_module, "render_trusted_derived_payload", render)
    cache = TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False)
    cache_writes: list[TrustedWorkKind] = []

    def cache_get(_key: object, *, cancel_check: Any) -> None:
        cancel_check()
        return None

    def cache_put(key: Any, _payload: object, *, cancel_check: Any) -> bool:
        cancel_check()
        cache_writes.append(key.kind)
        return False

    monkeypatch.setattr(cache, "get", cache_get)
    monkeypatch.setattr(cache, "put", cache_put)
    service = _IdenticalRevisionService()
    publications = []
    coordinator = TrustedWorkCoordinator(
        instance,
        (TrustedDerivedRun(1, "guid-1", fixed_revision),),
        service,
        cache=cache,
        on_publish=publications.append,
    )
    coordinator.start()
    _drain(coordinator)
    snapshot = coordinator.snapshot()
    coordinator.close()

    assert service.submissions == [1, 1]
    assert rendered == list(TrustedWorkKind)
    assert cache_writes == list(TrustedWorkKind)
    assert [publication.key.kind for publication in publications] == list(
        TrustedWorkKind
    )
    assert snapshot.completed_count == 3
    assert snapshot.pending_count == 0


@pytest.mark.parametrize("reopen_action", ["replay", "format"])
def test_conclusive_reopen_unparks_after_permanent_progressive_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reopen_action: str,
) -> None:
    monkeypatch.setattr(coordinator_module, "ThreadPoolExecutor", _ImmediateExecutor)
    instance = _instance(50)
    pending = _progressive_observation(1, instance)
    terminal = replace(
        pending,
        progressive_layout_pending=False,
        progressive_layout_cursor=0,
    )
    fixed_revision = TrustedSourceRevision(b"orphaned-progressive-revision")
    monkeypatch.setattr(
        coordinator_module,
        "trusted_derived_source_revision",
        lambda _observation: fixed_revision,
    )

    class _PermanentThenConclusiveService(_Service):
        def __init__(self) -> None:
            super().__init__(instance, {1: pending})
            self.attempt = 0

        def submit_derived_source(  # type: ignore[override]
            self,
            run_id: int,
            **_kwargs: Any,
        ) -> _Request:
            self.submissions.append(run_id)
            self.attempt += 1
            if self.attempt == 1:
                return _Request(pending)
            if self.attempt == 2:
                raise ValueError("permanent progressive proof failure")
            return _Request(terminal)

    rendered: list[TrustedWorkKind] = []

    def render(
        observation: TrustedDerivedSourceObservation,
        kind: TrustedWorkKind,
        options: object,
        *,
        cancel_check: Any,
    ) -> object:
        rendered.append(kind)
        return _render_empty_retry_payload(
            observation,
            kind,
            options,
            cancel_check=cancel_check,
        )

    monkeypatch.setattr(coordinator_module, "render_trusted_derived_payload", render)
    cache = TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False)
    cache_writes: list[TrustedWorkKind] = []

    def cache_get(_key: object, *, cancel_check: Any) -> None:
        cancel_check()
        return None

    def cache_put(key: Any, _payload: object, *, cancel_check: Any) -> bool:
        cancel_check()
        cache_writes.append(key.kind)
        return False

    monkeypatch.setattr(cache, "get", cache_get)
    monkeypatch.setattr(cache, "put", cache_put)
    service = _PermanentThenConclusiveService()
    publications = []
    coordinator = TrustedWorkCoordinator(
        instance,
        (TrustedDerivedRun(1, "guid-1", fixed_revision),),
        service,
        cache=cache,
        on_publish=publications.append,
    )
    coordinator.start()
    _drain(coordinator)

    assert service.submissions == [1, 1]
    assert coordinator._progressive_active_count == 0
    assert rendered == []
    assert cache_writes == []
    publications.clear()
    if reopen_action == "replay":
        assert coordinator.request_completed_work(
            0,
            TrustedWorkKind.METADATA,
            database_instance=coordinator.scheduler.database_instance,
            generation=coordinator.scheduler.generation,
            run_guid="guid-1",
        )
    else:
        coordinator.update_format(
            TrustedWorkKind.METADATA,
            WorkFormat("metadata-orphan-reopen-v2"),
        )

    _drain(coordinator)
    snapshot = coordinator.snapshot()
    coordinator.close()

    assert service.submissions == [1, 1, 1]
    assert rendered == list(TrustedWorkKind)
    assert cache_writes == list(TrustedWorkKind)
    assert [publication.key.kind for publication in publications] == list(
        TrustedWorkKind
    )
    assert snapshot.completed_count == 3
    assert snapshot.pending_count == 0


def test_progressive_runs_rotate_page_by_page_then_fully_drain_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coordinator_module, "ThreadPoolExecutor", _ImmediateExecutor)
    instance = _instance(43)
    first_pages = {
        run_id: _progressive_observation(run_id, instance) for run_id in range(1, 4)
    }

    class _ThreePagesEachService(_Service):
        def __init__(self) -> None:
            super().__init__(instance, first_pages)
            self.page_counts = {run_id: 0 for run_id in first_pages}

        def submit_derived_source(  # type: ignore[override]
            self,
            run_id: int,
            **_kwargs: Any,
        ) -> _Request:
            self.submissions.append(run_id)
            page = self.page_counts[run_id] + 1
            self.page_counts[run_id] = page
            observation = _progressive_observation(run_id, instance, page=page)
            if page == 3:
                observation = replace(
                    observation,
                    progressive_layout_pending=False,
                    progressive_layout_cursor=0,
                )
            return _Request(observation)

    rendered: list[tuple[str, TrustedWorkKind]] = []

    def render(
        observation: TrustedDerivedSourceObservation,
        kind: TrustedWorkKind,
        options: object,
        *,
        cancel_check: Any,
    ) -> object:
        rendered.append((observation.run_guid, kind))
        return _render_empty_retry_payload(
            observation,
            kind,
            options,
            cancel_check=cancel_check,
        )

    monkeypatch.setattr(coordinator_module, "render_trusted_derived_payload", render)
    cache = TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False)
    cache_writes: list[tuple[str, TrustedWorkKind]] = []

    def cache_get(_key: object, *, cancel_check: Any) -> None:
        cancel_check()
        return None

    def cache_put(key: Any, _payload: object, *, cancel_check: Any) -> bool:
        cancel_check()
        cache_writes.append((key.run_guid, key.kind))
        return False

    monkeypatch.setattr(cache, "get", cache_get)
    monkeypatch.setattr(cache, "put", cache_put)
    service = _ThreePagesEachService()
    publications = []
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs(first_pages),
        service,
        cache=cache,
        on_publish=publications.append,
    )
    coordinator.start()
    _drain(coordinator)
    snapshot = coordinator.snapshot()
    coordinator.close()

    expected_pages = [1, 2, 3, 1, 2, 3, 1, 2, 3]
    assert service.submissions == expected_pages
    expected_outputs = [
        (f"guid-{run_id}", kind) for run_id in range(1, 4) for kind in TrustedWorkKind
    ]
    assert rendered == expected_outputs
    assert cache_writes == expected_outputs
    assert [
        (publication.key.run_guid, publication.key.kind) for publication in publications
    ] == expected_outputs
    assert snapshot.pending_count == 0
    assert snapshot.completed_count == 9
    assert not snapshot.running


def test_immediate_progressive_pages_have_bounded_owner_work_and_wakeups(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coordinator_module, "ThreadPoolExecutor", _ImmediateExecutor)
    now = [100.0]

    class _FakeTime:
        @staticmethod
        def monotonic() -> float:
            return now[0]

        @staticmethod
        def sleep(seconds: float) -> None:
            now[0] += seconds

    class _RecordingProgressiveNotifier:
        instances: list[_RecordingProgressiveNotifier] = []

        def __init__(self, wakeup: Any) -> None:
            self.wakeup = wakeup
            self.deadline: float | None = None
            self.created_deadlines: list[float] = []
            self.cancelled = False
            self.instances.append(self)

        def schedule(self, deadline: float) -> None:
            if self.deadline == deadline:
                return
            self.deadline = deadline
            self.created_deadlines.append(deadline)

        def cancel(self) -> None:
            self.deadline = None
            self.cancelled = True

        def fire_due(self) -> None:
            if self.deadline is None or now[0] < self.deadline:
                return
            self.deadline = None
            if self.wakeup is not None:
                self.wakeup()

    monkeypatch.setattr(coordinator_module, "time", _FakeTime)
    monkeypatch.setattr(
        coordinator_module,
        "_ProgressiveWakeupNotifier",
        _RecordingProgressiveNotifier,
        raising=False,
    )
    minimum_interval = getattr(
        coordinator_module,
        "TRUSTED_DERIVED_PROGRESSIVE_MIN_INTERVAL_SECONDS",
        0.01,
    )
    assert type(minimum_interval) is float and 0.0 < minimum_interval <= 0.1
    instance = _instance(44)
    first_pages = {
        run_id: _progressive_observation(run_id, instance) for run_id in range(1, 6)
    }

    class _AlwaysProgressiveService(_Service):
        def __init__(self) -> None:
            super().__init__(instance, first_pages)
            self.page_counts = {run_id: 0 for run_id in first_pages}

        def submit_derived_source(  # type: ignore[override]
            self,
            run_id: int,
            **_kwargs: Any,
        ) -> _Request:
            self.submissions.append(run_id)
            page = self.page_counts[run_id] + 1
            self.page_counts[run_id] = page
            return _Request(_progressive_observation(run_id, instance, page=page))

    service = _AlwaysProgressiveService()
    wakeups: list[int] = []
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs(first_pages),
        service,
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
        wakeup=lambda: wakeups.append(len(service.submissions)),
    )
    try:
        coordinator.start()
        for _initial_run in range(len(first_pages)):
            assert coordinator.poll() == 1
        assert service.submissions == [1, 2, 3, 4, 5]
        assert len(_RecordingProgressiveNotifier.instances) == 1
        notifier = _RecordingProgressiveNotifier.instances[0]
        expected_deadline = now[0] + minimum_interval
        assert notifier.created_deadlines == [expected_deadline]
        assert coordinator._progressive_not_before == expected_deadline

        for _owner_turn in range(48):
            submissions_before = len(service.submissions)
            processed = coordinator.poll()
            assert processed == 0
            assert len(service.submissions) == submissions_before
            assert coordinator.scheduler.allocated_work_count <= 1
            assert coordinator._completions.maxsize == 1
            assert coordinator._completions.qsize() <= 1
            assert len(coordinator._progressive_state) == len(first_pages)
            assert coordinator._progressive_active_count <= len(first_pages)
            assert coordinator.snapshot().pending_count <= 3 * len(first_pages)

        assert notifier.created_deadlines == [expected_deadline]
        wakeups.clear()
        now[0] = expected_deadline
        notifier.fire_due()
        assert wakeups == [5]
        assert coordinator.poll() == 0
        assert len(service.submissions) == 6
        assert coordinator.scheduler.allocated_work_count == 1
        assert coordinator._completions.qsize() == 1
        assert (
            coordinator_module.TRUSTED_DERIVED_PROGRESSIVE_MIN_INTERVAL_SECONDS
            == minimum_interval
        )
    finally:
        coordinator.close()


def test_repeated_stale_progressive_outcomes_keep_one_paced_notifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coordinator_module, "ThreadPoolExecutor", _ImmediateExecutor)
    now, notifiers = _install_progressive_test_clock(monkeypatch)
    interval = coordinator_module.TRUSTED_DERIVED_PROGRESSIVE_MIN_INTERVAL_SECONDS
    instance = _instance(54)
    first_page = _progressive_observation(1, instance)

    class _RepeatedStaleService(_Service):
        def __init__(self) -> None:
            super().__init__(instance, {1: first_page})
            self.submission_times: list[float] = []

        def submit_derived_source(  # type: ignore[override]
            self,
            run_id: int,
            **_kwargs: Any,
        ) -> _Request:
            self.submissions.append(run_id)
            self.submission_times.append(now[0])
            if len(self.submissions) == 1:
                return _Request(first_page)
            raise InterruptedError("stale progressive attempt")

    service = _RepeatedStaleService()
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs({1: first_page}),
        service,
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
    )
    try:
        coordinator.start()
        assert coordinator.poll() == 1
        assert len(notifiers) == 1
        notifier = notifiers[0]

        for stale_attempt in range(1, 4):
            deadline = coordinator._progressive_not_before
            assert deadline == pytest.approx(now[0] + interval)
            assert notifier.deadline == deadline
            scheduled = tuple(notifier.created_deadlines)

            # Arbitrarily many owner polls before the boundary coalesce onto
            # the existing token and cannot immediately resubmit the stale M.
            for _owner_turn in range(5):
                assert coordinator.poll() == 0
                assert len(service.submissions) == stale_attempt
                assert tuple(notifier.created_deadlines) == scheduled

            now[0] = deadline
            assert coordinator.poll() == 0
            assert len(service.submissions) == stale_attempt + 1
            assert coordinator.active
            assert coordinator.poll() == 1
            assert not coordinator.active
            assert coordinator._progressive_restart_deferred

        assert len(notifiers) == 1
        assert service.submission_times == pytest.approx(
            [100.0 + interval * index for index in range(4)]
        )
        assert notifier.created_deadlines == pytest.approx(
            [100.0 + interval * index for index in range(1, 5)]
        )
    finally:
        coordinator.close()


def test_progressive_ready_state_is_exactly_one_byte_per_run(
    tmp_path: Path,
) -> None:
    instance = _instance(55)
    observations = {run_id: _observation(run_id, instance) for run_id in range(1, 18)}
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs(observations),
        _Service(instance, observations),
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
    )
    try:
        state = memoryview(coordinator._progressive_state)
        assert state.itemsize == 1
        assert state.nbytes == len(observations)
        assert state.format == "B"

        for run_index in (0, 8, 16):
            observation = _progressive_observation(run_index + 1, instance)
            coordinator._adopt_observation(
                observation,
                trusted_derived_source_revision(observation),
                coordinator._observation_size(observation),
            )
        assert coordinator._progressive_active_count == 3
        assert sum(bool(value) for value in coordinator._progressive_state) == 3

        # Progressive readiness has one and only one run-sized container.  In
        # particular, the former GUID registry and materialised round deque
        # must not coexist with the byte state and double O(run-count) memory.
        progressive_containers = {
            name: type(value)
            for name, value in vars(coordinator).items()
            if name.startswith("_progressive_")
            and isinstance(value, (bytearray, dict, set, list, tuple, deque))
        }
        assert progressive_containers == {"_progressive_state": bytearray}
        assert not hasattr(coordinator, "_progressive_observation_guids")
        assert not hasattr(coordinator, "_progressive_round_guids")
    finally:
        coordinator.close()


def test_completed_preview_replay_hits_cache_without_repeating_other_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = _instance()
    observations = {1: _observation(1, instance)}
    service = _Service(instance, observations)
    publications = []
    rendered = []
    real_render = coordinator_module.render_trusted_derived_payload

    def record_render(*args: Any, **kwargs: Any):
        rendered.append(args[1])
        return real_render(*args, **kwargs)

    monkeypatch.setattr(
        coordinator_module,
        "render_trusted_derived_payload",
        record_render,
    )
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs(observations),
        service,
        cache=TrustedDerivedDiskCache(tmp_path / "cache"),
        on_publish=publications.append,
    )
    coordinator.select_run(0)
    coordinator.start()
    _drain(coordinator)
    initial_submissions = len(service.submissions)
    initial_kinds = [publication.key.kind for publication in publications]
    initial_rendered = list(rendered)
    generation = coordinator.snapshot().generation

    assert coordinator.request_completed_work(
        0,
        TrustedWorkKind.PREVIEW,
        database_instance=instance,
        generation=generation,
        run_guid="guid-1",
    )
    assert not coordinator.request_completed_work(
        0,
        TrustedWorkKind.PREVIEW,
        database_instance=instance,
        generation=generation,
        run_guid="guid-1",
    )
    _drain(coordinator)

    assert initial_kinds == list(TrustedWorkKind)
    assert [publication.key.kind for publication in publications[3:]] == [
        TrustedWorkKind.PREVIEW
    ]
    assert rendered == initial_rendered
    assert len(service.submissions) == initial_submissions
    coordinator.close()


def test_append_reconciliation_adopts_refined_existing_revision(tmp_path: Path) -> None:
    instance = _instance()
    observations = {index: _observation(index, instance) for index in (1, 2)}
    publications = []
    coordinator = TrustedWorkCoordinator(
        instance,
        _provisional_runs({1: observations[1]}),
        _Service(instance, observations),
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
        on_publish=publications.append,
    )
    coordinator.start()
    _drain(coordinator)
    refined = coordinator.runs
    assert refined[0].source_revision == trusted_derived_source_revision(
        observations[1]
    )

    coordinator.reconcile_runs(
        (
            *refined,
            TrustedDerivedRun(
                2,
                observations[2].run_guid,
                TrustedSourceRevision(b"new-provisional"),
            ),
        )
    )
    _drain(coordinator)
    coordinator.close()

    assert {
        item.key.kind for item in publications if item.key.run_guid == "guid-2"
    } == set(TrustedWorkKind)


def test_completion_and_publication_are_marshaled_to_owner_thread(
    tmp_path: Path,
) -> None:
    instance = _instance()
    observations = {1: _observation(1, instance)}
    wake_threads: list[int] = []
    publish_threads: list[int] = []
    owner = threading.get_ident()
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs(observations),
        _Service(instance, observations),
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
        wakeup=lambda: wake_threads.append(threading.get_ident()),
        on_publish=lambda _publication: publish_threads.append(threading.get_ident()),
    )
    coordinator.start()
    _drain(coordinator)

    errors: list[BaseException] = []
    thread = threading.Thread(
        target=lambda: _capture_error(coordinator.poll, errors),
        name="wrong-owner",
    )
    thread.start()
    thread.join()
    coordinator.close()

    assert wake_threads and any(thread_id != owner for thread_id in wake_threads)
    assert publish_threads and set(publish_threads) == {owner}
    assert isinstance(errors[0], RuntimeError)


def _capture_error(action: Any, errors: list[BaseException]) -> None:
    try:
        action()
    except BaseException as error:
        errors.append(error)


def test_append_reconciliation_does_not_replay_completed_history(
    tmp_path: Path,
) -> None:
    instance = _instance()
    observations = {1: _observation(1, instance)}
    service = _Service(instance, observations)
    publications = []
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs(observations),
        service,
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
        on_publish=publications.append,
    )
    coordinator.start()
    _drain(coordinator)

    observations[2] = _observation(2, instance)
    coordinator.reconcile_runs(_runs(observations))
    _drain(coordinator)
    coordinator.close()

    assert [item.key.run_guid for item in publications].count("guid-1") == 3
    assert [item.key.run_guid for item in publications].count("guid-2") == 3


def test_active_change_is_coalesced_until_a_complete_prefix_publishes(
    tmp_path: Path,
) -> None:
    instance = _instance()
    observations = {1: _observation(1, instance)}
    publications = []
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs(observations),
        _Service(instance, observations),
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
        on_publish=publications.append,
    )
    coordinator.start()
    for _ in range(20):
        coordinator.source_changed(0)
    _drain(coordinator)
    coordinator.close()

    assert len(publications) >= 3
    assert {item.key.kind for item in publications[:3]} == set(TrustedWorkKind)
    assert all(item.result["status"] in {"ok", "unsupported"} for item in publications)


def test_deferred_change_survives_authoritative_revision_refinement(
    tmp_path: Path,
) -> None:
    instance = _instance(18)
    release = threading.Event()
    observations = {1: _observation(1, instance)}
    service = _Service(instance, observations, release=release)
    publications = []
    coordinator = TrustedWorkCoordinator(
        instance,
        _provisional_runs(observations),
        service,
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
        on_publish=publications.append,
    )
    coordinator.start()
    coordinator.source_changed(0)
    service.release = None
    release.set()
    _drain(coordinator)

    assert len(publications) == 3
    assert len(service.submissions) >= 2
    coordinator.close()


def test_database_switch_cancels_old_claim_and_publishes_nothing_stale(
    tmp_path: Path,
) -> None:
    first_instance = _instance(11)
    second_instance = _instance(12)
    release = threading.Event()
    first_observations = {1: _observation(1, first_instance)}
    second_observations = {2: _observation(2, second_instance)}
    publications = []
    coordinator = TrustedWorkCoordinator(
        first_instance,
        _runs(first_observations),
        _Service(first_instance, first_observations, release=release),
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
        on_publish=publications.append,
    )
    coordinator.start()
    coordinator.switch_database(
        second_instance,
        _runs(second_observations),
        _Service(second_instance, second_observations),
    )
    release.set()
    _drain(coordinator)
    coordinator.close()

    assert publications
    assert {item.key.database_instance for item in publications} == {second_instance}
    assert {item.key.run_guid for item in publications} == {"guid-2"}


def test_helper_restart_and_format_change_invalidate_only_their_namespaces(
    tmp_path: Path,
) -> None:
    instance = _instance()
    observations = {1: _observation(1, instance)}
    publications = []
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs(observations),
        _Service(instance, observations),
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
        on_publish=publications.append,
    )
    coordinator.start()
    _drain(coordinator)
    prior_generation = coordinator.snapshot().generation

    coordinator.helper_restarted()
    _drain(coordinator)
    assert coordinator.snapshot().generation == prior_generation + 1
    assert len(publications) == 6

    coordinator.update_format(
        TrustedWorkKind.PREVIEW,
        WorkFormat(
            "preview-v2",
            RenderingOptions.from_mapping({"width": 320, "height": 200}),
        ),
    )
    _drain(coordinator)
    coordinator.close()

    assert len(publications) == 7
    assert publications[-1].key.kind is TrustedWorkKind.PREVIEW
    assert publications[-1].key.renderer_version == "preview-v2"


def test_helper_restart_preserves_selected_and_multi_run_visible_tiers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coordinator_module, "ThreadPoolExecutor", _ImmediateExecutor)
    monkeypatch.setattr(
        coordinator_module,
        "render_trusted_derived_payload",
        _render_empty_retry_payload,
    )
    instance = _instance(56)
    observations = {run_id: _observation(run_id, instance) for run_id in range(1, 5)}
    publications = []
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs(observations),
        _Service(instance, observations),
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
        on_publish=publications.append,
    )
    try:
        # Run 1 is the lowest stable index but remains background.  A helper
        # generation boundary must restore the complete priority snapshot, not
        # only its selected member and an accidentally empty viewport.
        coordinator.scheduler.set_priority(2, (1, 3))
        coordinator._selected_index = 2
        coordinator.helper_restarted()
        snapshot = coordinator.snapshot()
        assert snapshot.selected_index == 2
        assert snapshot.visible_indices == (1, 3)

        _drain(coordinator)
        assert [
            (publication.key.run_guid, publication.key.kind)
            for publication in publications
        ] == [
            ("guid-3", TrustedWorkKind.METADATA),
            ("guid-3", TrustedWorkKind.THUMBNAIL),
            ("guid-3", TrustedWorkKind.PREVIEW),
            ("guid-2", TrustedWorkKind.METADATA),
            ("guid-4", TrustedWorkKind.METADATA),
            ("guid-2", TrustedWorkKind.THUMBNAIL),
            ("guid-4", TrustedWorkKind.THUMBNAIL),
            ("guid-2", TrustedWorkKind.PREVIEW),
            ("guid-4", TrustedWorkKind.PREVIEW),
            ("guid-1", TrustedWorkKind.METADATA),
            ("guid-1", TrustedWorkKind.THUMBNAIL),
            ("guid-1", TrustedWorkKind.PREVIEW),
        ]
    finally:
        coordinator.close()


def test_queued_old_completion_after_switch_to_fewer_runs_is_inert(
    tmp_path: Path,
) -> None:
    first = _instance(21)
    second = _instance(22)
    release = threading.Event()
    queued = threading.Event()
    observations = {1: _observation(1, first)}
    publications: list[object] = []
    errors: list[object] = []
    coordinator = TrustedWorkCoordinator(
        first,
        _provisional_runs(observations),
        _Service(first, observations, release=release),
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
        wakeup=queued.set,
        on_publish=publications.append,
        on_error=lambda _work, error: errors.append(error),
    )
    coordinator.start()
    release.set()
    _wait_for(queued)
    coordinator.switch_database(second, (), _Service(second, {}))

    assert coordinator.poll() == 1
    assert coordinator.snapshot().run_count == 0
    assert not publications
    assert not errors
    coordinator.close()


def test_queued_old_helper_completion_is_not_adopted(tmp_path: Path) -> None:
    instance = _instance(23)
    release = threading.Event()
    queued = threading.Event()
    observations = {1: _observation(1, instance)}
    publications = []
    service = _Service(instance, observations, release=release)
    coordinator = TrustedWorkCoordinator(
        instance,
        _provisional_runs(observations),
        service,
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
        wakeup=queued.set,
        on_publish=publications.append,
    )
    coordinator.start()
    release.set()
    _wait_for(queued)
    fresh = _observation(1, instance)
    observations[1] = TrustedDerivedSourceObservation(
        fresh.format_version,
        fresh.database_instance,
        fresh.run_id,
        fresh.run_guid,
        fresh.service_namespace,
        2,
        fresh.data_version,
        fresh.result_table_name,
        fresh.result_columns,
        fresh.result_schema_sha256,
        fresh.result_watermark,
        fresh.parameters,
        fresh.dependent_parameters,
        fresh.planned_shape,
        fresh.sample_columns,
        fresh.sample_rows,
    )
    service.release = None
    coordinator.helper_restarted()
    coordinator.poll()
    _drain(coordinator)

    assert publications
    assert all(
        dict(item.result["source"])["helper_incarnation"] == 2 for item in publications
    )
    assert len(service.submissions) >= 2
    coordinator.close()


def test_corrupt_cache_index_does_not_prevent_rendered_publication(
    tmp_path: Path,
) -> None:
    instance = _instance(231)
    observations = {1: _observation(1, instance)}
    root = tmp_path / "cache"
    root.mkdir()
    (root / ".qplot-derived-cache-index.sqlite3").write_bytes(b"not sqlite")
    cache = TrustedDerivedDiskCache(root)
    publications = []
    errors = []
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs(observations),
        _Service(instance, observations),
        cache=cache,
        on_publish=publications.append,
        on_error=lambda _work, error: errors.append(error),
    )
    coordinator.start()
    _drain(coordinator)
    coordinator.close()

    assert [item.key.kind for item in publications] == [
        TrustedWorkKind.METADATA,
        TrustedWorkKind.THUMBNAIL,
        TrustedWorkKind.PREVIEW,
    ]
    assert errors == []
    assert not cache.enabled


def test_existing_sqlite_cache_destination_does_not_prevent_publication(
    tmp_path: Path,
) -> None:
    instance = _instance(233)
    observations = {1: _observation(1, instance)}

    class CollisionCache(TrustedDerivedDiskCache):
        collision: Path | None = None
        snapshot: tuple[bytes, int, int] | None = None

        def put(self, key, payload, **kwargs):  # type: ignore[no-untyped-def]
            if self.collision is None:
                destination = self.root / trusted_cache_filename(key)
                destination.parent.mkdir(parents=True, exist_ok=True)
                connection = sqlite3.connect(destination)
                try:
                    connection.execute("CREATE TABLE protected(value TEXT NOT NULL)")
                    connection.execute("INSERT INTO protected VALUES('unchanged')")
                    connection.commit()
                finally:
                    connection.close()
                status = destination.stat()
                self.collision = destination
                self.snapshot = (
                    destination.read_bytes(),
                    status.st_mtime_ns,
                    status.st_ctime_ns,
                )
            return super().put(key, payload, **kwargs)

    cache = CollisionCache(tmp_path / "cache")
    publications = []
    errors = []
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs(observations),
        _Service(instance, observations),
        cache=cache,
        on_publish=publications.append,
        on_error=lambda _work, error: errors.append(error),
    )
    coordinator.start()
    _drain(coordinator)
    coordinator.close()

    assert [item.key.kind for item in publications] == [
        TrustedWorkKind.METADATA,
        TrustedWorkKind.THUMBNAIL,
        TrustedWorkKind.PREVIEW,
    ]
    assert errors == []
    assert not cache.enabled
    assert cache.collision is not None
    status = cache.collision.stat()
    assert cache.snapshot == (
        cache.collision.read_bytes(),
        status.st_mtime_ns,
        status.st_ctime_ns,
    )
    connection = sqlite3.connect(f"file:{cache.collision}?mode=ro", uri=True)
    try:
        assert connection.execute("SELECT value FROM protected").fetchall() == [
            ("unchanged",)
        ]
    finally:
        connection.close()


@pytest.mark.parametrize(
    "row_template",
    [
        "../protected.db",
        "{absolute}",
        r"C:\protected\file.qdc",
        r"\\server\share\file.qdc",
        "subdir/file.qdc",
        "live.db",
        "live.db-wal",
        "live.db-journal",
        "live.db-shm",
        ".qplot-derived-cache-index.sqlite3",
        ".qplot-derived-cache.lock",
        "subdir/../" + "3" * 64 + ".qdc",
    ],
)
def test_corrupt_index_deletion_target_still_publishes_rendered_results(
    tmp_path: Path,
    row_template: str,
) -> None:
    instance = _instance(232)
    observations = {1: _observation(1, instance)}
    root = tmp_path / "cache"
    protected = tmp_path / "protected.db"
    protected.write_bytes(b"must-not-change")
    row_name = row_template.format(absolute=protected)
    _seed_corrupt_cache_index(root, row_name)
    before = protected.read_bytes(), protected.stat().st_mtime_ns
    cache = TrustedDerivedDiskCache(
        root,
        max_entry_bytes=4_096,
        max_total_bytes=8_192,
        max_entries=1,
    )
    publications = []
    errors = []
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs(observations),
        _Service(instance, observations),
        cache=cache,
        on_publish=publications.append,
        on_error=lambda _work, error: errors.append(error),
    )
    coordinator.start()
    _drain(coordinator)
    coordinator.close()

    assert [item.key.kind for item in publications] == [
        TrustedWorkKind.METADATA,
        TrustedWorkKind.THUMBNAIL,
        TrustedWorkKind.PREVIEW,
    ]
    assert errors == []
    assert not cache.enabled
    assert (protected.read_bytes(), protected.stat().st_mtime_ns) == before


class _BlockingFailureCache:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def get(self, *_args: Any, **_kwargs: Any) -> object:
        self.entered.set()
        self.release.wait(2.0)
        raise RuntimeError("worker traceback must not cross")

    def put(self, *_args: Any, **_kwargs: Any) -> bool:
        return False


def test_stale_failure_has_no_callback_and_carries_no_exception_graph(
    tmp_path: Path,
) -> None:
    first = _instance(24)
    second = _instance(25)
    observations = {1: _observation(1, first)}
    cache = _BlockingFailureCache()
    queued = threading.Event()
    errors: list[object] = []
    coordinator = TrustedWorkCoordinator(
        first,
        _runs(observations),
        _Service(first, observations),
        cache=cache,  # type: ignore[arg-type]
        wakeup=queued.set,
        on_error=lambda _work, error: errors.append(error),
    )
    coordinator.start()
    _wait_for(cache.entered)
    coordinator.switch_database(second, (), _Service(second, {}))
    cache.release.set()
    _wait_for(queued)

    queued_result = coordinator._completions.queue[0]  # type: ignore[attr-defined]
    assert not any(
        isinstance(getattr(queued_result, item.name), BaseException)
        for item in fields(queued_result)
    )
    coordinator.poll()
    assert not errors
    coordinator.close()


class _SlowHitCache:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def get(self, *_args: Any, **_kwargs: Any) -> object:
        time.sleep(0.03)
        return self.payload

    def put(self, *_args: Any, **_kwargs: Any) -> bool:
        return False


def test_absolute_deadline_covers_initial_cache_lookup(tmp_path: Path) -> None:
    instance = _instance(26)
    observations = {1: _observation(1, instance)}
    publications = []
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs(observations),
        _Service(instance, observations),
        cache=_SlowHitCache(
            {
                "format": "qplot-trusted-derived-payload-v1",
                "kind": "metadata",
                "status": "ok",
                "description": "late",
                "source": (),
                "images": (),
            }
        ),  # type: ignore[arg-type]
        on_publish=publications.append,
        deadline_seconds=0.01,
    )
    coordinator.start()
    deadline = time.monotonic() + 1.0
    while coordinator.active and time.monotonic() < deadline:
        coordinator.poll()
        time.sleep(0.002)

    assert not publications
    coordinator.close()


class _PressuredService(_Service):
    def __init__(self, *args: Any, failures: int, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.failures = failures

    def submit_derived_source(self, run_id: int, **kwargs: Any) -> _Request:
        if self.failures:
            self.failures -= 1
            self.submissions.append(run_id)
            raise TrustedReadQueueFullError("temporary broker pressure")
        return super().submit_derived_source(run_id, **kwargs)


class _TimedPressuredService(_PressuredService):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.submission_times: list[float] = []

    def submit_derived_source(self, run_id: int, **kwargs: Any) -> _Request:
        self.submission_times.append(time.monotonic())
        return super().submit_derived_source(run_id, **kwargs)


def _render_empty_retry_payload(
    observation: TrustedDerivedSourceObservation,
    kind: TrustedWorkKind,
    _options: object,
    *,
    cancel_check,
):
    """Keep retry-notifier tests independent of cold Matplotlib startup."""

    cancel_check()
    return {
        "format": "qplot-trusted-derived-payload-v1",
        "kind": kind.name.lower(),
        "status": "empty",
        "description": "No rendered data required by this scheduling test.",
        "source": (("result_watermark", observation.result_watermark),),
        "images": (),
    }


def _poll_only_on_wakeup(
    coordinator: TrustedWorkCoordinator,
    wakeups: queue.Queue[float],
    *,
    timeout: float = 15.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        wakeups.get(timeout=remaining)
        coordinator.poll()
        snapshot = coordinator.snapshot()
        if snapshot.pending_count == 0 and not coordinator.active:
            return
    raise AssertionError("The event-driven coordinator did not drain")


def test_retry_notifier_rearms_an_early_platform_timer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [10.0]
    timers = []
    wakeups = []

    class EarlyTimer:
        def __init__(self, interval, callback, args=()) -> None:
            self.interval = interval
            self.callback = callback
            self.args = args
            self.daemon = False
            self.cancelled = False
            timers.append(self)

        def start(self) -> None:
            return None

        def cancel(self) -> None:
            self.cancelled = True

    monkeypatch.setattr(coordinator_module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(coordinator_module.threading, "Timer", EarlyTimer)
    notifier = coordinator_module._RetryWakeupNotifier(lambda: wakeups.append(now[0]))
    notifier.schedule(10.025)

    assert timers[-1].interval == pytest.approx(0.025)
    now[0] = 10.02
    timers[-1].callback(*timers[-1].args)
    assert wakeups == []
    assert timers[-1].interval == pytest.approx(0.005)

    now[0] = 10.025
    timers[-1].callback(*timers[-1].args)
    assert wakeups == [10.025]


def test_lower_tier_retry_backoff_does_not_block_new_selected_images(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coordinator_module, "ThreadPoolExecutor", _ImmediateExecutor)
    monkeypatch.setattr(
        coordinator_module,
        "render_trusted_derived_payload",
        _render_empty_retry_payload,
    )
    now = [100.0]

    class _FakeTime:
        @staticmethod
        def monotonic() -> float:
            return now[0]

        @staticmethod
        def sleep(seconds: float) -> None:
            now[0] += seconds

    class _RecordingRetryNotifier:
        def __init__(self, _wakeup: Any) -> None:
            self.deadline: float | None = None

        def schedule(self, deadline: float) -> None:
            self.deadline = deadline

        def cancel(self) -> None:
            self.deadline = None

    monkeypatch.setattr(coordinator_module, "time", _FakeTime)
    monkeypatch.setattr(
        coordinator_module,
        "_RetryWakeupNotifier",
        _RecordingRetryNotifier,
    )
    instance = _instance(275)
    observations = {
        1: _observation(1, instance),
        2: _observation(2, instance),
    }

    class _OneLowerTierFailureService(_Service):
        def __init__(self) -> None:
            super().__init__(instance, observations)
            self.fail_lower_once = True

        def submit_derived_source(  # type: ignore[override]
            self,
            run_id: int,
            **_kwargs: Any,
        ) -> _Request:
            self.submissions.append(run_id)
            if run_id == 1 and self.fail_lower_once:
                self.fail_lower_once = False
                raise TrustedReadQueueFullError("temporary lower-tier pressure")
            return _Request(self.observations[run_id])

    service = _OneLowerTierFailureService()
    publications = []
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs(observations),
        service,
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
        on_publish=publications.append,
    )
    try:
        # Pre-establish B's conclusive metadata while leaving its two image
        # slots pending.  This isolates the scheduling boundary under test from
        # the selected-detail metadata replay path.
        coordinator.scheduler.select_run(1)
        metadata = coordinator.scheduler.claim_next()
        assert metadata is not None
        assert metadata.run_index == 1
        assert metadata.key.kind is TrustedWorkKind.METADATA
        coordinator.scheduler.complete(metadata, {"preseeded": "metadata"})

        # A is selected only long enough to put its metadata claim in flight.
        # Before its queued transient failure is handled, reprioritise B.  A is
        # therefore a lower-tier retry-delayed slot and B's selected images are
        # the newly highest-priority ready work.
        coordinator.scheduler.select_run(0)
        coordinator._selected_index = 0
        coordinator.start()
        assert service.submissions == [1]
        coordinator._cancel_selected_detail_attempt()
        coordinator._selection_generation += 1
        coordinator._selected_index = 1
        coordinator.scheduler.select_run(1)

        assert coordinator.poll() == 1
        assert coordinator._retry_blocked_slot == (
            "guid-1",
            TrustedWorkKind.METADATA,
        )
        assert coordinator._retry_not_before == pytest.approx(100.025)
        assert service.submissions == [1, 2]
        assert coordinator.snapshot().running[0].key.kind is TrustedWorkKind.THUMBNAIL

        assert coordinator.poll() == 1
        assert coordinator.snapshot().running[0].key.kind is TrustedWorkKind.PREVIEW
        assert coordinator.poll() == 1
        assert not coordinator.active
        assert service.submissions == [1, 2]
        selected_publications = [
            publication
            for publication in publications
            if publication.key.run_guid == "guid-2"
        ]
        assert [publication.key.kind for publication in selected_publications] == list(
            TrustedWorkKind
        )

        # Once the exact backoff boundary arrives, the lower-tier slot remains
        # eligible and is admitted; it was delayed rather than discarded.
        now[0] = coordinator._retry_not_before
        assert coordinator.poll() == 0
        assert service.submissions == [1, 2, 1]
    finally:
        coordinator.close()


@pytest.mark.parametrize("promotion", ["selected", "visible"])
def test_lower_tier_retry_does_not_block_promoted_parked_continuation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    promotion: str,
) -> None:
    monkeypatch.setattr(coordinator_module, "ThreadPoolExecutor", _ImmediateExecutor)
    rendered: list[tuple[str, TrustedWorkKind]] = []

    def render(
        observation: TrustedDerivedSourceObservation,
        kind: TrustedWorkKind,
        options: object,
        *,
        cancel_check: Any,
    ) -> object:
        rendered.append((observation.run_guid, kind))
        return _render_empty_retry_payload(
            observation,
            kind,
            options,
            cancel_check=cancel_check,
        )

    monkeypatch.setattr(coordinator_module, "render_trusted_derived_payload", render)
    now = [100.0]

    class _FakeTime:
        @staticmethod
        def monotonic() -> float:
            return now[0]

        @staticmethod
        def sleep(seconds: float) -> None:
            now[0] += seconds

    class _RecordingNotifier:
        def __init__(self, _wakeup: Any) -> None:
            self.deadline: float | None = None

        def schedule(self, deadline: float) -> None:
            self.deadline = deadline

        def cancel(self) -> None:
            self.deadline = None

    monkeypatch.setattr(coordinator_module, "time", _FakeTime)
    monkeypatch.setattr(
        coordinator_module,
        "_RetryWakeupNotifier",
        _RecordingNotifier,
    )
    monkeypatch.setattr(
        coordinator_module,
        "_ProgressiveWakeupNotifier",
        _RecordingNotifier,
    )
    instance = _instance(276)
    lower = _observation(1, instance)
    selected_pending = _progressive_observation(2, instance)
    selected_terminal = replace(
        selected_pending,
        progressive_layout_pending=False,
        progressive_layout_cursor=0,
    )
    observations = {1: lower, 2: selected_pending}

    class _LowerFailureThenTerminalService(_Service):
        def __init__(self) -> None:
            super().__init__(instance, observations)
            self.fail_lower_once = True

        def submit_derived_source(  # type: ignore[override]
            self,
            run_id: int,
            **_kwargs: Any,
        ) -> _Request:
            self.submissions.append(run_id)
            if run_id == 1 and self.fail_lower_once:
                self.fail_lower_once = False
                raise TrustedReadQueueFullError("temporary lower-tier pressure")
            return _Request(lower if run_id == 1 else selected_terminal)

    service = _LowerFailureThenTerminalService()
    publications = []
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs(observations),
        service,
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
        on_publish=publications.append,
    )
    try:
        # Establish B as an already-parked progressive dependency without
        # consuming a worker turn.  Its M/T/P bits are intentionally hidden
        # from the ordinary scheduler snapshot until its continuation proves
        # the layout conclusive.
        coordinator.scheduler.select_run(1)
        parked_work = coordinator.scheduler.claim_next()
        assert parked_work is not None
        selected_revision = trusted_derived_source_revision(selected_pending)
        assert (
            coordinator.scheduler.park_progressive_metadata(
                parked_work,
                selected_revision,
            )
            is coordinator_module.CompletionDisposition.ACCEPTED
        )
        coordinator._adopt_observation(
            selected_pending,
            selected_revision,
            coordinator._observation_size(selected_pending),
        )

        # Put A in flight as selected, then promote parked B before A's queued
        # transient failure reaches the owner.  Once abandoned, A is a
        # remaining-tier retry slot whose deadline is later than B's progressive
        # pacing boundary.  Selection and viewport promotion exercise the same
        # hidden dependency through distinct scheduler tiers.
        coordinator.scheduler.select_run(0)
        coordinator._selected_index = 0
        coordinator.start()
        assert service.submissions == [1]
        coordinator._cancel_selected_detail_attempt()
        coordinator._selection_generation += 1
        if promotion == "selected":
            coordinator._selected_index = 1
            coordinator.scheduler.select_run(1)
        else:
            coordinator._selected_index = None
            coordinator.scheduler.select_run(None)
            coordinator.scheduler.set_visible_indices((1,))
        assert coordinator.poll() == 1
        assert coordinator._retry_not_before == pytest.approx(100.025)
        assert service.submissions == [1]

        now[0] += coordinator_module.TRUSTED_DERIVED_PROGRESSIVE_MIN_INTERVAL_SECONDS
        assert now[0] < coordinator._retry_not_before
        assert coordinator.poll() == 0
        assert service.submissions == [1, 2]
        assert coordinator.snapshot().running[0].key.kind is TrustedWorkKind.METADATA

        # The terminal continuation refines/replays metadata once, then its
        # promoted thumbnail and preview complete while A remains in backoff.
        _poll_completed_claims(coordinator, 4)
        assert now[0] < coordinator._retry_not_before
        assert service.submissions == [1, 2]
        assert rendered == [
            ("guid-2", TrustedWorkKind.METADATA),
            ("guid-2", TrustedWorkKind.THUMBNAIL),
            ("guid-2", TrustedWorkKind.PREVIEW),
        ]
        promoted_publications = [
            publication
            for publication in publications
            if publication.key.run_guid == "guid-2"
        ]
        assert [publication.key.kind for publication in promoted_publications] == list(
            TrustedWorkKind
        )

        now[0] = coordinator._retry_not_before
        assert coordinator.poll() == 0
        assert service.submissions == [1, 2, 1]
    finally:
        coordinator.close()


def test_deferred_source_change_keeps_retry_backoff_until_exact_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coordinator_module, "ThreadPoolExecutor", _ImmediateExecutor)
    monkeypatch.setattr(
        coordinator_module,
        "render_trusted_derived_payload",
        _render_empty_retry_payload,
    )
    now = [100.0]

    class _FakeTime:
        @staticmethod
        def monotonic() -> float:
            return now[0]

        @staticmethod
        def sleep(seconds: float) -> None:
            now[0] += seconds

    class _RecordingRetryNotifier:
        def __init__(self, _wakeup: Any) -> None:
            self.deadline: float | None = None

        def schedule(self, deadline: float) -> None:
            self.deadline = deadline

        def cancel(self) -> None:
            self.deadline = None

    monkeypatch.setattr(coordinator_module, "time", _FakeTime)
    monkeypatch.setattr(
        coordinator_module,
        "_RetryWakeupNotifier",
        _RecordingRetryNotifier,
    )
    instance = _instance(277)
    observations = {1: _observation(1, instance, watermark=8)}

    class _DeferredChangeService(_Service):
        def __init__(self) -> None:
            super().__init__(instance, observations)
            self.fail_once = True
            self.returned_watermarks: list[int] = []

        def submit_derived_source(  # type: ignore[override]
            self,
            run_id: int,
            **_kwargs: Any,
        ) -> _Request:
            self.submissions.append(run_id)
            if self.fail_once:
                self.fail_once = False
                raise TrustedReadQueueFullError("temporary broker pressure")
            observation = self.observations[run_id]
            self.returned_watermarks.append(observation.result_watermark)
            return _Request(observation)

    service = _DeferredChangeService()
    coordinator = TrustedWorkCoordinator(
        instance,
        _provisional_runs(observations),
        service,
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
    )
    try:
        coordinator.start()
        assert service.submissions == [1]
        observations[1] = _observation(1, instance, watermark=9)
        coordinator.source_changed(0)

        # The failure completion applies the deferred invalidation, but that
        # newest source remains governed by the exact original retry deadline.
        assert coordinator.poll() == 1
        assert coordinator._retry_not_before == pytest.approx(100.025)
        assert service.submissions == [1]
        assert not coordinator.active

        now[0] = 100.024
        assert coordinator.poll() == 0
        assert service.submissions == [1]
        now[0] = 100.025
        assert coordinator.poll() == 0
        assert service.submissions == [1, 1]
        assert service.returned_watermarks == [9]
        assert coordinator.active
    finally:
        coordinator.close()


def test_immediate_source_change_preserves_parked_retry_and_admits_peer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coordinator_module, "ThreadPoolExecutor", _ImmediateExecutor)
    monkeypatch.setattr(
        coordinator_module,
        "render_trusted_derived_payload",
        _render_empty_retry_payload,
    )
    now, _progressive_notifiers = _install_progressive_test_clock(monkeypatch)
    retry_notifiers = _install_recording_retry_notifier(monkeypatch)
    instance = _instance(278)
    progressive = _progressive_observation(1, instance)
    peer = _observation(2, instance)
    observations = {1: progressive, 2: peer}

    class _ProgressiveRetryService(_Service):
        def __init__(self) -> None:
            super().__init__(instance, observations)
            self.progressive_attempts = 0
            self.submission_times: list[tuple[int, float]] = []

        def submit_derived_source(  # type: ignore[override]
            self,
            run_id: int,
            **_kwargs: Any,
        ) -> _Request:
            self.submissions.append(run_id)
            self.submission_times.append((run_id, now[0]))
            if run_id == 1:
                self.progressive_attempts += 1
                if self.progressive_attempts == 2:
                    raise TrustedReadQueueFullError("temporary broker pressure")
                return _Request(
                    _progressive_observation(
                        run_id,
                        instance,
                        page=self.progressive_attempts,
                    )
                )
            return _Request(peer)

    service = _ProgressiveRetryService()
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs(observations),
        service,
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
    )
    try:
        # Leave the peer with no ordinary work until after the retry is parked.
        # Its later exact metadata replay proves that retaining A's deadline
        # does not turn the whole scheduler into a global backoff gate.
        coordinator.scheduler.select_run(1)
        for expected_kind in TrustedWorkKind:
            work = coordinator.scheduler.claim_next()
            assert work is not None
            assert work.run_index == 1
            assert work.key.kind is expected_kind
            coordinator.scheduler.complete(work, {"preseeded": expected_kind.name})

        coordinator.select_run(0)
        coordinator.start()
        assert service.submission_times == [(1, 100.0)]

        # Page one parks A.  Its continuation fails at the 10 ms pacing
        # boundary and acquires the exact first transient-pressure deadline.
        assert coordinator.poll() == 1
        now[0] = 100.01
        assert coordinator.poll() == 0
        assert service.submission_times == [(1, 100.0), (1, 100.01)]
        assert coordinator.poll() == 1
        retry_deadline = coordinator._retry_not_before
        assert retry_deadline == pytest.approx(100.035)
        assert retry_notifiers[0].deadline == pytest.approx(retry_deadline)
        assert not coordinator.active

        # This is the immediate (idle) invalidation path.  It must retain A's
        # exact slot deadline and must not submit A again at the current time.
        coordinator.source_changed(0)
        assert coordinator._retry_not_before == pytest.approx(retry_deadline)
        assert retry_notifiers[0].deadline == pytest.approx(retry_deadline)
        assert service.submission_times == [(1, 100.0), (1, 100.01)]
        assert not coordinator.active

        # An independent ready slot remains admissible during A's retained
        # backoff; only the exact (run, kind) retry slot is excluded.
        assert coordinator.request_completed_work(
            1,
            TrustedWorkKind.METADATA,
            database_instance=coordinator.scheduler.database_instance,
            generation=coordinator.scheduler.generation,
            run_guid="guid-2",
        )
        assert service.submission_times[-1] == (2, 100.01)
        assert coordinator.snapshot().running[0].run_index == 1
        assert coordinator.poll() == 1
        assert not coordinator.active

        now[0] = retry_deadline - 0.001
        assert coordinator.poll() == 0
        assert service.submission_times[-1] == (2, 100.01)
        now[0] = retry_deadline
        assert coordinator.poll() == 0
        assert service.submission_times[-1] == (1, retry_deadline)
        assert coordinator.active
    finally:
        coordinator.close()


def test_background_aging_skips_exact_retry_delayed_slot_for_foreground_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coordinator_module, "ThreadPoolExecutor", _ImmediateExecutor)
    monkeypatch.setattr(
        coordinator_module,
        "render_trusted_derived_payload",
        _render_empty_retry_payload,
    )
    now = [100.0]

    class _FakeTime:
        @staticmethod
        def monotonic() -> float:
            return now[0]

        @staticmethod
        def sleep(seconds: float) -> None:
            now[0] += seconds

    class _RecordingRetryNotifier:
        def __init__(self, _wakeup: Any) -> None:
            self.deadline: float | None = None

        def schedule(self, deadline: float) -> None:
            self.deadline = deadline

        def cancel(self) -> None:
            self.deadline = None

    monkeypatch.setattr(coordinator_module, "time", _FakeTime)
    monkeypatch.setattr(
        coordinator_module,
        "_RetryWakeupNotifier",
        _RecordingRetryNotifier,
    )
    instance = _instance(278)
    observations = {
        1: _observation(1, instance),
        2: _observation(2, instance),
    }

    class _AgingRetryService(_Service):
        def __init__(self) -> None:
            super().__init__(instance, observations)
            self.fail_background_once = True

        def submit_derived_source(  # type: ignore[override]
            self,
            run_id: int,
            **_kwargs: Any,
        ) -> _Request:
            self.submissions.append(run_id)
            if run_id == 1 and self.fail_background_once:
                self.fail_background_once = False
                raise TrustedReadQueueFullError("temporary background pressure")
            return _Request(self.observations[run_id])

    service = _AgingRetryService()
    publications = []
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs(observations),
        service,
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
        on_publish=publications.append,
    )
    try:
        # A enters the worker as selected.  Before its failure completion is
        # handled, B becomes selected, so abandoned A is the exact delayed
        # remaining slot and B metadata is the higher-priority base work.
        coordinator.scheduler.select_run(0)
        coordinator._selected_index = 0
        coordinator.start()
        assert service.submissions == [1]
        coordinator.select_run(1)
        coordinator._foreground_claims_since_background_metadata = (
            coordinator_module.TRUSTED_DERIVED_BACKGROUND_AGING_CLAIM_BOUND
        )

        assert coordinator.poll() == 1
        assert coordinator._retry_not_before == pytest.approx(100.025)
        assert now[0] < coordinator._retry_not_before
        assert service.submissions == [1, 2]
        assert coordinator.snapshot().running[0].run_index == 1
        assert coordinator.snapshot().running[0].key.kind is TrustedWorkKind.METADATA

        # Selected B's whole ordinary sequence proceeds while aging continues
        # to skip only the delayed A slot.
        _poll_completed_claims(coordinator, 3)
        assert now[0] < coordinator._retry_not_before
        assert service.submissions == [1, 2]
        selected_publications = [
            publication
            for publication in publications
            if publication.key.run_guid == "guid-2"
        ]
        assert [publication.key.kind for publication in selected_publications] == list(
            TrustedWorkKind
        )

        now[0] = coordinator._retry_not_before
        assert coordinator.poll() == 0
        assert service.submissions == [1, 2, 1]
    finally:
        coordinator.close()


def test_transient_retry_schedules_a_new_owner_wakeup_at_backoff_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        coordinator_module,
        "render_trusted_derived_payload",
        _render_empty_retry_payload,
    )
    instance = _instance(270)
    observations = {1: _observation(1, instance)}
    service = _TimedPressuredService(instance, observations, failures=1)
    wakeups: queue.Queue[float] = queue.Queue()
    publications = []
    coordinator = TrustedWorkCoordinator(
        instance,
        _provisional_runs(observations),
        service,
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
        wakeup=lambda: wakeups.put(time.monotonic()),
        on_publish=publications.append,
    )
    coordinator.start()

    first_wakeup = wakeups.get(timeout=2.0)
    assert coordinator.poll() == 1
    second_wakeup = wakeups.get(timeout=1.0)
    assert second_wakeup - first_wakeup >= 0.015
    coordinator.poll()
    _poll_only_on_wakeup(coordinator, wakeups)

    assert len(service.submissions) >= 2
    assert publications
    coordinator.close()


def test_repeated_transient_retries_are_event_driven_and_capped_without_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        coordinator_module,
        "render_trusted_derived_payload",
        _render_empty_retry_payload,
    )
    instance = _instance(271)
    observations = {1: _observation(1, instance)}
    service = _TimedPressuredService(instance, observations, failures=4)
    wakeups: queue.Queue[float] = queue.Queue()
    errors = []
    coordinator = TrustedWorkCoordinator(
        instance,
        _provisional_runs(observations),
        service,
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
        wakeup=lambda: wakeups.put(time.monotonic()),
        on_error=lambda _work, error: errors.append(error),
    )
    coordinator.start()
    _poll_only_on_wakeup(coordinator, wakeups)

    spacings = tuple(
        later - earlier
        for earlier, later in zip(
            service.submission_times,
            service.submission_times[1:],
            strict=False,
        )
    )
    assert all(
        actual >= minimum
        for actual, minimum in zip(
            spacings[:4], (0.015, 0.035, 0.075, 0.15), strict=True
        )
    )
    assert errors == []
    coordinator.close()


class _BlockedTransientService(_TimedPressuredService):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.entered = threading.Event()
        self.release_failure = threading.Event()

    def submit_derived_source(self, run_id: int, **kwargs: Any) -> _Request:
        if self.failures:
            self.submission_times.append(time.monotonic())
            self.submissions.append(run_id)
            self.failures -= 1
            self.entered.set()
            self.release_failure.wait(2.0)
            raise TrustedReadQueueFullError("temporary broker pressure")
        return _Service.submit_derived_source(self, run_id, **kwargs)


def test_source_change_during_transient_failure_preserves_backoff_for_newest_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        coordinator_module,
        "render_trusted_derived_payload",
        _render_empty_retry_payload,
    )
    instance = _instance(272)
    observations = {1: _observation(1, instance, watermark=8)}
    service = _BlockedTransientService(instance, observations, failures=1)
    wakeups: queue.Queue[float] = queue.Queue()
    publications = []
    coordinator = TrustedWorkCoordinator(
        instance,
        _provisional_runs(observations),
        service,
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
        wakeup=lambda: wakeups.put(time.monotonic()),
        on_publish=publications.append,
    )
    coordinator.start()
    _wait_for(service.entered)
    observations[1] = _observation(1, instance, watermark=9)
    coordinator.source_changed(0)
    service.release_failure.set()
    wakeups.get(timeout=2.0)
    coordinator.poll()

    assert len(service.submissions) == 1
    _poll_only_on_wakeup(coordinator, wakeups)
    assert publications
    assert dict(publications[-1].result["source"])["result_watermark"] == 9
    coordinator.close()


@pytest.mark.parametrize("action", ["switch", "restart", "close"])
def test_retry_notifier_cannot_revive_obsolete_generation(
    tmp_path: Path,
    action: str,
) -> None:
    instance = _instance(273)
    observations = {1: _observation(1, instance)}
    service = _TimedPressuredService(instance, observations, failures=1)
    wakeups: queue.Queue[float] = queue.Queue()
    publications = []
    coordinator = TrustedWorkCoordinator(
        instance,
        _provisional_runs(observations),
        service,
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
        wakeup=lambda: wakeups.put(time.monotonic()),
        on_publish=publications.append,
    )
    coordinator.start()
    wakeups.get(timeout=2.0)
    coordinator.poll()
    if action == "switch":
        replacement = _instance(274)
        coordinator.switch_database(replacement, (), _Service(replacement, {}))
    elif action == "restart":
        service.failures = 0
        coordinator.helper_restarted()
        _poll_only_on_wakeup(coordinator, wakeups)
    else:
        coordinator.close()
    while not wakeups.empty():
        wakeups.get_nowait()
    time.sleep(0.08)

    assert wakeups.empty()
    if action != "close":
        coordinator.close()


def test_transient_broker_pressure_retries_without_error_publication(
    tmp_path: Path,
) -> None:
    instance = _instance(27)
    observations = {1: _observation(1, instance)}
    service = _PressuredService(instance, observations, failures=2)
    publications = []
    coordinator = TrustedWorkCoordinator(
        instance,
        _provisional_runs(observations),
        service,
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
        on_publish=publications.append,
    )
    coordinator.start()
    _drain(coordinator)

    assert len(service.submissions) >= 3
    assert publications
    assert all(item.result["status"] != "error" for item in publications)
    coordinator.close()


def test_deferred_change_after_terminal_preview_failure_regenerates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = _instance(28)
    observations = {1: _observation(1, instance)}
    preview_entered = threading.Event()
    release_preview = threading.Event()
    real_render = coordinator_module.render_trusted_derived_payload

    def render(*args: Any, **kwargs: Any) -> object:
        if args[1] is TrustedWorkKind.PREVIEW:
            preview_entered.set()
            release_preview.wait(2.0)
            raise ValueError("terminal preview failure")
        return real_render(*args, **kwargs)

    monkeypatch.setattr(coordinator_module, "render_trusted_derived_payload", render)
    publications = []
    service = _Service(instance, observations)
    coordinator = TrustedWorkCoordinator(
        instance,
        _runs(observations),
        service,
        cache=TrustedDerivedDiskCache(tmp_path / "disabled", enabled=False),
        on_publish=publications.append,
    )
    coordinator.start()
    deadline = time.monotonic() + 2.0
    while not preview_entered.is_set():
        assert time.monotonic() < deadline
        coordinator.poll()
        time.sleep(0.002)
    coordinator.source_changed(0)
    release_preview.set()
    _drain(coordinator)

    assert len(publications) == 6
    assert len(service.submissions) >= 2
    coordinator.close()
