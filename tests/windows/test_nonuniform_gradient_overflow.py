"""Nonuniform stencils use original-coordinate rational oracles."""

import hashlib
from fractions import Fraction

import numpy as np
import pytest
from qcodes.dataset import Measurement, initialise_or_create_database_at, new_experiment

from qplot.datahandling.readonly import load_by_id_read_only
from qplot.tools.operation_registry import OperationCall
from qplot.tools.plot_tools import differentiate
from qplot.tools.worker import loader

COORDINATES = np.array([-1e-308, 0., 1e-308, 2.01e-308, 3.01e-308])
VALUES = np.array([-1., -1., 1., -1., -1.])


def rational_gradient(x, y):
    coordinates = [Fraction(v.item() if isinstance(v, np.generic) else v) for v in x]
    values = [Fraction(v.item() if isinstance(v, np.generic) else v) for v in y]
    spacings = [b - a for a, b in zip(coordinates[:-1], coordinates[1:], strict=True)]
    slopes = [(b - a) / step for a, b, step in zip(values[:-1], values[1:], spacings, strict=True)]
    interior = [
        (after * left + before * right) / (before + after)
        for before, after, left, right in zip(
            spacings[:-1], spacings[1:], slopes[:-1], slopes[1:], strict=True,
        )
    ]

    def representable(value):
        try:
            return float(value)
        except OverflowError:
            return np.inf if value > 0 else -np.inf

    return np.array([representable(v) for v in [slopes[0], *interior, slopes[-1]]])


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("dtype", [np.float64, np.float32, np.int64, object])
def test_nonuniform_overflowed_secants_keep_finite_stencils(reverse, dtype):
    x, y = COORDINATES.copy(), VALUES.astype(dtype)
    if reverse:
        x, y = x[::-1], y[::-1]
    expected = rational_gradient(x, y)
    assert np.all(np.isfinite(expected))
    with np.errstate(over="ignore", invalid="ignore"):
        observed = differentiate("x", {"x": x, "y": y, "z": None})["y"]
    np.testing.assert_array_equal(observed, expected)


def test_nonuniform_derivative_qcodes_worker_preserves_finite_signal(tmp_path):
    database = tmp_path / "nonuniform.db"
    initialise_or_create_database_at(database)
    experiment = new_experiment("Nonuniform stencil oracle", "test")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("x", paramtype="array")
    measurement.register_custom_parameter("signal", paramtype="array", setpoints=("x",))
    try:
        with measurement.run() as saver:
            saver.add_result(("x", COORDINATES), ("signal", VALUES))
        run_id = saver.run_id
    finally:
        experiment.conn.close()
    before = hashlib.sha256(database.read_bytes()).digest()
    dataset = load_by_id_read_only(run_id, database)
    try:
        specs = dataset.paramspecs
        operation = OperationCall(
            "dy/dx", lambda data, cancelled_callback=None: differentiate(
                "x", data, cancelled_callback=cancelled_callback,
            ), derivative_axis="x", cooperative=True,
        )
        worker = loader(dataset.cache, specs["signal"], specs, {"x": "x", "y": "signal"},
                        operations=[operation])
        finished, errors = [], []
        worker.emitter.finished.connect(finished.append)
        worker.emitter.errorOccurred.connect(errors.append)
        with np.errstate(over="ignore", invalid="ignore"):
            worker.run()
        assert finished == [True] and errors == []
        np.testing.assert_array_equal(worker.axis_data["y"], rational_gradient(COORDINATES, VALUES))
        np.testing.assert_array_equal(worker.cache_data["signal"]["signal"].ravel(), VALUES)
    finally:
        dataset.conn.close()
    assert hashlib.sha256(database.read_bytes()).digest() == before


@pytest.mark.parametrize("axis", ["x", "y"])
@pytest.mark.parametrize("dtype", [float, object])
def test_nonuniform_heatmap_stencil_repair_preserves_missing_cells(axis, dtype):
    grid = np.vstack([VALUES, VALUES, VALUES]).astype(dtype)
    grid[1, 2] = np.nan
    expected = np.vstack([
        rational_gradient(COORDINATES, VALUES),
        np.array([0., np.nan, np.nan, np.nan, 0.]),
        rational_gradient(COORDINATES, VALUES),
    ])
    if axis == "y":
        grid, expected = grid.T, expected.T
    coordinates = {axis: COORDINATES, "y" if axis == "x" else "x": np.arange(3.), "z": grid}
    with np.errstate(over="ignore", invalid="ignore"):
        observed = differentiate(axis, coordinates)["z"]
    np.testing.assert_array_equal(observed, expected)


def test_nonuniform_stencil_repair_does_not_hide_infinite_measurements():
    y = VALUES.copy()
    y[2] = np.inf
    with np.errstate(over="ignore", invalid="ignore"):
        observed = differentiate("x", {"x": COORDINATES, "y": y, "z": None})["y"]
    np.testing.assert_array_equal(observed, [0., np.inf, np.nan, -np.inf, 0.])


def test_nonuniform_stencil_repair_retains_true_unrepresentable_derivatives():
    x, y = np.array([0., 1e-308, 3e-308]), np.array([-1., 1., -3.])
    with np.errstate(over="ignore", invalid="ignore"):
        observed = differentiate("x", {"x": x, "y": y, "z": None})["y"]
    np.testing.assert_array_equal(observed, rational_gradient(x, y))


def test_nonuniform_stencil_repair_is_cancellable():
    checks = 0

    def cancelled():
        nonlocal checks
        checks += 1
        return checks == 4

    with np.errstate(over="ignore", invalid="ignore"), pytest.raises(InterruptedError):
        differentiate("x", {"x": COORDINATES, "y": np.arange(1025.),
                            "z": np.tile(VALUES, (1025, 1))}, cancelled_callback=cancelled)
    assert checks == 4
