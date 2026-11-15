"""Native Qt FFT mapping accepts valid mixed QCoDeS coordinate records."""

import cmath
import hashlib
from fractions import Fraction

import numpy as np
import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.datahandling.readonly import load_by_id_read_only
from qplot.tools.worker import loader
from qplot.windows._native_transforms import (
    NativePlotDataItem,
    native_fft_coordinate_error,
)


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("coordinates", ["uniform", "nonuniform", "integer-step"])
def test_native_fft_mixed_real_qcodes_coordinate_span(tmp_path, reverse, coordinates):
    path = tmp_path / "f.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    exp = load_or_create_experiment("mixed FFT", sample_name="test")
    measurement = Measurement(exp=exp)
    measurement.register_custom_parameter("x", paramtype="array")
    measurement.register_custom_parameter("signal", setpoints=("x",), paramtype="array")
    x = [-1e308, -5e307, 0., 1e308] if coordinates == "nonuniform" else [-1e308, 0., 1e308]
    signal = [value / 1e308 for value in x]
    if coordinates == "integer-step":
        x, signal = [2**53 + 1, float(2**53 + 2), float(2**53 + 4)], [0., 1., 3.]
    if reverse:
        x = x[::-1]
        signal = signal[::-1]
    writer = None
    try:
        with measurement.run() as saver:
            writer = saver.dataset
            for value, sample in zip(x, signal, strict=True):
                saver.add_result(
                    ("x", np.array([value], dtype=np.int64
                                   if value == 0 or isinstance(value, int) else np.float64)),
                    ("signal", np.array([sample])),
                )
            run_id = saver.dataset.run_id
    finally:
        if writer is not None:
            writer.conn.close()
        exp.conn.close()
    before = (hashlib.sha256(path.read_bytes()).digest(), path.stat().st_size,
              path.stat().st_mtime_ns)
    assert not path.with_name(path.name + "-wal").exists()
    assert not path.with_name(path.name + "-journal").exists()
    dataset = load_by_id_read_only(run_id, str(path))
    try:
        params = {p.name: p for p in dataset.get_parameters()}
        worker = loader(dataset.cache, params["signal"], params, {"x": "x"})
        errors, finished = [], []
        worker.emitter.errorOccurred.connect(errors.append)
        worker.emitter.finished.connect(finished.append)
        worker.run()
        assert errors == [] and finished == [True]
        source_x, source_y = worker.axis_data["x"], worker.axis_data["y"]
        assert source_x.dtype == object
        assert native_fft_coordinate_error(source_x) is None
        line = NativePlotDataItem(x=source_x, y=source_y)
        line.setFftMode(True)
        actual_x, actual_y = line.getData()
        # Linear samples interpolate onto the known uniform signal. Compute
        # the DFT directly, independently of qPlot and NumPy's FFT routines.
        uniform_y = np.linspace(-1., 1., len(x))
        if coordinates == "integer-step":
            uniform_y = np.array([0., 1.5, 3.])
        expected_y = [abs(sum(float(value) * cmath.exp(-2j * np.pi * k * j / len(x))
                             for j, value in enumerate(uniform_y)) / len(x))
                      for k in range(len(x) // 2 + 1)]
        expected_y[0] = abs(float(sum(map(Fraction, uniform_y)) / len(x)))
        expected_x = np.arange(len(expected_y)) * ((1. / 1e308) * (len(x) - 1) / (2 * len(x)))
        if coordinates == "integer-step":
            expected_x = np.arange(len(expected_y)) * (2. / 9.)
        np.testing.assert_allclose(actual_x, expected_x, rtol=1e-14, atol=0)
        np.testing.assert_allclose(actual_y, expected_y, rtol=1e-14, atol=1e-15)
        original_x, original_y = line.getOriginalDataset()
        np.testing.assert_array_equal(original_x, source_x)
        np.testing.assert_array_equal(original_y, source_y)
    finally:
        dataset.conn.close()
    assert (hashlib.sha256(path.read_bytes()).digest(), path.stat().st_size,
            path.stat().st_mtime_ns) == before
    assert not path.with_name(path.name + "-wal").exists()
    assert not path.with_name(path.name + "-journal").exists()


@pytest.mark.parametrize("x", [[-1e308, np.nan, 1e308], [-1e308, np.inf, 1e308],
                              [-1e308, 0, 0, 1e308], [-1e308, 1e308, 0]])
def test_mixed_fft_coordinates_still_reject_invalid_sweeps(x):
    coordinates = np.array(x, dtype=object)
    assert native_fft_coordinate_error(coordinates) is not None
    line = NativePlotDataItem(x=coordinates, y=np.arange(len(x), dtype=float), fftMode=True)
    assert line.getData()[0].size == 0
