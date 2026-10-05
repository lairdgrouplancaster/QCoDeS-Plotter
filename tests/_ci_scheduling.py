"""Bound CI workloads and start slow files early without splitting files."""

from __future__ import annotations

import pytest

# These small files took the longest per file near the end of Windows CI.
# xdist's default test-count ordering otherwise leaves them until last.
_SLOW_FILES = {
    path: rank
    for rank, path in enumerate(
        (
            "tests/windows/test_trusted_derived_wal_grid_ui.py",
            "tests/datahandling/test_stage5c_real_qcodes.py",
            "tests/windows/test_trusted_derived_wal_ui.py",
            "tests/windows/test_trusted_derived_wal_tiers_ui.py",
            "tests/windows/test_trusted_derived_wal_snapshot_ui.py",
        )
    )
}


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--qplot-ci-partition",
        choices=("1/2", "2/2"),
        default=None,
        help="Run one of two balanced, disjoint whole-file CI partitions.",
    )


def _file_partitions(files: dict[str, list[pytest.Item]]) -> dict[str, int]:
    """Balance collected test counts; tie order is independent of collection."""
    loads = [0, 0]
    partitions = {}
    for path in sorted(files, key=lambda path: (-len(files[path]), path)):
        selected = min(range(2), key=lambda index: loads[index])
        partitions[path] = selected
        loads[selected] += len(files[path])
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
    if partition is not None:
        selected = int(partition[0]) - 1
        assignments = _file_partitions(files)
        deselected = [item for item in items if assignments[item.nodeid.split("::", 1)[0]] != selected]
        items[:] = [item for item in items if assignments[item.nodeid.split("::", 1)[0]] == selected]
        config.hook.pytest_deselected(items=deselected)
        files = {path: group for path, group in files.items() if assignments[path] == selected}

    if not getattr(config, "workerinput", {}).get("qplot_slow_files_first", False):
        return

    # Keep xdist's usual largest-count-first ordering for the other files,
    # including its stable tie order. Never reorder tests within a file.
    ordered = sorted(
        files.items(),
        key=lambda entry: (_SLOW_FILES.get(entry[0], len(_SLOW_FILES)), -len(entry[1])),
    )
    items[:] = [item for _path, group in ordered for item in group]
