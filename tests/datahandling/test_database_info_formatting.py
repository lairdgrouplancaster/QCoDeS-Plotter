"""Diagnostic display policy can be checked without Qt or database access."""

import subprocess
import sys

import pytest

from qplot.datahandling._database_info import (
    _database_info_groups,
    _format_bytes,
    _format_database_info,
    _timestamp_value,
)

pytestmark = pytest.mark.pure


@pytest.fixture
def summary():
    return {
        "path": "/data/measurement.db",
        "folder": "/data",
        "filename": "measurement.db",
        "file_size": 2048,
        "file_modified": None,
        "user_version": 9,
        "application_id": 0,
        "page_count": 2,
        "page_size": 4096,
        "table_count": 3,
        "experiment_count": 1,
        "run_count": 0,
        "latest_run": None,
    }


def test_report_preserves_display_order_and_section_breaks(summary):
    assert _format_database_info(summary) == (
        "Database: measurement.db\n"
        "Path: /data/measurement.db\n"
        "Folder: /data\n"
        "File size: 2.0 KB\n"
        "Last modified: Not recorded\n"
        "SQLite allocated size: 8.0 KB\n\n"
        "Database schema version: 9\n"
        "SQLite application_id: 0\n\n"
        "Tables: 3\n"
        "Experiments: 1\n"
        "Runs: 0\n\n"
        "Latest run: None"
    )


@pytest.mark.parametrize("completed", [0, 1])
def test_latest_run_uses_distinct_missing_value_and_status_labels(summary, completed):
    summary["latest_run"] = {
        "run_id": 12, "name": "", "is_completed": completed,
        "run_timestamp": None, "completed_timestamp": "invalid", "guid": "guid-12",
    }
    assert _database_info_groups(summary)[-1] == [
        ("Latest run ID", "12"),
        ("Latest run name", "Unknown"),
        ("Latest run status", "completed" if completed else "running or incomplete"),
        ("Latest run started", "Not recorded"),
        ("Latest run completed", "invalid"),
        ("Latest run GUID", "guid-12"),
    ]


@pytest.mark.parametrize("missing", ["page_count", "page_size"])
def test_missing_page_extent_is_unknown(summary, missing):
    summary[missing] = None
    assert ("SQLite allocated size", "Unknown") in _database_info_groups(summary)[0]


@pytest.mark.parametrize("value, expected", [
    (None, "Unknown"), (0, "0 B"), (1023, "1023 B"), (1024, "1.0 KB"),
    (1024 ** 2, "1.0 MB"), (1024 ** 3, "1.0 GB"), (1024 ** 4, "1.0 TB"),
    (1024 ** 5, "1024.0 TB"),
])
def test_byte_units_keep_binary_thresholds_and_final_unit(value, expected):
    assert _format_bytes(value) == expected


@pytest.mark.parametrize("value, expected", [
    (None, "Not recorded"), ("", "Not recorded"), ("invalid", "invalid"),
    (float("nan"), "nan"),
])
def test_missing_or_invalid_timestamp_retains_fallback(value, expected):
    assert _timestamp_value(value) == expected


def test_presentation_import_does_not_load_reader_or_gui():
    result = subprocess.run(
        [sys.executable, "-c", """
import sys
import qplot.datahandling._database_info
for name in ('qcodes', 'numpy', 'PyQt6', 'qplot.datahandling.database',
             'qplot.datahandling.readonly', 'qplot.datahandling.readSQL'):
    assert name not in sys.modules, name
"""],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
