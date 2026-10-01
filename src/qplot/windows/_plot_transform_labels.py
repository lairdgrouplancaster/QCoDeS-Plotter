"""Display quantities for PyQtGraph's native PlotDataItem mappings.

Input parameters describe samples after qPlot operations. These immutable
descriptions never replace measurement or operation metadata.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AxisQuantity:
    label: str
    unit: str

    @classmethod
    def from_parameter(cls, parameter: Any) -> "AxisQuantity":
        return cls(
            str(getattr(parameter, "label", None) or getattr(parameter, "name", "")),
            str(getattr(parameter, "unit", "") or ""),
        )


def _unit_operand(unit: str) -> str:
    return f"({unit})" if any(char in unit for char in "/ *") else unit


def _quotient_unit(numerator: str, denominator: str) -> str:
    if not denominator:
        return numerator
    return f"{_unit_operand(numerator) if numerator else '1'}/{_unit_operand(denominator)}"


def native_axis_quantities(
        x_parameter: Any, y_parameter: Any, options: dict[str, Any],
        ) -> dict[str, AxisQuantity]:
    """Mirror native mapping order, including mappings that reset to input.

    PyQtGraph 0.14 subtracts the mean, then computes abs(rfft(y)/N).
    Derivative mode replaces Y with diff(input Y)/diff(input X), retaining
    the current X. Phase-map mode replaces both coordinates with input Y
    and its derivative. Log ticking still describes these physical quantities.
    """
    input_x = AxisQuantity.from_parameter(x_parameter)
    input_y = AxisQuantity.from_parameter(y_parameter)
    x, y = input_x, input_y
    if options.get("subtractMeanMode"):
        y = AxisQuantity(f"{y.label} - mean({y.label})", y.unit)
    if options.get("fftMode"):
        x = AxisQuantity(f"Frequency of {input_x.label}", _quotient_unit("", input_x.unit))
        fft_input = f"({y.label})" if options.get("subtractMeanMode") else y.label
        y = AxisQuantity(f"FFT magnitude of {fft_input}", y.unit)
    if options.get("derivativeMode") or options.get("phasemapMode"):
        y = AxisQuantity(
            f"d({input_y.label})/d({input_x.label})",
            _quotient_unit(input_y.unit, input_x.unit),
        )
    if options.get("phasemapMode"):
        x = input_y
    return {"x": x, "y": y}
