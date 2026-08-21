import os
import sqlite3
import struct
import sys
import tempfile
import threading
import time
import weakref
from contextlib import contextmanager
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


def connect(
        name,
        debug=False,
        version=-1,
        read_only=False,
        *,
        cancelled_callback=None,
        deadline=None,
        ):
    """Open QCoDeS connections while keeping viewer reads under qPlot control."""
    from qcodes.dataset.sqlite.database import connect as qcodes_connect

    if not read_only:
        return qcodes_connect(
            name,
            debug=debug,
            version=version,
            read_only=False,
        )
    return _qcodes_read_only_atomic_connection(
        name,
        debug=debug,
        cancelled_callback=cancelled_callback,
        deadline=deadline,
    )


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
SNAPSHOT_COPY_CHUNK_BYTES = 1024 * 1024
QCODE_SQLITE_BUSY_QUANTUM_SECONDS = 0.05
QCODE_SQLITE_OPEN_TIMEOUT_SECONDS = 5.0
WAL_SNAPSHOT_ATTEMPTS = 5
_ROLLBACK_JOURNAL_MAGIC = b"\xd9\xd5\x05\xf9 \xa1c\xd7"
_ROLLBACK_JOURNAL_PREFIX_BYTES = 4096
_WAL_HEADER_BYTES = 32
_WAL_FRAME_HEADER_BYTES = 24
_WAL_FORMAT_VERSION = 3_007_000
_WAL_MAGIC_LITTLE_ENDIAN_CHECKSUMS = 0x377F0682
_WAL_MAGIC_BIG_ENDIAN_CHECKSUMS = 0x377F0683
_SQLITE_PENDING_BYTE = 0x40000000
_SQLITE_SHARED_FIRST = _SQLITE_PENDING_BYTE + 2
_SQLITE_SHARED_SIZE = 510
_DATABASE_INSTANCE_REGISTRY: dict[
    Path,
    tuple[DatabaseFileIdentity | None, bool],
] = {}
_DATABASE_INSTANCE_REGISTRY_LOCK = threading.Lock()


class ReadOnlyDatabaseAccessError(RuntimeError):
    """Raised when qPlot cannot take a non-mutating view of a database."""


class ReadOnlyDatabaseCancelledError(InterruptedError):
    """Raised when a caller cancels read-only database preparation."""


class DatabaseInstanceChangedError(ReadOnlyDatabaseAccessError):
    """Raised when a database was atomically replaced during a requested read."""


class UnverifiableDatabaseWalError(ReadOnlyDatabaseAccessError):
    """Raised when a WAL cannot be proven to descend from its selected main."""


def _raise_if_read_interrupted(cancelled_callback=None, deadline=None):
    """Stop cooperative work before it can publish a partial read target."""
    if cancelled_callback is not None and cancelled_callback():
        raise ReadOnlyDatabaseCancelledError("Database read cancelled.")
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("Timed out while preparing a read-only database view.")


def _read_control_progress_handler(cancelled_callback=None, deadline=None):
    """Return a SQLite progress handler for the same cooperative controls."""
    if cancelled_callback is None and deadline is None:
        return None

    def interrupted():
        if cancelled_callback is not None and cancelled_callback():
            return 1
        return int(deadline is not None and time.monotonic() >= deadline)

    return interrupted


def _install_read_control_progress_handler(
        connection,
        cancelled_callback=None,
        deadline=None,
        ):
    progress_handler = _read_control_progress_handler(
        cancelled_callback,
        deadline,
    )
    if progress_handler is not None:
        set_progress_handler = getattr(connection, "set_progress_handler", None)
        if callable(set_progress_handler):
            set_progress_handler(progress_handler, 1000)


def _sqlite_timeout_before_deadline(timeout, deadline):
    """Limit SQLite's busy wait to the remaining cooperative deadline."""
    if deadline is None:
        return timeout
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Timed out while preparing a read-only database view.")
    return min(float(timeout), remaining)


def _register_qcodes_sqlite_types(qcodes_database):
    """Register the adapters and converters used by QCoDeS connections."""
    sqlite3.register_adapter(
        qcodes_database.np.ndarray,
        qcodes_database._adapt_array,
    )
    sqlite3.register_converter("array", qcodes_database._convert_array)
    for numpy_int in qcodes_database.numpy_ints:
        sqlite3.register_adapter(numpy_int, int)
    sqlite3.register_converter("numeric", qcodes_database._convert_numeric)
    for numpy_float in (float, *qcodes_database.numpy_floats):
        sqlite3.register_adapter(numpy_float, qcodes_database._adapt_float)
    for complex_type in qcodes_database.complex_types:
        sqlite3.register_adapter(
            complex_type,
            qcodes_database._adapt_complex,
        )
    sqlite3.register_converter("complex", qcodes_database._convert_complex)


def _sqlite_error_is_busy(error):
    """Return whether SQLite reported a retryable lock or busy condition."""
    error_code = getattr(error, "sqlite_errorcode", None)
    if isinstance(error_code, int):
        primary_code = error_code & 0xFF
        return primary_code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    message = str(error).lower()
    return "locked" in message or "busy" in message


def _qcodes_read_only_atomic_connection(
        name,
        *,
        debug=False,
        cancelled_callback=None,
        deadline=None,
        ):
    """Build a latest-schema QCoDeS connection without viewer-side upgrades."""
    from qcodes.dataset.sqlite import database as qcodes_database

    _raise_if_read_interrupted(cancelled_callback, deadline)
    _register_qcodes_sqlite_types(qcodes_database)
    connection = None
    busy_deadline = time.monotonic() + QCODE_SQLITE_OPEN_TIMEOUT_SECONDS
    try:
        connection = sqlite3.connect(
            f"file:{name!s}?mode=ro",
            detect_types=sqlite3.PARSE_DECLTYPES,
            check_same_thread=True,
            timeout=_sqlite_timeout_before_deadline(
                QCODE_SQLITE_BUSY_QUANTUM_SECONDS,
                deadline,
            ),
            uri=True,
            factory=qcodes_database.AtomicConnection,
        )
        _install_read_control_progress_handler(
            connection,
            cancelled_callback,
            deadline,
        )

        while True:
            _raise_if_read_interrupted(cancelled_callback, deadline)
            try:
                version_row = connection.execute("PRAGMA user_version").fetchone()
                connection.execute(
                    "SELECT 1 FROM sqlite_schema LIMIT 1"
                ).fetchone()
            except sqlite3.OperationalError as error:
                _raise_if_read_interrupted(cancelled_callback, deadline)
                if (
                        not _sqlite_error_is_busy(error)
                        or time.monotonic() >= busy_deadline
                        ):
                    raise
                continue
            _raise_if_read_interrupted(cancelled_callback, deadline)
            break

        if (
                version_row is None
                or len(version_row) != 1
                or not isinstance(version_row[0], int)
                ):
            raise RuntimeError(
                f"Database {name} has an invalid SQLite user_version."
            )
        database_version = version_row[0]
        latest_version = qcodes_database._latest_available_version()
        if database_version != latest_version:
            raise RuntimeError(
                f"Database {name} has schema version {database_version}, but "
                f"qPlot requires the latest supported QCoDeS schema version "
                f"{latest_version}. qPlot did not upgrade the input database."
            )
        if debug:
            connection.set_trace_callback(print)
        return connection
    except Exception:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass
        raise


@contextmanager
def _posix_sqlite_shared_lock_bytes(
        database_path,
        *,
        cancelled_callback=None,
        deadline=None,
        ):
    """Hold SQLite's rollback-mode shared-lock bytes without opening SQLite."""
    import errno
    import fcntl

    _raise_if_read_interrupted(cancelled_callback, deadline)
    try:
        database_file = open(database_path, "rb")
    except OSError as error:
        raise ReadOnlyDatabaseAccessError(
            f"Could not open {database_path} for a read-only lock check: {error}"
        ) from error

    pending_locked = False
    shared_locked = False
    try:
        try:
            fcntl.lockf(
                database_file.fileno(),
                fcntl.LOCK_SH | fcntl.LOCK_NB,
                1,
                _SQLITE_PENDING_BYTE,
                os.SEEK_SET,
            )
            pending_locked = True
            _raise_if_read_interrupted(cancelled_callback, deadline)
            fcntl.lockf(
                database_file.fileno(),
                fcntl.LOCK_SH | fcntl.LOCK_NB,
                _SQLITE_SHARED_SIZE,
                _SQLITE_SHARED_FIRST,
                os.SEEK_SET,
            )
            shared_locked = True
            _raise_if_read_interrupted(cancelled_callback, deadline)
        except (ReadOnlyDatabaseCancelledError, TimeoutError):
            raise
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN):
                raise sqlite3.OperationalError("database is locked") from error
            raise ReadOnlyDatabaseAccessError(
                "Could not prove that SQLite shared-lock bytes are available "
                f"for {database_path}: {error}"
            ) from error

        yield database_file
    finally:
        if shared_locked:
            try:
                fcntl.lockf(
                    database_file.fileno(),
                    fcntl.LOCK_UN,
                    _SQLITE_SHARED_SIZE,
                    _SQLITE_SHARED_FIRST,
                    os.SEEK_SET,
                )
            except OSError:
                pass
        if pending_locked:
            try:
                fcntl.lockf(
                    database_file.fileno(),
                    fcntl.LOCK_UN,
                    1,
                    _SQLITE_PENDING_BYTE,
                    os.SEEK_SET,
                )
            except OSError:
                pass
        database_file.close()


@contextmanager
def _windows_sqlite_shared_lock_bytes(
        database_path,
        *,
        cancelled_callback=None,
        deadline=None,
        ):
    """Hold SQLite's Windows shared-lock ranges using nonblocking LockFileEx."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _Overlapped(ctypes.Structure):
        _fields_ = (
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        )

    _raise_if_read_interrupted(cancelled_callback, deadline)
    try:
        database_file = open(database_path, "rb")
    except OSError as error:
        raise ReadOnlyDatabaseAccessError(
            f"Could not open {database_path} for a read-only lock check: {error}"
        ) from error

    kernel32 = ctypes.WinDLL(  # type: ignore[attr-defined]
        "kernel32",
        use_last_error=True,
    )
    lock_file_ex = kernel32.LockFileEx
    lock_file_ex.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    )
    lock_file_ex.restype = wintypes.BOOL
    unlock_file_ex = kernel32.UnlockFileEx
    unlock_file_ex.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    )
    unlock_file_ex.restype = wintypes.BOOL
    windows_handle = wintypes.HANDLE(
        msvcrt.get_osfhandle(  # type: ignore[attr-defined]
            database_file.fileno()
        )
    )
    locked_ranges = []

    def lock_shared_range(offset, length):
        overlapped = _Overlapped()
        overlapped.Offset = offset & 0xFFFFFFFF
        overlapped.OffsetHigh = (offset >> 32) & 0xFFFFFFFF
        if not lock_file_ex(
                windows_handle,
                0x00000001,
                0,
                length & 0xFFFFFFFF,
                (length >> 32) & 0xFFFFFFFF,
                ctypes.byref(overlapped),
                ):
            error_code = ctypes.get_last_error()  # type: ignore[attr-defined]
            if error_code in (32, 33, 997):
                raise sqlite3.OperationalError("database is locked")
            raise ReadOnlyDatabaseAccessError(
                "Could not prove that SQLite shared-lock bytes are available "
                f"for {database_path}: Windows error {error_code}."
            )
        locked_ranges.append((overlapped, length))

    try:
        lock_shared_range(_SQLITE_PENDING_BYTE, 1)
        _raise_if_read_interrupted(cancelled_callback, deadline)
        lock_shared_range(_SQLITE_SHARED_FIRST, _SQLITE_SHARED_SIZE)
        _raise_if_read_interrupted(cancelled_callback, deadline)
        yield database_file
    finally:
        for overlapped, length in reversed(locked_ranges):
            unlock_file_ex(
                windows_handle,
                0,
                length & 0xFFFFFFFF,
                (length >> 32) & 0xFFFFFFFF,
                ctypes.byref(overlapped),
            )
        database_file.close()


@contextmanager
def _sqlite_shared_lock_bytes(
        database_path,
        *,
        cancelled_callback=None,
        deadline=None,
        ):
    """Hold the platform's SQLite shared-lock ranges or fail closed."""
    if os.name == "posix":
        lock_context = _posix_sqlite_shared_lock_bytes(
            database_path,
            cancelled_callback=cancelled_callback,
            deadline=deadline,
        )
    elif os.name == "nt":
        lock_context = _windows_sqlite_shared_lock_bytes(
            database_path,
            cancelled_callback=cancelled_callback,
            deadline=deadline,
        )
    else:
        raise ReadOnlyDatabaseAccessError(
            "qPlot cannot perform a non-mutating SQLite lock check on this "
            "platform. Database access was rejected without opening a mutable "
            "SQLite view."
        )
    with lock_context as database_file:
        yield database_file


def _source_matches_under_sqlite_lock(
        database_path,
        expected_source,
        database_file,
        *,
        cancelled_callback=None,
        deadline=None,
        ):
    """Validate a source while never reopening its locked main file."""
    _raise_if_read_interrupted(cancelled_callback, deadline)
    try:
        descriptor_signature_before = _stat_signature(
            os.fstat(database_file.fileno())
        )
        descriptor_identity_before = open_file_identity(database_file.fileno())
        path_signature_before = _file_signature(database_path)
        path_identity_before = path_bound_file_identity(database_path)
    except (ReadOnlyDatabaseCancelledError, TimeoutError):
        raise
    except OSError as error:
        raise ReadOnlyDatabaseAccessError(
            "Could not validate the database main file under its SQLite "
            f"shared lock: {error}"
        ) from error

    try:
        wal_signature, wal_identity, wal_stable = _wal_observation(
            database_path,
            cancelled_callback=cancelled_callback,
            deadline=deadline,
        )
        journal = _rollback_journal_observation(
            database_path,
            cancelled_callback=cancelled_callback,
            deadline=deadline,
        )
    except (ReadOnlyDatabaseCancelledError, TimeoutError):
        raise
    except OSError as error:
        raise ReadOnlyDatabaseAccessError(
            "Could not validate SQLite sidecars while holding the database "
            f"shared lock: {error}"
        ) from error

    _raise_if_read_interrupted(cancelled_callback, deadline)
    try:
        descriptor_signature_after = _stat_signature(
            os.fstat(database_file.fileno())
        )
        descriptor_identity_after = open_file_identity(database_file.fileno())
        path_signature_after = _file_signature(database_path)
        path_identity_after = path_bound_file_identity(database_path)
    except (ReadOnlyDatabaseCancelledError, TimeoutError):
        raise
    except OSError as error:
        raise ReadOnlyDatabaseAccessError(
            "Could not finish validating the database main file under its "
            f"SQLite shared lock: {error}"
        ) from error

    main_file_stable = _path_and_descriptor_observations_match(
        path_signature_before,
        path_identity_before,
        descriptor_signature_before,
        descriptor_identity_before,
        descriptor_signature_after,
        descriptor_identity_after,
        path_signature_after,
        path_identity_after,
    )
    return (
        expected_source.database is not None
        and main_file_stable
        and path_signature_before
        == path_signature_after
        == expected_source.database
        and path_identity_before
        == path_identity_after
        == expected_source.database_identity
        and wal_stable
        and wal_signature == expected_source.wal
        and wal_identity == expected_source.wal_identity
        and journal == expected_source.journal
        and (journal is None or journal.stable)
    )


def _copy_file_cooperatively(
        source,
        destination,
        *,
        cancelled_callback=None,
        deadline=None,
        ):
    """Copy one snapshot artifact in bounded, cooperatively checked chunks."""
    _raise_if_read_interrupted(cancelled_callback, deadline)
    if _clone_file_if_supported(source, destination):
        _raise_if_read_interrupted(cancelled_callback, deadline)
        return destination
    with open(source, "rb") as source_file, open(destination, "wb") as destination_file:
        while True:
            _raise_if_read_interrupted(cancelled_callback, deadline)
            chunk = source_file.read(SNAPSHOT_COPY_CHUNK_BYTES)
            _raise_if_read_interrupted(cancelled_callback, deadline)
            if not chunk:
                break
            written = destination_file.write(chunk)
            if written != len(chunk):
                raise OSError(
                    f"Short write while copying {source} to a private snapshot."
                )
            _raise_if_read_interrupted(cancelled_callback, deadline)
    _raise_if_read_interrupted(cancelled_callback, deadline)
    return destination


def _clone_file_if_supported(source, destination):
    """Create a copy-on-write clone without weakening snapshot isolation."""
    destination_path = Path(destination)
    destination_existed = destination_path.exists()
    if sys.platform == "darwin":
        import ctypes

        clonefile = ctypes.CDLL(None, use_errno=True).clonefile
        clonefile.argtypes = (
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_int,
        )
        clonefile.restype = ctypes.c_int
        result = clonefile(
            os.fsencode(source),
            os.fsencode(destination),
            0,
        )
        if result == 0:
            return True
    elif sys.platform.startswith("linux"):
        import fcntl

        try:
            with (
                open(source, "rb") as source_file,
                open(destination, "xb") as destination_file,
            ):
                fcntl.ioctl(
                    destination_file.fileno(),
                    0x40049409,  # Linux FICLONE
                    source_file.fileno(),
                )
            return True
        except OSError:
            pass
    else:
        return False

    if not destination_existed:
        try:
            destination_path.unlink()
        except FileNotFoundError:
            pass
    return False


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


@dataclass(frozen=True, slots=True)
class _SourceReadPolicy:
    """Sidecar policy derived from one stable source observation."""

    journal: _RollbackJournalObservation | None
    wal_present: bool
    quarantined: bool
    ignore_wal: bool
    include_wal: bool
    database_is_wal_format: bool

    @property
    def requires_private_snapshot(self):
        """Classify states unsuitable for the probe's direct lock-byte check.

        Full viewer openers snapshot every source state. This narrower flag is
        retained only so the isolated access probe never treats a journal- or
        WAL-bearing source as a rollback-mode direct-lock candidate.
        """
        return (
            self.journal is not None
            or self.wal_present
            or self.database_is_wal_format
        )


class _ManagedSQLiteConnection(sqlite3.Connection):
    """SQLite connection that owns its temporary database snapshot."""

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
        cancelled_callback=None,
        deadline=None,
        ):
    """Open an AtomicConnection-compatible private database snapshot.

    Every source state is copied under the system temporary directory before
    SQLite is invoked. Immutable access, WAL handling, and rollback recovery
    therefore occur only on qPlot's private files.
    """
    _raise_if_read_interrupted(cancelled_callback, deadline)
    source_path = _resolved_database_path(database_path)
    for _attempt in range(2):
        _raise_if_read_interrupted(cancelled_callback, deadline)
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
            cancelled_callback=cancelled_callback,
            deadline=deadline,
        )
        conn = None
        try:
            _raise_if_read_interrupted(cancelled_callback, deadline)
            if not _prepared_source_is_current(
                    source_path,
                    prepared_source,
                    direct=snapshot is None,
                    ignore_wal=ignore_wal,
                    cancelled_callback=cancelled_callback,
                    deadline=deadline,
                    ):
                if snapshot is not None:
                    _cleanup_failed_connection_open(None, snapshot)
                continue
            _require_publication_complete(source_path)
            conn = connect(
                _qcodes_uri_path(target_path, immutable=immutable),
                get_DB_debug(),
                read_only=True,
                cancelled_callback=cancelled_callback,
                deadline=deadline,
                )
            _install_read_control_progress_handler(
                conn,
                cancelled_callback,
                deadline,
            )
            _raise_if_read_interrupted(cancelled_callback, deadline)
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
                    cancelled_callback=cancelled_callback,
                    deadline=deadline,
                    ):
                _cleanup_failed_connection_open(conn, snapshot)
                continue
            _require_publication_complete(source_path)
            conn.path_to_dbfile = str(source_path)
            if snapshot is not None:
                _attach_snapshot_cleanup(conn, snapshot)
                snapshot = None
            configured = configure_read_only_sqlite_connection(conn)
            _raise_if_read_interrupted(cancelled_callback, deadline)
            return configured
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
        cancelled_callback=None,
        deadline=None,
        ):
    """Load a QCoDeS dataset by GUID through a read-only connection."""
    if database_path is None:
        database_path = get_DB_location()
    conn = qcodes_read_only_connection(
        database_path,
        expected_database_identity=expected_database_identity,
        cancelled_callback=cancelled_callback,
        deadline=deadline,
    )
    connection_transferred = False
    try:
        _raise_if_read_interrupted(cancelled_callback, deadline)
        result = load_by_guid(guid, conn=conn)
        _raise_if_read_interrupted(cancelled_callback, deadline)
        connection_transferred = result_owns_supplied_connection(result)
        return result
    except Exception:
        _raise_if_read_interrupted(cancelled_callback, deadline)
        raise
    finally:
        if not connection_transferred:
            conn.close()


def load_by_id_read_only(
        run_id,
        database_path=None,
        *,
        expected_database_identity=None,
        cancelled_callback=None,
        deadline=None,
        ):
    """Load a QCoDeS dataset by run ID through a read-only connection."""
    if database_path is None:
        database_path = get_DB_location()
    conn = qcodes_read_only_connection(
        database_path,
        expected_database_identity=expected_database_identity,
        cancelled_callback=cancelled_callback,
        deadline=deadline,
    )
    connection_transferred = False
    try:
        _raise_if_read_interrupted(cancelled_callback, deadline)
        result = load_by_id(run_id, conn=conn)
        _raise_if_read_interrupted(cancelled_callback, deadline)
        connection_transferred = result_owns_supplied_connection(result)
        return result
    except Exception:
        _raise_if_read_interrupted(cancelled_callback, deadline)
        raise
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
        cancelled_callback=None,
        deadline=None,
        **kwargs,
        ):
    """Open a source-preserving SQLite connection to a private snapshot.

    Every source state uses a consistency-checked private copy. SQLite can
    therefore use immutable access, follow a WAL, or recover a hot journal
    without opening or changing the source database and its sidecars.
    """
    _raise_if_read_interrupted(cancelled_callback, deadline)
    source_path = _resolved_database_path(database_path)
    kwargs.setdefault("factory", _ManagedSQLiteConnection)

    for _attempt in range(2):
        _raise_if_read_interrupted(cancelled_callback, deadline)
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
            cancelled_callback=cancelled_callback,
            deadline=deadline,
        )
        conn = None
        try:
            _raise_if_read_interrupted(cancelled_callback, deadline)
            if not _prepared_source_is_current(
                    source_path,
                    prepared_source,
                    direct=snapshot is None,
                    ignore_wal=ignore_wal,
                    cancelled_callback=cancelled_callback,
                    deadline=deadline,
                    ):
                if snapshot is not None:
                    _cleanup_failed_connection_open(None, snapshot)
                continue
            _require_publication_complete(source_path)
            conn = sqlite3.connect(
                sqlite_read_only_uri(target_path, immutable=immutable),
                timeout=_sqlite_timeout_before_deadline(timeout, deadline),
                uri=True,
                **kwargs,
                )
            _install_read_control_progress_handler(
                conn,
                cancelled_callback,
                deadline,
            )
            _raise_if_read_interrupted(cancelled_callback, deadline)
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
                    cancelled_callback=cancelled_callback,
                    deadline=deadline,
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
            configured = configure_read_only_sqlite_connection(conn)
            _raise_if_read_interrupted(cancelled_callback, deadline)
            return configured
        except Exception:
            _cleanup_failed_connection_open(conn, snapshot)
            raise

    raise ReadOnlyDatabaseAccessError(
        "The database became busy while qPlot was opening a transaction-consistent "
        "view. It is temporarily unavailable; refresh to retry. qPlot did not "
        "modify the source database or its SQLite sidecars."
        )


def _probe_sqlite_user_version(
        database_path,
        *,
        cancelled_callback=None,
        deadline=None,
        ):
    """Perform one bounded immutable read without ever following sidecars.

    Lock availability is checked separately through the platform lock-byte
    protocol when the isolated access-probe child requests it. Keeping this
    SQLite connection immutable prevents a DELETE-to-WAL race from creating a
    source ``-shm`` file between signature observation and connection opening.
    """
    connection = None
    try:
        _raise_if_read_interrupted(cancelled_callback, deadline)
        connection = sqlite3.connect(
            sqlite_read_only_uri(database_path, immutable=True),
            timeout=_sqlite_timeout_before_deadline(1, deadline),
            uri=True,
        )
        _install_read_control_progress_handler(
            connection,
            cancelled_callback,
            deadline,
        )
        connection.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone()
        _raise_if_read_interrupted(cancelled_callback, deadline)
    except sqlite3.Error:
        _raise_if_read_interrupted(cancelled_callback, deadline)
        raise
    finally:
        if connection is not None:
            connection.close()


def probe_read_only_database(
        database_path,
        *,
        ignore_unpaired_wal=False,
        expected_database_identity=None,
        cancelled_callback=None,
        deadline=None,
        _check_sqlite_lock_bytes=False,
        ):
    """Promptly check access and identity without building a full snapshot.

    The real opener remains responsible for transaction-consistent snapshot
    creation and generated-WAL provenance.  This probe deliberately touches
    only bounded regions of source artifacts and opens SQLite immutable, so a
    concurrent transition to WAL cannot create source sidecars. The isolated
    access-check child can additionally request a platform lock-byte check for
    a sidecar-free rollback-mode source.
    """
    _raise_if_read_interrupted(cancelled_callback, deadline)
    source_path = _resolved_database_path(database_path)
    _require_publication_complete(source_path)
    if not source_path.is_file():
        raise FileNotFoundError(source_path)

    _require_expected_database_instance(
        source_path,
        expected_database_identity,
    )
    prepared_database_identity = database_file_identity(source_path)
    if prepared_database_identity is None:
        raise FileNotFoundError(source_path)

    try:
        generation_marker = database_has_qplot_generation_marker(source_path)
    except (ReadOnlyDatabaseCancelledError, TimeoutError):
        raise
    except OSError as error:
        raise ReadOnlyDatabaseAccessError(
            f"Could not inspect the database publication marker: {error}"
        ) from error

    for _attempt in range(WAL_SNAPSHOT_ATTEMPTS):
        _raise_if_read_interrupted(cancelled_callback, deadline)
        _require_expected_database_instance(
            source_path,
            expected_database_identity,
        )
        try:
            before = _source_signature_with_controls(
                source_path,
                cancelled_callback,
                deadline,
            )
        except (ReadOnlyDatabaseCancelledError, TimeoutError):
            raise
        except OSError as error:
            raise ReadOnlyDatabaseAccessError(
                f"Could not inspect a read-only view of {source_path}: {error}"
            ) from error
        if before.database is None:
            raise FileNotFoundError(source_path)
        if not before.stable:
            continue

        policy = _source_read_policy(
            source_path,
            before,
            generation_marker=generation_marker,
            ignore_unpaired_wal=ignore_unpaired_wal,
        )
        if policy.include_wal and not generation_marker:
            _require_expected_database_instance(
                source_path,
                expected_database_identity,
            )
            _require_prepared_database_instance(
                source_path,
                prepared_database_identity,
            )
            after = _source_signature_for_validation(
                source_path,
                cancelled_callback=cancelled_callback,
                deadline=deadline,
            )
            if after != before or not after.stable:
                continue
            raise _unverifiable_unmarked_wal_error(source_path)

        if not _bounded_artifact_is_current(
                source_path,
                before.database,
                before.database_identity,
                cancelled_callback=cancelled_callback,
                deadline=deadline,
                ):
            continue
        if (
                before.wal is not None
                and not _bounded_artifact_is_current(
                    _wal_path(source_path),
                    before.wal,
                    before.wal_identity,
                    cancelled_callback=cancelled_callback,
                    deadline=deadline,
                )
                ):
            continue

        def source_is_still_current(expected_source=before):
            _require_publication_complete(source_path)
            _require_expected_database_instance(
                source_path,
                expected_database_identity,
            )
            _require_prepared_database_instance(
                source_path,
                prepared_database_identity,
            )
            after = _source_signature_for_validation(
                source_path,
                cancelled_callback=cancelled_callback,
                deadline=deadline,
            )
            return after == expected_source and after.stable

        sqlite_error = None
        try:
            _probe_sqlite_user_version(
                source_path,
                cancelled_callback=cancelled_callback,
                deadline=deadline,
            )
        except (ReadOnlyDatabaseCancelledError, TimeoutError):
            raise
        except sqlite3.Error as error:
            sqlite_error = error

        # This is deliberately the last full validation before a platform
        # lock-byte check. On POSIX, closing *any* descriptor for an inode drops
        # all process-owned fcntl locks for that inode, so no helper that opens
        # the main file may run after the lock context is entered.
        if not source_is_still_current():
            continue
        if sqlite_error is not None:
            raise sqlite_error

        if (
                not _check_sqlite_lock_bytes
                or policy.requires_private_snapshot
                ):
            _raise_if_read_interrupted(cancelled_callback, deadline)
            return

        try:
            with _sqlite_shared_lock_bytes(
                    source_path,
                    cancelled_callback=cancelled_callback,
                    deadline=deadline,
                    ) as database_file:
                _require_publication_complete(source_path)
                _require_expected_database_instance(
                    source_path,
                    expected_database_identity,
                )
                _require_prepared_database_instance(
                    source_path,
                    prepared_database_identity,
                )
                if not _source_matches_under_sqlite_lock(
                        source_path,
                        before,
                        database_file,
                        cancelled_callback=cancelled_callback,
                        deadline=deadline,
                        ):
                    continue
                _raise_if_read_interrupted(cancelled_callback, deadline)
                return
        except (ReadOnlyDatabaseCancelledError, TimeoutError):
            raise
        except sqlite3.Error:
            # A concurrent journal-mode or file transition can look exactly
            # like lock contention. Revalidate after releasing any partial OS
            # locks; reject the lock only when the source is still identical.
            if not source_is_still_current():
                continue
            raise

    raise ReadOnlyDatabaseAccessError(
        "The database main file or SQLite sidecars changed continuously while "
        "qPlot checked read-only access. The database is busy or temporarily "
        "unavailable; refresh to retry. qPlot did not modify the source database "
        "or its SQLite sidecars."
    )


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


def _file_prefix_observation(
        path,
        byte_count,
        *,
        include_trailer=False,
        cancelled_callback=None,
        deadline=None,
        ):
    """Read identifying bytes while proving which path instance supplied them."""
    _raise_if_read_interrupted(cancelled_callback, deadline)
    path_identity_before = path_bound_file_identity(path)
    path_signature_before = _file_signature(path)
    if path_signature_before is None:
        return None, None, b"", b"", True
    try:
        with path.open("rb") as handle:
            _raise_if_read_interrupted(cancelled_callback, deadline)
            descriptor_identity_before = open_file_identity(handle.fileno())
            descriptor_before = _stat_signature(os.fstat(handle.fileno()))
            prefix = handle.read(byte_count)
            _raise_if_read_interrupted(cancelled_callback, deadline)
            trailer = b""
            if include_trailer and descriptor_before[2] >= 16:
                handle.seek(-16, os.SEEK_END)
                trailer = handle.read(16)
                _raise_if_read_interrupted(cancelled_callback, deadline)
            descriptor_after = _stat_signature(os.fstat(handle.fileno()))
            descriptor_identity_after = open_file_identity(handle.fileno())
    except FileNotFoundError:
        return path_signature_before, path_identity_before, b"", b"", False
    _raise_if_read_interrupted(cancelled_callback, deadline)
    path_signature_after = _file_signature(path)
    path_identity_after = path_bound_file_identity(path)
    stable = _path_and_descriptor_observations_match(
        path_signature_before,
        path_identity_before,
        descriptor_before,
        descriptor_identity_before,
        descriptor_after,
        descriptor_identity_after,
        path_signature_after,
        path_identity_after,
    )
    observed_signature = (
        path_signature_after
        if path_signature_after is not None
        else path_signature_before
    )
    return observed_signature, path_identity_after, prefix, trailer, stable


def _path_and_descriptor_observations_match(
        path_signature_before,
        path_identity_before,
        descriptor_signature_before,
        descriptor_identity_before,
        descriptor_signature_after,
        descriptor_identity_after,
        path_signature_after,
        path_identity_after,
        ):
    """Compare path and descriptor observations without mixing stat domains."""
    signatures = (
        path_signature_before,
        descriptor_signature_before,
        descriptor_signature_after,
        path_signature_after,
    )
    if any(signature is None for signature in signatures):
        return False

    stable_metadata = (
        path_signature_before == path_signature_after
        and descriptor_signature_before == descriptor_signature_after
        and path_signature_before[2:4] == descriptor_signature_before[2:4]
        and path_signature_after[2:4] == descriptor_signature_after[2:4]
    )
    identities = (
        path_identity_before,
        descriptor_identity_before,
        descriptor_identity_after,
        path_identity_after,
    )
    if all(identity is not None for identity in identities):
        return stable_metadata and len(set(identities)) == 1
    if any(identity is not None for identity in identities):
        return False
    return len(set(signatures)) == 1


def _bounded_artifact_is_current(
        artifact_path,
        expected_signature,
        expected_identity,
        *,
        cancelled_callback=None,
        deadline=None,
        ):
    """Touch an artifact's beginning and end while retaining its identity."""
    signature, identity, _prefix, _trailer, stable = _file_prefix_observation(
        artifact_path,
        _WAL_HEADER_BYTES,
        include_trailer=True,
        cancelled_callback=cancelled_callback,
        deadline=deadline,
    )
    _raise_if_read_interrupted(cancelled_callback, deadline)
    return (
        stable
        and signature == expected_signature
        and identity == expected_identity
    )


def _rollback_journal_observation(
        database_path,
        *,
        cancelled_callback=None,
        deadline=None,
        ):
    journal_path = _journal_path(database_path)
    _raise_if_read_interrupted(cancelled_callback, deadline)
    signature, identity, prefix, trailer, stable = _file_prefix_observation(
        journal_path,
        _ROLLBACK_JOURNAL_PREFIX_BYTES,
        include_trailer=True,
        cancelled_callback=cancelled_callback,
        deadline=deadline,
    )
    if signature is None:
        return None
    _raise_if_read_interrupted(cancelled_callback, deadline)
    return _RollbackJournalObservation(
        file_signature=signature,
        file_identity=identity,
        prefix=prefix,
        trailer=trailer,
        stable=stable,
    )


def _wal_observation(
        database_path,
        *,
        cancelled_callback=None,
        deadline=None,
        ):
    """Observe one path-bound WAL instance without opening it through SQLite."""
    wal_path = _wal_path(database_path)
    _raise_if_read_interrupted(cancelled_callback, deadline)
    identity_before = path_bound_file_identity(wal_path)
    signature_before = _file_signature(wal_path)
    _raise_if_read_interrupted(cancelled_callback, deadline)
    signature_after = _file_signature(wal_path)
    identity_after = path_bound_file_identity(wal_path)
    return (
        signature_after,
        identity_after,
        identity_before == identity_after and signature_before == signature_after,
    )


def _source_signature(
        database_path,
        *,
        cancelled_callback=None,
        deadline=None,
        ):
    _raise_if_read_interrupted(cancelled_callback, deadline)
    (
        database_signature,
        database_identity,
        database_header,
        _trailer,
        database_stable,
    ) = (
        _file_prefix_observation(
            database_path,
            100,
            cancelled_callback=cancelled_callback,
            deadline=deadline,
        )
    )
    wal_signature, wal_identity, wal_stable = _wal_observation(
        database_path,
        cancelled_callback=cancelled_callback,
        deadline=deadline,
    )
    journal = _rollback_journal_observation(
        database_path,
        cancelled_callback=cancelled_callback,
        deadline=deadline,
    )
    _raise_if_read_interrupted(cancelled_callback, deadline)
    return _SourceSignature(
        database=database_signature,
        database_identity=database_identity,
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


def _source_signature_with_controls(
        database_path,
        cancelled_callback=None,
        deadline=None,
        ):
    """Keep no-control instrumentation compatible with the legacy call shape."""
    if cancelled_callback is None and deadline is None:
        return _source_signature(database_path)
    return _source_signature(
        database_path,
        cancelled_callback=cancelled_callback,
        deadline=deadline,
    )


def _signature_requires_private_snapshot(signature, *, ignore_wal):
    return (
        not signature.stable
        or signature.journal is not None
        or (signature.wal is not None and not ignore_wal)
    )


def _source_read_policy(
        database_path,
        signature,
        *,
        generation_marker,
        ignore_unpaired_wal,
        ):
    """Apply the shared fail-closed sidecar policy to one observation."""
    journal = signature.journal
    wal_present = signature.wal is not None
    empty_wal = wal_present and signature.wal[2] == 0
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

    return _SourceReadPolicy(
        journal=journal,
        wal_present=wal_present,
        quarantined=quarantined,
        ignore_wal=ignore_wal,
        include_wal=include_wal,
        database_is_wal_format=(
            signature.database_header[18:20] == b"\x02\x02"
        ),
    )


def _prepared_source_is_current(
        database_path,
        prepared_source,
        *,
        direct,
        ignore_wal,
        cancelled_callback=None,
        deadline=None,
        ):
    """Validate the exact main/journal instance through connection opening."""
    try:
        current_source = _source_signature_with_controls(
            database_path,
            cancelled_callback,
            deadline,
        )
    except (ReadOnlyDatabaseCancelledError, TimeoutError):
        raise
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


def _source_signature_for_validation(
        database_path,
        *,
        cancelled_callback=None,
        deadline=None,
        ):
    """Read a validation signature while keeping filesystem errors explicit."""
    try:
        return _source_signature_with_controls(
            database_path,
            cancelled_callback,
            deadline,
        )
    except (ReadOnlyDatabaseCancelledError, TimeoutError):
        raise
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


def _committed_wal_transaction_pages(
        wal_path,
        *,
        cancelled_callback=None,
        deadline=None,
        ):
    """Return page sets for SQLite's valid committed private-WAL prefix.

    WAL files can retain invalid frames from an earlier checkpoint cycle.  The
    checksum scan mirrors SQLite recovery: it stops at the first invalid frame
    and ignores valid but uncommitted tail frames after the last commit marker.
    """
    _raise_if_read_interrupted(cancelled_callback, deadline)
    with open(wal_path, "rb") as wal_file:
        header = wal_file.read(_WAL_HEADER_BYTES)
        _raise_if_read_interrupted(cancelled_callback, deadline)
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
            _raise_if_read_interrupted(cancelled_callback, deadline)
            frame_header = wal_file.read(_WAL_FRAME_HEADER_BYTES)
            _raise_if_read_interrupted(cancelled_callback, deadline)
            if not frame_header:
                break
            if len(frame_header) != _WAL_FRAME_HEADER_BYTES:
                break
            page = wal_file.read(page_size)
            _raise_if_read_interrupted(cancelled_callback, deadline)
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

    _raise_if_read_interrupted(cancelled_callback, deadline)
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
        *,
        cancelled_callback=None,
        deadline=None,
        ):
    """Require a private WAL view to carry this generated main's lineage."""
    main_connection = None
    snapshot_connection = None
    try:
        _raise_if_read_interrupted(cancelled_callback, deadline)
        if committed_transactions is None:
            committed_transactions = _committed_wal_transaction_pages(
                _wal_path(snapshot_path),
                cancelled_callback=cancelled_callback,
                deadline=deadline,
            )
        main_connection = sqlite3.connect(
            sqlite_read_only_uri(main_snapshot_path, immutable=True),
            uri=True,
        )
        _install_read_control_progress_handler(
            main_connection,
            cancelled_callback,
            deadline,
        )
        _raise_if_read_interrupted(cancelled_callback, deadline)
        main_provenance = _generation_provenance(main_connection)
        _raise_if_read_interrupted(cancelled_callback, deadline)
        main_lineage = _generation_lineage_state(main_connection)
        _raise_if_read_interrupted(cancelled_callback, deadline)
        main_lineage_root = _lineage_state_root_page(main_connection)
        _require_generation_lineage_trigger_inventory(main_connection)
        _raise_if_read_interrupted(cancelled_callback, deadline)
        snapshot_connection = sqlite3.connect(snapshot_path)
        _install_read_control_progress_handler(
            snapshot_connection,
            cancelled_callback,
            deadline,
        )
        _raise_if_read_interrupted(cancelled_callback, deadline)
        snapshot_provenance = _generation_provenance(snapshot_connection)
        _raise_if_read_interrupted(cancelled_callback, deadline)
        snapshot_lineage = _generation_lineage_state(snapshot_connection)
        _raise_if_read_interrupted(cancelled_callback, deadline)
        snapshot_lineage_root = _lineage_state_root_page(snapshot_connection)
        _require_generation_lineage_trigger_inventory(snapshot_connection)
        _raise_if_read_interrupted(cancelled_callback, deadline)
    except (ReadOnlyDatabaseCancelledError, TimeoutError):
        raise
    except (sqlite3.Error, ValueError) as error:
        _raise_if_read_interrupted(cancelled_callback, deadline)
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
            _raise_if_read_interrupted(cancelled_callback, deadline)
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
        except sqlite3.Error as error:
            _raise_if_read_interrupted(cancelled_callback, deadline)
            raise _unverifiable_generated_wal_error(
                database_path,
                "the WAL lineage branch could not be validated",
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


def _recover_private_rollback_journal(
        database_path,
        snapshot_path,
        *,
        cancelled_callback=None,
        deadline=None,
        ):
    """Let SQLite resolve a copied journal without ever opening the source."""
    connection = None
    try:
        _raise_if_read_interrupted(cancelled_callback, deadline)
        connection = sqlite3.connect(
            f"file:{_sqlite_uri_path(snapshot_path)}?mode=rw",
            uri=True,
            timeout=0,
        )
        _install_read_control_progress_handler(
            connection,
            cancelled_callback,
            deadline,
        )
        _raise_if_read_interrupted(cancelled_callback, deadline)
        connection.execute("PRAGMA query_only = ON")
        connection.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone()
        _raise_if_read_interrupted(cancelled_callback, deadline)
    except (ReadOnlyDatabaseCancelledError, TimeoutError):
        raise
    except sqlite3.Error as error:
        _raise_if_read_interrupted(cancelled_callback, deadline)
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
        cancelled_callback=None,
        deadline=None,
    ):
    """Capture a stable private SQLite snapshot for every viewer read."""
    _raise_if_read_interrupted(cancelled_callback, deadline)
    _require_publication_complete(database_path)
    if not database_path.is_file():
        raise FileNotFoundError(database_path)

    _require_expected_database_instance(
        database_path,
        expected_database_identity,
    )
    _raise_if_read_interrupted(cancelled_callback, deadline)
    prepared_database_identity = database_file_identity(database_path)
    if prepared_database_identity is None:
        raise FileNotFoundError(database_path)

    try:
        generation_marker = database_has_qplot_generation_marker(database_path)
    except (ReadOnlyDatabaseCancelledError, TimeoutError):
        raise
    except OSError as error:
        raise ReadOnlyDatabaseAccessError(
            f"Could not inspect the database publication marker: {error}"
        ) from error

    wal_path = _wal_path(database_path)
    journal_path = _journal_path(database_path)

    for _attempt in range(WAL_SNAPSHOT_ATTEMPTS):
        _raise_if_read_interrupted(cancelled_callback, deadline)
        _require_expected_database_instance(
            database_path,
            expected_database_identity,
        )
        try:
            before = _source_signature_with_controls(
                database_path,
                cancelled_callback,
                deadline,
            )
        except (ReadOnlyDatabaseCancelledError, TimeoutError):
            raise
        except OSError as error:
            raise ReadOnlyDatabaseAccessError(
                f"Could not inspect a read-only view of {database_path}: {error}"
            ) from error
        if before.database is None:
            raise FileNotFoundError(database_path)
        if not before.stable:
            continue

        policy = _source_read_policy(
            database_path,
            before,
            generation_marker=generation_marker,
            ignore_unpaired_wal=ignore_unpaired_wal,
        )
        journal = policy.journal
        quarantined = policy.quarantined
        ignore_wal = policy.ignore_wal
        include_wal = policy.include_wal

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
            after = _source_signature_for_validation(
                database_path,
                cancelled_callback=cancelled_callback,
                deadline=deadline,
            )
            if after != before or not after.stable:
                continue
            raise _unverifiable_unmarked_wal_error(database_path)

        snapshot = tempfile.TemporaryDirectory(prefix="qplot-readonly-")
        snapshot_path = Path(snapshot.name) / "database.db"
        main_snapshot_path = Path(snapshot.name) / "database-main.db"
        try:
            _copy_file_cooperatively(
                database_path,
                snapshot_path,
                cancelled_callback=cancelled_callback,
                deadline=deadline,
            )
            if include_wal and generation_marker:
                _copy_file_cooperatively(
                    snapshot_path,
                    main_snapshot_path,
                    cancelled_callback=cancelled_callback,
                    deadline=deadline,
                )
            if include_wal:
                _copy_file_cooperatively(
                    wal_path,
                    _wal_path(snapshot_path),
                    cancelled_callback=cancelled_callback,
                    deadline=deadline,
                )
            if journal is not None:
                _copy_file_cooperatively(
                    journal_path,
                    _journal_path(snapshot_path),
                    cancelled_callback=cancelled_callback,
                    deadline=deadline,
                )
        except (ReadOnlyDatabaseCancelledError, TimeoutError):
            _cleanup_failed_connection_open(None, snapshot)
            raise
        except FileNotFoundError:
            _cleanup_failed_connection_open(None, snapshot)
            continue
        except OSError as err:
            _cleanup_failed_connection_open(None, snapshot)
            raise ReadOnlyDatabaseAccessError(
                f"Could not copy a read-only view of {database_path}: {err}"
                ) from err

        try:
            after_copy = _source_signature_with_controls(
                database_path,
                cancelled_callback,
                deadline,
            )
        except (ReadOnlyDatabaseCancelledError, TimeoutError):
            _cleanup_failed_connection_open(None, snapshot)
            raise
        except OSError as error:
            _cleanup_failed_connection_open(None, snapshot)
            raise ReadOnlyDatabaseAccessError(
                f"Could not validate a read-only copy of {database_path}: {error}"
            ) from error
        if after_copy != before or not after_copy.stable:
            _cleanup_failed_connection_open(None, snapshot)
            continue

        try:
            _raise_if_read_interrupted(cancelled_callback, deadline)
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
                        _wal_path(snapshot_path),
                        cancelled_callback=cancelled_callback,
                        deadline=deadline,
                    )
                except (ReadOnlyDatabaseCancelledError, TimeoutError):
                    raise
                except (OSError, ValueError) as error:
                    if _source_signature_for_validation(
                            database_path,
                            cancelled_callback=cancelled_callback,
                            deadline=deadline,
                            ) != before:
                        _cleanup_failed_connection_open(None, snapshot)
                        continue
                    raise _unverifiable_generated_wal_error(
                        database_path,
                        "the WAL has no verifiable committed lineage events",
                    ) from error
            if journal is not None:
                try:
                    _recover_private_rollback_journal(
                        database_path,
                        snapshot_path,
                        cancelled_callback=cancelled_callback,
                        deadline=deadline,
                    )
                except ReadOnlyDatabaseAccessError:
                    if _source_signature_for_validation(
                            database_path,
                            cancelled_callback=cancelled_callback,
                            deadline=deadline,
                            ) != before:
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
                        cancelled_callback=cancelled_callback,
                        deadline=deadline,
                    )
                except UnverifiableDatabaseWalError:
                    if _source_signature_for_validation(
                            database_path,
                            cancelled_callback=cancelled_callback,
                            deadline=deadline,
                            ) != before:
                        _cleanup_failed_connection_open(None, snapshot)
                        continue
                    if not (ignore_unpaired_wal or quarantined):
                        raise
                    _raise_if_read_interrupted(cancelled_callback, deadline)
                    return main_snapshot_path, True, snapshot, True, before

            if _source_signature_for_validation(
                    database_path,
                    cancelled_callback=cancelled_callback,
                    deadline=deadline,
                    ) != before:
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
            _raise_if_read_interrupted(cancelled_callback, deadline)
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
