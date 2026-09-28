from __future__ import annotations

import builtins
import sqlite3
import threading
from collections.abc import Sequence
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

import pytest

import qplot.datahandling.trusted_live_queries as query_module
import qplot.datahandling.trusted_work_scheduler as scheduler_module
from qplot.datahandling.file_identity import DatabaseInstance
from qplot.datahandling.trusted_live_queries import (
    TrustedRunRecord,
    TrustedSourceRevisionNamespace,
    trusted_source_revision,
)
from qplot.datahandling.trusted_work_scheduler import (
    CompletionDisposition,
    RenderingOptions,
    ScheduledWork,
    SchedulerLifecycle,
    TrustedCacheWorkKey,
    TrustedRunWorkSource,
    TrustedSourceRevision,
    TrustedWorkKind,
    TrustedWorkScheduler,
    TrustedWorkState,
    WorkFormat,
    trusted_derived_cache_root,
)


def _instance(
    identity: tuple[int, int] = (7, 11),
    path: str = "/data/current.db",
) -> DatabaseInstance:
    return DatabaseInstance(path, path, identity)


def _revision(value: int) -> TrustedSourceRevision:
    return TrustedSourceRevision(f"revision-{value}".encode())


def _runs(count: int) -> list[TrustedRunWorkSource]:
    return [
        TrustedRunWorkSource(f"guid-{index}", _revision(index))
        for index in range(count)
    ]


def _drain(scheduler: TrustedWorkScheduler) -> list[ScheduledWork]:
    claimed = []
    while (work := scheduler.claim_next()) is not None:
        claimed.append(work)
        assert (
            scheduler.complete(work, f"result-{work.claim_id}")
            is CompletionDisposition.ACCEPTED
        )
    return claimed


def _order(work: Sequence[ScheduledWork]) -> list[tuple[int, TrustedWorkKind]]:
    return [(item.run_index, item.key.kind) for item in work]


def test_priority_is_selected_then_visible_then_remaining_with_information_order() -> (
    None
):
    scheduler = TrustedWorkScheduler(_instance(), _runs(5))
    scheduler.select_run(2)
    scheduler.set_visible_range(1, 4)

    assert _order(_drain(scheduler)) == [
        (2, TrustedWorkKind.METADATA),
        (2, TrustedWorkKind.THUMBNAIL),
        (2, TrustedWorkKind.PREVIEW),
        (1, TrustedWorkKind.METADATA),
        (3, TrustedWorkKind.METADATA),
        (1, TrustedWorkKind.THUMBNAIL),
        (3, TrustedWorkKind.THUMBNAIL),
        (1, TrustedWorkKind.PREVIEW),
        (3, TrustedWorkKind.PREVIEW),
        (0, TrustedWorkKind.METADATA),
        (4, TrustedWorkKind.METADATA),
        (0, TrustedWorkKind.THUMBNAIL),
        (4, TrustedWorkKind.THUMBNAIL),
        (0, TrustedWorkKind.PREVIEW),
        (4, TrustedWorkKind.PREVIEW),
    ]


def test_exact_noncontiguous_viewport_and_range_api_remain_coherent() -> None:
    scheduler = TrustedWorkScheduler(_instance(), _runs(5))
    scheduler.set_visible_indices((1, 3))

    assert _order(_drain(scheduler))[:6] == [
        (1, TrustedWorkKind.METADATA),
        (3, TrustedWorkKind.METADATA),
        (1, TrustedWorkKind.THUMBNAIL),
        (3, TrustedWorkKind.THUMBNAIL),
        (1, TrustedWorkKind.PREVIEW),
        (3, TrustedWorkKind.PREVIEW),
    ]

    scheduler = TrustedWorkScheduler(_instance(), _runs(5))
    scheduler.set_visible_indices((1, 3))
    scheduler.set_visible_range(1, 4)
    assert scheduler.snapshot().visible_indices == (1, 2, 3)


def test_selection_and_viewport_changes_reprioritize_without_duplicates() -> None:
    scheduler = TrustedWorkScheduler(_instance(), _runs(6))
    first = scheduler.claim_next()
    assert first is not None
    assert first.priority_key == (2, 0, 0)
    assert scheduler.claim_next() is None

    scheduler.select_run(5)
    scheduler.set_visible_range(2, 4)
    assert scheduler.complete(first) is CompletionDisposition.ACCEPTED

    promoted = scheduler.claim_next()
    assert promoted is not None
    assert promoted.priority_key == (0, 0, 5)
    assert scheduler.complete(promoted) is CompletionDisposition.ACCEPTED
    assert scheduler.claim_next().priority_key == (0, 1, 5)  # type: ignore[union-attr]


def test_previous_selection_is_demoted_to_its_current_tier() -> None:
    scheduler = TrustedWorkScheduler(_instance(), _runs(4))
    scheduler.set_visible_range(0, 2)
    scheduler.select_run(0)
    first = scheduler.claim_next()
    assert first is not None
    assert scheduler.complete(first) is CompletionDisposition.ACCEPTED

    scheduler.select_run(3)
    assert _order(_drain(scheduler))[:6] == [
        (3, TrustedWorkKind.METADATA),
        (3, TrustedWorkKind.THUMBNAIL),
        (3, TrustedWorkKind.PREVIEW),
        (1, TrustedWorkKind.METADATA),
        (0, TrustedWorkKind.THUMBNAIL),
        (1, TrustedWorkKind.THUMBNAIL),
    ]


def test_database_switch_cancels_work_and_aba_completion_is_stale() -> None:
    publications = []
    old_instance = _instance((7, 11))
    replacement = _instance((7, 12))
    scheduler = TrustedWorkScheduler(
        old_instance, _runs(2), on_publish=publications.append
    )
    old_work = scheduler.claim_next()
    assert old_work is not None

    scheduler.switch_database(replacement, _runs(2))

    assert old_work.cancellation.cancelled
    assert scheduler.database_instance == replacement
    assert scheduler.generation == 2
    assert scheduler.complete(old_work, "obsolete") is CompletionDisposition.STALE
    assert publications == []
    new_work = scheduler.claim_next()
    assert new_work is not None
    assert new_work.key.database_instance == replacement
    assert (
        new_work.key.database_instance.logical_path
        == old_work.key.database_instance.logical_path
    )


def test_late_completion_is_keyed_and_not_marked_for_new_selection() -> None:
    publications = []
    selected_publications = []
    scheduler = TrustedWorkScheduler(
        _instance(),
        _runs(2),
        on_publish=publications.append,
        on_selected_publish=selected_publications.append,
    )
    scheduler.select_run(0)
    work = scheduler.claim_next()
    assert work is not None

    scheduler.select_run(1)
    assert scheduler.complete(work, "run-zero") is CompletionDisposition.ACCEPTED

    assert len(publications) == 1
    assert publications[0].key.run_guid == "guid-0"
    assert not publications[0].is_current_selection
    assert selected_publications == []


def test_transition_callback_database_switch_suppresses_obsolete_publication() -> None:
    publications = []
    scheduler: TrustedWorkScheduler

    def switch_on_completion(transition) -> None:
        if transition.current is TrustedWorkState.COMPLETED:
            scheduler.switch_database(_instance((7, 99)), _runs(1))

    scheduler = TrustedWorkScheduler(
        _instance(),
        _runs(1),
        on_transition=switch_on_completion,
        on_publish=publications.append,
    )
    work = scheduler.claim_next()
    assert work is not None

    assert scheduler.complete(work, "obsolete") is CompletionDisposition.STALE
    assert publications == []


def test_general_publication_reselection_suppresses_selected_publication() -> None:
    selected_publications = []
    scheduler: TrustedWorkScheduler

    def reselect(_publication) -> None:
        scheduler.select_run(1)

    scheduler = TrustedWorkScheduler(
        _instance(),
        _runs(2),
        on_publish=reselect,
        on_selected_publish=selected_publications.append,
    )
    scheduler.select_run(0)
    work = scheduler.claim_next()
    assert work is not None

    assert scheduler.complete(work, "run-zero") is CompletionDisposition.ACCEPTED
    assert selected_publications == []


def test_idle_scheduler_drains_every_kind_for_every_run() -> None:
    transitions = []
    timestamps = iter(range(100))
    scheduler = TrustedWorkScheduler(
        _instance(),
        _runs(7),
        clock=lambda: float(next(timestamps)),
        on_transition=transitions.append,
    )

    work = _drain(scheduler)

    snapshot = scheduler.snapshot()
    assert len(work) == 21
    assert snapshot.pending_count == 0
    assert snapshot.completed_count == 21
    assert snapshot.running == ()
    assert snapshot.next_priority_key is None
    assert [transition.timestamp for transition in transitions] == [
        float(value) for value in range(42)
    ]


class _LargeRunTable(Sequence[TrustedRunWorkSource]):
    def __init__(self, count: int) -> None:
        self.count = count
        self.items_materialized = 0

    def __len__(self) -> int:
        return self.count

    def __getitem__(self, index: int) -> TrustedRunWorkSource:
        if isinstance(index, slice):
            raise TypeError("The scheduler must not slice the run table.")
        if index < 0:
            index += self.count
        if not 0 <= index < self.count:
            raise IndexError(index)
        self.items_materialized += 1
        return TrustedRunWorkSource(f"guid-{index}", _revision(index))


def test_large_table_has_no_eager_derived_tasks_or_ready_queue() -> None:
    runs = _LargeRunTable(100_000)
    scheduler = TrustedWorkScheduler(_instance(), runs)

    assert runs.items_materialized == 100_000
    assert scheduler.allocated_work_count == 0
    assert scheduler.snapshot().pending_count == 300_000
    work = scheduler.claim_next()
    assert work is not None
    assert scheduler.allocated_work_count == 1
    assert runs.items_materialized == 100_000


def test_cache_key_changes_for_every_invalidation_dimension() -> None:
    base = TrustedCacheWorkKey(
        _instance((1, 1)),
        "run-guid",
        TrustedWorkKind.THUMBNAIL,
        _revision(1),
        "renderer-1",
        RenderingOptions.from_mapping({"width": 160, "dark": False}),
    )

    variants = {
        TrustedCacheWorkKey(
            _instance((1, 2)),
            base.run_guid,
            base.kind,
            base.source_revision,
            base.renderer_version,
            base.rendering_options,
        ),
        TrustedCacheWorkKey(
            base.database_instance,
            base.run_guid,
            base.kind,
            _revision(2),
            base.renderer_version,
            base.rendering_options,
        ),
        TrustedCacheWorkKey(
            base.database_instance,
            base.run_guid,
            base.kind,
            base.source_revision,
            "renderer-2",
            base.rendering_options,
        ),
        TrustedCacheWorkKey(
            base.database_instance,
            base.run_guid,
            base.kind,
            base.source_revision,
            base.renderer_version,
            RenderingOptions.from_mapping({"width": 320, "dark": False}),
        ),
    }
    assert base not in variants
    assert len(variants) == 4


def test_trusted_query_layer_supplies_opaque_data_revision() -> None:
    run = TrustedRunRecord(4, (("guid", "run-guid"),))
    namespace = TrustedSourceRevisionNamespace(b"deterministic-test-session")

    first = trusted_source_revision(run, 10, namespace=namespace, helper_incarnation=3)
    same = trusted_source_revision(run, 10, namespace=namespace, helper_incarnation=3)
    after_commit = trusted_source_revision(
        run, 11, namespace=namespace, helper_incarnation=3
    )

    assert first == same
    assert first != after_commit


def test_revision_and_format_changes_invalidate_only_their_namespace() -> None:
    runs = _runs(1)
    scheduler = TrustedWorkScheduler(_instance(), runs)
    metadata = scheduler.claim_next()
    assert metadata is not None
    scheduler.complete(metadata)
    thumbnail = scheduler.claim_next()
    assert thumbnail is not None
    scheduler.complete(thumbnail)

    scheduler.update_format(TrustedWorkKind.THUMBNAIL, WorkFormat("thumbnail-v2"))
    assert (
        scheduler.state_for(0, TrustedWorkKind.METADATA) is TrustedWorkState.COMPLETED
    )
    assert scheduler.state_for(0, TrustedWorkKind.THUMBNAIL) is TrustedWorkState.PENDING

    scheduler.update_source_revision(0, _revision(99))
    assert scheduler.snapshot().completed_count == 0
    replacement = scheduler.claim_next()
    assert replacement is not None
    assert replacement.key.source_revision == _revision(99)


def test_stage5a_does_not_create_cache_files_or_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    before = set(tmp_path.iterdir())
    scheduler = TrustedWorkScheduler(_instance(), _runs(3))

    _drain(scheduler)

    assert set(tmp_path.iterdir()) == before


@pytest.mark.parametrize(
    ("platform", "environment", "home", "expected"),
    [
        (
            "linux",
            {"XDG_CACHE_HOME": "/cache"},
            PurePosixPath("/users/test"),
            PurePosixPath("/cache/qplot/trusted-derived"),
        ),
        (
            "darwin",
            {},
            PurePosixPath("/users/test"),
            PurePosixPath("/users/test/Library/Caches/qplot/trusted-derived"),
        ),
        (
            "win32",
            {"LOCALAPPDATA": "C:/Local"},
            PureWindowsPath("C:/Users/test"),
            PureWindowsPath("C:/Local/qplot/trusted-derived"),
        ),
    ],
)
def test_future_cache_namespace_is_in_application_cache_without_writes(
    tmp_path: Path,
    platform: str,
    environment: dict[str, str],
    home: PurePath,
    expected: PurePath,
) -> None:
    before = set(tmp_path.iterdir())

    result = trusted_derived_cache_root(
        environment=environment,
        home=home,
        platform=platform,
    )

    assert result == expected
    assert set(tmp_path.iterdir()) == before


def test_scheduler_does_not_follow_caller_run_sequence_mutation() -> None:
    runs = _runs(1)
    scheduler = TrustedWorkScheduler(_instance(), runs)

    runs.append(TrustedRunWorkSource("guid-late", _revision(99)))

    assert scheduler.snapshot().run_count == 1


def test_static_drain_candidate_inspections_are_linear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_count = 400
    scheduler = TrustedWorkScheduler(_instance(), _runs(run_count))
    inspections = 0

    def counted_range(*args: int):
        nonlocal inspections
        for run_index in builtins.range(*args):
            inspections += 1
            yield run_index

    monkeypatch.setattr(scheduler_module, "range", counted_range, raising=False)

    _drain(scheduler)

    assert inspections == run_count * len(TrustedWorkKind)


def test_database_switch_callback_observes_coherent_new_generation() -> None:
    callback_claims = []
    scheduler: TrustedWorkScheduler

    def claim_after_cancel(transition) -> None:
        if transition.current is TrustedWorkState.CANCELLED:
            callback_claims.append(scheduler.claim_next())

    scheduler = TrustedWorkScheduler(
        _instance(),
        _runs(1),
        on_transition=claim_after_cancel,
    )
    old_work = scheduler.claim_next()
    assert old_work is not None

    scheduler.switch_database(_instance((7, 22)), _runs(1))

    assert callback_claims[0] is not None
    assert callback_claims[0].generation == scheduler.generation
    assert callback_claims[0].key.database_instance == scheduler.database_instance


def test_scheduler_rejects_wrong_thread_access_before_mutation() -> None:
    scheduler = TrustedWorkScheduler(_instance(), _runs(1))
    failures: list[BaseException] = []

    def access_from_non_owner() -> None:
        try:
            scheduler.select_run(0)
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=access_from_non_owner)
    thread.start()
    thread.join()

    assert len(failures) == 1
    assert isinstance(failures[0], RuntimeError)
    assert scheduler.snapshot().selected_index is None


def test_rendering_option_identity_preserves_scalar_type() -> None:
    boolean = RenderingOptions.from_mapping({"value": True})
    integer = RenderingOptions.from_mapping({"value": 1})
    floating = RenderingOptions.from_mapping({"value": 1.0})

    assert len({boolean, integer, floating}) == 3
    assert len({WorkFormat("v1", boolean), WorkFormat("v1", integer)}) == 2
    keys = {
        TrustedCacheWorkKey(
            _instance(),
            "guid",
            TrustedWorkKind.PREVIEW,
            _revision(1),
            "v1",
            options,
        )
        for options in (boolean, integer, floating)
    }
    assert len(keys) == 3


def test_rendering_options_retain_canonical_validation() -> None:
    with pytest.raises(ValueError, match="sorted"):
        RenderingOptions((("z", 1), ("a", 2)))
    with pytest.raises(ValueError, match="unique"):
        RenderingOptions((("a", 1), ("a", 2)))
    with pytest.raises(ValueError, match="finite"):
        RenderingOptions.from_mapping({"value": float("inf")})
    with pytest.raises(TypeError, match="cache-key safe"):
        RenderingOptions((("value", object()),))  # type: ignore[arg-type]


@pytest.mark.parametrize("cache_value", ["", "relative/cache"])
def test_relative_linux_cache_environment_uses_absolute_home_fallback(
    cache_value: str,
) -> None:
    result = trusted_derived_cache_root(
        environment={"XDG_CACHE_HOME": cache_value},
        home=PurePosixPath("/users/test"),
        platform="linux",
    )

    assert result == PurePosixPath("/users/test/.cache/qplot/trusted-derived")
    assert result.is_absolute()


def test_source_revision_namespace_covers_reader_and_qplot_restarts() -> None:
    run = TrustedRunRecord(4, (("guid", "run-guid"),))
    first_session = TrustedSourceRevisionNamespace(b"first-service")
    second_session = TrustedSourceRevisionNamespace(b"second-service")

    stable = trusted_source_revision(
        run, 2, namespace=first_session, helper_incarnation=1
    )

    assert stable == trusted_source_revision(
        run, 2, namespace=first_session, helper_incarnation=1
    )
    assert stable != trusted_source_revision(
        run, 3, namespace=first_session, helper_incarnation=1
    )
    assert stable != trusted_source_revision(
        run, 2, namespace=first_session, helper_incarnation=2
    )
    assert stable != trusted_source_revision(
        run, 2, namespace=second_session, helper_incarnation=1
    )


def test_source_revision_namespace_factory_is_ephemeral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nonces = iter((b"a" * 32, b"b" * 32))
    monkeypatch.setattr(query_module.secrets, "token_bytes", lambda _size: next(nonces))

    first = TrustedSourceRevisionNamespace.create()
    second = TrustedSourceRevisionNamespace.create()

    assert len(first.nonce) == 32
    assert first != second


def test_repeated_data_version_after_real_wal_reader_restart_is_namespaced(
    tmp_path: Path,
) -> None:
    database = tmp_path / "ordinary-live.db"
    writer = sqlite3.connect(database)
    assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    writer.execute("CREATE TABLE results (id INTEGER PRIMARY KEY, value REAL)")
    writer.execute("INSERT INTO results(value) VALUES (1.0)")
    writer.commit()

    reader_one = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    first_data_version = reader_one.execute("PRAGMA data_version").fetchone()[0]
    first_count = reader_one.execute("SELECT count(*) FROM results").fetchone()[0]
    reader_one.close()

    writer.execute("INSERT INTO results(value) VALUES (2.0)")
    writer.commit()

    reader_two = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    second_data_version = reader_two.execute("PRAGMA data_version").fetchone()[0]
    second_count = reader_two.execute("SELECT count(*) FROM results").fetchone()[0]
    reader_two.close()
    writer.close()

    assert (first_count, second_count) == (1, 2)
    assert first_data_version == second_data_version
    run = TrustedRunRecord(1, (("guid", "ordinary-run"),))
    namespace = TrustedSourceRevisionNamespace(b"deterministic-live-service")
    first_revision = trusted_source_revision(
        run,
        first_data_version,
        namespace=namespace,
        helper_incarnation=1,
    )
    second_revision = trusted_source_revision(
        run,
        second_data_version,
        namespace=namespace,
        helper_incarnation=2,
    )
    assert first_revision != second_revision


def test_reconcile_runs_appends_without_replaying_completed_work() -> None:
    scheduler = TrustedWorkScheduler(_instance(), _runs(2))
    assert len(_drain(scheduler)) == 6

    scheduler.reconcile_runs(_runs(5))
    scheduler.select_run(4)
    appended_work = _drain(scheduler)

    assert len(appended_work) == 9
    assert {work.run_index for work in appended_work} == {2, 3, 4}
    assert _order(appended_work)[:3] == [
        (4, TrustedWorkKind.METADATA),
        (4, TrustedWorkKind.THUMBNAIL),
        (4, TrustedWorkKind.PREVIEW),
    ]
    assert scheduler.snapshot().completed_count == 15


def test_exact_completed_kind_replay_is_coalesced_and_identity_guarded() -> None:
    instance = _instance()
    scheduler = TrustedWorkScheduler(instance, _runs(2))
    assert len(_drain(scheduler)) == 6
    generation = scheduler.generation

    assert not scheduler.request_completed_work(
        0,
        TrustedWorkKind.PREVIEW,
        database_instance=_instance((7, 12)),
        generation=generation,
        run_guid="guid-0",
    )
    assert not scheduler.request_completed_work(
        0,
        TrustedWorkKind.PREVIEW,
        database_instance=instance,
        generation=generation + 1,
        run_guid="guid-0",
    )
    assert not scheduler.request_completed_work(
        0,
        TrustedWorkKind.PREVIEW,
        database_instance=instance,
        generation=generation,
        run_guid="guid-replaced",
    )
    assert scheduler.request_completed_work(
        0,
        TrustedWorkKind.PREVIEW,
        database_instance=instance,
        generation=generation,
        run_guid="guid-0",
    )
    assert not scheduler.request_completed_work(
        0,
        TrustedWorkKind.PREVIEW,
        database_instance=instance,
        generation=generation,
        run_guid="guid-0",
    )

    replay = scheduler.claim_next()
    assert replay is not None
    assert (replay.run_index, replay.key.kind) == (0, TrustedWorkKind.PREVIEW)
    assert scheduler.claim_next() is None
    assert scheduler.complete(replay) is CompletionDisposition.ACCEPTED
    assert scheduler.snapshot().completed_count == 6
    assert scheduler.claim_next() is None


def test_reconcile_runs_preserves_revision_override() -> None:
    scheduler = TrustedWorkScheduler(_instance(), _runs(1))
    scheduler.update_source_revision(0, _revision(77))
    _drain(scheduler)

    scheduler.reconcile_runs(_runs(2))
    scheduler.select_run(0)
    scheduler.update_format(TrustedWorkKind.METADATA, WorkFormat("metadata-v2"))
    work = scheduler.claim_next()

    assert work is not None
    assert work.run_index == 0
    assert work.key.source_revision == _revision(77)


@pytest.mark.parametrize(
    "runs",
    [
        [TrustedRunWorkSource("guid-0", _revision(0))] * 2,
        [
            TrustedRunWorkSource("guid-1", _revision(1)),
            TrustedRunWorkSource("guid-0", _revision(0)),
        ],
        [TrustedRunWorkSource("guid-replacement", _revision(0))],
    ],
)
def test_reconcile_runs_rejects_duplicates_reorder_and_replacement(
    runs: list[TrustedRunWorkSource],
) -> None:
    scheduler = TrustedWorkScheduler(_instance(), _runs(2))

    with pytest.raises(ValueError):
        scheduler.reconcile_runs(runs)


def test_close_callback_observes_closed_scheduler() -> None:
    observations = []
    scheduler: TrustedWorkScheduler

    def observe_close(transition) -> None:
        if transition.current is TrustedWorkState.CANCELLED:
            observations.append(scheduler.lifecycle)
            with pytest.raises(RuntimeError, match="closed"):
                scheduler.claim_next()

    scheduler = TrustedWorkScheduler(_instance(), _runs(1), on_transition=observe_close)
    assert scheduler.claim_next() is not None

    scheduler.close()

    assert observations == [SchedulerLifecycle.CLOSED]
    assert scheduler.snapshot().running == ()


def test_source_revision_callback_claims_only_revised_work() -> None:
    callback_claims = []
    scheduler: TrustedWorkScheduler

    def claim_after_cancel(transition) -> None:
        if transition.current is TrustedWorkState.CANCELLED:
            callback_claims.append(scheduler.claim_next())

    scheduler = TrustedWorkScheduler(
        _instance(), _runs(1), on_transition=claim_after_cancel
    )
    assert scheduler.claim_next() is not None

    scheduler.update_source_revision(0, _revision(88))

    assert callback_claims[0] is not None
    assert callback_claims[0].key.source_revision == _revision(88)
    assert len(scheduler.snapshot().running) == 1


def test_format_callback_claims_only_reformatted_work() -> None:
    callback_claims = []
    scheduler: TrustedWorkScheduler

    def claim_after_cancel(transition) -> None:
        if transition.current is TrustedWorkState.CANCELLED:
            callback_claims.append(scheduler.claim_next())

    scheduler = TrustedWorkScheduler(
        _instance(), _runs(1), on_transition=claim_after_cancel
    )
    assert scheduler.claim_next() is not None

    scheduler.update_format(TrustedWorkKind.METADATA, WorkFormat("metadata-v2"))

    assert callback_claims[0] is not None
    assert callback_claims[0].key.renderer_version == "metadata-v2"
    assert len(scheduler.snapshot().running) == 1


def test_wrong_thread_reads_and_completion_are_rejected() -> None:
    scheduler = TrustedWorkScheduler(_instance(), _runs(1))
    work = scheduler.claim_next()
    assert work is not None
    failures: list[BaseException] = []

    def access_from_non_owner() -> None:
        for operation in (
            lambda: scheduler.generation,
            scheduler.snapshot,
            lambda: scheduler.complete(work),
        ):
            try:
                operation()
            except BaseException as error:
                failures.append(error)
        work.cancellation.cancel()

    thread = threading.Thread(target=access_from_non_owner)
    thread.start()
    thread.join()

    assert len(failures) == 3
    assert all(isinstance(error, RuntimeError) for error in failures)
    assert scheduler.snapshot().running == (work,)
    assert work.cancellation.cancelled


def test_rendering_option_canonical_values_are_explicitly_tagged() -> None:
    options = RenderingOptions.from_mapping(
        {"bool": True, "float": 1.0, "int": 1, "none": None, "str": "1"}
    )

    assert options.canonical_values == (
        ("bool", ("boolean", True)),
        ("float", ("float", 1.0)),
        ("int", ("integer", 1)),
        ("none", ("none", None)),
        ("str", ("string", "1")),
    )


@pytest.mark.parametrize(
    ("platform", "environment", "home", "expected"),
    [
        (
            "linux",
            {"XDG_CACHE_HOME": ""},
            PurePosixPath("/home/test"),
            PurePosixPath("/home/test/.cache/qplot/trusted-derived"),
        ),
        (
            "linux",
            {"XDG_CACHE_HOME": "relative/cache"},
            PurePosixPath("/home/test"),
            PurePosixPath("/home/test/.cache/qplot/trusted-derived"),
        ),
        (
            "linux",
            {"XDG_CACHE_HOME": "/var/cache/test"},
            PurePosixPath("/home/test"),
            PurePosixPath("/var/cache/test/qplot/trusted-derived"),
        ),
        (
            "win32",
            {"LOCALAPPDATA": ""},
            PureWindowsPath("C:/Users/test"),
            PureWindowsPath("C:/Users/test/AppData/Local/qplot/trusted-derived"),
        ),
        (
            "win32",
            {"LOCALAPPDATA": "relative/cache"},
            PureWindowsPath("C:/Users/test"),
            PureWindowsPath("C:/Users/test/AppData/Local/qplot/trusted-derived"),
        ),
        (
            "win32",
            {"LOCALAPPDATA": "D:/Cache"},
            PureWindowsPath("C:/Users/test"),
            PureWindowsPath("D:/Cache/qplot/trusted-derived"),
        ),
        (
            "darwin",
            {"XDG_CACHE_HOME": ""},
            PurePosixPath("/Users/test"),
            PurePosixPath("/Users/test/Library/Caches/qplot/trusted-derived"),
        ),
        (
            "darwin",
            {"XDG_CACHE_HOME": "relative/cache"},
            PurePosixPath("/Users/test"),
            PurePosixPath("/Users/test/Library/Caches/qplot/trusted-derived"),
        ),
        (
            "darwin",
            {"XDG_CACHE_HOME": "/var/cache/ignored"},
            PurePosixPath("/Users/test"),
            PurePosixPath("/Users/test/Library/Caches/qplot/trusted-derived"),
        ),
    ],
)
def test_cache_root_uses_target_platform_absolute_path_rules(
    platform: str,
    environment: dict[str, str],
    home: PurePath,
    expected: PurePath,
) -> None:
    result = trusted_derived_cache_root(
        environment=environment,
        home=home,
        platform=platform,
    )

    assert result == expected
    assert result.is_absolute()


@pytest.mark.parametrize(
    ("platform", "home"),
    [
        ("linux", PurePosixPath("relative/home")),
        ("darwin", PurePosixPath("relative/home")),
        ("win32", PureWindowsPath("relative/home")),
    ],
)
def test_cache_root_rejects_relative_home_fallback(
    platform: str,
    home: PurePath,
) -> None:
    with pytest.raises(ValueError, match="home must be absolute"):
        trusted_derived_cache_root(environment={}, home=home, platform=platform)


def test_snapshot_does_not_advance_lane_cursors() -> None:
    scheduler = TrustedWorkScheduler(_instance(), _runs(3))

    first = scheduler.snapshot()
    second = scheduler.snapshot()
    work = scheduler.claim_next()

    assert first.next_priority_key == (2, 0, 0)
    assert second.next_priority_key == first.next_priority_key
    assert work is not None
    assert work.priority_key == first.next_priority_key
