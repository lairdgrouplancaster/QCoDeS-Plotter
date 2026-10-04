"""Complex 1D QCoDeS data fails normally, without publishing a partial plot."""

import csv
import hashlib
import sys
import warnings

import numpy as np
import pytest
from PyQt6 import QtWidgets as qtw
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.datahandling.qcodes_cache import (
    cache_parameter_is_synchronized,
    snapshot_cache_parameter_publication_state,
)
from qplot.windows import main as main_window
from tests._window_lifecycle import close_main_window
from tests.windows.test_plot_integration import configure_temp_qplot, wait_for


def create_measurement(path, kind):
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("complex_line", sample_name="test")
    measurement = Measurement(exp=experiment)
    array = "array" in kind
    coordinate = "coordinate" in kind
    measurement.register_custom_parameter(
        "x", paramtype="array" if array else "complex" if coordinate else "numeric",
    )
    measurement.register_custom_parameter(
        "signal", setpoints=("x",),
        paramtype="array" if array else "numeric" if coordinate else "complex",
    )
    measurement.register_custom_parameter("real_x")
    measurement.register_custom_parameter("real_signal", setpoints=("real_x",))
    x = np.array([0., 1., 2.])
    signal = np.array([1 + 10j, 2 + 20j, 3 + 30j])
    if coordinate:
        x, signal = signal, x
    with measurement.run(write_in_background=False) as datasaver:
        if array:
            # Unequal record lengths exercise QCoDeS' object-array storage.
            slices = (slice(0, 1), slice(1, 3)) if "ragged" in kind else (slice(None),)
            for selection in slices:
                datasaver.add_result(("x", x[selection]), ("signal", signal[selection]))
        else:
            for xv, yv in zip(x, signal, strict=True):
                datasaver.add_result(("x", xv), ("signal", yv))
        for index in range(3):
            datasaver.add_result(("real_x", index), ("real_signal", index + 4))
        dataset = datasaver.dataset
        run_id, guid = dataset.run_id, dataset.guid
        params = {param.name: param for param in dataset.get_parameters()}
    dataset.conn.close()
    experiment.conn.close()
    return run_id, guid, x, signal, params


def database_state(path):
    return {
        suffix: (hashlib.sha256(artifact.read_bytes()).digest(), artifact.stat().st_mtime_ns)
        if artifact.exists() else None
        for suffix in ("", "-wal", "-journal")
        for artifact in (path.with_name(path.name + suffix),)
    }


@pytest.mark.parametrize("kind", [
    "signal", "coordinate", "array_signal", "array_coordinate",
    "ragged_array_signal", "ragged_array_coordinate",
])
def test_complex_measurement_actual_plot_error_export_and_recovery(tmp_path, monkeypatch, kind):
    configure_temp_qplot(monkeypatch, tmp_path)
    path = tmp_path / "measurement.db"
    run_id, guid, x, signal, params = create_measurement(path, kind)
    before = database_state(path)
    uncaught = []
    monkeypatch.setattr(sys, "excepthook", lambda *args: uncaught.append(args))
    window = main_window.MainWindow()
    try:
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.close_database(status=False)
        assert window.load_file(str(path))
        wait_for(lambda: not window._database_load_active and not window._database_detail_active)
        window.monitor.stop()
        assert window.selected_run_id == run_id
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            window.openPlot(guid=guid, params=[params["signal"]], show=False)
            plot = window.windows[-1]
            wait_for(lambda: not plot.worker.running)
            plot.monitor.stop()
        assert not uncaught, [str(args[1]) for args in uncaught]
        assert not any("complex" in str(warning.message).lower() for warning in caught)
        assert "complex" in plot._last_error_text.lower()
        assert "not supported" in plot._last_error_text.lower()
        assert ("x" if "coordinate" in kind else "signal") in plot._last_error_text
        assert not plot.__dict__.get("_qplot_display_synchronized", False)
        assert not cache_parameter_is_synchronized(plot.ds.cache, "signal")
        assert "axis_data" not in plot.__dict__
        assert plot.line.getData() == (None, None)
        assert not isinstance(getattr(plot.worker, "_qplot_publication_snapshot", None), dict)

        # Raw run export remains available even when the plot cannot be rendered.
        target = tmp_path / "raw.csv"
        monkeypatch.setattr(qtw.QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
        window.measurementBox.setText("1")
        window.exportRunCsv()
        with target.open(newline="", encoding="utf-8") as csv_file:
            rows = list(csv.DictReader(csv_file))
        np.testing.assert_array_equal([complex(row["x"]) for row in rows], x)
        np.testing.assert_array_equal([complex(row["signal"]) for row in rows], signal)

        window.openPlot(guid=guid, params=[params["real_signal"]], show=False)
        real_plot = window.windows[-1]
        wait_for(lambda: not real_plot.worker.running)
        real_plot.monitor.stop()
        assert real_plot._qplot_display_synchronized
        np.testing.assert_array_equal(real_plot.line.getData()[0], [0, 1, 2])
        np.testing.assert_array_equal(real_plot.line.getData()[1], [4, 5, 6])
        if kind == "signal":
            # A later refresh must reject complex operation output too, while
            # retaining the already displayed real curve and cache slots.
            updates = []
            real_plot.trace_updated.connect(lambda: updates.append(True))
            for axis in ("x", "y"):
                fields = {
                    name: getattr(real_plot, name)
                    for name in ("axis_data", "axis_param", "display_param", "last_ds_len")
                }
                cache = real_plot.ds.cache
                prior_cache = snapshot_cache_parameter_publication_state(cache, "real_signal")
                updates.clear()

                def complex_output(data, axis=axis):
                    # Even zero imaginary parts have an unsupported dtype.
                    return {axis: data[axis].astype(complex)}

                monkeypatch.setattr(real_plot.oper_widget, "get_data", lambda op=complex_output: [op])
                real_plot.refreshWindow(force=True)
                wait_for(lambda: not real_plot.worker.running)
                real_plot.monitor.stop()
                assert "complex" in real_plot._last_error_text.lower()
                assert not real_plot._qplot_display_synchronized
                assert not updates
                assert all(getattr(real_plot, name) is value for name, value in fields.items())
                current_cache = snapshot_cache_parameter_publication_state(cache, "real_signal")
                assert current_cache == prior_cache
                np.testing.assert_array_equal(real_plot.line.getData()[0], [0, 1, 2])
                np.testing.assert_array_equal(real_plot.line.getData()[1], [4, 5, 6])
                assert not isinstance(getattr(real_plot.worker, "_qplot_publication_snapshot", None), dict)

                monkeypatch.setattr(real_plot.oper_widget, "get_data", lambda: [])
                real_plot.refreshWindow(force=True)
                wait_for(lambda: not real_plot.worker.running)
                real_plot.monitor.stop()
                assert real_plot._qplot_display_synchronized
        assert not uncaught
    finally:
        close_main_window(window)
    assert database_state(path) == before
