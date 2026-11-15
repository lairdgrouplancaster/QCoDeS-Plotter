"""Shared fixtures and assertions for sample statistics accuracy."""

from decimal import Decimal, localcontext
from fractions import Fraction

import numpy as np

__all__ = [
    'summary_oracle',
]


def summary_oracle(values):
    exact = [Fraction(v.item() if isinstance(v, np.generic) else v) for v in values.flat]
    mean = sum(exact) / len(exact)
    variance = sum((v - mean) ** 2 for v in exact) / len(exact)
    with localcontext() as context:
        context.prec = 90
        deviation = (Decimal(variance.numerator) / Decimal(variance.denominator)).sqrt()
    return float(mean), float(deviation)
