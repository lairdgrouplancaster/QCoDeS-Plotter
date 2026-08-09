import hashlib
import os
import sqlite3

import pytest
import qcodes
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.dataset.sqlite.connection import AtomicConnection
from qcodes.parameters import ManualParameter

from qplot._repair import repair
from qplot.datahandling.readonly import (
    load_by_guid_read_only,
    load_by_id_read_only,
    qcodes_read_only_connection,
    set_qcodes_database_location,
    sqlite_read_only_connection,
)
from qplot.datahandling.readSQL import get_runs_via_sql


def _directory_state(directory):
    state = {}
    for path in directory.iterdir():
        stat = path.stat()
        checksum = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        state[path.name] = (path.is_file(), stat.st_size, stat.st_mtime_ns, checksum)
    return state


def _create_qcodes_run(database_path):
    initialise_or_create_database_at(database_path, journal_mode="DELETE")
    experiment = load_or_create_experiment("read_only", sample_name="reserved path")
    setpoint = ManualParameter("setpoint")
    signal = ManualParameter("signal")
    measurement = Measurement(exp=experiment, name="reserved_path_run")
    measurement.register_parameter(setpoint)
    measurement.register_parameter(signal, setpoints=(setpoint,))
    with measurement.run() as datasaver:
        datasaver.add_result((setpoint, 1.0), (signal, 2.0))
        dataset = datasaver.dataset
    guid = dataset.guid
    run_id = dataset.run_id
    dataset.conn.close()
    return run_id, guid


def test_sqlite_read_only_connection_rejects_writes(tmp_path):
    database_path = tmp_path / "plain.db"
    writable = sqlite3.connect(database_path)
    try:
        writable.execute("CREATE TABLE probe (value INTEGER)")
        writable.commit()
    finally:
        writable.close()

    conn = sqlite_read_only_connection(database_path)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO probe VALUES (1)")
    finally:
        conn.close()


def test_qcodes_read_only_connection_rejects_writes(tmp_path):
    database_path = tmp_path / "qcodes.db"
    original_database_path = qcodes.config.core.db_location
    try:
        initialise_or_create_database_at(str(database_path))
    finally:
        qcodes.config.core.db_location = original_database_path

    conn = qcodes_read_only_connection(database_path)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("CREATE TABLE qplot_read_only_probe (value INTEGER)")
    finally:
        conn.close()


@pytest.mark.parametrize(
    "database_name",
    [
        "experiment#scan.db",
        pytest.param(
            "experiment?scan.db",
            marks=pytest.mark.skipif(
                os.name == "nt",
                reason="Question marks are not valid in Windows filenames.",
            ),
        ),
        "experiment%scan.db",
        "experiment scan.db",
        "experiment-café.db",
    ],
)
def test_qcodes_reads_reserved_paths_without_changing_any_file(
    tmp_path,
    database_name,
):
    source_path = tmp_path / "source.db"
    database_path = tmp_path / database_name
    original_database_path = qcodes.config.core.db_location
    try:
        run_id, guid = _create_qcodes_run(source_path)
        source_path.rename(database_path)
        set_qcodes_database_location(database_path)
        original_state = _directory_state(tmp_path)

        runs = get_runs_via_sql(database_path)
        assert list(runs) == [run_id]
        assert runs[run_id]["guid"] == guid

        by_explicit_guid = load_by_guid_read_only(guid, database_path)
        try:
            assert by_explicit_guid.run_id == run_id
            assert by_explicit_guid.guid == guid
        finally:
            by_explicit_guid.conn.close()

        by_global_guid = load_by_guid_read_only(guid)
        try:
            assert by_global_guid.run_id == run_id
            assert by_global_guid.guid == guid
        finally:
            by_global_guid.conn.close()

        by_global_id = load_by_id_read_only(run_id)
        try:
            assert by_global_id.run_id == run_id
            assert by_global_id.guid == guid
        finally:
            by_global_id.conn.close()

        repair()

        conn = qcodes_read_only_connection(database_path)
        try:
            assert isinstance(conn, AtomicConnection)
            assert conn.path_to_dbfile == str(database_path.resolve())
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                conn.execute("CREATE TABLE qplot_read_only_probe (value INTEGER)")
        finally:
            conn.close()

        assert _directory_state(tmp_path) == original_state
    finally:
        qcodes.config.core.db_location = original_database_path
