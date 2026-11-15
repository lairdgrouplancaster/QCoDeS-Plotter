"""Line validation names the data role independently of displayed axis order."""

from types import SimpleNamespace

import numpy as np
import pytest

from tests._plot_worker import make_plot_worker


@pytest.mark.parametrize("swapped", [False, True])
@pytest.mark.parametrize("imaginary", [0, 10])
def test_line_rejects_complex_coordinates_in_either_axis_order(swapped, imaginary):
    complex_name, role = "x", "coordinate"
    worker = make_plot_worker()
    worker.param = SimpleNamespace(name="signal", depends_on_=("x",))
    worker.param_dict = {
        name: SimpleNamespace(name=name) for name in ("x", "signal")
    }
    worker.axes_dict = {"x": "signal" if swapped else "x"}
    data = {"x": np.arange(3.), "signal": np.arange(1., 4.)}
    data[complex_name] = data[complex_name] + imaginary * 1j
    original = data[complex_name].copy()
    with pytest.raises(ValueError, match=f"'{complex_name}' contains.*{role}"):
        worker.for_1d(data, np.ones(3, dtype=bool))
    np.testing.assert_array_equal(data[complex_name], original)


@pytest.mark.parametrize("swapped", [False, True])
@pytest.mark.parametrize("mode", ["magnitude", "phase", "real", "imag", "imag_vs_real"])
@pytest.mark.parametrize("imaginary", [0, 10])
def test_line_projects_complex_measurements_without_changing_samples(swapped, mode, imaginary):
    worker = make_plot_worker(
        axes={"x": "signal" if swapped else "x"}, complex_mode=mode,
    )
    recorded = np.arange(1., 4.) + imaginary * 1j
    data = {"x": np.arange(3.), "signal": recorded.copy()}
    axes, parameters = worker.for_1d(data, np.ones(3, dtype=bool))
    np.testing.assert_array_equal(data["signal"], recorded)
    np.testing.assert_array_equal(worker.complex_samples.values, recorded)
    np.testing.assert_array_equal(worker.complex_samples.coordinates[0], data["x"])
    assert worker.complex_samples.mode == mode
    if mode == "imag_vs_real":
        np.testing.assert_array_equal(axes["x"], recorded.real)
        np.testing.assert_array_equal(axes["y"], recorded.imag)
        assert parameters["x"].label == "Re(Signal)"
        assert parameters["y"].label == "Im(Signal)"
    else:
        expected = {
            "magnitude": np.abs(recorded), "phase": np.angle(recorded, deg=True),
            "real": recorded.real, "imag": recorded.imag,
        }[mode]
        measurement_axis, coordinate_axis = ("x", "y") if swapped else ("y", "x")
        np.testing.assert_allclose(axes[measurement_axis], expected)
        np.testing.assert_array_equal(axes[coordinate_axis], data["x"])
        assert parameters[measurement_axis].unit == ("°" if mode == "phase" else "A")
    assert worker.param.label == "Signal"
    assert worker.param.unit == "A"
