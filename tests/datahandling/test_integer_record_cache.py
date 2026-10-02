"""Incremental read-only QCoDeS cache merges retain integer array samples."""

import numpy as np
import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.datahandling.LoadFromDB import load_param_data_from_db
from qplot.datahandling.parameter_data import flatten_record_columns
from qplot.datahandling.readonly import load_by_id_read_only
from tests.windows.test_complex_line_data import database_state


@pytest.mark.parametrize("shaped", [False, True])
@pytest.mark.parametrize("tail", [[-1, -3], [-1, -3, -5], [0.5, 1.5]])
def test_incremental_mixed_records_keep_exact_acquired_values(tmp_path, shaped, tail):
    path = tmp_path / "incremental.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("cache precision", "test")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("x", paramtype="array")
    measurement.register_custom_parameter("signal", paramtype="array", setpoints=("x",))
    expected = [2**63 + 1, 2**63 + 3, *tail]
    if shaped:
        measurement.set_shapes({"signal": (len(expected) + 2,)})
    try:
        with measurement.run(write_in_background=False, in_memory_cache=False) as saver:
            saver.add_result(("x", np.arange(2)), ("signal", np.array(expected[:2], dtype=np.uint64)))
            saver.add_result(("x", np.arange(2, len(expected))), ("signal", np.array(tail)))
            run_id = saver.dataset.run_id
    finally:
        saver.dataset.conn.close()
        experiment.conn.close()
    protected = database_state(path)
    dataset = load_by_id_read_only(run_id, str(path))
    try:
        cache = {"signal": {"signal": np.array([]), "x": np.array([])}}
        read, written = {"signal": 0}, {"signal": None}
        for end, expected_prefix in ((1, expected[:2]), (2, expected)):
            read, written, cache = load_param_data_from_db(
                dataset.conn, dataset.table_name, dataset.description,
                "signal", written, read, cache, end=end,
            )
            flat = flatten_record_columns(cache["signal"])["signal"]
            assert flat[:len(expected_prefix)].tolist() == expected_prefix
            assert read["signal"] == end
            if shaped:
                assert written["signal"] == len(expected_prefix)
    finally:
        dataset.conn.close()
    assert database_state(path) == protected
