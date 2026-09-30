"""Exercise CI's file ordering through real pytest-xdist workers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("prioritize_slow_files", [False, True])
def test_loadfile_preserves_tests_and_starts_the_expected_files(
    tmp_path: Path, prioritize_slow_files: bool
) -> None:
    # Deliberately give the expensive files fewer tests: ordinary loadfile
    # scheduling puts the larger files first regardless of collection order.
    file_sizes = {
        "tests/windows/test_trusted_derived_wal_grid_ui.py": 2,
        "tests/datahandling/test_stage5c_real_qcodes.py": 3,
        "tests/windows/test_trusted_derived_wal_ui.py": 1,
        "tests/windows/test_trusted_derived_wal_tiers_ui.py": 1,
        "tests/windows/test_trusted_derived_wal_snapshot_ui.py": 1,
        "tests/test_many.py": 6,
        "tests/test_more.py": 5,
    }
    expected_by_file = {}
    for filename, count in file_sizes.items():
        path = tmp_path / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        # Descending names detect accidental sorting within each file.
        names = [f"test_case_{index}" for index in reversed(range(count))]
        path.write_text(
            "\n".join(f"def {name}():\n    pass\n" for name in names),
            encoding="utf-8",
        )
        expected_by_file[filename] = [f"{filename}::{name}" for name in names]

    (tmp_path / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n")
    (tmp_path / "conftest.py").write_text(
        """import json
from pathlib import Path

def pytest_runtest_call(item):
    worker = item.config.workerinput["workerid"]
    trace = Path(__file__).parent / f"{worker}.jsonl"
    with trace.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(item.nodeid) + "\\n")
""",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    environment.pop("PYTEST_ADDOPTS", None)
    environment.pop("PYTEST_PLUGINS", None)
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "xdist.plugin",
        "-p",
        "tests._ci_scheduling",
        "-n",
        "2",
        "--dist=loadfile",
        "--max-worker-restart=0",
        "-q",
    ]
    if prioritize_slow_files:
        command.append("--no-loadscope-reorder")
    result = subprocess.run(
        command,
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    traces = [
        [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        for path in sorted(tmp_path.glob("gw*.jsonl"))
    ]
    assert len(traces) == 2
    expected_first = (
        {
            "tests/windows/test_trusted_derived_wal_grid_ui.py",
            "tests/datahandling/test_stage5c_real_qcodes.py",
        }
        if prioritize_slow_files
        else {"tests/test_many.py", "tests/test_more.py"}
    )
    assert {trace[0].split("::", 1)[0] for trace in traces} == expected_first

    observed_by_file = {}
    for trace in traces:
        worker_files = {}
        for nodeid in trace:
            filename = nodeid.split("::", 1)[0]
            worker_files.setdefault(filename, []).append(nodeid)
        # A file's tests must never be split between workers.
        assert observed_by_file.keys().isdisjoint(worker_files)
        observed_by_file.update(worker_files)
    # Every original test runs exactly once, retaining its within-file order.
    assert observed_by_file == expected_by_file
