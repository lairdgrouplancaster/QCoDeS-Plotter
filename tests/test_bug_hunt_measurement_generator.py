"""Synthetic finite sweeps must not persist NaN/Inf coordinates."""

import sqlite3
import warnings

import numpy as np
import pytest

from qplot.testdata import RunSpecification, generate_database


@pytest.mark.parametrize("surface", ["line", "slow", "fast"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("bound", [1e308, float(np.finfo(float).max)])
def test_generator_keeps_extreme_finite_sweep_coordinates(tmp_path, surface, reverse, bound):
    start, stop = (-bound, bound) if not reverse else (bound, -bound)
    specification = RunSpecification(
        dimensions=1 if surface == "line" else 2,
        measured_name="signal", measured_label="Signal", measured_unit="A",
        v_sd_start=start if surface != "fast" else 0.,
        v_sd_stop=stop if surface != "fast" else 2.,
        v_sd_points=3,
        v_g_start=(start if surface == "fast" else 0.) if surface != "line" else None,
        v_g_stop=(stop if surface == "fast" else 2.) if surface != "line" else None,
        v_g_points=3 if surface != "line" else None,
    )
    output = tmp_path / "finite.db"
    with warnings.catch_warnings(record=True) as observed:
        warnings.simplefilter("always", RuntimeWarning)
        generate_database([specification], output, rng=np.random.default_rng(0))
    connection = sqlite3.connect(output.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        table = connection.execute("SELECT result_table_name FROM runs").fetchone()[0]
        selected = '"V_G"' if surface == "fast" else '"V_SD"'
        values = connection.execute(f'SELECT {selected} FROM "{table}" ORDER BY id').fetchall()
    finally:
        connection.close()
    actual = np.array(values, dtype=float).ravel()
    expected = np.array([start, 0., stop])
    if surface == "slow":
        expected = np.repeat(expected, 3)
    elif surface == "fast":
        expected = np.tile(expected, 3)
    np.testing.assert_array_equal(actual, expected)
    assert not [warning for warning in observed if issubclass(warning.category, RuntimeWarning)]
