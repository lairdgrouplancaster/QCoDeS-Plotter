"""Shared rules for the dimensionality qPlot can display safely."""

from collections.abc import Iterable

MAX_SUPPORTED_PLOT_DIMENSIONS = 2


class UnsupportedPlotDimensionError(ValueError):
    """Raised when a measurement cannot be represented without projection."""


def normalise_axes(axes: Iterable[object] | None) -> tuple[str, ...]:
    """Return non-empty axis names in their declared order."""

    items = () if axes is None else axes
    return tuple(str(axis) for axis in items if str(axis))


def unsupported_plot_message(parameter: object, axes: Iterable[object] | None) -> str:
    """Describe why a measurement is not safe to plot."""

    axis_names = normalise_axes(axes)
    name = str(parameter or "Measurement")
    axis_text = ", ".join(axis_names)
    return (
        f'{name} has {len(axis_names)} independent axes ({axis_text}). '
        "qPlot supports 1D and 2D measurements only; displaying this data "
        "would require an explicit slice or projection."
    )


def ensure_supported_plot_dimensions(
        parameter: object,
        axes: Iterable[object] | None,
        ) -> tuple[str, ...]:
    """Validate that a dependent parameter can be displayed without data loss."""

    axis_names = normalise_axes(axes)
    if len(axis_names) > MAX_SUPPORTED_PLOT_DIMENSIONS:
        raise UnsupportedPlotDimensionError(
            unsupported_plot_message(parameter, axis_names)
            )
    return axis_names
