"""Mean repeated real QCoDeS cells without losing finite measurements."""

from contextlib import contextmanager

import numpy as np
import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.datahandling.parameter_data import parameter_data_for_export
from qplot.datahandling.readonly import load_by_id_read_only
from qplot.tools.general import data2matrix
from tests.test_worker_array_heatmaps import make_worker, run_worker


@contextmanager
def repeated_dataset(tmp_path, values, *, shaped):
    path = tmp_path / "repeated-means.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("repeated means", sample_name="test")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("slow")
    measurement.register_custom_parameter("fast", paramtype="array")
    measurement.register_custom_parameter(
        "signal", paramtype="array", setpoints=("slow", "fast")
    )
    if shaped:
        measurement.set_shapes({"signal": (2, 4)})
    try:
        with measurement.run(write_in_background=False) as saver:
            for slow in (0, 1):
                saver.add_result(
                    ("slow", slow), ("fast", np.zeros(len(values))), ("signal", values)
                )
        run_id = saver.dataset.run_id
    finally:
        experiment.conn.close()
    protected = {
        suffix: (
            (
                path.with_name(path.name + suffix).read_bytes(),
                path.with_name(path.name + suffix).stat().st_mtime_ns,
            )
            if path.with_name(path.name + suffix).exists()
            else None
        )
        for suffix in ("", "-wal", "-journal")
    }
    dataset = load_by_id_read_only(run_id, str(path))
    try:
        yield dataset
    finally:
        dataset.conn.close()
        for suffix, before in protected.items():
            artifact = path.with_name(path.name + suffix)
            after = (
                (artifact.read_bytes(), artifact.stat().st_mtime_ns)
                if artifact.exists()
                else None
            )
            assert after == before


@pytest.mark.parametrize(
    "values",
    [
        np.full(4, 60000, dtype=np.float16),
        np.full(4, 3e38, dtype=np.float32),
        np.full(4, 1e308),
        np.full(4, np.finfo(float).max),
        np.array([1e308, 1e308, -1e308, -1e308]),
    ],
)
@pytest.mark.parametrize("shaped", [False, True])
def test_real_repeated_heatmap_mean(tmp_path, values, shaped):
    with repeated_dataset(tmp_path, values, shaped=shaped) as dataset:
        worker = make_worker(dataset)
        run_worker(worker)
        from fractions import Fraction

        expected = float(sum(Fraction(float(value)) for value in values) / len(values))
        np.testing.assert_array_equal(worker.axis_data["x"], [0.0])
        np.testing.assert_array_equal(worker.axis_data["y"], [0.0, 1.0])
        np.testing.assert_allclose(
            worker.dataGrid, np.full((2, 1), expected), rtol=2e-15
        )
        exported = parameter_data_for_export(dataset, "signal")["signal"]
        assert exported.dtype == values.dtype
        np.testing.assert_array_equal(exported, np.tile(values, (2, 1)))


def test_extreme_cell_recovery_preserves_small_cells_and_holes():
    rows = np.array([0.0, 0.0, 0.0, 0.0, 1.0])
    columns = np.array([0.0, 0.0, 0.0, 0.0, 1.0])
    values = np.array([1e308, 1e308, -1e308, -1e308, 1e-308])
    grid = data2matrix(rows, columns, values).to_numpy()
    np.testing.assert_array_equal(grid, [[0.0, np.nan], [np.nan, 1e-308]])
    np.testing.assert_array_equal(values, [1e308, 1e308, -1e308, -1e308, 1e-308])


def test_extreme_mean_recovery_is_cancellable():
    calls = 0

    def check_cancelled():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise InterruptedError("cancelled")

    with pytest.raises(InterruptedError, match="cancelled"):
        data2matrix(
            np.zeros(4), np.zeros(4), np.full(4, 1e308), check_cancelled=check_cancelled
        )
