"""Mixed QCoDeS object samples remain cancellable during classification."""

from types import SimpleNamespace

import numpy as np
import pytest

import qplot.tools.worker as worker_module
from qplot.tools.worker import PlotWorkCancelled, loader
from tests.test_worker_array_heatmaps import heatmap_dataset, make_worker


@pytest.mark.parametrize("phase", ["normalise", "heatmap", "coordinates"])
@pytest.mark.parametrize("second_dtype", [np.uint64, np.float64])
def test_real_mixed_records_cancel_during_classification(
        tmp_path, monkeypatch, qapplication, phase, second_dtype):
    values = np.arange(4096, dtype=np.int64)
    with heatmap_dataset(tmp_path, shaped=False, records=[
        (0, values, values),
        (1, values.astype(second_dtype) if phase == "coordinates" else values,
         values if phase == "coordinates" else values.astype(second_dtype)),
    ]) as dataset:
        worker = make_worker(dataset)
        method_name = {"normalise": "_normalise_array_records", "heatmap": "_real_heatmap_values",
                       "coordinates": "_float_heatmap_coordinates"}[phase]
        predicate_name = {"normalise": "ndim", "heatmap": "iscomplexobj",
                          "coordinates": "isfinite"}[phase]
        original_method = getattr(worker, method_name)
        original_predicate = getattr(np, predicate_name)
        active = False
        consumed = 0
        observed = []

        def predicate(value):
            nonlocal consumed
            result = original_predicate(value)
            if active and np.isscalar(value):
                consumed += 1
                if consumed == 1024:
                    worker.cancel()
            return result

        def classify(data, *args, **kwargs):
            nonlocal active
            arrays = list(data.values()) if isinstance(data, dict) else [data]
            if not any(array.dtype.kind == "O" for array in arrays):
                return original_method(data, *args, **kwargs)
            observed.extend((array, array.copy()) for array in arrays)
            active = True
            try:
                return original_method(data, *args, **kwargs)
            finally:
                active = False

        monkeypatch.setattr(worker, method_name, classify)
        monkeypatch.setattr(worker_module.np, predicate_name, predicate)
        finished, errors = [], []
        worker.emitter.finished.connect(finished.append)
        worker.emitter.errorOccurred.connect(errors.append)
        worker.run()
        assert consumed == 1024
        assert finished == [False] and not errors
        assert not worker.running
        assert not hasattr(worker, "dataGrid")
        assert observed
        for actual, original in observed:
            np.testing.assert_array_equal(actual, original)


@pytest.mark.parametrize("method_name", ["_real_line_values", "_real_heatmap_values"])
def test_object_complex_validation_cancels_and_retains_late_rejection(monkeypatch, method_name):
    worker = loader.__new__(loader)
    worker.param = SimpleNamespace(name="signal")
    values = np.arange(4096).astype(object)
    values[-1] = 1j
    original = values.copy()
    predicate = np.iscomplexobj
    consumed = 0

    def observe(value):
        nonlocal consumed
        result = predicate(value)
        if np.isscalar(value):
            consumed += 1
            if consumed == 1024:
                worker.cancel()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(worker_module.np, "iscomplexobj", observe)
        with pytest.raises(PlotWorkCancelled):
            getattr(worker, method_name)(values, "signal")
    assert consumed == 1024
    np.testing.assert_array_equal(values, original)
    worker._cancelled.clear()
    with pytest.raises(ValueError, match="Complex"):
        getattr(worker, method_name)(values, "signal")
    np.testing.assert_array_equal(values, original)
