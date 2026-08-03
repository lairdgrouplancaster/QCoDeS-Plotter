"""
Operation metadata for plot refresh processing.

The functions live in ``plot_tools``; this module records which operations are
available for each plot surface and what kind of user input they need.
"""

from collections.abc import Callable
from dataclasses import dataclass

from qplot.tools.plot_tools import (
    differentiate,
    fill_heatmap,
    pass_filter,
    subtract_mean,
)


@dataclass(frozen=True)
class OperationSpec:
    name: str
    func: Callable
    input_type: object
    default: object = ""
    derivative_axis: str | None = None


@dataclass(frozen=True)
class OperationCall:
    """A configured operation and the metadata needed after it succeeds."""

    name: str
    func: Callable
    derivative_axis: str | None = None
    cooperative: bool = False

    def __call__(self, data):
        return self.func(data)

    def execute(self, data, cancelled_callback):
        """Run qPlot operations cooperatively while retaining one-arg calls."""

        if self.cooperative:
            return self.func(data, cancelled_callback=cancelled_callback)
        return self.func(data)


class OperationValidationError(ValueError):
    """Raised when an enabled operation has missing or invalid input."""


class OperationExecutionError(RuntimeError):
    """Raised when an operation pipeline cannot be completed atomically."""


COMMON_OPERATION_SPECS = (
    OperationSpec(
        "Limit Maximum",
        lambda limit, data, cancelled_callback=None: pass_filter(
            "low", limit, data, cancelled_callback=cancelled_callback
            ),
        float,
        ),
    OperationSpec(
        "Limit Minimum",
        lambda limit, data, cancelled_callback=None: pass_filter(
            "high", limit, data, cancelled_callback=cancelled_callback
            ),
        float,
        ),
    )

PLOT_OPERATION_SPECS = {
    "plot1d": (
        OperationSpec(
            "dy/dx",
            lambda data, cancelled_callback=None: differentiate(
                "x", data, cancelled_callback=cancelled_callback
                ),
            None,
            derivative_axis="x",
            ),
        ),
    "plot2d": (
        OperationSpec(
            "Subtract Row Mean",
            lambda data, cancelled_callback=None: subtract_mean(
                "x", data, cancelled_callback=cancelled_callback
                ),
            None,
            ),
        OperationSpec(
            "Subtract Column Mean",
            lambda data, cancelled_callback=None: subtract_mean(
                "y", data, cancelled_callback=cancelled_callback
                ),
            None,
            ),
        OperationSpec(
            "dz/dx",
            lambda data, cancelled_callback=None: differentiate(
                "x", data, cancelled_callback=cancelled_callback
                ),
            None,
            derivative_axis="x",
            ),
        OperationSpec(
            "dz/dy",
            lambda data, cancelled_callback=None: differentiate(
                "y", data, cancelled_callback=cancelled_callback
                ),
            None,
            derivative_axis="y",
            ),
        OperationSpec(
            "Fill Below",
            lambda value, data, cancelled_callback=None: fill_heatmap(
                "below",
                data,
                max_depth=value,
                cancelled_callback=cancelled_callback,
                ),
            int,
            10,
            ),
        OperationSpec(
            "Fill Right",
            lambda value, data, cancelled_callback=None: fill_heatmap(
                "right",
                data,
                max_depth=value,
                cancelled_callback=cancelled_callback,
                ),
            int,
            10,
            ),
        ),
    "sweeper": (
        OperationSpec(
            "Subtract Cut Mean",
            lambda data, cancelled_callback=None: subtract_mean(
                "x", data, cancelled_callback=cancelled_callback
                ),
            None,
            ),
        OperationSpec(
            "Subtract Fixed Mean",
            lambda data, cancelled_callback=None: subtract_mean(
                "y", data, cancelled_callback=cancelled_callback
                ),
            None,
            ),
        OperationSpec(
            "Differentiate Cut",
            lambda data, cancelled_callback=None: differentiate(
                "x", data, cancelled_callback=cancelled_callback
                ),
            None,
            derivative_axis="x",
            ),
        OperationSpec(
            "Differentiate Fixed",
            lambda data, cancelled_callback=None: differentiate(
                "y", data, cancelled_callback=cancelled_callback
                ),
            None,
            derivative_axis="y",
            ),
        ),
    }


def operation_specs_for(plot_type):
    """
    Return common and plot-specific operations for a plot widget class name.

    """
    return COMMON_OPERATION_SPECS + PLOT_OPERATION_SPECS[plot_type]
