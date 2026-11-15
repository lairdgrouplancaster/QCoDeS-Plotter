"""Shared fixtures and assertions for sample statistics accuracy."""

from decimal import Decimal, localcontext
from fractions import Fraction

import numpy as np

__all__ = [
    'summary_oracle',
]


def summary_oracle(values):
    exact = []
    for value in values.flat:
        if isinstance(value, np.generic):
            value = value.item()
        # longdouble.item() remains a NumPy scalar. Passing its exact ratio
        # also works before Python 3.14, without narrowing it to a float.
        exact.append(Fraction(*value.as_integer_ratio()))
    mean = sum(exact) / len(exact)
    variance = sum((v - mean) ** 2 for v in exact) / len(exact)
    with localcontext() as context:
        context.prec = 90
        deviation = (Decimal(variance.numerator) / Decimal(variance.denominator)).sqrt()
    return float(mean), float(deviation)
