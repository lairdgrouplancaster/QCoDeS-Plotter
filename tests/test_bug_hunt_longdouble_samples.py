"""QCoDeS NumPy longdouble cells remain valid exact-fallback inputs."""

from fractions import Fraction

import numpy as np
import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.datahandling.readonly import load_by_id_read_only
from qplot.tools.finite_means import FiniteBinMeans
from qplot.tools.plot_tools import differentiate, subtract_mean
from qplot.tools.sample_statistics import (
    finite_mean,
    finite_standard_deviation,
    sample_fraction,
)
from qplot.tools.worker import loader
from qplot.windows._native_averaging import NativePlotItem
from qplot.windows._native_transforms import NativePlotDataItem
from tests.windows.test_complex_line_data import database_state


def _ratio(value):
    if isinstance(value, (float, np.floating)):
        return Fraction(*value.as_integer_ratio())
    return Fraction(int(value))


def test_longdouble_ratio_retains_its_mantissa():
    value = np.longdouble(1) + np.finfo(np.longdouble).eps
    assert sample_fraction(value) == 1 + Fraction(*np.finfo(np.longdouble).eps.as_integer_ratio())


def test_mixed_longdouble_cells_center_and_differentiate(qapplication):
    values = np.array([1, np.longdouble(2.5), np.longdouble(4)], dtype=object)
    x = np.array([0, np.longdouble(1), np.longdouble(2)], dtype=object)
    assert finite_mean(values) == 2.5
    np.testing.assert_allclose(finite_standard_deviation(values), np.sqrt(1.5))
    np.testing.assert_array_equal(
        subtract_mean("x", {"x": x, "y": np.array([0]), "z": values[None, :]})["z"],
        [[-1.5, 0., 1.5]],
    )
    np.testing.assert_array_equal(
        differentiate("x", {"x": x, "y": values, "z": None})["y"], [1.5, 1.5, 1.5],
    )
    line = NativePlotDataItem(x, values)
    line.setSubtractMeanMode(True)
    np.testing.assert_array_equal(line.getData()[1], [-1.5, 0., 1.5])
    line.setSubtractMeanMode(False)
    line.setDerivativeMode(True)
    np.testing.assert_array_equal(line.getData()[1], [1.5, 1.5])
    line.setDerivativeMode(False)
    line.setFftMode(True)
    np.testing.assert_allclose(line.getData()[1], np.abs(np.fft.rfft([1., 2.5, 4.])) / 3)
    bins = FiniteBinMeans((1,))
    indices = (np.zeros(3, dtype=int),)
    bins.add(indices, values)
    if bins.begin_exact_replay():
        bins.replay(indices, values)
    np.testing.assert_array_equal(bins.means(), [2.5])


@pytest.mark.parametrize("replacement", [False, True])
def test_native_mixed_longdouble_average_exact_fallback(qapplication, replacement):
    values = np.array([1e308, 1e-308], dtype=np.longdouble)
    plot = NativePlotItem()
    try:
        arrays = ([np.zeros(1)] if replacement else []) + [np.ones(2, dtype=np.int64), values]
        # Invoke synchronously so a regression is an ordinary test exception,
        # rather than an unhandled Qt checkbox callback.
        for array in arrays:
            source = NativePlotDataItem(np.arange(len(array)), array)
            source.setDynamicRangeLimit(None)
            plot.addItem(source)
            plot.addAvgCurve(source)
        count, average = next(iter(plot.avgCurves.values()))
        weight = 2 if replacement else 1
        expected = [float((weight + _ratio(value)) / (weight + 1)) for value in values]
        assert count == weight + 1
        np.testing.assert_array_equal(average.getData()[1], expected)
    finally:
        plot.clear()


def test_real_qcodes_mixed_longdouble_records_support_exact_operations(tmp_path, qapplication):
    path = tmp_path / "longdouble.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("longdouble", sample_name="test")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("x", paramtype="array")
    measurement.register_custom_parameter("signal", paramtype="array", setpoints=("x",))
    try:
        with measurement.run(write_in_background=False, in_memory_cache=False) as saver:
            saver.add_result(("x", np.array([0, 1])), ("signal", np.array([1, 2])))
            saver.add_result(("x", np.array([2, 3])),
                             ("signal", np.array([3.25, 4.5], dtype=np.longdouble)))
            run_id = saver.dataset.run_id
    finally:
        saver.dataset.conn.close()
        experiment.conn.close()
    protected = database_state(path)
    dataset = load_by_id_read_only(run_id, str(path))
    try:
        parameters = {p.name: p for p in dataset.get_parameters()}
        worker = loader(dataset.cache, parameters["signal"], parameters, {"x": "x"})
        errors, finished = [], []
        worker.emitter.errorOccurred.connect(errors.append)
        worker.emitter.finished.connect(finished.append)
        worker.run()
        assert errors == [] and finished == [True]
        x, values = worker.axis_data["x"], worker.axis_data["y"]
        assert values.dtype.kind == "O"
        if np.finfo(np.longdouble).nmant > np.finfo(float).nmant:
            assert any(isinstance(value, np.longdouble) for value in values)
        exact = list(map(_ratio, values))
        mean = sum(exact) / len(exact)
        centered = np.array([float(value - mean) for value in exact])
        assert finite_mean(values) == float(mean)
        np.testing.assert_allclose(finite_standard_deviation(values), np.std(centered))
        bins = FiniteBinMeans((1,))
        indices = (np.zeros(len(values), dtype=int),)
        bins.add(indices, values)
        if bins.begin_exact_replay():
            bins.replay(indices, values)
        np.testing.assert_array_equal(bins.means(), [float(mean)])
        np.testing.assert_array_equal(
            subtract_mean("x", {"x": x, "y": np.array([0]), "z": values[None, :]})["z"],
            centered[None, :],
        )
        line = NativePlotDataItem(x, values)
        line.setSubtractMeanMode(True)
        np.testing.assert_array_equal(line.getData()[1], centered)
        line.setSubtractMeanMode(False)
        line.setDerivativeMode(True)
        np.testing.assert_array_equal(line.getData()[1], [1., 1.25, 1.25])
        np.testing.assert_array_equal(
            differentiate("x", {"x": x, "y": values, "z": None})["y"],
            [1., 1.125, 1.25, 1.25],
        )
        line.setDerivativeMode(False)
        line.setFftMode(True)
        expected_fft = np.abs(np.fft.rfft(np.asarray(values, dtype=float))) / 4
        np.testing.assert_allclose(line.getData()[1], expected_fft)
        np.testing.assert_array_equal(worker.axis_data["y"], values)
    finally:
        dataset.conn.close()
    assert database_state(path) == protected
