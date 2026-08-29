"""Real public-QCoDeS WAL acceptance for the Stage 5B backend."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from qplot.datahandling.file_identity import database_instance
from qplot.datahandling.trusted_derived_cache import TrustedDerivedDiskCache
from qplot.datahandling.trusted_live_queries import trusted_source_revision
from qplot.datahandling.trusted_live_service import TrustedLiveReadService
from qplot.datahandling.trusted_work_coordinator import (
    TrustedDerivedRun,
    TrustedWorkCoordinator,
)
from qplot.datahandling.trusted_work_scheduler import TrustedWorkKind
from tests.datahandling.test_trusted_live import (
    _assert_protected_artifacts_unchanged,
    _QcodesWalWriter,
    _stable_artifact_state,
)

pytestmark = pytest.mark.timeout(120)


@pytest.fixture
def stage5b_wal_writer(tmp_path: Path) -> _QcodesWalWriter:
    database_directory = tmp_path / "database"
    database_directory.mkdir()
    writer = _QcodesWalWriter.start(database_directory / "stage5b-live.db")
    try:
        assert writer.startup["run_count"] == 1
        assert writer.request("commit_many", count=999) == 999
        yield writer
    finally:
        writer.close()


def _drain(coordinator: TrustedWorkCoordinator, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        coordinator.poll()
        snapshot = coordinator.snapshot()
        if snapshot.pending_count == 0 and not coordinator.active:
            return
        time.sleep(0.005)
    raise AssertionError("The real Stage 5B coordinator did not drain")


def _source_fields(publication: object) -> dict[str, object]:
    result = publication.result  # type: ignore[attr-defined]
    return dict(result["source"])


def test_real_stage5b_backend_publishes_live_prefixes_without_source_writes(
    stage5b_wal_writer: _QcodesWalWriter,
    tmp_path: Path,
) -> None:
    writer = stage5b_wal_writer
    accepted = database_instance(writer.database_path)
    service = TrustedLiveReadService(
        writer.database_path,
        expected_database_instance=accepted,
        request_timeout_seconds=30.0,
    )
    coordinator: TrustedWorkCoordinator | None = None
    publications = []
    try:
        discovery_started = time.monotonic()
        bootstrap = service.submit_bootstrap().wait(30.0)
        page = service.submit_basic_page(0, bootstrap.run_id_watermark).wait(30.0)
        discovery_elapsed = time.monotonic() - discovery_started
        assert page.complete and len(page.runs) == 1
        assert discovery_elapsed < 15.0

        run = page.runs[0]
        initial_revision = trusted_source_revision(
            run,
            bootstrap.data_version,
            namespace=service.source_revision_namespace,
            helper_incarnation=bootstrap.helper_incarnation,
        )
        before = _stable_artifact_state(
            writer.database_path,
            consecutive_observations=2,
            observation_interval=0.02,
        )
        cache = TrustedDerivedDiskCache(tmp_path / "cache")
        coordinator = TrustedWorkCoordinator(
            service.database_instance,
            (
                TrustedDerivedRun(
                    run.run_id, str(run.as_dict()["guid"]), initial_revision
                ),
            ),
            service,
            cache=cache,
            on_publish=publications.append,
        )
        assert cache.enabled
        coordinator.select_run(0)
        coordinator.set_visible_range(0, 1)
        coordinator.start()
        _drain(coordinator)

        after = _stable_artifact_state(
            writer.database_path,
            consecutive_observations=2,
            observation_interval=0.02,
        )
        _assert_protected_artifacts_unchanged(before, after)
        assert [item.key.kind for item in publications[:3]] == list(TrustedWorkKind)
        assert _source_fields(publications[0])["result_watermark"] == 1_000
        metadata = dict(publications[0].result["metadata"])
        run_fields = dict(metadata["run_fields"])
        assert run_fields["run_id"] == run.run_id
        assert run_fields["guid"] == run.as_dict()["guid"]
        assert run_fields["name"] == "trusted_live_run"
        assert run_fields["result_count"] == 1_000
        assert not bool(run_fields["is_completed"])

        captured = service.submit_derived_source(run.run_id).wait(30.0)
        sample_ids = tuple(row[0] for row in captured.sample_rows)
        assert sample_ids[0] == 1
        assert sample_ids[-1] == 1_000
        assert any(250 < row_id < 750 for row_id in sample_ids)
        expensive = service.submit_expensive_run(run.run_id).wait(30.0).as_dict()
        derived_fields = dict(captured.run_fields)
        for field in (
            "point_shape",
            "setpoint_shape",
            "setpoint_count",
            "read_setpoint_count",
        ):
            assert field in derived_fields
            derived_value = derived_fields[field]
            expensive_value = expensive[field]
            if isinstance(expensive_value, list):
                expensive_value = tuple(expensive_value)
            assert derived_value == expensive_value
        selected = service.submit_selected_run(run.run_id).wait(30.0)
        assert captured.setpoint_summaries == selected.setpoint_summaries
        assert captured.setpoint_summaries

        writer_errors: list[BaseException] = []

        def commit_continuously() -> None:
            try:
                for index in range(12):
                    writer.request("commit", value=f"stage5b-live-{index}")
            except BaseException as error:
                writer_errors.append(error)

        commit_thread = threading.Thread(target=commit_continuously)
        commit_thread.start()
        coordinator.source_changed(0)
        _drain(coordinator)
        commit_thread.join(30.0)
        assert not commit_thread.is_alive()
        assert writer_errors == []
        coordinator.source_changed(0)
        _drain(coordinator)

        current = [
            item
            for item in publications
            if _source_fields(item).get("result_watermark") == 1_012
        ]
        assert {item.key.kind for item in current} == set(TrustedWorkKind)
        assert all(item.result["status"] in {"ok", "unsupported"} for item in current)
        checkpoint = writer.request("checkpoint", mode="PASSIVE")
        assert checkpoint[0] == 0
        assert writer.request("commit", value="after-passive") == 1_012
        assert writer.request("checkpoint", mode="TRUNCATE") == (0, 0, 0)
        assert writer.request("commit", value="after-truncate") == 1_013

        coordinator.source_changed(0)
        queued_deadline = time.monotonic() + 30.0
        while coordinator._completions.empty():  # type: ignore[attr-defined]
            assert time.monotonic() < queued_deadline
            time.sleep(0.005)
        supervisor = service._required_supervisor()  # type: ignore[attr-defined]
        prior_incarnation = supervisor.incarnation
        supervisor.restart()
        assert supervisor.incarnation > prior_incarnation
        publication_count = len(publications)
        coordinator.helper_restarted()
        coordinator.poll()
        _drain(coordinator)
        replacement = publications[publication_count:]
        assert replacement
        assert all(
            dict(item.result["source"])["helper_incarnation"] == supervisor.incarnation
            for item in replacement
        )

        assert not tuple(writer.database_path.parent.rglob("*.qdc"))
        assert tuple(cache.root.glob("*.qdc"))
    finally:
        if coordinator is not None:
            coordinator.close(timeout=30.0)
        service.close(timeout=30.0)

    assert not service.liveness().helper_alive
    assert writer.request("commit", value="after-stage5b-close") == 1_014
    assert writer.request("checkpoint", mode="TRUNCATE") == (0, 0, 0)


def test_real_stage5b_metadata_refreshes_run_completion_without_pre_enrichment(
    stage5b_wal_writer: _QcodesWalWriter,
) -> None:
    writer = stage5b_wal_writer
    service = TrustedLiveReadService(
        writer.database_path,
        expected_database_instance=database_instance(writer.database_path),
        request_timeout_seconds=30.0,
    )
    try:
        bootstrap = service.submit_bootstrap().wait(30.0)
        page = service.submit_basic_page(0, bootstrap.run_id_watermark).wait(30.0)
        run = page.runs[0]
        before = service.submit_derived_source(run.run_id).wait(30.0)
        before_fields = dict(before.run_fields)
        assert not bool(before_fields["is_completed"])
        assert before_fields.get("completed_timestamp") is None

        assert writer.request("complete_run") is True
        after = service.submit_derived_source(run.run_id).wait(30.0)
        after_fields = dict(after.run_fields)

        assert bool(after_fields["is_completed"])
        assert isinstance(after_fields["completed_timestamp"], float)
    finally:
        service.close(timeout=30.0)


def test_real_queued_completion_is_inert_after_database_switch_to_no_runs(
    stage5b_wal_writer: _QcodesWalWriter,
    tmp_path: Path,
) -> None:
    first_writer = stage5b_wal_writer
    second_directory = tmp_path / "second-database"
    second_directory.mkdir()
    second_writer = _QcodesWalWriter.start(second_directory / "second.db")
    first_service = TrustedLiveReadService(
        first_writer.database_path,
        expected_database_instance=database_instance(first_writer.database_path),
        request_timeout_seconds=30.0,
    )
    second_service = TrustedLiveReadService(
        second_writer.database_path,
        expected_database_instance=database_instance(second_writer.database_path),
        request_timeout_seconds=30.0,
    )
    coordinator: TrustedWorkCoordinator | None = None
    publications = []
    try:
        bootstrap = first_service.submit_bootstrap().wait(30.0)
        page = first_service.submit_basic_page(0, bootstrap.run_id_watermark).wait(30.0)
        run = page.runs[0]
        initial_revision = trusted_source_revision(
            run,
            bootstrap.data_version,
            namespace=first_service.source_revision_namespace,
            helper_incarnation=bootstrap.helper_incarnation,
        )
        queued = threading.Event()
        coordinator = TrustedWorkCoordinator(
            first_service.database_instance,
            (
                TrustedDerivedRun(
                    run.run_id,
                    str(run.as_dict()["guid"]),
                    initial_revision,
                ),
            ),
            first_service,
            cache=TrustedDerivedDiskCache(tmp_path / "switch-cache"),
            wakeup=queued.set,
            on_publish=publications.append,
        )
        coordinator.start()
        assert queued.wait(30.0)
        coordinator.switch_database(
            second_service.database_instance,
            (),
            second_service,
        )

        assert coordinator.poll() == 1
        assert coordinator.snapshot().run_count == 0
        assert publications == []
    finally:
        if coordinator is not None:
            coordinator.close(timeout=30.0)
        first_service.close(timeout=30.0)
        second_service.close(timeout=30.0)
        second_writer.close()
