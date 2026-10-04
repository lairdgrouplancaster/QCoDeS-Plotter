"""Exercise CI's file ordering through real pytest-xdist workers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def _create_suite(tmp_path: Path) -> dict[str, list[str]]:
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
    return expected_by_file


def _run_suite(
    tmp_path: Path, prioritize_slow_files: bool, partition: str | None = None,
    filenames: list[str] | None = None,
) -> list[list[str]]:
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
    if partition is not None:
        command.append(f"--qplot-ci-partition={partition}")
    if filenames is not None:
        command.extend(filenames)
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
    return traces


@pytest.mark.parametrize("prioritize_slow_files", [False, True])
def test_loadfile_preserves_tests_and_starts_the_expected_files(
    tmp_path: Path, prioritize_slow_files: bool
) -> None:
    expected_by_file = _create_suite(tmp_path)
    traces = _run_suite(tmp_path, prioritize_slow_files)
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


@pytest.mark.parametrize("prioritize_slow_files", [False, True])
def test_partitions_run_every_test_once_without_splitting_files(
    tmp_path: Path, prioritize_slow_files: bool,
) -> None:
    observed = {}
    counts = []
    for partition in ("1/2", "2/2"):
        directory = tmp_path / partition.replace("/", "-")
        expected = _create_suite(directory)
        traces = _run_suite(directory, prioritize_slow_files, partition)
        files = {}
        for trace in traces:
            worker_files = {}
            for nodeid in trace:
                worker_files.setdefault(nodeid.split("::", 1)[0], []).append(nodeid)
            assert files.keys().isdisjoint(worker_files)
            files.update(worker_files)
        assert observed.keys().isdisjoint(files)
        for filename, tests in files.items():
            assert tests == expected[filename]
        observed.update(files)
        counts.append(sum(map(len, files.values())))
    assert observed == expected
    assert sorted(counts) == [9, 10]


@pytest.mark.parametrize("partition", ["1/2", "2/2"])
def test_partition_membership_is_independent_of_collection_order(
    tmp_path: Path, partition: str,
) -> None:
    results = []
    for reverse in (False, True):
        directory = tmp_path / str(reverse)
        expected = _create_suite(directory)
        filenames = list(expected)
        if reverse:
            filenames.reverse()
        traces = _run_suite(directory, True, partition, filenames)
        results.append(sorted(nodeid for trace in traces for nodeid in trace))
    assert results[0] == results[1]


@pytest.mark.parametrize("partition", ["0/2", "3/2", "1/3", "x/y", "1"])
def test_invalid_partition_cannot_silently_omit_tests(tmp_path: Path, partition: str) -> None:
    _create_suite(tmp_path)
    environment = os.environ.copy()
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    environment.pop("PYTEST_ADDOPTS", None)
    environment.pop("PYTEST_PLUGINS", None)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "tests._ci_scheduling",
         f"--qplot-ci-partition={partition}", "--collect-only"],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == pytest.ExitCode.USAGE_ERROR
    assert "invalid choice" in result.stderr
    assert not list(tmp_path.glob("gw*.jsonl"))
