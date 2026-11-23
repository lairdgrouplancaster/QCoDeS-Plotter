"""Resolution explanations preserve measurement facts and unknown counts."""

from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from qplot.tools.heatmap_resolution import (
    FULL_HEATMAP_POINTS_KEY,
    HEATMAP_GRID_CELLS_KEY,
    GridSize,
    HeatmapResolutionReport,
    resolution_details,
    resolution_report,
    resolution_summary,
    source_grid_size,
)


@pytest.mark.parametrize("metadata, expected", [
    ({"source_aggregated": True, "source_row_count": 101}, FULL_HEATMAP_POINTS_KEY),
    ({"grid_binned": True, "unique_x_count": 11, "unique_y_count": 10}, FULL_HEATMAP_POINTS_KEY),
    ({"source_aggregated": True, "source_row_count": 101, "grid_cell_limit_reached": True},
     HEATMAP_GRID_CELLS_KEY),
    ({"grid_binned": True, "source_grid_cell_count": 300, "unique_x_count": 4, "unique_y_count": 4}, None),
    ({"source_aggregated": True, "source_row_count": 101, "estimated_range_rows": 20,
      "axis_ranges": {"x": (0, 1)}}, None),
    ({"source_sampled": True, "source_row_count": 100}, None),
    ({"empty_bins_filled": True}, None),
])
def test_limit_focus_uses_observed_points_or_display_budget(metadata, expected):
    report = HeatmapResolutionReport.from_metadata({"full_resolution_point_limit": 100, **metadata})
    assert report.limiting_preference == expected


@pytest.mark.parametrize("metadata, expected, absent", [
    ({"grid_binned": True}, "Loaded values were averaged", "All matching"),
    ({"source_sampled": True}, "A subset of source rows was sampled", "uniformly"),
    ({"source_sampled": True, "grid_binned": True},
     "Sampled values were averaged", "All matching"),
    ({"source_aggregated": True}, "Source rows were aggregated", "spatial means"),
    ({"source_aggregated": True, "source_aggregation_strategy": "spatial mean",
      "aggregated_source_row_count": 200, "loaded_point_count": 20},
     "Source rows were combined using spatial means", "sampled before plotting"),
    ({"source_sampled": True, "source_aggregated": True,
      "source_aggregation_strategy": "spatial mean", "aggregated_source_row_count": 200},
     "A subset of source rows was sampled", "All 200"),
])
def test_heatmap_resolution_wording_follows_metadata(metadata, expected, absent):
    summary = resolution_summary(HeatmapResolutionReport.from_metadata(metadata))
    details = resolution_details(HeatmapResolutionReport.from_metadata(metadata))
    assert summary.startswith("Source grid:")
    assert expected in summary
    assert absent not in summary + details
    assert "All matching source rows were read" not in details
    assert "does not omit source measurements" not in details


def test_heatmap_resolution_summary_compares_known_dimensions_and_keeps_details_separate():
    metadata = {
        "source_sampled": True, "source_sample_stride": 3,
        "source_sample_limit": 100, "grid_binned": True,
        "source_grid_columns": 2_000, "source_grid_rows": 1_000,
        "grid_columns": 500, "grid_rows": 400,
        "full_resolution_point_limit": 1_000_000, "empty_bins_filled": True,
    }
    summary = resolution_summary(HeatmapResolutionReport.from_metadata(metadata))
    assert "Source grid: 2,000 × 1,000 (columns × rows)" in summary
    assert "Displayed grid: 500 × 400 (columns × rows)" in summary
    assert "3rd" not in summary
    assert "interpolation" not in summary
    assert "about every 3rd matching row" in resolution_details(HeatmapResolutionReport.from_metadata(metadata))
    metadata.update({
        "source_dimensions_limited": True, "source_grid_rows": None,
        "source_grid_columns": 2_000,
    })
    summary = resolution_summary(HeatmapResolutionReport.from_metadata(metadata))
    assert "Source grid: Exact dimensions unavailable" in summary
    assert "2,000 ×" not in summary
    assert "Displayed grid: 500 × 400" in summary


def test_heatmap_resolution_details_do_not_treat_loaded_source_rows_as_display_cells():
    metadata = {
        "source_aggregated": True, "source_aggregation_strategy": "spatial mean",
        "aggregated_source_row_count": 2_000, "loaded_point_count": 2_000,
        "grid_columns": 10, "grid_rows": 10, "grid_cell_count": 100,
        "grid_binned": True,
    }
    details = resolution_details(HeatmapResolutionReport.from_metadata(metadata))
    assert "Spatial averaging used 2,000 matching source rows." in details
    assert "Aggregated values were displayed on a 10 x 10 display grid." in details
    assert "2,000 spatial mean cells" not in details
    metadata.pop("source_aggregation_strategy")
    details = resolution_details(HeatmapResolutionReport.from_metadata(metadata))
    assert "spatial mean" not in details
    assert "averaged" not in details


@pytest.mark.parametrize("value", [None, -1, float("nan"), float("inf"), 1.5, True, "unknown"])
def test_invalid_counts_remain_unknown(value):
    report = HeatmapResolutionReport.from_metadata({
        "source_sampled": True, "loaded_point_count": value, "source_grid_rows": value,
    })
    assert report.loaded_points is None
    assert report.source.rows is None
    assert "The number of loaded source rows is unknown." in resolution_details(report)
    assert "The displayed grid dimensions are unavailable." in resolution_details(report)


def test_report_is_an_immutable_snapshot_of_nested_metadata():
    info = {"source_sampled": True, "axis_ranges": {"x": [1., 2.], "y": [3., 4.]},
            "grid_columns": 5, "grid_rows": 6}
    report = HeatmapResolutionReport.from_metadata(info)
    info["axis_ranges"]["x"][0] = 99.
    info["grid_columns"] = 999
    assert report.x_range == (1., 2.)
    assert report.displayed.columns == 5
    with pytest.raises(FrozenInstanceError):
        report.sampled = False
    with pytest.raises(FrozenInstanceError):
        report.displayed.columns = 99


@pytest.mark.parametrize("ranges", [None, {"x": (1., 2.), "y": (3., 4.)}], ids=["full", "cropped"])
def test_full_resolution_slices_do_not_infer_downsampling(ranges):
    result = SimpleNamespace(heatmap_full_resolution=True, heatmap_axis_ranges=ranges)
    assert resolution_report(result, source=GridSize(1000, 1000, 1_000_000),
                             displayed=GridSize(2, 2, 4), full_resolution_limit=10) is None


def test_cropped_grid_is_not_evidence_of_reduction_or_sampling():
    result = SimpleNamespace(heatmap_axis_ranges={"x": (1., 2.), "y": (3., 4.)},
                             loaded_point_count=4, total_point_count_estimate=1_000_000)
    assert resolution_report(result, source=GridSize(1000, 1000, 1_000_000),
                             displayed=GridSize(2, 2, 4), full_resolution_limit=10) is None


def test_fallback_does_not_invent_averaging_interpolation_or_sampling_strategy():
    result = SimpleNamespace(sampled_heatmap_source=True, loaded_point_count=4,
                             total_point_count_estimate=1_000_000)
    report = resolution_report(result, source=GridSize(1000, 1000, 1_000_000),
                               displayed=GridSize(2, 2, 4), full_resolution_limit=10)
    assert report.sampled and report.inferred_reduction
    assert not report.binned
    assert not report.interpolated
    assert not report.uniform_sample
    assert report.source_rows is None
    details = resolution_details(report)
    assert "Estimated source rows: 1,000,000" in details
    assert "reduction method is unavailable" in details
    assert "averaged" not in details
    assert "interpolation" not in details
    assert "uniformly" not in details


def test_missing_shape_does_not_turn_database_rows_into_grid_cells():
    result = SimpleNamespace(total_point_count_estimate=1_000_000, loaded_point_count=4)
    assert resolution_report(result, displayed=GridSize(2, 2, 4), full_resolution_limit=10) is None


@pytest.mark.parametrize("axes,expected", [
    ({"x": "gate", "y": "bias"}, (3, 5)),
    ({"x": "bias", "y": "gate"}, (5, 3)),
    ({"x": "missing", "y": "gate"}, (None, None)),
])
def test_planned_source_dimensions_follow_named_axes(axes, expected):
    grid = source_grid_size({"signal": (5, 3)}, "signal", ["gate", "bias"], axes)
    assert (grid.rows, grid.columns) == expected


@pytest.mark.parametrize("shape", [None, (), (1,), (2, 0), (float("inf"), 2), (-1, 2)])
def test_invalid_shapes_remain_unknown(shape):
    assert GridSize.from_shape(shape) == GridSize()

