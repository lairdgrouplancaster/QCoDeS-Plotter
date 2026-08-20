import os
import shutil
import sqlite3
import struct
import tempfile
import threading
import weakref
from dataclasses import dataclass
from pathlib import Path

from qplot.datahandling.file_identity import (
    QPLOT_GENERATED_DATABASE_APPLICATION_ID,
    QPLOT_GENERATION_LINEAGE_FORMAT_VERSION,
    QPLOT_GENERATION_LINEAGE_NONCE_BYTES,
    QPLOT_GENERATION_LINEAGE_RING_TABLE,
    QPLOT_GENERATION_LINEAGE_STATE_TABLE,
    QPLOT_GENERATION_LINEAGE_WINDOW,
    QPLOT_GENERATION_PROVENANCE_TABLE,
    QPLOT_GENERATION_PROVENANCE_TOKEN_BYTES,
    DatabaseFileIdentity,
    database_file_identity,
    database_has_qplot_generation_marker,
    database_publication_guard_path,
    generation_provenance_trigger,
    open_file_identity,
    path_bound_file_identity,
)


def connect(*args, **kwargs):
    """Forward to QCoDeS without importing it for SQLite-only reads."""
    from qcodes.dataset.sqlite.database import connect as qcodes_connect

    return qcodes_connect(*args, **kwargs)


def get_DB_debug():
    """Return QCoDeS' database debug setting on demand."""
    from qcodes.dataset.sqlite.database import get_DB_debug as qcodes_get_DB_debug

    return qcodes_get_DB_debug()


def get_DB_location():
    """Return QCoDeS' configured database path on demand."""
    from qcodes.dataset.sqlite.database import (
        get_DB_location as qcodes_get_DB_location,
    )

    return qcodes_get_DB_location()


def load_by_guid(*args, **kwargs):
    """Forward to QCoDeS' GUID loader on demand."""
    from qcodes.dataset import load_by_guid as qcodes_load_by_guid

    return qcodes_load_by_guid(*args, **kwargs)


def load_by_id(*args, **kwargs):
    """Forward to QCoDeS' run-ID loader on demand."""
    from qcodes.dataset import load_by_id as qcodes_load_by_id

    return qcodes_load_by_id(*args, **kwargs)


def result_owns_supplied_connection(result):
    """Check QCoDeS connection ownership without importing it for probes."""
    from qplot.datahandling.qcodes_compat import (
        result_owns_supplied_connection as qcodes_result_owns_connection,
    )

    return qcodes_result_owns_connection(result)

SQLITE_READ_ONLY_CACHE_KIB = 16 * 1024
WAL_SNAPSHOT_ATTEMPTS = 5
_ROLLBACK_JOURNAL_MAGIC = b"\xd9\xd5\x05\xf9 \xa1c\xd7"
_ROLLBACK_JOURNAL_PREFIX_BYTES = 4096
_WAL_HEADER_BYTES = 32
_WAL_FRAME_HEADER_BYTES = 24
_WAL_FORMAT_VERSION = 3_007_000
_WAL_MAGIC_LITTLE_ENDIAN_CHECKSUMS = 0x377F0682
_WAL_MAGIC_BIG_ENDIAN_CHECKSUMS = 0x377F0683
_DATABASE_INSTANCE_REGISTRY: dict[
    Path,
    tuple[DatabaseFileIdentity | None, bool],
] = {}
_DATABASE_INSTANCE_REGISTRY_LOCK = threading.Lock()


class ReadOnlyDatabaseAccessError(RuntimeError):
    """Raised when qPlot cannot take a non-mutating view of a database."""


class DatabaseInstanceChangedError(ReadOnlyDatabaseAccessError):
    """Raised when a database was atomically replaced during a requested read."""


class UnverifiableDatabaseWalError(ReadOnlyDatabaseAccessError):
    """Raised when a WAL cannot be proven to descend from its selected main."""


@dataclass(frozen=True, slots=True)
class _RollbackJournalObservation:
    """One path-bound rollback-journal identity and state observation."""

    file_signature: tuple[int, int, int, int, int]
    file_identity: DatabaseFileIdentity | None
    prefix: bytes
    trailer: bytes
    stable: bool

    @property
    def potentially_hot(self):
        return (
            self.file_signature[2] > 512
            and self.prefix.startswith(_ROLLBACK_JOURNAL_MAGIC)
        )


@dataclass(frozen=True, slots=True)
class _SourceSignature:
    """Ephemeral state binding one private snapshot to its source files."""

    database: tuple[int, int, int, int, int] | None
    database_identity: DatabaseFileIdentity | None
    database_header: bytes
    wal: tuple[int, int, int, int, int] | None
    wal_identity: DatabaseFileIdentity | None
    journal: _RollbackJournalObservation | None
    stable: bool


class _ManagedSQLiteConnection(sqlite3.Connection):
    """SQLite connection that owns a temporary database snapshot, when needed."""

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
    import qcodes

    qcodes.config.core.db_location = str(database_path)


def quarantine_wal_for_replaced_database(database_path):
    """Keep an unpaired WAL out of a newly replaced database view.

    Atomic replacement changes the main database file but can leave an old
    writer's ``-wal`` sidecar behind. SQLite can otherwise combine those two
    unrelated files and expose the old database. Once a replacement is
    detected, qPlot captures a private main-only view while the retained
    sidecar is unpaired. A sidecar can vanish and later be recreated by the old
    writer, so a transient absence is not enough to prove a later WAL belongs
    to the replacement. A generated database's matching token and nonce-linked
    history can establish that a later WAL strictly descends from the
    replacement and release this quarantine. qPlot never changes the source
    database or any sidecar to make that happen.

    A changed WAL identity is not enough to prove it belongs to the replacement
    main file: an old writer can rotate or recreate its WAL after the main file
    has been atomically replaced. Therefore the whole sidecar lifetime is
    treated conservatively as ambiguous unless embedded lineage proves it.
    """

    source_path = _resolved_database_path(database_path)
    database_identity = database_file_identity(source_path)
    with _DATABASE_INSTANCE_REGISTRY_LOCK:
        _DATABASE_INSTANCE_REGISTRY[source_path] = (database_identity, True)
    return database_identity is not None


def _release_wal_quarantine(database_path, expected_database_identity):
    """Trust a WAL whose embedded lineage proves it belongs to this main."""
    source_path = _resolved_database_path(database_path)
    with _DATABASE_INSTANCE_REGISTRY_LOCK:
        database_identity = database_file_identity(source_path)
        if database_identity != expected_database_identity:
            return False
        _DATABASE_INSTANCE_REGISTRY[source_path] = (database_identity, False)
    return True


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


def _require_prepared_database_instance(database_path, prepared_database_identity):
    """Bind publication-marker and sidecar decisions to one main-file identity."""
    if database_file_identity(database_path) == prepared_database_identity:
        return
    quarantine_wal_for_replaced_database(database_path)
    raise DatabaseInstanceChangedError(
        "The database was replaced while qPlot was selecting a read-only view. "
        "Refresh to retry; qPlot did not combine the replacement with a prior "
        "SQLite sidecar."
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


def _cleanup_failed_connection_open(conn, snapshot):
    """Release both provisional owners after any open/attachment failure."""
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    if snapshot is not None:
        try:
            snapshot.cleanup()
        except Exception:
            pass


def qcodes_read_only_connection(
        database_path,
        *,
        ignore_unpaired_wal=False,
        expected_database_identity=None,
        ):
    """Open a source-preserving, AtomicConnection-compatible database view.

    Quiescent rollback-format sources use SQLite's enforced read-only locking.
    WAL-format databases and sources with a WAL or rollback journal are
    captured under the system temporary directory. Immutable access and any
    rollback recovery are permitted only on that private copy.
    """
    source_path = _resolved_database_path(database_path)
    for _attempt in range(2):
        (
            target_path,
            immutable,
            snapshot,
            ignore_wal,
            prepared_source,
        ) = _prepare_read_target(
            source_path,
            ignore_unpaired_wal=ignore_unpaired_wal,
            expected_database_identity=expected_database_identity,
        )
        conn = None
        try:
            if not _prepared_source_is_current(
                    source_path,
                    prepared_source,
                    direct=snapshot is None,
                    ignore_wal=ignore_wal,
                    ):
                if snapshot is not None:
                    _cleanup_failed_connection_open(None, snapshot)
                continue
            _require_publication_complete(source_path)
            conn = connect(
                _qcodes_uri_path(target_path, immutable=immutable),
                get_DB_debug(),
                read_only=True,
                )
            _require_publication_complete(source_path)
            _require_expected_database_instance(
                source_path,
                expected_database_identity,
            )
            if not _prepared_source_is_current(
                    source_path,
                    prepared_source,
                    direct=snapshot is None,
                    ignore_wal=ignore_wal,
                    ):
                _cleanup_failed_connection_open(conn, snapshot)
                continue
            _require_publication_complete(source_path)
            conn.path_to_dbfile = str(source_path)
            if snapshot is not None:
                _attach_snapshot_cleanup(conn, snapshot)
                snapshot = None
            return configure_read_only_sqlite_connection(conn)
        except Exception:
            _cleanup_failed_connection_open(conn, snapshot)
            raise

    raise ReadOnlyDatabaseAccessError(
        "The database became busy while qPlot was opening a transaction-consistent "
        "view. It is temporarily unavailable; refresh to retry. qPlot did not "
        "modify the source database or its SQLite sidecars."
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
    connection_transferred = False
    try:
        result = load_by_guid(guid, conn=conn)
        connection_transferred = result_owns_supplied_connection(result)
        return result
    finally:
        if not connection_transferred:
            conn.close()


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
    connection_transferred = False
    try:
        result = load_by_id(run_id, conn=conn)
        connection_transferred = result_owns_supplied_connection(result)
        return result
    finally:
        if not connection_transferred:
            conn.close()


def _resolved_database_path(database_path):
    return Path(database_path).resolve()


def _require_publication_complete(database_path):
    """Reject a view while qPlot is replacing the database main file."""
    guard_path = database_publication_guard_path(database_path)
    try:
        guard_path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise ReadOnlyDatabaseAccessError(
            f"Could not prove that database publication is complete: {error}"
        ) from error
    raise ReadOnlyDatabaseAccessError(
        "Test-database publication is still in progress or needs recovery. "
        "If generation has stopped, follow its recovery instructions before "
        "removing the publication guard. qPlot did not open the database or "
        "its SQLite sidecars."
    )


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

    Quiescent rollback-format sources use enforced read-only locking. WAL-
    format databases and sources with a WAL or rollback journal use a
    consistency-checked private snapshot. SQLite can therefore use immutable
    access or recover a hot journal without doing either to the source.
    """
    source_path = _resolved_database_path(database_path)
    kwargs.setdefault("factory", _ManagedSQLiteConnection)

    for _attempt in range(2):
        (
            target_path,
            immutable,
            snapshot,
            ignore_wal,
            prepared_source,
        ) = _prepare_read_target(
            source_path,
            ignore_unpaired_wal=ignore_unpaired_wal,
            expected_database_identity=expected_database_identity,
        )
        conn = None
        try:
            if not _prepared_source_is_current(
                    source_path,
                    prepared_source,
                    direct=snapshot is None,
                    ignore_wal=ignore_wal,
                    ):
                if snapshot is not None:
                    _cleanup_failed_connection_open(None, snapshot)
                continue
            _require_publication_complete(source_path)
            conn = sqlite3.connect(
                sqlite_read_only_uri(target_path, immutable=immutable),
                timeout=timeout,
                uri=True,
                **kwargs,
                )
            _require_publication_complete(source_path)
            _require_expected_database_instance(
                source_path,
                expected_database_identity,
            )
            if not _prepared_source_is_current(
                    source_path,
                    prepared_source,
                    direct=snapshot is None,
                    ignore_wal=ignore_wal,
                    ):
                _cleanup_failed_connection_open(conn, snapshot)
                continue
            _require_publication_complete(source_path)
            if snapshot is not None:
                attach_snapshot = getattr(conn, "attach_snapshot", None)
                if not callable(attach_snapshot):
                    raise TypeError(
                        "A custom SQLite connection factory used for a live "
                        "database snapshot must provide attach_snapshot()."
                    )
                attach_snapshot(snapshot)
                snapshot = None
            return configure_read_only_sqlite_connection(conn)
        except Exception:
            _cleanup_failed_connection_open(conn, snapshot)
            raise

    raise ReadOnlyDatabaseAccessError(
        "The database became busy while qPlot was opening a transaction-consistent "
        "view. It is temporarily unavailable; refresh to retry. qPlot did not "
        "modify the source database or its SQLite sidecars."
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


def _journal_path(database_path):
    return Path(f"{database_path}-journal")


def replacement_wal_is_quarantined(database_path):
    """Return whether an unpaired WAL must be omitted from this read.

    Replacement history is path-local and intentionally carries forward to a
    later main-file identity. A changed sidecar or a transient missing sidecar
    cannot prove it belongs to the current main file.
    """

    _database_identity, quarantined = _observe_database_instance(database_path)
    return bool(quarantined)


def _stat_signature(stat_result):
    return (
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_size,
        stat_result.st_mtime_ns,
        stat_result.st_ctime_ns,
    )


def _file_signature(path):
    try:
        stat_result = path.stat()
    except FileNotFoundError:
        return None
    return _stat_signature(stat_result)


def _file_prefix_observation(path, byte_count, *, include_trailer=False):
    """Read identifying bytes while proving which path instance supplied them."""
    path_identity_before = path_bound_file_identity(path)
    path_signature_before = _file_signature(path)
    if path_signature_before is None:
        return None, b"", b"", True
    try:
        with path.open("rb") as handle:
            descriptor_identity_before = open_file_identity(handle.fileno())
            descriptor_before = _stat_signature(os.fstat(handle.fileno()))
            prefix = handle.read(byte_count)
            trailer = b""
            if include_trailer and descriptor_before[2] >= 16:
                handle.seek(-16, os.SEEK_END)
                trailer = handle.read(16)
            descriptor_after = _stat_signature(os.fstat(handle.fileno()))
            descriptor_identity_after = open_file_identity(handle.fileno())
    except FileNotFoundError:
        return path_signature_before, b"", b"", False
    path_signature_after = _file_signature(path)
    path_identity_after = path_bound_file_identity(path)
    identities = (
        path_identity_before,
        descriptor_identity_before,
        descriptor_identity_after,
        path_identity_after,
    )
    identities_available = all(identity is not None for identity in identities)
    stable_identity = identities_available and len(set(identities)) == 1
    stable_metadata = (
        path_signature_before == path_signature_after
        and descriptor_before == descriptor_after
        and path_signature_before[2:4] == descriptor_before[2:4]
    )
    if identities_available:
        stable = stable_identity and stable_metadata
    else:
        stable = (
            path_signature_before
            == descriptor_before
            == descriptor_after
            == path_signature_after
        )
    return descriptor_after, prefix, trailer, stable


def _rollback_journal_observation(database_path):
    journal_path = _journal_path(database_path)
    identity_before = database_file_identity(journal_path)
    signature, prefix, trailer, stable = _file_prefix_observation(
        journal_path,
        _ROLLBACK_JOURNAL_PREFIX_BYTES,
        include_trailer=True,
    )
    if signature is None:
        return None
    identity_after = database_file_identity(journal_path)
    return _RollbackJournalObservation(
        file_signature=signature,
        file_identity=identity_after,
        prefix=prefix,
        trailer=trailer,
        stable=stable and identity_before == identity_after,
    )


def _wal_observation(database_path):
    """Observe one path-bound WAL instance without opening it through SQLite."""
    wal_path = _wal_path(database_path)
    identity_before = database_file_identity(wal_path)
    signature_before = _file_signature(wal_path)
    signature_after = _file_signature(wal_path)
    identity_after = database_file_identity(wal_path)
    return (
        signature_after,
        identity_after,
        identity_before == identity_after and signature_before == signature_after,
    )


def _source_signature(database_path):
    database_signature, database_header, _trailer, database_stable = (
        _file_prefix_observation(database_path, 100)
    )
    wal_signature, wal_identity, wal_stable = _wal_observation(database_path)
    journal = _rollback_journal_observation(database_path)
    return _SourceSignature(
        database=database_signature,
        database_identity=database_file_identity(database_path),
        database_header=database_header,
        wal=wal_signature,
        wal_identity=wal_identity,
        journal=journal,
        stable=(
            database_stable
            and wal_stable
            and (journal is None or journal.stable)
        ),
        )


def _signature_requires_private_snapshot(signature, *, ignore_wal):
    return (
        not signature.stable
        or signature.journal is not None
        or (signature.wal is not None and not ignore_wal)
    )


def _prepared_source_is_current(
        database_path,
        prepared_source,
        *,
        direct,
        ignore_wal,
        ):
    """Validate the exact main/journal instance through connection opening."""
    try:
        current_source = _source_signature(database_path)
    except OSError as error:
        raise ReadOnlyDatabaseAccessError(
            f"Could not validate the database instance and sidecars: {error}"
        ) from error
    if current_source.database_identity != prepared_source.database_identity:
        quarantine_wal_for_replaced_database(database_path)
        raise DatabaseInstanceChangedError(
            "The database was replaced while qPlot was opening its read-only view."
        )
    return (
        current_source == prepared_source
        and (
            not direct
            or not _signature_requires_private_snapshot(
                current_source,
                ignore_wal=ignore_wal,
            )
        )
    )


def _source_signature_for_validation(database_path):
    """Read a validation signature while keeping filesystem errors explicit."""
    try:
        return _source_signature(database_path)
    except OSError as error:
        raise ReadOnlyDatabaseAccessError(
            f"Could not validate a read-only copy of {database_path}: {error}"
        ) from error


def _generation_provenance(connection):
    application_id = connection.execute("PRAGMA application_id").fetchone()
    row = connection.execute(
        f"SELECT generation_token, write_epoch "
        f"FROM {QPLOT_GENERATION_PROVENANCE_TABLE} WHERE singleton = 1"
    ).fetchone()
    extra_row = connection.execute(
        f"SELECT 1 FROM {QPLOT_GENERATION_PROVENANCE_TABLE} "
        "WHERE singleton != 1 LIMIT 1"
    ).fetchone()
    try:
        token_bytes = (
            bytes.fromhex(row[0])
            if row is not None and isinstance(row[0], str)
            else b""
        )
    except ValueError:
        token_bytes = b""
    if (
            application_id != (QPLOT_GENERATED_DATABASE_APPLICATION_ID,)
            or row is None
            or extra_row is not None
            or len(token_bytes) != QPLOT_GENERATION_PROVENANCE_TOKEN_BYTES
            or not isinstance(row[1], int)
            or row[1] < 0
            ):
        raise ValueError("invalid qPlot generation provenance")
    return row


def _generation_lineage_state(connection):
    """Read and structurally validate the branch-bound provenance head."""
    state = connection.execute(
        f"SELECT singleton, format_version, generation_token, head_sequence, "
        f"head_nonce, window_size FROM {QPLOT_GENERATION_LINEAGE_STATE_TABLE}"
    ).fetchall()
    if len(state) != 1:
        raise ValueError("invalid qPlot generation lineage state")
    singleton, version, token, sequence, nonce, window = state[0]
    tip = connection.execute(
        f"SELECT slot, parent_nonce, nonce FROM "
        f"{QPLOT_GENERATION_LINEAGE_RING_TABLE} WHERE sequence = ?",
        (sequence,),
    ).fetchone()
    if (
            singleton != 1
            or version != QPLOT_GENERATION_LINEAGE_FORMAT_VERSION
            or not isinstance(token, str)
            or not isinstance(sequence, int)
            or sequence < 0
            or not isinstance(nonce, bytes)
            or len(nonce) != QPLOT_GENERATION_LINEAGE_NONCE_BYTES
            or window != QPLOT_GENERATION_LINEAGE_WINDOW
            or tip is None
            or tip[0] != sequence % QPLOT_GENERATION_LINEAGE_WINDOW
            or tip[2] != nonce
            ):
        raise ValueError("invalid qPlot generation lineage state")
    return token, sequence, nonce, window


def _wal_checksum(data, byte_order, state=(0, 0)):
    """Return SQLite's cumulative two-word WAL checksum."""
    if len(data) % 8:
        raise ValueError("invalid WAL checksum input length")
    checksum_0, checksum_1 = state
    for word_0, word_1 in struct.iter_unpack(f"{byte_order}II", data):
        checksum_0 = (checksum_0 + word_0 + checksum_1) & 0xFFFFFFFF
        checksum_1 = (checksum_1 + word_1 + checksum_0) & 0xFFFFFFFF
    return checksum_0, checksum_1


def _committed_wal_transaction_pages(wal_path):
    """Return page sets for SQLite's valid committed private-WAL prefix.

    WAL files can retain invalid frames from an earlier checkpoint cycle.  The
    checksum scan mirrors SQLite recovery: it stops at the first invalid frame
    and ignores valid but uncommitted tail frames after the last commit marker.
    """
    with open(wal_path, "rb") as wal_file:
        header = wal_file.read(_WAL_HEADER_BYTES)
        if len(header) != _WAL_HEADER_BYTES:
            raise ValueError("missing or truncated WAL header")
        magic = int.from_bytes(header[:4], "big")
        if magic == _WAL_MAGIC_LITTLE_ENDIAN_CHECKSUMS:
            checksum_byte_order = "<"
        elif magic == _WAL_MAGIC_BIG_ENDIAN_CHECKSUMS:
            checksum_byte_order = ">"
        else:
            raise ValueError("invalid WAL magic")
        if int.from_bytes(header[4:8], "big") != _WAL_FORMAT_VERSION:
            raise ValueError("unsupported WAL format")
        page_size = int.from_bytes(header[8:12], "big")
        if (
                page_size < 512
                or page_size > 65_536
                or page_size & (page_size - 1)
                ):
            raise ValueError("invalid WAL page size")
        checksum = _wal_checksum(header[:24], checksum_byte_order)
        stored_header_checksum = (
            int.from_bytes(header[24:28], "big"),
            int.from_bytes(header[28:32], "big"),
        )
        if checksum != stored_header_checksum:
            raise ValueError("invalid WAL header checksum")

        salts = header[16:24]
        current_transaction_pages = set()
        committed_transactions = []
        while True:
            frame_header = wal_file.read(_WAL_FRAME_HEADER_BYTES)
            if not frame_header:
                break
            if len(frame_header) != _WAL_FRAME_HEADER_BYTES:
                break
            page = wal_file.read(page_size)
            if len(page) != page_size or frame_header[8:16] != salts:
                break
            next_checksum = _wal_checksum(
                frame_header[:8],
                checksum_byte_order,
                checksum,
            )
            next_checksum = _wal_checksum(
                page,
                checksum_byte_order,
                next_checksum,
            )
            stored_frame_checksum = (
                int.from_bytes(frame_header[16:20], "big"),
                int.from_bytes(frame_header[20:24], "big"),
            )
            if next_checksum != stored_frame_checksum:
                break
            page_number = int.from_bytes(frame_header[:4], "big")
            if page_number == 0:
                break
            checksum = next_checksum
            current_transaction_pages.add(page_number)
            if int.from_bytes(frame_header[4:8], "big"):
                committed_transactions.append(
                    frozenset(current_transaction_pages)
                )
                current_transaction_pages.clear()

    if not committed_transactions:
        raise ValueError("the WAL has no valid committed transaction")
    return tuple(committed_transactions)


def _lineage_state_root_page(connection):
    rows = connection.execute(
        "SELECT rootpage FROM sqlite_schema WHERE type = 'table' AND name = ?",
        (QPLOT_GENERATION_LINEAGE_STATE_TABLE,),
    ).fetchall()
    if len(rows) != 1 or not isinstance(rows[0][0], int) or rows[0][0] <= 0:
        raise ValueError("invalid qPlot generation lineage root page")
    return rows[0][0]


def _require_generation_lineage_trigger_inventory(connection):
    """Require every current user table to carry the exact v2 trigger trio."""
    excluded_tables = (
        QPLOT_GENERATION_PROVENANCE_TABLE,
        QPLOT_GENERATION_LINEAGE_STATE_TABLE,
        QPLOT_GENERATION_LINEAGE_RING_TABLE,
    )
    table_names = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_schema "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "AND name NOT IN (?, ?, ?) ORDER BY name",
            excluded_tables,
        ).fetchall()
    ]
    triggers = {
        row[0]: (row[1], row[2])
        for row in connection.execute(
            "SELECT name, tbl_name, sql FROM sqlite_schema WHERE type = 'trigger'"
        ).fetchall()
    }
    for table_name in table_names:
        for operation in ("INSERT", "UPDATE", "DELETE"):
            trigger_name, statement = generation_provenance_trigger(
                table_name,
                operation,
            )
            if triggers.get(trigger_name) != (table_name, statement):
                raise ValueError(
                    f"missing or invalid lineage trigger for {table_name!r}"
                )


def _require_generation_lineage_descendant(
        connection,
        main_sequence,
        main_nonce,
        snapshot_sequence,
        snapshot_nonce,
        window,
        ):
    """Prove the WAL head is linked directly to the selected main's head."""
    transition_count = snapshot_sequence - main_sequence
    if transition_count <= 0:
        raise ValueError("non-advancing lineage")
    if transition_count > window:
        raise OverflowError("lineage proof window exceeded")
    rows = connection.execute(
        f"SELECT slot, sequence, parent_nonce, nonce FROM "
        f"{QPLOT_GENERATION_LINEAGE_RING_TABLE} "
        "WHERE sequence > ? AND sequence <= ? ORDER BY sequence",
        (main_sequence, snapshot_sequence),
    ).fetchall()
    if len(rows) != transition_count:
        raise ValueError("lineage branch has missing transitions")

    expected_sequence = main_sequence + 1
    expected_parent = main_nonce
    for slot, sequence, parent_nonce, nonce in rows:
        if (
                sequence != expected_sequence
                or slot != sequence % window
                or parent_nonce != expected_parent
                or not isinstance(nonce, bytes)
                or len(nonce) != QPLOT_GENERATION_LINEAGE_NONCE_BYTES
                ):
            raise ValueError("lineage branch does not descend from the main")
        expected_sequence += 1
        expected_parent = nonce
    if expected_parent != snapshot_nonce:
        raise ValueError("lineage branch does not reach the WAL head")


def _unverifiable_generated_wal_error(database_path, reason):
    writer_guidance = ""
    if (
            "lineage event" in reason
            or "older qPlot" in reason
            or "proof window" in reason
            ):
        writer_guidance = (
            " If the owner will create later QCoDeS runs, call "
            "qplot.testdata.enable_generation_provenance_for_writer on the "
            "quiescent writable QCoDeS connection before creating the "
            "Measurement. The writer API refuses to bless a nonempty WAL; "
            "checkpoint it with TRUNCATE first. SQLite cannot prove an "
            "already-written uninstrumented WAL after the fact."
        )
    return UnverifiableDatabaseWalError(
        f"qPlot cannot prove that {database_path}-wal belongs to the current "
        "generated database main file, so it refused to show a possibly stale "
        f"view ({reason}).{writer_guidance} Close the QCoDeS/SQLite writer "
        "cleanly so it can "
        "checkpoint and remove the WAL, then refresh. If the database is being "
        "moved or copied, stop the writer and keep the main, -wal, and -shm "
        "files together. qPlot did not modify the database or its sidecars."
    )


def _unverifiable_unmarked_wal_error(database_path):
    return UnverifiableDatabaseWalError(
        f"qPlot cannot prove that {database_path}-wal belongs to the selected "
        "database main file. Standard SQLite WAL files contain no identifier "
        "that binds them to a particular main file, and this database has no "
        "qPlot generation provenance. qPlot therefore refused to show possibly "
        "unrelated or stale data. Close every QCoDeS/SQLite connection that "
        "owns the database cleanly so the owning writer checkpoints the WAL, "
        "then refresh or retry loading. Do not delete, rename, or move the WAL "
        "by hand. qPlot did not modify the database or any SQLite sidecar."
    )


def _require_matching_generated_wal_provenance(
        database_path,
        main_snapshot_path,
        snapshot_path,
        committed_transactions=None,
        ):
    """Require a private WAL view to carry this generated main's lineage."""
    main_connection = None
    snapshot_connection = None
    try:
        if committed_transactions is None:
            committed_transactions = _committed_wal_transaction_pages(
                _wal_path(snapshot_path)
            )
        main_connection = sqlite3.connect(
            sqlite_read_only_uri(main_snapshot_path, immutable=True),
            uri=True,
        )
        main_provenance = _generation_provenance(main_connection)
        main_lineage = _generation_lineage_state(main_connection)
        main_lineage_root = _lineage_state_root_page(main_connection)
        _require_generation_lineage_trigger_inventory(main_connection)
        snapshot_connection = sqlite3.connect(snapshot_path)
        snapshot_provenance = _generation_provenance(snapshot_connection)
        snapshot_lineage = _generation_lineage_state(snapshot_connection)
        snapshot_lineage_root = _lineage_state_root_page(snapshot_connection)
        _require_generation_lineage_trigger_inventory(snapshot_connection)
    except (sqlite3.Error, ValueError) as error:
        raise _unverifiable_generated_wal_error(
            database_path,
            "the lineage history or trigger coverage is missing, invalid, or "
            "from an older qPlot",
        ) from error
    else:
        if (
                snapshot_lineage_root != main_lineage_root
                or any(
                    main_lineage_root not in transaction_pages
                    for transaction_pages in committed_transactions
                )
                ):
            raise _unverifiable_generated_wal_error(
                database_path,
                "one or more WAL transactions carry no verified lineage event",
            )
        main_token, main_epoch = main_provenance
        snapshot_token, snapshot_epoch = snapshot_provenance
        main_lineage_token, main_sequence, main_nonce, main_window = main_lineage
        (
            snapshot_lineage_token,
            snapshot_sequence,
            snapshot_nonce,
            snapshot_window,
        ) = snapshot_lineage
        if (
                snapshot_token != main_token
                or snapshot_lineage_token != main_lineage_token
                or main_lineage_token != main_token
                ):
            raise _unverifiable_generated_wal_error(
                database_path,
                "the WAL carries a different generation token",
            )
        if main_epoch != main_sequence or snapshot_epoch != snapshot_sequence:
            raise _unverifiable_generated_wal_error(
                database_path,
                "the lineage counters disagree with their branch heads",
            )
        if main_window != snapshot_window:
            raise _unverifiable_generated_wal_error(
                database_path,
                "the WAL carries a different lineage proof window",
            )
        if snapshot_sequence <= main_sequence:
            raise _unverifiable_generated_wal_error(
                database_path,
                "the WAL does not carry a later verified lineage event",
            )
        try:
            _require_generation_lineage_descendant(
                snapshot_connection,
                main_sequence,
                main_nonce,
                snapshot_sequence,
                snapshot_nonce,
                main_window,
            )
        except OverflowError as error:
            raise _unverifiable_generated_wal_error(
                database_path,
                "the WAL exceeded the retained lineage proof window",
            ) from error
        except ValueError as error:
            raise _unverifiable_generated_wal_error(
                database_path,
                "the WAL lineage branch does not descend from the selected main",
            ) from error
    finally:
        if snapshot_connection is not None:
            try:
                snapshot_connection.close()
            except sqlite3.Error:
                pass
        if main_connection is not None:
            try:
                main_connection.close()
            except sqlite3.Error:
                pass


def _journal_names_super_journal(journal):
    """Reject recovery that could follow a path outside the private snapshot."""
    if not journal.potentially_hot or len(journal.trailer) != 16:
        return False
    if journal.trailer[-8:] != _ROLLBACK_JOURNAL_MAGIC:
        return False
    name_length = int.from_bytes(journal.trailer[:4], "big")
    journal_size = journal.file_signature[2]
    return 0 < name_length <= journal_size - 20


def _recover_private_rollback_journal(database_path, snapshot_path):
    """Let SQLite resolve a copied journal without ever opening the source."""
    connection = None
    try:
        connection = sqlite3.connect(
            f"file:{_sqlite_uri_path(snapshot_path)}?mode=rw",
            uri=True,
            timeout=0,
        )
        connection.execute("PRAGMA query_only = ON")
        connection.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone()
    except sqlite3.Error as error:
        raise ReadOnlyDatabaseAccessError(
            "qPlot could not recover a transaction-consistent private copy of "
            f"{database_path}. The database is busy or temporarily unavailable; "
            "finish the current transaction or refresh to retry. qPlot did not "
            "modify the source database or its SQLite sidecars."
        ) from error
    finally:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass


def _prepare_read_target(
        database_path,
        *,
        ignore_unpaired_wal=False,
        expected_database_identity=None,
    ):
    """Select safe direct access or capture a stable private SQLite snapshot."""
    _require_publication_complete(database_path)
    if not database_path.is_file():
        raise FileNotFoundError(database_path)

    _require_expected_database_instance(
        database_path,
        expected_database_identity,
    )
    prepared_database_identity = database_file_identity(database_path)
    if prepared_database_identity is None:
        raise FileNotFoundError(database_path)

    try:
        generation_marker = database_has_qplot_generation_marker(database_path)
    except OSError as error:
        raise ReadOnlyDatabaseAccessError(
            f"Could not inspect the database publication marker: {error}"
        ) from error

    wal_path = _wal_path(database_path)
    journal_path = _journal_path(database_path)

    for _attempt in range(WAL_SNAPSHOT_ATTEMPTS):
        _require_expected_database_instance(
            database_path,
            expected_database_identity,
        )
        try:
            before = _source_signature(database_path)
        except OSError as error:
            raise ReadOnlyDatabaseAccessError(
                f"Could not inspect a read-only view of {database_path}: {error}"
            ) from error
        if before.database is None:
            raise FileNotFoundError(database_path)
        if not before.stable:
            continue

        journal = before.journal
        wal_present = before.wal is not None
        empty_wal = wal_present and before.wal[2] == 0
        quarantined = (
            replacement_wal_is_quarantined(database_path)
            if wal_present or journal is not None
            else False
        )
        ignore_wal = wal_present and (
            empty_wal
            or (
                not generation_marker
                and (ignore_unpaired_wal or quarantined)
            )
        )
        include_wal = wal_present and not ignore_wal

        if journal is not None and journal.potentially_hot and include_wal:
            raise ReadOnlyDatabaseAccessError(
                "The database has both an active-looking rollback journal and "
                "a WAL, so qPlot cannot prove which transaction state is valid. "
                "The database is busy or temporarily unavailable; finish the "
                "writer transaction and refresh. qPlot did not modify the source "
                "database or its SQLite sidecars."
            )
        if journal is not None and journal.potentially_hot and quarantined:
            raise ReadOnlyDatabaseAccessError(
                "The rollback journal cannot be paired with the database instance "
                "that replaced the previously loaded file. The database is busy "
                "or temporarily unavailable; close the old writer and refresh. "
                "qPlot did not modify the source database or its SQLite sidecars."
            )
        if journal is not None and _journal_names_super_journal(journal):
            raise ReadOnlyDatabaseAccessError(
                "The rollback journal belongs to a multi-database transaction, "
                "which qPlot cannot recover without following files outside its "
                "private snapshot. The database is temporarily unavailable; "
                "finish that transaction and refresh. qPlot did not modify the "
                "source database or its SQLite sidecars."
            )

        if include_wal and not generation_marker:
            # SQLite WAL salts and checksums validate only the WAL's own frame
            # sequence. They do not commit to the base main file, and SQLite
            # will replay a structurally compatible unrelated WAL. Revalidate
            # the observed pair before rejecting it, but do not allocate or
            # copy a private snapshot whose contents can never be trusted.
            _require_expected_database_instance(
                database_path,
                expected_database_identity,
            )
            _require_prepared_database_instance(
                database_path,
                prepared_database_identity,
            )
            after = _source_signature_for_validation(database_path)
            if after != before or not after.stable:
                continue
            raise _unverifiable_unmarked_wal_error(database_path)

        database_is_wal_format = before.database_header[18:20] == b"\x02\x02"
        if not wal_present and journal is None and not database_is_wal_format:
            _require_expected_database_instance(
                database_path,
                expected_database_identity,
            )
            _require_prepared_database_instance(
                database_path,
                prepared_database_identity,
            )
            try:
                after = _source_signature(database_path)
            except OSError as error:
                raise ReadOnlyDatabaseAccessError(
                    f"Could not validate a read-only view of {database_path}: {error}"
                ) from error
            if after != before or not after.stable:
                continue
            return (
                database_path,
                False,
                None,
                False,
                before,
            )

        snapshot = tempfile.TemporaryDirectory(prefix="qplot-readonly-")
        snapshot_path = Path(snapshot.name) / "database.db"
        main_snapshot_path = Path(snapshot.name) / "database-main.db"
        try:
            shutil.copyfile(database_path, snapshot_path)
            if include_wal and generation_marker:
                shutil.copyfile(snapshot_path, main_snapshot_path)
            if include_wal:
                shutil.copyfile(wal_path, _wal_path(snapshot_path))
            if journal is not None:
                shutil.copyfile(journal_path, _journal_path(snapshot_path))
        except FileNotFoundError:
            _cleanup_failed_connection_open(None, snapshot)
            continue
        except OSError as err:
            _cleanup_failed_connection_open(None, snapshot)
            raise ReadOnlyDatabaseAccessError(
                f"Could not copy a read-only view of {database_path}: {err}"
                ) from err

        try:
            after_copy = _source_signature(database_path)
        except OSError as error:
            _cleanup_failed_connection_open(None, snapshot)
            raise ReadOnlyDatabaseAccessError(
                f"Could not validate a read-only copy of {database_path}: {error}"
            ) from error
        if after_copy != before or not after_copy.stable:
            _cleanup_failed_connection_open(None, snapshot)
            continue

        try:
            _require_expected_database_instance(
                database_path,
                expected_database_identity,
            )
            _require_prepared_database_instance(
                database_path,
                prepared_database_identity,
            )
            committed_transactions = None
            if include_wal and generation_marker:
                try:
                    committed_transactions = _committed_wal_transaction_pages(
                        _wal_path(snapshot_path)
                    )
                except (OSError, ValueError) as error:
                    if _source_signature_for_validation(database_path) != before:
                        _cleanup_failed_connection_open(None, snapshot)
                        continue
                    raise _unverifiable_generated_wal_error(
                        database_path,
                        "the WAL has no verifiable committed lineage events",
                    ) from error
            if journal is not None:
                try:
                    _recover_private_rollback_journal(database_path, snapshot_path)
                except ReadOnlyDatabaseAccessError:
                    if _source_signature_for_validation(database_path) != before:
                        _cleanup_failed_connection_open(None, snapshot)
                        continue
                    raise
            if include_wal and generation_marker:
                try:
                    _require_matching_generated_wal_provenance(
                        database_path,
                        main_snapshot_path,
                        snapshot_path,
                        committed_transactions,
                    )
                except UnverifiableDatabaseWalError:
                    if _source_signature_for_validation(database_path) != before:
                        _cleanup_failed_connection_open(None, snapshot)
                        continue
                    if not (ignore_unpaired_wal or quarantined):
                        raise
                    return main_snapshot_path, True, snapshot, True, before

            if _source_signature_for_validation(database_path) != before:
                _cleanup_failed_connection_open(None, snapshot)
                continue
            if include_wal and generation_marker:
                if not _release_wal_quarantine(
                        database_path,
                        prepared_database_identity,
                        ):
                    raise DatabaseInstanceChangedError(
                        "The database was replaced while qPlot was validating "
                        "its WAL provenance."
                    )
            return snapshot_path, not include_wal, snapshot, ignore_wal, before
        except Exception:
            _cleanup_failed_connection_open(None, snapshot)
            raise

    raise ReadOnlyDatabaseAccessError(
        "The database main file or SQLite sidecars changed continuously while "
        "qPlot tried to capture a transaction-consistent snapshot. The database "
        "is busy or temporarily unavailable; refresh to retry. qPlot did not "
        "modify the source database or its SQLite sidecars."
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
