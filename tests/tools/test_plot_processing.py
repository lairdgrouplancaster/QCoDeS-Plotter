"""Numerical processing can run without a worker, Qt or a database source."""

from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import numpy as np
import pytest

from qplot.tools.operation_registry import OperationExecutionError
from qplot.tools.plot_processing import (
    PlotProcessingInput,
    PlotProcessingSpec,
    PlotProcessor,
    PlotWorkCancelled,
)

pytestmark = pytest.mark.pure


def processor(*, dimensions=1, operations=(), cancelled=lambda: False, **options):
    names = ("x", "y")[:dimensions]
    parameters = {
        name: SimpleNamespace(name=name, label=name, unit="V", type="numeric", depends_on_=())
        for name in names
    }
    signal = SimpleNamespace(
        name="signal", label="Signal", unit="A", type="numeric", depends_on_=names,
    )
    parameters[signal.name] = signal
    return PlotProcessor(
        PlotProcessingSpec(
            signal, parameters, {"x": "x", "y": "signal" if dimensions == 1 else "y"},
            list(operations), **options,
        ),
        cancelled=cancelled,
    )


def test_line_pipeline_keeps_operation_order_and_source_samples():
    x = np.arange(3, dtype=np.int64)
    values = np.array([0, 2, 4], dtype=np.uint64)
    processing = processor(operations=[
        lambda data: {"y": data["y"] + 1},
        lambda data: {"y": data["y"] * 3},
    ])

    result = processing.process(PlotProcessingInput({"x": x, "signal": values}, {"signal": 3}))

    np.testing.assert_array_equal(result.axis_data["y"], [3, 9, 15])
    np.testing.assert_array_equal(values, [0, 2, 4])
    np.testing.assert_array_equal(x, [0, 1, 2])
    assert result.loaded_point_count == 3
    assert result.dataGrid is None


def test_shaped_grid_uses_acquired_extent_and_preserves_zero():
    processing = processor(dimensions=2)
    arrays = {
        "x": np.array([[0, 1, 2], [0, 1, 2]]),
        "y": np.array([[0, 0, 0], [1, 1, 1]]),
        "signal": np.array([[0, 1, 2], [3, 0, 0]]),
    }
    result = processing.process(PlotProcessingInput(arrays, {"signal": 4}, (2, 3)))

    np.testing.assert_array_equal(result.axis_data["x"], [0, 1, 2])
    np.testing.assert_array_equal(result.axis_data["y"], [0, 1])
    assert result.dataGrid[0, 0] == 0
    assert result.dataGrid[1, 0] == 3
    assert np.isnan(result.dataGrid[1, 1:].astype(float)).all()
    assert result.loaded_point_count == 4
    assert result.heatmap_full_resolution
    np.testing.assert_array_equal(arrays["signal"], [[0, 1, 2], [3, 0, 0]])


def test_failed_transform_does_not_change_prepared_data_or_call_later_operations():
    calls = []

    def fail(data):
        data["y"][:] = -100
        raise ValueError("calculation failed")

    processing = processor(operations=[fail, lambda data: calls.append(data)])
    values = np.array([2.0, 3.0])
    processing.prepare(PlotProcessingInput({"x": np.arange(2), "signal": values}, {}))

    with pytest.raises(OperationExecutionError, match="calculation failed"):
        processing.finish()

    assert calls == []
    np.testing.assert_array_equal(processing.axis_data["y"], [2.0, 3.0])
    np.testing.assert_array_equal(values, [2.0, 3.0])


def test_cancellation_between_operations_stops_processing():
    cancelled = False
    calls = []

    def cancel(data):
        nonlocal cancelled
        cancelled = True
        return {"y": data["y"] * 2}

    processing = processor(
        cancelled=lambda: cancelled, operations=[cancel, lambda data: calls.append(data)],
    )
    with pytest.raises(PlotWorkCancelled):
        processing.process(PlotProcessingInput({"x": np.arange(2), "signal": np.ones(2)}, {}))
    assert calls == []
    np.testing.assert_array_equal(processing.axis_data["y"], [1.0, 1.0])


def test_results_snapshot_mappings_without_copying_array_buffers():
    processing = processor()
    result = processing.process(PlotProcessingInput({"x": np.arange(2), "signal": np.ones(2)}, {}))
    values = processing.axis_data["y"]
    processing.axis_data["y"] = np.zeros(2)

    assert result.axis_data["y"] is values
    assert values.flags.writeable
    with pytest.raises(FrozenInstanceError):
        result.loaded_point_count = 100
