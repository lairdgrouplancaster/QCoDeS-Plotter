import hashlib
import sqlite3
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.datahandling import readonly
from qplot.datahandling.qcodes_cache import update_cache_parameter_data
from qplot.datahandling.trusted_live_service import (
    TrustedLiveReadService,
    TrustedReadRequestCancelledError,
)
from qplot.tools.worker import loader


def make_run(path, *, arrays=False, dimensions=2):
    initialise_or_create_database_at(str(path), journal_mode="WAL")
    experiment = load_or_create_experiment("plots", sample_name="disposable")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("x")
    if dimensions == 2:
        measurement.register_custom_parameter("y", paramtype="array" if arrays else "numeric")
    measurement.register_custom_parameter(
        "z", setpoints=("x", "y") if dimensions == 2 else ("x",),
        paramtype="array" if arrays else "numeric",
    )
    with measurement.run() as saver:
        for x in range(3):
            if arrays:
                values = [("x", x), ("z", np.arange(4, dtype=np.int64) + 10 * x)]
                if dimensions == 2:
                    values.append(("y", np.arange(4, dtype=np.int64)))
                saver.add_result(*values)
            else:
                for y in range(4 if dimensions == 2 else 1):
                    values = [("x", x), ("z", 10 * x + y)]
                    if dimensions == 2:
                        values.append(("y", y))
                    saver.add_result(*values)
        dataset = saver.dataset
        identity = dataset.run_id, dataset.guid, dataset.table_name
    dataset.conn.close()
    experiment.conn.close()
    return identity


def prohibit_snapshots(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Trusted plotting attempted a whole-database snapshot")
    monkeypatch.setattr(readonly, "_copy_file_cooperatively", forbidden)
    import qplot.tools.worker as worker
    monkeypatch.setattr(worker, "qcodes_read_only_connection", forbidden)
    monkeypatch.setattr(worker, "sqlite_read_only_connection", forbidden)


def work(dataset, *, limit=2_000_000):
    param = dataset.paramspecs["z"]
    axes = {"x": "y", "y": "x"} if len(param.depends_on_) == 2 else {"x": "x", "y": "z"}
    worker = loader(dataset.cache, param, dataset.paramspecs, axes,
                    max_full_heatmap_points=limit)
    worker.setAutoDelete(False)
    errors, finished = [], []
    worker.emitter.errorOccurred.connect(errors.append)
    worker.emitter.finished.connect(finished.append)
    worker.run()
    assert not errors, errors
    assert finished == [True]
    if worker.read_data:
        assert update_cache_parameter_data(
            dataset.cache, "z", worker.updated_read_status, worker.updated_write_status,
            worker.cache_data, dataset_completed=worker.dataset_completed,
        )
    return worker


def protected_state(path):
    result = {}
    for suffix in ("", "-wal", "-journal"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            stat = candidate.stat()
            result[suffix] = (stat.st_size, stat.st_mtime_ns,
                              hashlib.sha256(candidate.read_bytes()).hexdigest())
    return result


@pytest.mark.parametrize("finish", ["subscriber_cancel", "deadline", "close"])
def test_suspended_capture_lifecycle_and_shared_subscribers(tmp_path, monkeypatch, finish):
    from qplot.datahandling import trusted_plot
    from qplot.datahandling.trusted_live_service import (
        TrustedReadRequestDeadlineError,
        TrustedReadServiceClosedError,
    )

    path = tmp_path / "suspended.db"
    _, guid, _ = make_run(path, arrays=True)
    before = protected_state(path)
    prohibit_snapshots(monkeypatch)
    original = trusted_plot.plot_prefix
    original_init = trusted_plot.PlotPrefix.__init__
    reached, release = threading.Event(), threading.Event()
    spools = []
    closed_on = []

    def record_spool(self):
        original_init(self)
        spools.append(self.path)

    def suspend(executor, dataset):
        steps = original(executor, dataset)
        try:
            next(steps)  # Suspend inside an array record's private BLOB writer.
            reached.set()
            while not release.is_set():
                executor.check_cancelled()
                time.sleep(0.002)
                yield
            return (yield from steps)
        finally:
            steps.close()
            closed_on.append(threading.get_ident())

    monkeypatch.setattr(trusted_plot.PlotPrefix, "__init__", record_spool)
    monkeypatch.setattr(trusted_plot, "plot_prefix", suspend)
    service = TrustedLiveReadService(path)
    try:
        dataset = service.submit_plot_dataset(guid).wait()
        request = service.submit_plot_prefix(
            dataset, deadline=time.monotonic() + 0.5 if finish == "deadline" else None,
        )
        assert reached.wait(5)
        if finish == "subscriber_cancel":
            second = service.submit_plot_prefix(dataset)
            assert request._state.operation_id == second._state.operation_id
            assert second.progress is request.progress
            cancelled_progress = request.progress
            assert request.cancel()
            with pytest.raises(TrustedReadRequestCancelledError):
                request.wait()
            assert not second.done
            release.set()
            assert second.wait().row_count == 3
            assert request.progress is cancelled_progress
            assert second.progress.phase == "Validating plot data"
            assert len(spools) == 1
        elif finish == "deadline":
            with pytest.raises(TrustedReadRequestDeadlineError):
                request.wait(5)
        else:
            service.close_async()
            with pytest.raises(TrustedReadServiceClosedError):
                request.wait(5)
    finally:
        release.set()
        service.close()
    assert closed_on == [service._dispatcher_thread.ident]
    if finish != "subscriber_cancel":
        assert all(not spool.exists() for spool in spools)
    assert protected_state(path) == before


@pytest.mark.parametrize("arrays,dimensions,limit", [
    (False, 2, 2_000_000), (True, 2, 2_000_000),
    (False, 2, 4), (True, 2, 4), (False, 1, 2_000_000),
])
def test_trusted_worker_data_and_source_protection(tmp_path, monkeypatch, arrays, dimensions, limit):
    path = tmp_path / "source.db"
    _, guid, _ = make_run(path, arrays=arrays, dimensions=dimensions)
    # Retain a writer so the WAL exists throughout the reader session.
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE unrelated(payload)")
    writer.execute("INSERT INTO unrelated VALUES (zeroblob(8000000))")
    writer.commit()
    before = protected_state(path)
    prohibit_snapshots(monkeypatch)
    service = TrustedLiveReadService(path)
    try:
        dataset = service.submit_plot_dataset(guid).wait()
        result = work(dataset, limit=limit)
        if dimensions == 2:
            np.testing.assert_array_equal(result.dataGrid, np.arange(3)[:, None] * 10 + np.arange(4))
        else:
            np.testing.assert_array_equal(result.axis_data["y"], [0, 10, 20])
        assert protected_state(path) == before
    finally:
        service.close()
        assert protected_state(path) == before
        writer.close()


def test_live_append_refresh_has_no_snapshot(tmp_path, monkeypatch):
    path = tmp_path / "live.db"
    run_id, guid, table = make_run(path)
    writer = sqlite3.connect(path)
    writer.execute("UPDATE runs SET is_completed=0 WHERE run_id=?", (run_id,))
    writer.commit()
    prohibit_snapshots(monkeypatch)
    service = TrustedLiveReadService(path)
    try:
        dataset = service.submit_plot_dataset(guid).wait()
        first = work(dataset)
        assert first.dataset_completed is False
        writer.executemany(f'INSERT INTO "{table}" (x,y,z) VALUES (?,?,?)',
                           [(3, y, 30 + y) for y in range(4)])
        writer.execute("UPDATE runs SET is_completed=1 WHERE run_id=?", (run_id,))
        writer.commit()
        before = protected_state(path)
        second = work(dataset)
        np.testing.assert_array_equal(second.dataGrid, np.arange(4)[:, None] * 10 + np.arange(4))
        assert second.dataset_completed is True
        assert protected_state(path) == before
    finally:
        service.close()
        writer.close()


@pytest.mark.parametrize("cancel", [False, True])
def test_capture_releases_reader_between_pages(tmp_path, monkeypatch, cancel):
    from qplot.datahandling import trusted_plot
    from qplot.datahandling.trusted_live_service import _BrokerQueryExecutor
    path = tmp_path / "concurrent.db"
    run_id, guid, table = make_run(path)
    writer = sqlite3.connect(path)
    writer.execute("UPDATE runs SET is_completed=0 WHERE run_id=?", (run_id,))
    writer.commit()
    monkeypatch.setattr(trusted_plot, "PAGE_ROWS", 2)
    prohibit_snapshots(monkeypatch)
    reached, resume = threading.Event(), threading.Event()
    original = _BrokerQueryExecutor.query

    def pause_after_page(self, sql, *args, **kwargs):
        result = original(self, sql, *args, **kwargs)
        if "WHERE id>? AND id<=?" in sql and not reached.is_set():
            reached.set()
            assert resume.wait(5)
        return result

    monkeypatch.setattr(_BrokerQueryExecutor, "query", pause_after_page)
    service = TrustedLiveReadService(path)
    try:
        dataset = service.submit_plot_dataset(guid).wait()
        request = service.submit_plot_prefix(dataset)
        assert reached.wait(5)
        # A completed pinned-reader page must not retain a WAL reader mark.
        assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
        writer.executemany(f'INSERT INTO "{table}" (x,y,z) VALUES (?,?,?)',
                           [(3, y, 30 + y) for y in range(4)])
        writer.commit()
        if cancel:
            assert request.cancel()
        resume.set()
        if cancel:
            with pytest.raises(TrustedReadRequestCancelledError):
                request.wait()
        else:
            prefix = request.wait()
            assert prefix.row_count == 12  # committed append is beyond the watermark
        second = service.submit_plot_prefix(dataset).wait()
        assert second.row_count == 16
    finally:
        resume.set()
        service.close()
        writer.close()


def test_large_blob_is_chunked_and_precision_is_preserved(tmp_path, monkeypatch):
    from io import BytesIO
    path = tmp_path / "arrays.db"
    _, guid, table = make_run(path, arrays=True)
    values = np.arange(600_000, dtype=np.int64) + 2**53
    stream = BytesIO()
    np.save(stream, values, allow_pickle=False)
    writer = sqlite3.connect(path)
    writer.execute(f'DELETE FROM "{table}"')
    writer.execute(f'INSERT INTO "{table}" (x,y,z) VALUES (?,?,?)',
                   (0, stream.getvalue(), stream.getvalue()))
    writer.commit()
    prohibit_snapshots(monkeypatch)
    service = TrustedLiveReadService(path)
    try:
        dataset = service.submit_plot_dataset(guid).wait()
        prefix = service.submit_plot_prefix(dataset).wait()
        with prefix.connect(decode_arrays=True) as connection:
            stored = connection.execute(f'SELECT z FROM "{table}"').fetchone()[0]
            np.testing.assert_array_equal(stored, values)
            assert stored.dtype == np.int64
        connection.close()
        assert prefix.path.stat().st_size < 12_000_000
    finally:
        service.close()
        writer.close()


def test_incremental_blob_reader_limits_and_native_protection(tmp_path):
    from qplot.datahandling.trusted_live import (
        TrustedLiveReader,
        TrustedLiveSqlRejectedError,
    )
    from tests.datahandling.test_trusted_live import _assert_safe_audit
    path = tmp_path / "blob.db"
    with sqlite3.connect(path) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE blobs(payload BLOB)")
        writer.execute("INSERT INTO blobs VALUES (zeroblob(5000000))")
    before = protected_state(path)
    with TrustedLiveReader.open(path) as reader:
        assert reader.query("SELECT qplot_read_blob('blobs','payload',1,4999990,10)").rows == ((bytes(10),),)
        for offset, length in [(-1, 1), (0, -1), (0, 262145), (0.5, 1)]:
            with pytest.raises(TrustedLiveSqlRejectedError):
                reader.query("SELECT qplot_read_blob('blobs','payload',1,?,?)", (offset, length))
        assert reader.query("SELECT 1").rows == ((1,),)
        _assert_safe_audit(reader.audit().counters)
    assert protected_state(path) == before
    writer.close()


def test_accepted_trusted_failure_never_falls_back(tmp_path, monkeypatch):
    from qplot.datahandling import trusted_plot
    from qplot.datahandling.trusted_live import TrustedLiveReaderUnavailableError
    path = tmp_path / "failure.db"
    _, guid, _ = make_run(path)
    prohibit_snapshots(monkeypatch)
    service = TrustedLiveReadService(path)
    try:
        dataset = service.submit_plot_dataset(guid).wait()
        def fail(*_args):
            raise TrustedLiveReaderUnavailableError("accepted session failure")
        monkeypatch.setattr(trusted_plot, "plot_prefix", fail)
        worker = loader(dataset.cache, dataset.paramspecs["z"], dataset.paramspecs,
                        {"x": "y", "y": "x"})
        errors, finished = [], []
        worker.emitter.errorOccurred.connect(errors.append)
        worker.emitter.finished.connect(finished.append)
        worker.run()
        assert finished == [False]
        assert len(errors) == 1 and isinstance(errors[0], TrustedLiveReaderUnavailableError)
        assert not hasattr(worker, "dataGrid")
    finally:
        service.close()


def test_plot_worker_cancel_discards_partial_prefix(tmp_path, monkeypatch):
    from PyQt6 import QtCore

    from qplot.datahandling.trusted_live_service import _BrokerQueryExecutor
    path = tmp_path / "cancel.db"
    _, guid, _ = make_run(path)
    before = protected_state(path)
    prohibit_snapshots(monkeypatch)
    reached, resume = threading.Event(), threading.Event()
    original = _BrokerQueryExecutor.query
    def pause_after_page(self, sql, *args, **kwargs):
        result = original(self, sql, *args, **kwargs)
        if "WHERE id>? AND id<=?" in sql:
            reached.set()
            assert resume.wait(5)
        return result
    monkeypatch.setattr(_BrokerQueryExecutor, "query", pause_after_page)
    service = TrustedLiveReadService(path)
    thread = None
    try:
        dataset = service.submit_plot_dataset(guid).wait()
        worker = loader(dataset.cache, dataset.paramspecs["z"], dataset.paramspecs,
                        {"x": "y", "y": "x"})
        finished = []
        worker.emitter.finished.connect(finished.append, QtCore.Qt.ConnectionType.DirectConnection)
        thread = threading.Thread(target=worker.run)
        thread.start()
        assert reached.wait(5)
        worker.cancel()
        resume.set()
        thread.join(5)
        assert not thread.is_alive()
        assert finished == [False]
        assert not hasattr(worker, "cache_data")
        assert not hasattr(worker, "dataGrid")
        assert worker._trusted_prefix is None
    finally:
        resume.set()
        if thread is not None:
            thread.join(5)
        service.close()
    assert protected_state(path) == before


def test_huge_planned_line_is_rejected_before_cache_allocation(tmp_path, monkeypatch):
    from qcodes.dataset.descriptions.rundescriber import RunDescriber
    from qcodes.dataset.descriptions.versioning import serialization
    path = tmp_path / "planned.db"
    run_id, guid, _ = make_run(path, dimensions=1)
    with sqlite3.connect(path) as writer:
        text = writer.execute("SELECT run_description FROM runs WHERE run_id=?", (run_id,)).fetchone()[0]
        description = serialization.from_json_to_current(text)
        huge = RunDescriber(description.interdeps, shapes={"z": (100_000_000,)})
        writer.execute("UPDATE runs SET run_description=? WHERE run_id=?",
                       (serialization.to_json_for_storage(huge), run_id))
    writer.close()
    prohibit_snapshots(monkeypatch)
    service = TrustedLiveReadService(path)
    try:
        dataset = service.submit_plot_dataset(guid).wait()
        worker = loader(dataset.cache, dataset.paramspecs["z"], dataset.paramspecs,
                        {"x": "x", "y": "z"})
        errors, finished = [], []
        worker.emitter.errorOccurred.connect(errors.append)
        worker.emitter.finished.connect(finished.append)
        worker.run()
        assert finished == [False]
        assert len(errors) == 1 and "decoding budget" in str(errors[0])
        assert not hasattr(worker, "cache_data")
    finally:
        service.close()
