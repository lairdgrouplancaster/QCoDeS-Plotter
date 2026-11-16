"""Build explicit numerical output for GUI publication tests using fake workers."""

from qplot.tools.plot_processing import PlotProcessingResult
from qplot.tools.plot_result import capture_plot_result


def capture_fake_plot_result(worker):
    """Give a publication-only fake a completed result without running a loader."""
    processed = PlotProcessingResult(
        axis_data=dict(worker.axis_data),
        axis_param=dict(worker.axis_param),
        dataGrid=getattr(worker, "dataGrid", None),
        display_param=getattr(worker, "display_param", None),
        loaded_point_count=getattr(worker, "loaded_point_count", None),
        sampled_heatmap_source=getattr(worker, "sampled_heatmap_source", False),
        aggregated_heatmap_source=getattr(worker, "aggregated_heatmap_source", False),
        heatmap_full_resolution=getattr(worker, "heatmap_full_resolution", False),
        heatmap_downsample_info=getattr(worker, "heatmap_downsample_info", None),
        heatmap_source_grid_shape=getattr(worker, "heatmap_source_grid_shape", None),
        heatmap_source_axis_ranges=getattr(worker, "heatmap_source_axis_ranges", None),
        complex_samples=getattr(worker, "complex_samples", None),
    )
    return capture_plot_result(worker, processed=processed)
