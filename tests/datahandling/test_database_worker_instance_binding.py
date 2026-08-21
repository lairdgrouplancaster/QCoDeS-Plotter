"""End-to-end database-instance binding regressions for metadata workers."""

import json
import os
import sqlite3
from pathlib import Path

import numpy as np
import pytest
from qcodes.dataset import Measurement, load_experiment
from qcodes.parameters import ManualParameter

from qplot import testdata as testdata_module
from qplot.datahandling import database as database_module
from qplot.datahandling import readonly as readonly_module
from qplot.datahandling import readSQL
from qplot.datahandling.file_identity import (
    DatabaseInstance,
    database_file_identity,
    database_instance,
)
from qplot.datahandling.readonly import DatabaseInstanceChangedError
from qplot.testdata import RunSpecification, generate_database
from qplot.windows import _database_actions as database_actions

_SQLITE_ARTIFACT_SUFFIXES = ("", "-wal", "-shm", "-journal")


def _artifact_state(database_path: Path):
    """Capture bytes, identity, metadata, and absence for every SQLite artifact."""

    state = {}
    for suffix in _SQLITE_ARTIFACT_SUFFIXES:
        artifact_path = Path(f"{database_path}{suffix}")
        try:
            contents = artifact_path.read_bytes()
            status = artifact_path.stat()
        except FileNotFoundError:
            state[suffix] = None
            continue
        state[suffix] = (
            contents,
            database_file_identity(artifact_path),
            status.st_dev,
            status.st_ino,
            status.st_nlink,
            status.st_size,
            status.st_mtime_ns,
        )
    return state


def _build_qcodes_database(
    database_path: Path,
    point_counts: tuple[int, ...],
    *,
    seed: int,
):
    """Create a real latest-schema QCoDeS database with compact completed runs."""

    specifications = [
        RunSpecification(
            dimensions=1,
            measured_name=f"signal_{index}",
            measured_label=f"Signal {index}",
            measured_unit="V",
            v_sd_start=0.0,
            v_sd_stop=1.0,
            v_sd_points=point_count,
        )
        for index, point_count in enumerate(point_counts, start=1)
    ]
    generate_database(
        specifications,
        database_path,
        rng=np.random.default_rng(seed),
    )

    # A checkpointed WAL-format main makes qPlot open a private snapshot even
    # without sidecars. That lets the test restore A while an unbound old worker
    # still has B open, including on Windows where an open source cannot be moved.
    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
    finally:
        connection.close()
    assert database_path.read_bytes()[18:20] == b"\x02\x02"
    assert all(
        not Path(f"{database_path}{suffix}").exists()
        for suffix in _SQLITE_ARTIFACT_SUFFIXES[1:]
    )

    connection = sqlite3.connect(
        f"{database_path.resolve().as_uri()}?mode=ro&immutable=1",
        uri=True,
    )
    try:
        return {
            run_id: {
                "guid": guid,
                "result_table_name": table_name,
            }
            for run_id, guid, table_name in connection.execute(
                "SELECT run_id, guid, result_table_name FROM runs ORDER BY run_id"
            )
        }
    finally:
        connection.close()


def _set_run_guid(database_path: Path, run_id: int, guid: str):
    """Give an independent QCoDeS run a watched GUID used by another instance."""

    connection = sqlite3.connect(database_path)
    try:
        connection.execute("UPDATE runs SET guid = ? WHERE run_id = ?", (guid, run_id))
        connection.commit()
    finally:
        connection.close()
    assert all(
        not Path(f"{database_path}{suffix}").exists()
        for suffix in _SQLITE_ARTIFACT_SUFFIXES[1:]
    )


class _MainFileABASwap:
    """Temporarily install B at A's path, then restore both exact instances."""

    def __init__(self, selected_path: Path, replacement_path: Path):
        self.selected_path = selected_path
        self.replacement_path = replacement_path
        self.parked_path = selected_path.with_name(f"{selected_path.stem}-parked-a.db")
        self.count = 0

    def around(self, operation):
        assert not self.parked_path.exists()
        os.replace(self.selected_path, self.parked_path)
        os.replace(self.replacement_path, self.selected_path)
        self.count += 1
        try:
            return operation()
        finally:
            os.replace(self.selected_path, self.replacement_path)
            os.replace(self.parked_path, self.selected_path)


def _aba_on_connection(monkeypatch, swap: _MainFileABASwap, *, call_number: int):
    """Install B only while one readSQL connection is being opened."""

    real_open = readSQL.qcodes_read_only_connection
    calls = []

    def open_with_aba(database_path, *args, **kwargs):
        calls.append((database_path, kwargs.get("expected_database_identity")))
        if len(calls) != call_number:
            return real_open(database_path, *args, **kwargs)
        return swap.around(lambda: real_open(database_path, *args, **kwargs))

    monkeypatch.setattr(readSQL, "qcodes_read_only_connection", open_with_aba)
    return calls


def _collect_worker(worker):
    batches = []
    finished = []
    batch_ready = getattr(worker.signals, "batch_ready", None)
    if batch_ready is not None:
        batch_ready.connect(lambda *args: batches.append(args))
    worker.signals.finished.connect(lambda *args: finished.append(args))
    worker.run()
    return batches, finished


def _assert_bound_worker(worker, accepted_instance: DatabaseInstance):
    assert worker.database_instance is accepted_instance
    assert worker.database_path == accepted_instance.logical_path
    assert worker.logical_database_path == accepted_instance.logical_path
    assert worker.resolved_database_path == accepted_instance.resolved_path
    assert worker.database_identity == accepted_instance.identity
    assert worker.sidecar_identities == accepted_instance.sidecar_identities


def test_metadata_workers_retain_the_exact_scheduled_database_instance(tmp_path):
    database_path = tmp_path / "scheduled.db"
    _build_qcodes_database(database_path, (2,), seed=1)
    accepted_instance = database_instance(database_path)

    workers = (
        database_module.DatabaseLoadWorker(
            1,
            str(database_path),
            expected_database_instance=accepted_instance,
        ),
        database_module.DatabaseRefreshWorker(
            2,
            str(database_path),
            0,
            [],
            expected_database_instance=accepted_instance,
        ),
        database_module.DatabaseDetailWorker(
            3,
            str(database_path),
            [1],
            expected_database_instance=accepted_instance,
        ),
        database_module.DatabaseExpensiveDetailWorker(
            4,
            str(database_path),
            [1],
            expected_database_instance=accepted_instance,
        ),
    )

    for worker in workers:
        _assert_bound_worker(worker, accepted_instance)


def test_database_load_worker_rejects_read_open_aba_without_emitting_b(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "selected.db"
    replacement_path = tmp_path / "replacement.db"
    runs_a = _build_qcodes_database(database_path, (2,), seed=2)
    runs_b = _build_qcodes_database(replacement_path, (7,), seed=3)
    accepted_instance = database_instance(database_path)
    before = {
        database_path: _artifact_state(database_path),
        replacement_path: _artifact_state(replacement_path),
    }
    probe_identities = []

    def successful_probe(
        _database_path,
        timeout=database_module.DATABASE_ACCESS_TIMEOUT_SECONDS,
        expected_database_identity=None,
    ):
        del timeout
        probe_identities.append(expected_database_identity)
        return None

    monkeypatch.setattr(database_module, "database_access_error", successful_probe)
    swap = _MainFileABASwap(database_path, replacement_path)
    connection_calls = _aba_on_connection(monkeypatch, swap, call_number=1)
    worker = database_module.DatabaseLoadWorker(
        10,
        str(database_path),
        expected_database_instance=accepted_instance,
    )

    _batches, finished = _collect_worker(worker)

    assert swap.count == 1
    assert probe_identities == [accepted_instance.identity]
    assert connection_calls == [
        (accepted_instance.logical_path, accepted_instance.identity)
    ]
    assert len(finished) == 1
    assert finished[0][:3] == (10, accepted_instance.logical_path, {})
    assert isinstance(finished[0][3], DatabaseInstanceChangedError)
    assert runs_a[1]["guid"] != runs_b[1]["guid"]
    assert database_instance(database_path).identity == accepted_instance.identity
    assert _artifact_state(database_path) == before[database_path]
    assert _artifact_state(replacement_path) == before[replacement_path]


def test_database_load_worker_binds_the_subprocess_access_probe_across_aba(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "probe-selected.db"
    replacement_path = tmp_path / "probe-replacement.db"
    _build_qcodes_database(database_path, (2,), seed=4)
    _build_qcodes_database(replacement_path, (5,), seed=5)
    accepted_instance = database_instance(database_path)
    before = {
        database_path: _artifact_state(database_path),
        replacement_path: _artifact_state(replacement_path),
    }
    swap = _MainFileABASwap(database_path, replacement_path)
    real_run = database_module.subprocess.run
    child_expected_identities = []

    def run_child_with_aba(command, **kwargs):
        encoded_identity = json.loads(command[-1])
        child_expected_identities.append(
            None if encoded_identity is None else tuple(encoded_identity)
        )
        return swap.around(lambda: real_run(command, **kwargs))

    monkeypatch.setattr(database_module.subprocess, "run", run_child_with_aba)
    worker = database_module.DatabaseLoadWorker(
        11,
        str(database_path),
        expected_database_instance=accepted_instance,
    )

    _batches, finished = _collect_worker(worker)

    assert swap.count == 1
    assert child_expected_identities == [accepted_instance.identity]
    assert len(finished) == 1
    assert finished[0][:3] == (11, accepted_instance.logical_path, {})
    assert isinstance(finished[0][3], DatabaseInstanceChangedError)
    assert database_instance(database_path).identity == accepted_instance.identity
    assert _artifact_state(database_path) == before[database_path]
    assert _artifact_state(replacement_path) == before[replacement_path]


def test_database_refresh_worker_discards_new_runs_when_watched_status_open_is_b(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "refresh-selected.db"
    replacement_path = tmp_path / "refresh-replacement.db"
    runs_a = _build_qcodes_database(database_path, (2, 3), seed=6)
    runs_b = _build_qcodes_database(replacement_path, (11, 13), seed=7)
    watched_guid = runs_a[1]["guid"]
    _set_run_guid(replacement_path, 1, watched_guid)
    accepted_instance = database_instance(database_path)
    before = {
        database_path: _artifact_state(database_path),
        replacement_path: _artifact_state(replacement_path),
    }
    swap = _MainFileABASwap(database_path, replacement_path)
    connection_calls = _aba_on_connection(monkeypatch, swap, call_number=2)
    worker = database_module.DatabaseRefreshWorker(
        20,
        str(database_path),
        1,
        [watched_guid],
        expected_database_instance=accepted_instance,
    )

    _batches, finished = _collect_worker(worker)

    assert swap.count == 1
    assert len(connection_calls) == 2
    assert all(
        expected_identity == accepted_instance.identity
        for _path, expected_identity in connection_calls
    )
    assert len(finished) == 1
    assert finished[0][:4] == (
        20,
        accepted_instance.logical_path,
        {},
        {},
    )
    assert isinstance(finished[0][4], DatabaseInstanceChangedError)
    assert runs_a[2]["guid"] != runs_b[2]["guid"]
    assert database_instance(database_path).identity == accepted_instance.identity
    assert _artifact_state(database_path) == before[database_path]
    assert _artifact_state(replacement_path) == before[replacement_path]


def test_database_detail_worker_binds_every_iterated_batch_and_suppresses_b(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "detail-selected.db"
    replacement_path = tmp_path / "detail-replacement.db"
    runs_a = _build_qcodes_database(database_path, (2, 3), seed=8)
    runs_b = _build_qcodes_database(replacement_path, (17, 19), seed=9)
    accepted_instance = database_instance(database_path)
    before = {
        database_path: _artifact_state(database_path),
        replacement_path: _artifact_state(replacement_path),
    }
    swap = _MainFileABASwap(database_path, replacement_path)
    connection_calls = _aba_on_connection(monkeypatch, swap, call_number=2)
    worker = database_module.DatabaseDetailWorker(
        30,
        str(database_path),
        [1, 2],
        batch_size=1,
        expected_database_instance=accepted_instance,
    )

    batches, finished = _collect_worker(worker)

    assert swap.count == 1
    assert len(connection_calls) == 2
    assert all(
        expected_identity == accepted_instance.identity
        for _path, expected_identity in connection_calls
    )
    emitted_guids = {
        metadata["guid"]
        for _generation, _path, details in batches
        for metadata in details.values()
    }
    assert emitted_guids <= {metadata["guid"] for metadata in runs_a.values()}
    assert emitted_guids.isdisjoint(
        {metadata["guid"] for metadata in runs_b.values()}
    )
    assert len(finished) == 1
    assert finished[0][:2] == (30, accepted_instance.logical_path)
    assert isinstance(finished[0][2], DatabaseInstanceChangedError)
    assert database_instance(database_path).identity == accepted_instance.identity
    assert _artifact_state(database_path) == before[database_path]
    assert _artifact_state(replacement_path) == before[replacement_path]


def test_expensive_detail_worker_binds_storage_phase_and_suppresses_b(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "expensive-selected.db"
    replacement_path = tmp_path / "expensive-replacement.db"
    runs_a = _build_qcodes_database(database_path, (5,), seed=10)
    runs_b = _build_qcodes_database(replacement_path, (23,), seed=11)
    accepted_instance = database_instance(database_path)
    before = {
        database_path: _artifact_state(database_path),
        replacement_path: _artifact_state(replacement_path),
    }
    swap = _MainFileABASwap(database_path, replacement_path)
    connection_calls = _aba_on_connection(monkeypatch, swap, call_number=2)
    worker = database_module.DatabaseExpensiveDetailWorker(
        40,
        str(database_path),
        [1],
        batch_size=1,
        expected_database_instance=accepted_instance,
    )

    batches, finished = _collect_worker(worker)

    assert swap.count == 1
    assert len(connection_calls) == 2
    assert all(
        expected_identity == accepted_instance.identity
        for _path, expected_identity in connection_calls
    )
    assert batches
    assert all(
        metadata["guid"] == runs_a[run_id]["guid"]
        for _generation, _path, details in batches
        for run_id, metadata in details.items()
    )
    assert all(
        "storage_bytes" not in metadata
        for _generation, _path, details in batches
        for metadata in details.values()
    )
    assert runs_a[1]["guid"] != runs_b[1]["guid"]
    assert len(finished) == 1
    assert finished[0][:2] == (40, accepted_instance.logical_path)
    assert isinstance(finished[0][2], DatabaseInstanceChangedError)
    assert database_instance(database_path).identity == accepted_instance.identity
    assert _artifact_state(database_path) == before[database_path]
    assert _artifact_state(replacement_path) == before[replacement_path]


def test_same_instance_qcodes_live_write_is_accepted_and_source_is_unchanged(
    tmp_path,
):
    database_path = tmp_path / "live.db"
    existing_runs = _build_qcodes_database(database_path, (2,), seed=12)
    accepted_instance = database_instance(database_path)
    writer = testdata_module._connect_writable_exact_path(database_path)
    experiment = None
    dataset = None
    try:
        testdata_module.enable_generation_provenance_for_writer(writer)
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        experiment = load_experiment(1, conn=writer)
        setpoint = ManualParameter("live_worker_setpoint")
        signal = ManualParameter("live_worker_signal")
        measurement = Measurement(exp=experiment, name="live_worker_run")
        measurement.register_parameter(setpoint)
        measurement.register_parameter(signal, setpoints=(setpoint,))

        with measurement.run(write_in_background=False) as datasaver:
            dataset = datasaver.dataset
            datasaver.add_result((setpoint, 1.0), (signal, 2.0))
            datasaver.flush_data_to_database(block=True)
            worker = database_module.DatabaseRefreshWorker(
                50,
                str(database_path),
                1,
                [dataset.guid, existing_runs[1]["guid"]],
                expected_database_instance=accepted_instance,
            )
            before_worker = _artifact_state(database_path)

            _batches, finished = _collect_worker(worker)

            assert _artifact_state(database_path) == before_worker
            assert database_instance(database_path).identity == accepted_instance.identity
            assert len(finished) == 1
            generation, path, new_runs, statuses, error = finished[0]
            assert (generation, path, error) == (
                50,
                accepted_instance.logical_path,
                None,
            )
            assert new_runs[dataset.run_id]["guid"] == dataset.guid
            assert statuses[dataset.guid]["result_count"] == 1
            assert statuses[existing_runs[1]["guid"]]["result_count"] == 2
    finally:
        if dataset is not None:
            dataset.conn.close()
        if experiment is not None:
            experiment.conn.close()
        writer.close()


def test_one_way_replacement_rejects_before_touching_any_sqlite_artifact(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "one-way-selected.db"
    replacement_path = tmp_path / "one-way-replacement.db"
    parked_path = tmp_path / "one-way-parked-a.db"
    _build_qcodes_database(database_path, (2,), seed=13)
    _build_qcodes_database(replacement_path, (29,), seed=14)
    accepted_instance = database_instance(database_path)
    os.replace(database_path, parked_path)
    os.replace(replacement_path, database_path)
    for suffix in _SQLITE_ARTIFACT_SUFFIXES[1:]:
        Path(f"{database_path}{suffix}").write_bytes(f"owned {suffix}".encode())
    replacement_state = _artifact_state(database_path)
    parked_state = _artifact_state(parked_path)
    monkeypatch.setattr(database_module, "database_access_error", lambda *_a, **_k: None)
    worker = database_module.DatabaseLoadWorker(
        60,
        str(database_path),
        expected_database_instance=accepted_instance,
    )

    _batches, finished = _collect_worker(worker)

    assert len(finished) == 1
    assert finished[0][:3] == (60, accepted_instance.logical_path, {})
    assert isinstance(finished[0][3], DatabaseInstanceChangedError)
    assert _artifact_state(database_path) == replacement_state
    assert _artifact_state(parked_path) == parked_state


def test_symlink_target_replacement_is_rejected_by_captured_full_instance(
    tmp_path,
    monkeypatch,
):
    target_a = tmp_path / "target-a.db"
    target_b = tmp_path / "target-b.db"
    view_path = tmp_path / "view.db"
    next_link = tmp_path / "next-view.db"
    _build_qcodes_database(target_a, (2,), seed=15)
    _build_qcodes_database(target_b, (31,), seed=16)
    try:
        view_path.symlink_to(target_a)
        next_link.symlink_to(target_b)
    except OSError as error:
        pytest.skip(f"This platform cannot create symlinks: {error}")
    accepted_instance = database_instance(view_path)
    target_a_state = _artifact_state(target_a)
    target_b_state = _artifact_state(target_b)
    os.replace(next_link, view_path)
    monkeypatch.setattr(database_module, "database_access_error", lambda *_a, **_k: None)
    worker = database_module.DatabaseLoadWorker(
        70,
        str(view_path),
        expected_database_instance=accepted_instance,
    )

    _batches, finished = _collect_worker(worker)

    _assert_bound_worker(worker, accepted_instance)
    assert len(finished) == 1
    assert isinstance(finished[0][3], DatabaseInstanceChangedError)
    assert database_instance(view_path).resolved_path != accepted_instance.resolved_path
    assert _artifact_state(target_a) == target_a_state
    assert _artifact_state(target_b) == target_b_state


def test_detail_worker_cancellation_closes_its_snapshot_and_stops_later_batches(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "cancel.db"
    runs = _build_qcodes_database(database_path, (2, 3), seed=17)
    accepted_instance = database_instance(database_path)
    before = _artifact_state(database_path)
    worker = database_module.DatabaseDetailWorker(
        80,
        str(database_path),
        [1, 2],
        batch_size=1,
        expected_database_instance=accepted_instance,
    )
    batches = []
    finished = []
    snapshot_directories = []
    real_temporary_directory = readonly_module.tempfile.TemporaryDirectory

    def tracked_temporary_directory(*args, **kwargs):
        snapshot = real_temporary_directory(*args, **kwargs)
        snapshot_directories.append(Path(snapshot.name))
        return snapshot

    monkeypatch.setattr(
        readonly_module.tempfile,
        "TemporaryDirectory",
        tracked_temporary_directory,
    )

    def cancel_after_first_batch(*args):
        batches.append(args)
        worker.cancel()

    worker.signals.batch_ready.connect(cancel_after_first_batch)
    worker.signals.finished.connect(lambda *args: finished.append(args))
    worker.run()

    assert len(batches) == 1
    assert next(iter(batches[0][2].values()))["guid"] in {
        metadata["guid"] for metadata in runs.values()
    }
    assert finished == []
    assert worker._cancelled.is_set()
    assert snapshot_directories
    assert all(not path.exists() for path in snapshot_directories)
    assert _artifact_state(database_path) == before


def test_stale_detail_callbacks_cannot_publish_partial_batches():
    class Field:
        @staticmethod
        def text():
            return "current.db"

    class RunList:
        @staticmethod
        def updateRuns(_runs):
            raise AssertionError("A stale detail callback reached the run-list cache")

    class Harness:
        database_detail_batch_ready = (
            database_actions.DatabaseActionsMixin.database_detail_batch_ready
        )
        database_expensive_detail_batch_ready = (
            database_actions.DatabaseActionsMixin.database_expensive_detail_batch_ready
        )

        def __init__(self):
            self._database_detail_generation = 5
            self._database_detail_active = True
            self._database_expensive_detail_generation = 7
            self._database_expensive_detail_active = True
            self.fileTextbox = Field()
            self.RunList = RunList()

    harness = Harness()
    stale_b = {1: {"guid": "b-guid", "result_count": 999}}

    harness.database_detail_batch_ready(4, "current.db", stale_b)
    harness.database_expensive_detail_batch_ready(6, "current.db", stale_b)


def test_load_instance_error_invalidates_generation_and_uses_replacement_reload(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "load-callback.db"
    _build_qcodes_database(database_path, (2,), seed=18)
    accepted_instance = database_instance(database_path)

    class Harness:
        database_load_finished = (
            database_actions.DatabaseActionsMixin.database_load_finished
        )

        def __init__(self):
            self._database_load_generation = 90
            self._database_load_active = True
            self._database_load_state = {
                "abspath": str(database_path),
                "load_instance": accepted_instance,
                "load_identity": accepted_instance.identity,
                "load_started_at": 123.0,
                "generation_recovery": False,
            }
            self._database_load_worker = object()
            self.reloads = []
            self.statuses = []

        @staticmethod
        def _set_database_load_controls_enabled(_enabled):
            return None

        @staticmethod
        def _hide_database_load_panel():
            return None

        def show_status(self, message, timeout=5000):
            self.statuses.append((message, timeout))

        def _reload_replaced_database(self, path, **kwargs):
            self.reloads.append((path, kwargs))

    monkeypatch.setattr(
        database_actions.QtCore.QTimer,
        "singleShot",
        lambda _delay, callback: callback(),
    )
    harness = Harness()

    harness.database_load_finished(
        90,
        str(database_path),
        {1: {"guid": "must-not-commit"}},
        DatabaseInstanceChangedError("injected ABA mismatch"),
    )

    assert harness._database_load_generation == 91
    assert not harness._database_load_active
    assert harness._database_load_state is None
    assert harness._database_load_worker is None
    assert harness.reloads == [(
        str(database_path),
        {"generation_recovery": False, "load_started_at": 123.0},
    )]
    assert harness.statuses[-1] == (
        "Database changed while loading; retrying...",
        0,
    )


def test_refresh_instance_error_discards_results_invalidates_and_reloads(tmp_path):
    database_path = tmp_path / "refresh-callback.db"
    _build_qcodes_database(database_path, (2,), seed=19)
    accepted_instance = database_instance(database_path)

    class Field:
        @staticmethod
        def text():
            return str(database_path)

    class Worker:
        def __init__(self):
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

    class Harness:
        database_refresh_finished = (
            database_actions.DatabaseActionsMixin.database_refresh_finished
        )

        def __init__(self):
            self.fileTextbox = Field()
            self._database_refresh_generation = 100
            self._database_refresh_active = True
            self._database_refresh_pending = True
            self._database_refresh_identity = accepted_instance.identity
            self._database_refresh_instance = accepted_instance
            self._database_refresh_worker = Worker()
            self.cancelled_worker = self._database_refresh_worker
            self.reloads = []

        def _reload_replaced_database(self, path):
            self.reloads.append(path)

        @staticmethod
        def _apply_database_refresh_result(_new_runs, _statuses):
            raise AssertionError("Instance-mismatched refresh data reached the UI")

    harness = Harness()

    harness.database_refresh_finished(
        100,
        str(database_path),
        {2: {"guid": "must-not-commit"}},
        {"watched": {"result_count": 999}},
        DatabaseInstanceChangedError("injected watched-status ABA mismatch"),
    )

    assert harness.cancelled_worker.cancelled
    assert harness._database_refresh_generation == 101
    assert not harness._database_refresh_active
    assert not harness._database_refresh_pending
    assert harness._database_refresh_worker is None
    assert harness.reloads == [str(database_path)]


@pytest.mark.parametrize(
    ("callback_name", "generation_attribute", "generation"),
    [
        ("database_detail_finished", "_database_detail_generation", 110),
        (
            "database_expensive_detail_finished",
            "_database_expensive_detail_generation",
            120,
        ),
    ],
)
def test_detail_instance_error_invalidates_both_workers_and_reloads(
    callback_name,
    generation_attribute,
    generation,
):
    class Field:
        @staticmethod
        def text():
            return "details.db"

    class Worker:
        def __init__(self):
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

    class Harness:
        database_detail_finished = (
            database_actions.DatabaseActionsMixin.database_detail_finished
        )
        database_expensive_detail_finished = (
            database_actions.DatabaseActionsMixin.database_expensive_detail_finished
        )

        def __init__(self):
            self.fileTextbox = Field()
            self._database_detail_generation = 110
            self._database_detail_active = True
            self._database_detail_worker = Worker()
            self._database_detail_instance = None
            self._database_expensive_detail_generation = 120
            self._database_expensive_detail_active = True
            self._database_expensive_detail_worker = Worker()
            self._database_expensive_detail_instance = None
            self.detail_worker = self._database_detail_worker
            self.expensive_worker = self._database_expensive_detail_worker
            self.reloads = []

        def _reload_replaced_database(self, path):
            self.reloads.append(path)

        @staticmethod
        def show_status(_message, _timeout=5000):
            raise AssertionError("Instance mismatch was reported as an ordinary error")

    harness = Harness()
    callback = getattr(harness, callback_name)

    callback(
        generation,
        "details.db",
        DatabaseInstanceChangedError("injected detail ABA mismatch"),
    )

    assert harness._database_detail_generation > 110
    assert harness._database_expensive_detail_generation > 120
    assert not harness._database_detail_active
    assert not harness._database_expensive_detail_active
    assert harness._database_detail_worker is None
    assert harness._database_expensive_detail_worker is None
    assert harness.reloads == ["details.db"]
    if generation_attribute == "_database_detail_generation":
        assert harness.expensive_worker.cancelled
    else:
        assert harness.detail_worker.cancelled
