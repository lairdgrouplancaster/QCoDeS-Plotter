"""Explicit real display projections with the recorded complex samples retained."""

from copy import copy
from dataclasses import dataclass, replace
from typing import Any

import numpy as np

COMPLEX_1D_KEY = "user_preference.complex_1d"
COMPLEX_ND_KEY = "user_preference.complex_nd"
COMPLEX_COMPONENT_OPTIONS = (
    ("Magnitude", "magnitude"), ("Phase (degrees)", "phase"),
    ("Re", "real"), ("Im", "imag"),
)
COMPLEX_1D_OPTIONS = (*COMPLEX_COMPONENT_OPTIONS, ("Im vs Re", "imag_vs_real"))


def contains_complex(values, *, check=lambda: None):
    values = np.asarray(values)
    if np.iscomplexobj(values):
        return True
    if values.dtype.kind == "O":
        for index, value in enumerate(values.flat):
            if index % 4096 == 0:
                check()
            if np.iscomplexobj(value):
                return True
    return False


def complex_values(values):
    """Convert numeric object cells only when an explicit projection is needed."""
    values = np.asarray(values)
    return values.astype(complex) if values.dtype.kind == "O" else values


def project_complex(values, mode):
    values = complex_values(values)
    if mode == "magnitude":
        result = np.abs(values)
    elif mode == "phase":
        result = np.angle(values, deg=True)
    elif mode == "real":
        result = values.real.copy()
    elif mode == "imag":
        result = values.imag.copy()
    else:
        raise ValueError(f"Unknown complex display mode: {mode}")
    # A missing component makes the whole recorded value unavailable.
    return np.where(np.isfinite(values), result, np.nan)


def complex_parameter(parameter, mode):
    result = copy(parameter)
    label = getattr(parameter, "label", None) or parameter.name
    result.label = {
        "magnitude": f"Magnitude of {label}", "phase": f"Phase of {label}",
        "real": f"Re({label})", "imag": f"Im({label})",
    }[mode]
    if mode == "phase":
        result.unit = "°"
    return result


@dataclass(frozen=True)
class ComplexSamples:
    """Original acquired values, aligned with their original setpoints."""

    coordinates: tuple[np.ndarray, ...]
    parameters: tuple[Any, ...]
    values: np.ndarray
    mode: str
    grid_indices: np.ndarray | None = None

    def bind_grid(self, x, y, *, check=lambda: None):
        """Index source samples off the GUI thread for constant-time hover."""
        if not len(x) or not len(y):
            return self
        x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
        indices = np.full((y.size, x.size), -1, dtype=np.int64)
        best = np.full(indices.shape, np.inf)
        for start in range(0, self.values.size, 65_536):
            check()
            stop = min(start + 65_536, self.values.size)
            xs, ys = (np.asarray(axis[start:stop], dtype=float) for axis in self.coordinates)
            valid = np.isfinite(xs) & np.isfinite(ys) & np.isfinite(self.values[start:stop])
            positions, distances = [], []
            for axis, values in ((x, xs), (y, ys)):
                middle = axis[:-1] / 2 + axis[1:] / 2
                edges = np.r_[axis[0] - (middle[0] - axis[0]) if middle.size else axis[0] - .5,
                              middle,
                              axis[-1] + (axis[-1] - middle[-1]) if middle.size else axis[-1] + .5]
                position = np.searchsorted(middle, values, side="right")
                if axis.size > 1:
                    valid &= (values >= edges[0]) & (values <= edges[-1])
                positions.append(position)
                with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
                    distances.append((values / 2 - axis[position] / 2)
                                     / np.maximum((edges[position + 1] / 2 - edges[position] / 2),
                                                  np.finfo(float).tiny))
            cells = positions[1][valid] * x.size + positions[0][valid]
            distance = distances[0][valid]**2 + distances[1][valid]**2
            source_indices = np.arange(start, stop)[valid]
            order = np.lexsort((-source_indices, distance, cells))
            ordered = cells[order]
            first = np.r_[True, ordered[1:] != ordered[:-1]] if ordered.size else np.array([], dtype=bool)
            selected = order[first]
            cells, distance, source_indices = cells[selected], distance[selected], source_indices[selected]
            better = distance <= best.ravel()[cells]
            indices.ravel()[cells[better]] = source_indices[better]
            best.ravel()[cells[better]] = distance[better]
        check()
        return replace(self, grid_indices=indices)

    def cell_index(self, i, j):
        if self.grid_indices is None:
            return None
        index = int(self.grid_indices[j, i])
        return index if index >= 0 else None

