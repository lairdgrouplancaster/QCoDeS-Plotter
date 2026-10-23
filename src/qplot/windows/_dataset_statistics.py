"""Statistics over complete recorded measurements, independent of previews."""

import numpy as np

from qplot.datahandling.parameter_data import numeric_isfinite
from qplot.tools.sample_statistics import (
    finite_mean,
    finite_standard_deviation,
    finite_sum_is_zero,
)

from ._plotWin import plotWidget


def _finite_statistics_samples(values, parameter_name):
    values = np.asarray(values)
    if np.iscomplexobj(values) or (
        values.dtype.kind == "O" and any(np.iscomplexobj(value) for value in values.flat)
    ):
        raise ValueError(
            f"Statistics require real-valued data; {parameter_name!r} contains complex values."
        )
    values = values[numeric_isfinite(values)]
    # Keep the recorded samples for exact centering. Conversion here only
    # checks whether finite extended/object values would display as inf or 0.
    if values.dtype.kind == "O" or (values.dtype.kind == "f" and values.dtype.itemsize > 8):
        try:
            with np.errstate(over="ignore", under="ignore", invalid="ignore"):
                displayed = values.astype(float)
            supported = np.all(np.isfinite(displayed)) and not np.any(
                (displayed == 0) & (values != 0)
            )
        except OverflowError:
            supported = False
        if not supported:
            raise ValueError(
                f"Finite values in {parameter_name!r} are outside the statistics display range."
            )
    return values


def dataset_statistics_text(columns, parameter_name, *, setpoint_names):
    values = _finite_statistics_samples(columns[parameter_name], parameter_name)
    if not values.size:
        return None
    average = finite_mean(values)
    deviation = finite_standard_deviation(values)
    if not np.isfinite(average) or not np.isfinite(deviation) or (
        deviation == 0 and values.min() != values.max()
    ) or (
        average == 0 and not finite_sum_is_zero(values)
    ):
        raise ValueError(
            f"Statistics for {parameter_name!r} are outside the statistics display range."
        )
    format_num = plotWidget.formatNum
    lines = [f"{values.size} points"]
    # Inferred measurements also contain basis columns. Only declared
    # dependencies define the plot axes; basis values need not be numeric.
    axes = list(setpoint_names)
    # For a line, Y is the measured value; for a map, both are setpoints.
    # Match the default plot axes: the inner (second) setpoint is X,
    # and the outer (first) setpoint is Y.
    range_names = axes[::-1] if len(axes) == 2 else (axes + [parameter_name])[:2]
    for axis, name in zip(("X", "Y"), range_names, strict=False):
        samples = values if name == parameter_name else _finite_statistics_samples(columns[name], name)
        if samples.size:
            lines.append(
                f"{axis} range: {format_num(samples.min())} to {format_num(samples.max())}"
            )
    lines.extend((
        f"Average: {format_num(average)}",
        f"Standard deviation: {format_num(deviation)}",
        f"Max: {format_num(float(values.max()))}",
        f"Min: {format_num(float(values.min()))}",
    ))
    return "\n".join(lines)
