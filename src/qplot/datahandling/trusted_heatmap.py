"""Bounded numeric heatmap reduction before transferring acquisition rows.

Every source query covers a finite primary-key interval of one fixed prefix.
Only axis values and spatial sufficient statistics cross the helper boundary.
"""

from dataclasses import dataclass
from fractions import Fraction

import numpy as np

from qplot.tools.heatmap_geometry import bounded_grid_shape, spatial_axis_bins
from qplot.tools.operation_registry import OperationExecutionError

from .trusted_live import (
    TrustedLiveQueryError,
    TrustedLiveResultLimitError,
    TrustedQuery,
)

SUMMARY_ROWS = 65_536
AGGREGATE_ROWS = 65_536
MAX_GROUPS = 32_768
MAX_AXIS_VALUES = 131_072
MAX_NUMERIC_HEATMAP_CELLS = 2_000_000


class PlotOperationLimitError(TrustedLiveQueryError, OperationExecutionError):
    """A plot's resolution limit does not invalidate its trusted session."""


@dataclass(frozen=True)
class NumericHeatmapPlan:
    x: str
    y: str
    z: str
    full_limit: int
    max_cells: int
    max_side: int
    ranges: tuple
    operations: bool


@dataclass
class NumericHeatmap:
    row_count: int
    selected_count: int
    matching_count: int
    completed: bool
    axes: tuple
    grid: np.ndarray
    unique_counts: tuple
    cardinality_exact: tuple
    bounds: tuple
    watermark: int = 0

    @property
    def disk_bytes(self):
        # Share the broker's bounded cache accounting with private prefixes.
        return self.grid.nbytes + sum(axis.nbytes for axis in self.axes)


def numeric_heatmap(executor, dataset, plan, watermark, completed):
    from .trusted_plot import identifier

    table = identifier(dataset.table_name)
    x, y, z = (identifier(name) for name in (plan.x, plan.y, plan.z))
    finite = float(np.finfo(float).max)
    valid = ' AND '.join(f'{name} BETWEEN {-finite} AND {finite}' for name in (x, y, z))
    range_bindings = []
    for name, limits in zip((x, y), plan.ranges, strict=True):
        if limits is not None:
            valid += f' AND {name} BETWEEN ? AND ?'
            range_bindings.extend(limits)
    unique: list[set[int | float] | None] = [set(), set()]
    lower: list[int | float | None] = [None, None]
    upper: list[int | float | None] = [None, None]
    row_count = selected = matching = 0
    for start in range(0, watermark, SUMMARY_ROWS):
        executor.check_cancelled()
        stop = min(start + SUMMARY_ROWS, watermark)
        interval = 'id>? AND id<=?'
        where = f'{interval} AND {valid}'
        bindings = (start, stop, *range_bindings)
        queries = [
            TrustedQuery(f'SELECT COUNT(*), COUNT({z}) FROM {table} WHERE {interval}', (start, stop)),
            TrustedQuery(f'SELECT COUNT(*), MIN({x}), MAX({x}), MIN({y}), MAX({y}) '
                         f'FROM {table} WHERE {where}', bindings),
        ]
        active_axes = [axis for axis in range(2) if unique[axis] is not None]
        for axis in active_axes:
            name = (x, y)[axis]
            queries.append(TrustedQuery(
                f'SELECT DISTINCT {name} FROM {table} WHERE {where} LIMIT ?',
                (*bindings, MAX_AXIS_VALUES + 1),
            ))
        results = executor.query_batch(tuple(queries))
        count, count_z = results[0].rows[0]
        row_count += count
        selected += count_z
        count, *bounds = results[1].rows[0]
        matching += count
        if count:
            for axis in range(2):
                lo, hi = bounds[2 * axis:2 * axis + 2]
                lower[axis] = lo if lower[axis] is None else min(lower[axis], lo)
                upper[axis] = hi if upper[axis] is None else max(upper[axis], hi)
        for axis, result in zip(active_axes, results[2:], strict=True):
            values = unique[axis]
            assert values is not None
            values.update(row[0] for row in result.rows)
            if len(values) > MAX_AXIS_VALUES:
                unique[axis] = None
        yield

    counts = tuple(len(values) if values is not None else MAX_AXIS_VALUES + 1 for values in unique)
    exact = tuple(values is not None for values in unique)
    if not matching:
        return NumericHeatmap(row_count, selected, 0, completed, (np.array([]), np.array([])),
                              np.empty((0, 0)), counts, exact, tuple(zip(lower, upper, strict=True)))

    # Use the same geometry as the existing SQL heatmap renderer.
    bins = bounded_grid_shape(*counts, max_cells=plan.max_cells, max_side=plan.max_side)
    if bins[0] * bins[1] > MAX_NUMERIC_HEATMAP_CELLS:
        raise TrustedLiveResultLimitError(
            "The numeric heatmap exceeds its bounded aggregation memory budget. "
            "Reduce max_heatmap_grid_cells to at most 2,000,000.")
    axes, groups = [], []
    group_bindings: list[int | float] = []
    for axis, (name, size) in enumerate(zip((x, y), bins, strict=True)):
        if exact[axis] and size >= counts[axis]:
            values = unique[axis]
            assert values is not None
            centres = np.array(sorted(values), dtype=float)
            if np.any(centres[1:] == centres[:-1]):
                raise TrustedLiveQueryError(
                    "Distinct recorded coordinates would be merged by floating-point display. "
                    "The heatmap cannot be represented at this coordinate precision.")
            axes.append(centres)
            groups.append(name)
        else:
            lo, hi = lower[axis], upper[axis]
            assert lo is not None and hi is not None
            centres, edge, scale = spatial_axis_bins(float(lo), float(hi), counts[axis], size)
            axes.append(centres)
            groups.append(f'MIN(CAST(({name} - ?) * ? AS INTEGER), ?)')
            group_bindings.extend((edge, scale, size - 1))
    shape = (len(axes[1]), len(axes[0]))
    sums = np.zeros(shape)
    samples = np.zeros(shape, dtype=np.int64)
    minima, maxima = np.full(shape, np.inf), np.full(shape, -np.inf)
    integers = np.zeros(shape, dtype=bool)

    def indices(gx, gy):
        return tuple(int(np.searchsorted(axes[axis], value))
                     if exact[axis] and bins[axis] >= counts[axis] else int(value)
                     for axis, value in ((1, gy), (0, gx)))

    def source(start, stop):
        sql = (f'SELECT id, {groups[0]} AS gx, {groups[1]} AS gy, {z} AS v '
               f'FROM {table} WHERE id>? AND id<=? AND {valid}')
        return sql, (*group_bindings, start, stop, *range_bindings)

    # Split dense pages when necessary, keeping response size independent of
    # the configured display size and acquisition length.
    pending: list[tuple[int, int]] = []
    next_start = 0
    while pending or next_start < watermark:
        executor.check_cancelled()
        if pending:
            start, stop = pending.pop()
        else:
            start, stop = next_start, min(next_start + AGGREGATE_ROWS, watermark)
            next_start = stop
        sql, bindings = source(start, stop)
        rows = executor.query(
            'SELECT gx, gy, TOTAL(v), COUNT(*), MIN(v), MAX(v), '
            f"MAX(typeof(v)='integer') FROM ({sql}) GROUP BY gx, gy LIMIT ?",
            (*bindings, MAX_GROUPS + 1),
        ).rows
        if len(rows) > MAX_GROUPS:
            middle = (start + stop) // 2
            pending.extend(((middle, stop), (start, middle)))
        else:
            for number, (gx, gy, total, count, lo, hi, integer) in enumerate(rows):
                if number % 1024 == 0:
                    executor.check_cancelled()
                key = indices(gx, gy)
                sums[key] = float(sums[key]) + (total if total is not None else np.nan)
                samples[key] += count
                minima[key] = min(minima[key], lo)
                maxima[key] = max(maxima[key], hi)
                integers[key] |= bool(integer)
        yield

    largest = np.maximum(np.abs(minima), np.abs(maxima))
    with np.errstate(over='ignore', invalid='ignore'):
        risky = (samples > 0) & (
            ~np.isfinite(sums) | ((minima < 0) & (maxima > 0))
            | (largest > (finite / 2) / np.maximum(samples, 1))
            | (integers & (largest * samples >= 2**53)))
    totals = {tuple(key): Fraction() for key in np.argwhere(risky)}
    if totals:
        # Replay original values, never averages of page averages. This keeps
        # cancellation residuals, large integers and near-overflow means.
        for start in range(0, watermark, MAX_GROUPS):
            executor.check_cancelled()
            sql, bindings = source(start, min(start + MAX_GROUPS, watermark))
            for number, (_id, gx, gy, value) in enumerate(executor.query(sql, bindings).rows):
                if number % 1024 == 0:
                    executor.check_cancelled()
                key = indices(gx, gy)
                if key in totals:
                    totals[key] += Fraction(value)
            yield
    grid = np.full(shape, np.nan)
    np.divide(sums, samples, out=grid, where=samples > 0)
    for key, total in totals.items():
        executor.check_cancelled()
        grid[key] = float(total / int(samples[key]))
    return NumericHeatmap(row_count, selected, matching, completed, tuple(axes), grid,
                          counts, exact, tuple(zip(lower, upper, strict=True)))
