"""Only acquired planned-array samples reach real line and heatmap plots."""

import numpy as np
import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.datahandling.qcodes_cache import (
    cache_parameter_is_synchronized,
    snapshot_cache_parameter_publication_state,
)
from qplot.datahandling.readonly import load_by_id_read_only
from qplot.tools.worker import loader
from qplot.windows import main as main_window
from tests._window_lifecycle import close_main_window
from tests.windows.test_complex_line_data import database_state
from tests.windows.test_plot_integration import configure_temp_qplot, wait_for


def run_worker(worker, *, cancelled=False):
    finished, errors = [], []
    worker.emitter.finished.connect(finished.append)
    worker.emitter.errorOccurred.connect(errors.append)
    worker.run()
    assert errors == []
    assert finished == [not cancelled]
    assert worker._sql_connection is None


def wait_for_plot_refresh(plot):
    # A finished worker may still have a forced refresh queued. Wait for that
    # request to publish too before checking the cache or stopping its monitor.
    wait_for(lambda: (
        not plot.worker.running
        and not plot.__dict__.get("_refresh_pending", False)
        and not plot.__dict__.get("_refresh_pending_scheduled", False)
    ))


@pytest.mark.parametrize("kind", [
    "line", "heatmap", "flat_heatmap", "heatmap_duplicate", "overrun_line", "overrun_heatmap",
])
@pytest.mark.parametrize("initial_zero", [False, True], ids=["ten", "measured-zero"])
@pytest.mark.parametrize("finish_early", [False, True], ids=["full-scan", "partial-completion"])
def test_actual_planned_integer_array_acquisition_extent(
    tmp_path, monkeypatch, kind, initial_zero, finish_early,
):
    configure_temp_qplot(monkeypatch, tmp_path)
    path = tmp_path / "integer-array.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("integer_array_extent", sample_name="test")
    measurement = Measurement(exp=experiment)
    heatmap = "line" not in kind
    names = ("slow", "fast", "signal") if heatmap else ("fast", "signal")
    for name in names[:-1]:
        measurement.register_custom_parameter(name, paramtype="array")
    measurement.register_custom_parameter("signal", paramtype="array", setpoints=names[:-1])
    planned = (6,) if kind == "flat_heatmap" else (2, 3) if heatmap else (5,)
    measurement.set_shapes({"signal": planned})
    measurement.write_period = 3600
    first = 0 if initial_zero else 10
    if heatmap:
        records = [([0, 0], [0, 1], [first, 20])]
        if kind == "heatmap_duplicate":
            records += [([0, 0], [0, 2], [30, 0]), ([1, 1], [0, 1], [40, 50])]
        else:
            records += [([0, 1], [2, 0], [0, 30]), ([1, 1], [1, 2], [40, 50])]
        if kind == "overrun_heatmap":
            records[-1] = ([1, 1, 1], [1, 2, 3], [40, 50, 60])
    else:
        records = [([0, 1], [first, 20]), ([0, 2], [30, 0]), ([4], [40])]
        if kind == "overrun_line":
            records[-1] = ([4, 5], [40, 50])
    records = [dict(zip(names, (np.asarray(values, dtype=np.int64) for values in record),
                        strict=True)) for record in records]
    expected = {name: [] for name in names}
    window = dataset = None
    try:
        with measurement.run(write_in_background=False, in_memory_cache=False) as datasaver:
            dataset = datasaver.dataset
            params = {param.name: param for param in dataset.get_parameters()}
            axes = {"x": "fast", **({"y": "slow"} if heatmap else {})}

            def assert_values(worker, x, y, grid):
                np.testing.assert_array_equal(worker.axis_data["x"], x)
                np.testing.assert_array_equal(worker.axis_data["y"], y)
                if heatmap:
                    # Exact integer grids use object cells for NaN padding;
                    # compare missingness separately from acquired values.
                    missing = np.isnan(grid)
                    np.testing.assert_array_equal(
                        np.isnan(np.asarray(worker.dataGrid, dtype=float)), missing,
                    )
                    np.testing.assert_array_equal(worker.dataGrid[~missing], grid[~missing])

            def check_plot(*, completed=False):
                samples = {name: np.concatenate(parts) for name, parts in expected.items()}
                if heatmap:
                    # Average genuinely acquired duplicates, including zeros.
                    x, y = np.unique(samples["fast"]), np.unique(samples["slow"])
                    grid = np.full((len(y), len(x)), np.nan)
                    for iy, slow in enumerate(y):
                        for ix, fast in enumerate(x):
                            values = samples["signal"][
                                (samples["slow"] == slow) & (samples["fast"] == fast)
                            ]
                            if values.size:
                                grid[iy, ix] = np.mean(values)
                else:
                    x, y, grid = samples["fast"], samples["signal"], None
                assert not plot._last_error_text
                assert_values(plot, x, y, grid)
                if heatmap:
                    np.testing.assert_array_equal(plot.image.image, grid)
                else:
                    np.testing.assert_array_equal(plot.line.getData()[0], x)
                    np.testing.assert_array_equal(plot.line.getData()[1], y)
                prior = snapshot_cache_parameter_publication_state(plot.ds.cache, "signal")
                assert prior.write_status == len(samples["signal"])
                assert prior.read_status == len(expected["signal"])
                assert prior.dataset_completed == completed
                assert prior.synchronized == completed
                saved = {name: values.copy() for name, values in prior.data.items()}
                for name, values in prior.data.items():
                    assert values.shape == (
                        (len(samples["signal"]),) if len(samples["signal"]) > np.prod(planned)
                        else planned
                    )
                    assert values.dtype == np.int64
                    np.testing.assert_array_equal(values.ravel()[:prior.write_status], samples[name])
                    np.testing.assert_array_equal(values.ravel()[prior.write_status:], 0)

                # Completed-run operations and other cache-only passes must
                # use the same acquired extent without modifying storage.
                cached = loader(plot.ds.cache, params["signal"], params, axes, read_data=False)
                run_worker(cached)
                assert_values(cached, x, y, grid)

                cancelled = loader(plot.ds.cache, params["signal"], params, axes, read_data=False)
                acquired_mask = cancelled._acquired_sample_mask

                def cancel_after_extent(*args):
                    mask = acquired_mask(*args)
                    cancelled.cancel()
                    return mask

                monkeypatch.setattr(cancelled, "_acquired_sample_mask", cancel_after_extent)
                run_worker(cancelled, cancelled=True)
                assert not hasattr(cancelled, "axis_data")
                assert not hasattr(cancelled, "dataGrid")
                for name, values in saved.items():
                    np.testing.assert_array_equal(prior.data[name], values)
                assert snapshot_cache_parameter_publication_state(plot.ds.cache, "signal") == prior

                fresh = load_by_id_read_only(dataset.run_id, str(path))
                try:
                    full = loader(fresh.cache, params["signal"], params, axes)
                    run_worker(full)
                    assert_values(full, x, y, grid)
                    if heatmap:
                        bounded = loader(fresh.cache, params["signal"], params, axes,
                                         force_sql_heatmap=True)
                        run_worker(bounded)
                        assert bounded.loaded_from_sql_heatmap
                        assert_values(bounded, x, y, grid)
                finally:
                    fresh.conn.close()

            for record in records[:1] if finish_early else records:
                datasaver.add_result(*record.items())
                datasaver.flush_data_to_database(block=True)
                protected = database_state(path)
                for name, values in record.items():
                    expected[name].append(values.copy())
                if window is None:
                    window = main_window.MainWindow()
                    window.startupDatabaseTimer.stop()
                    window.monitor.stop()
                    window.config.config["user_preference"]["confirm_close"] = False
                    window.config.config["user_preference"]["confirm_close_all"] = False
                    errors = []
                    monkeypatch.setattr(window, "show_error", lambda *args, errors=errors: errors.append(args))
                    window.close_database(status=False)
                    assert window.load_file(str(path))
                    wait_for(lambda window=window: not window._database_load_active and not window._database_detail_active)
                    window.monitor.stop()
                    window.openPlot(guid=dataset.guid, params=[params["signal"]], show=False)
                    assert errors == []
                    plot = window.windows[-1]
                else:
                    plot.refreshWindow(force=True)
                wait_for_plot_refresh(plot)
                plot.monitor.stop()
                check_plot()
                assert database_state(path) == protected

        # Finishing early never makes unused planned storage into samples.
        protected = database_state(path)
        plot.refreshWindow(force=True)
        wait_for_plot_refresh(plot)
        plot.monitor.stop()
        check_plot(completed=True)
        assert cache_parameter_is_synchronized(plot.ds.cache, "signal")
        plot.refreshWindow(force=True)
        wait_for_plot_refresh(plot)
        plot.monitor.stop()
        assert plot.worker.read_data is False
        check_plot(completed=True)
        close_main_window(window)
        window = None
        assert database_state(path) == protected
    finally:
        if window is not None:
            close_main_window(window)
        if dataset is not None:
            dataset.conn.close()
        experiment.conn.close()
