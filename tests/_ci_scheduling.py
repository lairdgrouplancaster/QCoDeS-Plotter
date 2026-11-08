"""Bound CI workloads and start slow files early without splitting files."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

_TIMINGS = pytest.StashKey[dict[str, tuple[float, int]]]()


def read_timings(path: Path) -> dict[str, tuple[float, int]]:
    """Read one committed profile, shared by every partition and worker."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != 1 or not isinstance(data.get("files"), dict):
        raise ValueError("Expected version 1 with a files mapping")
    timings = {}
    for filename, record in data["files"].items():
        seconds, count = record["seconds"], record["tests"]
        if (
            not filename.startswith("tests/")
            or not filename.endswith(".py")
            or ".." in Path(filename).parts
            or type(seconds) not in (int, float)
            or not math.isfinite(seconds)
            or seconds < 0
            or type(count) is not int
            or count <= 0
        ):
            raise ValueError(f"Invalid file timing: {filename}")
        timings[filename] = (float(seconds), count)
    return timings


def _file_costs(files, timings: dict[str, tuple[float, int]]) -> dict[str, float]:
    """Scale known timings with collection size; estimate new files by count."""
    total_tests = sum(count for _, count in timings.values())
    total_seconds = sum(seconds for seconds, _ in timings.values())
    fallback = total_seconds / total_tests if total_tests else 1.0
    costs = {}
    for path, items in files.items():
        seconds, count = timings.get(path, (fallback, 1))
        costs[path] = max(seconds / count, 0.001) * len(items)
    return costs


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--qplot-ci-partition",
        choices=("1/2", "2/2", "1/4", "2/4", "3/4", "4/4"),
        default=None,
        help="Run one of two or four balanced, disjoint whole-file CI partitions.",
    )
    parser.addoption(
        "--qplot-ci-timings",
        default=str(Path(__file__).with_name("ci_timings.json")),
        help="Committed file timing profile used for partitioning and loadfile order.",
    )


def pytest_configure(config: pytest.Config) -> None:
    path = Path(config.getoption("qplot_ci_timings"))
    try:
        config.stash[_TIMINGS] = read_timings(path)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise pytest.UsageError(f"Cannot read CI timings from {path}: {error}") from error


def _file_partitions(costs: dict[str, float], count: int = 2) -> dict[str, int]:
    """Place expensive files first; tie order is independent of collection."""
    loads = [0.0] * count
    partitions = {}
    for path in sorted(costs, key=lambda path: (-costs[path], path)):
        selected = min(range(count), key=lambda index: loads[index])
        partitions[path] = selected
        loads[selected] += costs[path]
    return partitions


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node) -> None:
    # xdist resets each worker's "dist" option to "no" before collection.
    node.workerinput["qplot_slow_files_first"] = (
        node.config.getoption("dist") == "loadfile"
        and not node.config.getoption("loadscopereorder", default=True)
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    files: dict[str, list[pytest.Item]] = {}
    for item in items:
        files.setdefault(item.nodeid.split("::", 1)[0], []).append(item)

    partition = config.getoption("qplot_ci_partition")
    costs = _file_costs(files, config.stash[_TIMINGS])
    if partition is not None:
        selected = int(partition[0]) - 1
        assignments = _file_partitions(costs, int(partition.split("/")[1]))
        deselected = [item for item in items if assignments[item.nodeid.split("::", 1)[0]] != selected]
        items[:] = [item for item in items if assignments[item.nodeid.split("::", 1)[0]] == selected]
        config.hook.pytest_deselected(items=deselected)
        files = {path: group for path, group in files.items() if assignments[path] == selected}

    if not getattr(config, "workerinput", {}).get("qplot_slow_files_first", False):
        return

    # xdist's --no-loadscope-reorder preserves this measured-cost order.
    # Never reorder tests within a file or split a Qt/QCoDeS file across workers.
    ordered = sorted(
        files.items(),
        key=lambda entry: (-costs[entry[0]], entry[0]),
    )
    items[:] = [item for _path, group in ordered for item in group]
