"""A complex measurement has one projection across differently typed QCoDeS records."""
import numpy as np
import pytest
from qcodes.dataset import Measurement, initialise_or_create_database_at, new_experiment

from tests.support.windows.plot3d import main as main
from tests.support.windows.plot_integration import wait_for


@pytest.mark.parametrize("mixed_integer_plane", (False, True), ids=("real-only-depth", "integer-and-complex"))
def test_phase_and_original_readings_across_depths(main, tmp_path, mixed_integer_plane):
    path = tmp_path / "capacitance.db"
    initialise_or_create_database_at(path, journal_mode="DELETE")
    experiment = new_experiment("Capacitance", "synthetic")
    try:
        measurement = Measurement(exp=experiment)
        axes = ("field", "bias", "gate")
        for name in axes:
            measurement.register_custom_parameter(name, paramtype="array")
        measurement.register_custom_parameter("signal", unit="fF", paramtype="array", setpoints=axes)
        with measurement.run(write_in_background=False, in_memory_cache=False) as saver:
            for field, values in ((3, [-5., -11.]), (7, [1-8j, 5+2j])):
                saver.add_result(("field", np.full(2, field)), ("bias", np.zeros(2)), ("gate", np.zeros(2)),
                                 ("signal", np.asarray(values)))
            if mixed_integer_plane:
                saver.add_result(("field", np.array([7])), ("bias", np.array([0])), ("gate", np.array([0])),
                                 ("signal", np.asarray([3], dtype=np.int64)))
        guid = saver.dataset.guid
        if saver.dataset.conn is not experiment.conn:
            saver.dataset.conn.close()
    finally:
        experiment.conn.close()
    window, errors = main
    window.config.update("user_preference.complex_nd", "phase")
    assert window.load_file(str(path))
    wait_for(lambda: not window._database_load_active and window._selected_run_guid == guid)
    window.plotRunButton.click()
    wait_for(lambda: len(window.windows) == 1 and window.windows[0]._qplot_display_synchronized)
    plot = window.windows[0]
    plot.openSweep("d")
    wait_for(lambda: len(window.windows) == 2 and (window.windows[-1]._qplot_display_synchronized or errors))
    assert not errors
    cut = window.windows[-1]
    assert plot.display_param.unit == cut.display_param.unit == "°"
    np.testing.assert_array_equal(cut.axis_data["x"], [3, 7])
    np.testing.assert_allclose(cut.axis_data["y"], [180., np.degrees(np.arctan2(-6, 9 if mixed_integer_plane else 6))])
    np.testing.assert_array_equal(cut.complex_samples.values, [-11+0j, 3+0j if mixed_integer_plane else 5+2j])
