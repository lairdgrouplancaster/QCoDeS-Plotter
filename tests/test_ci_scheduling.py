"""Exercise CI's file ordering through real pytest-xdist workers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.update_ci_timings import merge_reports
from tests._ci_scheduling import _file_costs, _file_partitions, read_timings


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

    # A fixed profile makes these subprocess checks independent of production
    # timings while proving that small expensive files receive priority.
    seconds = [40, 35, 6, 4, 3, 1, 1]
    (tmp_path / "ci_timings.json").write_text(json.dumps({
        "version": 1,
        "files": {
            filename: {"seconds": duration, "tests": count}
            for (filename, count), duration in zip(file_sizes.items(), seconds, strict=True)
        },
    }))
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
        f"--qplot-ci-timings={tmp_path / 'ci_timings.json'}",
        "-o", "junit_family=xunit1",
        f"--junitxml={tmp_path / 'results.xml'}",
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
    assert 1 <= len(traces) <= 2
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
    profile = merge_reports([tmp_path / "results.xml"])
    assert {name: record["tests"] for name, record in profile["files"].items()} == {
        name: len(tests) for name, tests in expected_by_file.items()
    }


@pytest.mark.parametrize("prioritize_slow_files", [False, True])
@pytest.mark.parametrize("partition_count", [2, 4])
def test_partitions_run_every_test_once_without_splitting_files(
    tmp_path: Path, prioritize_slow_files: bool, partition_count: int,
) -> None:
    observed = {}
    counts = []
    for partition in (f"{index}/{partition_count}" for index in range(1, partition_count + 1)):
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
    assert all(counts)
    if partition_count == 2:
        assert sorted(counts) == [9, 10]


@pytest.mark.parametrize("partition", ["1/2", "2/2", "1/4", "2/4", "3/4", "4/4"])
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


def test_partitioning_balances_duration_instead_of_test_count() -> None:
    files = {"many-a": [None] * 20, "many-b": [None] * 20, "few-a": [None], "few-b": [None]}
    timings = {"many-a": (10, 20), "many-b": (9, 20), "few-a": (5, 1), "few-b": (4, 1)}
    costs = _file_costs(files, timings)
    partitions = _file_partitions(costs)
    assert partitions["many-a"] == partitions["few-b"]
    assert partitions["many-b"] == partitions["few-a"]
    assert [sum(costs[name] for name, part in partitions.items() if part == index)
            for index in range(2)] == [14, 14]


def test_new_files_and_growing_files_keep_a_positive_count_estimate() -> None:
    files = {"known": [None] * 4, "new": [None] * 3, "skipped": [None]}
    costs = _file_costs(files, {"known": (6, 2), "skipped": (0, 1)})
    assert costs["known"] == 12
    assert costs["new"] == 6
    assert costs["skipped"] > 0
    assert _file_costs(files, {}) == {name: len(items) for name, items in files.items()}


@pytest.mark.parametrize("record", [
    {"seconds": -1, "tests": 1}, {"seconds": float("nan"), "tests": 1},
    {"seconds": float("inf"), "tests": 1}, {"seconds": True, "tests": 1},
    {"seconds": 1, "tests": 0}, {"seconds": 1, "tests": True},
])
def test_invalid_profile_cannot_change_partition_membership_silently(tmp_path, record):
    path = tmp_path / "timings.json"
    path.write_text(json.dumps({"version": 1, "files": {"tests/test_a.py": record}}))
    with pytest.raises(ValueError, match="Invalid file timing"):
        read_timings(path)


def test_repository_profile_is_valid() -> None:
    profile = read_timings(Path(__file__).with_name("ci_timings.json"))
    assert profile
