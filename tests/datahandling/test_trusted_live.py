"""Black-box safety and live-visibility tests for the trusted reader."""

from __future__ import annotations

import gc
import hashlib
import multiprocessing
import os
import queue
import shutil
import stat
import subprocess
import sys
import threading
import time
import traceback
import weakref
from collections.abc import Mapping
from dataclasses import dataclass
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.dataset.sqlite.connection import atomic
from qcodes.dataset.sqlite.database import connect
from qcodes.parameters import ManualParameter

from qplot.datahandling.file_identity import database_instance
from qplot.datahandling.trusted_live import (
    TrustedLiveBusyTimeoutError,
    TrustedLiveCancelledError,
    TrustedLiveCleanupError,
    TrustedLiveDeadlineExceededError,
    TrustedLiveInvalidDatabaseError,
    TrustedLiveQueryError,
    TrustedLiveReader,
    TrustedLiveReaderClosedError,
    TrustedLiveReaderThreadError,
    TrustedLiveReaderUnavailableError,
    TrustedLiveSourceChangedError,
    TrustedLiveSourceIOError,
    TrustedLiveSqlRejectedError,
    TrustedLiveUnsupportedSourceError,
    TrustedQuery,
)

pytestmark = pytest.mark.timeout(120)

_ARTIFACT_SUFFIXES = ("", "-wal", "-shm", "-journal")
_SQLITE_UNIX_DMS_BYTE = 128
_SQLITE_UNIX_SHARED_FIRST_BYTE = 0x40000002
_SQLITE_UNIX_SHARED_BYTE_COUNT = 510
_AUDIT_KEYS = {
    "source_open_readonly",
    "source_open_readwrite",
    "source_open_create",
    "source_open_delete_on_close",
    "source_open_flags_stripped",
    "source_read",
    "source_read_bytes",
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
        pass
    except BaseException:
        try:
            control.send(("startup_error", traceback.format_exc()))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        if writer is not None:
            if uncommitted:
                try:
                    writer.execute("ROLLBACK")
                except BaseException:
                    pass
            writer.close()
        if run_context is not None:
            run_context.__exit__(None, None, None)
        _close_qcodes_run_handles(dataset, experiment)
        control.close()


def _clean_qcodes_wal_database_process(
    database_path: str,
    control: Connection,
) -> None:
    """Create a checkpointed QCoDeS WAL database, then close every handle."""
    experiment = None
    run_context = None
    dataset = None
    writer = None
    try:
        (
            experiment,
            _setpoint,
            _signal,
            run_context,
            _datasaver,
            dataset,
        ) = _start_qcodes_run(database_path)
        writer = connect(database_path)
        with atomic(writer):
            writer.execute(
                "CREATE TABLE IF NOT EXISTS qplot_trusted_probe ("
                "seq INTEGER PRIMARY KEY, value TEXT NOT NULL, "
                "payload BLOB NOT NULL DEFAULT X'')"
            )
            writer.execute(
                "INSERT OR IGNORE INTO qplot_trusted_probe(seq, value) "
                "VALUES(0, 'initial')"
            )
        run_context.__exit__(None, None, None)
        run_context = None
        _close_qcodes_run_handles(dataset, experiment)
        dataset = None
        experiment = None
        checkpoint = writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is None or checkpoint[0] != 0:
            raise RuntimeError(f"Could not checkpoint clean WAL source: {checkpoint!r}")
        writer.close()
        writer = None
        control.send(("ok", None))
    except BaseException:
        control.send(("error", traceback.format_exc()))
    finally:
        if writer is not None:
            writer.close()
        if run_context is not None:
            run_context.__exit__(None, None, None)
        _close_qcodes_run_handles(dataset, experiment)
        control.close()


def _create_clean_qcodes_wal_database(database_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    parent_control, child_control = context.Pipe(duplex=False)
    process = context.Process(
        target=_clean_qcodes_wal_database_process,
        args=(str(database_path), child_control),
        name="qplot-test-clean-wal-creator",
    )
    process.start()
    child_control.close()
    try:
        assert parent_control.poll(60), "Clean QCoDeS WAL creation timed out"
        kind, payload = parent_control.recv()
        assert kind == "ok", f"Clean QCoDeS WAL creation failed:\n{payload}"
    finally:
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(10)
        parent_control.close()
    assert process.exitcode == 0


def _cleanup_fault_reader_process(
    database_path: str,
    fault: str,
    control: Connection,
) -> None:
    """Exercise one native cleanup fault in a disposable process session."""

    reader: TrustedLiveReader | None = None
    try:
        reader = TrustedLiveReader.open(
            database_path,
            _test_cleanup_fault=fault,
        )
        assert reader.query("SELECT 1").rows == ((1,),)
        try:
            reader.close()
        except BaseException as error:
            close_error = (type(error).__name__, str(error))
        else:
            close_error = None

        audit = dict(reader.audit().counters)
        try:
            replacement_reader = TrustedLiveReader.open(database_path)
        except BaseException as error:
            reuse_error = (type(error).__name__, str(error))
        else:
            reuse_error = None
            replacement_reader.close()
        control.send(
            (
                "ok",
                {
                    "close_error": close_error,
                    "audit": audit,
                    "reuse_error": reuse_error,
                },
            )
        )
    except BaseException:
        control.send(("error", traceback.format_exc()))
    finally:
        if reader is not None and not reader.closed:
            try:
                reader.close()
            except BaseException:
                pass
        control.close()


def _exercise_cleanup_fault(
    database_path: Path,
    fault: str,
) -> dict[str, Any]:
    context = multiprocessing.get_context("spawn")
    parent_control, child_control = context.Pipe(duplex=False)
    process = context.Process(
        target=_cleanup_fault_reader_process,
        args=(str(database_path), fault, child_control),
        name=f"qplot-test-cleanup-fault-{fault}",
    )
    process.start()
    child_control.close()
    try:
        assert parent_control.poll(30), f"Cleanup fault {fault!r} timed out"
        kind, payload = parent_control.recv()
        assert kind == "ok", f"Cleanup fault {fault!r} failed:\n{payload}"
    finally:
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(10)
        parent_control.close()
    assert process.exitcode == 0
    assert isinstance(payload, dict)
    return payload


def _forced_cleanup_fault_process(
    database_path: str,
    failure_phase: str,
    control: Connection,
) -> None:
    """Inject uncertainty while unwinding an open or operation failure."""

    original_validate = TrustedLiveReader._validate_native_source
    reader: TrustedLiveReader | None = None

    def fail_after_native_open(_reader: TrustedLiveReader) -> None:
        raise TrustedLiveSourceIOError(
            f"simulated {failure_phase} failure after the native main handle opened"
        )

    try:
        if failure_phase == "open":
            TrustedLiveReader._validate_native_source = fail_after_native_open
        elif failure_phase == "operation":
            reader = TrustedLiveReader.open(
                database_path,
                _test_cleanup_fault="base_close",
            )
            TrustedLiveReader._validate_native_source = fail_after_native_open
        else:
            raise AssertionError(f"Unknown forced-cleanup phase {failure_phase!r}")
        try:
            if failure_phase == "open":
                reader = TrustedLiveReader.open(
                    database_path,
                    _test_cleanup_fault="base_close",
                )
            else:
                assert reader is not None
                reader.query("SELECT 1")
        except BaseException as error:
            cause = error.__cause__
            first_error = (
                type(error).__name__,
                str(error),
                None if cause is None else type(cause).__name__,
                None if cause is None else str(cause),
            )
        else:
            first_error = None
        finally:
            TrustedLiveReader._validate_native_source = original_validate
        if reader is not None and not reader.closed:
            reader.close()

        try:
            replacement_reader = TrustedLiveReader.open(database_path)
        except BaseException as error:
            reuse_error = (type(error).__name__, str(error))
        else:
            reuse_error = None
            replacement_reader.close()
        control.send(
            (
                "ok",
                {
                    "first_error": first_error,
                    "reuse_error": reuse_error,
                },
            )
        )
    except BaseException:
        control.send(("error", traceback.format_exc()))
    finally:
        TrustedLiveReader._validate_native_source = original_validate
        if reader is not None and not reader.closed:
            try:
                reader.close()
            except BaseException:
                pass
        control.close()


def _exercise_forced_cleanup_fault(
    database_path: Path,
    failure_phase: str,
) -> dict[str, Any]:
    context = multiprocessing.get_context("spawn")
    parent_control, child_control = context.Pipe(duplex=False)
    process = context.Process(
        target=_forced_cleanup_fault_process,
        args=(str(database_path), failure_phase, child_control),
        name=f"qplot-test-{failure_phase}-cleanup-fault",
    )
    process.start()
    child_control.close()
    try:
        assert parent_control.poll(30), f"{failure_phase} cleanup fault timed out"
        kind, payload = parent_control.recv()
        assert kind == "ok", f"{failure_phase} cleanup fault failed:\n{payload}"
    finally:
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(10)
        parent_control.close()
    assert process.exitcode == 0
    assert isinstance(payload, dict)
    return payload


def _preflight_cleanup_fault_process(
    database_path: str,
    fault: str,
    control: Connection,
) -> None:
    """Inject one proven preflight-close failure in a disposable process."""

    from qplot.datahandling import trusted_live as trusted_live_module

    try:
        if fault == "identity_handle":

            def fail_identity_handle_close(_path: Any) -> Any:
                raise trusted_live_module.FileIdentityHandleCloseError(
                    5,
                    "simulated checked identity HANDLE close failure",
                )

            trusted_live_module.checked_path_bound_file_identity = (
                fail_identity_handle_close
            )
        elif fault == "header_descriptor":
            real_close = trusted_live_module._close_preflight_file_descriptor

            def fail_header_descriptor_close(file_descriptor: int) -> None:
                real_close(file_descriptor)
                raise trusted_live_module._PreflightFileDescriptorCloseError(
                    5,
                    "simulated checked header descriptor close failure",
                )

            trusted_live_module._close_preflight_file_descriptor = (
                fail_header_descriptor_close
            )
        else:
            raise AssertionError(f"Unknown preflight cleanup fault {fault!r}")

        try:
            reader = TrustedLiveReader.open(database_path)
        except BaseException as error:
            first_error = (type(error).__name__, str(error))
        else:
            first_error = None
            reader.close()

        try:
            replacement_reader = TrustedLiveReader.open(database_path)
        except BaseException as error:
            reuse_error = (type(error).__name__, str(error))
        else:
            reuse_error = None
            replacement_reader.close()
        control.send(
            (
                "ok",
                {
                    "first_error": first_error,
                    "reuse_error": reuse_error,
                },
            )
        )
    except BaseException:
        control.send(("error", traceback.format_exc()))
    finally:
        control.close()


def _exercise_preflight_cleanup_fault(
    database_path: Path,
    fault: str,
) -> dict[str, Any]:
    context = multiprocessing.get_context("spawn")
    parent_control, child_control = context.Pipe(duplex=False)
    process = context.Process(
        target=_preflight_cleanup_fault_process,
        args=(str(database_path), fault, child_control),
        name=f"qplot-test-preflight-cleanup-fault-{fault}",
    )
    process.start()
    child_control.close()
    try:
        assert parent_control.poll(30), f"Preflight cleanup fault {fault!r} timed out"
        kind, payload = parent_control.recv()
        assert kind == "ok", f"Preflight cleanup fault {fault!r} failed:\n{payload}"
    finally:
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(10)
        parent_control.close()
    assert process.exitcode == 0
    assert isinstance(payload, dict)
    return payload


def _posix_exclusive_lock_probe_process(
    file_path: str,
    start: int,
    length: int,
    control: Connection,
    probe_now: Any | None = None,
) -> None:
    """Report whether a separate process can take one exclusive range lock."""
    descriptor = -1
    try:
        import errno
        import fcntl

        if probe_now is not None:
            control.send(("ready", None))
            if not probe_now.wait(30):
                raise TimeoutError("deferred exclusive-lock probe was not released")
        descriptor = os.open(file_path, os.O_RDWR)
        try:
            fcntl.lockf(
                descriptor,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
                length,
                start,
                os.SEEK_SET,
            )
        except OSError as error:
            if error.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
            exclusive_lock_acquired = False
        else:
            exclusive_lock_acquired = True
            fcntl.lockf(
                descriptor,
                fcntl.LOCK_UN,
                length,
                start,
                os.SEEK_SET,
            )
        control.send(("ok", exclusive_lock_acquired))
    except BaseException:
        control.send(("error", traceback.format_exc()))
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        control.close()


def _rollback_exclusive_writer_process(
    database_path: str,
    control: Connection,
) -> None:
    """Hold a real rollback-mode SQLite EXCLUSIVE transaction on demand."""
    import sqlite3

    connection = None
    try:
        connection = sqlite3.connect(database_path, isolation_level=None)
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("BEGIN EXCLUSIVE")
        control.send(("ready", None))
        control.recv()
        connection.execute("ROLLBACK")
        control.send(("ok", None))
    except BaseException:
        control.send(("error", traceback.format_exc()))
    finally:
        if connection is not None:
            connection.close()
        control.close()


@dataclass
class _RollbackExclusiveWriter:
    process: multiprocessing.Process
    control: Connection

    @classmethod
    def start(cls, database_path: Path) -> _RollbackExclusiveWriter:
        context = multiprocessing.get_context("spawn")
        parent_control, child_control = context.Pipe(duplex=True)
        process = context.Process(
            target=_rollback_exclusive_writer_process,
            args=(str(database_path), child_control),
            name="qplot-test-rollback-exclusive-writer",
        )
        process.start()
        child_control.close()
        assert parent_control.poll(30), "Exclusive rollback writer timed out"
        kind, payload = parent_control.recv()
        assert kind == "ready", f"Exclusive rollback writer failed:\n{payload}"
        return cls(process, parent_control)

    def close(self) -> None:
        if self.process.is_alive():
            self.control.send(("stop", None))
            assert self.control.poll(30), "Exclusive rollback writer did not stop"
            kind, payload = self.control.recv()
            assert kind == "ok", f"Exclusive rollback writer failed:\n{payload}"
        self.process.join(10)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(10)
        self.control.close()
        assert self.process.exitcode == 0


class _ABASidecarSwap:
    """Install B at A's pathname, then restore both exact filesystem objects."""

    def __init__(self, selected: Path, replacement: Path) -> None:
        self.selected = selected
        self.replacement = replacement
        self.parked = selected.with_name(f"{selected.name}.qplot-race-parked-a")
        self.installed = False

    def install_b(self) -> None:
        assert not self.installed
        assert not self.parked.exists()
        os.replace(self.selected, self.parked)
        os.replace(self.replacement, self.selected)
        self.installed = True

    def restore_a(self) -> None:
        if not self.installed:
            return
        os.replace(self.selected, self.replacement)
        os.replace(self.parked, self.selected)
        self.installed = False


def _wait_for_race_marker(directory: Path, pattern: str) -> Path:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        matches = list(directory.glob(pattern))
        if len(matches) == 1:
            return matches[0]
        assert len(matches) == 0, matches
        time.sleep(0.005)
    raise AssertionError(f"Native race marker did not appear: {pattern}")


def _exercise_native_aba_race(
    database_path: Path,
    replacement_path: Path,
    artifact: str,
) -> None:
    published: queue.Queue[Path] = queue.Queue()
    outcomes: queue.Queue[BaseException | None] = queue.Queue()

    def publish_before_open(_token: Any, temporary_directory: Path) -> None:
        published.put(temporary_directory)

    def open_in_owner_thread() -> None:
        reader: TrustedLiveReader | None = None
        try:
            reader = TrustedLiveReader.open(
                database_path,
                _test_race_artifact=artifact,
                _test_pre_open_callback=publish_before_open,
            )
            reader.query("SELECT 1")
        except BaseException as error:
            outcomes.put(error)
        else:
            outcomes.put(None)
        finally:
            if reader is not None:
                try:
                    reader.close()
                except BaseException as error:
                    if outcomes.empty():
                        outcomes.put(error)

    artifact_suffix = "" if artifact == "main" else f"-{artifact}"
    swap = _ABASidecarSwap(
        Path(f"{database_path}{artifact_suffix}"),
        replacement_path,
    )
    owner_thread = threading.Thread(target=open_in_owner_thread)
    owner_thread.start()
    temporary_directory = published.get(timeout=10)
    try:
        proof_ready = _wait_for_race_marker(
            temporary_directory,
            f"qplot-*-race-{artifact}-proof-ready.tmp",
        )
        swap.install_b()
        proof_ready.with_name(proof_ready.name.replace("-ready", "-release")).touch()
        actual_ready = _wait_for_race_marker(
            temporary_directory,
            f"qplot-*-race-{artifact}-actual-ready.tmp",
        )
        restore_after_native_close = False
        try:
            swap.restore_a()
        except OSError:
            if os.name != "nt":
                raise
            # SQLite's pinned Windows VFS omits FILE_SHARE_DELETE from its
            # actual source HANDLE.  That makes the A return leg impossible
            # while B is open; release validation with B still installed, then
            # restore A after the native handle has closed.
            restore_after_native_close = True
        actual_ready.with_name(actual_ready.name.replace("-ready", "-release")).touch()
        if restore_after_native_close:
            owner_thread.join(15)
            swap.restore_a()
    finally:
        # Ensure a failed assertion never strands the native test hook.
        for ready in temporary_directory.glob(f"qplot-*-race-{artifact}-*-ready.tmp"):
            ready.with_name(ready.name.replace("-ready", "-release")).touch()
        owner_thread.join(15)
        swap.restore_a()
    assert not owner_thread.is_alive()
    error = outcomes.get_nowait()
    assert isinstance(error, TrustedLiveSourceChangedError), error


def _posix_exclusive_lock_is_available(
    file_path: Path,
    *,
    start: int,
    length: int,
) -> bool:
    context = multiprocessing.get_context("spawn")
    parent_control, child_control = context.Pipe(duplex=True)
    process = context.Process(
        target=_posix_exclusive_lock_probe_process,
        args=(str(file_path), start, length, child_control),
        name="qplot-test-exclusive-lock-probe",
    )
    process.start()
    child_control.close()
    try:
        assert parent_control.poll(30), "DMS lock probe timed out"
        kind, payload = parent_control.recv()
        assert kind == "ok", f"DMS lock probe failed:\n{payload}"
    finally:
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(10)
        parent_control.close()
    assert process.exitcode == 0
    return bool(payload)


def _posix_dms_exclusive_lock_is_available(shm_path: Path) -> bool:
    return _posix_exclusive_lock_is_available(
        shm_path,
        start=_SQLITE_UNIX_DMS_BYTE,
        length=1,
    )


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
        yield writer
    finally:
        writer.close()


def _probe_rows(reader: TrustedLiveReader) -> tuple[tuple[Any, ...], ...]:
    result = reader.query(
        "SELECT seq, value, length(payload) FROM qplot_trusted_probe ORDER BY seq"
    )
    assert result.columns == ("seq", "value", "length(payload)")
    return result.rows


def _qcodes_result_count(
    reader: TrustedLiveReader,
    writer: _QcodesWalWriter,
) -> int:
    table_name = str(writer.startup["result_table_name"])
    quoted_table_name = '"' + table_name.replace('"', '""') + '"'
    return reader.query(f"SELECT count(*) FROM {quoted_table_name}").rows[0][0]


def _file_descriptor_digest(file_descriptor: int) -> str:
    """Hash the exact object already bound to ``file_descriptor``."""

    digest = hashlib.sha256()
    while chunk := os.read(file_descriptor, 1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def _artifact_state_in_audit_process(
    database_path: str,
    control: Connection,
) -> None:
    """Capture source state without touching reader-process POSIX descriptors."""
    state: dict[str, tuple[Any, ...] | None] = {}
    try:
        for suffix in _ARTIFACT_SUFFIXES:
            artifact = Path(f"{database_path}{suffix}")
            open_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
            open_flags |= getattr(os, "O_CLOEXEC", 0)
            open_flags |= getattr(os, "O_NOINHERIT", 0)
            open_flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                file_descriptor = os.open(artifact, open_flags)
            except FileNotFoundError:
                state[suffix] = None
                continue
            try:
                status = os.fstat(file_descriptor)
                digest = (
                    _file_descriptor_digest(file_descriptor)
                    if stat.S_ISREG(status.st_mode)
                    else None
                )
                state[suffix] = (
                    digest,
                    status.st_dev,
                    status.st_ino,
                    status.st_mode,
                    status.st_nlink,
                    status.st_uid,
                    status.st_gid,
                    status.st_size,
                    status.st_mtime_ns,
                    status.st_ctime_ns,
                )
            finally:
                os.close(file_descriptor)
        control.send(("ok", state))
    except BaseException:
        control.send(("error", traceback.format_exc()))
    finally:
        control.close()


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


def _path_stat_in_audit_process(file_path: str, control: Connection) -> None:
    try:
        try:
            status = os.stat(file_path, follow_symlinks=False)
        except FileNotFoundError:
            status = None
        control.send(("ok", status))
    except BaseException:
        control.send(("error", traceback.format_exc()))
    finally:
        control.close()


def _audited_stat(file_path: Path) -> os.stat_result | None:
    """Stat one potentially huge source without hashing or touching reader fds."""
    context = multiprocessing.get_context("spawn")
    parent_control, child_control = context.Pipe(duplex=False)
    process = context.Process(
        target=_path_stat_in_audit_process,
        args=(str(file_path), child_control),
        name="qplot-test-source-stat-audit",
    )
    process.start()
    child_control.close()
    try:
        assert parent_control.poll(30), "Source stat audit timed out"
        kind, payload = parent_control.recv()
        assert kind == "ok", f"Source stat audit failed:\n{payload}"
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
    """Wait for repeated identical source-family content and metadata."""
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


def test_committed_qcodes_wal_rows_refresh_on_one_persistent_reader(
    live_writer: _QcodesWalWriter,
) -> None:
    with TrustedLiveReader.open(live_writer.database_path) as reader:
        assert reader.query("SELECT name FROM runs ORDER BY run_id").rows == (
            ("trusted_live_run",),
        )
        assert _qcodes_result_count(reader, live_writer) == 1
        assert _probe_rows(reader) == ((0, "initial", 0),)

        seq = live_writer.request("commit", value="later", payload_size=17)

        assert seq == 1
        assert _qcodes_result_count(reader, live_writer) == 2
        assert _probe_rows(reader) == (
            (0, "initial", 0),
            (1, "later", 17),
        )
        _assert_safe_audit(reader.audit().counters)


def test_zero_row_select_retains_validated_result_columns(
    live_writer: _QcodesWalWriter,
) -> None:
    with TrustedLiveReader.open(live_writer.database_path) as reader:
        result = reader.query(
            "SELECT run_id, guid FROM runs WHERE 0",
        )

        assert result.columns == ("run_id", "guid")
        assert result.rows == ()


def test_wal_and_shm_may_appear_between_finite_reader_operations(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "initially-clean.db"
    _create_clean_qcodes_wal_database(database_path)
    assert database_path.is_file()
    assert not Path(f"{database_path}-wal").exists()
    assert not Path(f"{database_path}-shm").exists()
    before_reader = _artifact_state(database_path)

    writer: _QcodesWalWriter | None = None
    with TrustedLiveReader.open(database_path) as reader:
        assert _probe_rows(reader) == ((0, "initial", 0),)
        during_reader = _artifact_state(database_path)
        assert during_reader["-wal"] is None
        assert during_reader["-shm"] is not None
        _assert_protected_artifacts_unchanged(before_reader, during_reader)
        writer = _QcodesWalWriter.start(database_path)
        try:
            appeared = _artifact_state(database_path)
            assert appeared["-wal"] is not None
            assert appeared["-shm"] is not None
            writer.request("commit", value="after-sidecars-appeared")
            assert _probe_rows(reader)[-1][1] == "after-sidecars-appeared"
        finally:
            writer.close()


def test_uncommitted_wal_rows_are_invisible_until_commit(
    live_writer: _QcodesWalWriter,
) -> None:
    with TrustedLiveReader.open(live_writer.database_path) as reader:
        seq = live_writer.request("begin_uncommitted", value="not-yet-visible")
        assert seq == 1
        assert _probe_rows(reader) == ((0, "initial", 0),)

        live_writer.request("commit_uncommitted")

        assert _probe_rows(reader) == (
            (0, "initial", 0),
            (1, "not-yet-visible", 0),
        )


def test_finite_query_batch_is_repeatable_then_next_operation_refreshes(
    live_writer: _QcodesWalWriter,
) -> None:
    with TrustedLiveReader.open(live_writer.database_path) as reader:
        commit_error: list[BaseException] = []

        def commit_during_batch() -> None:
            try:
                time.sleep(0.05)
                live_writer.request("commit", value="new-commit")
            except BaseException as error:
                commit_error.append(error)

        commit_thread = threading.Thread(target=commit_during_batch)
        commit_thread.start()
        results = reader.query_batch(
            (
                TrustedQuery("SELECT value FROM qplot_trusted_probe ORDER BY seq"),
                TrustedQuery(
                    "WITH RECURSIVE values_(n) AS ("
                    "SELECT 1 UNION ALL SELECT n + 1 FROM values_ "
                    "WHERE n < 5000000) SELECT sum(n) FROM values_"
                ),
                TrustedQuery("SELECT value FROM qplot_trusted_probe ORDER BY seq"),
            ),
            timeout=4.0,
        )
        commit_thread.join(10)
        assert not commit_thread.is_alive()
        assert commit_error == []
        assert results[0].rows == (("initial",),)
        assert results[2].rows == (("initial",),)
        assert reader.query(
            "SELECT value FROM qplot_trusted_probe ORDER BY seq"
        ).rows == (("initial",), ("new-commit",))
        assert not hasattr(reader, "read_transaction")


def test_checkpoint_reset_and_writer_reopen_remain_live(
    live_writer: _QcodesWalWriter,
) -> None:
    with TrustedLiveReader.open(live_writer.database_path) as reader:
        for commit_number, checkpoint_mode in enumerate(
            ("PASSIVE", "RESTART", "TRUNCATE")
        ):
            value = f"before-{checkpoint_mode.casefold()}-{commit_number}"
            live_writer.request("commit", value=value)
            rows = _probe_rows(reader)
            assert [row[0] for row in rows] == list(range(len(rows)))
            assert rows[-1][1] == value

            checkpoint = live_writer.request("checkpoint", mode=checkpoint_mode)
            assert checkpoint[0] == 0
            if checkpoint_mode == "TRUNCATE":
                assert checkpoint == (0, 0, 0)
                wal_state = _artifact_state(live_writer.database_path)["-wal"]
                assert wal_state is not None and wal_state[7] == 0

            after_value = f"after-{checkpoint_mode.casefold()}"
            live_writer.request("commit", value=after_value)
            assert _probe_rows(reader)[-1][1] == after_value

        live_writer.request("reopen")
        live_writer.request("commit", value="after-reopen")
        assert _probe_rows(reader)[-1][1] == "after-reopen"

    with TrustedLiveReader.open(live_writer.database_path) as reopened_reader:
        assert _probe_rows(reopened_reader)[-1][1] == "after-reopen"


def test_reader_handles_a_resized_shared_wal_index(
    live_writer: _QcodesWalWriter,
) -> None:
    shm_path = Path(f"{live_writer.database_path}-shm")
    initial_size = shm_path.stat().st_size

    with TrustedLiveReader.open(live_writer.database_path) as reader:
        live_writer.request(
            "commit",
            value="large-wal-index",
            payload_size=20 * 1024**2,
        )
        assert _probe_rows(reader)[-1][1:] == ("large-wal-index", 20 * 1024**2)
        observed_shm = _artifact_state(live_writer.database_path)["-shm"]
        assert observed_shm is not None
        assert observed_shm[7] > initial_size


def test_reader_recovers_a_corrupt_retained_shm_after_abrupt_writer_exit(
    live_writer: _QcodesWalWriter,
) -> None:
    live_writer.request("commit", value="committed-before-crash")
    live_writer.request("exit_without_sqlite_cleanup")
    live_writer.process.join(10)
    assert live_writer.process.exitcode == 0

    shm_path = Path(f"{live_writer.database_path}-shm")
    assert shm_path.is_file()
    with shm_path.open("r+b", buffering=0) as shm_file:
        shm_file.truncate(1)
        shm_file.seek(0)
        shm_file.write(b"\xff")

    with TrustedLiveReader.open(live_writer.database_path) as reader:
        assert _probe_rows(reader)[-1][1] == "committed-before-crash"
        observed_shm = _artifact_state(live_writer.database_path)["-shm"]
        assert observed_shm is not None
        assert observed_shm[7] >= 32 * 1024

    assert shm_path.is_file()


@pytest.mark.parametrize("exit_kind", ["clean", "abrupt"])
def test_acquisition_can_restart_after_writer_exit(
    tmp_path: Path,
    exit_kind: str,
) -> None:
    database_path = tmp_path / f"writer-{exit_kind}.db"
    writer = _QcodesWalWriter.start(database_path)
    writer_closed = False
    restarted: _QcodesWalWriter | None = None
    reader = TrustedLiveReader.open(database_path)
    try:
        writer.request("commit", value="before-writer-exit")
        assert _probe_rows(reader)[-1][1] == "before-writer-exit"

        if exit_kind == "clean":
            writer.close()
            writer_closed = True
        else:
            writer.request("exit_without_sqlite_cleanup")
            writer.process.join(10)
            assert writer.process.exitcode == 0

        assert _artifact_state(database_path)["-shm"] is not None
        restarted = _QcodesWalWriter.start(database_path)
        restarted.request("commit", value="after-writer-restart")
        assert _probe_rows(reader)[-1][1] == "after-writer-restart"
    finally:
        reader.close()
        if not writer_closed:
            writer.close()
        if restarted is not None:
            restarted.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX fcntl DMS-lock proof")
def test_posix_dms_lock_survives_writer_exit_and_reader_handles_restart(
    live_writer: _QcodesWalWriter,
) -> None:
    reader = TrustedLiveReader.open(live_writer.database_path)
    restarted_writer: _QcodesWalWriter | None = None
    try:
        assert _probe_rows(reader) == ((0, "initial", 0),)
        live_writer.request("exit_without_sqlite_cleanup")
        live_writer.process.join(10)
        assert live_writer.process.exitcode == 0

        shm_path = Path(f"{live_writer.database_path}-shm")
        assert _artifact_state(live_writer.database_path)["-shm"] is not None
        assert not _posix_dms_exclusive_lock_is_available(shm_path)

        restarted_writer = _QcodesWalWriter.start(live_writer.database_path)
        restarted_writer.request("commit", value="after-last-close")
        try:
            rows = _probe_rows(reader)
        except TrustedLiveSourceChangedError:
            # Older writer SQLite builds may unlink and recreate the sidecars
            # on an intermediate last close.  That valid replacement must be
            # rebound explicitly; if identities stayed stable, the original
            # reader remains usable through the restart.
            reader.close()
            reader = TrustedLiveReader.open(live_writer.database_path)
            rows = _probe_rows(reader)
        assert rows[-1][1] == "after-last-close"
        _assert_safe_audit(reader.audit().counters)
    finally:
        reader.close()
        if restarted_writer is not None:
            restarted_writer.close()


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="macOS last-MAP_SHARED teardown metadata regression",
)
def test_macos_last_reader_close_allows_only_shm_coordination_metadata(
    live_writer: _QcodesWalWriter,
) -> None:
    reader = TrustedLiveReader.open(live_writer.database_path)
    try:
        assert _probe_rows(reader) == ((0, "initial", 0),)
        live_writer.request("exit_without_sqlite_cleanup")
        live_writer.process.join(10)
        assert live_writer.process.exitcode == 0

        # Keep using the same WAL snapshot after the writer's descriptors and
        # writable SHM mapping have been torn down. qPlot is now the final WAL
        # participant and owns the final shared WAL-index mapping.
        assert _probe_rows(reader) == ((0, "initial", 0),)
        assert _probe_rows(reader) == ((0, "initial", 0),)
        before_close = _stable_artifact_state(live_writer.database_path)
    finally:
        reader.close()

    after_close = _stable_artifact_state(live_writer.database_path)
    _assert_protected_artifacts_unchanged(before_close, after_close)


@pytest.mark.skipif(os.name == "nt", reason="POSIX fcntl main-lock proof")
def test_posix_identity_validation_does_not_drop_active_main_database_lock(
    live_writer: _QcodesWalWriter,
) -> None:
    reader = TrustedLiveReader.open(live_writer.database_path)
    try:
        live_writer.request("exit_without_sqlite_cleanup")
        live_writer.process.join(10)
        assert live_writer.process.exitcode == 0

        lock_results: list[bool] = []

        def probe_during_query() -> None:
            time.sleep(0.05)
            lock_results.append(
                _posix_exclusive_lock_is_available(
                    live_writer.database_path,
                    start=_SQLITE_UNIX_SHARED_FIRST_BYTE,
                    length=_SQLITE_UNIX_SHARED_BYTE_COUNT,
                )
            )

        probe_thread = threading.Thread(target=probe_during_query)
        probe_thread.start()
        result = reader.query(
            "WITH RECURSIVE values_(n) AS ("
            "SELECT 1 UNION ALL SELECT n + 1 FROM values_ WHERE n < 5000000"
            ") SELECT (SELECT count(*) FROM runs), sum(n) FROM values_",
            timeout=4.0,
        )
        probe_thread.join(10)
        assert not probe_thread.is_alive()
        assert result.rows[0][0] == 1
        assert lock_results == [False]
    finally:
        reader.close()


def test_native_boundary_changes_only_allowed_shm_coordination_state(
    live_writer: _QcodesWalWriter,
) -> None:
    live_writer.request("barrier")
    before = _artifact_state(live_writer.database_path)
    assert before[""] is not None
    assert before["-wal"] is not None
    assert before["-shm"] is not None
    assert before["-journal"] is None

    reader = TrustedLiveReader.open(live_writer.database_path)
    try:
        assert _probe_rows(reader) == ((0, "initial", 0),)
        assert reader.data_version() > 0
        counters = reader.audit().counters
        _assert_safe_audit(counters)
        assert counters["shm_unmap_delete_forwarded"] == 0
    finally:
        reader.close()

    _assert_safe_audit(reader.audit().counters)
    assert reader.audit().counters["proof_active"] == 0
    assert (
        reader.audit().counters["proof_open"] == reader.audit().counters["proof_close"]
    )
    _assert_protected_artifacts_unchanged(
        before,
        _artifact_state(live_writer.database_path),
    )


def test_missing_shm_is_created_recovered_and_retained_without_source_writes(
    live_writer: _QcodesWalWriter,
    tmp_path: Path,
) -> None:
    live_writer.request("barrier")
    incomplete = tmp_path / "missing-shm.db"
    shutil.copyfile(live_writer.database_path, incomplete)
    shutil.copyfile(
        f"{live_writer.database_path}-wal",
        f"{incomplete}-wal",
    )
    before = _artifact_state(incomplete)
    assert before["-shm"] is None

    with TrustedLiveReader.open(incomplete) as reader:
        assert _probe_rows(reader) == ((0, "initial", 0),)
        assert _artifact_state(incomplete)["-shm"] is not None

    after = _artifact_state(incomplete)
    _assert_protected_artifacts_unchanged(before, after)


@pytest.mark.parametrize(
    "suffix",
    ["", "-wal", "-shm"],
    ids=["main", "wal", "shm"],
)
@pytest.mark.skipif(os.name == "nt", reason="Open-file replacement is POSIX-only")
def test_open_reader_rejects_source_file_replacement(
    live_writer: _QcodesWalWriter,
    tmp_path: Path,
    suffix: str,
) -> None:
    reader = TrustedLiveReader.open(live_writer.database_path)
    source = Path(f"{live_writer.database_path}{suffix}")
    replacement = tmp_path / f"replacement{suffix or '-main'}"
    original = tmp_path / f"original{suffix or '-main'}"
    os.link(source, original)
    shutil.copyfile(source, replacement)
    os.replace(replacement, source)
    try:
        with pytest.raises(
            TrustedLiveSourceChangedError,
            match="identity|changed|replaced",
        ):
            reader.query("SELECT 1")
        with pytest.raises(TrustedLiveSourceChangedError, match="changed"):
            reader.query("SELECT 1")
    finally:
        reader.close()
        os.replace(original, source)


def test_actual_main_handle_rejects_deterministic_aba_replacement(
    tmp_path: Path,
) -> None:
    selected = tmp_path / "aba-main.db"
    replacement = tmp_path / "aba-main-b.db"
    initialise_or_create_database_at(selected, journal_mode="DELETE")
    initialise_or_create_database_at(replacement, journal_mode="DELETE")

    _exercise_native_aba_race(selected, replacement, "main")

    with TrustedLiveReader.open(selected) as reader:
        assert reader.query("SELECT count(*) FROM runs").rows == ((0,),)


@pytest.mark.parametrize("artifact", ["wal", "shm"])
def test_actual_sidecar_handle_rejects_deterministic_aba_replacement(
    live_writer: _QcodesWalWriter,
    tmp_path: Path,
    artifact: str,
) -> None:
    live_writer.request("exit_without_sqlite_cleanup")
    live_writer.process.join(10)
    assert live_writer.process.exitcode == 0
    replacement = tmp_path / f"aba-b-{artifact}"
    shutil.copy2(
        Path(f"{live_writer.database_path}-{artifact}"),
        replacement,
    )

    _exercise_native_aba_race(live_writer.database_path, replacement, artifact)

    with TrustedLiveReader.open(live_writer.database_path) as reader:
        assert _probe_rows(reader) == ((0, "initial", 0),)


@pytest.mark.skipif(os.name == "nt", reason="Symlink creation is not portable")
def test_open_reader_rejects_symlink_selection(
    live_writer: _QcodesWalWriter,
    tmp_path: Path,
) -> None:
    live_writer.request("barrier")
    logical = tmp_path / "selected.db"
    logical.symlink_to(live_writer.database_path)

    with pytest.raises(
        TrustedLiveUnsupportedSourceError,
        match="symbolic[- ]link|symlink",
    ):
        TrustedLiveReader.open(logical)


def test_open_reader_rejects_symlinked_parent_component(
    live_writer: _QcodesWalWriter,
    tmp_path: Path,
) -> None:
    real_directory = tmp_path / "real"
    real_directory.mkdir()
    for suffix in ("", "-wal", "-shm"):
        shutil.copy2(
            Path(f"{live_writer.database_path}{suffix}"),
            Path(f"{real_directory / 'source.db'}{suffix}"),
        )
    linked_directory = tmp_path / "linked"
    if os.name == "nt":
        subprocess.run(
            [
                "cmd",
                "/d",
                "/c",
                "mklink",
                "/J",
                str(linked_directory),
                str(real_directory),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    else:
        linked_directory.symlink_to(real_directory, target_is_directory=True)

    with pytest.raises(
        TrustedLiveUnsupportedSourceError,
        match="symbolic[- ]link|symlink",
    ):
        TrustedLiveReader.open(linked_directory / "source.db")


@pytest.mark.skipif(os.name == "nt", reason="Symlink creation is not portable")
def test_open_reader_rejects_noncolocated_symlink_sidecar(
    live_writer: _QcodesWalWriter,
    tmp_path: Path,
) -> None:
    selected = tmp_path / "sidecar-link.db"
    shutil.copy2(live_writer.database_path, selected)
    shutil.copy2(f"{live_writer.database_path}-wal", f"{selected}-wal")
    Path(f"{selected}-shm").symlink_to(f"{live_writer.database_path}-shm")

    with pytest.raises(
        TrustedLiveUnsupportedSourceError,
        match="SHM|symbolic link|symlink",
    ):
        TrustedLiveReader.open(selected)


def test_open_reader_rejects_hardlinked_shm_without_mutating_its_alias(
    live_writer: _QcodesWalWriter,
    tmp_path: Path,
) -> None:
    live_writer.request("exit_without_sqlite_cleanup")
    live_writer.process.join(10)
    assert live_writer.process.exitcode == 0
    shm_path = Path(f"{live_writer.database_path}-shm")
    unrelated_alias = tmp_path / "unrelated-shm-alias.bin"
    shutil.copy2(shm_path, unrelated_alias)
    shm_path.unlink()
    os.link(unrelated_alias, shm_path)
    assert shm_path.stat().st_nlink >= 2
    before = _artifact_state(live_writer.database_path)

    with pytest.raises(
        TrustedLiveUnsupportedSourceError,
        match="SHM|hard.?link|unsupported",
    ):
        TrustedLiveReader.open(live_writer.database_path)

    assert _artifact_state(live_writer.database_path) == before


def test_rejection_before_native_session_configuration_is_reusable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qplot.datahandling import trusted_live as trusted_live_module

    database_path = tmp_path / "preconfiguration-rejection.db"
    initialise_or_create_database_at(database_path, journal_mode="DELETE")

    with monkeypatch.context() as patch:
        patch.setattr(
            trusted_live_module,
            "_sqlite_uri_path",
            lambda _path: "relative-native-temp",
        )
        with pytest.raises(
            TrustedLiveUnsupportedSourceError,
            match="unsupported|source|platform",
        ):
            TrustedLiveReader.open(database_path)

    with TrustedLiveReader.open(database_path) as reader:
        assert reader.query("SELECT count(*) FROM runs").rows == ((0,),)


@pytest.mark.skipif(os.name != "nt", reason="NTFS alternate streams are Windows-only")
def test_open_reader_rejects_ntfs_alternate_data_stream(tmp_path: Path) -> None:
    database_path = tmp_path / "ordinary.db"
    initialise_or_create_database_at(database_path, journal_mode="DELETE")
    host_path = tmp_path / "stream-host.bin"
    host_path.write_bytes(b"unrelated host data")
    stream_path = Path(f"{host_path}:database")
    shutil.copyfile(database_path, stream_path)
    before = stream_path.read_bytes()

    with pytest.raises(
        TrustedLiveUnsupportedSourceError,
        match="alternate|stream|path|unsupported",
    ):
        TrustedLiveReader.open(stream_path)

    assert stream_path.read_bytes() == before
    with TrustedLiveReader.open(database_path) as reader:
        assert reader.query("SELECT count(*) FROM runs").rows == ((0,),)


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO fixture")
def test_open_reader_rejects_nonregular_main(tmp_path: Path) -> None:
    fifo_main = tmp_path / "fifo.db"
    os.mkfifo(fifo_main)
    with pytest.raises(TrustedLiveUnsupportedSourceError, match="regular file"):
        TrustedLiveReader.open(fifo_main)


@pytest.mark.parametrize("artifact", ["wal", "shm"])
def test_open_reader_rejects_nonregular_sidecar(
    live_writer: _QcodesWalWriter,
    tmp_path: Path,
    artifact: str,
) -> None:
    selected = tmp_path / f"directory-{artifact}.db"
    shutil.copy2(live_writer.database_path, selected)
    for candidate in ("wal", "shm"):
        target = Path(f"{selected}-{candidate}")
        if candidate == artifact:
            target.mkdir()
        else:
            shutil.copy2(f"{live_writer.database_path}-{candidate}", target)
    with pytest.raises(
        TrustedLiveUnsupportedSourceError,
        match=f"{artifact.upper()}|regular file",
    ):
        TrustedLiveReader.open(selected)

    # Every partial-open proof/session state must have unwound.
    with TrustedLiveReader.open(live_writer.database_path) as reader:
        assert reader.query("SELECT 1").rows == ((1,),)


def test_expected_instance_rejects_replacement_before_native_open(
    tmp_path: Path,
) -> None:
    selected = tmp_path / "selected-rollback.db"
    replacement = tmp_path / "replacement-rollback.db"
    initialise_or_create_database_at(selected, journal_mode="DELETE")
    expected = database_instance(selected)
    initialise_or_create_database_at(replacement, journal_mode="DELETE")
    os.replace(replacement, selected)

    with pytest.raises(TrustedLiveSourceChangedError, match="approved|differs"):
        TrustedLiveReader.open(
            selected,
            expected_database_instance=expected,
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission fixture")
def test_native_proof_permission_failure_is_source_io(tmp_path: Path) -> None:
    database_path = tmp_path / "permission.db"
    initialise_or_create_database_at(database_path, journal_mode="DELETE")
    original_mode = stat.S_IMODE(database_path.stat().st_mode)

    def remove_read_permission(
        _token: Any,
        _temporary_directory: Path,
    ) -> None:
        database_path.chmod(0)

    try:
        with pytest.raises(TrustedLiveSourceIOError, match="I/O|read|source"):
            TrustedLiveReader.open(
                database_path,
                _test_pre_open_callback=remove_read_permission,
            )
    finally:
        database_path.chmod(original_mode)

    with TrustedLiveReader.open(database_path) as reader:
        assert reader.query("SELECT count(*) FROM runs").rows == ((0,),)


@pytest.mark.parametrize("artifact", ["wal", "shm"])
@pytest.mark.skipif(os.name == "nt", reason="POSIX permission fixture")
def test_native_sidecar_permission_failure_is_source_io(
    live_writer: _QcodesWalWriter,
    artifact: str,
) -> None:
    live_writer.request("barrier")
    source = Path(f"{live_writer.database_path}-{artifact}")
    original_mode = stat.S_IMODE(source.stat().st_mode)
    source.chmod(0)
    try:
        with pytest.raises(TrustedLiveSourceIOError, match="I/O|read|source"):
            TrustedLiveReader.open(live_writer.database_path)
    finally:
        source.chmod(original_mode)

    with TrustedLiveReader.open(live_writer.database_path) as reader:
        assert _probe_rows(reader) == ((0, "initial", 0),)


@pytest.mark.parametrize("artifact", ["wal", "shm"])
def test_expected_existing_sidecar_rejects_replacement_before_native_open(
    live_writer: _QcodesWalWriter,
    tmp_path: Path,
    artifact: str,
) -> None:
    live_writer.request("exit_without_sqlite_cleanup")
    live_writer.process.join(10)
    assert live_writer.process.exitcode == 0
    source = Path(f"{live_writer.database_path}-{artifact}")
    original = tmp_path / f"expected-{artifact}-a"
    replacement = tmp_path / f"expected-{artifact}-b"
    os.link(source, original)
    shutil.copyfile(source, replacement)

    def replace_after_capture(
        _token: Any,
        _temporary_directory: Path,
    ) -> None:
        os.replace(replacement, source)

    try:
        with pytest.raises(TrustedLiveSourceChangedError, match="identity|changed"):
            TrustedLiveReader.open(
                live_writer.database_path,
                _test_pre_open_callback=replace_after_capture,
            )
    finally:
        os.replace(original, source)

    # A rejected partial open must leave the native singleton and proofs reusable.
    with TrustedLiveReader.open(live_writer.database_path) as reader:
        assert _probe_rows(reader) == ((0, "initial", 0),)


def test_mutating_and_unsafe_sql_is_rejected_without_source_changes(
    live_writer: _QcodesWalWriter,
) -> None:
    live_writer.request("barrier")
    before = _artifact_state(live_writer.database_path)
    rejected_statements = (
        "INSERT INTO qplot_trusted_probe(value) VALUES('forbidden')",
        "UPDATE qplot_trusted_probe SET value='forbidden'",
        "DELETE FROM qplot_trusted_probe",
        "CREATE TABLE forbidden(value)",
        "DROP TABLE qplot_trusted_probe",
        "ALTER TABLE qplot_trusted_probe ADD COLUMN forbidden",
        "PRAGMA journal_mode=DELETE",
        "PRAGMA wal_checkpoint(TRUNCATE)",
        "PRAGMA writable_schema=ON",
        "ATTACH DATABASE ':memory:' AS other",
        "VACUUM",
        "ANALYZE",
        "REINDEX",
        "BEGIN",
        "SELECT 1; SELECT 2",
        "EXPLAIN SELECT 1",
        "SELECT load_extension('forbidden')",
    )

    reader = TrustedLiveReader.open(live_writer.database_path)
    try:
        for statement in rejected_statements:
            with pytest.raises(TrustedLiveSqlRejectedError):
                reader.query(statement)
            assert _probe_rows(reader) == ((0, "initial", 0),)
        assert reader.query("PRAGMA data_version").columns == ("data_version",)
        _assert_safe_audit(reader.audit().counters)
    finally:
        reader.close()

    _assert_safe_audit(reader.audit().counters)
    _assert_protected_artifacts_unchanged(
        before,
        _artifact_state(live_writer.database_path),
    )


def test_ordinary_query_failure_is_not_relabelled_as_policy_or_source_change(
    live_writer: _QcodesWalWriter,
) -> None:
    with TrustedLiveReader.open(live_writer.database_path) as reader:
        with pytest.raises(TrustedLiveQueryError, match="query|table|SQL"):
            reader.query("SELECT value FROM table_that_does_not_exist")
        with pytest.raises(TrustedLiveSqlRejectedError):
            reader.query("DELETE FROM qplot_trusted_probe")
        assert reader.query("SELECT 1").rows == ((1,),)


def test_corrupt_database_has_distinct_invalid_database_error(tmp_path: Path) -> None:
    database_path = tmp_path / "corrupt.db"
    initialise_or_create_database_at(database_path, journal_mode="DELETE")
    with database_path.open("r+b", buffering=0) as database_file:
        database_file.seek(100)
        database_file.write(b"\xff" * 512)

    with pytest.raises(TrustedLiveInvalidDatabaseError):
        TrustedLiveReader.open(database_path)


def test_retained_malformed_wal_is_unsupported(
    live_writer: _QcodesWalWriter,
) -> None:
    live_writer.request("exit_without_sqlite_cleanup")
    live_writer.process.join(10)
    assert live_writer.process.exitcode == 0
    wal_path = Path(f"{live_writer.database_path}-wal")
    assert wal_path.stat().st_size >= 32
    with wal_path.open("r+b", buffering=0) as wal_file:
        wal_file.seek(0)
        wal_file.write(b"not a SQLite WAL header".ljust(32, b"!"))
    before = _artifact_state(live_writer.database_path)

    with pytest.raises(
        TrustedLiveUnsupportedSourceError,
        match="wal|sidecar|unsupported",
    ):
        TrustedLiveReader.open(live_writer.database_path)

    _assert_protected_artifacts_unchanged(
        before,
        _artifact_state(live_writer.database_path),
    )


def test_retained_malformed_rollback_journal_is_unsupported(tmp_path: Path) -> None:
    database_path = tmp_path / "malformed-journal.db"
    initialise_or_create_database_at(database_path, journal_mode="DELETE")
    journal_path = Path(f"{database_path}-journal")
    journal_path.write_bytes(b"not a SQLite rollback journal")

    with pytest.raises(
        TrustedLiveUnsupportedSourceError,
        match="sidecar-free|journal",
    ):
        TrustedLiveReader.open(database_path)


def test_rollback_journal_appearing_after_capture_is_unsupported(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "appearing-journal.db"
    initialise_or_create_database_at(database_path, journal_mode="DELETE")
    journal_path = Path(f"{database_path}-journal")

    def publish_journal_after_capture(
        _token: Any,
        _temporary_directory: Path,
    ) -> None:
        journal_path.write_bytes(b"retained after identity capture")

    try:
        with pytest.raises(
            TrustedLiveUnsupportedSourceError,
            match="journal.*appeared|ambiguous",
        ):
            TrustedLiveReader.open(
                database_path,
                _test_pre_open_callback=publish_journal_after_capture,
            )
    finally:
        journal_path.unlink(missing_ok=True)

    # The policy rejection occurs before native xOpen and must leave the
    # process singleton reusable.
    with TrustedLiveReader.open(database_path) as reader:
        assert reader.query("SELECT count(*) FROM runs").rows == ((0,),)


def test_rollback_journal_appearing_between_operations_invalidates_reader(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "operation-journal.db"
    initialise_or_create_database_at(database_path, journal_mode="DELETE")
    journal_path = Path(f"{database_path}-journal")
    reader = TrustedLiveReader.open(database_path)
    journal_contents = b"retained between trusted-reader operations"

    try:
        assert reader.query("SELECT count(*) FROM runs").rows == ((0,),)
        journal_path.write_bytes(journal_contents)

        with pytest.raises(
            TrustedLiveUnsupportedSourceError,
            match="journal|source-handle validation",
        ):
            reader.query("SELECT 1")
        assert journal_path.read_bytes() == journal_contents
    finally:
        reader.close()
        journal_path.unlink(missing_ok=True)

    # Removing the unsupported sidecar permits an explicit fresh session.
    with TrustedLiveReader.open(database_path) as reopened:
        assert reopened.query("SELECT count(*) FROM runs").rows == ((0,),)


def test_busy_wait_is_bounded_and_connection_recovers(tmp_path: Path) -> None:
    database_path = tmp_path / "busy.db"
    initialise_or_create_database_at(database_path, journal_mode="DELETE")
    before = _artifact_state(database_path)

    with TrustedLiveReader.open(
        database_path,
        busy_timeout_ms=75,
        operation_timeout_seconds=2.0,
    ) as reader:
        writer = _RollbackExclusiveWriter.start(database_path)
        started = time.monotonic()
        try:
            with pytest.raises(TrustedLiveBusyTimeoutError):
                reader.query("SELECT count(*) FROM runs")
        finally:
            writer.close()
        assert time.monotonic() - started < 1.0
        assert reader.query("SELECT count(*) FROM runs").rows == ((0,),)

    assert _artifact_state(database_path) == before


@pytest.mark.parametrize(
    "interruption_kind",
    ["deadline", "cancel-event", "cross-thread-interrupt"],
)
def test_long_query_interruption_rolls_back_and_releases_locks(
    live_writer: _QcodesWalWriter,
    interruption_kind: str,
) -> None:
    with TrustedLiveReader.open(live_writer.database_path) as reader:
        controls: dict[str, Any] = {}
        interrupter: threading.Thread | None = None
        interrupt_results: list[bool] = []
        if interruption_kind == "deadline":
            controls["deadline"] = time.monotonic() + 0.03
            expected_error = TrustedLiveDeadlineExceededError
        elif interruption_kind == "cancel-event":
            cancel_event = threading.Event()
            controls["cancel_event"] = cancel_event

            def cancel_later() -> None:
                time.sleep(0.03)
                cancel_event.set()

            interrupter = threading.Thread(target=cancel_later)
            expected_error = TrustedLiveCancelledError
        else:

            def interrupt_later() -> None:
                time.sleep(0.03)
                interrupt_results.append(reader.interrupt())

            interrupter = threading.Thread(target=interrupt_later)
            expected_error = TrustedLiveCancelledError

        if interrupter is not None:
            interrupter.start()
        started = time.monotonic()
        with pytest.raises(expected_error):
            reader.query(
                "WITH RECURSIVE values_(n) AS ("
                "SELECT 1 UNION ALL SELECT n + 1 FROM values_ "
                "WHERE n < 100000000) SELECT sum(n) FROM values_",
                **controls,
            )
        assert time.monotonic() - started < 1.0
        if interrupter is not None:
            interrupter.join(10)
            assert not interrupter.is_alive()
        if interruption_kind == "cross-thread-interrupt":
            assert interrupt_results == [True]

        assert reader.query("SELECT count(*) FROM runs").rows == ((1,),)
        assert live_writer.request("checkpoint", mode="TRUNCATE") == (0, 0, 0)


def test_sparse_large_database_is_read_directly_without_a_full_size_copy(
    tmp_path: Path,
) -> None:
    # Keep the native no-copy integration fixture physically modest on every
    # platform.  Logical 32-GiB arithmetic is covered by stat/payload proxies in
    # test_trusted_live_queries.py, without creating a 32-GiB filesystem path.
    sparse_size = 64 * 1024**2
    writer = _QcodesWalWriter.start(
        tmp_path / "sparse.db",
        sparse_size=sparse_size,
    )
    try:
        source_status = _audited_stat(writer.database_path)
        assert source_status is not None
        assert source_status.st_size >= sparse_size
        allocated_blocks = getattr(source_status, "st_blocks", 0)
        if allocated_blocks:
            assert allocated_blocks * 512 < source_status.st_size // 2

        reader = TrustedLiveReader.open(writer.database_path)
        temporary_directory = reader.temporary_directory
        assert temporary_directory is not None
        assert temporary_directory.is_dir()
        try:
            startup_counters = reader.audit().counters
            assert startup_counters["source_read_bytes"] < min(
                sparse_size // 4,
                16 * 1024**2,
            )
            assert reader.query(
                "SELECT count(*), max(seq) FROM qplot_trusted_probe"
            ).rows == ((2, 1),)
            counters = reader.audit().counters
            _assert_safe_audit(counters)
            assert counters["source_read_bytes"] < min(
                sparse_size // 4,
                16 * 1024**2,
            )
            assert counters["temp_write_bytes"] >= 0
            assert counters["temp_write_bytes"] < 1024 * 1024
            temporary_files = [
                path for path in temporary_directory.rglob("*") if path.is_file()
            ]
            assert sum(path.stat().st_size for path in temporary_files) < 1024 * 1024
            assert all(
                path.stat().st_size < sparse_size // 16 for path in temporary_files
            )
            observed_source = _audited_stat(writer.database_path)
            assert observed_source is not None
            assert observed_source.st_size == source_status.st_size
            assert not any(
                path.name.startswith("sparse.db")
                for path in tmp_path.iterdir()
                if path != writer.database_path
                and path.name
                not in {"sparse.db-wal", "sparse.db-shm", "sparse.db-journal"}
            )
        finally:
            reader.close()
        _assert_safe_audit(reader.audit().counters)
        assert not temporary_directory.exists()
    finally:
        writer.close()


def test_owner_thread_cleanup_and_closed_state(
    live_writer: _QcodesWalWriter,
) -> None:
    reader = TrustedLiveReader.open(live_writer.database_path)
    temporary_directory = reader.temporary_directory
    assert temporary_directory is not None and temporary_directory.is_dir()
    outcomes: queue.Queue[type[BaseException] | None] = queue.Queue()

    def use_from_wrong_thread() -> None:
        for operation in (
            lambda: reader.query("SELECT 1"),
            reader.audit,
            reader.close,
        ):
            try:
                operation()
            except BaseException as error:
                outcomes.put(type(error))
            else:
                outcomes.put(None)

    thread = threading.Thread(target=use_from_wrong_thread)
    thread.start()
    thread.join(10)
    assert not thread.is_alive()
    assert [outcomes.get_nowait() for _ in range(3)] == [
        TrustedLiveReaderThreadError,
        TrustedLiveReaderThreadError,
        TrustedLiveReaderThreadError,
    ]
    assert not reader.closed
    assert reader.query("SELECT 1").rows == ((1,),)

    reader.close()
    reader.close()

    assert reader.closed
    assert not temporary_directory.exists()
    assert live_writer.request("checkpoint", mode="TRUNCATE") == (0, 0, 0)
    with pytest.raises(TrustedLiveReaderClosedError):
        reader.query("SELECT 1")
    _assert_safe_audit(reader.audit().counters)


@pytest.mark.parametrize(
    ("fault", "failure_counter"),
    [
        pytest.param(
            "proof_close",
            "proof_close_error",
            marks=pytest.mark.skipif(
                os.name == "nt",
                reason=(
                    "Windows path inspection closes proof handles before the "
                    "deterministic final-close phase"
                ),
            ),
        ),
        ("shm_unmap", "shm_unmap_error"),
        ("base_close", "base_close_error"),
    ],
)
def test_native_cleanup_fault_is_reported_audited_and_quarantined(
    live_writer: _QcodesWalWriter,
    fault: str,
    failure_counter: str,
) -> None:
    live_writer.request("barrier")
    outcome = _exercise_cleanup_fault(live_writer.database_path, fault)

    close_error = outcome["close_error"]
    assert close_error is not None
    assert close_error[0] == TrustedLiveCleanupError.__name__
    audit = outcome["audit"]
    assert set(audit) == _AUDIT_KEYS
    assert audit[failure_counter] == 1
    if fault == "proof_close":
        assert audit["proof_active"] > 0
    reuse_error = outcome["reuse_error"]
    assert reuse_error is not None
    assert reuse_error[0] == TrustedLiveReaderUnavailableError.__name__
    assert "cleanup failure quarantined this process" in reuse_error[1]


@pytest.mark.parametrize("failure_phase", ["open", "operation"])
def test_forced_cleanup_uncertainty_is_chained_and_quarantined(
    tmp_path: Path,
    failure_phase: str,
) -> None:
    database_path = tmp_path / f"{failure_phase}-cleanup.db"
    initialise_or_create_database_at(database_path, journal_mode="DELETE")

    outcome = _exercise_forced_cleanup_fault(database_path, failure_phase)
    first_error = outcome["first_error"]
    assert first_error is not None
    assert first_error[0] == TrustedLiveCleanupError.__name__
    assert "cleanup could not be proved" in first_error[1]
    assert "quarantined" in first_error[1]
    assert first_error[2] == TrustedLiveSourceIOError.__name__
    assert (
        f"simulated {failure_phase} failure after the native main handle opened"
        in (first_error[3])
    )

    reuse_error = outcome["reuse_error"]
    assert reuse_error is not None
    assert reuse_error[0] == TrustedLiveReaderUnavailableError.__name__
    assert (
        "prior trusted-reader cleanup failure quarantined this process"
        in (reuse_error[1])
    )
    assert "Terminate the process" in reuse_error[1]


@pytest.mark.parametrize("fault", ["identity_handle", "header_descriptor"])
def test_preflight_close_uncertainty_quarantines_process_session(
    tmp_path: Path,
    fault: str,
) -> None:
    database_path = tmp_path / f"preflight-{fault}.db"
    initialise_or_create_database_at(database_path, journal_mode="DELETE")

    outcome = _exercise_preflight_cleanup_fault(database_path, fault)
    first_error = outcome["first_error"]
    assert first_error is not None
    assert first_error[0] == TrustedLiveCleanupError.__name__
    reuse_error = outcome["reuse_error"]
    assert reuse_error is not None
    assert reuse_error[0] == TrustedLiveReaderUnavailableError.__name__
    assert "cleanup failure quarantined this process" in reuse_error[1]


def test_process_session_is_exclusive_and_reusable_after_close(
    live_writer: _QcodesWalWriter,
) -> None:
    first = TrustedLiveReader.open(live_writer.database_path)
    try:
        with pytest.raises(
            TrustedLiveReaderUnavailableError,
            match="existing trusted reader",
        ):
            TrustedLiveReader.open(live_writer.database_path)
        assert first.query("SELECT 1").rows == ((1,),)
    finally:
        first.close()

    with TrustedLiveReader.open(live_writer.database_path) as replacement:
        assert _probe_rows(replacement) == ((0, "initial", 0),)


@pytest.mark.skipif(os.name == "nt", reason="POSIX fcntl main-lock proof")
def test_rejected_second_reader_does_not_drop_first_reader_main_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "exclusive-rollback.db"
    initialise_or_create_database_at(database_path, journal_mode="DELETE")
    context = multiprocessing.get_context("spawn")
    parent_control, child_control = context.Pipe(duplex=True)
    probe_now = context.Event()
    process = context.Process(
        target=_posix_exclusive_lock_probe_process,
        args=(
            str(database_path),
            _SQLITE_UNIX_SHARED_FIRST_BYTE,
            _SQLITE_UNIX_SHARED_BYTE_COUNT,
            child_control,
            probe_now,
        ),
        name="qplot-test-deferred-exclusive-lock-probe",
    )
    process.start()
    child_control.close()
    try:
        assert parent_control.poll(60), "Deferred DMS lock probe did not start"
        kind, payload = parent_control.recv()
        assert kind == "ready", f"Deferred DMS lock probe failed:\n{payload}"

        with TrustedLiveReader.open(database_path) as first:
            outcomes: list[bool] = []
            original_progress_handler = first._progress_handler

            def reject_second_and_probe_lock(control: Any) -> bool:
                abort_requested = original_progress_handler(control)
                if abort_requested or outcomes:
                    return abort_requested
                with pytest.raises(
                    TrustedLiveReaderUnavailableError,
                    match="existing trusted reader",
                ):
                    TrustedLiveReader.open(database_path)
                probe_now.set()
                assert parent_control.poll(30), "Deferred DMS lock probe timed out"
                probe_kind, probe_payload = parent_control.recv()
                assert probe_kind == "ok", (
                    f"Deferred DMS lock probe failed:\n{probe_payload}"
                )
                outcomes.append(bool(probe_payload))
                return original_progress_handler(control)

            monkeypatch.setattr(
                first,
                "_progress_handler",
                reject_second_and_probe_lock,
            )
            result = first.query(
                "WITH RECURSIVE values_(n, run_count) AS ("
                "SELECT 1, (SELECT count(*) FROM runs) "
                "UNION ALL SELECT n + 1, run_count FROM values_ WHERE n < 100000"
                ") SELECT max(run_count), sum(n) FROM values_",
                timeout=45.0,
            )
            assert result.rows[0][0] == 0
            assert outcomes == [False]
    finally:
        probe_now.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(10)
        parent_control.close()
    assert process.exitcode == 0


def test_abandoned_reader_finalizer_releases_session_and_temp_directory(
    live_writer: _QcodesWalWriter,
) -> None:
    reader = TrustedLiveReader.open(live_writer.database_path)
    assert reader.query("SELECT 1").rows == ((1,),)
    temporary_directory = reader.temporary_directory
    assert temporary_directory is not None and temporary_directory.is_dir()
    reader_reference = weakref.ref(reader)

    del reader
    gc.collect()

    assert reader_reference() is None
    assert not temporary_directory.exists()
    with TrustedLiveReader.open(live_writer.database_path) as replacement:
        assert _probe_rows(replacement) == ((0, "initial", 0),)


def test_sidecar_free_qcodes_rollback_database_is_readable(tmp_path: Path) -> None:
    database_path = tmp_path / "rollback.db"
    initialise_or_create_database_at(database_path, journal_mode="DELETE")
    assert all(
        not Path(f"{database_path}{suffix}").exists()
        for suffix in ("-wal", "-shm", "-journal")
    )
    before = _artifact_state(database_path)

    with TrustedLiveReader.open(database_path) as reader:
        assert reader.source_identity.journal_mode == "rollback"
        assert reader.query("SELECT count(*) FROM runs").rows == ((0,),)

    assert _artifact_state(database_path) == before
