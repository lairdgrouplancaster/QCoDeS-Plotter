"""Statistics over complete recorded measurements, independent of previews."""

import numpy as np

from qplot.datahandling.parameter_data import numeric_isfinite
from qplot.tools.sample_statistics import finite_mean, finite_standard_deviation

from ._plotWin import plotWidget


def dataset_statistics_text(columns, parameter_name, *, setpoint_names):
    values = np.asarray(columns[parameter_name])
    values = values[numeric_isfinite(values)]
    if not values.size:
        return None
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
        samples = np.asarray(columns[name])
        samples = samples[numeric_isfinite(samples)]
        if samples.size:
            lines.append(
                f"{axis} range: {format_num(samples.min())} to {format_num(samples.max())}"
            )
    lines.extend((
        f"Average: {format_num(finite_mean(values))}",
        f"Standard deviation: {format_num(finite_standard_deviation(values))}",
        f"Max: {format_num(float(values.max()))}",
        f"Min: {format_num(float(values.min()))}",
    ))
    return "\n".join(lines)
