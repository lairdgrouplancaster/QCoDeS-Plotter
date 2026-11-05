"""Run interpretation can be shared without importing a database reader."""

import json
import subprocess
import sys
from copy import deepcopy

import pytest

from qplot.datahandling.run_metadata import (
    materialize_run_basic_fields,
    materialize_run_observation,
)


def test_run_metadata_import_does_not_load_database_or_gui_dependencies():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import qplot.datahandling.run_metadata; "
            "assert not any(name.split('.')[0] in ('qcodes', 'sqlite3', 'PyQt6') "
            "for name in sys.modules); "
            "assert 'qplot.datahandling.readSQL' not in sys.modules; "
            "assert 'qplot.datahandling.readonly' not in sys.modules",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _planned_run():
    return {
        "run_description": json.dumps({
            "interdependencies_": {"dependencies": {"first": ["x"], "second": ["x"]}},
            "shapes": {"first": [6], "second": [6]},
        }),
        "parameters": "x,first,second",
        "is_completed": False,
        "read_setpoint_count": 2,
    }


def test_planned_fields_preserve_input_and_parameter_tree_counts():
    metadata = _planned_run()
    original = deepcopy(metadata)

    materialized = materialize_run_basic_fields(metadata)

    assert metadata == original
    assert materialized["measure_parameters"] == ["first", "second"]
    assert materialized["sweep_parameters"] == ["x"]
    assert materialized["point_shape"] == [6]
    assert materialized["setpoint_count"] == 6
    assert materialized["expected_results"] == 12
    assert materialized["expected_results_source"] == "planned"


@pytest.mark.parametrize("completed, expected_results", [(False, None), (True, 8)])
@pytest.mark.parametrize("observed_shape, point_shape", [([4], [4, 2]), (None, None)])
def test_observed_counts_replace_planned_fields_without_inventing_shape(
    completed, expected_results, observed_shape, point_shape,
):
    metadata = _planned_run()
    metadata["is_completed"] = completed
    original = deepcopy(metadata)

    materialized = materialize_run_observation(
        metadata,
        result_count=8,
        setpoint_shape=observed_shape,
        setpoint_count=4,
    )

    assert metadata == original
    assert materialized["point_shape"] == point_shape
    assert materialized["setpoint_shape"] == observed_shape
    assert materialized["setpoint_shape_source"] == ("observed" if observed_shape else None)
    assert materialized["setpoint_count"] == 4
    assert materialized["setpoint_count_source"] == "observed"
    assert materialized["expected_results"] == expected_results
    assert materialized["expected_results_source"] == ("observed" if completed else None)


def test_omitted_and_cleared_acquired_counts_remain_distinct():
    metadata = _planned_run()

    retained = materialize_run_observation(metadata)
    cleared = materialize_run_observation(metadata, read_setpoint_count=None)
    updated = materialize_run_observation(metadata, read_setpoint_count=4)

    assert retained["read_setpoint_count"] == 2
    assert cleared["read_setpoint_count"] is None
    assert updated["read_setpoint_count"] == 4


@pytest.mark.parametrize("description", [None, "{", "[]", 17])
def test_invalid_description_keeps_declared_parameter_fallback(description):
    materialized = materialize_run_basic_fields({
        "run_description": description,
        "parameters": "first,second",
    })

    assert materialized["measure_parameters"] == ["first", "second"]
    assert materialized["sweep_parameters"] == []
    assert materialized["point_shape"] is None
    assert materialized["expected_results"] is None
