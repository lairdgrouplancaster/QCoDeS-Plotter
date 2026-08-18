"""Fresh-interpreter scenarios for SQLite WAL lineage regressions.

This module is an executable test helper rather than a pytest test module.  Each
scenario owns all of its setup and assertions so qPlot's process-local database
registry cannot leak between regression cases.
"""

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import qcodes
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_experiment,
    load_or_create_experiment,
)
from qcodes.dataset.sqlite.db_upgrades import _latest_available_version
from qcodes.parameters import ManualParameter

from qplot.datahandling import readonly
from qplot.datahandling.database import database_access_error
from qplot.datahandling.file_identity import database_file_identity
from qplot.datahandling.readonly import (
    DatabaseInstanceChangedError,
    ReadOnlyDatabaseAccessError,
    UnverifiableDatabaseWalError,
    probe_read_only_database,
    qcodes_read_only_connection,
    replacement_wal_is_quarantined,
    sqlite_read_only_connection,
)
from qplot.testdata import (
    RunSpecification,
    enable_generation_provenance_for_writer,
    generate_database,
)

_SQLITE_ARTIFACT_SUFFIXES = ("", "-wal", "-shm", "-journal")


def _artifact_state(database_path):
    """Capture bytes, identity, mtime, and presence of every source artifact."""
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


def _assert_operation_preserves_source(database_path, operation):
    before = _artifact_state(database_path)
    result = operation()
    assert _artifact_state(database_path) == before
    return result


def _build_latest_qcodes_database(database_path, run_name):
    """Create one independent latest-schema rollback-format QCoDeS database."""
    initialise_or_create_database_at(database_path, journal_mode="DELETE")
    experiment = load_or_create_experiment(
        "wal_lineage_experiment",
        sample_name="wal_lineage_sample",
    )
    setpoint = ManualParameter("wal_lineage_setpoint")
    signal = ManualParameter("wal_lineage_signal")
    measurement = Measurement(exp=experiment, name=run_name)
    measurement.register_parameter(setpoint)
    measurement.register_parameter(signal, setpoints=(setpoint,))
    dataset = None
    try:
        with measurement.run(write_in_background=False) as datasaver:
            datasaver.add_result((setpoint, 1.0), (signal, 2.0))
            dataset = datasaver.dataset
    finally:
        if dataset is not None:
            dataset.conn.close()
        experiment.conn.close()

    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone() == (
            _latest_available_version(),
        )
        assert connection.execute("SELECT name FROM runs").fetchone() == (run_name,)
    finally:
        connection.close()


def _immutable_run_name(database_path):
    connection = sqlite3.connect(
        f"{Path(database_path).resolve().as_uri()}?mode=ro&immutable=1",
        uri=True,
    )
    try:
        return connection.execute("SELECT name FROM runs").fetchone()[0]
    finally:
        connection.close()


def _qplot_run_name(database_path, opener=qcodes_read_only_connection):
    connection = opener(database_path)
    try:
        return connection.execute("SELECT name FROM runs").fetchone()[0]
    finally:
        connection.close()


def _private_wal_row(database_path, statement):
    """Read main plus WAL only after copying both away from the source path."""
    with tempfile.TemporaryDirectory(prefix="qplot-wal-lineage-probe-") as temp_dir:
        probe_path = Path(temp_dir) / "probe.db"
        shutil.copyfile(database_path, probe_path)
        shutil.copyfile(f"{database_path}-wal", f"{probe_path}-wal")
        connection = sqlite3.connect(probe_path)
        try:
            return connection.execute(statement).fetchone()
        finally:
            connection.close()


def _private_wal_rows(database_path, statement):
    """Read all requested rows from a private main/WAL copy."""
    with tempfile.TemporaryDirectory(prefix="qplot-wal-lineage-probe-") as temp_dir:
        probe_path = Path(temp_dir) / "probe.db"
        shutil.copyfile(database_path, probe_path)
        shutil.copyfile(f"{database_path}-wal", f"{probe_path}-wal")
        connection = sqlite3.connect(probe_path)
        try:
            return connection.execute(statement).fetchall()
        finally:
            connection.close()


def _assert_actionable_unverifiable(error):
    assert isinstance(error, UnverifiableDatabaseWalError)
    message = str(error).lower()
    for phrase in (
        "cannot prove",
        "-wal",
        "checkpoint",
        "refresh",
        "did not modify",
    ):
        assert phrase in message, message


def _expect_unverifiable(database_path, opener=qcodes_read_only_connection):
    try:
        connection = opener(database_path)
    except UnverifiableDatabaseWalError as error:
        _assert_actionable_unverifiable(error)
        return str(error)
    connection.close()
    raise AssertionError("An unverified WAL was unexpectedly accepted")


def _install_unrelated_main_and_wal(work_directory, *, stem="selected"):
    """Install B's main at A's path while retaining A's committed WAL."""
    database_path = work_directory / f"{stem}.db"
    replacement_path = work_directory / f"{stem}-replacement.db"
    parked_wal_path = work_directory / f"{stem}-parked.wal"
    _build_latest_qcodes_database(database_path, "BASE_A")
    _build_latest_qcodes_database(replacement_path, "NEW_MAIN")

    writer = sqlite3.connect(database_path)
    try:
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("UPDATE runs SET name = 'OLD_WAL'")
        writer.commit()
        assert writer.execute("SELECT name FROM runs").fetchone() == ("OLD_WAL",)
        shutil.copyfile(f"{database_path}-wal", parked_wal_path)
    finally:
        writer.close()

    os.replace(replacement_path, database_path)
    shutil.copyfile(parked_wal_path, f"{database_path}-wal")
    assert _immutable_run_name(database_path) == "NEW_MAIN"
    assert _private_wal_row(
        database_path,
        "SELECT name FROM runs",
    ) == ("OLD_WAL",)
    return database_path


def _generated_specification():
    return RunSpecification(
        1,
        "current",
        "Current",
        "nA",
        -1.0,
        1.0,
        5,
    )


def _open_generated_wal(database_path, run_name):
    writer = sqlite3.connect(database_path)
    assert writer.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
    writer.execute("PRAGMA wal_autocheckpoint = 0")
    # The generator installs an UPDATE trigger on runs, so this advances the
    # WAL's embedded write epoch as well as changing the visible value.
    writer.execute("UPDATE runs SET name = ?", (run_name,))
    writer.commit()
    assert writer.execute("SELECT name FROM runs").fetchone() == (run_name,)
    return writer


def _install_detached_generated_wal(work_directory, stem):
    """Create a valid generated main/WAL pair without retaining writer handles."""
    database_path = work_directory / f"{stem}.db"
    parked_main = work_directory / f"{stem}-parked-main.db"
    parked_wal = work_directory / f"{stem}-parked.wal"
    generate_database([_generated_specification()], database_path)
    writer = _open_generated_wal(database_path, "TRUSTED_WAL")
    try:
        shutil.copyfile(database_path, parked_main)
        shutil.copyfile(f"{database_path}-wal", parked_wal)
    finally:
        writer.close()

    # Closing the last writer checkpoints the source. Restore the exact
    # pre-checkpoint main and WAL as a detached, internally proven pair.
    shutil.copyfile(parked_main, database_path)
    shutil.copyfile(parked_wal, f"{database_path}-wal")
    Path(f"{database_path}-shm").unlink(missing_ok=True)
    assert _immutable_run_name(database_path) == "run_1"
    assert _private_wal_row(
        database_path,
        "SELECT name FROM runs",
    ) == ("TRUSTED_WAL",)
    return database_path


def _install_same_token_fork_main_and_wal(work_directory):
    """Install a later fork WAL beside its independently advanced sibling."""
    database_path = work_directory / "same-token-selected.db"
    fork_path = work_directory / "same-token-fork.db"
    parked_wal_path = work_directory / "same-token-fork-parked.wal"
    generate_database([_generated_specification()], database_path)

    # Fork the generated database before either branch advances. Both branches
    # therefore carry the exact same generation token and lineage root.
    shutil.copyfile(database_path, fork_path)
    original_connection = sqlite3.connect(
        f"{database_path.resolve().as_uri()}?mode=ro&immutable=1",
        uri=True,
    )
    fork_connection = sqlite3.connect(
        f"{fork_path.resolve().as_uri()}?mode=ro&immutable=1",
        uri=True,
    )
    try:
        original_token = original_connection.execute(
            "SELECT generation_token FROM qplot_generation_provenance"
        ).fetchone()[0]
        fork_token = fork_connection.execute(
            "SELECT generation_token FROM qplot_generation_provenance"
        ).fetchone()[0]
    finally:
        original_connection.close()
        fork_connection.close()
    assert fork_token == original_token

    # Advance and checkpoint the selected branch. Its main is now a durable
    # descendant that the independently advancing fork never observed.
    selected_writer = sqlite3.connect(database_path)
    try:
        assert (
            selected_writer.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        )
        selected_writer.execute("PRAGMA wal_autocheckpoint = 0")
        selected_writer.execute("UPDATE runs SET name = 'SELECTED_MAIN'")
        selected_writer.commit()
        assert selected_writer.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)"
        ).fetchone() == (0, 0, 0)
    finally:
        selected_writer.close()

    selected_connection = sqlite3.connect(
        f"{database_path.resolve().as_uri()}?mode=ro&immutable=1",
        uri=True,
    )
    try:
        selected_token, selected_epoch = selected_connection.execute(
            "SELECT generation_token, write_epoch FROM qplot_generation_provenance"
        ).fetchone()
    finally:
        selected_connection.close()
    assert selected_token == original_token
    assert _immutable_run_name(database_path) == "SELECTED_MAIN"

    # Independently advance the pre-checkpoint fork beyond the selected epoch.
    # Epoch ordering and the shared token deliberately look valid; only durable
    # branch history can prove that this WAL does not descend from selected.
    fork_writer = sqlite3.connect(fork_path)
    try:
        assert fork_writer.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        fork_writer.execute("PRAGMA wal_autocheckpoint = 0")
        fork_writer.execute("UPDATE runs SET name = 'FORK_INTERMEDIATE'")
        fork_writer.commit()
        fork_writer.execute("UPDATE runs SET name = 'FORK_WAL'")
        fork_writer.commit()
        shutil.copyfile(f"{fork_path}-wal", parked_wal_path)
        fork_snapshot = _private_wal_row(
            fork_path,
            "SELECT generation_token, write_epoch FROM qplot_generation_provenance",
        )
        assert fork_snapshot[0] == selected_token
        assert fork_snapshot[1] > selected_epoch
        assert _private_wal_row(
            fork_path,
            "SELECT name FROM runs",
        ) == ("FORK_WAL",)
    finally:
        fork_writer.close()

    shutil.copyfile(parked_wal_path, f"{database_path}-wal")
    Path(f"{database_path}-shm").unlink(missing_ok=True)

    # SQLite accepts and replays this structurally compatible fork WAL. That
    # is intentionally not proof that it belongs to the selected main.
    assert _immutable_run_name(database_path) == "SELECTED_MAIN"
    assert _private_wal_row(
        database_path,
        "SELECT name FROM runs",
    ) == ("FORK_WAL",)
    installed_snapshot = _private_wal_row(
        database_path,
        "SELECT generation_token, write_epoch FROM qplot_generation_provenance",
    )
    assert installed_snapshot[0] == selected_token
    assert installed_snapshot[1] > selected_epoch
    return database_path


def scenario_independent_main_and_wal(work_directory):
    database_path = _install_unrelated_main_and_wal(work_directory)
    source_state = _artifact_state(database_path)
    assert source_state[""] is not None
    assert source_state["-wal"] is not None
    assert source_state["-wal"][5] > 0
    assert source_state["-shm"] is None
    assert source_state["-journal"] is None

    real_temporary_directory = readonly.tempfile.TemporaryDirectory
    snapshot_directories = []

    def tracked_temporary_directory(*args, **kwargs):
        snapshot = real_temporary_directory(*args, **kwargs)
        snapshot_directories.append(Path(snapshot.name))
        return snapshot

    readonly.tempfile.TemporaryDirectory = tracked_temporary_directory
    try:
        _assert_operation_preserves_source(
            database_path,
            lambda: _expect_unverifiable(database_path),
        )
    finally:
        readonly.tempfile.TemporaryDirectory = real_temporary_directory
    assert snapshot_directories == []
    probe_error = _assert_operation_preserves_source(
        database_path,
        lambda: database_access_error(database_path, timeout=30),
    )
    assert probe_error is not None
    assert getattr(probe_error, "error_type", None) == (
        UnverifiableDatabaseWalError.__name__
    )
    _assert_actionable_unverifiable(
        UnverifiableDatabaseWalError(probe_error),
    )


def scenario_live_qcodes_safe_path(work_directory):
    database_path = work_directory / "live-qcodes.db"
    _build_latest_qcodes_database(database_path, "CHECKPOINTED_MAIN")
    writer = sqlite3.connect(database_path)
    try:
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("UPDATE runs SET name = 'LEGITIMATE_LIVE_WAL'")
        writer.commit()
        assert _immutable_run_name(database_path) == "CHECKPOINTED_MAIN"
        assert _private_wal_row(
            database_path,
            "SELECT name FROM runs",
        ) == ("LEGITIMATE_LIVE_WAL",)

        live_state = _artifact_state(database_path)
        assert live_state["-wal"] is not None
        assert live_state["-shm"] is not None
        _assert_operation_preserves_source(
            database_path,
            lambda: _expect_unverifiable(database_path),
        )

        # An owning writer may explicitly checkpoint while it remains open.
        # TRUNCATE leaves a stable zero-byte WAL, which contains no frames and
        # is therefore safe for qPlot to omit from its private main-only copy.
        checkpoint_result = writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        assert checkpoint_result == (0, 0, 0)
        assert Path(f"{database_path}-wal").is_file()
        assert Path(f"{database_path}-wal").stat().st_size == 0
        assert Path(f"{database_path}-shm").is_file()
        assert _immutable_run_name(database_path) == "LEGITIMATE_LIVE_WAL"
        for opener in (sqlite_read_only_connection, qcodes_read_only_connection):
            assert (
                _assert_operation_preserves_source(
                    database_path,
                    lambda opener=opener: _qplot_run_name(database_path, opener),
                )
                == "LEGITIMATE_LIVE_WAL"
            )
        assert (
            _assert_operation_preserves_source(
                database_path,
                lambda: database_access_error(database_path, timeout=30),
            )
            is None
        )
    finally:
        writer.close()

    # Closing the owner cleanly removes its already-checkpointed sidecars.
    assert not Path(f"{database_path}-wal").exists()
    assert not Path(f"{database_path}-shm").exists()
    assert _immutable_run_name(database_path) == "LEGITIMATE_LIVE_WAL"
    assert (
        _assert_operation_preserves_source(
            database_path,
            lambda: _qplot_run_name(database_path),
        )
        == "LEGITIMATE_LIVE_WAL"
    )


def scenario_snapshot_replacement(work_directory, artifact):
    database_path = _install_detached_generated_wal(
        work_directory,
        f"snapshot-{artifact}",
    )
    target_suffix = "" if artifact == "main" else "-wal"
    target_path = Path(f"{database_path}{target_suffix}")
    attempts = 2
    replacement_paths = []
    replacement_count = 1 if artifact == "main" else attempts
    for index in range(replacement_count):
        replacement_path = work_directory / f"{artifact}-replacement-{index}"
        shutil.copy2(target_path, replacement_path)
        replacement_paths.append(replacement_path)

    before = _artifact_state(database_path)
    real_copyfile = readonly.shutil.copyfile
    real_temporary_directory = readonly.tempfile.TemporaryDirectory
    injected_states = []
    snapshot_directories = []

    def tracked_temporary_directory(*args, **kwargs):
        snapshot = real_temporary_directory(*args, **kwargs)
        snapshot_directories.append(Path(snapshot.name))
        return snapshot

    def copy_then_replace_source(source, destination, *args, **kwargs):
        result = real_copyfile(source, destination, *args, **kwargs)
        if Path(source) == target_path and len(injected_states) < replacement_count:
            os.replace(replacement_paths[len(injected_states)], target_path)
            injected_states.append(_artifact_state(database_path))
        return result

    readonly.WAL_SNAPSHOT_ATTEMPTS = attempts
    readonly.tempfile.TemporaryDirectory = tracked_temporary_directory
    readonly.shutil.copyfile = copy_then_replace_source
    caught = None
    try:
        try:
            connection = sqlite_read_only_connection(database_path)
        except ReadOnlyDatabaseAccessError as error:
            caught = error
        else:
            connection.close()
            raise AssertionError("A source replacement was unexpectedly accepted")
    finally:
        readonly.shutil.copyfile = real_copyfile
        readonly.tempfile.TemporaryDirectory = real_temporary_directory

    assert caught is not None
    if artifact == "main":
        assert isinstance(caught, DatabaseInstanceChangedError)
    else:
        message = str(caught).lower()
        assert "changed" in message or "replaced" in message or "busy" in message
    assert len(injected_states) == replacement_count
    assert snapshot_directories
    assert all(not path.exists() for path in snapshot_directories)

    after = _artifact_state(database_path)
    assert after == injected_states[-1]
    # The test deliberately changed only the target's identity. qPlot must not
    # change its bytes or mtime, nor any other source artifact or its presence.
    assert before[target_suffix][0] == after[target_suffix][0]
    assert before[target_suffix][5:] == after[target_suffix][5:]
    assert before[target_suffix][1] != after[target_suffix][1]
    for suffix in _SQLITE_ARTIFACT_SUFFIXES:
        if suffix != target_suffix:
            assert after[suffix] == before[suffix]


def scenario_registered_replacement_history(work_directory):
    database_path = work_directory / "registered.db"
    replacement_path = work_directory / "registered-replacement.db"
    parked_wal = work_directory / "registered-parked.wal"
    _build_latest_qcodes_database(database_path, "OBSERVED_MAIN")
    _build_latest_qcodes_database(replacement_path, "NEW_MAIN")

    assert (
        _assert_operation_preserves_source(
            database_path,
            lambda: _qplot_run_name(database_path),
        )
        == "OBSERVED_MAIN"
    )
    # Register the accepted main identity in the same process. The GUI's
    # replacement monitoring follows this public registry path as well.
    assert not replacement_wal_is_quarantined(database_path)

    writer = sqlite3.connect(database_path)
    try:
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("UPDATE runs SET name = 'PROVEN_STALE_WAL'")
        writer.commit()
        shutil.copyfile(f"{database_path}-wal", parked_wal)
    finally:
        writer.close()

    os.replace(replacement_path, database_path)
    shutil.copyfile(parked_wal, f"{database_path}-wal")
    assert _immutable_run_name(database_path) == "NEW_MAIN"
    assert _private_wal_row(
        database_path,
        "SELECT name FROM runs",
    ) == ("PROVEN_STALE_WAL",)

    assert replacement_wal_is_quarantined(database_path)
    assert (
        _assert_operation_preserves_source(
            database_path,
            lambda: _qplot_run_name(database_path),
        )
        == "NEW_MAIN"
    )
    assert replacement_wal_is_quarantined(database_path)
    assert (
        _assert_operation_preserves_source(
            database_path,
            lambda: database_access_error(database_path, timeout=30),
        )
        is None
    )


def scenario_generated_provenance(work_directory):
    database_path = work_directory / "generated.db"
    generate_database([_generated_specification()], database_path)
    writer = _open_generated_wal(database_path, "GENERATED_WAL")
    try:
        assert readonly._DATABASE_INSTANCE_REGISTRY == {}
        main_row = sqlite3.connect(
            f"{database_path.resolve().as_uri()}?mode=ro&immutable=1",
            uri=True,
        )
        try:
            main_provenance = main_row.execute(
                "SELECT generation_token, write_epoch FROM qplot_generation_provenance",
            ).fetchone()
        finally:
            main_row.close()
        wal_provenance = _private_wal_row(
            database_path,
            "SELECT generation_token, write_epoch FROM qplot_generation_provenance",
        )
        assert wal_provenance[0] == main_provenance[0]
        assert wal_provenance[1] > main_provenance[1]
        assert _immutable_run_name(database_path) == "run_1"
        assert _private_wal_row(
            database_path,
            "SELECT name FROM runs",
        ) == ("GENERATED_WAL",)

        assert (
            _assert_operation_preserves_source(
                database_path,
                lambda: _qplot_run_name(database_path),
            )
            == "GENERATED_WAL"
        )
        assert (
            _assert_operation_preserves_source(
                database_path,
                lambda: database_access_error(database_path, timeout=30),
            )
            is None
        )
    finally:
        writer.close()


def scenario_future_table_checkpoint_append(work_directory):
    """Accept an enabled writer's post-checkpoint future-table append."""
    database_path = work_directory / "future-table.db"
    generate_database([_generated_specification()], database_path)
    original_database_path = qcodes.config.core.db_location
    experiment = None
    dataset = None
    try:
        qcodes.config.core.db_location = str(database_path)
        experiment = load_experiment(1)
        writer = experiment.conn
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        enable_generation_provenance_for_writer(writer)

        setpoint = ManualParameter("future_setpoint")
        signal = ManualParameter("future_signal")
        measurement = Measurement(exp=experiment, name="future_table_run")
        measurement.register_parameter(setpoint)
        measurement.register_parameter(signal, setpoints=(setpoint,))

        with measurement.run(write_in_background=False) as datasaver:
            dataset = datasaver.dataset
            table_name = dataset.table_name
            datasaver.add_result((setpoint, 1.0), (signal, 10.0))
            datasaver.flush_data_to_database(block=True)

            checkpoint_result = writer.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone()
            assert checkpoint_result == (0, 0, 0)
            assert Path(f"{database_path}-wal").stat().st_size == 0
            main_provenance = sqlite3.connect(
                f"{database_path.resolve().as_uri()}?mode=ro&immutable=1",
                uri=True,
            )
            try:
                main_epoch = main_provenance.execute(
                    "SELECT write_epoch FROM qplot_generation_provenance"
                ).fetchone()[0]
            finally:
                main_provenance.close()

            # This INSERT targets the result table that QCoDeS created after
            # synthetic generation. The writer opt-in must advance durable
            # provenance in the same transaction, despite the checkpoint.
            datasaver.add_result((setpoint, 2.0), (signal, 20.0))
            datasaver.flush_data_to_database(block=True)
            wal_epoch = _private_wal_row(
                database_path,
                "SELECT write_epoch FROM qplot_generation_provenance",
            )[0]
            assert wal_epoch > main_epoch
            statement = (
                f'SELECT "future_setpoint", "future_signal" '
                f'FROM "{table_name}" ORDER BY id'
            )
            assert _private_wal_rows(database_path, statement) == [
                (1.0, 10.0),
                (2.0, 20.0),
            ]

            source_state = _artifact_state(database_path)
            assert source_state[""] is not None
            assert source_state["-wal"] is not None
            assert source_state["-wal"][5] > 0
            assert source_state["-shm"] is not None
            assert source_state["-journal"] is None
            assert readonly._DATABASE_INSTANCE_REGISTRY == {}

            real_temporary_directory = readonly.tempfile.TemporaryDirectory
            snapshot_directories = []

            def tracked_temporary_directory(*args, **kwargs):
                snapshot = real_temporary_directory(*args, **kwargs)
                snapshot_directories.append(Path(snapshot.name))
                return snapshot

            readonly.tempfile.TemporaryDirectory = tracked_temporary_directory
            try:
                for opener in (
                    sqlite_read_only_connection,
                    qcodes_read_only_connection,
                ):

                    def visible_rows(opener=opener):
                        connection = opener(database_path)
                        try:
                            return connection.execute(statement).fetchall()
                        finally:
                            connection.close()

                    assert _assert_operation_preserves_source(
                        database_path,
                        visible_rows,
                    ) == [(1.0, 10.0), (2.0, 20.0)]
            finally:
                readonly.tempfile.TemporaryDirectory = real_temporary_directory
            assert snapshot_directories
            assert all(not path.exists() for path in snapshot_directories)
            assert _artifact_state(database_path) == source_state
    finally:
        if dataset is not None:
            dataset.conn.close()
        if experiment is not None:
            experiment.conn.close()
        qcodes.config.core.db_location = original_database_path


def scenario_same_token_fork_wal(work_directory):
    database_path = _install_same_token_fork_main_and_wal(work_directory)
    source_state = _artifact_state(database_path)
    assert source_state[""] is not None
    assert source_state["-wal"] is not None
    assert source_state["-wal"][5] > 0
    assert source_state["-shm"] is None
    assert source_state["-journal"] is None

    real_temporary_directory = readonly.tempfile.TemporaryDirectory
    snapshot_directories = []

    def tracked_temporary_directory(*args, **kwargs):
        snapshot = real_temporary_directory(*args, **kwargs)
        snapshot_directories.append(Path(snapshot.name))
        return snapshot

    readonly.tempfile.TemporaryDirectory = tracked_temporary_directory
    try:
        for opener in (sqlite_read_only_connection, qcodes_read_only_connection):
            message = _assert_operation_preserves_source(
                database_path,
                lambda opener=opener: _expect_unverifiable(
                    database_path,
                    opener,
                ),
            )
            assert "lineage" in message.lower() or "branch" in message.lower()
    finally:
        readonly.tempfile.TemporaryDirectory = real_temporary_directory
    assert snapshot_directories
    assert all(not path.exists() for path in snapshot_directories)
    assert _artifact_state(database_path) == source_state


def scenario_provenance_validation_replacement(work_directory, artifact):
    """Replace a source identity from inside provenance validation."""
    database_path = _install_detached_generated_wal(
        work_directory,
        f"validation-{artifact}",
    )
    target_suffix = "" if artifact == "main" else "-wal"
    target_path = Path(f"{database_path}{target_suffix}")
    attempts = 2
    replacement_paths = []
    for index in range(attempts):
        replacement_path = work_directory / f"{artifact}-validation-replacement-{index}"
        shutil.copy2(target_path, replacement_path)
        replacement_paths.append(replacement_path)

    before = _artifact_state(database_path)
    real_validator = readonly._require_matching_generated_wal_provenance
    real_temporary_directory = readonly.tempfile.TemporaryDirectory
    original_attempts = readonly.WAL_SNAPSHOT_ATTEMPTS
    injected_states = []
    snapshot_directories = []

    def tracked_temporary_directory(*args, **kwargs):
        snapshot = real_temporary_directory(*args, **kwargs)
        snapshot_directories.append(Path(snapshot.name))
        return snapshot

    def validate_then_replace_source(*args, **kwargs):
        result = real_validator(*args, **kwargs)
        replacement_path = replacement_paths[len(injected_states)]
        os.replace(replacement_path, target_path)
        injected_states.append(_artifact_state(database_path))
        return result

    readonly.WAL_SNAPSHOT_ATTEMPTS = attempts
    readonly.tempfile.TemporaryDirectory = tracked_temporary_directory
    readonly._require_matching_generated_wal_provenance = validate_then_replace_source
    caught = None
    try:
        try:
            connection = sqlite_read_only_connection(database_path)
        except ReadOnlyDatabaseAccessError as error:
            caught = error
        else:
            connection.close()
            raise AssertionError(
                "A source replaced during provenance validation was accepted"
            )
    finally:
        readonly._require_matching_generated_wal_provenance = real_validator
        readonly.tempfile.TemporaryDirectory = real_temporary_directory
        readonly.WAL_SNAPSHOT_ATTEMPTS = original_attempts

    assert caught is not None
    if artifact == "main":
        assert isinstance(caught, DatabaseInstanceChangedError)
    else:
        message = str(caught).lower()
        assert "changed" in message or "replaced" in message or "busy" in message
    assert injected_states
    assert snapshot_directories
    assert all(not path.exists() for path in snapshot_directories)

    after = _artifact_state(database_path)
    assert after == injected_states[-1]
    # qPlot changed nothing: only this test's os.replace changed the selected
    # target identity. Its bytes, mtime, and every other source artifact match.
    assert before[target_suffix][0] == after[target_suffix][0]
    assert before[target_suffix][5:] == after[target_suffix][5:]
    assert before[target_suffix][1] != after[target_suffix][1]
    for suffix in _SQLITE_ARTIFACT_SUFFIXES:
        if suffix != target_suffix:
            assert after[suffix] == before[suffix]


def scenario_source_invariance(work_directory):
    generated_path = work_directory / "invariance-generated.db"
    generate_database([_generated_specification()], generated_path)
    generated_writer = _open_generated_wal(generated_path, "VISIBLE_WAL")
    try:
        assert (
            _assert_operation_preserves_source(
                generated_path,
                lambda: _qplot_run_name(generated_path, sqlite_read_only_connection),
            )
            == "VISIBLE_WAL"
        )
        assert (
            _assert_operation_preserves_source(
                generated_path,
                lambda: _qplot_run_name(generated_path, qcodes_read_only_connection),
            )
            == "VISIBLE_WAL"
        )
        _assert_operation_preserves_source(
            generated_path,
            lambda: probe_read_only_database(generated_path),
        )
        assert (
            _assert_operation_preserves_source(
                generated_path,
                lambda: database_access_error(generated_path, timeout=30),
            )
            is None
        )
    finally:
        generated_writer.close()

    unrelated_path = _install_unrelated_main_and_wal(
        work_directory,
        stem="invariance-unrelated",
    )
    for opener in (sqlite_read_only_connection, qcodes_read_only_connection):
        _assert_operation_preserves_source(
            unrelated_path,
            lambda opener=opener: _expect_unverifiable(unrelated_path, opener),
        )

    def expect_probe_rejection():
        try:
            probe_read_only_database(unrelated_path)
        except UnverifiableDatabaseWalError as error:
            _assert_actionable_unverifiable(error)
            return
        raise AssertionError("The read-only probe accepted an unrelated WAL")

    _assert_operation_preserves_source(unrelated_path, expect_probe_rejection)
    probe_error = _assert_operation_preserves_source(
        unrelated_path,
        lambda: database_access_error(unrelated_path, timeout=30),
    )
    assert probe_error is not None
    assert getattr(probe_error, "error_type", None) == (
        UnverifiableDatabaseWalError.__name__
    )
    _assert_actionable_unverifiable(UnverifiableDatabaseWalError(probe_error))


_SCENARIOS = {
    "future-table-checkpoint-append": scenario_future_table_checkpoint_append,
    "independent-main-and-wal": scenario_independent_main_and_wal,
    "live-qcodes-safe-path": scenario_live_qcodes_safe_path,
    "registered-replacement-history": scenario_registered_replacement_history,
    "same-token-fork-wal": scenario_same_token_fork_wal,
    "generated-provenance": scenario_generated_provenance,
    "source-invariance": scenario_source_invariance,
}


def main(argv):
    scenario_name = argv[1]
    work_directory = Path(argv[2]).resolve()
    work_directory.mkdir(parents=True, exist_ok=False)
    assert readonly._DATABASE_INSTANCE_REGISTRY == {}
    if scenario_name == "snapshot-replacement":
        scenario_snapshot_replacement(work_directory, argv[3])
    elif scenario_name == "provenance-validation-replacement":
        scenario_provenance_validation_replacement(work_directory, argv[3])
    else:
        _SCENARIOS[scenario_name](work_directory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
