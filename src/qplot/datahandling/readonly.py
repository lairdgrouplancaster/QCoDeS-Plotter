import shutil
import sqlite3
import tempfile
import threading
import weakref
from pathlib import Path

import qcodes
from qcodes.dataset import load_by_guid, load_by_id
from qcodes.dataset.sqlite.database import connect, get_DB_debug, get_DB_location

from qplot.datahandling.file_identity import (
    DatabaseFileIdentity,
    database_file_identity,
)

SQLITE_READ_ONLY_CACHE_KIB = 16 * 1024
WAL_SNAPSHOT_ATTEMPTS = 5
_DATABASE_INSTANCE_REGISTRY: dict[
    Path,
    tuple[DatabaseFileIdentity | None, bool],
] = {}
_DATABASE_INSTANCE_REGISTRY_LOCK = threading.Lock()


class ReadOnlyDatabaseAccessError(RuntimeError):
    """Raised when qPlot cannot take a non-mutating view of a database."""


class DatabaseInstanceChangedError(ReadOnlyDatabaseAccessError):
    """Raised when a database was atomically replaced during a requested read."""


class _ManagedSQLiteConnection(sqlite3.Connection):
    """SQLite connection that owns a temporary WAL snapshot, when needed."""

    _qplot_snapshot: tempfile.TemporaryDirectory | None = None

    def attach_snapshot(self, snapshot):
        self._qplot_snapshot = snapshot

    def close(self):
        try:
            super().close()
        finally:
            if self._qplot_snapshot is not None:
                self._qplot_snapshot.cleanup()
                self._qplot_snapshot = None


def set_qcodes_database_location(database_path):
    """Point QCoDeS at a database without initialising or upgrading it."""
    qcodes.config.core.db_location = str(database_path)


def quarantine_wal_for_replaced_database(database_path):
    """Keep an unpaired WAL out of a newly replaced database view.

    Atomic replacement changes the main database file but can leave an old
    writer's ``-wal`` sidecar behind. SQLite can otherwise combine those two
    unrelated files and expose the old database. Once a replacement is
    detected, qPlot deliberately opens the new main file immutable for as
    long as qPlot is viewing that path. A sidecar can vanish and later be
    recreated by the old writer, so a transient absence is not enough to prove
    a later WAL belongs to the replacement. qPlot never changes the source
    database or any sidecar to make that happen.

    A changed WAL identity is not enough to prove it belongs to the replacement
    main file: an old writer can rotate or recreate its WAL after the main file
    has been atomically replaced. Therefore the whole sidecar lifetime is
    treated conservatively as ambiguous.
    """

    source_path = _resolved_database_path(database_path)
    database_identity = database_file_identity(source_path)
    with _DATABASE_INSTANCE_REGISTRY_LOCK:
        _DATABASE_INSTANCE_REGISTRY[source_path] = (database_identity, True)
    return database_identity is not None


def _observe_database_instance(database_path):
    """Record replacement history for every source opened by qPlot.

    The quarantine intentionally survives a momentary missing WAL and later
    main-file replacements. An old writer can recreate either sidecar after a
    transient gap, so absence alone cannot prove that a future WAL belongs to
    the current main file.
    """

    source_path = _resolved_database_path(database_path)
    database_identity = database_file_identity(source_path)
    with _DATABASE_INSTANCE_REGISTRY_LOCK:
        prior = _DATABASE_INSTANCE_REGISTRY.get(source_path)
        if prior is None:
            quarantined = False
        else:
            prior_identity, quarantined = prior
            if prior_identity is not None and prior_identity != database_identity:
                quarantined = True
        _DATABASE_INSTANCE_REGISTRY[source_path] = (
            database_identity,
            quarantined,
        )
    return database_identity, quarantined


def _require_expected_database_instance(database_path, expected_database_identity):
    """Reject a read whose source identity no longer matches its dataset key."""

    if expected_database_identity is None:
        return
    database_identity, _quarantined = _observe_database_instance(database_path)
    if database_identity == expected_database_identity:
        return
    quarantine_wal_for_replaced_database(database_path)
    raise DatabaseInstanceChangedError(
        "The database was replaced while qPlot was preparing a read."
    )


def configure_read_only_sqlite_connection(conn):
    """Keep read-only SQLite scans from growing qPlot's memory footprint."""
    pragmas = (
        "PRAGMA query_only = ON",
        f"PRAGMA cache_size = -{SQLITE_READ_ONLY_CACHE_KIB}",
        "PRAGMA temp_store = FILE",
        )
    for pragma in pragmas:
        try:
            conn.execute(pragma)
        except Exception:
            pass
    return conn


def qcodes_read_only_connection(
        database_path,
        *,
        ignore_unpaired_wal=False,
        expected_database_identity=None,
        ):
    """Open a source-preserving, AtomicConnection-compatible database view.

    A checkpointed database with no WAL is opened directly as immutable. If a
    WAL exists, qPlot instead opens a consistent private copy under the system
    temporary directory. This keeps live WAL rows visible without letting
    SQLite create or update source ``-wal`` or ``-shm`` files.
    """
    source_path = _resolved_database_path(database_path)
    for _attempt in range(2):
        target_path, immutable, snapshot, ignore_wal = _prepare_read_target(
            source_path,
            ignore_unpaired_wal=ignore_unpaired_wal,
            expected_database_identity=expected_database_identity,
        )
        conn = None
        try:
            conn = connect(
                _qcodes_uri_path(target_path, immutable=immutable),
                get_DB_debug(),
                read_only=True,
                )
            _require_expected_database_instance(
                source_path,
                expected_database_identity,
            )
        except Exception:
            if conn is not None:
                conn.close()
            if snapshot is not None:
                snapshot.cleanup()
            raise

        if (
                immutable
                and _wal_path(source_path).exists()
                and not (
                    ignore_wal
                    and (
                        ignore_unpaired_wal
                        or replacement_wal_is_quarantined(source_path)
                    )
                )
                ):
            conn.close()
            continue

        conn.path_to_dbfile = str(source_path)
        if snapshot is not None:
            _attach_snapshot_cleanup(conn, snapshot)
        return configure_read_only_sqlite_connection(conn)

    raise ReadOnlyDatabaseAccessError(
        "The database became live while qPlot was opening it. Refresh to retry; "
        "qPlot did not open the source in a mode that could change it."
        )


def load_by_guid_read_only(
        guid,
        database_path=None,
        *,
        expected_database_identity=None,
        ):
    """Load a QCoDeS dataset by GUID through a read-only connection."""
    if database_path is None:
        database_path = get_DB_location()
    conn = qcodes_read_only_connection(
        database_path,
        expected_database_identity=expected_database_identity,
    )
    try:
        return load_by_guid(guid, conn=conn)
    except Exception:
        conn.close()
        raise


def load_by_id_read_only(
        run_id,
        database_path=None,
        *,
        expected_database_identity=None,
        ):
    """Load a QCoDeS dataset by run ID through a read-only connection."""
    if database_path is None:
        database_path = get_DB_location()
    conn = qcodes_read_only_connection(
        database_path,
        expected_database_identity=expected_database_identity,
    )
    try:
        return load_by_id(run_id, conn=conn)
    except Exception:
        conn.close()
        raise


def _resolved_database_path(database_path):
    return Path(database_path).resolve()


def _sqlite_uri_path(database_path):
    """Return an absolute, URI-escaped SQLite path without the ``file:`` prefix."""
    return _resolved_database_path(database_path).as_uri().removeprefix("file:")


def sqlite_read_only_uri(database_path, *, immutable=False):
    """Build an exact-path SQLite URI with enforced read-only access."""
    uri = f"file:{_sqlite_uri_path(database_path)}?mode=ro"
    if immutable:
        uri += "&immutable=1"
    return uri


def sqlite_read_only_connection(
        database_path,
        timeout=10,
        *,
        ignore_unpaired_wal=False,
        expected_database_identity=None,
        **kwargs,
        ):
    """Open a direct source-preserving SQLite connection.

    Static databases are opened immutable in place. WAL databases are opened
    from a consistency-checked temporary snapshot so SQLite never touches the
    source sidecars and every new connection sees a fresh committed WAL view.
    """
    source_path = _resolved_database_path(database_path)
    kwargs.setdefault("factory", _ManagedSQLiteConnection)

    for _attempt in range(2):
        target_path, immutable, snapshot, ignore_wal = _prepare_read_target(
            source_path,
            ignore_unpaired_wal=ignore_unpaired_wal,
            expected_database_identity=expected_database_identity,
        )
        conn = None
        try:
            conn = sqlite3.connect(
                sqlite_read_only_uri(target_path, immutable=immutable),
                timeout=timeout,
                uri=True,
                **kwargs,
                )
            _require_expected_database_instance(
                source_path,
                expected_database_identity,
            )
        except Exception:
            if conn is not None:
                conn.close()
            if snapshot is not None:
                snapshot.cleanup()
            raise

        if (
                immutable
                and _wal_path(source_path).exists()
                and not (
                    ignore_wal
                    and (
                        ignore_unpaired_wal
                        or replacement_wal_is_quarantined(source_path)
                    )
                )
                ):
            conn.close()
            continue

        if snapshot is not None:
            attach_snapshot = getattr(conn, "attach_snapshot", None)
            if not callable(attach_snapshot):
                conn.close()
                snapshot.cleanup()
                raise TypeError(
                    "A custom SQLite connection factory used for a live WAL "
                    "database must provide attach_snapshot()."
                    )
            attach_snapshot(snapshot)
        return configure_read_only_sqlite_connection(conn)

    raise ReadOnlyDatabaseAccessError(
        "The database became live while qPlot was opening it. Refresh to retry; "
        "qPlot did not open the source in a mode that could change it."
        )


def probe_read_only_database(
        database_path,
        *,
        ignore_unpaired_wal=False,
        expected_database_identity=None,
        ):
    """Exercise the same non-mutating open policy used by all viewer reads."""
    conn = sqlite_read_only_connection(
        database_path,
        timeout=1,
        ignore_unpaired_wal=ignore_unpaired_wal,
        expected_database_identity=expected_database_identity,
    )
    try:
        conn.execute("PRAGMA user_version").fetchone()
    finally:
        conn.close()


def _qcodes_uri_path(database_path, *, immutable):
    """Build the URI path expected by QCoDeS' URI-constructing helper.

    QCoDeS adds ``file:`` and a final ``?mode=ro`` itself. For immutable
    access, the dummy final parameter absorbs that second question mark while
    retaining the earlier, valid ``mode=ro`` and ``immutable=1`` options.
    """
    path = _sqlite_uri_path(database_path)
    if immutable:
        return f"{path}?mode=ro&immutable=1&qplot="
    return path


def _wal_path(database_path):
    return Path(f"{database_path}-wal")


def replacement_wal_is_quarantined(database_path):
    """Return whether an unpaired WAL must be omitted from this read.

    Replacement history is path-local and intentionally carries forward to a
    later main-file identity. A changed sidecar or a transient missing sidecar
    cannot prove it belongs to the current main file.
    """

    _database_identity, quarantined = _observe_database_instance(database_path)
    return bool(quarantined)


def _file_signature(path):
    try:
        stat_result = path.stat()
    except FileNotFoundError:
        return None
    return (
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_size,
        stat_result.st_mtime_ns,
        stat_result.st_ctime_ns,
        )


def _source_signature(database_path):
    return (
        _file_signature(database_path),
        _file_signature(_wal_path(database_path)),
        )


def _prepare_read_target(
        database_path,
        *,
        ignore_unpaired_wal=False,
        expected_database_identity=None,
        ):
    """Select immutable static access or make a stable private WAL snapshot."""
    if not database_path.is_file():
        raise FileNotFoundError(database_path)

    _require_expected_database_instance(
        database_path,
        expected_database_identity,
    )

    wal_path = _wal_path(database_path)
    if ignore_unpaired_wal or replacement_wal_is_quarantined(database_path):
        return database_path, True, None, True
    if not wal_path.exists():
        _require_expected_database_instance(
            database_path,
            expected_database_identity,
        )
        return database_path, True, None, False

    for _attempt in range(WAL_SNAPSHOT_ATTEMPTS):
        _require_expected_database_instance(
            database_path,
            expected_database_identity,
        )
        before = _source_signature(database_path)
        if before[0] is None:
            raise FileNotFoundError(database_path)
        if before[1] is None:
            _require_expected_database_instance(
                database_path,
                expected_database_identity,
            )
            return (
                database_path,
                True,
                None,
                replacement_wal_is_quarantined(database_path),
            )

        snapshot = tempfile.TemporaryDirectory(prefix="qplot-readonly-")
        snapshot_path = Path(snapshot.name) / "database.db"
        try:
            shutil.copyfile(database_path, snapshot_path)
            shutil.copyfile(wal_path, _wal_path(snapshot_path))
        except FileNotFoundError:
            snapshot.cleanup()
            continue
        except OSError as err:
            snapshot.cleanup()
            raise ReadOnlyDatabaseAccessError(
                f"Could not copy a read-only view of {database_path}: {err}"
                ) from err

        if _source_signature(database_path) == before:
            try:
                _require_expected_database_instance(
                    database_path,
                    expected_database_identity,
                )
            except Exception:
                snapshot.cleanup()
                raise
            if replacement_wal_is_quarantined(database_path):
                snapshot.cleanup()
                return database_path, True, None, True
            return snapshot_path, False, snapshot, False
        snapshot.cleanup()

    raise ReadOnlyDatabaseAccessError(
        "The database WAL changed continuously while qPlot tried to make a "
        "non-mutating snapshot. Refresh to retry; the source database and its "
        "SQLite sidecars were not opened for writing."
        )


def _attach_snapshot_cleanup(conn, snapshot):
    """Make an AtomicConnection clean up its private snapshot on close or GC."""
    connection_ref = weakref.ref(conn)
    finalizer = weakref.finalize(conn, snapshot.cleanup)

    def close_snapshot():
        connection = connection_ref()
        try:
            if connection is not None:
                sqlite3.Connection.close(connection)
        finally:
            finalizer()

    conn.close = close_snapshot
    conn._qplot_snapshot_finalizer = finalizer
