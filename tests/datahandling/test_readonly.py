import hashlib
import json
import os
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import qcodes
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.dataset.data_set_in_memory import DataSetInMem
from qcodes.dataset.sqlite.connection import AtomicConnection
from qcodes.dataset.sqlite.db_upgrades import _latest_available_version
from qcodes.parameters import ManualParameter

from qplot import testdata as testdata_module
from qplot._repair import repair
from qplot.datahandling import file_identity as file_identity_module
from qplot.datahandling import readonly as readonly_module
from qplot.datahandling.database import database_access_error, database_info_rows
from qplot.datahandling.file_identity import database_file_identity
from qplot.datahandling.readonly import (
    DatabaseInstanceChangedError,
    ReadOnlyDatabaseAccessError,
    load_by_guid_read_only,
    load_by_id_read_only,
    qcodes_read_only_connection,
    quarantine_wal_for_replaced_database,
    replacement_wal_is_quarantined,
    set_qcodes_database_location,
    sqlite_read_only_connection,
)
from qplot.datahandling.readSQL import get_runs_via_sql
from qplot.testdata import RunSpecification, generate_database
from qplot.windows._dataset_handle import DatasetHandle

_ROLLBACK_RUN_COUNT = 26
_COMMITTED_RUN_NAMES = tuple(
    f"COMMITTED_{index:02d}" for index in range(_ROLLBACK_RUN_COUNT)
)
_UNCOMMITTED_RUN_NAMES = tuple(
    f"UNCOMMITTED_{index:02d}" for index in range(_ROLLBACK_RUN_COUNT)
)
_ROLLBACK_JOURNAL_MAGIC = b"\xd9\xd5\x05\xf9 \xa1c\xd7"
_SQLITE_ARTIFACT_SUFFIXES = ("", "-wal", "-shm", "-journal")


def test_windows_file_index_is_used_when_stat_inode_is_zero(monkeypatch):
    stat_result = SimpleNamespace(st_ino=0, st_dev=9)
    windows_identity = ("windows-file-id", 17, 23)
    monkeypatch.setattr(
        file_identity_module,
        "canonical_database_path",
        lambda _path: "C:/data/view.db",
    )
    monkeypatch.setattr(file_identity_module.os, "stat", lambda _path: stat_result)
    monkeypatch.setattr(file_identity_module.os, "name", "nt")
    monkeypatch.setattr(
        file_identity_module,
        "_windows_file_identity",
        lambda _path: windows_identity,
    )

    assert file_identity_module.database_file_identity("view.db") == windows_identity


def test_unavailable_identity_does_not_fall_back_to_mutable_metadata(monkeypatch):
    stat_result = SimpleNamespace(
        st_ino=0,
        st_dev=9,
        st_size=100,
        st_mtime_ns=200,
        st_ctime_ns=300,
    )
    monkeypatch.setattr(
        file_identity_module,
        "canonical_database_path",
        lambda _path: "/data/view.db",
    )
    monkeypatch.setattr(file_identity_module.os, "stat", lambda _path: stat_result)
    monkeypatch.setattr(file_identity_module.os, "name", "posix")

    assert file_identity_module.database_file_identity("view.db") is None


def test_wal_fallback_identity_change_marks_source_observation_unstable(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "fallback-identity.db"
    connection = sqlite3.connect(database_path)
    connection.close()
    wal_path = readonly_module._wal_path(database_path)
    wal_path.write_bytes(b"detached WAL placeholder")
    real_database_file_identity = readonly_module.database_file_identity
    wal_identities = iter(
        (
            ("windows-file-id", 17, 23),
            ("windows-file-id", 17, 24),
        )
    )

    def changing_wal_identity(path):
        if Path(path) == wal_path:
            return next(wal_identities)
        return real_database_file_identity(path)

    monkeypatch.setattr(
        readonly_module,
        "database_file_identity",
        changing_wal_identity,
    )

    signature = readonly_module._source_signature(database_path)

    assert signature.wal is not None
    assert signature.wal_identity == ("windows-file-id", 17, 24)
    assert not signature.stable


def test_wal_stat_signature_is_used_when_file_identity_is_unavailable(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "stat-fallback.db"
    connection = sqlite3.connect(database_path)
    connection.close()
    wal_path = readonly_module._wal_path(database_path)
    wal_path.write_bytes(b"detached WAL placeholder")
    real_database_file_identity = readonly_module.database_file_identity

    def unavailable_wal_identity(path):
        if Path(path) == wal_path:
            return None
        return real_database_file_identity(path)

    monkeypatch.setattr(
        readonly_module,
        "database_file_identity",
        unavailable_wal_identity,
    )

    signature = readonly_module._source_signature(database_path)

    assert signature.wal is not None
    assert signature.wal_identity is None
    assert signature.stable


def _directory_state(directory):
    state = {}
    for path in directory.iterdir():
        stat = path.stat()
        checksum = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        state[path.name] = (path.is_file(), stat.st_size, stat.st_mtime_ns, checksum)
    return state


def _run_without_source_changes(directory, operation):
    before = _directory_state(directory)
    result = operation()
    assert _directory_state(directory) == before
    return result


def _sqlite_artifact_state(database_path):
    """Capture exact source contents, identity, mtime, and sidecar presence."""
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


def _run_names_from_connection(opener, database_path):
    connection = opener(database_path)
    try:
        return tuple(
            row[0]
            for row in connection.execute(
                "SELECT name FROM runs ORDER BY run_id"
            ).fetchall()
        )
    finally:
        connection.close()


def _run_names_without_source_changes(opener, database_path):
    before = _sqlite_artifact_state(database_path)
    try:
        return _run_names_from_connection(opener, database_path)
    finally:
        assert _sqlite_artifact_state(database_path) == before


def _immutable_run_names(database_path):
    connection = sqlite3.connect(
        f"{Path(database_path).resolve().as_uri()}?mode=ro&immutable=1",
        uri=True,
    )
    try:
        return tuple(
            row[0]
            for row in connection.execute(
                "SELECT name FROM runs ORDER BY run_id"
            ).fetchall()
        )
    finally:
        connection.close()


@pytest.fixture(scope="module")
def latest_schema_rollback_template(tmp_path_factory):
    """Create one real latest-schema QCoDeS database for journal tests."""
    database_path = tmp_path_factory.mktemp("rollback-template") / "latest.db"
    original_database_path = qcodes.config.core.db_location
    experiment = None
    try:
        initialise_or_create_database_at(database_path, journal_mode="DELETE")
        experiment = load_or_create_experiment(
            "rollback_journal",
            sample_name="latest_schema",
        )
        setpoint = ManualParameter("rollback_setpoint")
        signal = ManualParameter("rollback_signal")
        for index, run_name in enumerate(_COMMITTED_RUN_NAMES):
            measurement = Measurement(exp=experiment, name=run_name)
            measurement.register_parameter(setpoint)
            measurement.register_parameter(signal, setpoints=(setpoint,))
            with measurement.run(write_in_background=False) as datasaver:
                datasaver.add_result(
                    (setpoint, float(index)),
                    (signal, float(index + 1)),
                )
    finally:
        if experiment is not None:
            experiment.conn.close()
        qcodes.config.core.db_location = original_database_path

    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone() == (
            _latest_available_version(),
        )
        connection.execute(
            "CREATE TABLE qplot_rollback_spill ("
            "sequence INTEGER PRIMARY KEY, payload BLOB)"
        )
        connection.commit()
    finally:
        connection.close()

    assert _immutable_run_names(database_path) == _COMMITTED_RUN_NAMES
    assert _sqlite_artifact_state(database_path)["-journal"] is None
    return database_path


def _copy_rollback_template(template_path, database_path):
    shutil.copyfile(template_path, database_path)
    assert _immutable_run_names(database_path) == _COMMITTED_RUN_NAMES


def _begin_spilled_run_name_update(database_path, journal_mode):
    """Leave all run-name pages spilled under an uncommitted transaction."""
    writer = sqlite3.connect(database_path)
    try:
        selected_mode = writer.execute(
            f"PRAGMA journal_mode = {journal_mode}"
        ).fetchone()[0]
        assert selected_mode == journal_mode.lower()
        writer.execute("PRAGMA synchronous = FULL")
        writer.execute("PRAGMA cache_size = 1")
        writer.execute("PRAGMA cache_spill = 1")
        writer.execute("BEGIN IMMEDIATE")
        writer.executemany(
            "UPDATE runs SET name = ? WHERE run_id = ?",
            (
                (run_name, run_id)
                for run_id, run_name in enumerate(
                    _UNCOMMITTED_RUN_NAMES,
                    start=1,
                )
            ),
        )
        writer.executemany(
            "INSERT INTO qplot_rollback_spill VALUES (?, ?)",
            ((index, b"x" * 8192) for index in range(64)),
        )

        journal_path = readonly_module._journal_path(database_path)
        journal_contents = journal_path.read_bytes()
        assert journal_contents.startswith(_ROLLBACK_JOURNAL_MAGIC)
        assert _immutable_run_names(database_path) == _UNCOMMITTED_RUN_NAMES
        return writer
    except Exception:
        writer.rollback()
        writer.close()
        raise


def _assert_clear_temporary_access_error(error):
    message = str(error).lower()
    assert "busy" in message or "temporar" in message or "changed continuously" in message


def _track_readonly_snapshot_directories(monkeypatch):
    real_temporary_directory = readonly_module.tempfile.TemporaryDirectory
    snapshot_directories = []

    def tracked_temporary_directory(*args, **kwargs):
        snapshot = real_temporary_directory(*args, **kwargs)
        snapshot_directories.append(Path(snapshot.name))
        return snapshot

    monkeypatch.setattr(
        readonly_module.tempfile,
        "TemporaryDirectory",
        tracked_temporary_directory,
    )
    return snapshot_directories


def _tracked_provisional_opener(
    monkeypatch,
    opener_kind,
    connection_state,
    *,
    fail_snapshot_attachment=False,
):
    """Return an opener whose provisional connection closure is observable."""
    records = []
    if opener_kind == "sqlite":

        class TrackingConnection(readonly_module._ManagedSQLiteConnection):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._qplot_test_record = SimpleNamespace(
                    connection=self,
                    close_count=0,
                )
                records.append(self._qplot_test_record)
                connection_state["opened"] = True

            def attach_snapshot(self, snapshot):
                if fail_snapshot_attachment:
                    raise RuntimeError("injected snapshot attachment failure")
                return super().attach_snapshot(snapshot)

            def close(self):
                self._qplot_test_record.close_count += 1
                return super().close()

        def open_sqlite(database_path):
            return sqlite_read_only_connection(
                database_path,
                factory=TrackingConnection,
            )

        return open_sqlite, records

    assert opener_kind == "qcodes"
    real_connect = readonly_module.connect

    def tracked_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        record = SimpleNamespace(connection=connection, close_count=0)
        original_close = connection.close

        def tracked_close():
            record.close_count += 1
            return original_close()

        connection.close = tracked_close
        records.append(record)
        connection_state["opened"] = True
        return connection

    monkeypatch.setattr(readonly_module, "connect", tracked_connect)
    if fail_snapshot_attachment:

        def fail_attachment(*_args, **_kwargs):
            raise RuntimeError("injected snapshot attachment failure")

        monkeypatch.setattr(
            readonly_module,
            "_attach_snapshot_cleanup",
            fail_attachment,
        )
    return qcodes_read_only_connection, records


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


def _create_default_wal_run(database_path):
    initialise_or_create_database_at(database_path)
    experiment = load_or_create_experiment("read_only_wal", sample_name="wal")
    setpoint = ManualParameter("wal_setpoint")
    signal = ManualParameter("wal_signal")
    measurement = Measurement(exp=experiment, name="wal_run")
    measurement.register_parameter(setpoint)
    measurement.register_parameter(signal, setpoints=(setpoint,))
    with measurement.run() as datasaver:
        datasaver.add_result((setpoint, 1.0), (signal, 2.0))
        dataset = datasaver.dataset
    run_id = dataset.run_id
    guid = dataset.guid
    table_name = dataset.table_name
    dataset.conn.close()
    experiment.conn.close()
    return run_id, guid, table_name


def _create_generated_wal_run(database_path):
    generate_database(
        [
            RunSpecification(
                1,
                "wal_signal",
                "WAL signal",
                "V",
                -1.0,
                1.0,
                2,
            )
        ],
        database_path,
    )
    connection = sqlite3.connect(database_path)
    try:
        return connection.execute(
            "SELECT run_id, guid, result_table_name FROM runs"
        ).fetchone()
    finally:
        connection.close()


def _install_test_generation_provenance(database_path):
    """Add the same lineage record used by qPlot's generated databases."""
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            f"PRAGMA application_id = "
            f"{file_identity_module.QPLOT_GENERATED_DATABASE_APPLICATION_ID}"
        )
        testdata_module._install_generation_provenance(connection)
        connection.commit()
    finally:
        connection.close()


def _open_active_wal(database_path):
    writer = testdata_module._connect_writable_exact_path(database_path)
    testdata_module.enable_generation_provenance_for_writer(writer)
    assert writer.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
    writer.execute("PRAGMA wal_autocheckpoint = 0")
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("CREATE TABLE qplot_wal_marker (value INTEGER)")
    writer.execute("INSERT INTO qplot_wal_marker VALUES (1)")
    writer.execute("UPDATE runs SET name = name")
    writer.commit()
    assert Path(f"{database_path}-wal").is_file()
    assert Path(f"{database_path}-shm").is_file()
    return writer


def _load_read_only(loader_kind, run_id, guid, database_path):
    if loader_kind == "guid":
        return load_by_guid_read_only(guid, database_path)
    if loader_kind == "run_id":
        return load_by_id_read_only(run_id, database_path)
    raise AssertionError(f"Unexpected loader kind: {loader_kind}")


def _track_explicit_qcodes_connections(monkeypatch):
    real_open = readonly_module.qcodes_read_only_connection
    records = []

    def tracked_open(*args, **kwargs):
        conn = real_open(*args, **kwargs)
        opened_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
        source_path = Path(conn.path_to_dbfile).resolve()
        snapshot_directory = (
            opened_path.parent if opened_path != source_path else None
        )
        record = SimpleNamespace(
            connection=conn,
            close_count=0,
            snapshot_directory=snapshot_directory,
        )
        original_close = conn.close

        def tracked_close():
            record.close_count += 1
            return original_close()

        conn.close = tracked_close
        records.append(record)
        return conn

    monkeypatch.setattr(
        readonly_module,
        "qcodes_read_only_connection",
        tracked_open,
    )
    return records


def _assert_connection_closed(conn):
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")


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


@pytest.mark.parametrize("loader_kind", ["guid", "run_id"])
def test_database_dataset_owns_connection_until_dataset_handle_closes(
    tmp_path,
    monkeypatch,
    loader_kind,
):
    database_path = tmp_path / "owned-active-wal.db"
    original_database_path = qcodes.config.core.db_location
    writer = None
    try:
        run_id, guid, _table_name = _create_generated_wal_run(database_path)
        writer = _open_active_wal(database_path)
        source_state = _directory_state(tmp_path)
        records = _track_explicit_qcodes_connections(monkeypatch)

        dataset = _load_read_only(loader_kind, run_id, guid, database_path)

        assert len(records) == 1
        record = records[0]
        assert dataset.conn is record.connection
        assert record.close_count == 0
        assert record.connection.execute("SELECT COUNT(*) FROM runs").fetchone() == (1,)
        assert record.snapshot_directory is not None
        assert record.snapshot_directory.is_dir()

        handle = DatasetHandle(dataset)
        assert handle.close()
        assert record.close_count == 1
        _assert_connection_closed(record.connection)
        assert not record.snapshot_directory.exists()
        assert _directory_state(tmp_path) == source_state
    finally:
        if writer is not None:
            writer.close()
        qcodes.config.core.db_location = original_database_path


@pytest.mark.parametrize("loader_kind", ["guid", "run_id"])
def test_missing_result_table_closes_non_owning_dataset_connection(
    tmp_path,
    monkeypatch,
    loader_kind,
):
    database_path = tmp_path / "missing-table-active-wal.db"
    original_database_path = qcodes.config.core.db_location
    writer = None
    try:
        run_id, guid, table_name = _create_generated_wal_run(database_path)
        writer = _open_active_wal(database_path)
        escaped_table_name = table_name.replace('"', '""')
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(f'DROP TABLE "{escaped_table_name}"')
        writer.commit()
        source_state = _directory_state(tmp_path)
        records = _track_explicit_qcodes_connections(monkeypatch)

        dataset = _load_read_only(loader_kind, run_id, guid, database_path)

        assert isinstance(dataset, DataSetInMem)
        assert len(records) == 1
        record = records[0]
        assert record.close_count == 1
        _assert_connection_closed(record.connection)
        assert record.snapshot_directory is not None
        assert not record.snapshot_directory.exists()
        assert _directory_state(tmp_path) == source_state
    finally:
        if writer is not None:
            writer.close()
        qcodes.config.core.db_location = original_database_path


@pytest.mark.parametrize("loader_kind", ["guid", "run_id"])
def test_exported_netcdf_closes_non_owning_dataset_connection(
    tmp_path,
    monkeypatch,
    loader_kind,
):
    pytest.importorskip("xarray")
    pytest.importorskip("h5netcdf")
    database_path = tmp_path / "exported.db"
    original_database_path = qcodes.config.core.db_location
    writable_dataset = None
    try:
        run_id, guid = _create_qcodes_run(database_path)
        writable_dataset = qcodes.dataset.load_by_id(run_id)
        writable_dataset.export(export_type="netcdf", path=tmp_path)
        writable_dataset.conn.close()
        writable_dataset = None
        monkeypatch.setattr(
            qcodes.config.dataset,
            "load_from_exported_file",
            True,
        )
        source_state = _directory_state(tmp_path)
        records = _track_explicit_qcodes_connections(monkeypatch)

        dataset = _load_read_only(loader_kind, run_id, guid, database_path)

        assert isinstance(dataset, DataSetInMem)
        assert dataset.guid == guid
        assert len(records) == 1
        record = records[0]
        assert record.close_count == 1
        _assert_connection_closed(record.connection)
        assert record.snapshot_directory is None
        assert _directory_state(tmp_path) == source_state
    finally:
        if writable_dataset is not None:
            writable_dataset.conn.close()
        qcodes.config.core.db_location = original_database_path


def test_repeated_in_memory_loads_release_connections_and_wal_snapshots(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "repeated-in-memory-active-wal.db"
    original_database_path = qcodes.config.core.db_location
    writer = None
    try:
        run_id, guid, table_name = _create_generated_wal_run(database_path)
        writer = _open_active_wal(database_path)
        escaped_table_name = table_name.replace('"', '""')
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(f'DROP TABLE "{escaped_table_name}"')
        writer.commit()
        source_state = _directory_state(tmp_path)
        records = _track_explicit_qcodes_connections(monkeypatch)

        for index in range(30):
            loader_kind = "guid" if index % 2 == 0 else "run_id"
            dataset = _load_read_only(loader_kind, run_id, guid, database_path)
            assert isinstance(dataset, DataSetInMem)
            assert len(records) == index + 1
            record = records[-1]
            assert record.close_count == 1
            _assert_connection_closed(record.connection)
            assert record.snapshot_directory is not None
            assert not record.snapshot_directory.exists()
            assert all(item.close_count == 1 for item in records)

        assert _directory_state(tmp_path) == source_state
    finally:
        if writer is not None:
            writer.close()
        qcodes.config.core.db_location = original_database_path


@pytest.mark.parametrize(
    ("loader_kind", "qcodes_loader_name"),
    [("guid", "load_by_guid"), ("run_id", "load_by_id")],
)
def test_loader_exception_closes_connection_and_wal_snapshot(
    tmp_path,
    monkeypatch,
    loader_kind,
    qcodes_loader_name,
):
    database_path = tmp_path / "loader-error-active-wal.db"
    original_database_path = qcodes.config.core.db_location
    writer = None
    try:
        run_id, guid, _table_name = _create_generated_wal_run(database_path)
        writer = _open_active_wal(database_path)
        source_state = _directory_state(tmp_path)
        records = _track_explicit_qcodes_connections(monkeypatch)

        def fail_loader(*_args, **_kwargs):
            raise RuntimeError("loader failed")

        monkeypatch.setattr(readonly_module, qcodes_loader_name, fail_loader)

        with pytest.raises(RuntimeError, match="loader failed"):
            _load_read_only(loader_kind, run_id, guid, database_path)

        assert len(records) == 1
        record = records[0]
        assert record.close_count == 1
        _assert_connection_closed(record.connection)
        assert record.snapshot_directory is not None
        assert not record.snapshot_directory.exists()
        assert _directory_state(tmp_path) == source_state
    finally:
        if writer is not None:
            writer.close()
        qcodes.config.core.db_location = original_database_path


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


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission test")
def test_completed_default_wal_database_opens_from_read_only_directory(
    tmp_path,
):
    database_path = tmp_path / "completed.db"
    original_database_path = qcodes.config.core.db_location
    original_directory_mode = tmp_path.stat().st_mode
    try:
        run_id, guid, _table_name = _create_default_wal_run(database_path)
        set_qcodes_database_location(database_path)

        assert database_path.read_bytes()[18:20] == b"\x02\x02"
        assert sorted(path.name for path in tmp_path.iterdir()) == ["completed.db"]

        database_path.chmod(0o444)
        tmp_path.chmod(0o555)
        expected_state = _directory_state(tmp_path)

        def direct_sqlite_read():
            conn = sqlite_read_only_connection(database_path)
            try:
                return conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            finally:
                conn.close()

        assert _run_without_source_changes(tmp_path, direct_sqlite_read) == 1

        def qcodes_sqlite_read():
            conn = qcodes_read_only_connection(database_path)
            try:
                assert isinstance(conn, AtomicConnection)
                assert conn.path_to_dbfile == str(database_path.resolve())
                return conn.execute("SELECT guid FROM runs").fetchone()[0]
            finally:
                conn.close()

        assert _run_without_source_changes(tmp_path, qcodes_sqlite_read) == guid

        runs = _run_without_source_changes(
            tmp_path,
            lambda: get_runs_via_sql(database_path),
            )
        assert list(runs) == [run_id]

        def load_dataset(loader_kind):
            dataset = _load_read_only(
                loader_kind,
                run_id,
                guid,
                database_path,
            )
            try:
                return dataset.run_id
            finally:
                dataset.conn.close()

        for loader_kind in ("guid", "run_id"):
            assert _run_without_source_changes(
                tmp_path,
                lambda loader_kind=loader_kind: load_dataset(loader_kind),
            ) == run_id
        assert _run_without_source_changes(tmp_path, repair) is None
        assert _run_without_source_changes(
            tmp_path,
            lambda: database_access_error(database_path),
            ) is None
        info_rows = _run_without_source_changes(
            tmp_path,
            lambda: database_info_rows(database_path),
            )
        assert any(label == "Runs" and value == "1" for label, value in info_rows)

        assert _directory_state(tmp_path) == expected_state
        assert sorted(path.name for path in tmp_path.iterdir()) == ["completed.db"]
    finally:
        tmp_path.chmod(original_directory_mode)
        database_path.chmod(0o644)
        qcodes.config.core.db_location = original_database_path


@pytest.mark.parametrize(
    "opener",
    [sqlite_read_only_connection, qcodes_read_only_connection],
    ids=["sqlite", "qcodes"],
)
def test_checkpointed_wal_format_uses_private_immutable_snapshot(
    tmp_path,
    monkeypatch,
    opener,
):
    database_path = tmp_path / "checkpointed-wal.db"
    original_database_path = qcodes.config.core.db_location
    try:
        _create_default_wal_run(database_path)
        assert database_path.read_bytes()[18:20] == b"\x02\x02"
        assert not readonly_module._wal_path(database_path).exists()
        assert not readonly_module._journal_path(database_path).exists()

        source_state = _sqlite_artifact_state(database_path)
        snapshot_directories = _track_readonly_snapshot_directories(monkeypatch)
        connection = opener(database_path)
        try:
            opened_path = Path(
                connection.execute("PRAGMA database_list").fetchone()[2]
            ).resolve()
            assert opened_path != database_path.resolve()
            assert opened_path.parent in {
                snapshot_directory.resolve()
                for snapshot_directory in snapshot_directories
            }
            assert opened_path.is_file()
            assert _sqlite_artifact_state(database_path) == source_state
        finally:
            connection.close()

        assert all(not path.exists() for path in snapshot_directories)
        assert _sqlite_artifact_state(database_path) == source_state
    finally:
        qcodes.config.core.db_location = original_database_path


def test_provenance_marked_live_wal_refreshes_see_new_rows_without_source_changes(
    tmp_path,
):
    database_path = tmp_path / "live.db"
    original_database_path = qcodes.config.core.db_location
    writer = None
    try:
        initialise_or_create_database_at(database_path)
        _install_test_generation_provenance(database_path)
        writer = testdata_module._connect_writable_exact_path(database_path)
        testdata_module.enable_generation_provenance_for_writer(writer)
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        experiment = load_or_create_experiment(
            "live_wal",
            sample_name="wal",
            conn=writer,
        )
        setpoint = ManualParameter("live_setpoint")
        signal = ManualParameter("live_signal")
        measurement = Measurement(exp=experiment, name="live_wal_run")
        measurement.register_parameter(setpoint)
        measurement.register_parameter(signal, setpoints=(setpoint,))

        with measurement.run() as datasaver:
            dataset = datasaver.dataset
            table_name = dataset.table_name
            guid = dataset.guid
            datasaver.add_result((setpoint, 1.0), (signal, 2.0))
            datasaver.flush_data_to_database(block=True)

            assert (tmp_path / "live.db-wal").stat().st_size > 0
            assert (tmp_path / "live.db-shm").stat().st_size > 0

            immutable = sqlite3.connect(
                f"{database_path.resolve().as_uri()}?mode=ro&immutable=1",
                uri=True,
                )
            try:
                try:
                    immutable_count = immutable.execute(
                        f'SELECT COUNT(*) FROM "{table_name}"'
                        ).fetchone()[0]
                except sqlite3.OperationalError as err:
                    assert "no such table" in str(err)
                    immutable_count = 0
            finally:
                immutable.close()
            assert immutable_count < 1

            def direct_row_count():
                conn = sqlite_read_only_connection(database_path)
                try:
                    return conn.execute(
                        f'SELECT COUNT(*) FROM "{table_name}"'
                        ).fetchone()[0]
                finally:
                    conn.close()

            def qcodes_row_count():
                conn = qcodes_read_only_connection(database_path)
                try:
                    return conn.execute(
                        f'SELECT COUNT(*) FROM "{table_name}"'
                        ).fetchone()[0]
                finally:
                    conn.close()

            def snapshot_is_temporary(opener):
                conn = opener(database_path)
                snapshot_path = Path(
                    conn.execute("PRAGMA database_list").fetchone()[2]
                    )
                try:
                    assert snapshot_path != database_path.resolve()
                    assert snapshot_path.is_file()
                    return snapshot_path.parent
                finally:
                    conn.close()

            for opener in (
                sqlite_read_only_connection,
                qcodes_read_only_connection,
            ):
                snapshot_directory = _run_without_source_changes(
                    tmp_path,
                    lambda opener=opener: snapshot_is_temporary(opener),
                    )
                assert not snapshot_directory.exists()

            assert _run_without_source_changes(tmp_path, direct_row_count) == 1
            assert _run_without_source_changes(tmp_path, qcodes_row_count) == 1
            assert _run_without_source_changes(
                tmp_path,
                lambda: database_access_error(database_path),
                ) is None

            datasaver.add_result((setpoint, 2.0), (signal, 3.0))
            datasaver.flush_data_to_database(block=True)

            assert _run_without_source_changes(tmp_path, direct_row_count) == 2
            assert _run_without_source_changes(tmp_path, qcodes_row_count) == 2

            def dataset_row_count():
                loaded = load_by_guid_read_only(guid, database_path)
                try:
                    data = loaded.get_parameter_data("live_signal")
                    return len(data["live_signal"]["live_signal"])
                finally:
                    loaded.conn.close()

            assert _run_without_source_changes(tmp_path, dataset_row_count) == 2

        dataset.conn.close()
        experiment.conn.close()
    finally:
        if writer is not None:
            writer.close()
        qcodes.config.core.db_location = original_database_path


def test_replaced_main_quarantines_unpaired_wal_without_source_changes(tmp_path):
    """An unknown WAL must never be paired with an atomically replaced main."""

    database_path = tmp_path / "replace-live.db"
    replacement_path = tmp_path / "replacement.db"
    second_replacement_path = tmp_path / "second-replacement.db"
    parked_wal_path = tmp_path / "parked-old-wal"
    original_database_path = qcodes.config.core.db_location
    old_dataset = None
    experiment = None
    writer = None
    try:
        # qPlot can legitimately have loaded this checkpointed instance before
        # an external process changes it to WAL mode. No prior WAL identity is
        # available when the later atomic replacement happens.
        initialise_or_create_database_at(str(database_path), journal_mode="DELETE")
        experiment = load_or_create_experiment(
            "stale_wal",
            sample_name="replacement",
        )
        setpoint = ManualParameter("stale_wal_setpoint")
        signal = ManualParameter("stale_wal_signal")
        measurement = Measurement(exp=experiment, name="old_live_run")
        measurement.register_parameter(setpoint)
        measurement.register_parameter(signal, setpoints=(setpoint,))

        with measurement.run(write_in_background=False) as datasaver:
            old_dataset = datasaver.dataset
            datasaver.add_result((setpoint, 1.0), (signal, 2.0))
        old_dataset.conn.close()
        old_dataset = None
        experiment.conn.close()
        experiment = None

        # An unrelated writer creates a WAL after the static viewer state was
        # accepted. It remains live while a replacement main file is installed.
        writer = sqlite3.connect(database_path)
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("UPDATE runs SET name = ?", ("old_wal_run",))
        writer.commit()
        assert database_file_identity(f"{database_path}-wal") is not None
        assert Path(f"{database_path}-shm").is_file()
        shutil.copyfile(f"{database_path}-wal", parked_wal_path)
        # Windows cannot rename a WAL while SQLite has it open. Preserve a
        # byte-for-byte old sidecar, then close the unrelated writer before
        # simulating the external replacement.
        writer.close()
        writer = None

        generate_database(
            [
                RunSpecification(
                    1,
                    "replacement_signal",
                    "Replacement signal",
                    "V",
                    -1.0,
                    1.0,
                    4,
                )
            ],
            replacement_path,
        )
        replacement_conn = sqlite3.connect(replacement_path)
        try:
            expected_run = replacement_conn.execute(
                "SELECT guid, result_counter FROM runs"
                ).fetchone()
        finally:
            replacement_conn.close()

        # The old writer can temporarily remove its sidecar before the main
        # file is replaced, then recreate it later. Register the replacement
        # epoch while no WAL exists and restore that proven-old WAL afterwards.
        os.replace(replacement_path, database_path)
        assert quarantine_wal_for_replaced_database(database_path)
        assert replacement_wal_is_quarantined(database_path)

        absent_wal_state = _directory_state(tmp_path)
        conn = sqlite_read_only_connection(database_path)
        try:
            assert conn.execute("SELECT guid, result_counter FROM runs").fetchone() == (
                expected_run
            )
        finally:
            conn.close()
        assert _directory_state(tmp_path) == absent_wal_state

        shutil.copyfile(parked_wal_path, f"{database_path}-wal")
        source_state = _directory_state(tmp_path)
        for opener in (
                sqlite_read_only_connection,
                qcodes_read_only_connection,
                ):
            conn = opener(database_path)
            try:
                assert conn.execute("SELECT guid, result_counter FROM runs").fetchone() == expected_run
            finally:
                conn.close()

        assert database_access_error(database_path) is None
        assert _directory_state(tmp_path) == source_state

        # The missing-WAL observation immediately before restoration above
        # must not expire the replacement epoch.
        assert replacement_wal_is_quarantined(database_path)

        generate_database(
            [
                RunSpecification(
                    1,
                    "second_replacement_signal",
                    "Second replacement signal",
                    "V",
                    -1.0,
                    1.0,
                    5,
                )
            ],
            second_replacement_path,
        )
        second_conn = sqlite3.connect(second_replacement_path)
        try:
            expected_second_run = second_conn.execute(
                "SELECT guid, result_counter FROM runs"
                ).fetchone()
        finally:
            second_conn.close()

        # Carry the same quarantine forward across N1 -> N2 while the old WAL
        # is still beside the path.
        os.replace(second_replacement_path, database_path)
        source_state = _directory_state(tmp_path)
        assert replacement_wal_is_quarantined(database_path)
        for opener in (
                sqlite_read_only_connection,
                qcodes_read_only_connection,
                ):
            conn = opener(database_path)
            try:
                assert conn.execute("SELECT guid, result_counter FROM runs").fetchone() == expected_second_run
            finally:
                conn.close()
        assert _directory_state(tmp_path) == source_state
    finally:
        if writer is not None:
            writer.close()
        if old_dataset is not None:
            old_dataset.conn.close()
        if experiment is not None:
            experiment.conn.close()
        qcodes.config.core.db_location = original_database_path


def test_expected_identity_rejects_replacement_during_read_preparation(
        tmp_path,
        monkeypatch,
        ):
    """A source replacement between UI checks and open cannot form a snapshot."""

    from qplot.datahandling import readonly

    database_path = tmp_path / "identity-race.db"
    replacement_path = tmp_path / "identity-race-replacement.db"
    writer = sqlite3.connect(database_path)
    replacement_writer = None
    replaced_state = None
    try:
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("CREATE TABLE old_rows (value TEXT)")
        writer.execute("INSERT INTO old_rows VALUES ('old')")
        writer.commit()
        expected_identity = database_file_identity(database_path)
        assert expected_identity is not None
        assert Path(f"{database_path}-wal").is_file()

        replacement_writer = sqlite3.connect(replacement_path)
        replacement_writer.execute("CREATE TABLE replacement_rows (value TEXT)")
        replacement_writer.execute("INSERT INTO replacement_rows VALUES ('new')")
        replacement_writer.commit()
        replacement_writer.close()
        replacement_writer = None

        real_require = readonly._require_expected_database_instance
        replaced = False

        def replace_after_initial_identity_check(path, expected_identity):
            nonlocal replaced, replaced_state, writer
            result = real_require(path, expected_identity)
            if not replaced and expected_identity is not None:
                # The identity race itself does not depend on keeping the old
                # writer open. Close it so Windows permits the atomic swap.
                writer.close()
                writer = None
                os.replace(replacement_path, database_path)
                replaced_state = _directory_state(tmp_path)
                replaced = True
            return result

        monkeypatch.setattr(
            readonly,
            "_require_expected_database_instance",
            replace_after_initial_identity_check,
        )
        with pytest.raises(DatabaseInstanceChangedError, match="replaced"):
            sqlite_read_only_connection(
                database_path,
                expected_database_identity=expected_identity,
            )

        assert replaced
        assert replacement_wal_is_quarantined(database_path)
        assert _directory_state(tmp_path) == replaced_state
    finally:
        if replacement_writer is not None:
            replacement_writer.close()
        if writer is not None:
            writer.close()


def test_database_access_probe_rejects_replacement_before_child_open(
        tmp_path,
        monkeypatch,
        ):
    """The probe child must reject a main file replaced after parent setup."""

    from qplot.datahandling import database as database_module

    database_path = tmp_path / "probe-identity-race.db"
    replacement_path = tmp_path / "probe-identity-race-replacement.db"
    parked_wal_path = tmp_path / "probe-old-wal"
    writer = sqlite3.connect(database_path)
    replacement_writer = None
    replaced_state = None
    try:
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("CREATE TABLE stale_rows (value TEXT)")
        writer.execute("INSERT INTO stale_rows VALUES ('old')")
        writer.commit()
        expected_identity = database_file_identity(database_path)
        assert expected_identity is not None
        assert Path(f"{database_path}-wal").is_file()
        shutil.copyfile(f"{database_path}-wal", parked_wal_path)
        writer.close()
        writer = None

        replacement_writer = sqlite3.connect(replacement_path)
        replacement_writer.execute("CREATE TABLE replacement_rows (value TEXT)")
        replacement_writer.execute("INSERT INTO replacement_rows VALUES ('new')")
        replacement_writer.commit()
        replacement_writer.close()
        replacement_writer = None

        real_run = database_module.subprocess.run

        def replace_before_child_open(command, **kwargs):
            nonlocal replaced_state
            assert tuple(json.loads(command[-1])) == expected_identity
            os.replace(replacement_path, database_path)
            shutil.copyfile(parked_wal_path, f"{database_path}-wal")
            replaced_state = _directory_state(tmp_path)
            return real_run(command, **kwargs)

        monkeypatch.setattr(
            database_module.subprocess,
            "run",
            replace_before_child_open,
        )

        error = database_module.database_access_error(database_path)

        assert error is not None
        assert "replaced" in error
        assert replaced_state is not None
        assert _directory_state(tmp_path) == replaced_state
    finally:
        if replacement_writer is not None:
            replacement_writer.close()
        if writer is not None:
            writer.close()


def test_unstable_live_wal_fails_instead_of_using_immutable_data(
    tmp_path,
    monkeypatch,
):
    from qplot.datahandling import readonly

    database_path = tmp_path / "unstable.db"
    writer = sqlite3.connect(database_path)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE live_rows (value INTEGER)")
        writer.execute("INSERT INTO live_rows VALUES (1)")
        writer.commit()
        original_state = _directory_state(tmp_path)

        real_signature = readonly._source_signature
        signature_call = 0

        def changing_signature(path):
            nonlocal signature_call
            signature_call += 1
            signature = real_signature(path)
            wal_signature = signature.wal
            assert wal_signature is not None
            return replace(
                signature,
                wal=(*wal_signature[:-1], signature_call),
            )

        monkeypatch.setattr(readonly, "_source_signature", changing_signature)
        monkeypatch.setattr(readonly, "WAL_SNAPSHOT_ATTEMPTS", 2)

        with pytest.raises(ReadOnlyDatabaseAccessError, match="changed continuously"):
            sqlite_read_only_connection(database_path)

        assert _directory_state(tmp_path) == original_state
    finally:
        writer.close()


@pytest.mark.parametrize("journal_mode", ["DELETE", "PERSIST", "TRUNCATE"])
@pytest.mark.parametrize("outcome", ["rollback", "commit"])
def test_spilled_rollback_journal_never_exposes_uncommitted_run_names(
    tmp_path,
    monkeypatch,
    latest_schema_rollback_template,
    journal_mode,
    outcome,
):
    database_path = tmp_path / f"{journal_mode.lower()}-{outcome}.db"
    _copy_rollback_template(latest_schema_rollback_template, database_path)
    snapshot_directories = _track_readonly_snapshot_directories(monkeypatch)
    writer = _begin_spilled_run_name_update(database_path, journal_mode)
    try:
        for opener in (
            sqlite_read_only_connection,
            qcodes_read_only_connection,
        ):
            try:
                names = _run_names_without_source_changes(opener, database_path)
            except ReadOnlyDatabaseAccessError as error:
                _assert_clear_temporary_access_error(error)
            else:
                assert names == _COMMITTED_RUN_NAMES
            assert all(not path.exists() for path in snapshot_directories)

        if outcome == "rollback":
            writer.rollback()
            expected_names = _COMMITTED_RUN_NAMES
        else:
            writer.commit()
            expected_names = _UNCOMMITTED_RUN_NAMES

        journal_path = readonly_module._journal_path(database_path)
        if journal_mode == "DELETE":
            assert not journal_path.exists()
        elif journal_mode == "PERSIST":
            journal_contents = journal_path.read_bytes()
            assert len(journal_contents) > 512
            assert journal_contents[:8] == b"\x00" * 8
        else:
            assert journal_mode == "TRUNCATE"
            assert journal_path.read_bytes() == b""

        for opener in (
            sqlite_read_only_connection,
            qcodes_read_only_connection,
        ):
            assert (
                _run_names_without_source_changes(opener, database_path)
                == expected_names
            )
            assert all(not path.exists() for path in snapshot_directories)
    finally:
        if writer.in_transaction:
            writer.rollback()
        writer.close()


def _install_detached_hot_rollback_journal(template_path, database_path):
    """Install a genuine hot journal with no process retaining its file handle."""
    _copy_rollback_template(template_path, database_path)
    writer = _begin_spilled_run_name_update(database_path, "DELETE")
    journal_path = readonly_module._journal_path(database_path)
    journal_contents = journal_path.read_bytes()
    writer.rollback()
    writer.close()

    assert not journal_path.exists()
    assert _immutable_run_names(database_path) == _COMMITTED_RUN_NAMES
    journal_path.write_bytes(journal_contents)
    assert journal_path.read_bytes().startswith(_ROLLBACK_JOURNAL_MAGIC)
    return journal_path


@pytest.mark.parametrize("race_kind", ["mutate", "replace"])
def test_continuously_changing_rollback_journal_fails_closed_and_cleans_snapshots(
    tmp_path,
    monkeypatch,
    latest_schema_rollback_template,
    race_kind,
):
    database_path = tmp_path / f"changing-{race_kind}.db"
    journal_path = _install_detached_hot_rollback_journal(
        latest_schema_rollback_template,
        database_path,
    )
    attempts = 2
    replacement_paths = []
    if race_kind == "replace":
        for index in range(attempts):
            replacement_path = tmp_path / f"journal-replacement-{index}"
            shutil.copy2(journal_path, replacement_path)
            replacement_paths.append(replacement_path)

    snapshot_directories = _track_readonly_snapshot_directories(monkeypatch)
    real_copyfile = shutil.copyfile
    injection_states = []
    journal_copy_count = 0

    def copyfile_then_change_journal(source, destination, *args, **kwargs):
        nonlocal journal_copy_count
        result = real_copyfile(source, destination, *args, **kwargs)
        if Path(source) != journal_path:
            return result

        if race_kind == "mutate":
            with journal_path.open("r+b") as journal_file:
                journal_file.seek(100)
                original_byte = journal_file.read(1)
                assert original_byte
                journal_file.seek(100)
                journal_file.write(bytes((original_byte[0] ^ 1,)))
                journal_file.flush()
            status = journal_path.stat()
            changed_mtime = status.st_mtime_ns + 1_000_000 + journal_copy_count
            os.utime(
                journal_path,
                ns=(status.st_atime_ns, changed_mtime),
            )
        else:
            os.replace(replacement_paths[journal_copy_count], journal_path)

        journal_copy_count += 1
        injection_states.append(_sqlite_artifact_state(database_path))
        return result

    monkeypatch.setattr(readonly_module, "WAL_SNAPSHOT_ATTEMPTS", attempts)
    monkeypatch.setattr(
        readonly_module.shutil,
        "copyfile",
        copyfile_then_change_journal,
    )

    with pytest.raises(ReadOnlyDatabaseAccessError) as caught:
        sqlite_read_only_connection(database_path)

    _assert_clear_temporary_access_error(caught.value)
    assert journal_copy_count == attempts
    assert len(snapshot_directories) == attempts
    assert all(not path.exists() for path in snapshot_directories)
    assert _sqlite_artifact_state(database_path) == injection_states[-1]
    journal_states = [state["-journal"] for state in injection_states]
    assert all(state is not None for state in journal_states)
    if race_kind == "mutate":
        assert len({state[0] for state in journal_states}) == attempts
        assert len({state[1] for state in journal_states}) == 1
    else:
        assert len({state[0] for state in journal_states}) == 1
        assert len({state[1] for state in journal_states}) == attempts


@pytest.mark.parametrize(
    "failure_stage",
    ["main_copy", "journal_copy", "private_recovery", "instance_validation"],
)
def test_rollback_snapshot_cleanup_on_failure(
    tmp_path,
    monkeypatch,
    latest_schema_rollback_template,
    failure_stage,
):
    database_path = tmp_path / f"cleanup-{failure_stage}.db"
    journal_path = _install_detached_hot_rollback_journal(
        latest_schema_rollback_template,
        database_path,
    )
    source_state = _sqlite_artifact_state(database_path)
    snapshot_directories = _track_readonly_snapshot_directories(monkeypatch)

    if failure_stage in {"main_copy", "journal_copy"}:
        real_copyfile = shutil.copyfile
        failed_source = database_path if failure_stage == "main_copy" else journal_path

        def fail_selected_copy(source, destination, *args, **kwargs):
            if Path(source) == failed_source:
                raise OSError(f"injected {failure_stage} failure")
            return real_copyfile(source, destination, *args, **kwargs)

        monkeypatch.setattr(readonly_module.shutil, "copyfile", fail_selected_copy)
    elif failure_stage == "private_recovery":

        def fail_private_recovery(*_args, **_kwargs):
            raise ReadOnlyDatabaseAccessError("injected private recovery failure")

        monkeypatch.setattr(
            readonly_module,
            "_recover_private_rollback_journal",
            fail_private_recovery,
        )
    else:
        assert failure_stage == "instance_validation"

        def fail_instance_validation(*_args, **_kwargs):
            raise DatabaseInstanceChangedError("injected instance validation failure")

        monkeypatch.setattr(
            readonly_module,
            "_require_prepared_database_instance",
            fail_instance_validation,
        )

    with pytest.raises(ReadOnlyDatabaseAccessError):
        sqlite_read_only_connection(database_path)

    assert len(snapshot_directories) == 1
    assert all(not path.exists() for path in snapshot_directories)
    assert _sqlite_artifact_state(database_path) == source_state


@pytest.mark.parametrize("opener_kind", ["sqlite", "qcodes"])
@pytest.mark.parametrize("source_event", ["replacement", "unlink"])
def test_source_instance_change_after_connection_open_closes_provisional_connection(
    tmp_path,
    monkeypatch,
    latest_schema_rollback_template,
    opener_kind,
    source_event,
):
    database_path = tmp_path / f"post-open-{opener_kind}-{source_event}.db"
    _copy_rollback_template(latest_schema_rollback_template, database_path)
    source_state = _sqlite_artifact_state(database_path)
    connection_state = {"opened": False}
    opener, records = _tracked_provisional_opener(
        monkeypatch,
        opener_kind,
        connection_state,
    )
    real_source_signature = readonly_module._source_signature
    injected = []

    def source_signature_with_instance_change(path):
        signature = real_source_signature(path)
        if connection_state["opened"] and not injected:
            injected.append(source_event)
            if source_event == "unlink":
                return replace(
                    signature,
                    database=None,
                    database_identity=None,
                )
            return replace(
                signature,
                database_identity=("test-replacement", 17, 23),
            )
        return signature

    monkeypatch.setattr(
        readonly_module,
        "_source_signature",
        source_signature_with_instance_change,
    )

    with pytest.raises(DatabaseInstanceChangedError, match="replaced"):
        opener(database_path)

    assert injected == [source_event]
    assert len(records) == 1
    assert records[0].close_count == 1
    _assert_connection_closed(records[0].connection)
    assert _sqlite_artifact_state(database_path) == source_state


@pytest.mark.parametrize("opener_kind", ["sqlite", "qcodes"])
def test_post_open_source_signature_error_closes_connection_and_snapshot(
    tmp_path,
    monkeypatch,
    latest_schema_rollback_template,
    opener_kind,
):
    database_path = tmp_path / f"post-open-signature-{opener_kind}.db"
    _install_detached_hot_rollback_journal(
        latest_schema_rollback_template,
        database_path,
    )
    source_state = _sqlite_artifact_state(database_path)
    snapshot_directories = _track_readonly_snapshot_directories(monkeypatch)
    connection_state = {"opened": False}
    opener, records = _tracked_provisional_opener(
        monkeypatch,
        opener_kind,
        connection_state,
    )
    real_source_signature = readonly_module._source_signature

    def fail_signature_after_open(path):
        if connection_state["opened"]:
            raise OSError("injected post-open signature failure")
        return real_source_signature(path)

    monkeypatch.setattr(
        readonly_module,
        "_source_signature",
        fail_signature_after_open,
    )

    with pytest.raises(
        ReadOnlyDatabaseAccessError,
        match="Could not validate the database instance and sidecars",
    ) as caught:
        opener(database_path)

    assert isinstance(caught.value.__cause__, OSError)
    assert len(records) == 1
    assert records[0].close_count == 1
    _assert_connection_closed(records[0].connection)
    assert len(snapshot_directories) == 1
    assert all(not path.exists() for path in snapshot_directories)
    assert _sqlite_artifact_state(database_path) == source_state


@pytest.mark.parametrize("opener_kind", ["sqlite", "qcodes"])
def test_snapshot_attachment_failure_closes_connection_and_snapshot(
    tmp_path,
    monkeypatch,
    latest_schema_rollback_template,
    opener_kind,
):
    database_path = tmp_path / f"attachment-failure-{opener_kind}.db"
    _install_detached_hot_rollback_journal(
        latest_schema_rollback_template,
        database_path,
    )
    source_state = _sqlite_artifact_state(database_path)
    snapshot_directories = _track_readonly_snapshot_directories(monkeypatch)
    connection_state = {"opened": False}
    opener, records = _tracked_provisional_opener(
        monkeypatch,
        opener_kind,
        connection_state,
        fail_snapshot_attachment=True,
    )

    with pytest.raises(RuntimeError, match="snapshot attachment failure"):
        opener(database_path)

    assert len(records) == 1
    assert records[0].close_count == 1
    _assert_connection_closed(records[0].connection)
    assert len(snapshot_directories) == 1
    assert all(not path.exists() for path in snapshot_directories)
    assert _sqlite_artifact_state(database_path) == source_state


@pytest.mark.parametrize(
    "opener",
    [sqlite_read_only_connection, qcodes_read_only_connection],
    ids=["sqlite", "qcodes"],
)
def test_simultaneous_hot_journal_and_wal_fails_closed(
    tmp_path,
    monkeypatch,
    latest_schema_rollback_template,
    opener,
):
    database_path = tmp_path / "hot-journal-and-wal.db"
    wal_source_path = tmp_path / "wal-source.db"
    _install_detached_hot_rollback_journal(
        latest_schema_rollback_template,
        database_path,
    )
    _copy_rollback_template(latest_schema_rollback_template, wal_source_path)
    wal_writer = sqlite3.connect(wal_source_path)
    try:
        assert wal_writer.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        wal_writer.execute("PRAGMA wal_autocheckpoint = 0")
        wal_writer.execute("UPDATE runs SET name = 'WAL_COMMITTED'")
        wal_writer.commit()
        wal_source = readonly_module._wal_path(wal_source_path)
        assert wal_source.is_file()
        shutil.copyfile(wal_source, readonly_module._wal_path(database_path))

        source_state = _sqlite_artifact_state(database_path)
        snapshot_directories = _track_readonly_snapshot_directories(monkeypatch)
        with pytest.raises(
            ReadOnlyDatabaseAccessError,
            match="both an active-looking rollback journal and a WAL",
        ):
            opener(database_path)

        assert snapshot_directories == []
        assert _sqlite_artifact_state(database_path) == source_state
    finally:
        wal_writer.close()


def _create_cold_rollback_journal(database_path, journal_mode):
    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute(
            f"PRAGMA journal_mode = {journal_mode}"
        ).fetchone()[0] == journal_mode.lower()
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE runs SET name = 'COLD_JOURNAL_SETUP'")
        connection.rollback()
    finally:
        connection.close()

    journal_path = readonly_module._journal_path(database_path)
    journal_contents = journal_path.read_bytes()
    if journal_mode == "PERSIST":
        assert len(journal_contents) > 512
        assert journal_contents[:8] == b"\x00" * 8
    else:
        assert journal_mode == "TRUNCATE"
        assert journal_contents == b""
    journal_path.unlink()
    return journal_contents


@pytest.mark.parametrize("journal_mode", ["PERSIST", "TRUNCATE"])
def test_cold_rollback_journal_with_live_wal_remains_transaction_consistent(
    tmp_path,
    monkeypatch,
    latest_schema_rollback_template,
    journal_mode,
):
    database_path = tmp_path / f"cold-{journal_mode.lower()}-with-wal.db"
    _copy_rollback_template(latest_schema_rollback_template, database_path)
    _install_test_generation_provenance(database_path)
    cold_journal = _create_cold_rollback_journal(database_path, journal_mode)
    wal_writer = sqlite3.connect(database_path)
    try:
        assert wal_writer.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        wal_writer.execute("PRAGMA wal_autocheckpoint = 0")
        wal_writer.executemany(
            "UPDATE runs SET name = ? WHERE run_id = ?",
            (
                (run_name, run_id)
                for run_id, run_name in enumerate(
                    _UNCOMMITTED_RUN_NAMES,
                    start=1,
                )
            ),
        )
        wal_writer.commit()
        assert readonly_module._wal_path(database_path).is_file()
        readonly_module._journal_path(database_path).write_bytes(cold_journal)

        snapshot_directories = _track_readonly_snapshot_directories(monkeypatch)
        for opener in (
            sqlite_read_only_connection,
            qcodes_read_only_connection,
        ):
            assert (
                _run_names_without_source_changes(opener, database_path)
                == _UNCOMMITTED_RUN_NAMES
            )
            assert all(not path.exists() for path in snapshot_directories)
    finally:
        wal_writer.close()
