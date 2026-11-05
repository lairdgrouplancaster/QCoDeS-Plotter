"""Shared numeric primitives do not initialize database or GUI dependencies."""

import subprocess
import sys

import pytest

pytestmark = pytest.mark.pure


def test_numeric_samples_import_is_independent_of_database_and_gui():
    result = subprocess.run(
        [sys.executable, "-c", (
            "import sys; import qplot.datahandling.numeric_samples; "
            "assert not any(name.split('.')[0] in ('qcodes', 'sqlite3', 'PyQt6') "
            "for name in sys.modules); "
            "assert 'qplot.datahandling.parameter_data' not in sys.modules"
        )],
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
