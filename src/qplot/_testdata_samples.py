"""Bounded numerical sample synthesis without QCoDeS or database writes."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import NamedTuple

import numpy as np

from qplot._testdata_specifications import AxisSpecification

_MINIMUM_AMPLITUDE = 0.5
_MAXIMUM_AMPLITUDE = 1.5
_MINIMUM_FREQUENCY = 0.5
_MAXIMUM_FREQUENCY = 4.0
_SINUSOID_COMPONENT_COUNT = 2


class SinusoidComponent(NamedTuple):
    amplitude: float
    frequencies: tuple[float, ...]
    phase: float


@dataclass(frozen=True)
class SampleChunk:
    """Coordinates in declared sweep order and their measured sample values."""

    coordinates: tuple[tuple[str, np.ndarray], ...]
    values: np.ndarray


def random_sinusoid_components(
    random_generator: np.random.Generator, dimensions: int,
) -> tuple[SinusoidComponent, ...]:
    """Return independently randomised sinusoid parameters for one run."""
    components = []
    for _ in range(_SINUSOID_COMPONENT_COUNT):
        amplitude = random_generator.uniform(_MINIMUM_AMPLITUDE, _MAXIMUM_AMPLITUDE)
        frequencies = tuple(
            random_generator.uniform(_MINIMUM_FREQUENCY, _MAXIMUM_FREQUENCY)
            for _ in range(dimensions)
        )
        phase = random_generator.uniform(0.0, 2.0 * np.pi)
        components.append(SinusoidComponent(amplitude, frequencies, phase))
    return tuple(components)


def _axis_coordinates(
    axis: AxisSpecification, indices: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Interpolate a bounded chunk, including extreme finite endpoints."""
    if axis.points == 1:
        # Programmatic fixtures also use single-point sweeps. The CSV format
        # requires at least two points, but linspace's singleton convention
        # must remain usable by those fixtures.
        return np.full(indices.shape, axis.start), np.zeros(indices.shape)
    normalized = indices / (axis.points - 1)
    if math.isfinite(axis.stop - axis.start):
        coordinates = axis.start + (axis.stop - axis.start) * normalized
    else:
        coordinates = (
            axis.start / 2.0 + (axis.stop / 2.0 - axis.start / 2.0) * normalized
        ) * 2.0
    coordinates[indices == 0] = axis.start
    coordinates[indices == axis.points - 1] = axis.stop
    return coordinates, normalized


def sample_chunk(
    axes: tuple[AxisSpecification, ...],
    components: tuple[SinusoidComponent, ...],
    start: int,
    stop: int,
    *,
    imaginary_components: tuple[SinusoidComponent, ...] | None = None,
) -> SampleChunk:
    """Evaluate one caller-bounded slice in outermost-to-innermost sweep order."""
    remaining = np.arange(
        start, stop, dtype=np.int64
    )
    coordinates = []
    normalized = []
    for axis in reversed(axes):
        indices = remaining % axis.points
        remaining = remaining // axis.points
        values, fractions = _axis_coordinates(axis, indices)
        coordinates.append((axis.name, values))
        normalized.append(fractions)
    normalized.reverse()
    values = np.zeros(
        stop - start, dtype=complex if imaginary_components is not None else float
    )
    for terms, factor in ((components, 1.0), (imaginary_components or (), 1.0j)):
        for amplitude, frequencies, phase in terms:
            argument = sum(
                frequency * fractions
                for frequency, fractions in zip(
                    frequencies, normalized, strict=True
                )
            )
            values += factor * amplitude * np.sin(2.0 * np.pi * argument + phase)
    return SampleChunk(tuple(reversed(coordinates)), values)
