"""Immutable heatmap resolution facts and explanations, without GUI access."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
from typing import Any

FULL_HEATMAP_POINTS_KEY = "runtime_settings.max_full_heatmap_points"
HEATMAP_GRID_CELLS_KEY = "runtime_settings.max_heatmap_grid_cells"


def _count(value: Any) -> int | None:
    """Accept known non-negative whole counts; invalid values stay unknown."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
        if number < 0 or (not isinstance(value, str) and value != number):
            return None
        return number
    except (TypeError, ValueError, OverflowError):
        return None


def _range(value: Any) -> tuple[float, float] | None:
    try:
        low, high = (float(item) for item in value)
    except (TypeError, ValueError, OverflowError):
        return None
    return (low, high) if isfinite(low) and isfinite(high) else None


@dataclass(frozen=True)
class GridSize:
    rows: int | None = None
    columns: int | None = None
    cells: int | None = None

    @classmethod
    def from_shape(cls, shape: Any) -> GridSize:
        try:
            rows, columns = (_count(value) for value in shape)
        except (TypeError, ValueError):
            return cls()
        if not rows or not columns:
            return cls()
        return cls(rows, columns, rows * columns)

    @classmethod
    def from_metadata(cls, info: Mapping[str, Any], prefix: str) -> GridSize:
        # An absent count is not replaced by a guessed value. Array-backed
        # measurements can have incomplete or deliberately bounded metadata.
        return cls(_count(info.get(prefix + "rows")),
                   _count(info.get(prefix + "columns")),
                   _count(info.get(prefix + "cell_count")))


def source_grid_size(
    shapes: Mapping[str, Any] | None,
    parameter: str,
    dependencies: Sequence[str],
    axes: Mapping[str, str],
) -> GridSize:
    """Orient planned dimensions using parameter names, including swapped axes."""
    shape = shapes.get(parameter) if isinstance(shapes, Mapping) else None
    if shape is None:
        return GridSize()
    try:
        if len(shape) != len(dependencies):
            return GridSize()
        return GridSize.from_shape((shape[dependencies.index(axes["y"])],
                                    shape[dependencies.index(axes["x"])]))
    except (KeyError, TypeError, ValueError, IndexError):
        return GridSize()


@dataclass(frozen=True)
class HeatmapResolutionReport:
    source: GridSize = GridSize()
    displayed: GridSize = GridSize()
    sampled: bool = False
    aggregated: bool = False
    spatial_mean: bool = False
    binned: bool = False
    interpolated: bool = False
    inferred_reduction: bool = False
    dimensions_limited: bool = False
    source_rows: int | None = None
    estimated_source_rows: int | None = None
    estimated_range_rows: int | None = None
    loaded_points: int | None = None
    aggregated_rows: int | None = None
    sample_limit: int | None = None
    sample_stride: int | None = None
    uniform_sample: bool = False
    unique_x: int | None = None
    unique_y: int | None = None
    full_resolution_limit: int | None = None
    x_range: tuple[float, float] | None = None
    y_range: tuple[float, float] | None = None
    grid_cell_limit_reached: bool = False
    grid_cell_limit: int | None = None

    @property
    def reduced(self) -> bool:
        return self.sampled or self.aggregated or self.binned or self.interpolated or self.inferred_reduction

    @property
    def limiting_preference(self) -> str | None:
        """Identify an adjustable limit evidenced by this displayed result."""
        if self.grid_cell_limit_reached:
            return HEATMAP_GRID_CELLS_KEY
        if self.full_resolution_limit is None or not (self.sampled or self.aggregated or self.binned):
            return None
        cropped = self.x_range is not None or self.y_range is not None
        rows = self.estimated_range_rows if cropped else self.source_rows
        cells = (self.unique_x or 0) * (self.unique_y or 0)
        if max(rows or 0, cells) > self.full_resolution_limit:
            return FULL_HEATMAP_POINTS_KEY
        return None

    @classmethod
    def from_metadata(cls, info: Mapping[str, Any]) -> HeatmapResolutionReport:
        ranges = info.get("axis_ranges")
        ranges = ranges if isinstance(ranges, Mapping) else {}
        return cls(
            source=GridSize.from_metadata(info, "source_grid_"),
            displayed=GridSize.from_metadata(info, "grid_"),
            sampled=bool(info.get("source_sampled")),
            aggregated=bool(info.get("source_aggregated")),
            spatial_mean="spatial mean" in str(info.get("source_aggregation_strategy", "")),
            binned=bool(info.get("grid_binned")),
            interpolated=bool(info.get("empty_bins_filled")),
            dimensions_limited=bool(info.get("source_dimensions_limited")),
            source_rows=_count(info.get("source_row_count")),
            estimated_range_rows=_count(info.get("estimated_range_rows")),
            loaded_points=_count(info.get("loaded_point_count")),
            aggregated_rows=_count(info.get("aggregated_source_row_count")),
            sample_limit=_count(info.get("source_sample_limit")),
            sample_stride=_count(info.get("source_sample_stride")),
            uniform_sample=info.get("source_sample_strategy") == "uniform rowid sample",
            unique_x=_count(info.get("unique_x_count")),
            unique_y=_count(info.get("unique_y_count")),
            full_resolution_limit=_count(info.get("full_resolution_point_limit")),
            grid_cell_limit_reached=bool(info.get("grid_cell_limit_reached")),
            grid_cell_limit=_count(info.get("grid_cell_limit")),
            x_range=_range(ranges.get("x")),
            y_range=_range(ranges.get("y")),
        )


def resolution_report(
    result: Any,
    *,
    source: GridSize = GridSize(),
    displayed: GridSize = GridSize(),
    source_rows: Any = None,
    full_resolution_limit: Any = None,
) -> HeatmapResolutionReport | None:
    """Prefer explicit processing facts; infer only an uncropped grid reduction.

    The result is an observation only: no arrays are copied and no source is
    queried. Fallback inputs are already available on the plot window.
    """
    info = getattr(result, "heatmap_downsample_info", None)
    if isinstance(info, Mapping):
        report = HeatmapResolutionReport.from_metadata(info)
        if report.reduced:
            return report
    if getattr(result, "heatmap_full_resolution", False) is True:
        return None

    result_grid = getattr(result, "dataGrid", None)
    if result_grid is not None:
        displayed = GridSize.from_shape(getattr(result_grid, "shape", None))
    result_source = GridSize.from_shape(getattr(result, "heatmap_source_grid_shape", None))
    if result_source.cells is not None:
        source = result_source
    limit = _count(getattr(result, "max_full_heatmap_points", None))
    if limit is None:
        limit = _count(full_resolution_limit)
    estimate = _count(getattr(result, "total_point_count_estimate", None))
    if estimate is None:
        estimate = _count(source_rows)
    ranges = getattr(result, "heatmap_axis_ranges", None)
    cropped = ranges is not None
    inferred = bool(
        not cropped and source.cells is not None and displayed.cells is not None
        and limit is not None and source.cells > max(1, limit)
        and (displayed.cells < source.cells
             or (displayed.rows is not None and source.rows is not None and displayed.rows < source.rows)
             or (displayed.columns is not None and source.columns is not None
                 and displayed.columns < source.columns))
    )
    sampled = bool(getattr(result, "sampled_heatmap_source", False))
    aggregated = bool(getattr(result, "aggregated_heatmap_source", False))
    if not (inferred or sampled or aggregated):
        return None
    ranges = ranges if isinstance(ranges, Mapping) else {}
    return HeatmapResolutionReport(
        source=source, displayed=displayed, sampled=sampled, aggregated=aggregated,
        inferred_reduction=inferred, estimated_source_rows=estimate,
        estimated_range_rows=_count(getattr(result, "_heatmap_estimated_range_rows", None)),
        loaded_points=_count(getattr(result, "loaded_point_count", None)),
        aggregated_rows=_count(getattr(result, "_heatmap_aggregated_source_rows", None)),
        sample_stride=_count(getattr(result, "_heatmap_source_sample_stride", None)),
        full_resolution_limit=limit, x_range=_range(ranges.get("x")), y_range=_range(ranges.get("y")),
    )


def _format_count(value: int | None) -> str:
    return f"{value:,}" if value is not None else "unknown"


def _ordinal(number: int) -> str:
    suffix = "th"
    if number % 100 not in (11, 12, 13):
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number:,}{suffix}"


def resolution_summary(report: HeatmapResolutionReport) -> str:
    lines = []
    for label, grid in (("Source", report.source), ("Displayed", report.displayed)):
        dimensions = (f"{grid.columns:,} × {grid.rows:,} (columns × rows)"
                      if grid.columns is not None and grid.rows is not None
                      else "Exact dimensions unavailable")
        lines.append(f"{label} grid: {dimensions}")
    if report.limiting_preference == HEATMAP_GRID_CELLS_KEY:
        if report.grid_cell_limit is not None:
            lines.append(f"The display grid is limited to {report.grid_cell_limit:,} cells.")
        else:
            lines.append("The display grid cell limit was reached.")
    elif report.limiting_preference == FULL_HEATMAP_POINTS_KEY and report.full_resolution_limit is not None:
        lines.append(f"The full-resolution heatmap limit of {report.full_resolution_limit:,} points was exceeded.")
    lines.append("")
    if report.sampled:
        lines.append("A subset of source rows was sampled before plotting.")
        if report.binned:
            lines.append("Sampled values were averaged into display cells.")
    elif report.aggregated:
        lines.append("Source rows were combined using spatial means." if report.spatial_mean
                     else "Source rows were aggregated before plotting.")
    elif report.binned:
        lines.append("Loaded values were averaged into display cells.")
    elif report.inferred_reduction:
        lines.append("The displayed grid is smaller than the source grid.")
    return "\n".join(lines)


def _source_description(report: HeatmapResolutionReport) -> str:
    if report.dimensions_limited:
        return ("Exact source dimensions were not retained during downsampling "
                "to keep memory use bounded.")
    grid = report.source
    if grid.columns is None or grid.rows is None:
        return "Exact source grid dimensions are unavailable in the run metadata."
    return (f"The source heatmap grid is {grid.columns:,} x {grid.rows:,} = "
            f"{_format_count(grid.cells)} cells.")


def resolution_details(report: HeatmapResolutionReport) -> str:
    lines = ["This heatmap is displayed at reduced resolution."]
    if not report.reduced:
        return "\n".join(lines)
    lines.append("")
    if report.x_range is not None and report.y_range is not None:
        x_low, x_high = report.x_range
        y_low, y_high = report.y_range
        lines.extend(["Visible range loaded:", f"X: {x_low:.6g} to {x_high:.6g}",
                      f"Y: {y_low:.6g} to {y_high:.6g}", ""])
    columns = _format_count(report.displayed.columns)
    rows = _format_count(report.displayed.rows)
    if report.binned or report.inferred_reduction:
        lines.extend([
            "Full-resolution grid:", _source_description(report),
            f"The full-resolution heatmap limit is {_format_count(report.full_resolution_limit)} points.",
            f"The plotted grid is {columns} x {rows} = {_format_count(report.displayed.cells)} cells.", "",
        ])

    lines.append("Source rows:")
    if report.aggregated and not report.sampled and report.spatial_mean and report.aggregated_rows is not None:
        lines.append(f"Spatial averaging used {report.aggregated_rows:,} matching source rows.")
    elif report.aggregated:
        lines.append("Source rows were aggregated before plotting.")
    elif report.loaded_points is not None and report.source_rows is not None:
        lines.append(f"Loaded {report.loaded_points:,} finite source rows from {report.source_rows:,} database rows.")
    elif report.loaded_points is not None:
        lines.append(f"Loaded {report.loaded_points:,} finite source rows.")
    else:
        lines.append("The number of loaded source rows is unknown.")
    if report.estimated_source_rows is not None:
        lines.append(f"Estimated source rows: {report.estimated_source_rows:,}.")
    if report.estimated_range_rows is not None and report.estimated_range_rows != report.source_rows:
        lines.append(f"Estimated rows in the visible range: {report.estimated_range_rows:,}.")
    if report.sampled:
        cap = f", capped at {report.sample_limit:,} rows." if report.sample_limit is not None else "."
        if report.sample_stride is not None and report.sample_stride > 1:
            lines.append("Database rows were stride-sampled before plotting "
                         f"(about every {_ordinal(report.sample_stride)} matching row)" + cap)
        elif report.uniform_sample:
            lines.append("Database rows were uniformly sampled before plotting" + cap)
        else:
            lines.append("Database rows were sampled before plotting" + cap)

    lines.extend(["", "Display grid:"])
    if report.binned:
        prefix = ("Aggregated values were displayed on a " if report.aggregated
                  else "Loaded values were averaged into a ")
        lines.append(prefix + f"{columns} x {rows} display grid.")
        if report.unique_x is None or report.unique_y is None:
            lines.append("Exact source-axis counts were not retained during downsampling."
                         if report.dimensions_limited else "Exact source-axis counts are unavailable.")
        else:
            lines.append(f"The source x/y positions would form up to {report.unique_x:,} x {report.unique_y:,} cells.")
    elif report.inferred_reduction:
        lines.append(f"Loaded values were displayed on a {columns} x {rows} grid.")
        lines.append("The reduction method is unavailable in the result metadata.")
    elif report.displayed.columns is not None and report.displayed.rows is not None:
        lines.append(f"Loaded values were displayed on an exact {columns} x {rows} grid.")
    else:
        lines.append("The displayed grid dimensions are unavailable.")
    if report.interpolated:
        lines.append("Empty sampled display bins were filled by interpolation." if report.sampled
                     else "Empty display bins were filled by interpolation.")
    return "\n".join(lines)
