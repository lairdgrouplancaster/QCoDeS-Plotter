from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import numpy as np
import pytest

from qplot.tools import worker as worker_module
from qplot.tools.plot_result import capture_plot_result
from tests._plot_worker import make_plot_worker


def _worker(tmp_path, **options):
    x = np.array([0.0, 1.0])
    y = np.array([2.0, 3.0])
    cache = SimpleNamespace(
        live=True,
        _dataset=SimpleNamespace(table_name="results", path_to_db=str(tmp_path / "absent.db")),
        rundescriber=SimpleNamespace(shapes=None),
        _read_status={"signal": 2},
        _write_status={"signal": 2},
        _data={"signal": {"x": x, "signal": y}},
    )
    worker = make_plot_worker(
        cache=cache, axes={"x": "x", "y": "signal"}, read_data=False, **options,
    )
    worker.dataset_length_at_start = 2
    return worker


def test_success_publishes_complete_post_operation_result_before_signal(tmp_path):
    worker = _worker(tmp_path, operations=[lambda data: {"y": 2 * data["y"]}])
    observed = []
    worker.emitter.finished.connect(lambda finished: observed.append((finished, worker.result)))

    worker.run()

    assert observed == [(True, worker.result)]
    result = worker.result
    assert result is not None
    np.testing.assert_array_equal(result.axis_data["y"], [4.0, 6.0])
    np.testing.assert_array_equal(worker.cache._data["signal"]["signal"], [2.0, 3.0])
    assert result.loaded_point_count == 2
    assert result.dataset_length_at_start == 2
    assert result.cache_update is None
    assert result.axis_data["y"] is worker.axis_data["y"]


def test_capture_failure_clears_previous_result_and_emits_normal_failure(tmp_path, monkeypatch):
    worker = _worker(tmp_path)
    worker.run()
    assert worker.result is not None
    errors, finished = [], []
    worker.emitter.errorOccurred.connect(errors.append)
    worker.emitter.finished.connect(finished.append)

    def fail_capture(_worker):
        raise ValueError("injected capture failure")

    monkeypatch.setattr(worker_module, "capture_plot_result", fail_capture)
    worker.run()

    assert worker.result is None
    assert finished == [False]
    assert len(errors) == 1
    assert str(errors[0]) == "injected capture failure"


def test_cancellation_during_capture_never_publishes_result(tmp_path, monkeypatch):
    worker = _worker(tmp_path)
    finished = []
    worker.emitter.finished.connect(finished.append)

    def capture_then_cancel(current):
        result = capture_plot_result(current)
        current.cancel()
        return result

    monkeypatch.setattr(worker_module, "capture_plot_result", capture_then_cancel)
    worker.run()

    assert finished == [False]
    assert worker.result is None
    assert not worker.running


def test_result_snapshots_mappings_without_copying_or_restricting_samples():
    samples = np.array([2**63 + 1], dtype=np.uint64)
    worker = SimpleNamespace(
        axis_data={"x": samples, "y": samples},
        axis_param={"x": "x", "y": "signal"},
        read_data=True,
        updated_read_status={"signal": 1},
        updated_write_status={"signal": 1},
        cache_data={"signal": {"signal": samples}},
        expected_database_path="original.db",
        expected_resolved_database_path="resolved.db",
        database_identity=(1, 2),
        expected_sidecar_identities=frozenset({(3, 4)}),
        heatmap_axis_ranges={"x": (0.0, 1.0)},
    )
    result = capture_plot_result(worker)
    worker.axis_data["y"] = np.array([0])
    worker.updated_read_status["signal"] = 99
    worker.cache_data["signal"]["signal"] = np.array([0])
    worker.heatmap_axis_ranges["x"] = (2.0, 3.0)
    worker.database_identity = (8, 9)

    assert result.axis_data["y"] is samples
    assert result.cache_update is not None
    assert result.cache_update.data["signal"]["signal"] is samples
    assert result.cache_update.read_status["signal"] == 1
    assert result.heatmap_axis_ranges == {"x": (0.0, 1.0)}
    assert result.source.database_identity == (1, 2)
    assert samples.flags.writeable
    with pytest.raises(FrozenInstanceError):
        result.dataset_completed = True
