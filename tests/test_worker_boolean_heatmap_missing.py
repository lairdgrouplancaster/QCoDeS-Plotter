"""Real Boolean QCoDeS heatmaps retain missing samples and coordinates."""

import hashlib
from functools import partial
from pathlib import Path

import numpy as np
import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.datahandling.parameter_data import flatten_record_columns
from qplot.datahandling.qcodes_cache import update_cache_parameter_data
from qplot.datahandling.readonly import load_by_id_read_only
from qplot.tools.operation_registry import OperationCall
from qplot.tools.plot_tools import differentiate, subtract_mean
from qplot.tools.worker import loader


def _protected_state(path):
    state = []
    for suffix in ("", "-wal", "-journal"):
        artifact = Path(str(path) + suffix)
        if not artifact.exists():
            state.append((suffix, None))
            continue
        info = artifact.stat()
        state.append((
            suffix, info.st_ino, info.st_size, info.st_mtime_ns,
            hashlib.sha256(artifact.read_bytes()).digest(),
        ))
    return state


def _run(worker):
    finished, errors = [], []
    worker.emitter.finished.connect(finished.append)
    worker.emitter.errorOccurred.connect(errors.append)
    worker.run()
    assert errors == []
    assert finished == [True]
    assert worker._sql_connection is None


@pytest.mark.parametrize("shape", [None, (6,), (2, 3), (1, 2, 3)])
@pytest.mark.parametrize("case", ["partial", "invalid-fast", "invalid-slow"])
@pytest.mark.parametrize("swapped", [False, True])
@pytest.mark.parametrize("loading", ["normal", "forced", "threshold"])
@pytest.mark.parametrize("split", [False, True], ids=["one-record", "two-records"])
def test_boolean_missing_heatmap_from_public_qcodes(
    tmp_path, shape, case, swapped, loading, split,
):
    path = tmp_path / "boolean.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("Boolean missing values", "test")
    measurement = Measurement(exp=experiment)
    for name in ("slow", "fast"):
        measurement.register_custom_parameter(name, paramtype="array")
    measurement.register_custom_parameter(
        "signal", paramtype="array", setpoints=("slow", "fast"),
    )
    if shape is not None:
        measurement.set_shapes({"signal": shape})

    slow = np.repeat([0., 1.], 3)
    fast = np.tile([0., 1., 2.], 2)
    signal = np.array([False, True, False, True, False, True])
    expected = signal.astype(float).reshape(2, 3)
    if case == "partial":
        slow, fast, signal = slow[:4], fast[:4], signal[:4]
        expected[1, 1:] = np.nan
    else:
        (fast if case == "invalid-fast" else slow)[4] = np.nan
        expected[1, 1] = np.nan

    writer_dataset = None
    try:
        with measurement.run(write_in_background=False, in_memory_cache=False) as saver:
            writer_dataset = saver.dataset
            parts = (slice(0, 3), slice(3, None)) if split else (slice(None),)
            for part in parts:
                # DataSaver restricts Boolean array dtypes. The public DataSet
                # API persists them using QCoDeS' ordinary array adapter.
                writer_dataset.add_results([{
                    "slow": slow[part], "fast": fast[part], "signal": signal[part],
                }])
            run_id = saver.run_id
    finally:
        if writer_dataset is not None:
            writer_dataset.conn.close()
        experiment.conn.close()

    protected = _protected_state(path)
    dataset = load_by_id_read_only(run_id, str(path))
    try:
        specs = {spec.name: spec for spec in dataset.get_parameters()}
        axes = {"x": "slow", "y": "fast"} if swapped else {"x": "fast", "y": "slow"}
        expected_grid = expected.T if swapped else expected
        kwargs = (
            {"force_sql_heatmap": True} if loading == "forced"
            else {"max_full_heatmap_points": 3} if loading == "threshold"
            else {}
        )
        worker = loader(dataset.cache, specs["signal"], specs, axes, **kwargs)
        _run(worker)

        def compare(worker):
            np.testing.assert_array_equal(worker.axis_data["x"], [0., 1.] if swapped else [0., 1., 2.])
            np.testing.assert_array_equal(worker.axis_data["y"], [0., 1., 2.] if swapped else [0., 1.])
            np.testing.assert_array_equal(np.asarray(worker.dataGrid, dtype=float), expected_grid)

        compare(worker)
        if not worker.loaded_from_sql_heatmap:
            assert update_cache_parameter_data(
                dataset.cache, "signal", worker.updated_read_status,
                worker.updated_write_status, worker.cache_data,
                dataset_completed=True,
            )
            raw = flatten_record_columns(worker.cache_data["signal"])["signal"]
            np.testing.assert_array_equal(raw[:len(signal)], signal)
            assert raw.dtype.kind == "b"
            saved = raw.copy()
            cached = loader(dataset.cache, specs["signal"], specs, axes, read_data=False)
            _run(cached)
            compare(cached)
            np.testing.assert_array_equal(
                flatten_record_columns(worker.cache_data["signal"])["signal"], saved,
            )
            # Plot operations consume the masked private grid, with both
            # derivatives and mean removal retaining the actual missing cells.
            for axis in ("x", "y"):
                derivative = loader(
                    dataset.cache, specs["signal"], specs, axes, read_data=False,
                    operations=[OperationCall(
                        "differentiate", partial(differentiate, axis),
                        derivative_axis=axis, cooperative=True,
                    )],
                )
                _run(derivative)
                axis_number = 1 if axis == "x" else 0
                reference = np.gradient(
                    expected_grid, cached.axis_data[axis], axis=axis_number,
                )
                np.testing.assert_array_equal(derivative.dataGrid, reference)
            centered = loader(
                dataset.cache, specs["signal"], specs, axes, read_data=False,
                operations=[OperationCall(
                    "subtract mean", partial(subtract_mean, "x"), cooperative=True,
                )],
            )
            _run(centered)
            np.testing.assert_allclose(
                centered.dataGrid,
                expected_grid - np.nanmean(expected_grid, axis=1, keepdims=True),
                rtol=1e-15, atol=0, equal_nan=True,
            )
            np.testing.assert_array_equal(
                flatten_record_columns(worker.cache_data["signal"])["signal"], saved,
            )
    finally:
        dataset.conn.close()
    assert _protected_state(path) == protected
