"""Slice geometry, numerical fidelity, gaps, and shared cut processing."""

import itertools
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from qplot.datahandling.volume import VolumeStore
from qplot.tools.operation_registry import OperationCall
from qplot.tools.plot_tools import differentiate, subtract_mean
from qplot.tools.volume_cuts import CutSelection, extract_cut
from qplot.tools.volume_worker import processed_plane


@pytest.fixture
def volume():
    coordinates = np.asarray(list(itertools.product([4., 1., 2.5], [0., 3.], [-2., 0., 5.])))
    values = 100 * coordinates[:, 0] + 10 * coordinates[:, 1] + coordinates[:, 2] ** 2
    store = VolumeStore.from_chunks(("field", "bias", "gate"), [(*coordinates.T, values)], completed=True)
    yield store
    store.directory.cleanup()


def test_all_six_planes_preserve_coordinates(volume):
    for x, y in itertools.permutations(volume.names, 2):
        depth = volume.depth_name(x, y)
        for index, value in enumerate(volume.axes[depth]):
            frame = volume.plane(x, y, index)
            for j, y_value in enumerate(frame.y):
                for i, x_value in enumerate(frame.x):
                    point = {x: x_value, y: y_value, depth: value}
                    assert frame.values[j, i] == 100 * point["field"] + 10 * point["bias"] + point["gate"]**2
            assert np.all(frame.counts == 1)


def test_depth_cut_matches_processed_slice_and_physical_derivative(volume):
    params = {name: SimpleNamespace(name=name, label=name, unit="V") for name in volume.names}
    signal = SimpleNamespace(name="signal", label="Signal", unit="A")
    operations = [OperationCall("row mean", lambda data: subtract_mean("x", data))]
    selected = CutSelection("d", "gate", "bias", "field", 0., 3., 1.)
    depths, values, counts, _ = extract_cut(volume, selected, operations, signal, params)
    for index, _depth in enumerate(depths):
        frame, _ = processed_plane(volume.plane("gate", "bias", index), operations, signal,
                                   {"x": params["gate"], "y": params["bias"]})
        assert values[index] == frame.values[1, 1]
    np.testing.assert_array_equal(counts, 1)
    raw = extract_cut(volume, selected, (), signal, params)[1]
    derivative = differentiate("x", {"x": depths, "y": raw, "z": None})
    np.testing.assert_allclose(derivative["y"], 100)


def test_missing_cells_and_repeated_large_integers_remain_exact():
    big = 2**60
    columns = (np.array([0., 0., 1., 1.]), np.array([0., 0., 0., 1.]),
               np.array([0., 0., 0., 1.]), np.array([big, big + 2, big + 3, big + 4], dtype=np.int64))
    volume = VolumeStore.from_chunks(("d", "y", "x"), [columns])
    try:
        plane = volume.plane("x", "y", 0)
        assert plane.values[0, 0] == big + 1
        assert plane.counts[0, 0] == 2
        assert np.isnan(plane.values[1, 1])
        assert plane.counts[1, 1] == 0
        param = SimpleNamespace(name="v", label="v", unit="")
        params = dict.fromkeys(volume.names, param)
        selected = CutSelection("d", "x", "y", "d", 1., 1., 0.)
        _, trace, _, _ = extract_cut(volume, selected, (), param, params)
        assert np.isnan(trace[0])
        assert trace[1] == big + 4
    finally:
        volume.directory.cleanup()


def test_region_mean_has_coverage_and_never_samples_nearest_cell(volume):
    param = SimpleNamespace(name="v", label="v", unit="")
    params = dict.fromkeys(volume.names, param)
    selection = CutSelection("mean", "gate", "bias", "field", 0., 0., 1., (-2., 0., 0., 0.))
    _, trace, counts, _ = extract_cut(volume, selection, (), param, params)
    np.testing.assert_array_equal(counts, 2)
    np.testing.assert_allclose(trace, 100 * volume.axes["field"] + 2)
    _, trace, _, _ = extract_cut(volume, replace(selection, kind="d", x_value=0.001), (), param, params)
    assert np.all(np.isnan(trace))


def test_sparse_pivot_is_bounded_and_work_cancellable():
    axis = np.arange(12, dtype=float)
    volume = VolumeStore.from_chunks(("d", "y", "x"), [(axis * 0, axis, axis, axis)], max_points=20)
    try:
        with pytest.raises(ValueError, match="exceeds max_full_heatmap_points"):
            volume.plane("x", "y", 0)
        def cancelled():
            raise InterruptedError()
        with pytest.raises(InterruptedError):
            volume.plane("x", "d", 0, check=cancelled)
    finally:
        volume.directory.cleanup()
