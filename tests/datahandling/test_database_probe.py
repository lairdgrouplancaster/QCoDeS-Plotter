"""The isolated access probe keeps its identity, deadline, and error contract."""

import json
import sqlite3
import subprocess
import sys

import pytest

from qplot.datahandling import _database_probe


@pytest.mark.pure
@pytest.mark.parametrize("identity", [None, [123, 456], ["windows", "volume", 12]])
@pytest.mark.parametrize("quarantined", [False, True])
@pytest.mark.parametrize("timeout", [-1, 2.5])
def test_probe_forwards_identity_and_bounded_deadline(
    monkeypatch, capsys, identity, quarantined, timeout,
):
    calls = []
    monkeypatch.setattr(_database_probe, "monotonic", lambda: 10.0)
    monkeypatch.setattr(
        _database_probe, "probe_read_only_database",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert _database_probe.main([
        "-measurement.db", "1" if quarantined else "0", str(timeout),
        json.dumps(identity),
    ]) == 0

    assert calls == [(("-measurement.db",), {
        "ignore_unpaired_wal": quarantined,
        "expected_database_identity": tuple(identity) if identity else None,
        "deadline": 10.0 + max(0.0, timeout),
        "_check_sqlite_lock_bytes": True,
    })]
    assert capsys.readouterr() == ("", "")


@pytest.mark.pure
@pytest.mark.parametrize("error", [TimeoutError("read deadline"), OSError("file unavailable")])
def test_probe_emits_structured_failures(monkeypatch, capsys, error):
    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(_database_probe, "probe_read_only_database", fail)
    assert _database_probe.main(["measurement.db", "0", "3", "null"]) == 1

    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == _database_probe.DATABASE_ACCESS_ERROR_PREFIX + json.dumps({
        "type": type(error).__name__, "message": str(error),
    }) + "\n"


@pytest.mark.pure
def test_probe_does_not_swallow_process_interrupt(monkeypatch):
    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(_database_probe, "probe_read_only_database", interrupt)
    with pytest.raises(KeyboardInterrupt):
        _database_probe.main(["measurement.db", "0", "3", "null"])


def test_probe_module_accepts_leading_dash_filename_without_source_changes(tmp_path, monkeypatch):
    from qplot.datahandling import database

    database_path = tmp_path / "-measurement.db"
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("CREATE TABLE samples (value REAL)")
        connection.execute("INSERT INTO samples VALUES (1.25)")
        connection.commit()
    finally:
        connection.close()
    contents = database_path.read_bytes()
    modified = database_path.stat().st_mtime_ns
    monkeypatch.chdir(tmp_path)
    results = []
    run = subprocess.run

    def capture_probe_output(*args, **kwargs):
        result = run(*args, **kwargs)
        results.append(result)
        return result

    monkeypatch.setattr(database.subprocess, "run", capture_probe_output)

    assert database.database_access_error(database_path.name) is None

    assert len(results) == 1
    assert results[0].stdout == results[0].stderr == ""
    assert database_path.read_bytes() == contents
    assert database_path.stat().st_mtime_ns == modified
    assert {path.name for path in tmp_path.iterdir()} == {database_path.name}


@pytest.mark.pure
def test_probe_module_keeps_heavy_dependencies_lazy():
    result = subprocess.run(
        [sys.executable, "-c", """
import sys
import qplot.datahandling._database_probe
heavy_modules = {'qcodes', 'numpy', 'PyQt6', 'pyqtgraph', 'jsonschema'}
assert heavy_modules.isdisjoint(sys.modules)
assert 'qplot.datahandling.database' not in sys.modules
"""],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
