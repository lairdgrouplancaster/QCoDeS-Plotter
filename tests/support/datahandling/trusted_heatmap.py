"""Shared fixtures and assertions for trusted heatmap."""

from qplot.datahandling.trusted_heatmap import NumericHeatmapPlan

__all__ = [
    'plan',
]


def plan(**overrides):
    values = dict(x='y', y='x', z='z', full_limit=4, max_cells=4, max_side=2,
                  ranges=(None, None), operations=False)
    return NumericHeatmapPlan(**(values | overrides))
