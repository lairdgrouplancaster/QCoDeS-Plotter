"""Snapshot planned steps stay associated with current QCoDeS dependents."""

import numpy as np
import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.datahandling import readSQL
from tests.datahandling.test_array_record_metadata import (
    _artifact_state,
    _protect_source,
)


def _read_detail(path, identity, monkeypatch):
    run_id, guid = identity
    before = _artifact_state(path)
    _protect_source(monkeypatch, path)
    metadata = readSQL.get_runs_via_sql(str(path))[run_id]
    detail = readSQL.get_snapshot_selected_run_detail(
        str(path), run_id, guid, metadata,
    )
    assert _artifact_state(path) == before
    return detail


@pytest.mark.parametrize("sizes", [(6, 4), (4, 6)])
def test_independent_array_plans_have_each_axis_step_count(tmp_path, monkeypatch, sizes):
    path = tmp_path / "independent.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("independent plans", sample_name="regression")
    measurement = Measurement(exp=experiment)
    for axis in ("x", "y"):
        measurement.register_custom_parameter(axis, paramtype="array")
    measurement.register_custom_parameter("za", paramtype="array", setpoints=("x",))
    measurement.register_custom_parameter("zb", paramtype="array", setpoints=("y",))
    measurement.set_shapes({"za": (sizes[0],), "zb": (sizes[1],)})
    try:
        with measurement.run(write_in_background=False) as saver:
            saver.add_result(("x", np.arange(sizes[0])), ("za", np.arange(sizes[0])))
            saver.add_result(("y", np.arange(sizes[1])), ("zb", np.arange(sizes[1])))
            identity = saver.run_id, saver.dataset.guid
    finally:
        saver.dataset.conn.close()
        experiment.conn.close()
    detail = _read_detail(path, identity, monkeypatch)
    assert [(s.name, s.first, s.last, s.steps) for s in detail.setpoint_summaries] == [
        ("x", None, None, sizes[0]), ("y", None, None, sizes[1]),
    ]


@pytest.mark.parametrize("sizes", [(4, 4), (4, 6)])
def test_shared_array_axis_requires_agreeing_planned_steps(tmp_path, monkeypatch, sizes):
    path = tmp_path / "shared.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("shared plans", sample_name="regression")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("x", paramtype="array")
    for dependent in ("za", "zb"):
        measurement.register_custom_parameter(dependent, paramtype="array", setpoints=("x",))
    measurement.set_shapes({"za": (sizes[0],), "zb": (sizes[1],)})
    try:
        with measurement.run(write_in_background=False) as saver:
            for dependent, size in zip(("za", "zb"), sizes, strict=True):
                saver.add_result(("x", np.arange(size)), (dependent, np.arange(size)))
            identity = saver.run_id, saver.dataset.guid
    finally:
        saver.dataset.conn.close()
        experiment.conn.close()
    detail = _read_detail(path, identity, monkeypatch)
    assert [(s.name, s.steps) for s in detail.setpoint_summaries] == (
        [("x", 4)] if sizes[0] == sizes[1] else []
    )


@pytest.mark.parametrize("axes", [("x", "y"), ("y", "x")])
def test_scalar_plans_keep_declared_axis_order_when_aggregates_are_skipped(
    tmp_path, monkeypatch, axes,
):
    path = tmp_path / "scalar.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("scalar plans", sample_name="regression")
    measurement = Measurement(exp=experiment)
    for axis in ("x", "y", "a"):
        measurement.register_custom_parameter(axis)
    measurement.register_custom_parameter("za", setpoints=axes)
    measurement.register_custom_parameter("zb", setpoints=("a",))
    measurement.set_shapes({"za": (2, 3), "zb": (4,)})
    try:
        with measurement.run(write_in_background=False) as saver:
            saver.add_result(("x", 0), ("y", 0), ("za", 1))
            saver.add_result(("a", 0), ("zb", 2))
            identity = saver.run_id, saver.dataset.guid
    finally:
        saver.dataset.conn.close()
        experiment.conn.close()
    monkeypatch.setattr(readSQL, "MAX_SELECTED_RUN_SETPOINT_SUMMARY_ROWS", 1)
    detail = _read_detail(path, identity, monkeypatch)
    expected = [(axes[0], None, None, 2), (axes[1], None, None, 3), ("a", None, None, 4)]
    assert [(s.name, s.first, s.last, s.steps) for s in detail.setpoint_summaries] == expected
