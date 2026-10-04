"""Planned progress follows the rows current QCoDeS actually persists."""

import hashlib
import json
from pathlib import Path

import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.datahandling import readSQL
from qplot.datahandling.trusted_live_queries import TrustedMetadataQueryAdapter
from qplot.windows._widgets._run_formatting import progress_percent_value
from tests.datahandling.test_stage5c_real_qcodes import (
    _load_basic_runs,
    _ReadOnlySqliteExecutor,
)


def _protected(path):
    state = []
    for suffix in ("", "-wal", "-journal"):
        artifact = Path(str(path) + suffix)
        if artifact.exists():
            info = artifact.stat()
            state.append((suffix, info.st_ino, info.st_size, info.st_mtime_ns,
                          hashlib.sha256(artifact.read_bytes()).digest()))
        else:
            state.append((suffix, None))
    return state


@pytest.mark.parametrize("case,roots", [
    ("inferred", 1), ("multi-basis", 2), ("independent", 2),
    ("inference-only", 1),
])
def test_public_qcodes_partial_and_completed_rows(tmp_path, case, roots):
    path = tmp_path / "inferred.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("inferred row counts", "test")
    measurement = Measurement(exp=experiment)
    if case != "inference-only":
        measurement.register_custom_parameter("x")
    setpoints = ("x",) if case != "inference-only" else None
    measurement.register_custom_parameter("y", setpoints=setpoints)
    names = ["y"]
    if case == "multi-basis":
        measurement.register_custom_parameter("other", setpoints=setpoints)
        names.append("other")
    basis = None if case == "independent" else tuple(names)
    measurement.register_custom_parameter("converted", setpoints=setpoints, basis=basis)
    names.append("converted")
    measurement.set_shapes({name: (3,) for name in names})
    saver = None
    try:
        with measurement.run(write_in_background=False) as saver:
            for index in range(3):
                values = [(name, (index + 1) * (position + 1))
                          for position, name in enumerate(names)]
                if setpoints:
                    values.append(("x", index))
                saver.add_result(*values)
                if index == 1:
                    saver.flush_data_to_database(block=True)
                    before = _protected(path)
                    status = readSQL.get_run_status(saver.dataset.guid, str(path))
                    assert _protected(path) == before
                    assert status["result_count"] == 2 * roots
                    assert status["expected_results"] == 3 * roots
                    assert progress_percent_value(status) == pytest.approx(200 / 3)
            run_id = saver.run_id
    finally:
        if saver is not None:
            saver.dataset.conn.close()
        experiment.conn.close()
    before = _protected(path)
    fields = readSQL.get_runs_via_sql(str(path))[run_id]
    assert fields["result_count"] == fields["expected_results"] == 3 * roots
    executor = _ReadOnlySqliteExecutor(path)
    try:
        adapter = TrustedMetadataQueryAdapter(executor, path)
        _load_basic_runs(adapter)
        fields = adapter.cheap_run(run_id).as_dict()
        assert fields["expected_results"] == 3 * roots
    finally:
        executor.close()
    assert _protected(path) == before


def test_conflicting_inferred_plans_retain_unknown_live_count(tmp_path):
    path = tmp_path / "conflicting.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("conflicting plans", "test")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("x")
    measurement.register_custom_parameter("y", setpoints=("x",))
    measurement.register_custom_parameter("converted", setpoints=("x",), basis=("y",))
    measurement.set_shapes({"y": (3,), "converted": (5,)})
    try:
        with measurement.run(write_in_background=False) as saver:
            saver.add_result(("x", 0), ("y", 1), ("converted", 100))
            saver.flush_data_to_database(block=True)
            status = readSQL.get_run_status(saver.dataset.guid, str(path))
            assert status["result_count"] == 1
            assert status["expected_results"] is None
            assert status["expected_results_source"] is None
            assert progress_percent_value(status) is None
            run_id = saver.run_id
    finally:
        saver.dataset.conn.close()
        experiment.conn.close()
    fields = readSQL.get_runs_via_sql(str(path))[run_id]
    assert fields["expected_results"] == 1
    assert fields["expected_results_source"] == "observed"


@pytest.mark.parametrize("inferences,truncated", [
    ({"y": ["converted"], "converted": ["y"]}, False),
    ({"converted": ["missing"]}, False),
    ({"converted": ["x"]}, False),
    ({"converted": []}, False),
    ({"converted": ["y"] * 33}, True),
    ({str(i): ["y"] for i in range(257)}, True),
])
def test_ambiguous_inference_graph_does_not_invent_count(inferences, truncated):
    description = {
        "interdependencies_": {"inferences": inferences},
        "shapes": {"y": [3], "converted": [3]},
    }
    expected, limited = readSQL._bounded_expected_results_from_shapes(
        json.loads(json.dumps(description)), ["y", "converted"],
    )
    assert expected is None
    assert limited is truncated


def test_complete_but_oversized_inference_graph_is_marked_truncated():
    names = [f"p{i}" for i in range(257)]
    description = {
        "interdependencies_": {"inferences": {name: ["p0"] for name in names[1:]}},
        "shapes": {name: [3] for name in names},
    }
    assert readSQL._bounded_expected_results_from_shapes(description, names) == (None, True)
