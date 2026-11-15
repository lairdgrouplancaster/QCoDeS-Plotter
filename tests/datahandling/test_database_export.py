"""Standalone backup visibility and source preservation."""

import hashlib
import multiprocessing
import sqlite3
import tempfile
import time
from contextlib import closing
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest

from qplot.datahandling import database_export
from qplot.datahandling.database_export import export_database_copy
from qplot.datahandling.file_identity import database_instance
from tests.support.datahandling.database_export import blocked_backup, exiting_backup


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture
def export_scratch(tmp_path, monkeypatch):
    folder = tmp_path / "private-exports"
    folder.mkdir()
    monkeypatch.setattr(database_export, "tempfile", SimpleNamespace(
        TemporaryDirectory=partial(tempfile.TemporaryDirectory, dir=folder),
    ))
    return folder


@pytest.mark.parametrize("live", [False, True])
def test_export_standalone_preserves_source(tmp_path, live):
    path = tmp_path / "source.db"
    writer = sqlite3.connect(path)
    writer.execute("CREATE TABLE runs(run_id INTEGER PRIMARY KEY, value BLOB)")
    writer.execute("INSERT INTO runs VALUES (196, ?)", (b"earlier",))
    writer.commit()
    if live:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("INSERT INTO runs VALUES (197, ?)", (b"newest" * 100000,))
    writer.commit()
    protected = [path] + ([Path(str(path) + "-wal")] if live else [])
    before = {str(p): (digest(p), p.stat().st_mtime_ns) for p in protected}
    stage = tmp_path / "stage.db"
    stage.touch()
    progress = []
    try:
        export_database_copy(
            stage, instance=database_instance(path), live=live,
            cancelled=lambda: False, progress=lambda *args: progress.append(args),
        )
        assert before == {str(p): (digest(p), p.stat().st_mtime_ns) for p in protected}
        with closing(sqlite3.connect(stage.as_uri() + "?mode=ro&immutable=1", uri=True)) as copy:
            assert copy.execute("SELECT run_id FROM runs").fetchall() == [(196,), (197,)]
            assert copy.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert copy.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        assert not Path(str(stage) + "-wal").exists()
        assert progress
    finally:
        writer.close()


def test_cancel_during_copy_does_not_write_stage(tmp_path):
    path = tmp_path / "source.db"
    with closing(sqlite3.connect(path)) as writer, writer:
        writer.execute("CREATE TABLE runs(run_id INTEGER PRIMARY KEY, value BLOB)")
        writer.execute("INSERT INTO runs VALUES (1, zeroblob(3000000))")
    stage = tmp_path / "stage.db"
    stage.touch()
    cancelled = False

    def progress(phase, *_args):
        nonlocal cancelled
        if phase == "Copying committed data":
            cancelled = True

    with pytest.raises(InterruptedError):
        export_database_copy(stage, instance=database_instance(path), live=True,
                             cancelled=lambda: cancelled, progress=progress)
    assert stage.stat().st_size == 0


def test_replaced_source_is_rejected(tmp_path):
    source = tmp_path / "source.db"
    with closing(sqlite3.connect(source)) as writer, writer:
        writer.execute("CREATE TABLE runs(run_id INTEGER)")
    instance = database_instance(source)
    source.rename(tmp_path / "old.db")
    source.write_bytes(b"different database")
    stage = tmp_path / "stage.db"
    stage.touch()
    with pytest.raises(RuntimeError, match="replaced"):
        export_database_copy(stage, instance=instance, live=True,
                             cancelled=lambda: False, progress=lambda *_: None)
    assert stage.read_bytes() == b""
    assert source.read_bytes() == b"different database"


@pytest.mark.parametrize("abort, error, message", [
    pytest.param("cancel", InterruptedError, "cancelled", id="cancel"),
    pytest.param("deadline", TimeoutError, "timed out", id="total-deadline"),
    pytest.param("no-progress", TimeoutError, "stopped responding", id="no-progress"),
])
def test_aborting_started_export_reaps_child_and_removes_private_files(
    tmp_path, monkeypatch, export_scratch, abort, error, message,
):
    source = tmp_path / "source.db"
    with closing(sqlite3.connect(source)) as writer, writer:
        writer.execute("CREATE TABLE runs(run_id INTEGER)")
    stage = tmp_path / "stage.db"
    stage.touch()
    before = source.read_bytes()
    clock = [time.monotonic()]
    cancelled = False
    observed_pids = set()
    prior_pids = {child.pid for child in multiprocessing.active_children()}
    monkeypatch.setattr(database_export, "_backup_child", blocked_backup)
    monkeypatch.setattr(database_export, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def progress(phase, *_args):
        nonlocal cancelled
        if phase != "Copying committed data":
            return
        children = [child for child in multiprocessing.active_children()
                    if child.pid not in prior_pids and child.name == "qplot-database-export"]
        assert len(children) == 1
        assert children[0].is_alive()
        observed_pids.add(children[0].pid)
        _assert_private_export_files(export_scratch)
        cancelled = abort == "cancel"
        if abort in ("deadline", "no-progress"):
            clock[0] += 61

    with pytest.raises(error, match=message):
        export_database_copy(stage, instance=database_instance(source), live=True,
                             cancelled=lambda: cancelled, progress=progress,
                             timeout=60 if abort == "deadline" else 1800)
    assert observed_pids
    assert not observed_pids & {child.pid for child in multiprocessing.active_children()}
    assert not list(export_scratch.iterdir())
    assert stage.read_bytes() == b""
    assert source.read_bytes() == before


def _assert_private_export_files(export_scratch):
    files = list(export_scratch.glob("*/copy.db*"))
    assert {path.name for path in files} == {
        "copy.db", "copy.db-wal", "copy.db-shm", "copy.db-journal",
    }
    assert all(path.read_bytes() == b"private export scratch" for path in files)


@pytest.mark.parametrize("outcome, message", [
    pytest.param("missing-result", "stopped unexpectedly", id="missing-result"),
    pytest.param("unsuccessful-exit", "did not close cleanly", id="unsuccessful-exit"),
])
def test_incomplete_export_reaps_child_and_never_copies_its_output(
    tmp_path, monkeypatch, export_scratch, outcome, message,
):
    source = tmp_path / "source.db"
    with closing(sqlite3.connect(source)) as writer, writer:
        writer.execute("CREATE TABLE runs(run_id INTEGER)")
    before = source.read_bytes()
    stage = tmp_path / "stage.db"
    stage.touch()
    released = multiprocessing.get_context("spawn").Event()
    prior_pids = {child.pid for child in multiprocessing.active_children()}
    observed_pids = set()
    monkeypatch.setattr(database_export, "_backup_child", partial(
        exiting_backup, released=released, outcome=outcome,
    ))

    def progress(phase, *_args):
        if phase != "Copying committed data":
            return
        children = [child for child in multiprocessing.active_children()
                    if child.pid not in prior_pids and child.name == "qplot-database-export"]
        assert len(children) == 1
        assert children[0].is_alive()
        observed_pids.add(children[0].pid)
        _assert_private_export_files(export_scratch)
        released.set()

    with pytest.raises(RuntimeError, match=message):
        export_database_copy(stage, instance=database_instance(source), live=True,
                             cancelled=lambda: False, progress=progress)

    assert observed_pids
    assert not observed_pids & {child.pid for child in multiprocessing.active_children()}
    assert not list(export_scratch.iterdir())
    assert stage.read_bytes() == b""
    assert source.read_bytes() == before


def test_existing_staging_contents_are_not_overwritten(tmp_path):
    source = tmp_path / "source.db"
    with closing(sqlite3.connect(source)) as writer, writer:
        writer.execute("CREATE TABLE runs(run_id INTEGER)")
    before = source.read_bytes()
    with pytest.raises(ValueError, match="private staging"):
        export_database_copy(source, instance=database_instance(source), live=False,
                             cancelled=lambda: False, progress=lambda *_: None)
    assert source.read_bytes() == before


def _measurement_database(tmp_path):
    from qplot.testdata import (
        AxisSpecification,
        MeasurementSpecification,
        RunSpecification,
        generate_database,
    )

    source = tmp_path / "measurement.db"
    generate_database((RunSpecification((MeasurementSpecification(
        "current", "Current", "A", (AxisSpecification("voltage", "Voltage", "V", 0, 1, 5),),
    ),)),), source)
    return source


def test_real_qcodes_copy_preserves_metadata_and_measurements(tmp_path):
    from qplot.datahandling.readonly import load_by_id_read_only

    source = _measurement_database(tmp_path)
    target = tmp_path / "stage.db"
    target.touch()
    export_database_copy(target, instance=database_instance(source), live=True,
                         cancelled=lambda: False, progress=lambda *_: None)
    dumps = []
    for path in (source, target):
        connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            dumps.append(list(connection.iterdump()))
        finally:
            connection.close()
    assert dumps[0] == dumps[1]
    dataset = load_by_id_read_only(1, database_path=str(target))
    try:
        assert dataset.get_parameter_data()["current"]["current"].size == 5
    finally:
        dataset.conn.close()


@pytest.mark.parametrize("live", [False, True], ids=["snapshot", "live"])
def test_missing_selected_run_leaves_source_and_stage_unchanged(tmp_path, export_scratch, live):
    source = _measurement_database(tmp_path)
    before = source.read_bytes()
    stage = tmp_path / "selected.db"
    stage.touch()

    with pytest.raises(RuntimeError, match="selected runs are no longer present"):
        export_database_copy(stage, instance=database_instance(source), live=live,
                             cancelled=lambda: False, progress=lambda *_: None, run_ids=[1, 999])

    assert stage.read_bytes() == b""
    assert source.read_bytes() == before
    assert not list(export_scratch.iterdir())


def test_external_results_are_not_misrepresented_as_standalone(tmp_path):
    source = tmp_path / "source.db"
    with closing(sqlite3.connect(source)) as writer, writer:
        writer.execute("CREATE TABLE runs(run_id INTEGER, raw_data_db_path TEXT)")
        writer.execute("INSERT INTO runs VALUES (1, 'unavailable-external.db')")
    stage = tmp_path / "stage.db"
    stage.touch()
    with pytest.raises(RuntimeError, match="separate files"):
        export_database_copy(stage, instance=database_instance(source), live=True,
                             cancelled=lambda: False, progress=lambda *_: None)
    assert stage.read_bytes() == b""


def test_writer_can_commit_during_export(tmp_path):
    source = tmp_path / "live.db"
    writer = sqlite3.connect(source, timeout=1)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE runs(run_id INTEGER PRIMARY KEY, value BLOB)")
        writer.execute("INSERT INTO runs VALUES (197, zeroblob(8000000))")
        writer.commit()
        committed = False

        def progress(phase, completed, _total):
            nonlocal committed
            if phase == "Copying committed data" and completed > 0 and not committed:
                writer.executemany("INSERT INTO runs VALUES (?, 'new')", [(198,), (199,)])
                writer.commit()
                committed = True

        target = tmp_path / "stage.db"
        target.touch()
        export_database_copy(target, instance=database_instance(source), live=True,
                             cancelled=lambda: False, progress=progress)
        assert committed
        with closing(sqlite3.connect(target.as_uri() + "?mode=ro&immutable=1", uri=True)) as copy:
            assert copy.execute("SELECT run_id FROM runs ORDER BY run_id").fetchall() in (
                [(197,)], [(197,), (198,), (199,)],
            )
            assert copy.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        writer.close()

@pytest.mark.parametrize("live", [False, True])
def test_selected_runs_reopen_in_qcodes(tmp_path, live):
    from qcodes.dataset import (
        Measurement,
        initialise_or_create_database_at,
        load_by_id,
        load_or_create_experiment,
    )
    from qcodes.dataset.sqlite.database import connect
    path = tmp_path / "qcodes.db"
    initialise_or_create_database_at(str(path), journal_mode="WAL" if live else "DELETE")
    with closing(connect(str(path))) as writer:
        runs = []
        for i in range(3):
            experiment = load_or_create_experiment(f"experiment-{i}", "sample", conn=writer)
            measurement = Measurement(exp=experiment)
            measurement.register_custom_parameter("x")
            measurement.register_custom_parameter("y", setpoints=("x",))
            with measurement.run(write_in_background=False) as saver:
                saver.add_result(("x", i), ("y", i + 10))
                saver.dataset.add_metadata("note", f"run-{i}")
                saver.dataset.add_snapshot('{"station": {}}', overwrite=True)
                runs.append((saver.dataset.run_id, saver.dataset.guid, saver.dataset.table_name))
        # An unfinished run must remain unfinished in the copy.
        writer.execute("UPDATE runs SET is_completed=0, completed_timestamp=NULL WHERE run_id=?", (runs[2][0],))
        writer.commit()
        protected = [path] + ([Path(str(path) + "-wal")] if live else [])
        before = {str(p): (digest(p), p.stat().st_mtime_ns) for p in protected}
        stage = tmp_path / "selected.db"
        stage.touch()
        export_database_copy(stage, instance=database_instance(path), live=live,
                             cancelled=lambda: False, progress=lambda *_: None,
                             run_ids=[runs[0][0], runs[2][0]])
        assert before == {str(p): (digest(p), p.stat().st_mtime_ns) for p in protected}
    with closing(connect(str(stage))) as exported:
        assert exported.execute("SELECT run_id FROM runs ORDER BY run_id").fetchall() == [(runs[0][0],), (runs[2][0],)]
        assert exported.execute("SELECT COUNT(*) FROM experiments").fetchone()[0] == 2
        assert exported.execute("SELECT name FROM sqlite_master WHERE name=?", (runs[1][2],)).fetchone() is None
        assert exported.execute("PRAGMA foreign_key_check").fetchall() == []
        for i in (0, 2):
            dataset = load_by_id(runs[i][0], conn=exported)
            assert dataset.guid == runs[i][1]
            assert dataset.metadata["note"] == f"run-{i}"
            assert dataset.snapshot == {"station": {}}
            assert dataset.get_parameter_data()["y"]["y"].tolist() == [i + 10]
            assert dataset.completed == (i == 0)


def test_empty_run_selection_rejected(tmp_path):
    with pytest.raises(ValueError, match="Select at least"):
        export_database_copy(tmp_path / "unused", instance=None, live=False,
                             cancelled=lambda: False, progress=lambda *_: None, run_ids=[])
