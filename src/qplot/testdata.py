"""Generate synthetic QCoDeS databases from CSV specifications."""

from __future__ import annotations

import argparse
import csv
import errno
import math
import os
import re
import secrets
import sqlite3
import stat
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from pathlib import Path
from time import perf_counter

import numpy as np
from qcodes.dataset import Measurement, new_experiment
from qcodes.dataset.sqlite.connection import AtomicConnection
from qcodes.dataset.sqlite.database import connect
from qcodes.parameters import ManualParameter

from qplot.datahandling.file_identity import (
    QPLOT_GENERATED_DATABASE_APPLICATION_ID,
    QPLOT_GENERATION_LINEAGE_FORMAT_VERSION,
    QPLOT_GENERATION_LINEAGE_NONCE_BYTES,
    QPLOT_GENERATION_LINEAGE_RING_TABLE,
    QPLOT_GENERATION_LINEAGE_STATE_TABLE,
    QPLOT_GENERATION_LINEAGE_WINDOW,
    QPLOT_GENERATION_PROVENANCE_TABLE,
    QPLOT_GENERATION_PROVENANCE_TOKEN_BYTES,
    QPLOT_GENERATION_PROVENANCE_TRIGGER_PREFIX,
    database_publication_guard_path,
    generation_lineage_transition_statements,
    generation_provenance_trigger,
)

CSV_COLUMNS = (
    "dimensions",
    "measured_name",
    "measured_label",
    "measured_unit",
    "v_sd_start",
    "v_sd_stop",
    "v_sd_points",
    "v_g_start",
    "v_g_stop",
    "v_g_points",
)

EXAMPLE_ROWS = (
    ("1", "current", "Current", "nA", "-0.1", "0.1", "501", "", "", ""),
    (
        "1",
        "conductance",
        "Conductance",
        "uS",
        "-0.1",
        "0.1",
        "501",
        "",
        "",
        "",
    ),
    (
        "1",
        "resistance",
        "Resistance",
        "kOhm",
        "-0.1",
        "0.1",
        "501",
        "",
        "",
        "",
    ),
    (
        "1",
        "transconductance",
        "Transconductance",
        "uS/V",
        "-0.1",
        "0.1",
        "501",
        "",
        "",
        "",
    ),
    (
        "1",
        "current",
        "Current",
        "nA",
        "-0.1",
        "0.1",
        "501",
        "",
        "",
        "",
    ),
    (
        "2",
        "current",
        "Current",
        "nA",
        "-0.01",
        "0.01",
        "121",
        "-1",
        "1",
        "81",
    ),
    (
        "2",
        "current",
        "Current",
        "nA",
        "-0.01",
        "0.01",
        "121",
        "-1",
        "1",
        "81",
    ),
)

INSTRUCTION_FILE_NAMES = (
    "qplot_test_db_01_10mb.csv",
    "qplot_test_db_02_25mb.csv",
    "qplot_test_db_03_50mb.csv",
    "qplot_test_db_04_100mb.csv",
    "qplot_test_db_05_250mb.csv",
    "qplot_test_db_06_500mb.csv",
    "qplot_test_db_07_1gb.csv",
    "qplot_test_db_08_5gb.csv",
    "qplot_test_db_09_10gb.csv",
    "qplot_test_db_10_30gb.csv",
)

_PARAMETER_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MINIMUM_AMPLITUDE = 0.5
_MAXIMUM_AMPLITUDE = 1.5
_MINIMUM_FREQUENCY = 0.5
_MAXIMUM_FREQUENCY = 4.0
_SINUSOID_COMPONENT_COUNT = 2
_RESULT_CHUNK_POINTS = 10_000
_TEMPORARY_DATABASE_PREFIX = ".qplot-testdata-"
_PROVENANCE_TRIGGER_PREFIX = QPLOT_GENERATION_PROVENANCE_TRIGGER_PREFIX
_PROVENANCE_WRITER_ENABLED_ATTRIBUTE = "_qplot_provenance_writer_enabled"
_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_SQLITE_HEADER = b"SQLite format 3\x00"
_SQLITE_ROLLBACK_JOURNAL_VERSIONS = b"\x01\x01"
_REPLACEMENT_BUSY_ERRNOS = frozenset(
    value
    for value in (
        errno.EACCES,
        errno.EBUSY,
        errno.EPERM,
        getattr(errno, "ETXTBSY", None),
    )
    if value is not None
)


class SpecificationError(ValueError):
    """Raised when a test-database CSV specification is invalid."""


class GenerationCancelled(RuntimeError):
    """Raised internally when test-database generation is cancelled."""


def _result_column_name_is_usable(name):
    """Return whether SQLite accepts ``name`` as QCoDeS will emit it."""
    connection = sqlite3.connect(":memory:")
    try:
        # Parameter names have already passed _PARAMETER_NAME_PATTERN, so this
        # parser probe cannot introduce additional SQL tokens. Keeping the
        # identifier unquoted deliberately mirrors QCoDeS result-table SQL.
        connection.execute(
            f"CREATE TABLE qplot_results (id INTEGER PRIMARY KEY, {name} numeric)"
        )
    except sqlite3.DatabaseError:
        return False
    finally:
        connection.close()
    return True


@dataclass(frozen=True)
class RunSpecification:
    """Validated settings for one generated QCoDeS run."""

    dimensions: int
    measured_name: str
    measured_label: str
    measured_unit: str
    v_sd_start: float
    v_sd_stop: float
    v_sd_points: int
    v_g_start: float | None = None
    v_g_stop: float | None = None
    v_g_points: int | None = None

    @property
    def point_count(self):
        """Return the number of measured points in the run."""
        if self.dimensions == 1:
            return self.v_sd_points
        assert self.v_g_points is not None
        return self.v_sd_points * self.v_g_points


def _row_error(line_number, message):
    return SpecificationError(f"CSV row {line_number}: {message}")


def _required(row, column, line_number):
    value = (row.get(column) or "").strip()
    if not value:
        raise _row_error(line_number, f"{column} is required")
    return value


def _finite_float(row, column, line_number):
    value = _required(row, column, line_number)
    try:
        number = float(value)
    except ValueError as error:
        raise _row_error(line_number, f"{column} must be a number") from error
    if not math.isfinite(number):
        raise _row_error(line_number, f"{column} must be finite")
    return number


def _point_count(row, column, line_number):
    value = _required(row, column, line_number)
    try:
        points = int(value)
    except ValueError as error:
        raise _row_error(line_number, f"{column} must be an integer") from error
    if points < 2:
        raise _row_error(line_number, f"{column} must be at least 2")
    return points


def _parse_row(row, line_number):
    dimensions_value = _required(row, "dimensions", line_number)
    try:
        dimensions = int(dimensions_value)
    except ValueError as error:
        raise _row_error(line_number, "dimensions must be 1 or 2") from error
    if dimensions not in (1, 2):
        raise _row_error(line_number, "dimensions must be 1 or 2")

    measured_name = _required(row, "measured_name", line_number)
    if not _PARAMETER_NAME_PATTERN.fullmatch(measured_name):
        raise _row_error(
            line_number,
            "measured_name must start with a letter or underscore and contain "
            "only letters, numbers, and underscores",
        )
    if measured_name.lower() in {"v_sd", "v_g"}:
        raise _row_error(
            line_number,
            "measured_name cannot be V_SD or V_G because those names are swept",
        )
    if not _result_column_name_is_usable(measured_name):
        raise _row_error(
            line_number,
            "measured_name cannot be used as a QCoDeS result column",
        )

    measured_label = _required(row, "measured_label", line_number)
    measured_unit = (row.get("measured_unit") or "").strip()
    v_sd_start = _finite_float(row, "v_sd_start", line_number)
    v_sd_stop = _finite_float(row, "v_sd_stop", line_number)
    if v_sd_start == v_sd_stop:
        raise _row_error(line_number, "v_sd_start and v_sd_stop must differ")
    v_sd_points = _point_count(row, "v_sd_points", line_number)

    gate_values = tuple((row.get(column) or "").strip() for column in CSV_COLUMNS[7:])
    if dimensions == 1:
        if any(gate_values):
            raise _row_error(
                line_number,
                "v_g_start, v_g_stop, and v_g_points must be blank for a 1D run",
            )
        return RunSpecification(
            dimensions=dimensions,
            measured_name=measured_name,
            measured_label=measured_label,
            measured_unit=measured_unit,
            v_sd_start=v_sd_start,
            v_sd_stop=v_sd_stop,
            v_sd_points=v_sd_points,
        )

    v_g_start = _finite_float(row, "v_g_start", line_number)
    v_g_stop = _finite_float(row, "v_g_stop", line_number)
    if v_g_start == v_g_stop:
        raise _row_error(line_number, "v_g_start and v_g_stop must differ")
    v_g_points = _point_count(row, "v_g_points", line_number)
    return RunSpecification(
        dimensions=dimensions,
        measured_name=measured_name,
        measured_label=measured_label,
        measured_unit=measured_unit,
        v_sd_start=v_sd_start,
        v_sd_stop=v_sd_stop,
        v_sd_points=v_sd_points,
        v_g_start=v_g_start,
        v_g_stop=v_g_stop,
        v_g_points=v_g_points,
    )


def read_specifications(csv_path):
    """Read and validate all nonblank rows in a generator CSV file."""
    csv_path = Path(csv_path)
    try:
        handle = csv_path.open("r", encoding="utf-8-sig", newline="")
    except OSError as error:
        raise SpecificationError(f"Could not open {csv_path}: {error}") from error

    with handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames
        if fieldnames is None:
            raise SpecificationError("CSV file is empty")
        fieldnames = [name.strip() for name in fieldnames]
        reader.fieldnames = fieldnames
        if len(fieldnames) != len(set(fieldnames)):
            raise SpecificationError("CSV header contains duplicate columns")
        missing = [column for column in CSV_COLUMNS if column not in fieldnames]
        extra = [column for column in fieldnames if column not in CSV_COLUMNS]
        if missing or extra:
            details = []
            if missing:
                details.append(f"missing columns: {', '.join(missing)}")
            if extra:
                details.append(f"unexpected columns: {', '.join(extra)}")
            raise SpecificationError("Invalid CSV header (" + "; ".join(details) + ")")

        specifications = []
        for line_number, row in enumerate(reader, start=2):
            if None in row and any(str(value).strip() for value in row[None]):
                raise _row_error(line_number, "contains more values than the header")
            if not any((row.get(column) or "").strip() for column in CSV_COLUMNS):
                continue
            specifications.append(_parse_row(row, line_number))

    if not specifications:
        raise SpecificationError("CSV file contains no run specifications")
    return specifications


def write_example_csv(csv_path, overwrite=False):
    """Write a ready-to-use example generator CSV and return its path."""
    csv_path = Path(csv_path)
    if csv_path.suffix.lower() != ".csv":
        raise SpecificationError("Example output path must end in .csv")
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    mode = "w" if overwrite else "x"
    try:
        with csv_path.open(mode, encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(CSV_COLUMNS)
            writer.writerows(EXAMPLE_ROWS)
    except FileExistsError as error:
        raise SpecificationError(
            f"{csv_path} already exists; use --overwrite to replace it"
        ) from error
    except OSError as error:
        raise SpecificationError(f"Could not write {csv_path}: {error}") from error
    return csv_path


def copy_instruction_collection(directory, overwrite=False):
    """Copy the installed cumulative instruction CSV collection to a directory."""
    directory = Path(directory)
    if directory.exists() and not directory.is_dir():
        raise SpecificationError(f"Collection destination is not a directory: {directory}")

    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise SpecificationError(f"Could not create {directory}: {error}") from error

    output_paths = tuple(directory / name for name in INSTRUCTION_FILE_NAMES)
    if not overwrite:
        existing = [path.name for path in output_paths if path.exists()]
        if existing:
            raise SpecificationError(
                f"{directory} already contains collection files; use --overwrite "
                f"to replace them: {', '.join(existing)}"
            )

    try:
        for output_path, (_name, content) in zip(
                output_paths,
                instruction_collection_contents(),
                strict=True,
                ):
            mode = "wb" if overwrite else "xb"
            with output_path.open(mode) as handle:
                handle.write(content)
    except OSError as error:
        raise SpecificationError(
            f"Could not copy the instruction CSV collection to {directory}: {error}"
        ) from error

    return output_paths


def instruction_collection_contents():
    """Return installed collection filenames and bytes without writing them."""
    resource_directory = resources.files("qplot").joinpath("resources", "testdata")
    return tuple(
        (name, resource_directory.joinpath(name).read_bytes())
        for name in INSTRUCTION_FILE_NAMES
    )


def _raise_if_cancelled(cancelled_callback):
    if cancelled_callback is not None and cancelled_callback():
        raise GenerationCancelled("Test-database generation was cancelled")


def _qcodes_uri_name(database_path):
    """Return one URI-encoded path layer for QCoDeS' ``file:`` prefix."""
    database_uri = Path(database_path).absolute().as_uri()
    return database_uri.removeprefix("file:")


def _connect_writable_exact_path(database_path):
    """Open an exact generator-owned path with QCoDeS' writable connector."""
    return connect(_qcodes_uri_name(database_path))


def _quote_sqlite_identifier(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def _generation_provenance_trigger(table_name, operation):
    """Return a stable trigger name and exact SQL for one table operation."""
    return generation_provenance_trigger(table_name, operation)


def _generation_provenance_user_tables(connection):
    excluded_tables = (
        QPLOT_GENERATION_PROVENANCE_TABLE,
        QPLOT_GENERATION_LINEAGE_STATE_TABLE,
        QPLOT_GENERATION_LINEAGE_RING_TABLE,
    )
    return [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_schema "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "AND name NOT IN (?, ?, ?) ORDER BY name",
            excluded_tables,
        )
    ]


def _ensure_generation_provenance_triggers(connection):
    """Install exact provenance triggers for every table in this transaction."""
    changed = False
    existing_triggers = {
        row[0]: (row[1], row[2])
        for row in connection.execute(
            "SELECT name, tbl_name, sql FROM sqlite_schema WHERE type = 'trigger'"
        ).fetchall()
    }
    for table_name in _generation_provenance_user_tables(connection):
        for operation in ("INSERT", "UPDATE", "DELETE"):
            trigger_name, statement = _generation_provenance_trigger(
                table_name,
                operation,
            )
            trigger_row = existing_triggers.get(trigger_name)
            if trigger_row is None:
                connection.execute(statement)
                existing_triggers[trigger_name] = (table_name, statement)
                changed = True
            elif trigger_row != (table_name, statement):
                raise RuntimeError(
                    "The generated database contains a conflicting qPlot "
                    f"provenance trigger named {trigger_name!r}."
                )
    return changed


def _create_generation_lineage(connection, token):
    """Create and seed the bounded nonce ancestry chain at sequence zero."""
    state_table = _quote_sqlite_identifier(QPLOT_GENERATION_LINEAGE_STATE_TABLE)
    ring_table = _quote_sqlite_identifier(QPLOT_GENERATION_LINEAGE_RING_TABLE)
    connection.execute(
        f"CREATE TABLE {ring_table} ("
        "slot INTEGER PRIMARY KEY, "
        "sequence INTEGER NOT NULL UNIQUE CHECK (sequence >= 0), "
        "parent_nonce BLOB, nonce BLOB NOT NULL UNIQUE, "
        f"CHECK (slot = sequence % {QPLOT_GENERATION_LINEAGE_WINDOW}), "
        "CHECK ((sequence = 0 AND parent_nonce IS NULL) OR "
        f"(typeof(parent_nonce) = 'blob' AND length(parent_nonce) = "
        f"{QPLOT_GENERATION_LINEAGE_NONCE_BYTES})), "
        f"CHECK (typeof(nonce) = 'blob' AND length(nonce) = "
        f"{QPLOT_GENERATION_LINEAGE_NONCE_BYTES})) WITHOUT ROWID"
    )
    connection.execute(
        f"INSERT INTO {ring_table} VALUES "
        f"(0, 0, NULL, randomblob({QPLOT_GENERATION_LINEAGE_NONCE_BYTES}))"
    )
    connection.execute(
        f"CREATE TABLE {state_table} ("
        "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
        "format_version INTEGER NOT NULL, generation_token TEXT NOT NULL, "
        "head_sequence INTEGER NOT NULL CHECK (head_sequence >= 0), "
        "head_nonce BLOB NOT NULL, window_size INTEGER NOT NULL, "
        f"CHECK (format_version = {QPLOT_GENERATION_LINEAGE_FORMAT_VERSION}), "
        f"CHECK (typeof(head_nonce) = 'blob' AND length(head_nonce) = "
        f"{QPLOT_GENERATION_LINEAGE_NONCE_BYTES}), "
        f"CHECK (window_size = {QPLOT_GENERATION_LINEAGE_WINDOW}))"
    )
    connection.execute(
        f"INSERT INTO {state_table} "
        "SELECT 1, ?, ?, 0, nonce, ? "
        f"FROM {ring_table} WHERE sequence = 0",
        (
            QPLOT_GENERATION_LINEAGE_FORMAT_VERSION,
            token,
            QPLOT_GENERATION_LINEAGE_WINDOW,
        ),
    )


def _advance_generation_lineage(connection):
    for statement in generation_lineage_transition_statements():
        connection.execute(statement)


def _generation_lineage_state(connection):
    """Return a validated v2 writer state, or ``None`` for legacy provenance."""
    state_exists = connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?",
        (QPLOT_GENERATION_LINEAGE_STATE_TABLE,),
    ).fetchone()
    ring_exists = connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?",
        (QPLOT_GENERATION_LINEAGE_RING_TABLE,),
    ).fetchone()
    if state_exists is None and ring_exists is None:
        return None
    if state_exists is None or ring_exists is None:
        raise ValueError("The qPlot generation lineage tables are incomplete.")

    state_table = _quote_sqlite_identifier(QPLOT_GENERATION_LINEAGE_STATE_TABLE)
    ring_table = _quote_sqlite_identifier(QPLOT_GENERATION_LINEAGE_RING_TABLE)
    rows = connection.execute(
        f"SELECT singleton, format_version, generation_token, head_sequence, "
        f"head_nonce, window_size FROM {state_table}"
    ).fetchall()
    if len(rows) != 1:
        raise ValueError("The qPlot generation lineage state is invalid.")
    singleton, version, token, sequence, nonce, window = rows[0]
    tip = connection.execute(
        f"SELECT slot, parent_nonce, nonce FROM {ring_table} WHERE sequence = ?",
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
        raise ValueError("The qPlot generation lineage state is invalid.")
    return token, sequence, nonce


def _legacy_generation_provenance_triggers(connection):
    """Return only structurally exact triggers written by legacy qPlot."""
    legacy_triggers = []
    pattern = re.compile(r"qplot_provenance_\d+_(insert|update|delete)\Z")
    provenance_table = _quote_sqlite_identifier(QPLOT_GENERATION_PROVENANCE_TABLE)
    for trigger_name, table_name, sql in connection.execute(
        "SELECT name, tbl_name, sql FROM sqlite_schema WHERE type = 'trigger'"
    ).fetchall():
        match = pattern.fullmatch(trigger_name)
        if match is None:
            continue
        operation = match.group(1).upper()
        expected = (
            f"CREATE TRIGGER {_quote_sqlite_identifier(trigger_name)} "
            f"AFTER {operation} ON {_quote_sqlite_identifier(table_name)} BEGIN "
            f"UPDATE {provenance_table} SET write_epoch = write_epoch + 1 "
            "WHERE singleton = 1; END"
        )
        if sql != expected:
            raise RuntimeError(
                "The database contains a conflicting legacy qPlot provenance "
                f"trigger named {trigger_name!r}."
            )
        legacy_triggers.append(trigger_name)
    return legacy_triggers


def _remove_legacy_generation_provenance_triggers(connection):
    changed = False
    for trigger_name in _legacy_generation_provenance_triggers(connection):
        connection.execute(
            f"DROP TRIGGER {_quote_sqlite_identifier(trigger_name)}"
        )
        changed = True
    return changed


def _install_generation_provenance(connection):
    """Make later WAL writes prove that they descend from this main file."""
    token = secrets.token_hex(QPLOT_GENERATION_PROVENANCE_TOKEN_BYTES)
    provenance_table = _quote_sqlite_identifier(QPLOT_GENERATION_PROVENANCE_TABLE)
    connection.execute(
        f"CREATE TABLE {provenance_table} ("
        "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
        "generation_token TEXT NOT NULL, "
        "write_epoch INTEGER NOT NULL CHECK (write_epoch >= 0))"
    )
    connection.execute(
        f"INSERT INTO {provenance_table} VALUES (1, ?, 0)",
        (token,),
    )
    _create_generation_lineage(connection, token)
    _ensure_generation_provenance_triggers(connection)


def _require_generation_provenance_for_writer(connection):
    application_id = connection.execute("PRAGMA application_id").fetchone()
    try:
        provenance_rows = connection.execute(
            f"SELECT singleton, generation_token, write_epoch "
            f"FROM {_quote_sqlite_identifier(QPLOT_GENERATION_PROVENANCE_TABLE)}"
        ).fetchall()
    except sqlite3.Error as error:
        raise ValueError(
            "This is not a qPlot-generated database with writable provenance."
        ) from error

    if len(provenance_rows) != 1:
        raise ValueError(
            "This is not a qPlot-generated database with writable provenance."
        )
    singleton, token, epoch = provenance_rows[0]
    try:
        token_bytes = bytes.fromhex(token) if isinstance(token, str) else b""
    except ValueError:
        token_bytes = b""
    if (
        application_id != (QPLOT_GENERATED_DATABASE_APPLICATION_ID,)
        or singleton != 1
        or len(token_bytes) != QPLOT_GENERATION_PROVENANCE_TOKEN_BYTES
        or not isinstance(epoch, int)
        or epoch < 0
    ):
        raise ValueError(
            "This is not a qPlot-generated database with writable provenance."
        )
    lineage = _generation_lineage_state(connection)
    if lineage is not None:
        lineage_token, lineage_sequence, _lineage_nonce = lineage
        if lineage_token != token or lineage_sequence != epoch:
            raise ValueError("The qPlot generation lineage state is invalid.")
    return lineage


def enable_generation_provenance_for_writer(connection):
    """Cover future QCoDeS result tables on an explicitly writable connection.

    SQLite has no persistent database-wide write trigger.  A QCoDeS writer that
    will create later runs must therefore opt into this connection hook before
    it performs any new experiment or measurement writes.  The hook refuses a
    nonempty WAL, installs table triggers before the outer QCoDeS transaction
    commits, and extends a token-bound nonce chain.  Subsequent foreground or
    background result writes therefore remain provable across checkpoints.

    This function is exclusively for a connection deliberately opened by the
    database owner for writing.  qPlot's viewer never calls it and never
    installs metadata or triggers in an input database.
    """
    if not isinstance(connection, AtomicConnection):
        raise TypeError(
            "enable_generation_provenance_for_writer requires a writable "
            "QCoDeS AtomicConnection"
        )
    if getattr(connection, _PROVENANCE_WRITER_ENABLED_ATTRIBUTE, False):
        return connection
    if connection.in_transaction:
        raise RuntimeError(
            "Enable qPlot generation provenance before starting a write "
            "transaction."
        )
    if connection.isolation_level is None:
        raise RuntimeError(
            "Enable qPlot generation provenance before using autocommit mode."
        )

    original_commit = connection.commit
    original_rollback = connection.rollback

    def synchronize_triggers():
        _ensure_generation_provenance_triggers(connection)
        _advance_generation_lineage(connection)

    def provenance_commit():
        if not connection.in_transaction:
            return original_commit()
        try:
            synchronize_triggers()
            return original_commit()
        except BaseException:
            try:
                original_rollback()
            except sqlite3.Error:
                pass
            raise

    # Acquire SQLite's writer lock before inspecting the lineage or WAL.  A
    # second connection must not be able to park uninstrumented frames between
    # the quiescence check and the first provenance event.
    connection.execute("BEGIN IMMEDIATE")
    try:
        lineage = _require_generation_provenance_for_writer(connection)
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0].lower()
        if journal_mode == "wal":
            wal_path = Path(f"{connection.path_to_dbfile}-wal")
            try:
                wal_size = wal_path.stat().st_size
            except FileNotFoundError:
                wal_size = 0
            if wal_size:
                raise RuntimeError(
                    "qPlot provenance refuses to bless writes already present in a "
                    "WAL. On the owner connection, checkpoint the WAL with TRUNCATE, "
                    "then enable provenance before creating a Measurement."
                )
        if lineage is None and journal_mode == "wal":
            raise RuntimeError(
                "This older qPlot-generated database must be migrated from a "
                "quiescent rollback-journal connection. Checkpoint the WAL, switch "
                "the owner connection to journal_mode=DELETE, then enable generation "
                "provenance before creating a Measurement."
            )
        if lineage is None:
            _remove_legacy_generation_provenance_triggers(connection)
            migrated_token = secrets.token_hex(
                QPLOT_GENERATION_PROVENANCE_TOKEN_BYTES
            )
            connection.execute(
                f"UPDATE {_quote_sqlite_identifier(QPLOT_GENERATION_PROVENANCE_TABLE)} "
                "SET generation_token = ?, write_epoch = 0 WHERE singleton = 1",
                (migrated_token,),
            )
            _create_generation_lineage(connection, migrated_token)
        else:
            _remove_legacy_generation_provenance_triggers(connection)
        synchronize_triggers()
        original_commit()
    except BaseException:
        try:
            original_rollback()
        except sqlite3.Error:
            pass
        raise

    connection.commit = provenance_commit
    setattr(connection, _PROVENANCE_WRITER_ENABLED_ATTRIBUTE, True)
    return connection


def _owned_temporary_artifacts(temporary_path, *, include_database=True):
    if include_database:
        yield temporary_path
    for suffix in _SQLITE_SIDECAR_SUFFIXES:
        yield Path(f"{temporary_path}{suffix}")


def _remove_owned_temporary_artifacts(temporary_path, *, include_database=True):
    """Remove only the generator-owned database and its possible sidecars."""
    failures = []
    for artifact_path in _owned_temporary_artifacts(
            temporary_path,
            include_database=include_database,
            ):
        try:
            artifact_path.unlink(missing_ok=True)
        except OSError as error:
            failures.append((artifact_path, error))
    if failures:
        failed_paths = ", ".join(str(path) for path, _error in failures)
        raise OSError(
            f"Could not remove generator-owned temporary files: {failed_paths}"
        ) from failures[0][1]


def _destination_entry_state(database_path):
    """Return a race-sensitive identity for the selected directory entry."""
    try:
        status = database_path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise _unsafe_publication_error(database_path) from error
    return (
        status.st_dev,
        status.st_ino,
        status.st_mode,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )


def _destination_sidecars(database_path):
    """Return every existing sidecar entry without following symlinks."""
    sidecars = []
    for suffix in _SQLITE_SIDECAR_SUFFIXES:
        sidecar_path = Path(f"{database_path}{suffix}")
        try:
            sidecar_path.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            # An inspection failure is not evidence that the entry is absent.
            # Publication must therefore fail closed without touching it.
            raise _unsafe_publication_error(database_path) from error
        sidecars.append(sidecar_path)
    return tuple(sidecars)


def _unsafe_publication_error(database_path, sidecars=()):
    details = ""
    if sidecars:
        details = " Detected: " + ", ".join(path.name for path in sidecars) + "."
    return SpecificationError(
        f"Cannot publish {database_path}: database active or SQLite sidecars "
        "present. Close every application using this database and have the "
        "owning SQLite/QCoDeS application resolve any -wal, -shm, or -journal "
        f"files, then retry or choose another output path.{details} qPlot did "
        "not change the destination database or its SQLite sidecars."
    )


def _ambiguous_sidecar_race_error(database_path, guard_path, sidecars):
    if sidecars:
        details = " Detected after replacement: " + ", ".join(
            path.name for path in sidecars
        ) + "."
    else:
        details = (
            " A post-install safety check failed; a transient sidecar cannot "
            "be ruled out."
        )
    return SpecificationError(
        f"Cannot publish {database_path}: database active or SQLite sidecars "
        f"present.{details} qPlot restored the "
        "previous database main and did not change the sidecars, but cannot "
        "prove which main they belong to. To prevent a stale main/WAL pairing, "
        f"qPlot left the safety guard {guard_path}. Close every application "
        "using the database, have the owning SQLite/QCoDeS application resolve "
        "the sidecars, then remove only that .qplot-publishing guard and retry "
        "or choose another output path."
    )


def _require_safe_destination_sidecars(database_path):
    sidecars = _destination_sidecars(database_path)
    if sidecars:
        raise _unsafe_publication_error(database_path, sidecars)


def _result_chunks(point_count):
    for start in range(0, point_count, _RESULT_CHUNK_POINTS):
        yield slice(start, min(start + _RESULT_CHUNK_POINTS, point_count))


def _timestamped_message(message):
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    print(f"[{timestamp}] {message}", flush=True)


def _random_sinusoid_components(random_generator, dimensions):
    """Return independently randomised sinusoid parameters for one run."""
    components = []
    for _ in range(_SINUSOID_COMPONENT_COUNT):
        amplitude = random_generator.uniform(_MINIMUM_AMPLITUDE, _MAXIMUM_AMPLITUDE)
        frequencies = tuple(
            random_generator.uniform(_MINIMUM_FREQUENCY, _MAXIMUM_FREQUENCY)
            for _ in range(dimensions)
        )
        phase = random_generator.uniform(0.0, 2.0 * np.pi)
        components.append((amplitude, frequencies, phase))
    return tuple(components)


def _sinusoid_sum_1d(normalized_values, components):
    values = np.zeros_like(normalized_values, dtype=float)
    for amplitude, frequencies, phase in components:
        values += amplitude * np.sin(
            2.0 * np.pi * frequencies[0] * normalized_values + phase
        )
    return values


def _sinusoid_sum_2d_row(v_sd_normalized, v_g_normalized, components):
    values = np.zeros_like(v_g_normalized, dtype=float)
    for amplitude, frequencies, phase in components:
        values += amplitude * np.sin(
            2.0
            * np.pi
            * (
                frequencies[0] * v_sd_normalized
                + frequencies[1] * v_g_normalized
            )
            + phase
        )
    return values


def _write_run(
        experiment,
        run_number,
        specification,
        random_generator,
        cancelled_callback=None,
        ):
    _raise_if_cancelled(cancelled_callback)
    v_sd = ManualParameter("V_SD", label="Source-drain voltage", unit="V")
    measured = ManualParameter(
        specification.measured_name,
        label=specification.measured_label,
        unit=specification.measured_unit,
    )
    measurement = Measurement(exp=experiment, name=f"run_{run_number}")
    measurement.register_parameter(v_sd)

    v_sd_values = np.linspace(
        specification.v_sd_start,
        specification.v_sd_stop,
        specification.v_sd_points,
    )
    v_sd_normalized = np.linspace(0.0, 1.0, specification.v_sd_points)
    components = _random_sinusoid_components(
        random_generator,
        specification.dimensions,
    )

    if specification.dimensions == 1:
        measurement.register_parameter(measured, setpoints=(v_sd,))
        measured_values = _sinusoid_sum_1d(v_sd_normalized, components)
        with measurement.run() as datasaver:
            for result_slice in _result_chunks(specification.v_sd_points):
                _raise_if_cancelled(cancelled_callback)
                datasaver.add_result(
                    (v_sd, v_sd_values[result_slice]),
                    (measured, measured_values[result_slice]),
                )
        points_written = int(datasaver.points_written)
        if points_written != specification.point_count:
            raise RuntimeError(
                f"run_{run_number} persisted {points_written} of "
                f"{specification.point_count} expected result rows"
            )
        return points_written

    assert specification.v_g_start is not None
    assert specification.v_g_stop is not None
    assert specification.v_g_points is not None
    v_g = ManualParameter("V_G", label="Gate voltage", unit="V")
    measurement.register_parameter(v_g)
    measurement.register_parameter(measured, setpoints=(v_sd, v_g))
    v_g_values = np.linspace(
        specification.v_g_start,
        specification.v_g_stop,
        specification.v_g_points,
    )
    v_g_normalized = np.linspace(0.0, 1.0, specification.v_g_points)

    with measurement.run() as datasaver:
        for v_sd_index, v_sd_value in enumerate(v_sd_values):
            measured_values = _sinusoid_sum_2d_row(
                v_sd_normalized[v_sd_index],
                v_g_normalized,
                components,
            )
            for result_slice in _result_chunks(specification.v_g_points):
                _raise_if_cancelled(cancelled_callback)
                result_count = result_slice.stop - result_slice.start
                datasaver.add_result(
                    (v_sd, np.full(result_count, float(v_sd_value))),
                    (v_g, v_g_values[result_slice]),
                    (measured, measured_values[result_slice]),
                )
    points_written = int(datasaver.points_written)
    if points_written != specification.point_count:
        raise RuntimeError(
            f"run_{run_number} persisted {points_written} of "
            f"{specification.point_count} expected result rows"
        )
    return points_written


def _require_rollback_journal_database(database_path):
    """Reject destinations that cannot be locked without creating sidecars."""
    try:
        status = database_path.lstat()
        with database_path.open("rb") as handle:
            header = handle.read(20)
    except OSError as error:
        raise _unsafe_publication_error(database_path) from error
    if (
            not stat.S_ISREG(status.st_mode)
            or not header.startswith(_SQLITE_HEADER)
            or header[18:20] != _SQLITE_ROLLBACK_JOURNAL_VERSIONS
            ):
        # A persistent WAL-mode main can have no sidecars while quiescent, but
        # BEGIN EXCLUSIVE would create its WAL/SHM. Rejecting it is the only
        # source-preserving choice; the user can choose a new output path.
        raise _unsafe_publication_error(database_path)


def _open_quiescent_destination(
        database_path,
        expected_destination_state,
        lock_path,
        ):
    """Hold SQLite's own exclusive inode lock through a private alias."""
    _require_safe_destination_sidecars(database_path)
    if _destination_entry_state(database_path) != expected_destination_state:
        raise _unsafe_publication_error(database_path)
    _require_rollback_journal_database(lock_path)
    connection = None
    try:
        connection = sqlite3.connect(
            f"file:{_qcodes_uri_name(lock_path)}?mode=rw",
            uri=True,
            isolation_level=None,
            timeout=0,
        )
        # The alias is a hardlink to the destination inode, so SQLite's
        # VFS-specific EXCLUSIVE lock conflicts with real readers/writers. Its
        # recovery and journal lookup, however, uses the private alias name.
        # A destination -journal racing into this call is therefore never
        # opened, recovered, modified, or deleted by qPlot; the recheck below
        # observes it and rejects publication.
        connection.execute("BEGIN EXCLUSIVE")
    except sqlite3.Error as error:
        if connection is not None:
            connection.close()
        raise _unsafe_publication_error(database_path) from error

    try:
        _require_safe_destination_sidecars(database_path)
        if _destination_entry_state(database_path) != expected_destination_state:
            raise _unsafe_publication_error(database_path)
    except Exception:
        connection.close()
        raise
    return connection


def _close_publication_connection(connection):
    if connection is None:
        return
    try:
        connection.rollback()
    except sqlite3.Error:
        pass
    try:
        connection.close()
    except sqlite3.Error:
        pass


def _create_publication_guard(database_path):
    """Create the qPlot-visible guard for the short replacement transaction."""
    guard_path = database_publication_guard_path(database_path)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(guard_path, flags, 0o600)
    except OSError as error:
        raise _unsafe_publication_error(database_path) from error
    try:
        os.close(descriptor)
    except OSError as error:
        # Ownership starts at os.open, not when this helper returns. Otherwise
        # a reported close failure could strand an untracked guard and block
        # every later read/retry.
        try:
            guard_path.unlink()
        except OSError:
            pass
        raise _unsafe_publication_error(database_path) from error
    return guard_path


def _create_database_backup_link(database_path):
    """Keep the old main inode available for a sidecar-race rollback."""
    handle = tempfile.NamedTemporaryFile(
        prefix=_TEMPORARY_DATABASE_PREFIX,
        suffix=".backup",
        dir=database_path.parent,
        delete=False,
    )
    backup_path = Path(handle.name)
    handle.close()
    backup_path.unlink()
    try:
        # This private alias lets SQLite lock the old inode without consulting
        # destination-named journals, and lets the publisher atomically restore
        # that exact inode if a post-install safety check fails.
        os.link(database_path, backup_path)
    except OSError as error:
        backup_path.unlink(missing_ok=True)
        raise _unsafe_publication_error(database_path) from error
    return backup_path


def _restore_database_backup(backup_path, database_path):
    """Atomically restore the old main without touching any sidecar."""
    try:
        os.replace(backup_path, database_path)
    except OSError as error:
        guard_path = database_publication_guard_path(database_path)
        raise OSError(
            f"Cannot publish {database_path}: database active or SQLite "
            "sidecars present. qPlot could not restore the previous database "
            "main because the replacement path is still active. The selected "
            f"path may contain the replacement; the prior main is retained at "
            f"{backup_path} unless qPlot's final retry restored it. The safety "
            f"guard {guard_path} remains. Close every application using the "
            "database and have the owning SQLite/QCoDeS application resolve "
            "all -wal, -shm, and -journal files before recovering the prior "
            "main or removing only that guard. qPlot did not modify or remove "
            "the SQLite sidecars."
        ) from error


def _replace_database_file(
        temporary_path,
        database_path,
        expected_destination_state,
        ):
    """Replace a proven-quiescent main and roll back any sidecar race."""
    guard_path = _create_publication_guard(database_path)
    backup_path = None
    connection = None
    replacement_installed = False
    guard_must_remain = False
    ambiguous_sidecars = ()
    try:
        _require_safe_destination_sidecars(database_path)
        if _destination_entry_state(database_path) != expected_destination_state:
            raise _unsafe_publication_error(database_path)
        _require_rollback_journal_database(database_path)
        backup_path = _create_database_backup_link(database_path)
        backup_state = _destination_entry_state(backup_path)
        locked_destination_state = _destination_entry_state(database_path)
        if (
                backup_state is None
                or locked_destination_state is None
                or backup_state[:5] != expected_destination_state[:5]
                or locked_destination_state != backup_state
                or not os.path.samefile(database_path, backup_path)
                ):
            raise _unsafe_publication_error(database_path)

        connection = _open_quiescent_destination(
            database_path,
            locked_destination_state,
            backup_path,
        )
        # Windows does not permit replacing the main while this connection is
        # open. Closing it is safe there because any competing SQLite handle
        # likewise prevents os.replace. POSIX keeps the VFS lock across replace.
        if os.name == "nt":
            _close_publication_connection(connection)
            connection = None

        _require_safe_destination_sidecars(database_path)
        if not os.path.samefile(database_path, backup_path):
            raise _unsafe_publication_error(database_path)

        try:
            os.replace(temporary_path, database_path)
        except OSError as error:
            if error.errno in _REPLACEMENT_BUSY_ERRNOS:
                raise _unsafe_publication_error(database_path) from error
            raise
        replacement_installed = True

        try:
            # This postcondition is essential: a non-cooperating filesystem
            # actor can create a sidecar in the final check/replace syscall gap.
            # qPlot readers see the guard throughout this interval. If a
            # sidecar appeared, restore the byte-identical old inode and leave
            # that externally owned sidecar exactly as it was created.
            ambiguous_sidecars = _destination_sidecars(database_path)
            if ambiguous_sidecars:
                raise _unsafe_publication_error(
                    database_path,
                    ambiguous_sidecars,
                )
            backup_after_replace = _destination_entry_state(backup_path)
            if (
                    backup_after_replace is None
                    or backup_after_replace[:5] != backup_state[:5]
                    ):
                raise _unsafe_publication_error(database_path)
            ambiguous_sidecars = _destination_sidecars(database_path)
            if ambiguous_sidecars:
                raise _unsafe_publication_error(
                    database_path,
                    ambiguous_sidecars,
                )
        except Exception as error:
            # Once a post-install postcondition fails, absence observed later
            # cannot prove that a transient sidecar did not belong to the new
            # main. Keep the guard even if rollback itself needs a retry.
            guard_must_remain = True
            _restore_database_backup(backup_path, database_path)
            backup_path = None
            replacement_installed = False
            if not ambiguous_sidecars:
                try:
                    ambiguous_sidecars = _destination_sidecars(database_path)
                except SpecificationError as inspection_error:
                    # Inspection itself is unsafe, so retain the guard exactly
                    # as for an observed sidecar. A fresh process must not guess.
                    guard_must_remain = True
                    raise _ambiguous_sidecar_race_error(
                        database_path,
                        guard_path,
                        (),
                    ) from inspection_error
            # A sidecar observed after the swap could belong to either the old
            # or new main. Even if none is visible now, a failed postcondition
            # cannot rule out a transient one. The guard is therefore
            # persistent recovery state, not a disposable generator temporary.
            guard_must_remain = True
            raise _ambiguous_sidecar_race_error(
                database_path,
                guard_path,
                ambiguous_sidecars,
            ) from error

        # Keep SQLite's old-inode EXCLUSIVE lock through commit on POSIX. A
        # cooperating old SQLite connection cannot create a stale sidecar in
        # the check/unlink gap; on Windows, a handle overlapping os.replace
        # makes that syscall fail. No portable filesystem syscall atomically
        # conditions a main-file replace on three sibling paths, so the main's
        # embedded replacement marker is the cross-process backstop: qPlot
        # permanently quarantines WAL access for this identity even if a raw
        # filesystem actor bypasses SQLite's lock after the last check.
        try:
            guard_path.unlink()
        except OSError as error:
            guard_must_remain = True
            _restore_database_backup(backup_path, database_path)
            backup_path = None
            replacement_installed = False
            try:
                ambiguous_sidecars = _destination_sidecars(database_path)
            except SpecificationError as inspection_error:
                guard_must_remain = True
                raise _ambiguous_sidecar_race_error(
                    database_path,
                    guard_path,
                    (),
                ) from inspection_error
            raise _ambiguous_sidecar_race_error(
                database_path,
                guard_path,
                ambiguous_sidecars,
            ) from error
        guard_path = None
        _close_publication_connection(connection)
        connection = None
        replacement_installed = False

        # Publication is now committed. Failure to remove this private name
        # must not turn a completed replacement into a reported rejection.
        try:
            backup_path.unlink()
        except OSError:
            pass
        else:
            backup_path = None
    finally:
        _close_publication_connection(connection)
        if replacement_installed and backup_path is not None:
            guard_must_remain = True
            try:
                _restore_database_backup(backup_path, database_path)
            except OSError:
                # Keep both the backup and publication guard for manual recovery.
                guard_must_remain = True
            else:
                backup_path = None
                replacement_installed = False
                try:
                    guard_must_remain = (
                        guard_must_remain
                        or bool(_destination_sidecars(database_path))
                    )
                except SpecificationError:
                    guard_must_remain = True
        if backup_path is not None and not replacement_installed:
            try:
                backup_path.unlink()
            except OSError:
                pass
        if (
                guard_path is not None
                and not guard_must_remain
                and not replacement_installed
                ):
            try:
                guard_path.unlink()
            except OSError:
                pass


def _link_database_file(temporary_path, database_path):
    """Publish a previously absent output with a guarded postcondition."""
    guard_path = _create_publication_guard(database_path)
    destination_linked = False
    try:
        _require_safe_destination_sidecars(database_path)
        if _destination_entry_state(database_path) is not None:
            raise SpecificationError(
                f"{database_path} already exists; use --overwrite to replace it"
            )
        try:
            # The hard link is an atomic no-clobber publish because the
            # generator temporary lives in the destination directory.
            os.link(temporary_path, database_path)
        except FileExistsError as error:
            raise SpecificationError(
                f"{database_path} already exists; use --overwrite to replace it"
            ) from error
        except OSError as error:
            raise OSError(
                "Could not atomically publish the generated database without "
                "overwriting an existing file"
            ) from error
        destination_linked = True

        # An orphan sidecar can be copied into place in the link syscall gap.
        # Since the old state was absence, rolling back only our own hard link
        # restores it without touching the externally owned sidecar.
        _require_safe_destination_sidecars(database_path)
        guard_path.unlink()
        guard_path = None
        destination_linked = False
    finally:
        if destination_linked:
            try:
                owns_destination = os.path.samefile(
                    temporary_path,
                    database_path,
                )
            except OSError:
                owns_destination = False
            if owns_destination:
                try:
                    database_path.unlink()
                except OSError as error:
                    # Keep the guard: qPlot must not open a main whose rollback
                    # could not be proved after a sidecar publication race.
                    raise OSError(
                        "Could not roll back an unsafe new database publication; "
                        "the qPlot publication guard was left in place"
                    ) from error
                destination_linked = False
        if guard_path is not None and not destination_linked:
            try:
                guard_path.unlink()
            except OSError:
                pass


def _publish_database(
        temporary_path,
        database_path,
        overwrite,
        expected_destination_state=None,
        publication_callback=None,
        ):
    """Publish atomically and notify once the destination is committed."""
    _require_safe_destination_sidecars(database_path)
    if overwrite:
        # A changed directory entry means the user's overwrite decision no
        # longer describes the file now at this path. Reject rather than
        # replacing an unexamined concurrent owner. The replacement helper then
        # obtains SQLite's own exclusive lock and enforces a rollback-capable
        # postcondition across the final filesystem operation.
        if _destination_entry_state(database_path) != expected_destination_state:
            raise _unsafe_publication_error(database_path)
        if expected_destination_state is not None:
            _replace_database_file(
                temporary_path,
                database_path,
                expected_destination_state,
            )
            if publication_callback is not None:
                publication_callback()
            return

    _link_database_file(temporary_path, database_path)
    if publication_callback is not None:
        publication_callback()


def generate_database(
        specifications,
        database_path,
        overwrite=False,
        rng=None,
        cancelled_callback=None,
        publication_callback=None,
        ):
    """Generate a database and optionally observe its publication boundary."""
    specifications = list(specifications)
    if not specifications:
        raise SpecificationError("At least one run specification is required")

    database_path = Path(database_path)
    if database_path.suffix.lower() != ".db":
        raise SpecificationError("Database output path must end in .db")
    expected_destination_state = _destination_entry_state(database_path)
    if expected_destination_state is not None and not overwrite:
        raise SpecificationError(
            f"{database_path} already exists; use --overwrite to replace it"
        )
    _require_safe_destination_sidecars(database_path)

    database_path.parent.mkdir(parents=True, exist_ok=True)
    random_generator = rng if rng is not None else np.random.default_rng()
    total_runs = len(specifications)
    total_points = sum(specification.point_count for specification in specifications)
    generation_started = perf_counter()
    _timestamped_message(
        f"Test database generation started: {database_path} "
        f"({total_runs} runs, {total_points} points)."
    )
    temporary_path = None
    publication_completed = False
    try:
        try:
            temporary_handle = tempfile.NamedTemporaryFile(
                prefix=_TEMPORARY_DATABASE_PREFIX,
                suffix=".db",
                dir=database_path.parent,
                delete=False,
            )
            temporary_path = Path(temporary_handle.name)
            temporary_handle.close()
            _raise_if_cancelled(cancelled_callback)
            connection = _connect_writable_exact_path(temporary_path)
            try:
                experiment = new_experiment(
                    "qplot_test_database",
                    sample_name="synthetic",
                    conn=connection,
                )
                for run_number, specification in enumerate(specifications, start=1):
                    run_started = perf_counter()
                    _timestamped_message(
                        f"Run started: run_{run_number} ({run_number}/{total_runs}, "
                        f"{specification.dimensions}D, "
                        f"{specification.point_count} points)."
                    )
                    try:
                        points_written = _write_run(
                            experiment,
                            run_number,
                            specification,
                            random_generator,
                            cancelled_callback=cancelled_callback,
                        )
                        if points_written != specification.point_count:
                            raise RuntimeError(
                                f"run_{run_number} persisted {points_written} of "
                                f"{specification.point_count} expected result rows"
                            )
                    except GenerationCancelled:
                        _timestamped_message(
                            f"Run stopped (cancelled): run_{run_number} "
                            f"after {perf_counter() - run_started:.2f} s."
                        )
                        raise
                    except Exception as error:
                        _timestamped_message(
                            f"Run stopped (failed): run_{run_number} after "
                            f"{perf_counter() - run_started:.2f} s "
                            f"({type(error).__name__}: {error})."
                        )
                        raise
                    _timestamped_message(
                        f"Run stopped (completed): run_{run_number} in "
                        f"{perf_counter() - run_started:.2f} s."
                    )
                # The marker identifies the provenance format; the unique token
                # and nonce-linked branch history let a fresh qPlot process
                # distinguish a WAL descending from this exact main from an
                # unrelated or divergent sidecar. Triggers are installed only
                # after generation so they do not penalise bulk creation.
                connection.execute(
                    "PRAGMA application_id = "
                    f"{QPLOT_GENERATED_DATABASE_APPLICATION_ID}"
                )
                _install_generation_provenance(connection)
                connection.commit()
                _raise_if_cancelled(cancelled_callback)
            finally:
                connection.close()
            # Sidecar cleanup is fallible, so complete it before the atomic
            # publication boundary. No cleanup failure may be reported as a
            # rejected publication after the destination has already changed.
            _remove_owned_temporary_artifacts(
                temporary_path,
                include_database=False,
            )
            if publication_callback is None:
                # Preserve the established four-argument call for test doubles
                # and internal callers that do not need publication tracking.
                _publish_database(
                    temporary_path,
                    database_path,
                    overwrite,
                    expected_destination_state,
                )
            else:
                _publish_database(
                    temporary_path,
                    database_path,
                    overwrite,
                    expected_destination_state,
                    publication_callback=publication_callback,
                )
            publication_completed = True
        finally:
            if temporary_path is not None:
                try:
                    _remove_owned_temporary_artifacts(temporary_path)
                except Exception:
                    if not publication_completed:
                        raise
                    # Publication is committed and must still be reported as
                    # success, but retry once so a transient unlink failure
                    # does not strand a generator-owned hardlink/sidecar.
                    try:
                        _remove_owned_temporary_artifacts(temporary_path)
                    except Exception:
                        pass
    except GenerationCancelled:
        _timestamped_message(
            "Test database generation stopped (cancelled) after "
            f"{perf_counter() - generation_started:.2f} s: {database_path}."
        )
        raise
    except Exception as error:
        _timestamped_message(
            "Test database generation stopped (failed) after "
            f"{perf_counter() - generation_started:.2f} s: {database_path} "
            f"({type(error).__name__}: {error})."
        )
        raise

    try:
        _timestamped_message(
            "Test database generation stopped (completed) in "
            f"{perf_counter() - generation_started:.2f} s: {database_path}."
        )
    except Exception:
        # Publication is already committed; a closed output stream must not
        # turn success into an apparent failure after replacing the database.
        pass
    return database_path


def generate_database_from_csv(
        csv_path,
        database_path,
        overwrite=False,
        rng=None,
        cancelled_callback=None,
        ):
    """Validate a CSV and generate its QCoDeS database."""
    specifications = read_specifications(csv_path)
    path = generate_database(
        specifications,
        database_path,
        overwrite=overwrite,
        rng=rng,
        cancelled_callback=cancelled_callback,
    )
    return path, specifications


def _argument_parser():
    parser = argparse.ArgumentParser(
        prog="qplot-generate-db",
        description="Generate two-sinusoid test runs in a QCoDeS database.",
    )
    parser.add_argument("specification", nargs="?", help="input CSV specification")
    parser.add_argument("database", nargs="?", help="output QCoDeS .db file")
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument(
        "--write-example",
        metavar="CSV",
        help="write a ready-to-use example CSV instead of generating a database",
    )
    output_group.add_argument(
        "--write-collection",
        metavar="DIRECTORY",
        help="copy the installed cumulative instruction CSV collection",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing output file",
    )
    return parser


def main(argv: Sequence[str] | None = None):
    """Run the ``qplot-generate-db`` command-line interface."""
    parser = _argument_parser()
    args = parser.parse_args(argv)

    try:
        if args.write_example:
            if args.specification or args.database:
                parser.error("positional arguments cannot be used with --write-example")
            output_path = write_example_csv(args.write_example, overwrite=args.overwrite)
            print(f"Wrote example CSV: {output_path}")
            return 0

        if args.write_collection:
            if args.specification or args.database:
                parser.error("positional arguments cannot be used with --write-collection")
            output_paths = copy_instruction_collection(
                args.write_collection,
                overwrite=args.overwrite,
            )
            print(
                f"Wrote {len(output_paths)} instruction CSV files to "
                f"{Path(args.write_collection)}"
            )
            return 0

        if not args.specification or not args.database:
            parser.error(
                "specification and database are required unless --write-example or "
                "--write-collection is used"
            )
        output_path, specifications = generate_database_from_csv(
            args.specification,
            args.database,
            overwrite=args.overwrite,
        )
    except (OSError, SpecificationError) as error:
        parser.exit(2, f"qplot-generate-db: error: {error}\n")

    total_points = sum(specification.point_count for specification in specifications)
    _timestamped_message(
        f"Generated {output_path} with {len(specifications)} runs "
        f"and {total_points} points."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
