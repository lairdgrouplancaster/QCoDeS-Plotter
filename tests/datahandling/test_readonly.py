import sqlite3

import pytest
import qcodes
from qcodes.dataset import initialise_or_create_database_at

from qplot.datahandling.readonly import (
    qcodes_read_only_connection,
    sqlite_read_only_connection,
)


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
