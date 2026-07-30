import math

import numpy as np
import pytest

from qplot.tools.heatmap_geometry import (
    AxisGeometry,
    HeatmapGeometry,
    canonicalize_heatmap_data,
)


@pytest.mark.parametrize(
    ("x_centres", "y_centres", "expected_grid"),
    [
        ([4.0, 1.0, 0.0], [10.0, 13.0], [[3, 2, 1], [6, 5, 4]]),
        ([0.0, 1.0, 4.0], [13.0, 10.0], [[4, 5, 6], [1, 2, 3]]),
        ([4.0, 1.0, 0.0], [13.0, 10.0], [[6, 5, 4], [3, 2, 1]]),
    ],
)
def test_canonicalize_heatmap_data_reverses_axes_with_grid(
        x_centres,
        y_centres,
        expected_grid,
        ):
    x, y, grid = canonicalize_heatmap_data(
        x_centres,
        y_centres,
        [[1, 2, 3], [4, 5, 6]],
        )

    np.testing.assert_array_equal(x, [0.0, 1.0, 4.0])
    np.testing.assert_array_equal(y, [10.0, 13.0])
    np.testing.assert_array_equal(grid, expected_grid)


def test_canonicalize_heatmap_data_rejects_shape_mismatch():
    with pytest.raises(ValueError, match="does not match"):
        canonicalize_heatmap_data([0.0, 1.0], [10.0, 11.0], np.zeros((3, 2)))


class TestAxisGeometry:
    def test_uniform_centres_are_expanded_to_cell_edges(self):
        axis = AxisGeometry([0.0, 1.0])

        assert axis.centres == (0.0, 1.0)
        assert axis.edges == (-0.5, 0.5, 1.5)
        assert axis.is_uniform
        assert axis.count == 2
        assert axis.bounds == (-0.5, 1.5)
        assert axis.span == 2.0

    def test_nonuniform_centres_use_midpoints_and_end_spacing(self):
        axis = AxisGeometry([1.0, 2.0, 5.0])

        assert axis.edges == (0.5, 1.5, 3.5, 6.5)
        assert not axis.is_uniform

    @pytest.mark.parametrize("centre", [0.0, 10.0, -10.0])
    def test_singleton_has_positive_default_span(self, centre):
        axis = AxisGeometry([centre])

        assert axis.centres == (centre,)
        assert axis.edges == (centre - 0.5, centre + 0.5)
        assert axis.span == 1.0
        assert axis.is_uniform

    def test_singleton_span_can_be_supplied_by_caller(self):
        axis = AxisGeometry([1000.0], singleton_span=20.0)

        assert axis.edges == (990.0, 1010.0)
        assert axis.span == 20.0

    @pytest.mark.parametrize("singleton_span", [0.0, -1.0, math.inf, math.nan])
    def test_singleton_span_must_be_positive_and_finite(self, singleton_span):
        with pytest.raises(ValueError, match="singleton_span"):
            AxisGeometry([0.0], singleton_span=singleton_span)

    @pytest.mark.parametrize(
        ("centres", "message"),
        [
            ([], "at least one"),
            ([0.0, math.nan], "finite"),
            ([0.0, math.inf], "finite"),
            ([2.0, 1.0], "descending"),
            ([0.0, 0.0], "strictly increasing"),
            ([0.0, 2.0, 1.0], "strictly increasing"),
        ],
    )
    def test_invalid_axes_are_rejected(self, centres, message):
        with pytest.raises(ValueError, match=message):
            AxisGeometry(centres)

    def test_index_at_maps_values_to_cells(self):
        axis = AxisGeometry([0.0, 1.0, 2.0])

        assert axis.index_at(-0.5) == 0
        assert axis.index_at(0.49) == 0
        assert axis.index_at(0.5) == 1
        assert axis.index_at(2.5) == 2
        assert axis.index_at(-0.5001) is None
        assert axis.index_at(2.5001) is None
        assert axis.index_at(math.nan) is None

    def test_index_at_can_clamp_to_nearest_outer_cell(self):
        axis = AxisGeometry([0.0, 1.0])

        assert axis.index_at(-100.0, clamp=True) == 0
        assert axis.index_at(100.0, clamp=True) == 1
        assert axis.index_at(math.inf, clamp=True) is None

    def test_snap_interval_uses_recorded_cell_edges(self):
        axis = AxisGeometry([0.0, 1.0, 4.0])

        assert axis.snap_interval(0.6, 4.6) == (0.5, 5.5)
        assert axis.snap_interval(4.6, 0.6) == (0.5, 5.5)
        assert axis.snap_interval(-100.0, 100.0) == (-0.5, 5.5)
        assert axis.snap_interval(*axis.snap_interval(0.6, 4.6)) == (0.5, 5.5)

    def test_slice_for_interval_uses_same_edges_as_snapping(self):
        axis = AxisGeometry([0.0, 1.0, 4.0])

        assert axis.slice_for_interval(0.6, 4.6) == slice(1, 3)
        assert axis.slice_for_interval(4.6, 0.6) == slice(1, 3)
        assert axis.slice_for_interval(-100.0, 100.0) == slice(0, 3)

    def test_centre_and_cell_bounds_validate_index(self):
        axis = AxisGeometry([0.0, 1.0, 3.0])

        assert axis.centre(1) == 1.0
        assert axis.cell_bounds(1) == (0.5, 2.0)
        with pytest.raises(IndexError, match="axis cell index"):
            axis.centre(3)
        with pytest.raises(IndexError, match="axis cell index"):
            axis.cell_bounds(-1)

    def test_uniformity_tolerance_is_configurable(self):
        nearly_uniform = [0.0, 1.0, 2.0 + 1e-10]

        assert AxisGeometry(nearly_uniform).is_uniform
        assert not AxisGeometry(nearly_uniform, uniform_rel_tol=1e-12).is_uniform

    def test_edges_must_remain_distinct_at_floating_point_precision(self):
        left = 1e16
        adjacent = np.nextafter(left, math.inf)

        with pytest.raises(ValueError, match="precision"):
            AxisGeometry([left, adjacent])


class TestHeatmapGeometry:
    def test_geometry_exposes_qt_free_rect_shape_and_bounds(self):
        geometry = HeatmapGeometry.from_centres(
            x_centres=[0.0, 1.0],
            y_centres=[10.0, 12.0, 14.0],
        )

        assert geometry.shape == (3, 2)
        assert geometry.bounds == (-0.5, 9.0, 1.5, 15.0)
        assert geometry.rect == (-0.5, 9.0, 2.0, 6.0)
        assert geometry.is_uniform

    def test_geometry_supports_independent_singleton_spans(self):
        geometry = HeatmapGeometry.from_centres(
            x_centres=[-4.0],
            y_centres=[0.0],
            x_singleton_span=2.0,
            y_singleton_span=4.0,
        )

        assert geometry.rect == (-5.0, -2.0, 2.0, 4.0)

    def test_cell_lookup_and_bounds_share_one_coordinate_model(self):
        geometry = HeatmapGeometry.from_centres(
            x_centres=[0.0, 1.0, 3.0],
            y_centres=[10.0, 20.0],
        )

        assert geometry.index_at(1.8, 16.0) == (1, 1)
        assert geometry.cell_bounds(1, 1) == (0.5, 15.0, 2.0, 25.0)
        assert geometry.cell_rect(1, 1) == (0.5, 15.0, 1.5, 10.0)
        assert geometry.index_at(-100.0, 16.0) is None
        assert geometry.index_at(-100.0, 16.0, clamp=True) == (0, 1)

    def test_heatmap_is_nonuniform_when_either_axis_is_nonuniform(self):
        geometry = HeatmapGeometry.from_centres(
            x_centres=[0.0, 1.0, 3.0],
            y_centres=[10.0, 20.0],
        )

        assert not geometry.is_uniform
