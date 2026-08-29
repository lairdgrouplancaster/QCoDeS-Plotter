from __future__ import annotations

import queue
import sqlite3
import threading
import time
from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest

import qplot.datahandling.trusted_work_coordinator as coordinator_module
from qplot.datahandling.file_identity import DatabaseInstance
from qplot.datahandling.trusted_derived_cache import (
    TrustedDerivedDiskCache,
    trusted_cache_filename,
)
from qplot.datahandling.trusted_live_queries import (
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
        if snapshot.pending_count == 0 and not coordinator.active:
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


def test_real_coordinator_preserves_exact_priority_and_eventual_drain(
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
    _observation: TrustedDerivedSourceObservation,
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
        "source": (),
        "images": (),
    }


def _poll_only_on_wakeup(
    coordinator: TrustedWorkCoordinator,
    wakeups: queue.Queue[float],
    *,
    timeout: float = 5.0,
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
) -> None:
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
