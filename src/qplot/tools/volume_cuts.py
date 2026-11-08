"""Coordinate-bound cuts through the same processed planes as the viewer."""

from dataclasses import dataclass

import numpy as np

from qplot.datahandling.numeric_samples import (
    concatenate_record_samples,
    numeric_isfinite,
)
from qplot.tools.sample_statistics import finite_mean
from qplot.tools.volume_worker import processed_plane


@dataclass(frozen=True)
class CutSelection:
    kind: str
    x: str
    y: str
    depth: str
    x_value: float
    y_value: float
    depth_value: float | None
    bounds: tuple[float, float, float, float] | None = None

    @property
    def varying(self):
        return self.x if self.kind == "h" else self.y if self.kind == "v" else self.depth


def extract_cut(volume, selection, operations, param, params, *, check=lambda: None,
                cancelled=lambda: False, progress=lambda *_: None):
    axes = {"x": params[selection.x], "y": params[selection.y]}
    depths = volume.axes[selection.depth]
    display_param = param

    def plane(index):
        frame = volume.plane(selection.x, selection.y, index, check=check)
        return processed_plane(frame, operations, param, axes, check=check, cancelled=cancelled)

    def exact_index(values, value):
        index = int(np.searchsorted(values, value))
        return index if index < values.size and values[index] == value else None

    if selection.kind in ("h", "v"):
        coordinates = volume.axes[selection.varying]
        index = exact_index(depths, selection.depth_value) if selection.depth_value is not None else None
        if index is None:
            return coordinates, np.full(coordinates.size, np.nan), np.zeros(coordinates.size, dtype=int), display_param
        frame, display_param = plane(index)
        fixed = exact_index(frame.y if selection.kind == "h" else frame.x,
                            selection.y_value if selection.kind == "h" else selection.x_value)
        if fixed is None:
            return coordinates, np.full(coordinates.size, np.nan), np.zeros(coordinates.size, dtype=int), display_param
        values = frame.values[fixed, :] if selection.kind == "h" else frame.values[:, fixed]
        counts = frame.counts[fixed, :] if selection.kind == "h" else frame.counts[:, fixed]
        return coordinates, values.copy(), counts.copy(), display_param

    samples, counts = [], []
    for index in range(len(depths)):
        check()
        progress(index, len(depths))
        frame, display_param = plane(index)
        value, count = np.nan, 0
        if selection.kind == "mean":
            left, right, bottom, top = selection.bounds
            region = frame.values[np.ix_((frame.y >= bottom) & (frame.y <= top),
                                        (frame.x >= left) & (frame.x <= right))]
            finite = region[numeric_isfinite(region)]
            count = finite.size
            if count:
                value = finite_mean(finite, check_cancelled=check)
        else:
            i, j = exact_index(frame.x, selection.x_value), exact_index(frame.y, selection.y_value)
            if i is not None and j is not None:
                value, count = frame.values[j, i], frame.counts[j, i]
        samples.append(np.asarray([value], dtype=object if frame.values.dtype.kind == "O" else None))
        counts.append(count)
    check()
    return depths, concatenate_record_samples(samples), np.asarray(counts), display_param
