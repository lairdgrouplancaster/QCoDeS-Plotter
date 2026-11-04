"""Timing updates retain measured costs and reject unusable reports."""

import json

import pytest

from scripts.update_ci_timings import merge_reports, report_timings
from tests._ci_scheduling import read_timings


def test_reports_merge_disjoint_partitions_and_use_slowest_file_cost(tmp_path):
    first = tmp_path / "mac-partition-1.xml"
    first.write_text('''<testsuites><testsuite>
      <testcase file="tests/test_a.py" name="a" time="1.25"/>
      <testcase file="tests/test_a.py" name="b" time="2.75"/>
    </testsuite></testsuites>''')
    second = tmp_path / "mac-partition-2.xml"
    second.write_text('''<testsuites><testsuite>
      <testcase file="tests/test_b.py" name="a" time="8"/>
    </testsuite></testsuites>''')
    windows = tmp_path / "windows.xml"
    windows.write_text('''<testsuites><testsuite>
      <testcase file="tests\\test_a.py" name="a" time="2"/>
      <testcase file="tests\\test_a.py" name="b" time="3"/>
      <testcase file="tests\\test_b.py" name="a" time="0"><skipped/></testcase>
    </testsuite></testsuites>''')
    profile = merge_reports([first, second, windows])
    assert profile["files"] == {
        "tests/test_a.py": {"seconds": 5, "tests": 2},
        "tests/test_b.py": {"seconds": 8, "tests": 1},
    }
    output = tmp_path / "timings.json"
    output.write_text(json.dumps(profile))
    assert read_timings(output) == {"tests/test_a.py": (5.0, 2), "tests/test_b.py": (8.0, 1)}


@pytest.mark.parametrize("content", [
    '<testcase file="tests/test_a.py"><failure/></testcase>',
    '<testcase file="tests/test_a.py"><error/></testcase>',
    '<testcase name="no_file"/>',
    '<testcase file="/absolute/tests/test_a.py"/>',
    '<testcase file="tests/../test_a.py"/>',
    '<testcase file="tests/test_a.py" time="NaN"/>',
    '<testcase file="tests/test_a.py" time="-1"/>',
    '',
])
def test_unusable_report_is_rejected(tmp_path, content):
    report = tmp_path / "report.xml"
    report.write_text(f"<testsuites><testsuite>{content}</testsuite></testsuites>")
    with pytest.raises(ValueError):
        report_timings(report)
