"""Coordinate geometry for rectilinear heatmaps.

This module deliberately has no Qt dependencies.  It defines the coordinate
contract shared by heatmap rendering and interactions: axis values are pixel
centres, while the derived edges are pixel boundaries.
"""

import math
import operator
from bisect import bisect_left, bisect_right
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt

_DEFAULT_SINGLETON_SPAN = 1.0
_DEFAULT_UNIFORM_REL_TOL = 1e-9


@dataclass(frozen=True, slots=True, init=False)
class AxisGeometry:
    """Immutable geometry for one strictly increasing heatmap axis.

    Descending axes are rejected rather than silently reordered because axis
    indices must remain aligned with the corresponding dimension of the data
    grid.  A caller that receives descending data must reverse both together.

    Parameters
    ----------
    centres:
        Finite, strictly increasing cell-centre coordinates.
    singleton_span:
        Width assigned to an axis containing one centre.  Callers can supply a
        known neighbouring step; otherwise one coordinate unit is used.
    uniform_rel_tol, uniform_abs_tol:
        Tolerances used only when classifying an axis as uniform.
    """

    centres: tuple[float, ...]
    edges: tuple[float, ...]
    is_uniform: bool

    def __init__(
            self,
            centres: Iterable[float],
            *,
            singleton_span: float = _DEFAULT_SINGLETON_SPAN,
            uniform_rel_tol: float = _DEFAULT_UNIFORM_REL_TOL,
            uniform_abs_tol: float = 0.0,
            ) -> None:
        values = tuple(float(value) for value in centres)
        span = float(singleton_span)
        rel_tol = float(uniform_rel_tol)
        abs_tol = float(uniform_abs_tol)

        if not values:
            raise ValueError("A heatmap axis requires at least one centre.")
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Heatmap axis centres must all be finite.")
        if not math.isfinite(span) or span <= 0.0:
            raise ValueError("singleton_span must be positive and finite.")
        if not math.isfinite(rel_tol) or rel_tol < 0.0:
            raise ValueError("uniform_rel_tol must be non-negative and finite.")
        if not math.isfinite(abs_tol) or abs_tol < 0.0:
            raise ValueError("uniform_abs_tol must be non-negative and finite.")

        deltas = tuple(
            right - left
            for left, right in zip(values[:-1], values[1:], strict=True)
        )
        if deltas and all(delta < 0.0 for delta in deltas):
            raise ValueError(
                "descending heatmap axes are not supported; reverse both the "
                "axis centres and the corresponding data dimension."
            )
        if any(delta <= 0.0 for delta in deltas):
            raise ValueError("Heatmap axis centres must be strictly increasing.")

        derived_edges: tuple[float, ...]
        if len(values) == 1:
            half_span = span / 2.0
            derived_edges = (values[0] - half_span, values[0] + half_span)
        else:
            interior_edges = tuple(
                left / 2.0 + right / 2.0
                for left, right in zip(values[:-1], values[1:], strict=True)
            )
            derived_edges = (
                values[0] - (interior_edges[0] - values[0]),
                *interior_edges,
                values[-1] + (values[-1] - interior_edges[-1]),
            )

        if not all(math.isfinite(edge) for edge in derived_edges):
            raise ValueError("Derived heatmap axis edges must all be finite.")
        if any(
                right <= left
                for left, right in zip(
                    derived_edges[:-1],
                    derived_edges[1:],
                    strict=True,
                    )
                ):
            raise ValueError(
                "Heatmap cell edges collapse at floating-point precision."
                )

        uniform = len(deltas) <= 1 or all(
            math.isclose(
                delta,
                deltas[0],
                rel_tol=rel_tol,
                abs_tol=abs_tol,
            )
            for delta in deltas[1:]
        )

        object.__setattr__(self, "centres", values)
        object.__setattr__(self, "edges", derived_edges)
        object.__setattr__(self, "is_uniform", uniform)

    @property
    def count(self) -> int:
        """Number of cells along this axis."""

        return len(self.centres)

    @property
    def bounds(self) -> tuple[float, float]:
        """Inclusive outer coordinate bounds of the axis."""

        return self.edges[0], self.edges[-1]

    @property
    def span(self) -> float:
        """Positive distance between the outer axis edges."""

        return self.edges[-1] - self.edges[0]

    def centre(self, index: int) -> float:
        """Return the recorded centre coordinate of one cell."""

        return self.centres[self._checked_index(index)]

    def cell_bounds(self, index: int) -> tuple[float, float]:
        """Return the lower and upper boundary of one cell."""

        checked_index = self._checked_index(index)
        return self.edges[checked_index], self.edges[checked_index + 1]

    def index_at(self, value: float, *, clamp: bool = False) -> int | None:
        """Return the cell containing ``value``.

        An interior boundary belongs to the cell on its right.  Both outer
        boundaries are included.  Out-of-bounds coordinates return ``None``
        unless ``clamp`` is true, in which case the nearest outer cell is used.
        Non-finite coordinates always return ``None``.
        """

        coordinate = float(value)
        if not math.isfinite(coordinate):
            return None

        lower, upper = self.bounds
        if coordinate < lower:
            return 0 if clamp else None
        if coordinate > upper:
            return self.count - 1 if clamp else None
        if coordinate == upper:
            return self.count - 1

        return bisect_right(self.edges, coordinate) - 1

    def snap_interval(self, low: float, high: float) -> tuple[float, float]:
        """Expand an interval to the cell edges that contain it."""

        start, stop = self._cell_interval(low, high)
        return self.edges[start], self.edges[stop]

    def slice_for_interval(self, low: float, high: float) -> slice:
        """Return the cells covered by an interval as a NumPy-style slice."""

        start, stop = self._cell_interval(low, high)
        return slice(start, stop)

    def _cell_interval(self, low: float, high: float) -> tuple[int, int]:
        low_value = float(low)
        high_value = float(high)
        if not math.isfinite(low_value) or not math.isfinite(high_value):
            raise ValueError("heatmap selection coordinates must be finite.")

        low_value, high_value = sorted((low_value, high_value))
        lower_bound, upper_bound = self.bounds
        low_value = min(max(low_value, lower_bound), upper_bound)
        high_value = min(max(high_value, lower_bound), upper_bound)

        start = bisect_right(self.edges, low_value) - 1
        start = min(max(start, 0), self.count - 1)
        stop = bisect_left(self.edges, high_value)
        stop = min(max(stop, start + 1), self.count)
        return start, stop

    def _checked_index(self, index: int) -> int:
        try:
            checked_index = operator.index(index)
        except TypeError as error:
            raise TypeError("axis cell index must be an integer") from error
        if not 0 <= checked_index < self.count:
            raise IndexError(
                f"axis cell index {checked_index} is outside "
                f"0..{self.count - 1}"
            )
        return checked_index


@dataclass(frozen=True, slots=True)
class HeatmapGeometry:
    """Immutable two-dimensional rectilinear heatmap geometry."""

    x: AxisGeometry
    y: AxisGeometry

    @classmethod
    def from_centres(
            cls,
            x_centres: Iterable[float],
            y_centres: Iterable[float],
            *,
            x_singleton_span: float = _DEFAULT_SINGLETON_SPAN,
            y_singleton_span: float = _DEFAULT_SINGLETON_SPAN,
            uniform_rel_tol: float = _DEFAULT_UNIFORM_REL_TOL,
            uniform_abs_tol: float = 0.0,
            ) -> "HeatmapGeometry":
        """Build heatmap geometry from X and Y cell-centre coordinates."""

        return cls(
            x=AxisGeometry(
                x_centres,
                singleton_span=x_singleton_span,
                uniform_rel_tol=uniform_rel_tol,
                uniform_abs_tol=uniform_abs_tol,
            ),
            y=AxisGeometry(
                y_centres,
                singleton_span=y_singleton_span,
                uniform_rel_tol=uniform_rel_tol,
                uniform_abs_tol=uniform_abs_tol,
            ),
        )

    @property
    def shape(self) -> tuple[int, int]:
        """Expected data-grid shape in NumPy row-major order: ``(Y, X)``."""

        return self.y.count, self.x.count

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """Return ``(left, bottom, right, top)`` outer cell boundaries."""

        return self.x.edges[0], self.y.edges[0], self.x.edges[-1], self.y.edges[-1]

    @property
    def rect(self) -> tuple[float, float, float, float]:
        """Return a Qt-free ``(left, bottom, width, height)`` rectangle."""

        return self.x.edges[0], self.y.edges[0], self.x.span, self.y.span

    @property
    def is_uniform(self) -> bool:
        """Whether both axes use uniform cell spacing."""

        return self.x.is_uniform and self.y.is_uniform

    def index_at(
            self,
            x_value: float,
            y_value: float,
            *,
            clamp: bool = False,
            ) -> tuple[int, int] | None:
        """Return the ``(X, Y)`` cell containing a plot coordinate."""

        x_index = self.x.index_at(x_value, clamp=clamp)
        y_index = self.y.index_at(y_value, clamp=clamp)
        if x_index is None or y_index is None:
            return None
        return x_index, y_index

    def cell_bounds(
            self,
            x_index: int,
            y_index: int,
            ) -> tuple[float, float, float, float]:
        """Return ``(left, bottom, right, top)`` for one heatmap cell."""

        left, right = self.x.cell_bounds(x_index)
        bottom, top = self.y.cell_bounds(y_index)
        return left, bottom, right, top

    def cell_rect(
            self,
            x_index: int,
            y_index: int,
            ) -> tuple[float, float, float, float]:
        """Return ``(left, bottom, width, height)`` for one heatmap cell."""

        left, bottom, right, top = self.cell_bounds(x_index, y_index)
        return left, bottom, right - left, top - bottom


def canonicalize_heatmap_data(
        x_centres: npt.ArrayLike,
        y_centres: npt.ArrayLike,
        data_grid: npt.ArrayLike,
        ) -> tuple[
            npt.NDArray[np.float64],
            npt.NDArray[np.float64],
            npt.NDArray[Any],
            ]:
    """Return increasing axes while preserving their grid-value mapping."""

    x_values = np.asarray(x_centres, dtype=float)
    y_values = np.asarray(y_centres, dtype=float)
    grid = np.asarray(data_grid)

    if x_values.ndim != 1 or y_values.ndim != 1:
        raise ValueError("Heatmap axes must be one-dimensional.")
    expected_shape = (y_values.size, x_values.size)
    if grid.ndim != 2 or grid.shape != expected_shape:
        raise ValueError(
            f"Heatmap data shape {grid.shape} does not match "
            f"axis shape {expected_shape}."
            )

    if x_values.size > 1 and np.all(np.diff(x_values) < 0.0):
        x_values = x_values[::-1].copy()
        grid = np.flip(grid, axis=1).copy()
    if y_values.size > 1 and np.all(np.diff(y_values) < 0.0):
        y_values = y_values[::-1].copy()
        grid = np.flip(grid, axis=0).copy()

    AxisGeometry(x_values)
    AxisGeometry(y_values)
    return x_values, y_values, grid
