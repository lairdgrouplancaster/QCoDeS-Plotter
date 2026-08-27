"""Real-WAL acceptance coverage for the Stage 4 trusted read service."""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from pathlib import Path
from typing import Any

import apsw
import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.parameters import ManualParameter

from qplot.datahandling import trusted_live_service as service_module
from qplot.datahandling.file_identity import database_instance
from qplot.datahandling.trusted_live_queries import (
    TRUSTED_RUN_PAGE_SIZE,
    TrustedBootstrapResult,
    TrustedMetadataQueryAdapter,
    TrustedRunRecord,
)
from qplot.datahandling.trusted_live_service import (
    TrustedLiveReadService,
    TrustedReadRequestCancelledError,
)
from qplot.datahandling.trusted_live_supervisor import (
    TrustedLiveReaderSupervisor,
)
from tests.datahandling.test_trusted_live import (
    _assert_protected_artifacts_unchanged,
    _QcodesWalWriter,
    _stable_artifact_state,
)

pytestmark = pytest.mark.timeout(120)


@pytest.fixture
def stage4_wal_writer(tmp_path: Path) -> _QcodesWalWriter:
    writer = _QcodesWalWriter.start(tmp_path / "stage4-live.db")
    try:
        assert writer.startup["run_count"] == 1
        assert Path(f"{writer.database_path}-wal").is_file()
        assert Path(f"{writer.database_path}-shm").is_file()
        yield writer
    finally:
        writer.close()


def _create_completed_qcodes_run(database_path: Path, index: int) -> dict[str, Any]:
    """Commit one later run while the spawned writer keeps its WAL handle open."""

    experiment: Any = None
    dataset: Any = None
    initialise_or_create_database_at(str(database_path), journal_mode="WAL")
    try:
        experiment = load_or_create_experiment(
            "stage4_later_experiment",
            sample_name="stage4_later_sample",
        )
        setpoint = ManualParameter(f"stage4_later_setpoint_{index}")
        signal = ManualParameter(f"stage4_later_signal_{index}")
        measurement = Measurement(
            exp=experiment,
            name=f"stage4_later_run_{index}",
        )
        measurement.write_period = 0.001
        measurement.register_parameter(setpoint)
        measurement.register_parameter(signal, setpoints=(setpoint,))
        with measurement.run(write_in_background=False) as datasaver:
            dataset = datasaver.dataset
            datasaver.add_result((setpoint, float(index)), (signal, index * 2.0))
            datasaver.flush_data_to_database(block=True)
        return {
            "run_id": int(dataset.run_id),
            "guid": str(dataset.guid),
            "table_name": str(dataset.table_name),
        }
    finally:
        if dataset is not None:
            dataset.conn.close()
        if experiment is not None:
            experiment.conn.close()


def _drain_run_pages(
    service: TrustedLiveReadService,
    after_run_id: int,
    through_run_id: int,
) -> tuple[TrustedRunRecord, ...]:
    records: list[TrustedRunRecord] = []
    cursor = after_run_id
    while True:
        page = service.submit_basic_page(cursor, through_run_id).wait(20.0)
        assert len(page.runs) <= TRUSTED_RUN_PAGE_SIZE
        assert page.after_run_id == cursor
        assert page.through_run_id == through_run_id
        records.extend(page.runs)
        if page.complete:
            break
        assert cursor < page.next_run_id <= through_run_id
        cursor = page.next_run_id
    return tuple(records)


def _stable_source_state(database_path: Path) -> dict[str, tuple[Any, ...] | None]:
    return _stable_artifact_state(
        database_path,
        consecutive_observations=2,
        observation_interval=0.02,
    )


def _wait_for(predicate: Any, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("Timed out waiting for the real Stage 4 service state")


def test_persistent_stage4_service_discovers_later_qcodes_run_without_writes(
    stage4_wal_writer: _QcodesWalWriter,
) -> None:
    writer = stage4_wal_writer
    accepted = database_instance(writer.database_path)
    captured_supervisors: list[TrustedLiveReaderSupervisor] = []

    def supervisor_factory(
        database_path: str,
        **options: object,
    ) -> TrustedLiveReaderSupervisor:
        supervisor = TrustedLiveReaderSupervisor.open(database_path, **options)
        captured_supervisors.append(supervisor)
        return supervisor

    before_initial_read = _stable_source_state(writer.database_path)
    service = TrustedLiveReadService(
        writer.database_path,
        expected_database_instance=accepted,
        request_timeout_seconds=30.0,
        supervisor_factory=supervisor_factory,
    )
    try:
        bootstrap = service.submit_bootstrap().wait(30.0)
        initial_records = _drain_run_pages(service, 0, bootstrap.run_id_watermark)

        assert bootstrap.run_id_watermark == 1
        assert [record.run_id for record in initial_records] == [1]
        assert initial_records[0].as_dict()["name"] == "trusted_live_run"
        assert service.accepted
        assert len(captured_supervisors) == 1
        supervisor = captured_supervisors[0]
        helper_pid = supervisor.helper_pid
        incarnation = supervisor.incarnation
        assert helper_pid is not None
        assert supervisor.helper_alive

        cheap = service.submit_cheap_run(1).wait(30.0)
        expensive = service.submit_expensive_run(1).wait(30.0)
        selected = service.submit_selected_run(1).wait(30.0)
        assert cheap.run_id == expensive.run_id == selected.run.run_id == 1
        assert expensive.as_dict()["result_count"] == 1
        assert selected.parameters

        after_initial_read = _stable_source_state(writer.database_path)
        _assert_protected_artifacts_unchanged(
            before_initial_read,
            after_initial_read,
        )

        # No reader transaction survives an operation, so an owner checkpoint
        # can reset and truncate the WAL while the same helper remains open.
        assert writer.request("checkpoint", mode="TRUNCATE") == (0, 0, 0)
        later = _create_completed_qcodes_run(writer.database_path, 2)
        assert later["run_id"] == 2
        before_refresh = _stable_source_state(writer.database_path)

        refresh = service.submit_refresh().wait(30.0)
        later_records = _drain_run_pages(
            service,
            refresh.prior_run_id_watermark,
            refresh.run_id_watermark,
        )
        assert refresh.data_version_changed
        assert refresh.prior_run_id_watermark == 1
        assert refresh.run_id_watermark == 2
        assert [record.run_id for record in later_records] == [2]
        assert later_records[0].as_dict()["guid"] == later["guid"]

        later_expensive = service.submit_expensive_run(2).wait(30.0)
        assert later_expensive.as_dict()["result_count"] == 1
        after_refresh = _stable_source_state(writer.database_path)
        _assert_protected_artifacts_unchanged(before_refresh, after_refresh)
        assert supervisor.helper_pid == helper_pid
        assert supervisor.incarnation == incarnation
        assert supervisor.helper_alive
        assert len(captured_supervisors) == 1
        assert service.liveness().outstanding_requests == 0

        assert writer.request("checkpoint", mode="TRUNCATE") == (0, 0, 0)
    finally:
        service.close(timeout=30.0)

    assert not service.liveness().helper_alive
    assert writer.request("commit", value="after-stage4-close") == 1
    assert writer.request("checkpoint", mode="TRUNCATE") == (0, 0, 0)


def test_large_source_edge_plan_is_accepted_by_the_real_supervisor(
    stage4_wal_writer: _QcodesWalWriter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = stage4_wal_writer
    accepted = database_instance(writer.database_path)
    monkeypatch.setattr(
        TrustedMetadataQueryAdapter,
        "_bounded_aggregate_prefix",
        lambda self, result_watermark: False,
    )
    service = TrustedLiveReadService(
        writer.database_path,
        expected_database_instance=accepted,
        request_timeout_seconds=30.0,
    )
    try:
        bootstrap = service.submit_bootstrap().wait(30.0)
        _drain_run_pages(service, 0, bootstrap.run_id_watermark)

        expensive = service.submit_expensive_run(1).wait(30.0)
        selected = service.submit_selected_run(1).wait(30.0)

        assert expensive.as_dict()["result_count"] == 1
        assert expensive.as_dict()["storage_bytes_estimated"] is True
        assert selected.setpoint_summaries
        assert selected.setpoint_summaries[0].first == 0.0
        assert selected.setpoint_summaries[0].last == 0.0
        assert writer.request("checkpoint", mode="TRUNCATE") == (0, 0, 0)
    finally:
        service.close(timeout=30.0)


@pytest.mark.skipif(os.name != "posix", reason="Idle-helper SIGKILL regression")
def test_refresh_reconciles_real_idle_helper_respawn_at_same_data_version(
    stage4_wal_writer: _QcodesWalWriter,
) -> None:
    writer = stage4_wal_writer
    accepted = database_instance(writer.database_path)
    supervisor = TrustedLiveReaderSupervisor.open(
        writer.database_path,
        expected_database_instance=accepted,
    )
    adapter = TrustedMetadataQueryAdapter(supervisor, writer.database_path)
    try:
        bootstrap = adapter.bootstrap()
        cursor = 0
        while cursor < bootstrap.run_id_watermark:
            page = adapter.basic_run_page(cursor, bootstrap.run_id_watermark)
            if page.complete:
                break
            cursor = page.next_run_id
        adapter.refresh_new_runs(accepted_run_id=bootstrap.run_id_watermark)
        unchanged = adapter.refresh_new_runs(accepted_run_id=bootstrap.run_id_watermark)
        assert not unchanged.data_version_changed

        first_pid = supervisor.helper_pid
        first_incarnation = supervisor.incarnation
        assert first_pid is not None
        os.kill(first_pid, signal.SIGKILL)
        _wait_for(lambda: not supervisor.helper_alive)

        restarted = adapter.refresh_new_runs(accepted_run_id=bootstrap.run_id_watermark)

        assert restarted.data_version == unchanged.data_version
        assert restarted.data_version_changed
        assert restarted.helper_incarnation == first_incarnation + 1
        assert restarted.run_id_watermark == bootstrap.run_id_watermark
        assert supervisor.helper_alive
        assert writer.request("checkpoint", mode="TRUNCATE") == (0, 0, 0)
    finally:
        supervisor.close()


def test_basic_page_stays_under_real_wire_budget_with_large_descriptions(
    stage4_wal_writer: _QcodesWalWriter,
) -> None:
    writer = stage4_wal_writer
    connection = apsw.Connection(str(writer.database_path))
    try:
        columns = tuple(
            str(row[1]) for row in connection.execute("PRAGMA table_info(runs)")
        )
        source = dict(
            zip(
                columns,
                connection.execute("SELECT * FROM runs WHERE run_id = 1").fetchone(),
                strict=True,
            )
        )
        description = json.dumps(
            {"interdependencies_": {}, "wire_padding": "x" * (34 * 1024)},
            separators=(",", ":"),
        )
        quoted_columns = ", ".join(
            '"' + name.replace('"', '""') + '"' for name in columns
        )
        placeholders = ", ".join("?" for _ in columns)
        rows = []
        for run_id in range(2, TRUSTED_RUN_PAGE_SIZE + 1):
            copied = dict(source)
            copied.update(
                {
                    "run_id": None,
                    "name": f"large-description-{run_id}",
                    "result_table_name": f"results_large_description_{run_id}",
                    "guid": f"00000000-0000-0000-0001-{run_id:012d}",
                    "run_description": description,
                    "captured_run_id": run_id,
                    "captured_counter": run_id,
                    "result_counter": run_id,
                }
            )
            rows.append(tuple(copied[name] for name in columns))
        with connection:
            connection.execute(
                "UPDATE runs SET run_description = ? WHERE run_id = 1",
                (description,),
            )
            connection.executemany(
                f"INSERT INTO runs ({quoted_columns}) VALUES ({placeholders})",
                rows,
            )

        accepted = database_instance(writer.database_path)
        with TrustedLiveReaderSupervisor.open(
            writer.database_path,
            expected_database_instance=accepted,
        ) as supervisor:
            adapter = TrustedMetadataQueryAdapter(supervisor, writer.database_path)
            bootstrap = adapter.bootstrap()
            page = adapter.basic_run_page(0, bootstrap.run_id_watermark)

        assert bootstrap.run_id_watermark == TRUSTED_RUN_PAGE_SIZE
        assert page.complete
        assert len(page.runs) == TRUSTED_RUN_PAGE_SIZE
        assert all("run_description" not in record.as_dict() for record in page.runs)
        assert all(record.as_dict()["name"] for record in page.runs)
    finally:
        connection.close()


class _SignallingSupervisor:
    """Delegate to a real supervisor while exposing active-job test barriers."""

    def __init__(self, supervisor: TrustedLiveReaderSupervisor) -> None:
        self._supervisor = supervisor
        self.query_submitted = threading.Event()
        self.cancel_completed = threading.Event()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._supervisor, name)

    def submit_query(self, *args: object, **kwargs: object) -> Any:
        job = self._supervisor.submit_query(*args, **kwargs)
        self.query_submitted.set()
        return job

    def cancel(self, *args: object, **kwargs: object) -> bool:
        try:
            return self._supervisor.cancel(*args, **kwargs)
        finally:
            self.cancel_completed.set()


def test_real_stage4_active_read_cancels_boundedly_and_releases_checkpoint(
    stage4_wal_writer: _QcodesWalWriter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = stage4_wal_writer
    real_adapter = TrustedMetadataQueryAdapter

    class SlowFirstBootstrapAdapter(real_adapter):
        def bootstrap(self) -> TrustedBootstrapResult:
            if not getattr(self, "_stage4_slow_bootstrap_attempted", False):
                self._stage4_slow_bootstrap_attempted = True
                self._executor.query(
                    "WITH RECURSIVE values_(n) AS ("
                    "SELECT 1 UNION ALL SELECT n + 1 FROM values_ "
                    "WHERE n < 100000000) SELECT sum(n) FROM values_"
                )
            return super().bootstrap()

    monkeypatch.setattr(
        service_module,
        "TrustedMetadataQueryAdapter",
        SlowFirstBootstrapAdapter,
    )
    wrappers: list[_SignallingSupervisor] = []

    def supervisor_factory(database_path: str, **options: object) -> Any:
        wrapper = _SignallingSupervisor(
            TrustedLiveReaderSupervisor.open(database_path, **options)
        )
        wrappers.append(wrapper)
        return wrapper

    before_read = _stable_source_state(writer.database_path)
    service = TrustedLiveReadService(
        writer.database_path,
        expected_database_instance=database_instance(writer.database_path),
        request_timeout_seconds=30.0,
        supervisor_factory=supervisor_factory,
        supervisor_options={"cancellation_grace_seconds": 2.0},
    )
    try:
        request = service.submit_bootstrap()
        _wait_for(lambda: bool(wrappers))
        wrapper = wrappers[0]
        assert wrapper.query_submitted.wait(10.0)

        cancel_started = time.monotonic()
        assert request.cancel()
        assert time.monotonic() - cancel_started < 0.5
        with pytest.raises(TrustedReadRequestCancelledError):
            request.wait(0.0)
        assert wrapper.cancel_completed.wait(10.0)

        def operation_retired() -> bool:
            with service._condition:
                return service._active_operation is None

        _wait_for(operation_retired)
        assert not service.closed
        assert service.liveness().helper_alive
        assert service.liveness().outstanding_requests == 0

        # The same service and helper remain usable after exact cooperative
        # cancellation; this second attempt executes the real fixed adapter.
        bootstrap = service.submit_bootstrap().wait(30.0)
        records = _drain_run_pages(service, 0, bootstrap.run_id_watermark)
        assert [record.run_id for record in records] == [1]
        assert len(wrappers) == 1

        after_read = _stable_source_state(writer.database_path)
        _assert_protected_artifacts_unchanged(before_read, after_read)
        assert writer.request("checkpoint", mode="TRUNCATE") == (0, 0, 0)
    finally:
        service.close(timeout=30.0)
