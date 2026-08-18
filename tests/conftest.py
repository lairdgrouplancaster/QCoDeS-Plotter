"""Shared pytest setup for qPlot's Qt-based tests."""

import os
import sqlite3
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6 import QtWidgets as qtw


def ensure_qapplication():
    """Return the process-wide QApplication, creating it for headless tests."""
    app = qtw.QApplication.instance()
    if app is None:
        app = qtw.QApplication([])
    return app


@pytest.fixture(scope="session", autouse=True)
def qapplication():
    return ensure_qapplication()


@pytest.fixture(autouse=True)
def assert_qcodes_snapshot_connections_closed(monkeypatch):
    """Require every test-owned QCoDeS snapshot to be explicitly closed."""
    from qplot.datahandling import readonly as readonly_module

    snapshot_connections = []
    original_attach_snapshot_cleanup = readonly_module._attach_snapshot_cleanup

    def record_snapshot_connection(connection, snapshot):
        snapshot_directory = Path(snapshot.name)
        original_attach_snapshot_cleanup(connection, snapshot)
        snapshot_connections.append((connection, snapshot_directory))

    monkeypatch.setattr(
        readonly_module,
        "_attach_snapshot_cleanup",
        record_snapshot_connection,
    )
    yield

    for connection, snapshot_directory in snapshot_connections:
        with pytest.raises((sqlite3.ProgrammingError, RuntimeError)):
            connection.cursor()
        assert not snapshot_directory.exists()
