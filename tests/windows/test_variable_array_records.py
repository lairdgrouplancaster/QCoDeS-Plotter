"""Real variable-shaped QCoDeS records through normal plotting and run export."""

import csv

import numpy as np
import pytest
from PyQt6 import QtWidgets as qtw
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.datahandling import parameter_data as decoding
from qplot.datahandling.LoadFromDB import load_param_data_from_db
from qplot.datahandling.parameter_data import get_parameter_data_for_one_paramtree
from qplot.datahandling.readonly import load_by_id_read_only
from qplot.tools.worker import loader
from qplot.windows import main as main_window
from qplot.windows._plot_actions import _csv_scalar_columns
from tests._window_lifecycle import close_main_window
from tests.windows.test_complex_line_data import database_state
from tests.windows.test_plot_integration import configure_temp_qplot, wait_for


def create_array_run(path, kind, *, shaped=False, missing=False):
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("array_shapes", sample_name="test")
    measurement = Measurement(exp=experiment)
    heatmap = kind.startswith("heatmap")
    measurement.register_custom_parameter("fast", paramtype="array")
    if heatmap:
        measurement.register_custom_parameter("slow", paramtype="array")
    measurement.register_custom_parameter(
        "signal", paramtype="array",
        setpoints=("slow", "fast") if heatmap else ("fast",),
    )
    widths = (3, 3) if "equal" in kind else (2, 3)
    if shaped:
        measurement.set_shapes({"signal": (4, 3) if heatmap else (2 * sum(widths),)})
    expected = {name: [] for name in ("signal", "fast", "slow") if heatmap or name != "slow"}
    offset = 0
    try:
        with measurement.run(write_in_background=False) as datasaver:
            for record_index, width in enumerate(widths):
                if heatmap:
                    fast = np.tile(np.arange(width, dtype=float), (2, 1))
                    slow = np.repeat(np.arange(2 * record_index, 2 * record_index + 2,
                                               dtype=float)[:, None], width, axis=1)
                    signal = 10 * slow + fast
                    values = {"slow": slow, "fast": fast, "signal": signal}
                else:
                    fast = np.arange(offset, offset + 2 * width, dtype=float).reshape(2, width)
                    signal = 2 * fast
                    if "precision" in kind:
                        fast = np.nextafter(fast, np.inf)
                        signal = np.arange(offset, offset + 2 * width,
                                           dtype=np.int64).reshape(2, width) + 2**53
                    values = {"fast": fast, "signal": signal}
                if missing:
                    values["signal"][0, 1] = np.nan
                    values["fast"][1, 0] = np.nan
                if "ragged" in kind:
                    values = {name: value.ravel() for name, value in values.items()}
                # Storage layout must not change the logical order of pairs.
                values["fast"] = np.asfortranarray(values["fast"])
                datasaver.add_result(*values.items())
                for name, value in values.items():
                    expected[name].append(value.ravel().copy())
                offset += 2 * width
            dataset = datasaver.dataset
            run_id, guid = dataset.run_id, dataset.guid
            params = {param.name: param for param in dataset.get_parameters()}
    finally:
        datasaver.dataset.conn.close()
        experiment.conn.close()
    return run_id, guid, params, {name: np.concatenate(parts) for name, parts in expected.items()}


@pytest.mark.parametrize("shaped", [False, True])
@pytest.mark.parametrize("kind", [
    "line_varying", "line_equal", "line_ragged", "line_precision",
    "line_varying_missing", "heatmap_varying", "heatmap_equal", "heatmap_ragged",
    "heatmap_varying_missing",
])
def test_actual_normal_plot_and_export_run_csv(tmp_path, monkeypatch, kind, shaped):
    configure_temp_qplot(monkeypatch, tmp_path)
    path = tmp_path / "measurement.db"
    run_id, guid, params, expected = create_array_run(
        path, kind, shaped=shaped, missing="missing" in kind,
    )
    before = database_state(path)
    errors = []
    window = main_window.MainWindow()
    try:
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        monkeypatch.setattr(window, "show_error", lambda *args: errors.append(args))
        window.close_database(status=False)
        assert window.load_file(str(path))
        wait_for(lambda: not window._database_load_active and not window._database_detail_active)
        window.monitor.stop()
        assert window.selected_run_id == run_id
        prior_plot_count = len(window.windows)
        window.openPlot(guid=guid, params=[params["signal"]], show=False)
        wait_for(lambda prior_plot_count=prior_plot_count: len(window.windows) > prior_plot_count)
        plot = window.windows[-1]
        wait_for(lambda: not plot.worker.running)
        plot.monitor.stop()
        assert plot._qplot_display_synchronized, getattr(plot, "_last_error_text", "")
        if kind.startswith("line"):
            assert not plot.worker.loaded_from_sql_heatmap
            valid = ~np.isnan(expected["fast"]) & ~np.isnan(expected["signal"])
            for axis, name in (("x", "fast"), ("y", "signal")):
                np.testing.assert_array_equal(plot.line.getData()["xy".index(axis)], expected[name][valid])
            # Exercise publication, then a cache-backed normal refresh too.
            plot.refreshWindow(force=True)
            wait_for(lambda: not plot.worker.running)
            plot.monitor.stop()
            np.testing.assert_array_equal(plot.line.getData()[0], expected["fast"][valid])
            np.testing.assert_array_equal(plot.line.getData()[1], expected["signal"][valid])
        else:
            valid = np.isfinite(expected["fast"]) & np.isfinite(expected["signal"])
            for fast, slow, signal in zip(expected["fast"], expected["slow"],
                                           expected["signal"], strict=True):
                if not np.isfinite(fast) or not np.isfinite(signal):
                    continue
                x = np.flatnonzero(plot.axis_data["x"] == fast).item()
                y = np.flatnonzero(plot.axis_data["y"] == slow).item()
                assert plot.dataGrid[y, x] == signal
            assert np.count_nonzero(np.isfinite(plot.dataGrid)) == np.count_nonzero(valid)
            assert plot.worker.loaded_from_sql_heatmap == kind.startswith("heatmap_varying")

        target = tmp_path / "raw.csv"
        monkeypatch.setattr(qtw.QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
        window.measurementBox.setText("1")
        window.exportRunCsv()
        assert errors == []
        with target.open(newline="", encoding="utf-8") as csv_file:
            reader = csv.DictReader(csv_file)
            assert set(reader.fieldnames) == set(expected)
            rows = list(reader)
        assert len(rows) == len(expected["signal"])
        for name, values in expected.items():
            convert = int if values.dtype.kind in "iu" else float
            np.testing.assert_array_equal(
                [convert(row[name]) if row[name] else np.nan for row in rows], values,
            )
    finally:
        close_main_window(window)
    assert database_state(path) == before


@pytest.mark.parametrize("shaped", [False, True])
def test_incremental_normal_decoding_across_different_record_shapes(tmp_path, shaped):
    path = tmp_path / "measurement.db"
    run_id, _, _, expected = create_array_run(path, "line_varying", shaped=shaped)
    before = database_state(path)
    dataset = load_by_id_read_only(run_id, str(path))
    try:
        read, write = {}, {}
        data = {"signal": {name: np.array([]) for name in expected}}
        for end, count in ((1, 4), (2, 10), (2, 10)):
            read, write, data = load_param_data_from_db(
                dataset.conn, dataset.table_name, dataset.description,
                "signal", write, read, data, end=end,
            )
            assert read["signal"] == min(end, 2)
            samples = _csv_scalar_columns(data["signal"])
            for name, values in expected.items():
                np.testing.assert_array_equal(samples[name][:count], values[:count])
        # The shared decoder supplies two object cells, each with its own shape.
        records, count = get_parameter_data_for_one_paramtree(
            dataset.conn, dataset.table_name, dataset.description, "signal",
        )
        assert count == 2
        assert records["signal"].shape == (2,)
        assert [record.shape for record in records["signal"]] == [(2, 2), (2, 3)]
    finally:
        dataset.conn.close()
    assert database_state(path) == before


@pytest.mark.parametrize("cancel", [False, True], ids=["decode-error", "cancel"])
def test_normal_decoder_unwinds_read_on_error_or_cancellation(tmp_path, monkeypatch, cancel):
    path = tmp_path / "measurement.db"
    run_id, _, params, _ = create_array_run(path, "line_varying")
    before = database_state(path)
    dataset = load_by_id_read_only(run_id, str(path))
    try:
        worker = loader(dataset.cache, params["signal"], params, {"x": "fast"})
        expand = decoding._expand_data_to_arrays
        observed_connections = []
        failure = ValueError("Unrelated decoding error")

        def stop_during_expansion(*args):
            observed_connections.append(worker._sql_connection)
            if cancel:
                worker.cancel()
                return expand(*args)
            raise failure

        monkeypatch.setattr(decoding, "_expand_data_to_arrays", stop_during_expansion)
        finished, errors = [], []
        worker.emitter.finished.connect(finished.append)
        worker.emitter.errorOccurred.connect(errors.append)
        worker.run()
        assert finished == [False]
        assert errors == ([] if cancel else [failure])
        assert worker._sql_connection is None
        assert not hasattr(worker, "axis_data")
        assert not hasattr(worker, "cache_data")
        assert len(observed_connections) == 1
        with pytest.raises(Exception, match="closed"):
            observed_connections[0].cursor()
    finally:
        dataset.conn.close()
    assert database_state(path) == before
