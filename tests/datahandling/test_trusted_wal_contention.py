"""WAL coordination must not turn live viewing into a competing writer."""

from __future__ import annotations

import ctypes
import multiprocessing
import sqlite3
import traceback

import apsw
import pytest

from qplot.datahandling.trusted_live import TrustedLiveReader
from tests.datahandling.test_trusted_live import _assert_safe_audit
from tests.datahandling.test_trusted_plot import make_run

pytestmark = pytest.mark.timeout(120)

_LATEST_RUNS = "SELECT run_id, snapshot FROM runs ORDER BY run_id DESC LIMIT 100"


def _poll_runs(path, stop, control):
    try:
        with TrustedLiveReader.open(path) as reader:
            reader.query(_LATEST_RUNS)
            control.send(("ready", None))
            queries = 0
            while not stop.is_set():
                reader.query(_LATEST_RUNS)
                queries += 1
                stop.wait(0.002)
            final = reader.query("SELECT COUNT(*), MAX(run_id) FROM runs").rows
            audit = dict(reader.audit().counters)
        control.send(("done", (queries, final, audit)))
    except BaseException:
        control.send(("error", traceback.format_exc()))
    finally:
        control.close()


def _deferred_writer(path, control):
    """One real SQLite writer, with the same read-before-write upgrade as QCoDeS."""
    try:
        with sqlite3.connect(path, timeout=0.1) as writer:
            control.send(("ready", None))
            while control.recv() == "write":
                try:
                    writer.execute("BEGIN")
                    run_id = writer.execute("SELECT MAX(run_id) FROM runs").fetchone()[0]
                    writer.execute("UPDATE runs SET snapshot='probe' WHERE run_id=?", (run_id,))
                    writer.commit()
                except sqlite3.OperationalError as error:
                    writer.rollback()
                    control.send(("busy", error.sqlite_errorcode))
                else:
                    control.send(("committed", None))
        writer.close()
    except BaseException:
        control.send(("error", traceback.format_exc()))
    finally:
        control.close()


def _receive(control):
    assert control.poll(30), "Child process did not reply"
    kind, payload = control.recv()
    assert kind != "error", payload
    return kind, payload


def _join(process):
    process.join(10)
    if process.is_alive():
        process.terminate()
        process.join(10)
        pytest.fail("Child process failed to stop")
    assert process.exitcode == 0


def _shm_callbacks(reader):
    # SQLITE_FCNTL_FILE_POINTER and sqlite3_io_methods are public SQLite ABI.
    # Exercise the installed native callbacks directly so the critical-section
    # assertion is deterministic, independent of scheduler speed or race luck.
    class IoMethods(ctypes.Structure):
        _fields_ = [("iVersion", ctypes.c_int)] + [
            (name, ctypes.c_void_p) for name in (
                "xClose", "xRead", "xWrite", "xTruncate", "xSync", "xFileSize",
                "xLock", "xUnlock", "xCheckReservedLock", "xFileControl",
                "xSectorSize", "xDeviceCharacteristics", "xShmMap", "xShmLock",
                "xShmBarrier",
            )
        ]

    pointer = ctypes.c_void_p()
    assert reader._connection.file_control(
        "main", apsw.SQLITE_FCNTL_FILE_POINTER, ctypes.addressof(pointer)
    )
    assert pointer.value
    methods = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(IoMethods))).contents.contents
    assert methods.iVersion >= 2
    lock = ctypes.CFUNCTYPE(
        ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int
    )(methods.xShmLock)
    barrier = ctypes.CFUNCTYPE(None, ctypes.c_void_p)(methods.xShmBarrier)
    return pointer, lock, barrier


def test_shm_critical_sections_keep_native_locks_without_filesystem_validation(tmp_path):
    path = tmp_path / "coordination.db"
    make_run(path)
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_deferred_writer, args=(str(path), child))
    process.start()
    child.close()
    try:
        assert _receive(parent) == ("ready", None)
        with TrustedLiveReader.open(path) as reader:
            reader.query(_LATEST_RUNS)
            pointer, lock, barrier = _shm_callbacks(reader)
            before = reader.audit().counters
            # Force SQLite's legitimate header-retry lock even on a machine
            # where no natural concurrent header race occurs during the test.
            assert lock(pointer, 0, 1, apsw.SQLITE_SHM_LOCK | apsw.SQLITE_SHM_EXCLUSIVE) == 0
            try:
                barrier(pointer)
                parent.send("write")
                assert _receive(parent) == ("busy", sqlite3.SQLITE_BUSY)
            finally:
                assert lock(pointer, 0, 1, apsw.SQLITE_SHM_UNLOCK | apsw.SQLITE_SHM_EXCLUSIVE) == 0
            after = reader.audit().counters
            # These used to perform path opens and identity syscalls between
            # the two header copies and while the WAL write lock was held.
            assert after["identity_verified"] == before["identity_verified"]
            assert after["proof_open"] == before["proof_open"]
            assert after["shm_write_lock_acquired"] == before["shm_write_lock_acquired"] + 1
            assert after["shm_write_lock_ns"] > before["shm_write_lock_ns"]
            parent.send("write")
            assert _receive(parent) == ("committed", None)
            assert reader.query(_LATEST_RUNS).rows[0][1] == "probe"
            _assert_safe_audit(reader.audit().counters)
    finally:
        parent.send("stop")
        _join(process)
        parent.close()


def test_dummy_qcodes_sweeps_with_concurrent_trusted_reader(tmp_path, monkeypatch):
    from qcodes.dataset import (
        do1d,
        initialise_or_create_database_at,
        load_or_create_experiment,
    )
    from qcodes.instrument_drivers.mock_instruments import (
        DummyInstrument,
        DummyInstrumentWithMeasurement,
    )
    from qcodes.parameters import ManualParameter
    from qcodes.station import Station

    path = tmp_path / "acquisition.db"
    initialise_or_create_database_at(str(path), journal_mode="WAL")
    experiment = load_or_create_experiment("contention", sample_name="dummy")
    dac = DummyInstrument("contention_dac", gates=["ch1", "ch2"])
    dmm = DummyInstrumentWithMeasurement("contention_dmm", setter_instr=dac)
    station = Station(dac, dmm, default=False)
    monkeypatch.setattr(Station, "default", station)
    sample = ManualParameter("contention_sample")
    context = multiprocessing.get_context("spawn")
    stop = context.Event()
    parent, child = context.Pipe()
    process = context.Process(target=_poll_runs, args=(str(path), stop, child))
    started = False
    failures = []
    try:
        do1d(sample, 0, 1, 2, 0, dmm.v1, exp=experiment, do_plot=False)
        process.start()
        started = True
        child.close()
        assert _receive(parent) == ("ready", None)
        for index in range(300):
            try:
                dataset, _, _ = do1d(
                    sample, 0, 1, 8, 0.001, dmm.v1, dmm.v2,
                    exp=experiment, write_period=0.01, do_plot=False,
                )
            except Exception:
                failures.append((index, traceback.format_exc()))
            else:
                assert dataset.completed
                assert dataset.snapshot is not None
                data = dataset.get_parameter_data()
                assert data[dmm.v1.full_name][dmm.v1.full_name].size == 8
                assert data[dmm.v2.full_name][dmm.v2.full_name].size == 8
        stop.set()
        kind, payload = _receive(parent)
        assert kind == "done"
        queries, rows, audit = payload
        assert not failures, (len(failures), failures[:3])
        assert queries >= 10, "The reader did not exercise sustained concurrent polling"
        assert rows == ((301, 301),)
        _assert_safe_audit(audit)
        # No retry at the writer, no immutable reader, and no long read
        # transaction left behind to obstruct a writer-owned checkpoint.
        assert experiment.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (0, 0, 0)
    finally:
        stop.set()
        if started:
            _join(process)
        child.close()
        parent.close()
        experiment.conn.close()
        dmm.close()
        dac.close()
