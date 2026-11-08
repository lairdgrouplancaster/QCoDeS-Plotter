"""Completed plot data handed from processing workers to GUI publication."""

from dataclasses import dataclass
from typing import Any

import numpy as np

from qplot.datahandling.file_identity import DatabaseFileIdentity


@dataclass(frozen=True, slots=True)
class PlotSourceIdentity:
    database_path: str | None
    resolved_database_path: str | None
    database_identity: DatabaseFileIdentity | None
    sidecar_identities: frozenset[DatabaseFileIdentity] | None


@dataclass(frozen=True, slots=True)
class PlotCacheUpdate:
    read_status: dict[str, int | None]
    write_status: dict[str, int | None]
    data: dict[str, dict[str, np.ndarray]]


@dataclass(frozen=True, slots=True)
class PlotLoadResult:
    """One complete success; arrays retain their existing owned buffers.

    The record and mapping snapshots separate published output from a worker's
    mutable execution attributes. Array storage is shared intentionally: no
    additional sample copy or change to array writeability happens at handoff.
    """

    axis_data: dict[str, np.ndarray]
    axis_param: dict[str, Any]
    source: PlotSourceIdentity
    cache_update: PlotCacheUpdate | None = None
    dataGrid: np.ndarray | None = None
    display_param: Any = None
    dataset_completed: bool | None = None
    dataset_length_at_start: int | None = None
    loaded_point_count: int | None = None
    loaded_from_sql_heatmap: bool = False
    aggregated_heatmap_source: bool = False
    sampled_heatmap_source: bool = False
    heatmap_full_resolution: bool = False
    heatmap_downsample_info: dict[str, Any] | None = None
    heatmap_axis_ranges: dict | None = None
    heatmap_source_axis_ranges: dict | None = None
    heatmap_source_grid_shape: tuple[int, int] | None = None
    total_point_count_estimate: int | None = None
    max_full_heatmap_points: int | None = None
    _heatmap_estimated_range_rows: int | None = None
    _heatmap_aggregated_source_rows: int | None = None
    _heatmap_source_sample_stride: int | None = None
    slice_volume: Any = None
    slice_frame: Any = None


def capture_plot_result(worker: Any) -> PlotLoadResult:
    """Snapshot output only after successful loading and final transforms."""
    cache_update = None
    if worker.read_data:
        cache_update = PlotCacheUpdate(
            dict(worker.updated_read_status),
            dict(worker.updated_write_status),
            {name: dict(columns) for name, columns in worker.cache_data.items()},
        )
    source = PlotSourceIdentity(
        getattr(worker, "expected_database_path", None),
        getattr(worker, "expected_resolved_database_path", None),
        getattr(worker, "database_identity", None),
        getattr(worker, "expected_sidecar_identities", None),
    )
    shape = getattr(worker, "heatmap_source_grid_shape", None)
    if shape is None and hasattr(worker, "_heatmap_source_grid_shape_from_metadata"):
        try:
            shape = worker._heatmap_source_grid_shape_from_metadata()
        except (AttributeError, TypeError, ValueError):
            pass
    info = getattr(worker, "heatmap_downsample_info", None)
    axis_ranges = getattr(worker, "heatmap_axis_ranges", None)
    source_axis_ranges = getattr(worker, "heatmap_source_axis_ranges", None)
    return PlotLoadResult(
        axis_data=dict(worker.axis_data),
        axis_param=dict(worker.axis_param),
        source=source,
        cache_update=cache_update,
        dataGrid=getattr(worker, "dataGrid", None),
        display_param=getattr(worker, "display_param", None),
        dataset_completed=getattr(worker, "dataset_completed", None),
        dataset_length_at_start=getattr(worker, "dataset_length_at_start", None),
        loaded_point_count=getattr(worker, "loaded_point_count", None),
        loaded_from_sql_heatmap=getattr(worker, "loaded_from_sql_heatmap", False),
        aggregated_heatmap_source=getattr(worker, "aggregated_heatmap_source", False),
        sampled_heatmap_source=getattr(worker, "sampled_heatmap_source", False),
        heatmap_full_resolution=getattr(worker, "heatmap_full_resolution", False),
        heatmap_downsample_info=dict(info) if info is not None else None,
        heatmap_axis_ranges=dict(axis_ranges) if axis_ranges is not None else None,
        heatmap_source_axis_ranges=dict(source_axis_ranges) if source_axis_ranges is not None else None,
        heatmap_source_grid_shape=shape,
        total_point_count_estimate=getattr(worker, "total_point_count_estimate", None),
        max_full_heatmap_points=getattr(worker, "max_full_heatmap_points", None),
        _heatmap_estimated_range_rows=getattr(worker, "_heatmap_estimated_range_rows", None),
        _heatmap_aggregated_source_rows=getattr(worker, "_heatmap_aggregated_source_rows", None),
        _heatmap_source_sample_stride=getattr(worker, "_heatmap_source_sample_stride", None),
    )
