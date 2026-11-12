"""Standalone backup visibility and source preservation."""

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from qplot.datahandling.database_export import export_database_copy
from qplot.datahandling.file_identity import database_instance


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


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


def test_total_deadline_stops_child(tmp_path):
    source = tmp_path / "source.db"
    with closing(sqlite3.connect(source)) as writer, writer:
        writer.execute("CREATE TABLE runs(run_id INTEGER)")
    stage = tmp_path / "stage.db"
    stage.touch()
    with pytest.raises(TimeoutError):
        export_database_copy(stage, instance=database_instance(source), live=True,
                             cancelled=lambda: False, progress=lambda *_: None,
                             timeout=0.001)
    assert stage.read_bytes() == b""


def test_existing_staging_contents_are_not_overwritten(tmp_path):
    source = tmp_path / "source.db"
    with closing(sqlite3.connect(source)) as writer, writer:
        writer.execute("CREATE TABLE runs(run_id INTEGER)")
    before = source.read_bytes()
    with pytest.raises(ValueError, match="private staging"):
        export_database_copy(source, instance=database_instance(source), live=False,
                             cancelled=lambda: False, progress=lambda *_: None)
    assert source.read_bytes() == before


def test_real_qcodes_copy_preserves_metadata_and_measurements(tmp_path):
    from qplot.datahandling.readonly import load_by_id_read_only
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
