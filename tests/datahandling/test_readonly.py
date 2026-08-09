import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path

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

        def load_dataset():
            dataset = load_by_guid_read_only(guid, database_path)
            try:
                return dataset.run_id
            finally:
                dataset.conn.close()

        assert _run_without_source_changes(tmp_path, load_dataset) == run_id
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


def test_live_wal_refreshes_see_new_rows_without_source_changes(tmp_path):
    database_path = tmp_path / "live.db"
    original_database_path = qcodes.config.core.db_location
    try:
        initialise_or_create_database_at(database_path)
        experiment = load_or_create_experiment("live_wal", sample_name="wal")
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
            database_signature, wal_signature = real_signature(path)
            assert wal_signature is not None
            return database_signature, (*wal_signature[:-1], signature_call)

        monkeypatch.setattr(readonly, "_source_signature", changing_signature)
        monkeypatch.setattr(readonly, "WAL_SNAPSHOT_ATTEMPTS", 2)

        with pytest.raises(ReadOnlyDatabaseAccessError, match="changed continuously"):
            sqlite_read_only_connection(database_path)

        assert _directory_state(tmp_path) == original_state
    finally:
        writer.close()
