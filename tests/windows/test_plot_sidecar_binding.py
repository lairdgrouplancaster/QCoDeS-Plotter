"""Exact WAL/SHM binding regressions for retained explicit plots."""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from qplot.datahandling.file_identity import database_sidecar_identities
from qplot.tools import worker as worker_module
from qplot.tools.worker import loader
from qplot.windows import _plot_actions as plot_actions_module
from qplot.windows import _plot_refresh as plot_refresh_module
from qplot.windows._dataset_handle import DatasetHandle, DatasetKey
from qplot.windows._plot_actions import PlotActionsMixin
from qplot.windows._plotWin import plotWidget
from qplot.windows.plot1d import plot1d
from qplot.windows.plot2d import plot2d


def _source_with_sidecars(tmp_path: Path, name: str) -> Path:
    database_path = tmp_path / name
    database_path.write_bytes(b"main database identity")
    Path(f"{database_path}-wal").write_bytes(b"accepted WAL")
    Path(f"{database_path}-shm").write_bytes(b"accepted SHM")
    return database_path


def _replace_sidecars(database_path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        replacement = Path(f"{database_path}{suffix}.replacement")
        replacement.write_bytes(f"replacement {suffix}".encode())
        os.replace(replacement, Path(f"{database_path}{suffix}"))


class _Signal:
    def __init__(self):
        self.values = []

    def emit(self, *values):
        self.values.append(values)


class _Monitor:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class _RefreshWorker:
    def __init__(self, *, running=False):
        self.running = running
        self.cancelled = False
        self.read_data = True

    def cancel(self):
        self.cancelled = True

    @staticmethod
    def is_cancelled():
        return False


def _retained_plot(database_path: Path):
    key = DatasetKey(database_path, "run-guid")
    plot = plotWidget.__new__(plotWidget)
    plot._dataset_key = key
    plot._dataset_holder = {
        key: DatasetHandle(
            object(),
            database_identity=key.database_identity,
            sidecar_identities=key.sidecar_identities,
        )
    }
    plot.monitor = _Monitor()
    plot.worker = _RefreshWorker()
    plot.database_replaced = _Signal()
    plot.end_wait = _Signal()
    plot.statuses = []
    plot.states = []
    plot.show_status = lambda *args: plot.statuses.append(args)
    plot.show_plot_state = lambda *args, **kwargs: plot.states.append((args, kwargs))
    return plot


def test_retained_plot_rejects_wal_shm_swap_before_automatic_refresh(tmp_path):
    database_path = _source_with_sidecars(tmp_path, "retained.db")
    plot = _retained_plot(database_path)
    accepted_sidecars = plot._dataset_key.sidecar_identities
    plot.load_calls = []
    plot.load_data = lambda *args, **kwargs: plot.load_calls.append((args, kwargs))

    _replace_sidecars(database_path)
    assert database_sidecar_identities(database_path).isdisjoint(accepted_sidecars)

    plotWidget.refreshWindow(plot, force=True)

    assert plot.load_calls == []
    assert plot.monitor.stopped
    assert plot.database_replaced.values == [(str(database_path),)]


def test_sidecar_swap_during_worker_read_is_rejected_before_publication(
    tmp_path,
    monkeypatch,
):
    database_path = _source_with_sidecars(tmp_path, "worker-race.db")
    key = DatasetKey(database_path, "run-guid")

    class Dataset:
        path_to_db = str(database_path)
        table_name = "results"
        completed = False

    class Cache:
        _dataset = Dataset()
        live = False

    class Param:
        name = "signal"
        depends_on_ = ("x",)

    class Connection:
        def close(self):
            return None

    plot_worker = loader(Cache(), Param(), {}, {})
    plot_worker.database_identity = key.database_identity
    plot_worker.expected_database_path = key.database_path
    plot_worker.expected_resolved_database_path = key.resolved_database_path
    plot_worker.expected_sidecar_identities = key.sidecar_identities
    plot_worker.sidecar_identities = key.sidecar_identities

    def swap_during_read(*_args, **_kwargs):
        _replace_sidecars(database_path)
        return False, False

    monkeypatch.setattr(
        worker_module,
        "qcodes_read_only_connection",
        lambda *_args, **_kwargs: Connection(),
    )
    monkeypatch.setattr(
        worker_module,
        "load_param_data_from_db_prep",
        swap_during_read,
    )
    finished = []
    errors = []
    plot_worker.emitter.finished.connect(finished.append)
    plot_worker.emitter.errorOccurred.connect(errors.append)

    plot_worker.run()

    assert plot_worker.database_replaced
    assert finished == [False]
    assert len(errors) == 1

    # Even a hypothetical late success callback cannot publish stale cache
    # state because the plot performs its own final exact source guard.
    plot = _retained_plot(database_path)
    plot._dataset_key = key
    plot.worker = plot_worker
    cache_updates = []
    monkeypatch.setattr(
        plot_refresh_module,
        "update_cache_parameter_data",
        lambda *args: cache_updates.append(args),
    )
    assert not plotWidget.refreshPlot(plot, True, worker=plot_worker)
    assert cache_updates == []


def test_callback_swap_rolls_back_shared_cache_and_axis_state(
    tmp_path,
    monkeypatch,
):
    database_path = _source_with_sidecars(tmp_path, "cache-callback.db")
    key = DatasetKey(database_path, "run-guid")
    old_parameter_data = {"signal": {"signal": [1.0], "x": [0.0]}}
    new_parameter_data = {"signal": {"signal": [2.0], "x": [1.0]}}

    class CacheDataset:
        _completed = False

    class Cache:
        _dataset = CacheDataset()
        _read_status = {"signal": 1}
        _write_status = {"signal": 1}
        _data = {"signal": old_parameter_data}
        _qplot_synchronized_parameters = set()
        live = False

    class Dataset:
        cache = Cache()
        number_of_results = 1
        running = True

    class Param:
        name = "signal"

    plot = plotWidget.__new__(plotWidget)
    plot._dataset_key = key
    plot._dataset_holder = {
        key: DatasetHandle(
            Dataset(),
            database_identity=key.database_identity,
            sidecar_identities=key.sidecar_identities,
        )
    }
    plot.param = Param()
    plot._qplot_display_synchronized = True
    plot._qplot_display_uses_direct_sql = False
    plot._live = True
    plot.axis_data = {"x": [0.0], "y": [1.0]}
    plot.axis_param = {"x": "old-x", "y": "old-y"}
    plot.display_param = "old-display"
    plot.last_ds_len = 1
    plot.monitor = _Monitor()
    plot.database_replaced = _Signal()
    plot.end_wait = _Signal()
    plot.show_status = lambda *_args: None
    plot.show_plot_state = lambda *_args, **_kwargs: None
    plot.hide_plot_state = lambda: None
    plot._set_param_axis_labels = lambda: None

    worker = _RefreshWorker()
    worker.updated_read_status = {"signal": 2}
    worker.updated_write_status = {"signal": 2}
    worker.cache_data = {"signal": new_parameter_data}
    worker.dataset_completed = False
    worker.axis_data = {"x": [1.0], "y": [2.0]}
    worker.axis_param = {"x": "new-x", "y": "new-y"}
    worker.display_param = "new-display"
    worker.dataset_length_at_start = 2
    worker.started_at = 0.0
    plot.worker = worker

    real_update = plot_refresh_module.update_cache_parameter_data

    def update_then_swap(*args, **kwargs):
        result = real_update(*args, **kwargs)
        _replace_sidecars(database_path)
        return result

    monkeypatch.setattr(
        plot_refresh_module,
        "update_cache_parameter_data",
        update_then_swap,
    )

    assert not plotWidget.refreshPlot(plot, True, worker=worker)

    assert plot.ds.cache._read_status == {"signal": 1}
    assert plot.ds.cache._write_status == {"signal": 1}
    assert plot.ds.cache._data["signal"] is old_parameter_data
    assert plot.axis_data == {"x": [0.0], "y": [1.0]}
    assert plot.axis_param == {"x": "old-x", "y": "old-y"}
    assert plot.display_param == "old-display"
    assert plot.database_replaced.values == [(str(database_path),)]


def test_attribute_error_after_swap_rolls_back_before_soft_error(tmp_path):
    database_path = _source_with_sidecars(tmp_path, "attribute-error-callback.db")
    key = DatasetKey(database_path, "run-guid")
    old_parameter_data = {"signal": {"signal": [1.0], "x": [0.0]}}
    new_parameter_data = {"signal": {"signal": [2.0], "x": [1.0]}}
    cache = SimpleNamespace(
        _dataset=SimpleNamespace(_completed=False),
        _read_status={"signal": 1},
        _write_status={"signal": 1},
        _data={"signal": old_parameter_data},
        _qplot_synchronized_parameters=set(),
        live=False,
    )
    dataset = SimpleNamespace(cache=cache, number_of_results=1, running=True)

    plot = plotWidget.__new__(plotWidget)
    plot._dataset_key = key
    plot._dataset_holder = {
        key: DatasetHandle(
            dataset,
            database_identity=key.database_identity,
            sidecar_identities=key.sidecar_identities,
        )
    }
    plot.param = SimpleNamespace(name="signal")
    plot._qplot_display_synchronized = True
    plot._qplot_display_uses_direct_sql = False
    plot._live = True
    plot.axis_data = {"x": [0.0], "y": [1.0]}
    plot.axis_param = {"x": "old-x", "y": "old-y"}
    plot.display_param = "old-display"
    plot.last_ds_len = 1
    plot.monitor = _Monitor()
    plot.database_replaced = _Signal()
    plot.end_wait = _Signal()
    plot.statuses = []
    plot.states = []
    plot.show_status = lambda *args: plot.statuses.append(args)
    plot.show_plot_state = lambda *args, **kwargs: plot.states.append((args, kwargs))
    plot.hide_plot_state = lambda: None

    def swap_then_fail_labels():
        _replace_sidecars(database_path)
        raise AttributeError("injected label failure")

    plot._set_param_axis_labels = swap_then_fail_labels

    worker = _RefreshWorker()
    worker.updated_read_status = {"signal": 2}
    worker.updated_write_status = {"signal": 2}
    worker.cache_data = {"signal": new_parameter_data}
    worker.dataset_completed = False
    worker.axis_data = {"x": [1.0], "y": [2.0]}
    worker.axis_param = {"x": "new-x", "y": "new-y"}
    worker.display_param = "new-display"
    worker.dataset_length_at_start = 2
    worker.started_at = 0.0
    plot.worker = worker

    assert not plotWidget.refreshPlot(plot, True, worker=worker)

    assert cache._read_status == {"signal": 1}
    assert cache._write_status == {"signal": 1}
    assert cache._data["signal"] is old_parameter_data
    assert plot.axis_data == {"x": [0.0], "y": [1.0]}
    assert plot.axis_param == {"x": "old-x", "y": "old-y"}
    assert plot.display_param == "old-display"
    assert plot.database_replaced.values == [(str(database_path),)]
    assert not any("Refresh skipped" in message for message, *_rest in plot.statuses)
    assert worker._qplot_publication_snapshot is None


def test_older_callback_rollback_cannot_clobber_reentrant_newer_publication():
    class CacheDataset:
        _completed = False

    class Cache:
        _dataset = CacheDataset()
        _read_status = {"signal": 1}
        _write_status = {"signal": 1}
        _data = {"signal": "initial-cache"}
        _qplot_synchronized_parameters = set()
        live = False

    class Dataset:
        cache = Cache()

    class Param:
        name = "signal"

    plot = plotWidget.__new__(plotWidget)
    plot._dataset_key = DatasetKey("missing.db", "run-guid")
    plot._dataset_holder = {
        plot._dataset_key: DatasetHandle(Dataset()),
    }
    plot.param = Param()
    plot.axis_data = "initial-axis"

    older_worker = _RefreshWorker()
    plot.worker = older_worker
    plot._begin_refresh_publication(older_worker)
    plot.axis_data = "older-axis"
    plot.ds.cache._data["signal"] = "older-cache"
    plot._record_refresh_cache_publication(older_worker)

    newer_worker = _RefreshWorker()
    plot.worker = newer_worker
    plot._begin_refresh_publication(newer_worker)
    plot.axis_data = "newer-axis"
    plot.ds.cache._data["signal"] = "newer-cache"
    plot._record_refresh_cache_publication(newer_worker)

    plot._rollback_refresh_publication(older_worker)

    assert plot.axis_data == "newer-axis"
    assert plot.ds.cache._data["signal"] == "newer-cache"
    assert isinstance(newer_worker._qplot_publication_snapshot, dict)
    assert older_worker._qplot_publication_snapshot is None


def test_old_line_callback_cannot_render_after_reentrant_newer_publication(
    tmp_path,
):
    database_path = _source_with_sidecars(tmp_path, "same-source-reentrant.db")
    key = DatasetKey(database_path, "run-guid")

    class CacheDataset:
        _completed = False

    class Cache:
        _dataset = CacheDataset()
        _read_status = {"signal": 1}
        _write_status = {"signal": 1}
        _data = {"signal": {"signal": {"signal": [1.0], "x": [0.0]}}}
        _qplot_synchronized_parameters = {"signal"}
        live = False

    class Dataset:
        cache = Cache()
        number_of_results = 1
        running = False

    class Param:
        name = "signal"

    class Line:
        def __init__(self):
            self.calls = []

        def setData(self, *args, **kwargs):
            self.calls.append((args, kwargs))

    plot = plot1d.__new__(plot1d)
    plot._dataset_key = key
    plot._dataset_holder = {
        key: DatasetHandle(
            Dataset(),
            database_identity=key.database_identity,
            sidecar_identities=key.sidecar_identities,
        )
    }
    plot.param = Param()
    plot._qplot_display_synchronized = True
    plot._qplot_display_uses_direct_sql = False
    plot._live = True
    plot.axis_data = {"x": [0.0], "y": [1.0]}
    plot.axis_param = {"x": "initial-x", "y": "initial-y"}
    plot.display_param = "initial-display"
    plot.last_ds_len = 1
    plot.line = Line()
    plot.marquee = None
    plot.monitor = _Monitor()
    plot.database_replaced = _Signal()
    plot.trace_updated = _Signal()
    plot.show_status = lambda *_args: None
    plot.show_plot_state = lambda *_args, **_kwargs: None
    plot.hide_plot_state = lambda: None
    plot._set_param_axis_labels = lambda: None
    plot._update_coordinate_context = lambda: None
    plot.refresh_secondary_lines = lambda: None

    older_worker = _RefreshWorker()
    older_worker.read_data = False
    older_worker.dataset_completed = False
    older_worker.axis_data = {"x": [1.0], "y": [11.0]}
    older_worker.axis_param = {"x": "older-x", "y": "older-y"}
    older_worker.display_param = "older-display"
    older_worker.dataset_length_at_start = 2
    older_worker.started_at = 0.0
    plot.worker = older_worker

    newer_worker = _RefreshWorker()
    newer_axis_data = {"x": [2.0], "y": [22.0]}

    class ReentrantEndWait:
        emitted = False

        def emit(self):
            if self.emitted:
                return
            self.emitted = True
            plot.worker = newer_worker
            plot._begin_refresh_publication(newer_worker)
            plot.axis_data = newer_axis_data
            plot.axis_param = {"x": "newer-x", "y": "newer-y"}
            plot.display_param = "newer-display"
            plot.line.setData(
                x=newer_axis_data["x"],
                y=newer_axis_data["y"],
            )
            plot._record_refresh_cache_publication(newer_worker)
            plot._commit_refresh_publication(newer_worker)

    plot.end_wait = ReentrantEndWait()

    plot1d.refreshPlot(plot, True, worker=older_worker)

    assert plot.worker is newer_worker
    assert plot.axis_data is newer_axis_data
    assert plot.axis_param == {"x": "newer-x", "y": "newer-y"}
    assert plot.line.calls == [
        ((), {"x": newer_axis_data["x"], "y": newer_axis_data["y"]})
    ]
    assert plot.trace_updated.values == []
    assert plot.database_replaced.values == []
    assert older_worker._qplot_publication_snapshot is None


def test_old_invalid_heatmap_callback_cannot_overwrite_reentrant_newer_state(
    monkeypatch,
):
    older_worker = _RefreshWorker(running=True)
    older_worker.loaded_from_sql_heatmap = False
    older_worker.dataset_completed = False
    older_worker._qplot_publication_snapshot = {"generation": 1}
    newer_worker = _RefreshWorker(running=True)

    plot = plot2d.__new__(plot2d)
    plot.worker = older_worker
    plot._qplot_publication_generation = 1
    plot.param = type("Param", (), {"name": "signal"})()
    plot.renderer_state = "older geometry"
    plot.statuses = []
    plot.states = []
    plot._source_database_matches_key = lambda: True
    plot._update_large_heatmap_state = lambda _worker: None
    plot.refresh_secondary_heatmaps = lambda: None
    plot._has_plottable_heatmap_data = lambda: True
    plot._update_heatmap_geometry = lambda: (_ for _ in ()).throw(
        ValueError("older invalid geometry")
    )
    plot._invalidate_heatmap_geometry = lambda: setattr(
        plot,
        "renderer_state",
        "invalidated by older callback",
    )
    plot.show_status = lambda *args: plot.statuses.append(args)
    plot.show_plot_state = lambda *args, **kwargs: plot.states.append((args, kwargs))
    plot._ensure_refresh_monitor = lambda: None

    def publish_newer_state():
        plot.worker = newer_worker
        plot._qplot_publication_generation = 2
        plot.renderer_state = "newer renderer"
        plot.statuses[:] = [("newer status", 5000)]
        plot.states[:] = [(("Newer plot state",), {})]

    plot._emit_heatmap_trace_updated = publish_newer_state
    monkeypatch.setattr(plotWidget, "refreshPlot", lambda *_args, **_kwargs: True)

    plot2d.refreshPlot(plot, True, worker=older_worker)

    assert plot.worker is newer_worker
    assert plot.renderer_state == "newer renderer"
    assert plot.statuses == [("newer status", 5000)]
    assert plot.states == [(("Newer plot state",), {})]
    assert older_worker._qplot_publication_snapshot is None


def test_line_render_swap_clears_display_and_rolls_back_axis_state(tmp_path):
    database_path = _source_with_sidecars(tmp_path, "line-callback.db")
    key = DatasetKey(database_path, "run-guid")

    class CacheDataset:
        _completed = False

    class Cache:
        _dataset = CacheDataset()
        _read_status = {"signal": 1}
        _write_status = {"signal": 1}
        _data = {"signal": {"signal": {"signal": [1.0], "x": [0.0]}}}
        _qplot_synchronized_parameters = {"signal"}
        live = False

    class Dataset:
        cache = Cache()
        number_of_results = 1
        running = False

    class Param:
        name = "signal"

    class Line:
        def __init__(self):
            self.calls = []

        def setData(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            if len(self.calls) == 1:
                _replace_sidecars(database_path)

    plot = plot1d.__new__(plot1d)
    plot._dataset_key = key
    plot._dataset_holder = {
        key: DatasetHandle(
            Dataset(),
            database_identity=key.database_identity,
            sidecar_identities=key.sidecar_identities,
        )
    }
    plot.param = Param()
    plot._qplot_display_synchronized = True
    plot._qplot_display_uses_direct_sql = False
    plot._live = True
    plot.axis_data = {"x": [0.0], "y": [1.0]}
    plot.axis_param = {"x": "old-x", "y": "old-y"}
    plot.display_param = "old-display"
    plot.last_ds_len = 1
    plot.line = Line()
    plot.marquee = None
    plot.monitor = _Monitor()
    plot.database_replaced = _Signal()
    plot.end_wait = _Signal()
    plot.trace_updated = _Signal()
    plot.show_status = lambda *_args: None
    plot.show_plot_state = lambda *_args, **_kwargs: None
    plot.hide_plot_state = lambda: None
    plot._set_param_axis_labels = lambda: None
    plot._update_coordinate_context = lambda: None
    plot.refresh_secondary_lines = lambda: None

    worker = _RefreshWorker()
    worker.read_data = False
    worker.dataset_completed = False
    worker.axis_data = {"x": [1.0], "y": [2.0]}
    worker.axis_param = {"x": "new-x", "y": "new-y"}
    worker.display_param = "new-display"
    worker.dataset_length_at_start = 2
    worker.started_at = 0.0
    plot.worker = worker

    plot1d.refreshPlot(plot, True, worker=worker)

    assert len(plot.line.calls) == 2
    assert plot.line.calls[-1] == (([], []), {})
    assert plot.axis_data == {"x": [0.0], "y": [1.0]}
    assert plot.axis_param == {"x": "old-x", "y": "old-y"}
    assert plot.trace_updated.values == []
    assert plot.database_replaced.values == [(str(database_path),)]


def test_first_sidecars_are_promoted_into_plot_and_holder_key(tmp_path):
    database_path = tmp_path / "first-sidecars.db"
    database_path.write_bytes(b"main database identity")
    initial_key = DatasetKey(database_path, "run-guid")
    assert initial_key.sidecar_identities == frozenset()

    dataset = type("Dataset", (), {"guid": "run-guid"})()

    class Harness(PlotActionsMixin):
        def __init__(self):
            self.dataset_holder = {
                initial_key: DatasetHandle(
                    dataset,
                    database_identity=initial_key.database_identity,
                    sidecar_identities=initial_key.sidecar_identities,
                )
            }
            self._plot_target_dataset_key = initial_key
            self.recoveries = []
            self.statuses = []

        def _handle_plot_database_replaced(self, path):
            self.recoveries.append(path)

        def show_status(self, *args):
            self.statuses.append(args)

    harness = Harness()
    Path(f"{database_path}-wal").write_bytes(b"first WAL")
    Path(f"{database_path}-shm").write_bytes(b"first SHM")

    bound_key = harness._accepted_plot_target_key(dataset)

    assert bound_key is not None
    assert bound_key.sidecar_identities == database_sidecar_identities(database_path)
    actual_holder_key = next(iter(harness.dataset_holder))
    assert actual_holder_key is bound_key
    assert (
        harness.dataset_holder[actual_holder_key].sidecar_identities
        == bound_key.sidecar_identities
    )

    _replace_sidecars(database_path)
    with pytest.raises(RuntimeError, match="SQLite sidecars"):
        harness._ensure_bound_plot_dataset_key_can_be_read(bound_key)

    assert harness.recoveries == [str(database_path)]


class _CsvHarness(PlotActionsMixin):
    def __init__(self, export_path: Path):
        self.export_path = export_path
        self.dataset_holder = {}
        self.recoveries = []
        self.statuses = []
        self.errors = []

    def _reload_if_database_instance_changed(self, path):
        self.recoveries.append(path)
        return False

    def show_status(self, *args):
        self.statuses.append(args)

    def show_error(self, *args):
        self.errors.append(args)


def test_run_csv_binds_first_sidecars_then_rejects_swap_before_open(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "run-csv.db"
    database_path.write_bytes(b"main database identity")
    initial_key = DatasetKey(database_path, "run-guid")
    export_path = tmp_path / "run.csv"
    harness = _CsvHarness(export_path)
    opened = []

    def choose_after_first_sidecars(_default_name):
        Path(f"{database_path}-wal").write_bytes(b"first WAL")
        Path(f"{database_path}-shm").write_bytes(b"first SHM")
        return str(export_path)

    real_bind = harness._bind_one_shot_dataset_key

    def bind_then_swap(dataset_key):
        bound_key = real_bind(dataset_key)
        assert bound_key.sidecar_identities == database_sidecar_identities(
            database_path
        )
        _replace_sidecars(database_path)
        return bound_key

    harness._choose_csv_export_filename = choose_after_first_sidecars
    harness._bind_one_shot_dataset_key = bind_then_swap
    monkeypatch.setattr(
        plot_actions_module,
        "load_by_guid_read_only",
        lambda *_args, **_kwargs: opened.append(True),
    )

    harness._export_measurement_csv(initial_key, ("signal",), run_id=1)

    assert opened == []
    assert not export_path.exists()
    assert harness.recoveries == [str(database_path)]


def test_preview_csv_rejects_swap_after_materialisation_before_publish(tmp_path):
    database_path = tmp_path / "preview-csv.db"
    database_path.write_bytes(b"main database identity")
    initial_key = DatasetKey(database_path, "run-guid")
    export_path = tmp_path / "preview.csv"
    harness = _CsvHarness(export_path)
    bound_keys = []

    class Param:
        name = "signal"
        depends_on = "x"

    class Dataset:
        guid = "run-guid"

        @staticmethod
        def get_parameters():
            return [Param()]

    def choose_after_first_sidecars(_default_name):
        Path(f"{database_path}-wal").write_bytes(b"first WAL")
        Path(f"{database_path}-shm").write_bytes(b"first SHM")
        return str(export_path)

    def materialise_then_swap(dataset_key):
        harness._require_run_csv_source_current(dataset_key)
        bound_keys.append(dataset_key)
        _replace_sidecars(database_path)
        return Dataset()

    harness._choose_csv_export_filename = choose_after_first_sidecars
    harness._load_run_csv_dataset = materialise_then_swap
    harness._measurement_dataframe = lambda *_args: object()

    harness._export_preview_csv(initial_key, "signal")

    assert len(bound_keys) == 1
    assert bound_keys[0].sidecar_identities
    assert not export_path.exists()
    assert harness.recoveries == [str(database_path)]
    assert harness.errors[-1][0] == "CSV Export Failed"
