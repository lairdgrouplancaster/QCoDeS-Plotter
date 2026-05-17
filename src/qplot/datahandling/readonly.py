import sqlite3
from pathlib import Path

import qcodes
from qcodes.dataset import load_by_guid, load_by_id
from qcodes.dataset.sqlite.database import connect, get_DB_debug


def set_qcodes_database_location(database_path):
    """Point QCoDeS at a database without initialising or upgrading it."""
    qcodes.config.core.db_location = str(database_path)


def qcodes_read_only_connection(database_path):
    """Open a QCoDeS SQLite connection with SQLite read-only enforcement."""
    return connect(database_path, get_DB_debug(), read_only=True)


def load_by_guid_read_only(guid):
    """Load a QCoDeS dataset by GUID through a read-only connection."""
    return load_by_guid(guid, read_only=True)


def load_by_id_read_only(run_id):
    """Load a QCoDeS dataset by run ID through a read-only connection."""
    return load_by_id(run_id, read_only=True)


def sqlite_read_only_uri(database_path):
    """Build a SQLite URI that opens an existing database read-only."""
    return f"{Path(database_path).resolve().as_uri()}?mode=ro"


def sqlite_read_only_connection(database_path, timeout=10, **kwargs):
    """Open a direct sqlite3 connection with SQLite read-only enforcement."""
    return sqlite3.connect(
        sqlite_read_only_uri(database_path),
        timeout=timeout,
        uri=True,
        **kwargs,
    )
