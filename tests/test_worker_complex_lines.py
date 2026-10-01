"""Line validation names the data role independently of displayed axis order."""

from types import SimpleNamespace

import numpy as np
import pytest

from qplot.tools.worker import loader


@pytest.mark.parametrize("swapped", [False, True])
@pytest.mark.parametrize("complex_name,role", [("x", "coordinate"), ("signal", "measurement data")])
@pytest.mark.parametrize("imaginary", [0, 10])
def test_line_rejects_complex_dtype_in_either_axis_order(swapped, complex_name, role, imaginary):
    worker = loader.__new__(loader)
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
