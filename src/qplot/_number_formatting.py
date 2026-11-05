"""Numeric presentation shared by statistics, plots, and cursor readouts.

These functions depend only on the supplied number and precision, so callers
do not need a plot window or configuration object to format a measurement.
"""

from math import floor, isfinite, isnan, log10


def format_number(value: float, significant_figures: int = 3) -> str:
    """Use compact fixed or scientific notation for a measurement summary."""
    if not isfinite(value):
        if isnan(value):
            return "nan"
        return "-inf" if value < 0 else "inf"

    try:
        exponent = floor(log10(abs(value)))
    except ValueError:
        return f"{0:.{significant_figures}f}"

    precision = max(significant_figures - 1, 0)
    if exponent >= significant_figures or exponent < 0:
        return f"{value:.{precision}e}"

    formatted = f"{value:.{max(significant_figures - exponent - 1, 0)}f}"
    rounded_exponent = floor(log10(abs(float(formatted))))
    if rounded_exponent != exponent:
        return f"{value:.{precision}e}"
    return formatted


def cursor_field_width(significant_figures: int) -> int:
    """Reserve a signed mantissa plus ``e`` and a signed two-digit exponent.

    A three-digit exponent may grow beyond this minimum field width.
    """
    mantissa_width = significant_figures + 2 if significant_figures > 1 else 2
    return mantissa_width + 4


def format_cursor_number(value: float, significant_figures: int) -> str:
    """Keep cursor coordinates aligned as their signs and exponents change."""
    width = cursor_field_width(significant_figures)
    if not isfinite(value):
        return f"{value:>{width}}"
    mantissa, exponent = f"{value:.{significant_figures - 1}e}".split("e")
    return f"{mantissa:>{width - 4}}e{int(exponent):+03d}"
