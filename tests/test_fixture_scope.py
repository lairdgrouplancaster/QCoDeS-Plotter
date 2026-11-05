"""Exercise the pure/integration boundary through isolated pytest sessions."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.pure


def _run_suite(tmp_path, source, *, block_integration_imports=False):
    (tmp_path / "conftest.py").write_text(
        Path(__file__).with_name("conftest.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nmarkers = pure: skip default integration fixtures\n",
        encoding="utf-8",
    )
    (tmp_path / "test_context.py").write_text(source, encoding="utf-8")
    environment = os.environ.copy()
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    # This subprocess tests fixture policy in its own suite. The enclosing CI
    # session performs the installed-wheel audit and report publication.
    for name in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "QPLOT_CI_WHEEL_AUDIT"):
        environment.pop(name, None)
    code = ""
    if block_integration_imports:
        code = """
import importlib.abc
import sys
class NoIntegrationImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'qcodes', 'PyQt6', 'matplotlib'}:
            raise AssertionError(f'Unexpected integration dependency: {fullname}')
sys.meta_path.insert(0, NoIntegrationImports())
"""
    code += "\nimport pytest\nraise SystemExit(pytest.main(['-q', '--strict-markers', '--color=no']))\n"
    return subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path, env=environment,
        capture_output=True, text=True, timeout=45,
    )


def test_pure_scope_never_imports_qt_or_database_dependencies(tmp_path):
    result = _run_suite(tmp_path, """
import pytest
from qplot.updates import check_due
pytestmark = pytest.mark.pure
def test_pure_behavior():
    assert check_due(0, 100)
""", block_integration_imports=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout


@pytest.mark.parametrize("close_snapshot", [False, True], ids=["leaked", "closed"])
def test_unmarked_scope_keeps_qt_isolation_and_snapshot_leak_audit(tmp_path, close_snapshot):
    result = _run_suite(tmp_path, f"""
import sqlite3
import tempfile
import qcodes
from PyQt6.QtWidgets import QApplication
from qplot.datahandling import readonly

original_path = qcodes.config.core.db_location

def test_integration_context():
    assert QApplication.instance() is not None
    qcodes.config.core.db_location = 'fixture-test-change.db'
    class OwnedConnection(sqlite3.Connection):
        pass
    connection = sqlite3.connect(':memory:', factory=OwnedConnection)
    snapshot = tempfile.TemporaryDirectory(prefix='qplot-fixture-audit-')
    readonly._attach_snapshot_cleanup(connection, snapshot)
    if {close_snapshot!r}:
        connection.close()

def test_next_test_restores_global_database_location():
    assert qcodes.config.core.db_location == original_path
""")
    output = result.stdout + result.stderr
    if close_snapshot:
        assert result.returncode == 0, output
        assert "2 passed" in output
    else:
        assert result.returncode == 1, output
        assert "DID NOT RAISE" in output
        assert "2 passed, 1 error" in output
