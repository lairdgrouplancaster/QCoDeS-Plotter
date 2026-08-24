"""Process-boundary tests for the trusted live-database supervisor."""

from __future__ import annotations

import gc
import json
import multiprocessing
import os
import sys
import threading
import time
import traceback
import weakref
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from multiprocessing.connection import Connection
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

from qplot.datahandling import _trusted_live_protocol as protocol_module
from qplot.datahandling import trusted_live_supervisor as supervisor_module
from qplot.datahandling._trusted_live_protocol import (
    MAX_BATCH_QUERIES,
    MAX_BINDINGS_PER_QUERY,
    MAX_CONTROL_BYTES,
    MAX_PATH_BYTES,
    MAX_REQUEST_BYTES,
    MAX_SCALAR_BYTES,
    MAX_SQL_BYTES,
    PROTOCOL_VERSION,
    ProtocolEnvelope,
    TrustedLiveProtocolValidationError,
    decode_control_frame,
    decode_database_instance,
    decode_request_frame,
    encode_cancel,
    encode_database_instance,
    encode_frame,
    encode_job_request,
    encode_query_results,
    validate_job_success,
)
from qplot.datahandling.file_identity import DatabaseInstance, database_instance
from qplot.datahandling.trusted_live import (
    TrustedLiveCancelledError,
    TrustedLiveCleanupError,
    TrustedLiveDeadlineExceededError,
    TrustedLiveSourceChangedError,
    TrustedQuery,
    TrustedQueryResult,
)
from qplot.datahandling.trusted_live_supervisor import (
    TrustedLiveHelperExitedError,
    TrustedLiveHelperForcedTerminationError,
    TrustedLiveHelperReplyTimeoutError,
    TrustedLiveHelperStartupError,
    TrustedLiveProtocolError,
    TrustedLiveReaderSupervisor,
    TrustedLiveSupervisorClosedError,
    TrustedLiveSupervisorError,
)

pytestmark = pytest.mark.timeout(120)

_TEST_PROTOCOL_SESSION = "1" * 32
_UNCLOSED_CHILD_SUPERVISOR: TrustedLiveReaderSupervisor | None = None


class _ContextBodyError(Exception):
    """Sentinel proving cleanup failures do not replace a body exception."""


class _OverLimitBindingsSequence(Sequence[object]):
    """Expose only an excessive length; every materialisation path is fatal."""

    def __len__(self) -> int:
        return MAX_BINDINGS_PER_QUERY + 1

    def __getitem__(self, index: int | slice) -> object:
        raise AssertionError(f"Oversized bindings must not be indexed: {index!r}")


class _OverLimitBindingsMapping(Mapping[str, object]):
    """Expose only an excessive length; every materialisation path is fatal."""

    def __len__(self) -> int:
        return MAX_BINDINGS_PER_QUERY + 1

    def __iter__(self) -> Any:
        raise AssertionError("Oversized bindings must not be iterated")

    def __getitem__(self, key: str) -> object:
        raise AssertionError(f"Oversized bindings must not be indexed: {key!r}")


def _raw_protocol_envelope(
    *,
    version: int = PROTOCOL_VERSION,
    operation: str = "query",
    extra: bool = False,
) -> bytes:
    envelope: dict[str, Any] = {
        "protocol_version": version,
        "session": _TEST_PROTOCOL_SESSION,
        "generation": 1,
        "operation": operation,
        "payload": {},
    }
    if extra:
        envelope["unexpected"] = True
    return json.dumps(envelope, separators=(",", ":")).encode()


def _return_with_live_supervisor_process(
    database_path: str,
    control: Connection,
) -> None:
    """Return normally while a module global retains a live helper owner."""

    global _UNCLOSED_CHILD_SUPERVISOR
    try:
        _UNCLOSED_CHILD_SUPERVISOR = TrustedLiveReaderSupervisor.open(database_path)
        control.send(("ready", _UNCLOSED_CHILD_SUPERVISOR.helper_pid))
    except BaseException:
        control.send(("error", traceback.format_exc()))
    finally:
        # Deliberately do not close the supervisor. Keeping it in a module
        # global carries its endpoints into multiprocessing's atexit phase.
        control.close()


def _apsw_wal_writer_process(database_path: str, control: Connection) -> None:
    """Hold a WAL writer open and service spawn-safe test commands."""

    connection: apsw.Connection | None = None
    try:
        connection = apsw.Connection(database_path)
        journal_mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
        if journal_mode is None or str(journal_mode[0]).casefold() != "wal":
            raise RuntimeError(f"Could not enable WAL mode: {journal_mode!r}")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute(
            "CREATE TABLE supervisor_probe ("
            "seq INTEGER PRIMARY KEY, value TEXT NOT NULL, payload BLOB NOT NULL)"
        )
        connection.execute(
            "INSERT INTO supervisor_probe(seq, value, payload) "
            "VALUES(0, 'initial', X'')"
        )
        control.send(("ready", None))

        while True:
            command, arguments = control.recv()
            try:
                if command == "commit":
                    next_seq = connection.execute(
                        "SELECT coalesce(max(seq), -1) + 1 FROM supervisor_probe"
                    ).fetchone()[0]
                    value = str(arguments["value"])
                    payload = bytes(arguments.get("payload", b""))
                    connection.execute(
                        "INSERT INTO supervisor_probe(seq, value, payload) "
                        "VALUES(?, ?, ?)",
                        (next_seq, value, payload),
                    )
                    control.send(("ok", next_seq))
                elif command == "checkpoint":
                    mode = str(arguments.get("mode", "TRUNCATE")).upper()
                    if mode not in {"PASSIVE", "RESTART", "TRUNCATE"}:
                        raise ValueError(f"Unsupported checkpoint mode {mode!r}")
                    result = connection.execute(
                        f"PRAGMA wal_checkpoint({mode})"
                    ).fetchone()
                    control.send(("ok", tuple(result)))
                elif command == "barrier":
                    count = connection.execute(
                        "SELECT count(*) FROM supervisor_probe"
                    ).fetchone()[0]
                    control.send(("ok", count))
                elif command == "stop":
                    control.send(("ok", None))
                    break
                else:
                    raise ValueError(f"Unknown writer command {command!r}")
            except BaseException:
                control.send(("error", traceback.format_exc()))
    except EOFError:
        pass
    except BaseException:
        try:
            control.send(("startup_error", traceback.format_exc()))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        if connection is not None:
            connection.close(True)
        control.close()


@dataclass
class _ApswWalWriter:
    database_path: Path
    process: multiprocessing.Process
    control: Connection

    @classmethod
    def start(cls, database_path: Path) -> _ApswWalWriter:
        context = multiprocessing.get_context("spawn")
        parent_control, child_control = context.Pipe(duplex=True)
        process = context.Process(
            target=_apsw_wal_writer_process,
            args=(str(database_path), child_control),
            name="qplot-test-supervisor-wal-writer",
        )
        process.start()
        child_control.close()
        if not parent_control.poll(30):
            process.terminate()
            process.join(10)
            if process.is_alive() and hasattr(process, "kill"):
                process.kill()
                process.join(10)
            parent_control.close()
            process.close()
            raise AssertionError("The APSW WAL writer did not start in time")
        kind, payload = parent_control.recv()
        if kind != "ready":
            process.join(10)
            if process.is_alive():
                process.terminate()
                process.join(10)
            if process.is_alive() and hasattr(process, "kill"):
                process.kill()
                process.join(10)
            parent_control.close()
            process.close()
            raise AssertionError(f"The APSW WAL writer failed:\n{payload}")
        return cls(database_path, process, parent_control)

    def request(
        self,
        command: str,
        *,
        timeout: float = 30.0,
        **arguments: Any,
    ) -> Any:
        if not self.process.is_alive():
            raise AssertionError(
                f"The APSW WAL writer exited with {self.process.exitcode}"
            )
        self.control.send((command, arguments))
        if not self.control.poll(timeout):
            raise AssertionError(f"Writer command {command!r} timed out")
        kind, payload = self.control.recv()
        if kind != "ok":
            raise AssertionError(
                f"Writer command {command!r} failed in the child:\n{payload}"
            )
        return payload

    def close(self) -> None:
        if self.process.is_alive():
            try:
                self.request("stop", timeout=10)
            except (AssertionError, BrokenPipeError, EOFError, OSError):
                pass
        self.process.join(10)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(10)
        if self.process.is_alive() and hasattr(self.process, "kill"):
            self.process.kill()
            self.process.join(10)
        exitcode = self.process.exitcode
        self.control.close()
        assert not self.process.is_alive()
        self.process.close()
        assert exitcode == 0


def _qcodes_wal_writer_process(database_path: str, control: Connection) -> None:
    """Own one live public-API QCoDeS measurement in a spawned process."""

    experiment: Any = None
    run_context: Any = None
    dataset: Any = None
    try:
        initialise_or_create_database_at(database_path, journal_mode="WAL")
        experiment = load_or_create_experiment(
            "supervisor_live_experiment",
            sample_name="supervisor_live_sample",
        )
        setpoint = ManualParameter("supervisor_live_setpoint")
        signal = ManualParameter("supervisor_live_signal")
        measurement = Measurement(exp=experiment, name="supervisor_live_run")
        measurement.write_period = 0.001
        measurement.register_parameter(setpoint)
        measurement.register_parameter(signal, setpoints=(setpoint,))
        run_context = measurement.run(write_in_background=False)
        datasaver = run_context.__enter__()
        dataset = datasaver.dataset
        datasaver.add_result((setpoint, 0.0), (signal, 0.0))
        datasaver.flush_data_to_database(block=True)
        control.send(("ready", {"result_table_name": dataset.table_name}))

        next_value = 1
        while True:
            command, arguments = control.recv()
            try:
                if command == "commit":
                    value = float(arguments.get("value", next_value))
                    datasaver.add_result(
                        (setpoint, value),
                        (signal, value * 2.0),
                    )
                    datasaver.flush_data_to_database(block=True)
                    next_value += 1
                    control.send(("ok", value))
                elif command == "checkpoint":
                    mode = str(arguments.get("mode", "TRUNCATE")).upper()
                    if mode not in {"PASSIVE", "RESTART", "TRUNCATE"}:
                        raise ValueError(f"Unsupported checkpoint mode {mode!r}")
                    result = dataset.conn.execute(
                        f"PRAGMA wal_checkpoint({mode})"
                    ).fetchone()
                    control.send(("ok", tuple(result)))
                elif command == "stop":
                    control.send(("ok", None))
                    break
                else:
                    raise ValueError(f"Unknown QCoDeS writer command {command!r}")
            except BaseException:
                control.send(("error", traceback.format_exc()))
    except EOFError:
        pass
    except BaseException:
        try:
            control.send(("startup_error", traceback.format_exc()))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        if run_context is not None:
            run_context.__exit__(None, None, None)
        if dataset is not None:
            dataset.conn.close()
        if experiment is not None:
            experiment.conn.close()
        control.close()


@dataclass
class _QcodesWalWriter:
    database_path: Path
    process: multiprocessing.Process
    control: Connection
    result_table_name: str

    @classmethod
    def start(cls, database_path: Path) -> _QcodesWalWriter:
        context = multiprocessing.get_context("spawn")
        parent_control, child_control = context.Pipe(duplex=True)
        process = context.Process(
            target=_qcodes_wal_writer_process,
            args=(str(database_path), child_control),
            name="qplot-test-supervisor-qcodes-wal-writer",
        )
        process.start()
        child_control.close()
        if not parent_control.poll(60):
            process.terminate()
            process.join(10)
            if process.is_alive() and hasattr(process, "kill"):
                process.kill()
                process.join(10)
            parent_control.close()
            process.close()
            raise AssertionError("The QCoDeS WAL writer did not start in time")
        kind, payload = parent_control.recv()
        if kind != "ready":
            process.join(10)
            if process.is_alive():
                process.terminate()
                process.join(10)
            if process.is_alive() and hasattr(process, "kill"):
                process.kill()
                process.join(10)
            parent_control.close()
            process.close()
            raise AssertionError(f"The QCoDeS WAL writer failed:\n{payload}")
        return cls(
            database_path,
            process,
            parent_control,
            str(payload["result_table_name"]),
        )

    def request(
        self,
        command: str,
        *,
        timeout: float = 30.0,
        **arguments: Any,
    ) -> Any:
        if not self.process.is_alive():
            raise AssertionError(
                f"The QCoDeS WAL writer exited with {self.process.exitcode}"
            )
        self.control.send((command, arguments))
        if not self.control.poll(timeout):
            raise AssertionError(f"QCoDeS writer command {command!r} timed out")
        kind, payload = self.control.recv()
        if kind != "ok":
            raise AssertionError(
                f"QCoDeS writer command {command!r} failed:\n{payload}"
            )
        return payload

    def close(self) -> None:
        if self.process.is_alive():
            try:
                self.request("stop", timeout=10)
            except (AssertionError, BrokenPipeError, EOFError, OSError):
                pass
        self.process.join(10)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(10)
        if self.process.is_alive() and hasattr(self.process, "kill"):
            self.process.kill()
            self.process.join(10)
        exitcode = self.process.exitcode
        self.control.close()
        assert not self.process.is_alive()
        self.process.close()
        assert exitcode == 0


@pytest.fixture
def wal_writer(tmp_path: Path) -> _ApswWalWriter:
    writer = _ApswWalWriter.start(tmp_path / "supervisor-live.db")
    try:
        assert writer.request("barrier") == 1
        assert Path(f"{writer.database_path}-wal").is_file()
        assert Path(f"{writer.database_path}-shm").is_file()
        yield writer
    finally:
        writer.close()


@pytest.fixture
def qcodes_wal_writer(tmp_path: Path) -> _QcodesWalWriter:
    writer = _QcodesWalWriter.start(tmp_path / "supervisor-qcodes-live.db")
    try:
        assert Path(f"{writer.database_path}-wal").is_file()
        assert Path(f"{writer.database_path}-shm").is_file()
        yield writer
    finally:
        writer.close()


def _probe_rows(
    supervisor: TrustedLiveReaderSupervisor,
) -> tuple[tuple[Any, ...], ...]:
    result = supervisor.query(
        "SELECT seq, value, payload FROM supervisor_probe ORDER BY seq",
        timeout=5.0,
    )
    assert result.columns == ("seq", "value", "payload")
    return result.rows


def _protected_artifact_contents(database_path: Path) -> dict[str, bytes | None]:
    return {
        suffix: (
            None
            if not Path(f"{database_path}{suffix}").exists()
            else Path(f"{database_path}{suffix}").read_bytes()
        )
        for suffix in ("", "-wal", "-journal")
    }


def _assert_helper_stopped(
    supervisor: TrustedLiveReaderSupervisor,
    helper_pid: int,
) -> None:
    assert not supervisor.helper_alive
    assert all(
        process.pid != helper_pid for process in multiprocessing.active_children()
    )


def test_successful_jobs_preserve_scalars_bytes_and_one_incarnation(
    wal_writer: _ApswWalWriter,
) -> None:
    expected = database_instance(wal_writer.database_path)
    with TrustedLiveReaderSupervisor.open(
        wal_writer.database_path,
        expected_database_instance=expected,
    ) as supervisor:
        helper_pid = supervisor.helper_pid
        incarnation = supervisor.incarnation
        assert helper_pid is not None
        assert incarnation is not None
        assert helper_pid != os.getpid()
        assert incarnation == 1
        assert supervisor.helper_alive

        scalar_job = supervisor.submit_query(
            "SELECT ?, ?, ?, ?, ?",
            (None, 2**40, 1.25, "embedded\x00text", b"\x00\xffbytes"),
            timeout=5.0,
        )
        scalar_result = supervisor.wait(scalar_job, timeout=10.0)
        assert scalar_result.rows == (
            (None, 2**40, 1.25, "embedded\x00text", b"\x00\xffbytes"),
        )

        batch_job = supervisor.submit_query_batch(
            (
                TrustedQuery("SELECT value FROM supervisor_probe ORDER BY seq"),
                TrustedQuery("SELECT count(*) FROM supervisor_probe"),
            ),
            timeout=5.0,
        )
        batch = supervisor.wait(batch_job, timeout=10.0)
        assert batch[0].rows == (("initial",),)
        assert batch[1].rows == ((1,),)

        version_job = supervisor.submit_data_version(timeout=5.0)
        assert supervisor.wait(version_job, timeout=10.0) > 0
        direct_batch = supervisor.query_batch(
            (TrustedQuery("SELECT 6"), TrustedQuery("SELECT 7")),
            timeout=5.0,
        )
        assert tuple(result.rows for result in direct_batch) == (((6,),), ((7,),))
        assert supervisor.helper_pid == helper_pid
        assert supervisor.incarnation == incarnation

    _assert_helper_stopped(supervisor, helper_pid)


def test_raw_top_level_memoryview_binding_round_trips_as_one_blob(
    wal_writer: _ApswWalWriter,
) -> None:
    with TrustedLiveReaderSupervisor.open(wal_writer.database_path) as supervisor:
        result = supervisor.query(
            "SELECT ?",
            memoryview(b"\x00\x80\xff"),
            timeout=5.0,
        )

        assert result.rows == ((b"\x00\x80\xff",),)


def test_query_batch_snapshots_mutable_blob_values_before_frame_encoding(
    wal_writer: _ApswWalWriter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    array_binding = bytearray(b"AAAA")
    view_source = bytearray(b"BBBB")
    view_binding = memoryview(view_source)
    queries = (
        TrustedQuery("SELECT ?", (array_binding,)),
        TrustedQuery("SELECT ?", (view_binding,)),
    )
    supervisor = TrustedLiveReaderSupervisor.open(
        wal_writer.database_path,
        shutdown_timeout_seconds=0.2,
        terminate_timeout_seconds=0.5,
        kill_timeout_seconds=0.5,
        _test_fault="hang_close",
    )
    helper_pid = supervisor.helper_pid
    assert helper_pid is not None
    encode_entered = threading.Event()
    allow_encode = threading.Event()
    submitted_jobs: list[Any] = []
    submit_errors: list[BaseException] = []
    real_encode = supervisor_module.encode_job_request
    barrier_used = False

    def delayed_encode(*args: Any, **kwargs: Any) -> bytes:
        nonlocal barrier_used
        if not barrier_used and len(args) >= 3 and args[2] == "query_batch":
            barrier_used = True
            encode_entered.set()
            if not allow_encode.wait(10.0):
                raise TimeoutError("The mutable-binding test did not release encoding")
        return real_encode(*args, **kwargs)

    def submit_batch() -> None:
        try:
            submitted_jobs.append(supervisor.submit_query_batch(queries, timeout=5.0))
        except BaseException as error:
            submit_errors.append(error)

    monkeypatch.setattr(supervisor_module, "encode_job_request", delayed_encode)
    submit_thread = threading.Thread(
        target=submit_batch,
        name="qplot-test-mutable-batch-submit",
        daemon=True,
    )
    submit_thread.start()
    try:
        assert encode_entered.wait(10.0)
        array_binding[:] = b"xxxx"
        view_source[:] = b"yyyy"
    finally:
        allow_encode.set()
        submit_thread.join(10.0)

    assert not submit_thread.is_alive()
    assert not submit_errors, submit_errors
    assert len(submitted_jobs) == 1
    supervisor._wait_for_test_notification(b"operation_started", 10.0)
    results = supervisor.wait(submitted_jobs[0], timeout=10.0)
    assert tuple(result.rows for result in results) == (
        ((b"AAAA",),),
        ((b"BBBB",),),
    )

    with pytest.raises(TrustedLiveHelperForcedTerminationError):
        supervisor.close(timeout=0.2)
    _assert_helper_stopped(supervisor, helper_pid)


def test_one_persistent_helper_observes_later_wal_commits(
    wal_writer: _ApswWalWriter,
) -> None:
    with TrustedLiveReaderSupervisor.open(wal_writer.database_path) as supervisor:
        helper_pid = supervisor.helper_pid
        incarnation = supervisor.incarnation
        before_version = supervisor.data_version(timeout=5.0)
        assert _probe_rows(supervisor) == ((0, "initial", b""),)

        seq = wal_writer.request(
            "commit",
            value="later",
            payload=b"\x00\x80\xff",
        )

        assert seq == 1
        assert _probe_rows(supervisor) == (
            (0, "initial", b""),
            (1, "later", b"\x00\x80\xff"),
        )
        assert supervisor.data_version(timeout=5.0) > before_version
        assert supervisor.helper_pid == helper_pid
        assert supervisor.incarnation == incarnation
        assert wal_writer.request("checkpoint", mode="TRUNCATE") == (0, 0, 0)

    assert helper_pid is not None
    _assert_helper_stopped(supervisor, helper_pid)
    assert wal_writer.request("commit", value="after-close") == 2
    assert wal_writer.request("checkpoint", mode="TRUNCATE") == (0, 0, 0)


def test_persistent_helper_observes_later_public_qcodes_measurement_commit(
    qcodes_wal_writer: _QcodesWalWriter,
) -> None:
    quoted_table = '"' + qcodes_wal_writer.result_table_name.replace('"', '""') + '"'
    with TrustedLiveReaderSupervisor.open(
        qcodes_wal_writer.database_path
    ) as supervisor:
        helper_pid = supervisor.helper_pid
        incarnation = supervisor.incarnation
        assert supervisor.query(
            f"SELECT count(*) FROM {quoted_table}",
            timeout=5.0,
        ).rows == ((1,),)

        assert qcodes_wal_writer.request("commit", value=7.0) == 7.0

        assert supervisor.query(
            f"SELECT count(*) FROM {quoted_table}",
            timeout=5.0,
        ).rows == ((2,),)
        assert supervisor.helper_pid == helper_pid
        assert supervisor.incarnation == incarnation

    assert helper_pid is not None
    _assert_helper_stopped(supervisor, helper_pid)
    assert qcodes_wal_writer.request("commit", value=8.0) == 8.0
    assert qcodes_wal_writer.request("checkpoint", mode="TRUNCATE") == (0, 0, 0)


def test_immediate_cooperative_cancellation_keeps_the_helper_reusable(
    wal_writer: _ApswWalWriter,
) -> None:
    with TrustedLiveReaderSupervisor.open(wal_writer.database_path) as supervisor:
        helper_pid = supervisor.helper_pid
        incarnation = supervisor.incarnation
        job = supervisor.submit_query(
            "WITH RECURSIVE values_(n) AS ("
            "SELECT 1 UNION ALL SELECT n + 1 FROM values_ "
            "WHERE n < 100000000) SELECT sum(n) FROM values_",
            timeout=20.0,
        )
        supervisor.cancel(job, grace_timeout=2.0)
        with pytest.raises(TrustedLiveCancelledError):
            supervisor.wait(job, timeout=5.0)

        assert supervisor.helper_pid == helper_pid
        assert supervisor.incarnation == incarnation
        assert supervisor.query("SELECT 1", timeout=5.0).rows == ((1,),)
        assert wal_writer.request("checkpoint", mode="TRUNCATE") == (0, 0, 0)


def test_child_operation_deadline_retires_incarnation_before_fresh_job(
    wal_writer: _ApswWalWriter,
) -> None:
    supervisor = TrustedLiveReaderSupervisor.open(wal_writer.database_path)
    try:
        first_pid = supervisor.helper_pid
        first_incarnation = supervisor.incarnation
        assert first_pid is not None
        job = supervisor.submit_query(
            "WITH RECURSIVE values_(n) AS ("
            "SELECT count(*) FROM supervisor_probe "
            "UNION ALL SELECT n + 1 FROM values_ WHERE n < 100000000"
            ") SELECT sum(n) FROM values_",
            timeout=0.01,
        )
        with pytest.raises(TrustedLiveDeadlineExceededError):
            supervisor.wait(job, timeout=5.0)
        _assert_helper_stopped(supervisor, first_pid)

        assert supervisor.query("SELECT 11", timeout=5.0).rows == ((11,),)
        assert supervisor.helper_pid is not None
        assert supervisor.helper_pid != first_pid
        assert supervisor.incarnation != first_incarnation
        replacement_pid = supervisor.helper_pid
    finally:
        supervisor.close()
    assert replacement_pid is not None
    _assert_helper_stopped(supervisor, replacement_pid)


def test_parent_reply_timeout_cooperatively_retires_before_fresh_job(
    wal_writer: _ApswWalWriter,
) -> None:
    supervisor = TrustedLiveReaderSupervisor.open(
        wal_writer.database_path,
        cancellation_grace_seconds=2.0,
    )
    try:
        first_pid = supervisor.helper_pid
        first_incarnation = supervisor.incarnation
        assert first_pid is not None
        job = supervisor.submit_query(
            "WITH RECURSIVE values_(n) AS ("
            "SELECT count(*) FROM supervisor_probe "
            "UNION ALL SELECT n + 1 FROM values_ WHERE n < 100000000"
            ") SELECT sum(n) FROM values_",
            timeout=20.0,
        )
        with pytest.raises(TrustedLiveHelperReplyTimeoutError):
            supervisor.wait(job, timeout=0.01)
        _assert_helper_stopped(supervisor, first_pid)

        assert supervisor.query("SELECT 14", timeout=5.0).rows == ((14,),)
        assert supervisor.helper_pid is not None
        assert supervisor.helper_pid != first_pid
        assert supervisor.incarnation != first_incarnation
        replacement_pid = supervisor.helper_pid
    finally:
        supervisor.close()
    assert replacement_pid is not None
    _assert_helper_stopped(supervisor, replacement_pid)


def test_noncooperative_job_is_forced_down_and_next_explicit_job_is_fresh(
    wal_writer: _ApswWalWriter,
) -> None:
    supervisor = TrustedLiveReaderSupervisor.open(
        wal_writer.database_path,
        cancellation_grace_seconds=0.1,
        _test_fault="hang_before_operation",
    )
    try:
        first_pid = supervisor.helper_pid
        first_incarnation = supervisor.incarnation
        assert first_pid is not None
        protected_before = _protected_artifact_contents(wal_writer.database_path)
        job = supervisor.submit_query("SELECT 1", timeout=20.0)
        supervisor._wait_for_test_notification(b"operation_started", 10.0)
        supervisor._wait_for_test_notification(b"operation_hang", 10.0)

        started = time.monotonic()
        with pytest.raises(TrustedLiveHelperForcedTerminationError):
            supervisor.wait(job, timeout=0.05)
        assert time.monotonic() - started < 5.0
        _assert_helper_stopped(supervisor, first_pid)
        assert (
            _protected_artifact_contents(wal_writer.database_path) == protected_before
        )

        assert supervisor.query("SELECT 2", timeout=5.0).rows == ((2,),)
        assert supervisor.helper_pid is not None
        assert supervisor.helper_pid != first_pid
        assert supervisor.incarnation != first_incarnation
        assert wal_writer.request("commit", value="after-forced-stop") == 1
        assert wal_writer.request("checkpoint", mode="TRUNCATE") == (0, 0, 0)
        second_pid = supervisor.helper_pid
    finally:
        supervisor.close()
    assert second_pid is not None
    _assert_helper_stopped(supervisor, second_pid)


def test_crashed_helper_is_not_replayed_and_next_job_uses_fresh_incarnation(
    wal_writer: _ApswWalWriter,
) -> None:
    supervisor = TrustedLiveReaderSupervisor.open(
        wal_writer.database_path,
        _test_fault="crash_before_reply",
    )
    try:
        first_pid = supervisor.helper_pid
        first_incarnation = supervisor.incarnation
        assert first_pid is not None
        failed_job = supervisor.submit_query("SELECT 1", timeout=5.0)
        supervisor._wait_for_test_notification(b"operation_started", 10.0)
        with pytest.raises(TrustedLiveHelperExitedError):
            supervisor.wait(failed_job, timeout=10.0)
        _assert_helper_stopped(supervisor, first_pid)

        replacement_job = supervisor.submit_query("SELECT 3", timeout=5.0)
        assert supervisor.wait(replacement_job, timeout=10.0).rows == ((3,),)
        assert supervisor.helper_pid is not None
        assert supervisor.helper_pid != first_pid
        assert supervisor.incarnation != first_incarnation
        second_pid = supervisor.helper_pid
    finally:
        supervisor.close()
    assert second_pid is not None
    _assert_helper_stopped(supervisor, second_pid)


def test_cleanup_quarantine_discards_only_the_faulted_incarnation(
    wal_writer: _ApswWalWriter,
) -> None:
    supervisor = TrustedLiveReaderSupervisor.open(
        wal_writer.database_path,
        _test_fault="cleanup_quarantine",
    )
    try:
        first_pid = supervisor.helper_pid
        first_incarnation = supervisor.incarnation
        assert first_pid is not None
        failed_job = supervisor.submit_query("SELECT 1", timeout=5.0)
        with pytest.raises(TrustedLiveCleanupError):
            supervisor.wait(failed_job, timeout=10.0)
        _assert_helper_stopped(supervisor, first_pid)

        assert supervisor.query("SELECT 4", timeout=5.0).rows == ((4,),)
        assert supervisor.helper_pid is not None
        assert supervisor.helper_pid != first_pid
        assert supervisor.incarnation != first_incarnation
        second_pid = supervisor.helper_pid
    finally:
        supervisor.close()
    assert second_pid is not None
    _assert_helper_stopped(supervisor, second_pid)


@pytest.mark.parametrize(
    ("decoder", "frame"),
    [
        pytest.param(decode_request_frame, b"\xff", id="request-invalid-utf8"),
        pytest.param(decode_request_frame, b"{", id="request-invalid-json"),
        pytest.param(
            decode_request_frame,
            b"x" * (MAX_REQUEST_BYTES + 1),
            id="request-oversized",
        ),
        pytest.param(
            decode_request_frame,
            _raw_protocol_envelope(version=PROTOCOL_VERSION + 1),
            id="request-wrong-version",
        ),
        pytest.param(
            decode_request_frame,
            encode_frame(
                _TEST_PROTOCOL_SESSION,
                1,
                "unknown",
                {},
                maximum_bytes=MAX_REQUEST_BYTES,
            ),
            id="request-unknown-operation",
        ),
        pytest.param(
            decode_request_frame,
            _raw_protocol_envelope(extra=True),
            id="request-extra-field",
        ),
        pytest.param(
            decode_request_frame,
            b"[" * 1_100 + b"0" + b"]" * 1_100,
            id="request-excessive-nesting",
        ),
        pytest.param(
            decode_control_frame,
            b"x" * (MAX_CONTROL_BYTES + 1),
            id="control-oversized",
        ),
        pytest.param(
            decode_control_frame,
            encode_frame(
                _TEST_PROTOCOL_SESSION,
                1,
                "query",
                {},
                maximum_bytes=MAX_CONTROL_BYTES,
            ),
            id="control-wrong-operation",
        ),
    ],
)
def test_request_and_control_decoders_reject_malformed_or_oversized_frames(
    decoder: Any,
    frame: bytes,
) -> None:
    with pytest.raises(TrustedLiveProtocolValidationError):
        decoder(frame)


def test_control_decoder_normalizes_json_integer_digit_limit_errors() -> None:
    previous_limit = sys.get_int_max_str_digits()
    test_limit = 640
    try:
        sys.set_int_max_str_digits(test_limit)
        oversized_integer = b"1" * (test_limit + 1)
        frame = (
            b'{"generation":'
            + oversized_integer
            + b',"operation":"cancel","payload":{},"protocol_version":1,'
            + b'"session":"'
            + _TEST_PROTOCOL_SESSION.encode("ascii")
            + b'"}'
        )
        assert len(frame) <= MAX_CONTROL_BYTES
        with pytest.raises(TrustedLiveProtocolValidationError):
            decode_control_frame(frame)
    finally:
        sys.set_int_max_str_digits(previous_limit)


def test_aggregate_blob_budgets_reject_before_any_base64_amplification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blob = b"x" * MAX_SCALAR_BYTES
    calls: list[bytes] = []

    def forbidden_base64(value: bytes) -> bytes:
        calls.append(value)
        raise AssertionError("Aggregate bounds must run before base64 encoding")

    monkeypatch.setattr(protocol_module.base64, "b64encode", forbidden_base64)
    with pytest.raises(TrustedLiveProtocolValidationError):
        encode_job_request(
            _TEST_PROTOCOL_SESSION,
            1,
            "query",
            (TrustedQuery("SELECT ?, ?", (blob, blob)),),
            1_000,
        )
    assert calls == []

    result = TrustedQueryResult(
        tuple(f"blob_{index}" for index in range(8)),
        (tuple(blob for _ in range(8)),),
    )
    with pytest.raises(TrustedLiveProtocolValidationError):
        encode_query_results((result,))
    assert calls == []


def test_database_identity_round_trip_preserves_uint64_high_bit() -> None:
    high_bit_file_index = (1 << 63) + 123
    maximum_uint64 = (1 << 64) - 1
    instance = DatabaseInstance(
        logical_path="logical.db",
        resolved_path="resolved.db",
        identity=("windows", 0xFFFFFFFF, high_bit_file_index),
        sidecar_identities=frozenset({("windows", 0xFFFFFFFE, maximum_uint64)}),
    )

    decoded = decode_database_instance(encode_database_instance(instance))

    assert decoded is not None
    assert decoded.logical_path == instance.logical_path
    assert decoded.resolved_path == instance.resolved_path
    assert decoded.identity == instance.identity
    assert decoded.sidecar_identities == instance.sidecar_identities


def test_birthtime_identity_allows_a_path_longer_than_generic_identity_text() -> None:
    birthtime_path = "/" + ("nested-path/" * 100)
    assert 1_024 < len(birthtime_path.encode("utf-8")) <= MAX_PATH_BYTES
    instance = DatabaseInstance(
        logical_path="logical.db",
        resolved_path="resolved.db",
        identity=("birthtime", birthtime_path, (1 << 63) + 17),
    )

    decoded = decode_database_instance(encode_database_instance(instance))

    assert decoded is not None
    assert decoded.identity == instance.identity


def test_database_instance_allows_six_distinct_sidecars_but_rejects_seven() -> None:
    six_sidecars = frozenset((index, index + 100) for index in range(6))
    instance = DatabaseInstance(
        logical_path="logical.db",
        resolved_path="resolved.db",
        identity=(99, 199),
        sidecar_identities=six_sidecars,
    )

    encoded = encode_database_instance(instance)
    decoded = decode_database_instance(encoded)

    assert decoded is not None
    assert decoded.sidecar_identities == six_sidecars

    seven_sidecars = frozenset((index, index + 100) for index in range(7))
    with pytest.raises(TrustedLiveProtocolValidationError):
        encode_database_instance(
            DatabaseInstance(
                logical_path="logical.db",
                resolved_path="resolved.db",
                identity=(99, 199),
                sidecar_identities=seven_sidecars,
            )
        )

    assert isinstance(encoded, dict)
    encoded_with_seven = {
        **encoded,
        "sidecar_identities": [list(identity) for identity in seven_sidecars],
    }
    with pytest.raises(TrustedLiveProtocolValidationError):
        decode_database_instance(encoded_with_seven)


def test_closed_supervisor_error_is_a_supervisor_error() -> None:
    error = TrustedLiveSupervisorClosedError("closed")

    assert isinstance(error, TrustedLiveSupervisorError)
    assert issubclass(TrustedLiveSupervisorClosedError, TrustedLiveSupervisorError)


@pytest.mark.parametrize(
    "scalar",
    [
        pytest.param(["real", "1.0"], id="noncanonical-real"),
        pytest.param(["real", "0" * 33], id="oversized-real"),
        pytest.param(
            ["blob", "A" * (4 * ((MAX_SCALAR_BYTES + 2) // 3))],
            id="decoded-blob-over-4mib",
        ),
    ],
)
def test_job_success_decoder_rejects_noncanonical_or_oversized_scalars(
    scalar: list[Any],
) -> None:
    payload = {
        "status": "ok",
        "results": [{"columns": ["value"], "rows": [[scalar]]}],
    }

    with pytest.raises(TrustedLiveProtocolValidationError):
        validate_job_success("query", payload)


@pytest.mark.parametrize(
    "fault",
    [
        "malformed_reply",
        "oversized_reply",
        "wrong_version_reply",
        "stale_generation_reply",
        "stale_session_reply",
    ],
)
def test_invalid_or_stale_reply_fails_closed_and_fresh_job_recovers(
    wal_writer: _ApswWalWriter,
    fault: str,
) -> None:
    supervisor = TrustedLiveReaderSupervisor.open(
        wal_writer.database_path,
        _test_fault=fault,
    )
    try:
        first_pid = supervisor.helper_pid
        first_incarnation = supervisor.incarnation
        assert first_pid is not None
        job = supervisor.submit_query("SELECT 1", timeout=5.0)
        supervisor._wait_for_test_notification(b"operation_started", 10.0)
        with pytest.raises(TrustedLiveProtocolError):
            supervisor.wait(job, timeout=10.0)
        _assert_helper_stopped(supervisor, first_pid)

        assert supervisor.query("SELECT 8", timeout=5.0).rows == ((8,),)
        assert supervisor.helper_pid is not None
        assert supervisor.helper_pid != first_pid
        assert supervisor.incarnation != first_incarnation
        replacement_pid = supervisor.helper_pid
    finally:
        supervisor.close()
    assert replacement_pid is not None
    _assert_helper_stopped(supervisor, replacement_pid)


@pytest.mark.parametrize("mutation", ["truncate", "append"])
def test_query_batch_reply_cardinality_mismatch_retires_the_incarnation(
    wal_writer: _ApswWalWriter,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    supervisor = TrustedLiveReaderSupervisor.open(wal_writer.database_path)
    first_pid = supervisor.helper_pid
    first_incarnation = supervisor.incarnation
    assert first_pid is not None
    real_decode = supervisor_module.decode_reply_frame

    def decode_mutated_batch(frame: bytes) -> ProtocolEnvelope:
        envelope = real_decode(frame)
        if envelope.operation != "query_batch":
            return envelope
        results = list(envelope.payload["results"])
        if mutation == "truncate":
            results = results[:-1]
        else:
            results.append(results[-1])
        return ProtocolEnvelope(
            envelope.session,
            envelope.generation,
            envelope.operation,
            {**envelope.payload, "results": results},
        )

    monkeypatch.setattr(
        supervisor_module,
        "decode_reply_frame",
        decode_mutated_batch,
    )
    try:
        job = supervisor.submit_query_batch(
            (TrustedQuery("SELECT 1"), TrustedQuery("SELECT 2")),
            timeout=5.0,
        )
        with pytest.raises(TrustedLiveProtocolError):
            supervisor.wait(job, timeout=10.0)
        _assert_helper_stopped(supervisor, first_pid)

        assert supervisor.query("SELECT 13", timeout=5.0).rows == ((13,),)
        assert supervisor.helper_pid is not None
        assert supervisor.helper_pid != first_pid
        assert supervisor.incarnation != first_incarnation
        replacement_pid = supervisor.helper_pid
    finally:
        supervisor.close()
    assert replacement_pid is not None
    _assert_helper_stopped(supervisor, replacement_pid)


def test_public_submission_limits_reject_before_ipc_and_leave_helper_usable(
    wal_writer: _ApswWalWriter,
) -> None:
    with TrustedLiveReaderSupervisor.open(wal_writer.database_path) as supervisor:
        helper_pid = supervisor.helper_pid
        incarnation = supervisor.incarnation

        with pytest.raises(TrustedLiveProtocolError, match="bounded IPC"):
            supervisor.submit_query("x" * (MAX_SQL_BYTES + 1))
        with pytest.raises(TrustedLiveProtocolError, match="bounded IPC"):
            supervisor.submit_query_batch(
                tuple(TrustedQuery("SELECT 1") for _ in range(MAX_BATCH_QUERIES + 1))
            )
        with pytest.raises(TrustedLiveProtocolError, match="bounded IPC"):
            supervisor.submit_query(
                "SELECT ?",
                (b"x" * (MAX_SCALAR_BYTES + 1),),
            )
        with pytest.raises(TrustedLiveProtocolError, match="bounded IPC"):
            supervisor.submit_query(
                "SELECT ?",
                (b"x" * MAX_REQUEST_BYTES,),
            )

        assert supervisor.active_job is None
        assert supervisor.helper_pid == helper_pid
        assert supervisor.incarnation == incarnation
        assert supervisor.query("SELECT 9", timeout=5.0).rows == ((9,),)


@pytest.mark.parametrize(
    "bindings",
    [
        pytest.param(_OverLimitBindingsSequence(), id="sequence"),
        pytest.param(_OverLimitBindingsMapping(), id="mapping"),
    ],
)
def test_oversized_custom_bindings_reject_from_length_without_materializing(
    wal_writer: _ApswWalWriter,
    bindings: Any,
) -> None:
    with TrustedLiveReaderSupervisor.open(wal_writer.database_path) as supervisor:
        helper_pid = supervisor.helper_pid
        incarnation = supervisor.incarnation

        with pytest.raises(TrustedLiveProtocolError, match="bounded IPC"):
            supervisor.submit_query("SELECT ?", bindings)

        assert supervisor.active_job is None
        assert supervisor.helper_pid == helper_pid
        assert supervisor.incarnation == incarnation
        assert supervisor.query("SELECT 15", timeout=5.0).rows == ((15,),)


def test_oversized_raw_bytearray_binding_rejects_before_ipc(
    wal_writer: _ApswWalWriter,
) -> None:
    oversized = bytearray(MAX_SCALAR_BYTES + 1)
    with TrustedLiveReaderSupervisor.open(wal_writer.database_path) as supervisor:
        helper_pid = supervisor.helper_pid
        incarnation = supervisor.incarnation

        with pytest.raises(TrustedLiveProtocolError, match="bounded IPC"):
            supervisor.submit_query("SELECT ?", oversized)

        assert supervisor.active_job is None
        assert supervisor.helper_pid == helper_pid
        assert supervisor.incarnation == incarnation
        assert supervisor.query("SELECT 16", timeout=5.0).rows == ((16,),)


def test_query_batch_bindings_share_one_request_budget_before_base64_encoding(
    wal_writer: _ApswWalWriter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blob = b"x" * (MAX_REQUEST_BYTES // 2)
    calls: list[bytes] = []

    def forbidden_base64(value: bytes) -> bytes:
        calls.append(value)
        raise AssertionError("The shared batch budget must precede base64 encoding")

    with TrustedLiveReaderSupervisor.open(wal_writer.database_path) as supervisor:
        helper_pid = supervisor.helper_pid
        incarnation = supervisor.incarnation
        monkeypatch.setattr(protocol_module.base64, "b64encode", forbidden_base64)

        with pytest.raises(TrustedLiveProtocolError, match="bounded IPC"):
            supervisor.submit_query_batch(
                (
                    TrustedQuery("SELECT ?", (blob,)),
                    TrustedQuery("SELECT ?", (blob,)),
                ),
                timeout=5.0,
            )

        assert calls == []
        assert supervisor.active_job is None
        assert supervisor.helper_pid == helper_pid
        assert supervisor.incarnation == incarnation
        assert supervisor.query("SELECT 17", timeout=5.0).rows == ((17,),)


def test_late_cancel_for_last_completed_generation_does_not_interrupt_next_job(
    wal_writer: _ApswWalWriter,
) -> None:
    supervisor = TrustedLiveReaderSupervisor.open(
        wal_writer.database_path,
        shutdown_timeout_seconds=0.2,
        terminate_timeout_seconds=0.5,
        kill_timeout_seconds=0.5,
        _test_fault="hang_close",
    )
    try:
        completed = supervisor.submit_query("SELECT 1", timeout=5.0)
        supervisor._wait_for_test_notification(b"operation_started", 10.0)
        assert supervisor.wait(completed, timeout=10.0).rows == ((1,),)
        helper_pid = supervisor.helper_pid
        incarnation = supervisor.incarnation
        assert helper_pid is not None
        current = supervisor.submit_query(
            "WITH RECURSIVE values_(n) AS ("
            "SELECT count(*) FROM supervisor_probe "
            "UNION ALL SELECT n + 1 FROM values_ WHERE n < 100000000"
            ") SELECT sum(n) FROM values_",
            timeout=20.0,
        )
        supervisor._wait_for_test_notification(b"operation_started", 10.0)
        helper = supervisor._helper
        assert helper is not None

        supervisor._send_test_control_frame(
            encode_cancel(current.session, completed.generation)
        )
        # The control loop polls every 50 ms. If the late frame poisoned or
        # interrupted the newer operation, its terminal reply would be visible.
        assert not helper.reply_ready.wait(0.5)
        assert supervisor.helper_alive
        assert supervisor.active_job is current

        assert supervisor.cancel(current, grace_timeout=2.0)
        with pytest.raises(TrustedLiveCancelledError):
            supervisor.wait(current, timeout=5.0)
        assert supervisor.helper_pid == helper_pid
        assert supervisor.incarnation == incarnation
        assert supervisor.query("SELECT 10", timeout=5.0).rows == ((10,),)
    finally:
        try:
            supervisor.close(timeout=0.2)
        except TrustedLiveHelperForcedTerminationError:
            pass
    _assert_helper_stopped(supervisor, helper_pid)


def test_duplicate_cancel_remains_fatal_after_the_next_generation_activates(
    wal_writer: _ApswWalWriter,
) -> None:
    supervisor = TrustedLiveReaderSupervisor.open(
        wal_writer.database_path,
        _test_fault="hang_close",
    )
    try:
        cancelled = supervisor.submit_query(
            "WITH RECURSIVE values_(n) AS ("
            "SELECT count(*) FROM supervisor_probe "
            "UNION ALL SELECT n + 1 FROM values_ WHERE n < 100000000"
            ") SELECT sum(n) FROM values_",
            timeout=20.0,
        )
        supervisor._wait_for_test_notification(b"operation_started", 10.0)
        assert supervisor.cancel(cancelled, grace_timeout=2.0)
        with pytest.raises(TrustedLiveCancelledError):
            supervisor.wait(cancelled, timeout=5.0)

        first_pid = supervisor.helper_pid
        first_incarnation = supervisor.incarnation
        assert first_pid is not None
        current = supervisor.submit_query(
            "WITH RECURSIVE values_(n) AS ("
            "SELECT count(*) FROM supervisor_probe "
            "UNION ALL SELECT n + 1 FROM values_ WHERE n < 100000000"
            ") SELECT sum(n) FROM values_",
            timeout=20.0,
        )
        supervisor._wait_for_test_notification(b"operation_started", 10.0)
        supervisor._send_test_control_frame(
            encode_cancel(current.session, cancelled.generation)
        )

        with pytest.raises(TrustedLiveProtocolError):
            supervisor.wait(current, timeout=10.0)
        _assert_helper_stopped(supervisor, first_pid)

        assert supervisor.query("SELECT 12", timeout=5.0).rows == ((12,),)
        assert supervisor.helper_pid is not None
        assert supervisor.helper_pid != first_pid
        assert supervisor.incarnation != first_incarnation
        replacement_pid = supervisor.helper_pid
    finally:
        supervisor.close()
    assert replacement_pid is not None
    _assert_helper_stopped(supervisor, replacement_pid)


def test_wrong_session_control_frame_is_fatal_but_next_job_can_recover(
    wal_writer: _ApswWalWriter,
) -> None:
    supervisor = TrustedLiveReaderSupervisor.open(wal_writer.database_path)
    try:
        first_pid = supervisor.helper_pid
        first_incarnation = supervisor.incarnation
        assert first_pid is not None
        current = supervisor.submit_query(
            "WITH RECURSIVE values_(n) AS ("
            "SELECT count(*) FROM supervisor_probe "
            "UNION ALL SELECT n + 1 FROM values_ WHERE n < 100000000"
            ") SELECT sum(n) FROM values_",
            timeout=20.0,
        )
        wrong_session = "0" * 32 if current.session != "0" * 32 else "1" * 32
        supervisor._send_test_control_frame(
            encode_frame(
                wrong_session,
                current.generation,
                "cancel",
                {},
                maximum_bytes=MAX_CONTROL_BYTES,
            )
        )

        with pytest.raises(TrustedLiveProtocolError):
            supervisor.wait(current, timeout=10.0)
        _assert_helper_stopped(supervisor, first_pid)

        assert supervisor.query("SELECT 10", timeout=5.0).rows == ((10,),)
        assert supervisor.helper_pid is not None
        assert supervisor.helper_pid != first_pid
        assert supervisor.incarnation != first_incarnation
        replacement_pid = supervisor.helper_pid
    finally:
        supervisor.close()
    assert replacement_pid is not None
    _assert_helper_stopped(supervisor, replacement_pid)


def test_startup_and_normal_close_are_bounded(
    wal_writer: _ApswWalWriter,
) -> None:
    started = time.monotonic()
    supervisor = TrustedLiveReaderSupervisor.open(
        wal_writer.database_path,
        startup_timeout_seconds=15.0,
    )
    try:
        assert time.monotonic() - started < 15.0
        helper_pid = supervisor.helper_pid
        assert helper_pid is not None

        started = time.monotonic()
        supervisor.close()
        assert time.monotonic() - started < 10.0
    finally:
        supervisor.close()
    _assert_helper_stopped(supervisor, helper_pid)


def test_spawned_parent_can_return_normally_without_explicit_supervisor_close(
    wal_writer: _ApswWalWriter,
) -> None:
    context = multiprocessing.get_context("spawn")
    parent_control, child_control = context.Pipe(duplex=False)
    process = context.Process(
        target=_return_with_live_supervisor_process,
        args=(str(wal_writer.database_path), child_control),
        name="qplot-test-unclosed-supervisor-parent",
    )
    process.start()
    child_control.close()
    message: tuple[str, Any] | None = None
    exited_within_budget = False
    exitcode: int | None = None
    try:
        if parent_control.poll(30.0):
            message = parent_control.recv()
        process.join(10.0)
        exited_within_budget = not process.is_alive()
    finally:
        if process.is_alive():
            process.terminate()
            process.join(10.0)
        if process.is_alive() and hasattr(process, "kill"):
            process.kill()
            process.join(10.0)
        exitcode = process.exitcode
        parent_control.close()
        process.close()

    assert message is not None, "The spawned parent did not report helper startup"
    assert message[0] == "ready", message[1]
    assert isinstance(message[1], int)
    assert exited_within_budget, (
        "multiprocessing atexit blocked on an unclosed supervisor helper"
    )
    assert exitcode == 0
    assert wal_writer.request("checkpoint", mode="TRUNCATE") == (0, 0, 0)


def test_abandoned_supervisor_is_collectable_and_reaps_its_helper(
    wal_writer: _ApswWalWriter,
) -> None:
    supervisor = TrustedLiveReaderSupervisor.open(wal_writer.database_path)
    helper_pid = supervisor.helper_pid
    assert helper_pid is not None
    supervisor_reference = weakref.ref(supervisor)

    del supervisor
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        gc.collect()
        reference_cleared = supervisor_reference() is None
        helper_active = any(
            process.pid == helper_pid for process in multiprocessing.active_children()
        )
        if reference_cleared and not helper_active:
            break
        time.sleep(0.01)

    assert supervisor_reference() is None
    assert all(
        process.pid != helper_pid for process in multiprocessing.active_children()
    )
    assert wal_writer.request("checkpoint", mode="TRUNCATE") == (0, 0, 0)


def test_abandoning_active_job_records_forced_termination_and_reaps_helper(
    wal_writer: _ApswWalWriter,
) -> None:
    supervisor = TrustedLiveReaderSupervisor.open(
        wal_writer.database_path,
        _test_fault="hang_before_operation",
    )
    helper_pid = supervisor.helper_pid
    assert helper_pid is not None
    job = supervisor.submit_query("SELECT 1", timeout=20.0)
    supervisor._wait_for_test_notification(b"operation_started", 10.0)
    supervisor._wait_for_test_notification(b"operation_hang", 10.0)
    supervisor_reference = weakref.ref(supervisor)

    del supervisor
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        gc.collect()
        helper_active = any(
            process.pid == helper_pid for process in multiprocessing.active_children()
        )
        if supervisor_reference() is None and job.done and not helper_active:
            break
        time.sleep(0.01)

    assert supervisor_reference() is None
    assert job.done
    assert isinstance(job._error, TrustedLiveHelperForcedTerminationError)
    assert all(
        process.pid != helper_pid for process in multiprocessing.active_children()
    )
    assert wal_writer.request("checkpoint", mode="TRUNCATE") == (0, 0, 0)


def test_close_consumes_an_already_buffered_job_reply_without_a_cancel_race(
    wal_writer: _ApswWalWriter,
) -> None:
    for value in range(8):
        supervisor = TrustedLiveReaderSupervisor.open(wal_writer.database_path)
        helper_pid = supervisor.helper_pid
        helper = supervisor._helper
        assert helper_pid is not None
        assert helper is not None
        job = supervisor.submit_query("SELECT ?", (value,), timeout=5.0)
        try:
            # Wait for bytes without consuming them, so close deterministically
            # encounters a completed operation still marked active in the parent.
            assert helper.reply_ready.wait(10.0)
            supervisor.close(timeout=2.0)
        finally:
            supervisor.close(timeout=2.0)

        assert supervisor.closed
        assert job.done
        assert supervisor.wait(job, timeout=0.0).rows == ((value,),)
        _assert_helper_stopped(supervisor, helper_pid)


def test_submit_cannot_install_a_job_after_concurrent_close_begins(
    wal_writer: _ApswWalWriter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = TrustedLiveReaderSupervisor.open(wal_writer.database_path)
    helper_pid = supervisor.helper_pid
    helper = supervisor._helper
    assert helper_pid is not None
    assert helper is not None
    completed_in_child = supervisor.submit_query("SELECT 1", timeout=5.0)
    assert helper.reply_ready.wait(10.0)

    cancel_entered = threading.Event()
    allow_cancel = threading.Event()
    close_errors: list[BaseException] = []
    real_cancel = supervisor.cancel

    def delayed_cancel(
        job: Any = None,
        *,
        grace_timeout: float | None = None,
    ) -> bool:
        cancel_entered.set()
        if not allow_cancel.wait(10.0):
            raise TimeoutError("The concurrent-submit test did not release cancel")
        return real_cancel(job, grace_timeout=grace_timeout)

    def close_supervisor() -> None:
        try:
            supervisor.close(timeout=2.0)
        except BaseException as error:
            close_errors.append(error)

    monkeypatch.setattr(supervisor, "cancel", delayed_cancel)
    close_thread = threading.Thread(
        target=close_supervisor,
        name="qplot-test-concurrent-supervisor-close",
        daemon=True,
    )
    close_thread.start()
    try:
        assert cancel_entered.wait(10.0)
        with pytest.raises(TrustedLiveSupervisorClosedError):
            supervisor.submit_query("SELECT 2", timeout=5.0)
        assert supervisor.active_job is completed_in_child
    finally:
        allow_cancel.set()
        close_thread.join(10.0)

    assert not close_thread.is_alive()
    assert not close_errors, close_errors
    assert supervisor.closed
    assert completed_in_child.done
    assert supervisor.wait(completed_in_child, timeout=0.0).rows == ((1,),)
    _assert_helper_stopped(supervisor, helper_pid)


@pytest.mark.parametrize("mutation", ["operation", "status"])
def test_shutdown_reply_operation_and_status_are_strictly_validated(
    wal_writer: _ApswWalWriter,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    supervisor = TrustedLiveReaderSupervisor.open(wal_writer.database_path)
    helper_pid = supervisor.helper_pid
    assert helper_pid is not None
    real_decode = supervisor_module.decode_reply_frame

    def decode_mutated_shutdown(frame: bytes) -> ProtocolEnvelope:
        envelope = real_decode(frame)
        if envelope.operation != "shutdown":
            return envelope
        if mutation == "operation":
            return ProtocolEnvelope(
                envelope.session,
                envelope.generation,
                "query",
                envelope.payload,
            )
        return ProtocolEnvelope(
            envelope.session,
            envelope.generation,
            envelope.operation,
            {**envelope.payload, "status": "unexpected"},
        )

    monkeypatch.setattr(
        supervisor_module,
        "decode_reply_frame",
        decode_mutated_shutdown,
    )
    with pytest.raises(TrustedLiveProtocolError, match="invalid shutdown IPC"):
        supervisor.close(timeout=2.0)

    assert supervisor.closed
    _assert_helper_stopped(supervisor, helper_pid)


def test_noncooperative_shutdown_is_forced_and_joined_within_bounds(
    wal_writer: _ApswWalWriter,
) -> None:
    supervisor = TrustedLiveReaderSupervisor.open(
        wal_writer.database_path,
        shutdown_timeout_seconds=0.2,
        terminate_timeout_seconds=0.5,
        kill_timeout_seconds=0.5,
        _test_fault="hang_close",
    )
    helper_pid = supervisor.helper_pid
    assert helper_pid is not None

    started = time.monotonic()
    with pytest.raises(TrustedLiveHelperForcedTerminationError):
        supervisor.close(timeout=0.2)
    assert time.monotonic() - started < 5.0
    assert supervisor.closed
    _assert_helper_stopped(supervisor, helper_pid)


def test_close_reports_forced_termination_of_an_active_noncooperative_job(
    wal_writer: _ApswWalWriter,
) -> None:
    supervisor = TrustedLiveReaderSupervisor.open(
        wal_writer.database_path,
        cancellation_grace_seconds=0.1,
        shutdown_timeout_seconds=0.2,
        terminate_timeout_seconds=0.5,
        kill_timeout_seconds=0.5,
        _test_fault="hang_before_operation",
    )
    helper_pid = supervisor.helper_pid
    assert helper_pid is not None
    job = supervisor.submit_query("SELECT 1", timeout=20.0)
    supervisor._wait_for_test_notification(b"operation_started", 10.0)
    supervisor._wait_for_test_notification(b"operation_hang", 10.0)

    started = time.monotonic()
    with pytest.raises(TrustedLiveHelperForcedTerminationError):
        supervisor.close(timeout=0.2)
    assert time.monotonic() - started < 5.0
    assert supervisor.closed
    assert job.done
    _assert_helper_stopped(supervisor, helper_pid)
    assert wal_writer.request("checkpoint", mode="TRUNCATE") == (0, 0, 0)


def test_public_qcodes_writer_progresses_after_forced_helper_termination(
    qcodes_wal_writer: _QcodesWalWriter,
) -> None:
    supervisor = TrustedLiveReaderSupervisor.open(
        qcodes_wal_writer.database_path,
        cancellation_grace_seconds=0.1,
        terminate_timeout_seconds=0.5,
        kill_timeout_seconds=0.5,
        _test_fault="hang_before_operation",
    )
    helper_pid = supervisor.helper_pid
    assert helper_pid is not None
    job = supervisor.submit_query("SELECT count(*) FROM runs", timeout=20.0)
    supervisor._wait_for_test_notification(b"operation_started", 10.0)
    supervisor._wait_for_test_notification(b"operation_hang", 10.0)

    with pytest.raises(TrustedLiveHelperForcedTerminationError):
        supervisor.wait(job, timeout=0.05)
    _assert_helper_stopped(supervisor, helper_pid)
    assert qcodes_wal_writer.request("commit", value=9.0) == 9.0
    assert qcodes_wal_writer.request("checkpoint", mode="TRUNCATE") == (0, 0, 0)

    supervisor.close()
    assert supervisor.closed


def test_context_manager_cleanup_does_not_mask_a_body_exception(
    wal_writer: _ApswWalWriter,
) -> None:
    supervisor = TrustedLiveReaderSupervisor.open(
        wal_writer.database_path,
        cancellation_grace_seconds=0.1,
        shutdown_timeout_seconds=0.2,
        terminate_timeout_seconds=0.5,
        kill_timeout_seconds=0.5,
        _test_fault="hang_before_operation",
    )
    helper_pid = supervisor.helper_pid
    assert helper_pid is not None

    with pytest.raises(_ContextBodyError, match="body sentinel"):
        with supervisor:
            job = supervisor.submit_query("SELECT 1", timeout=20.0)
            supervisor._wait_for_test_notification(b"operation_started", 10.0)
            supervisor._wait_for_test_notification(b"operation_hang", 10.0)
            raise _ContextBodyError("body sentinel")

    assert supervisor.closed
    assert job.done
    _assert_helper_stopped(supervisor, helper_pid)
    assert wal_writer.request("checkpoint", mode="TRUNCATE") == (0, 0, 0)


def test_startup_hang_is_terminated_within_the_parent_budget(
    wal_writer: _ApswWalWriter,
) -> None:
    started = time.monotonic()
    with pytest.raises(TrustedLiveHelperStartupError):
        TrustedLiveReaderSupervisor.open(
            wal_writer.database_path,
            startup_timeout_seconds=0.25,
            _test_fault="hang_startup",
        )
    assert time.monotonic() - started < 5.0


def test_parent_endpoint_disappearance_releases_idle_helper(
    wal_writer: _ApswWalWriter,
) -> None:
    supervisor = TrustedLiveReaderSupervisor.open(wal_writer.database_path)
    helper_pid = supervisor.helper_pid
    assert helper_pid is not None
    helper = supervisor._helper
    assert helper is not None

    helper.command_send.close()
    helper.control_send.close()
    deadline = time.monotonic() + 5.0
    while supervisor.helper_alive and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not supervisor.helper_alive

    supervisor.close()
    _assert_helper_stopped(supervisor, helper_pid)
    assert wal_writer.request("commit", value="after-parent-endpoint-loss") == 1
    assert wal_writer.request("checkpoint", mode="TRUNCATE") == (0, 0, 0)


def test_parent_endpoint_disappearance_interrupts_an_active_job(
    wal_writer: _ApswWalWriter,
) -> None:
    # This reply fault is never reached; it creates only the private readiness
    # pipe needed to prove that the child has published the active generation.
    supervisor = TrustedLiveReaderSupervisor.open(
        wal_writer.database_path,
        _test_fault="wrong_version_reply",
    )
    helper_pid = supervisor.helper_pid
    assert helper_pid is not None
    job = supervisor.submit_query(
        "WITH RECURSIVE values_(n) AS ("
        "SELECT count(*) FROM supervisor_probe "
        "UNION ALL SELECT n + 1 FROM values_ WHERE n < 100000000"
        ") SELECT sum(n) FROM values_",
        timeout=20.0,
    )
    supervisor._wait_for_test_notification(b"operation_started", 10.0)
    helper = supervisor._helper
    assert helper is not None

    helper.command_send.close()
    helper.control_send.close()
    deadline = time.monotonic() + 5.0
    while supervisor.helper_alive and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not supervisor.helper_alive

    with pytest.raises(TrustedLiveHelperForcedTerminationError):
        supervisor.close()
    assert job.done
    _assert_helper_stopped(supervisor, helper_pid)
    assert wal_writer.request("checkpoint", mode="TRUNCATE") == (0, 0, 0)


def test_parent_endpoint_disappearance_watchdog_exits_noncooperative_helper(
    wal_writer: _ApswWalWriter,
) -> None:
    supervisor = TrustedLiveReaderSupervisor.open(
        wal_writer.database_path,
        shutdown_timeout_seconds=0.25,
        _test_fault="hang_before_operation",
    )
    helper_pid = supervisor.helper_pid
    assert helper_pid is not None
    job = supervisor.submit_query("SELECT 1", timeout=20.0)
    supervisor._wait_for_test_notification(b"operation_started", 10.0)
    supervisor._wait_for_test_notification(b"operation_hang", 10.0)
    helper = supervisor._helper
    assert helper is not None

    started = time.monotonic()
    helper.command_send.close()
    helper.control_send.close()
    deadline = started + 5.0
    while supervisor.helper_alive and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not supervisor.helper_alive
    assert time.monotonic() - started < 5.0

    with pytest.raises(TrustedLiveHelperForcedTerminationError):
        supervisor.close()
    assert job.done
    _assert_helper_stopped(supervisor, helper_pid)
    assert wal_writer.request("checkpoint", mode="TRUNCATE") == (0, 0, 0)


@pytest.mark.skipif(
    os.name != "posix",
    reason="Atomic replacement of an open main file is a POSIX-specific boundary",
)
def test_main_file_replacement_is_rejected_at_each_operation_boundary(
    tmp_path: Path,
) -> None:
    selected = tmp_path / "operation-boundary.db"
    replacement = tmp_path / "replacement.db"
    for database_path, value in ((selected, "old"), (replacement, "new")):
        connection = apsw.Connection(str(database_path))
        connection.execute("CREATE TABLE identity_probe(value TEXT NOT NULL)")
        connection.execute("INSERT INTO identity_probe VALUES(?)", (value,))
        connection.close(True)

    accepted = database_instance(selected)
    supervisor = TrustedLiveReaderSupervisor.open(
        selected,
        expected_database_instance=accepted,
    )
    try:
        assert supervisor.query(
            "SELECT value FROM identity_probe",
            timeout=5.0,
        ).rows == (("old",),)
        first_pid = supervisor.helper_pid
        first_incarnation = supervisor.incarnation
        assert first_pid is not None

        os.replace(replacement, selected)
        failed_job = supervisor.submit_query(
            "SELECT value FROM identity_probe",
            timeout=5.0,
        )
        with pytest.raises(TrustedLiveSourceChangedError):
            supervisor.wait(failed_job, timeout=10.0)
        assert failed_job.done
        _assert_helper_stopped(supervisor, first_pid)

        with pytest.raises(TrustedLiveSourceChangedError):
            supervisor.submit_query(
                "SELECT value FROM identity_probe",
                timeout=5.0,
            )
        assert supervisor.incarnation == first_incarnation + 1
        assert supervisor.database_instance == accepted
        assert not supervisor.helper_alive
    finally:
        supervisor.close()

    replacement_reader = apsw.Connection(str(selected), flags=apsw.SQLITE_OPEN_READONLY)
    try:
        assert replacement_reader.execute(
            "SELECT value FROM identity_probe"
        ).fetchone() == ("new",)
    finally:
        replacement_reader.close(True)


@pytest.mark.parametrize("malformation", ["oversized-path", "negative-identity"])
def test_invalid_expected_instance_is_wrapped_before_any_helper_starts(
    tmp_path: Path,
    malformation: str,
) -> None:
    selected = tmp_path / "startup-validation.db"
    connection = apsw.Connection(str(selected))
    connection.execute("CREATE TABLE startup_probe(value INTEGER)")
    connection.close(True)
    actual = database_instance(selected)
    if malformation == "oversized-path":
        expected = DatabaseInstance(
            logical_path="/" + ("x" * (MAX_PATH_BYTES + 1)),
            resolved_path=actual.resolved_path,
            identity=actual.identity,
        )
    else:
        expected = DatabaseInstance(
            logical_path=actual.logical_path,
            resolved_path=actual.resolved_path,
            identity=(-1, 1),
        )
    children_before = {process.pid for process in multiprocessing.active_children()}

    with pytest.raises(TrustedLiveProtocolError) as captured:
        TrustedLiveReaderSupervisor.open(
            selected,
            expected_database_instance=expected,
        )

    assert isinstance(
        captured.value.__cause__,
        TrustedLiveProtocolValidationError,
    )
    children_after = {process.pid for process in multiprocessing.active_children()}
    assert children_after - children_before == set()


def test_expected_database_instance_mismatch_is_reported(tmp_path: Path) -> None:
    selected = tmp_path / "selected.db"
    replacement = tmp_path / "replacement.db"
    first = apsw.Connection(str(selected))
    first.execute("CREATE TABLE identity_probe(value INTEGER)")
    first.execute("INSERT INTO identity_probe VALUES(1)")
    first.close(True)
    approved = database_instance(selected)

    second = apsw.Connection(str(replacement))
    second.execute("CREATE TABLE identity_probe(value INTEGER)")
    second.execute("INSERT INTO identity_probe VALUES(2)")
    second.close(True)
    os.replace(replacement, selected)

    supervisor: TrustedLiveReaderSupervisor | None = None
    try:
        with pytest.raises(TrustedLiveSourceChangedError):
            supervisor = TrustedLiveReaderSupervisor.open(
                selected,
                expected_database_instance=approved,
                startup_timeout_seconds=10.0,
            )
    finally:
        if supervisor is not None:
            supervisor.close()
