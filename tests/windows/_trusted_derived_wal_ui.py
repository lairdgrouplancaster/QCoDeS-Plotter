"""Shared setup for independently scheduled real-WAL UI scenarios.

Keep the scenarios in separate test modules so pytest-xdist's loadfile scheduler
can run them across workers instead of assigning all four to one process.
"""

from __future__ import annotations

import time
from pathlib import Path

from PyQt6 import QtTest, QtWidgets
from qcodes import Station
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.dataset.sqlite.database import connect
from qcodes.parameters import ManualParameter

from qplot.testdata import (
    RunSpecification,
    enable_generation_provenance_for_writer,
    generate_database,
)
from qplot.windows._widgets import details_tables


def _process_until(predicate, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QtWidgets.QApplication.processEvents()
        if predicate():
            return
        QtTest.QTest.qWait(5)
    raise AssertionError("Stage 5C real-WAL UI condition was not reached")


def _bridge_complete(window, guid: str) -> bool:
    bridge = window._trusted_derived_bridge
    return bool(
        guid in bridge._metadata_by_guid
        and window.RunList.run_preview_is_ready(guid)
        and guid in window.infoBox.preview.cache
    )


def _prepare_live_database(path: Path, name: str):
    generate_database(
        [RunSpecification(1, f"{name}_seed", "Seed", "V", 0.0, 1.0, 3)],
        path,
    )
    initialise_or_create_database_at(str(path), journal_mode="WAL")
    writer = connect(path)
    writer.execute("PRAGMA wal_autocheckpoint = 0")
    enable_generation_provenance_for_writer(writer)
    return writer


def _start_partial_two_dependent_run(
    writer,
    name: str,
    *,
    slow_count: int,
    fast_count: int,
    acquired_count: int,
):
    experiment = load_or_create_experiment(
        f"{name}_experiment",
        sample_name=f"{name}_sample",
        conn=writer,
    )
    slow = ManualParameter(f"{name}_slow")
    fast = ManualParameter(f"{name}_fast")
    signal = ManualParameter(f"{name}_signal")
    signal_b = ManualParameter(f"{name}_signal_b")
    station = Station(slow, fast, signal, signal_b)
    measurement = Measurement(
        exp=experiment,
        name=f"{name}_run",
        station=station,
    )
    measurement.write_period = 3_600
    measurement.register_parameter(slow)
    measurement.register_parameter(fast)
    measurement.register_parameter(signal, setpoints=(slow, fast))
    measurement.register_parameter(signal_b, setpoints=(slow, fast))
    measurement.set_shapes(
        {
            signal.name: (slow_count, fast_count),
            signal_b.name: (slow_count, fast_count),
        }
    )
    context = measurement.run(write_in_background=False)
    datasaver = context.__enter__()
    for logical_index in range(acquired_count):
        slow_index, fast_index = divmod(logical_index, fast_count)
        datasaver.add_result(
            (slow, float(slow_index)),
            (fast, float(fast_index)),
            (signal, float(slow_index * 1_000 + fast_index)),
            (signal_b, float(1_000_000 + slow_index * 1_000 + fast_index)),
        )
    datasaver.flush_data_to_database(block=True)
    datasaver.dataset.add_metadata("stage5c_operator", "Ada")
    return (
        experiment,
        station,
        slow,
        fast,
        signal,
        signal_b,
        context,
        datasaver,
        datasaver.dataset,
    )


def _tree_items(tree: QtWidgets.QTreeWidget):
    iterator = QtWidgets.QTreeWidgetItemIterator(tree)
    while iterator.value() is not None:
        yield iterator.value()
        iterator += 1


def _tree_item_for_path(tree: QtWidgets.QTreeWidget, path: str):
    return next(
        item
        for item in _tree_items(tree)
        if str(item.data(0, details_tables.FULL_VALUE_PATH_ROLE) or "") == path
    )
