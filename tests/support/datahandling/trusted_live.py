"""Shared fixtures and assertions for trusted live."""

from __future__ import annotations

import multiprocessing
import os
import stat
import time
import traceback
from collections.abc import Mapping
from dataclasses import dataclass
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

import pytest

from tests._artifact_audit import (
    _artifact_state_in_audit_process,
)

__all__ = [
    '_QcodesWalWriter',
    '_artifact_state',
    '_assert_protected_artifacts_unchanged',
    '_assert_safe_audit',
    '_stable_artifact_state',
    'live_writer',
]


_AUDIT_KEYS = {
    "source_open_readonly",
    "source_open_readwrite",
    "source_open_create",
    "source_open_delete_on_close",
    "source_open_flags_stripped",
    "source_read",
    "source_read_bytes",
    "read_validation_ns",
    "read_io_ns",
    "read_path_reused",
    "source_write",
    "source_truncate",
    "source_sync",
    "source_delete",
    "source_fetch",
    "source_writable_map",
    "shm_map_readonly",
    "shm_map_writable",
    "shm_map_extend",
    "shm_map_rejected",
    "shm_lock",
    "shm_write_lock_attempts",
    "shm_write_lock_acquired",
    "shm_write_lock_ns",
    "shm_write_lock_max_ns",
    "shm_unmap_delete_requested",
    "temp_redirect",
    "temp_write",
    "temp_write_bytes",
    "temp_delete",
    "stale_callback_rejected",
    "identity_verified",
    "identity_rejected",
    "proof_open",
    "proof_close",
    "proof_close_error",
    "proof_active",
    "proof_peak",
    "shm_unmap",
    "shm_unmap_error",
    "shm_unmap_delete_forwarded",
    "partial_open_cleanup",
    "base_close_error",
}


_PROHIBITED_AUDIT_KEYS = {
    "source_open_readwrite",
    "source_open_create",
    "source_open_delete_on_close",
    "source_write",
    "source_truncate",
    "source_sync",
    "source_delete",
    "source_fetch",
    "source_writable_map",
    "shm_map_rejected",
    "identity_rejected",
    "proof_close_error",
    "shm_unmap_error",
    "shm_unmap_delete_forwarded",
    "stale_callback_rejected",
    "partial_open_cleanup",
    "base_close_error",
}


def _close_qcodes_run_handles(dataset: Any, experiment: Any) -> None:
    if dataset is not None:
        dataset.conn.close()
    if experiment is not None:
        experiment.conn.close()


def _start_qcodes_run(
    database_path: str,
) -> tuple[Any, Any, Any, Any, Any, Any]:
    """Start an acquisition through public QCoDeS measurement APIs."""
    # Reader-only spawn probes must not import QCoDeS and its dependencies.
    from qcodes.dataset import (
        Measurement,
        initialise_or_create_database_at,
        load_or_create_experiment,
    )
    from qcodes.parameters import ManualParameter

    initialise_or_create_database_at(database_path, journal_mode="WAL")
    experiment = load_or_create_experiment(
        "trusted_live_experiment",
        sample_name="trusted_live_sample",
    )
    setpoint = ManualParameter("trusted_live_setpoint")
    signal = ManualParameter("trusted_live_signal")
    measurement = Measurement(exp=experiment, name="trusted_live_run")
    measurement.write_period = 0.001
    measurement.register_parameter(setpoint)
    measurement.register_parameter(signal, setpoints=(setpoint,))
    run_context = measurement.run(write_in_background=False)
    datasaver = run_context.__enter__()
    dataset = datasaver.dataset
    datasaver.add_result((setpoint, 0.0), (signal, 0.0))
    datasaver.flush_data_to_database(block=True)
    return experiment, setpoint, signal, run_context, datasaver, dataset


def _qcodes_wal_writer_process(
    database_path: str,
    control: Connection,
    sparse_size: int | None,
) -> None:
    """Own a real QCoDeS WAL connection in a spawn-safe child process."""
    writer = None
    uncommitted = False
    experiment = None
    setpoint = None
    signal = None
    run_context = None
    datasaver = None
    dataset = None
    try:
        from qcodes.dataset.sqlite.connection import atomic
        from qcodes.dataset.sqlite.database import connect

        (
            experiment,
            setpoint,
            signal,
            run_context,
            datasaver,
            dataset,
        ) = _start_qcodes_run(database_path)
        result_table_name = dataset.table_name
        writer = connect(database_path)
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        with atomic(writer):
            writer.execute(
                "CREATE TABLE IF NOT EXISTS qplot_trusted_probe ("
                "seq INTEGER PRIMARY KEY, "
                "value TEXT NOT NULL, "
                "payload BLOB NOT NULL DEFAULT X''"
                ")"
            )
            writer.execute(
                "INSERT OR IGNORE INTO qplot_trusted_probe(seq, value) "
                "VALUES(0, 'initial')"
            )

        if sparse_size is not None:
            run_context.__exit__(None, None, None)
            run_context = None
            _close_qcodes_run_handles(dataset, experiment)
            dataset = None
            experiment = None
            datasaver = None
            setpoint = None
            signal = None
            checkpoint = writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is None or checkpoint[0] != 0:
                raise RuntimeError(
                    f"Could not checkpoint sparse source: {checkpoint!r}"
                )
            writer.close()
            writer = None
            current_size = os.stat(database_path).st_size
            if current_size < sparse_size:
                os.truncate(database_path, sparse_size)
            writer = connect(database_path)
            writer.execute("PRAGMA wal_autocheckpoint = 0")
            with atomic(writer):
                writer.execute(
                    "INSERT INTO qplot_trusted_probe(seq, value) "
                    "VALUES(1, 'after-sparse-extension')"
                )

        user_version = writer.execute("PRAGMA user_version").fetchone()
        run_count = writer.execute("SELECT count(*) FROM runs").fetchone()
        control.send(
            (
                "ready",
                {
                    "user_version": user_version[0],
                    "run_count": run_count[0],
                    "result_table_name": result_table_name,
                },
            )
        )

        while True:
            command, arguments = control.recv()
            try:
                if command == "commit":
                    value = str(arguments["value"])
                    payload_size = int(arguments.get("payload_size", 0))
                    next_seq = writer.execute(
                        "SELECT coalesce(max(seq), -1) + 1 FROM qplot_trusted_probe"
                    ).fetchone()[0]
                    if datasaver is not None:
                        datasaver.add_result(
                            (setpoint, float(next_seq)),
                            (signal, float(next_seq * 2)),
                        )
                        datasaver.flush_data_to_database(block=True)
                    with atomic(writer):
                        writer.execute(
                            "INSERT INTO qplot_trusted_probe(seq, value, payload) "
                            "VALUES(?, ?, ?)",
                            (next_seq, value, bytes(payload_size)),
                        )
                    control.send(("ok", next_seq))
                elif command == "commit_many":
                    count = int(arguments["count"])
                    if not 1 <= count <= 10_000:
                        raise ValueError("commit_many count is outside test bounds")
                    next_seq = writer.execute(
                        "SELECT coalesce(max(seq), -1) + 1 FROM qplot_trusted_probe"
                    ).fetchone()[0]
                    if datasaver is not None:
                        for offset in range(count):
                            seq = next_seq + offset
                            datasaver.add_result(
                                (setpoint, float(seq)),
                                (signal, float(seq * 2)),
                            )
                        datasaver.flush_data_to_database(block=True)
                    with atomic(writer):
                        writer.executemany(
                            "INSERT INTO qplot_trusted_probe(seq, value) VALUES(?, ?)",
                            (
                                (next_seq + offset, f"bulk-{next_seq + offset}")
                                for offset in range(count)
                            ),
                        )
                    control.send(("ok", next_seq + count - 1))
                elif command == "complete_run":
                    if run_context is None:
                        raise RuntimeError("The QCoDeS run is already complete")
                    run_context.__exit__(None, None, None)
                    run_context = None
                    datasaver = None
                    setpoint = None
                    signal = None
                    _close_qcodes_run_handles(dataset, experiment)
                    dataset = None
                    experiment = None
                    control.send(("ok", True))
                elif command == "begin_uncommitted":
                    if uncommitted:
                        raise RuntimeError("The writer already has a transaction")
                    value = str(arguments["value"])
                    next_seq = writer.execute(
                        "SELECT coalesce(max(seq), -1) + 1 FROM qplot_trusted_probe"
                    ).fetchone()[0]
                    writer.execute("BEGIN IMMEDIATE")
                    writer.execute(
                        "INSERT INTO qplot_trusted_probe(seq, value) VALUES(?, ?)",
                        (next_seq, value),
                    )
                    uncommitted = True
                    control.send(("ok", next_seq))
                elif command == "commit_uncommitted":
                    if not uncommitted:
                        raise RuntimeError("The writer has no transaction")
                    writer.execute("COMMIT")
                    uncommitted = False
                    control.send(("ok", None))
                elif command == "rollback_uncommitted":
                    if not uncommitted:
                        raise RuntimeError("The writer has no transaction")
                    writer.execute("ROLLBACK")
                    uncommitted = False
                    control.send(("ok", None))
                elif command == "checkpoint":
                    mode = str(arguments.get("mode", "PASSIVE")).upper()
                    if mode not in {"PASSIVE", "RESTART", "TRUNCATE"}:
                        raise ValueError(f"Unsupported checkpoint mode {mode!r}")
                    result = writer.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
                    control.send(("ok", tuple(result)))
                elif command == "reopen":
                    if uncommitted:
                        raise RuntimeError("Cannot reopen with an active transaction")
                    writer.close()
                    writer = connect(database_path)
                    writer.execute("PRAGMA wal_autocheckpoint = 0")
                    control.send(("ok", None))
                elif command == "exit_without_sqlite_cleanup":
                    if uncommitted:
                        raise RuntimeError("Cannot exit with an active transaction")
                    control.send(("ok", None))
                    control.close()
                    # Deliberately simulate writer-process loss so the OS closes
                    # every descriptor without SQLite unlinking the SHM file.
                    # This leaves the reader as the only possible DMS lock owner.
                    os._exit(0)
                elif command == "barrier":
                    count = writer.execute(
                        "SELECT count(*) FROM qplot_trusted_probe"
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
        # Controller EOF ends this test writer; its finally block still performs
        # rollback and closes owned resources.
        pass
    except BaseException:
        try:
            control.send(("startup_error", traceback.format_exc()))
        except (BrokenPipeError, EOFError, OSError):
            # The test controller has disappeared, so the child cannot deliver its
            # startup diagnostic.
            pass
    finally:
        if writer is not None:
            if uncommitted:
                try:
                    writer.execute("ROLLBACK")
                except BaseException:
                    # Rollback is best effort in test-writer teardown; subsequent
                    # connection and measurement cleanup still runs.
                    pass
            writer.close()
        if run_context is not None:
            run_context.__exit__(None, None, None)
        _close_qcodes_run_handles(dataset, experiment)
        control.close()


@dataclass
class _QcodesWalWriter:
    database_path: Path
    process: multiprocessing.Process
    control: Connection
    startup: dict[str, Any]

    @classmethod
    def start(
        cls,
        database_path: Path,
        *,
        sparse_size: int | None = None,
    ) -> _QcodesWalWriter:
        context = multiprocessing.get_context("spawn")
        parent_control, child_control = context.Pipe(duplex=True)
        process = context.Process(
            target=_qcodes_wal_writer_process,
            args=(str(database_path), child_control, sparse_size),
            name="qplot-test-qcodes-wal-writer",
        )
        process.start()
        child_control.close()
        if not parent_control.poll(60):
            process.terminate()
            process.join(10)
            parent_control.close()
            raise AssertionError("The QCoDeS WAL writer did not start in time")
        kind, payload = parent_control.recv()
        if kind != "ready":
            process.join(10)
            parent_control.close()
            raise AssertionError(f"The QCoDeS WAL writer failed:\n{payload}")
        return cls(database_path, process, parent_control, payload)

    def request(
        self,
        command: str,
        *,
        timeout: float = 30,
        **arguments: Any,
    ) -> Any:
        if not self.process.is_alive():
            raise AssertionError(
                f"The QCoDeS WAL writer exited with {self.process.exitcode}"
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
                # A dead test writer cannot acknowledge stop; join and forced
                # termination below still clean it up.
                pass
        self.process.join(10)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(10)
        self.control.close()
        assert not self.process.is_alive()
        assert self.process.exitcode == 0


@pytest.fixture
def live_writer(tmp_path: Path) -> _QcodesWalWriter:
    writer = _QcodesWalWriter.start(tmp_path / "live.db")
    try:
        assert writer.startup["run_count"] == 1
        assert Path(f"{writer.database_path}-wal").is_file()
        assert Path(f"{writer.database_path}-shm").is_file()
        # The command-driven writer is idle and no reader has started. On
        # Windows the first audit read/close can publish pending WAL metadata.
        _stable_artifact_state(writer.database_path)
        yield writer
    finally:
        writer.close()


def _artifact_state(database_path: Path) -> dict[str, tuple[Any, ...] | None]:
    """Observe source artifacts from a separate spawned audit process."""
    context = multiprocessing.get_context("spawn")
    parent_control, child_control = context.Pipe(duplex=False)
    process = context.Process(
        target=_artifact_state_in_audit_process,
        args=(str(database_path), child_control),
        name="qplot-test-source-audit",
    )
    process.start()
    child_control.close()
    try:
        assert parent_control.poll(30), "Source artifact audit timed out"
        kind, payload = parent_control.recv()
        assert kind == "ok", f"Source artifact audit failed:\n{payload}"
    finally:
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(10)
        parent_control.close()
    assert process.exitcode == 0
    return payload


def _stable_artifact_state(
    database_path: Path,
    *,
    consecutive_observations: int = 5,
    observation_interval: float = 0.05,
) -> dict[str, tuple[Any, ...] | None]:
    """Wait for identical content AND metadata at an explicit writer boundary.

    In particular, Windows can publish ChangeTime on the first audit read's
    close, after that audit sampled fstat. Establish writer baselines before
    reader activity; never replace a baseline after a protection failure.
    """
    previous: dict[str, tuple[Any, ...] | None] | None = None
    stable_observations = 0
    for _ in range(100):
        current = _artifact_state(database_path)
        if current == previous:
            stable_observations += 1
            if stable_observations >= consecutive_observations:
                return current
        else:
            previous = current
            stable_observations = 0
        time.sleep(observation_interval)
    raise AssertionError("The source database family did not become stable")


def _assert_protected_artifacts_unchanged(
    before: Mapping[str, tuple[Any, ...] | None],
    after: Mapping[str, tuple[Any, ...] | None],
) -> None:
    """Require immutable artifacts while allowing SQLite's SHM coordination."""
    for suffix in ("", "-wal", "-journal"):
        assert after[suffix] == before[suffix], suffix

    before_shm = before["-shm"]
    after_shm = after["-shm"]
    assert after_shm is not None, "The live reader must retain its exact SHM file"
    assert stat.S_ISREG(after_shm[3])
    if before_shm is not None:
        # Content, size, mtime and ctime are SQLite WAL-index state and may
        # legitimately change. SQLite may also normalise the exact SHM file's
        # permission bits. The regular filesystem object must stay singly
        # linked with the same owner.
        assert stat.S_ISREG(before_shm[3])
        assert (after_shm[1], after_shm[2]) == (before_shm[1], before_shm[2])
        assert before_shm[4] == after_shm[4] == 1
        assert (after_shm[5], after_shm[6]) == (before_shm[5], before_shm[6])


def _assert_safe_audit(counters: Mapping[str, int]) -> None:
    assert set(counters) == _AUDIT_KEYS
    assert all(type(counters[key]) is int for key in _AUDIT_KEYS)
    assert {key: counters[key] for key in _PROHIBITED_AUDIT_KEYS} == {
        key: 0 for key in _PROHIBITED_AUDIT_KEYS
    }
    assert counters["source_open_readonly"] >= 2
    assert counters["source_open_flags_stripped"] > 0
    assert counters["source_read"] > 0
    assert counters["source_read_bytes"] > 0
    assert counters["shm_map_readonly"] == 0
    assert counters["shm_map_writable"] > 0
    assert counters["shm_lock"] > 0
    assert counters["identity_verified"] >= 3
    assert counters["proof_open"] >= 3
    assert counters["proof_close"] <= counters["proof_open"]
    assert 0 <= counters["proof_active"] <= counters["proof_peak"]
