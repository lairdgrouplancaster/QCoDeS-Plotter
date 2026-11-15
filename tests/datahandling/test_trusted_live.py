"""Black-box safety and live-visibility tests for the trusted reader."""

from __future__ import annotations

import gc
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
from dataclasses import dataclass
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

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
from tests._artifact_audit import (
    _path_stat_in_audit_process,
)
from tests.support.datahandling.trusted_live import (
    _AUDIT_KEYS as _AUDIT_KEYS,
)
from tests.support.datahandling.trusted_live import (
    _PROHIBITED_AUDIT_KEYS as _PROHIBITED_AUDIT_KEYS,
)
from tests.support.datahandling.trusted_live import (
    _artifact_state as _artifact_state,
)
from tests.support.datahandling.trusted_live import (
    _assert_protected_artifacts_unchanged as _assert_protected_artifacts_unchanged,
)
from tests.support.datahandling.trusted_live import (
    _assert_safe_audit as _assert_safe_audit,
)
from tests.support.datahandling.trusted_live import (
    _close_qcodes_run_handles as _close_qcodes_run_handles,
)
from tests.support.datahandling.trusted_live import (
    _qcodes_wal_writer_process as _qcodes_wal_writer_process,
)
from tests.support.datahandling.trusted_live import (
    _QcodesWalWriter as _QcodesWalWriter,
)
from tests.support.datahandling.trusted_live import (
    _stable_artifact_state as _stable_artifact_state,
)
from tests.support.datahandling.trusted_live import (
    _start_qcodes_run as _start_qcodes_run,
)
from tests.support.datahandling.trusted_live import (
    live_writer as live_writer,
)

pytestmark = pytest.mark.timeout(120)

_SQLITE_UNIX_DMS_BYTE = 128
_SQLITE_UNIX_SHARED_FIRST_BYTE = 0x40000002
_SQLITE_UNIX_SHARED_BYTE_COUNT = 510


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
        from qcodes.dataset.sqlite.connection import atomic
        from qcodes.dataset.sqlite.database import connect

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
                # This fixture deliberately injected native cleanup failure and already
                # recorded the outcome sent to the parent.
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

    def fail_after_native_open(_reader: TrustedLiveReader, **_options) -> None:
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
                # This fixture deliberately injected forced-cleanup failure and already
                # recorded the outcome sent to the parent.
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
        elif fault in {
            "header_descriptor", "header_keyboard_interrupt", "header_system_exit",
        }:
            real_header = trusted_live_module._database_header_journal_mode

            def fail_header_descriptor_close(path: Path) -> str:
                real_close = os.close

                def fail_close(file_descriptor: int) -> None:
                    if fault == "header_keyboard_interrupt":
                        raise KeyboardInterrupt("simulated interrupted header close")
                    if fault == "header_system_exit":
                        raise SystemExit("simulated interrupted header close")
                    real_close(file_descriptor)
                    raise OSError(5, "simulated checked header descriptor close failure")

                with patch.object(os, "close", side_effect=fail_close):
                    return real_header(path)

            trusted_live_module._database_header_journal_mode = (
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


def test_paused_writer_control_has_stable_artifacts_without_qplot_reader(
    live_writer: _QcodesWalWriter,
) -> None:
    before_commit = _artifact_state(live_writer.database_path)
    live_writer.request("commit", value="writer-only control")
    baseline = _stable_artifact_state(live_writer.database_path)
    assert baseline["-wal"] != before_commit["-wal"]
    for _ in range(3):
        _assert_protected_artifacts_unchanged(
            baseline, _artifact_state(live_writer.database_path),
        )


@pytest.mark.parametrize("timestamp_index", [8, 9], ids=["mtime", "ctime"])
def test_artifact_baseline_settles_metadata_but_reader_checks_reject_changes(
    monkeypatch: pytest.MonkeyPatch, timestamp_index: int,
) -> None:
    # Identical bytes and identity are insufficient: a delayed timestamp must
    # reset baseline settling, and the same change DURING reading must fail.
    original = ("digest", 1, 2, stat.S_IFREG | 0o600, 1, 0, 0, 32, 100, 200)
    changed = list(original)
    changed[timestamp_index] += 1
    before = {suffix: original for suffix in ("", "-wal", "-shm", "-journal")}
    after = {**before, "-wal": tuple(changed)}
    observations = iter((before, before, after, after, after))
    observed = []

    def observe(_path):
        state = next(observations)
        observed.append(state)
        return state

    monkeypatch.setattr(sys.modules[_stable_artifact_state.__module__], "_artifact_state", observe)
    assert _stable_artifact_state(
        Path("synthetic.db"), consecutive_observations=2, observation_interval=0,
    ) == after
    assert len(observed) == 5
    with pytest.raises(AssertionError, match="-wal"):
        _assert_protected_artifacts_unchanged(before, after)


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
    monkeypatch,
) -> None:
    with TrustedLiveReader.open(live_writer.database_path) as reader:
        commit_error: list[BaseException] = []
        first_select_finished = threading.Event()
        commit_finished = threading.Event()
        stop_commit = threading.Event()
        select_sql = "SELECT value FROM qplot_trusted_probe ORDER BY seq"
        original_query = reader._query_spec_in_transaction

        def query_with_commit_barrier(connection, query, *args):
            result = original_query(connection, query, *args)
            if query.sql == select_sql and not first_select_finished.is_set():
                # The first SELECT has materialized and pinned this batch's
                # snapshot. Complete the writer commit before the next SELECT.
                first_select_finished.set()
                assert commit_finished.wait(3), "Writer commit did not finish"
                assert commit_error == []
            return result

        monkeypatch.setattr(reader, "_query_spec_in_transaction", query_with_commit_barrier)

        def commit_during_batch() -> None:
            try:
                assert first_select_finished.wait(5), "First SELECT did not finish"
                if stop_commit.is_set():
                    return
                live_writer.request("commit", timeout=2, value="new-commit")
            except BaseException as error:
                commit_error.append(error)
            finally:
                commit_finished.set()

        commit_thread = threading.Thread(target=commit_during_batch)
        commit_thread.start()
        try:
            results = reader.query_batch(
                (
                    TrustedQuery(select_sql),
                    # The barrier above proves that the writer has committed.
                    # Check the pinned snapshot without a CPU-dependent delay.
                    TrustedQuery("SELECT count(*) FROM qplot_trusted_probe"),
                    TrustedQuery(select_sql),
                ),
                timeout=4.0,
            )
        finally:
            stop_commit.set()
            first_select_finished.set()
            commit_thread.join(10)
            assert not commit_thread.is_alive()
        assert commit_error == []
        assert results[0].rows == (("initial",),)
        assert results[1].rows == ((1,),)
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
    with TrustedLiveReader.open(live_writer.database_path) as reader:
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

    after_close = _stable_artifact_state(live_writer.database_path)
    _assert_protected_artifacts_unchanged(before_close, after_close)


@pytest.mark.skipif(os.name == "nt", reason="POSIX fcntl main-lock proof")
def test_posix_identity_validation_does_not_drop_active_main_database_lock(
    live_writer: _QcodesWalWriter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = TrustedLiveReader.open(live_writer.database_path)
    context = multiprocessing.get_context("spawn")
    parent_control, child_control = context.Pipe(duplex=True)
    probe_now = context.Event()
    probe_process = context.Process(
        target=_posix_exclusive_lock_probe_process,
        args=(
            str(live_writer.database_path),
            _SQLITE_UNIX_SHARED_FIRST_BYTE,
            _SQLITE_UNIX_SHARED_BYTE_COUNT,
            child_control,
            probe_now,
        ),
        name="qplot-test-active-main-lock-probe",
    )
    probe_process.start()
    child_control.close()
    try:
        live_writer.request("exit_without_sqlite_cleanup")
        live_writer.process.join(10)
        assert live_writer.process.exitcode == 0
        assert parent_control.poll(30), "Main lock probe did not start"
        kind, payload = parent_control.recv()
        assert kind == "ready", f"Main lock probe failed:\n{payload}"

        lock_results: list[bool] = []
        original_query = reader._query_spec_in_transaction

        def query_with_lock_probe(connection, query, *args):
            result = original_query(connection, query, *args)
            assert connection.in_transaction
            # Revalidate identities after a real SELECT has pinned the read
            # transaction, then probe before that transaction can end.
            reader._validate_native_source()
            probe_now.set()
            assert parent_control.poll(2), "Active main lock probe timed out"
            probe_kind, probe_payload = parent_control.recv()
            assert probe_kind == "ok", f"Main lock probe failed:\n{probe_payload}"
            lock_results.append(bool(probe_payload))
            return result

        monkeypatch.setattr(reader, "_query_spec_in_transaction", query_with_lock_probe)
        result = reader.query(
            "SELECT count(*) FROM runs",
            timeout=4.0,
        )
        assert result.rows == ((1,),)
        assert lock_results == [False]
    finally:
        probe_now.set()
        probe_process.join(10)
        if probe_process.is_alive():
            probe_process.terminate()
            probe_process.join(10)
        parent_control.close()
        reader.close()
    assert probe_process.exitcode == 0


def test_native_boundary_changes_only_allowed_shm_coordination_state(
    live_writer: _QcodesWalWriter,
) -> None:
    live_writer.request("barrier")
    before = _artifact_state(live_writer.database_path)
    assert before[""] is not None
    assert before["-wal"] is not None
    assert before["-shm"] is not None
    assert before["-journal"] is None

    with TrustedLiveReader.open(live_writer.database_path) as reader:
        assert _probe_rows(reader) == ((0, "initial", 0),)
        assert reader.data_version() > 0
        counters = reader.audit().counters
        _assert_safe_audit(counters)
        assert counters["shm_unmap_delete_forwarded"] == 0

    _assert_safe_audit(reader.audit().counters)
    assert reader.audit().counters["proof_active"] == 0
    assert (
        reader.audit().counters["proof_open"] == reader.audit().counters["proof_close"]
    )
    _assert_protected_artifacts_unchanged(
        before,
        _artifact_state(live_writer.database_path),
    )


def test_source_identity_keeps_native_sidecars_separate_from_ui_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qplot.datahandling import trusted_live as trusted_live_module
    from qplot.datahandling.file_identity import DatabaseInstance

    logical_path = os.path.abspath("ui-comparable-sidecars.db")
    resolved_path = os.path.realpath(logical_path)
    main_native_identity = ("windows-file-id", 17, 101)
    wal_native_identity = ("windows-file-id", 17, 102)
    shm_native_identity = ("windows-file-id", 17, 103)
    comparable_sidecars = frozenset({(7, 102), (7, 103)})

    monkeypatch.setattr(
        trusted_live_module,
        "_capture_database_instance_for_trusted_open",
        lambda _path: (
            DatabaseInstance(logical_path, resolved_path, (7, 101)),
            main_native_identity,
        ),
    )
    monkeypatch.setattr(
        trusted_live_module,
        "_database_header_journal_mode",
        lambda _path: "wal",
    )

    def native_sidecar_identity(path: Path, _description: str) -> Any:
        if os.fspath(path).endswith("-wal"):
            return wal_native_identity
        if os.fspath(path).endswith("-shm"):
            return shm_native_identity
        return None

    monkeypatch.setattr(
        trusted_live_module,
        "_optional_file_identity",
        native_sidecar_identity,
    )
    monkeypatch.setattr(
        trusted_live_module,
        "database_sidecar_identities",
        lambda logical, resolved: (
            comparable_sidecars
            if (logical, resolved) == (logical_path, resolved_path)
            else frozenset()
        ),
    )

    source, captured_main_native_identity = (
        trusted_live_module._capture_source_identity(logical_path)
    )

    assert captured_main_native_identity == main_native_identity
    assert source.wal_identity == wal_native_identity
    assert source.shm_identity == shm_native_identity
    assert source.journal_identity is None
    assert source.database_instance.identity == (7, 101)
    assert source.database_instance.sidecar_identities == comparable_sidecars


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
    from qcodes.dataset import initialise_or_create_database_at

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


@pytest.mark.parametrize("canonicalize", [False, True])
def test_native_open_rejects_parent_redirection_after_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    canonicalize: bool,
) -> None:
    import apsw
    from qcodes.dataset import initialise_or_create_database_at

    from qplot.datahandling import trusted_live

    selected_directory = tmp_path / "selected"
    moved_directory = tmp_path / "moved"
    selected_directory.mkdir()
    database_path = selected_directory / "source.db"
    initialise_or_create_database_at(database_path, journal_mode="DELETE")
    before = _artifact_state(database_path)

    def redirect_parent() -> None:
        selected_directory.rename(moved_directory)
        if os.name == "nt":
            subprocess.run(
                [
                    "cmd", "/d", "/c", "mklink", "/J",
                    str(selected_directory), str(moved_directory),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        else:
            selected_directory.symlink_to(moved_directory, target_is_directory=True)

    class RedirectingVFS(apsw.VFS):
        def xFullPathname(self, name: str) -> str:
            full_path = super().xFullPathname(name)
            # POSIX SQLite resolves symlinks here. Exercise that behavior on
            # Windows too, and redirect only after resolution, before xOpen.
            if canonicalize:
                full_path = str(Path(full_path).resolve())
            assert Path(full_path) == database_path
            redirect_parent()
            return full_path

    native_vfs_name = trusted_live.TRUSTED_READER_VFS_NAME
    wrapper: apsw.VFS | None = None

    def install_wrapper(_token: Any, _temporary_directory: Path) -> None:
        nonlocal wrapper
        # The callback runs after the native VFS has been registered. Inherit
        # its I/O unchanged; intercept only SQLite's pathname resolution.
        wrapper_name = "qplot-test-parent-redirection"
        wrapper = RedirectingVFS(wrapper_name, native_vfs_name)
        monkeypatch.setattr(trusted_live, "TRUSTED_READER_VFS_NAME", wrapper_name)

    try:
        with pytest.raises(TrustedLiveUnsupportedSourceError):
            with TrustedLiveReader.open(
                database_path,
                _test_pre_open_callback=install_wrapper,
            ):
                pass
        assert _artifact_state(moved_directory / "source.db") == before
    finally:
        monkeypatch.setattr(trusted_live, "TRUSTED_READER_VFS_NAME", native_vfs_name)
        if wrapper is not None:
            wrapper.unregister()
        if moved_directory.exists():
            if selected_directory.exists():
                if os.name == "nt":
                    selected_directory.rmdir()
                else:
                    selected_directory.unlink()
            moved_directory.rename(selected_directory)

    with TrustedLiveReader.open(database_path) as reader:
        assert reader.query("SELECT count(*) FROM runs").rows == ((0,),)


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
    from qcodes.dataset import initialise_or_create_database_at

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
    from qcodes.dataset import initialise_or_create_database_at

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
    from qcodes.dataset import initialise_or_create_database_at

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
    from qcodes.dataset import initialise_or_create_database_at

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

    with TrustedLiveReader.open(live_writer.database_path) as reader:
        for statement in rejected_statements:
            with pytest.raises(TrustedLiveSqlRejectedError):
                reader.query(statement)
            assert _probe_rows(reader) == ((0, "initial", 0),)
        assert reader.query("PRAGMA data_version").columns == ("data_version",)
        _assert_safe_audit(reader.audit().counters)

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
    from qcodes.dataset import initialise_or_create_database_at

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
    from qcodes.dataset import initialise_or_create_database_at

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
    from qcodes.dataset import initialise_or_create_database_at

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
    from qcodes.dataset import initialise_or_create_database_at

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
    from qcodes.dataset import initialise_or_create_database_at

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

        with TrustedLiveReader.open(writer.database_path) as reader:
            temporary_directory = reader.temporary_directory
            assert temporary_directory is not None
            assert temporary_directory.is_dir()
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
    from qcodes.dataset import initialise_or_create_database_at

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


@pytest.mark.parametrize("fault", [
    "identity_handle", "header_descriptor", "header_keyboard_interrupt", "header_system_exit",
])
def test_preflight_close_uncertainty_quarantines_process_session(
    tmp_path: Path,
    fault: str,
) -> None:
    from qcodes.dataset import initialise_or_create_database_at

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
    with TrustedLiveReader.open(live_writer.database_path) as first:
        with pytest.raises(
            TrustedLiveReaderUnavailableError,
            match="existing trusted reader",
        ):
            TrustedLiveReader.open(live_writer.database_path)
        assert first.query("SELECT 1").rows == ((1,),)

    with TrustedLiveReader.open(live_writer.database_path) as replacement:
        assert _probe_rows(replacement) == ((0, "initial", 0),)


@pytest.mark.skipif(os.name == "nt", reason="POSIX fcntl main-lock proof")
def test_rejected_second_reader_does_not_drop_first_reader_main_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qcodes.dataset import initialise_or_create_database_at

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
    from qcodes.dataset import initialise_or_create_database_at

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
