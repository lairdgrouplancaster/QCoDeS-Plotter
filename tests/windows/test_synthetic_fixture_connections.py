"""Synthetic GUI fixtures must release their own QCoDeS writer connections."""

import sqlite3

import pytest
from qcodes.dataset import initialise_or_create_database_at
from qcodes.dataset.measurements import DataSaver
from qcodes.dataset.sqlite.connection import AtomicConnection

from tests.windows import test_plot_integration as fixtures


@pytest.mark.parametrize("helper_name", [
    "build_synthetic_database",
    "append_nonuniform_heatmap_run",
    "build_missing_cell_array_database",
])
@pytest.mark.parametrize("fail_recording", [False, True], ids=["success", "recording-error"])
def test_synthetic_helpers_close_owned_connections(tmp_path, monkeypatch, helper_name, fail_recording):
    connections = []
    original_init = AtomicConnection.__init__

    def record_connection(connection, *args, **kwargs):
        original_init(connection, *args, **kwargs)
        connections.append(connection)

    monkeypatch.setattr(AtomicConnection, "__init__", record_connection)
    path = tmp_path / "fixture-ownership.db"
    helper = getattr(fixtures, helper_name)
    if helper_name == "append_nonuniform_heatmap_run":
        initialise_or_create_database_at(str(path), journal_mode="DELETE")
        arguments = ()
    else:
        arguments = (path,)

    def fail_add_result(*args, **kwargs):
        raise RuntimeError("injected fixture recording failure")

    try:
        if fail_recording:
            monkeypatch.setattr(DataSaver, "add_result", fail_add_result)
            with pytest.raises(RuntimeError, match="injected fixture recording failure"):
                helper(*arguments)
        else:
            helper(*arguments)
        assert connections
        for connection in connections:
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                connection.cursor()
    finally:
        # Retain and retire every fixture connection even when the regression
        # assertion fails, so it cannot contaminate another warning assertion.
        for connection in connections:
            connection.close()
