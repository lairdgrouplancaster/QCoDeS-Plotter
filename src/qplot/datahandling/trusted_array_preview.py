"""Bounded QCoDeS array previews using stored coordinates within each record."""

import io
import math
from bisect import bisect_left
from dataclasses import replace

import numpy as np

from .trusted_live_queries import TRUSTED_DERIVED_MAX_SAMPLE_ROWS

MAX_SAMPLES = 131_072


def _decode(value):
    from .trusted_derived_rendering import _UnsupportedNumericData

    if not isinstance(value, bytes):
        return np.asarray([value])
    try:
        stream = io.BytesIO(value)
        version = np.lib.format.read_magic(stream)
        if version not in {(1, 0), (2, 0), (3, 0)}:
            raise ValueError("Unsupported NPY version")
        # Numeric headers are ASCII in both v2 and QCoDeS' v3; the latter
        # changes only the encoding of structured dtype field names, which
        # this renderer rejects below.
        reader = (np.lib.format.read_array_header_1_0 if version == (1, 0)
                  else np.lib.format.read_array_header_2_0)
        shape, fortran, dtype = reader(stream)
        count = math.prod(shape)
        if dtype.kind not in "biuf" or not 0 <= count <= MAX_SAMPLES:
            raise ValueError("Unsupported array dtype or size")
        if len(value) - stream.tell() != count * dtype.itemsize:
            raise ValueError("Invalid array payload size")
        # Validate the header and exact payload length before allocating. Never
        # load pickled object arrays or allocate from an unchecked NPY shape.
        return np.frombuffer(value, dtype=dtype, offset=stream.tell()).reshape(
            shape, order="F" if fortran else "C",
        ).ravel()
    except (ValueError, KeyError, EOFError, OverflowError) as error:
        raise _UnsupportedNumericData("The array sample cannot be decoded safely.") from error


def render_array_preview(observation, dependencies, dependent, width, height, check):
    from .trusted_derived_rendering import (
        _MISSING_CELL_RGBA,
        _finite_number,
        _pixel,
        _render_1d,
        _UnsupportedNumericData,
        _viridis_rgba,
    )

    indexes = [observation.sample_columns.index(name)
               for name in (*dependencies, dependent)]
    points = []
    expanded_count = 0
    for row in observation.sample_rows:
        check()
        if any(row[index] is None for index in indexes):
            continue
        columns = [_decode(row[index]) for index in indexes]
        size = max(column.size for column in columns)
        if any(column.size != (size if isinstance(row[source], bytes) else 1)
               for source, column in zip(indexes, columns, strict=True)):
            raise _UnsupportedNumericData("Array measurement and setpoint sizes differ.")
        expanded_count += size
        if expanded_count > MAX_SAMPLES:
            raise _UnsupportedNumericData("The array preview exceeds its sample budget.")
        for index in range(size):
            if index % 128 == 0:
                check()
            values = tuple(_finite_number(column[0 if column.size == 1 else index].item())
                           for column in columns)
            if all(value is not None for value in values[:-1]):
                points.append(values)
    if not points:
        raise _UnsupportedNumericData("No bounded numeric array samples are available.")
    if len(dependencies) == 1:
        # Keep both endpoints while limiting the ordinary line renderer's input.
        selected = np.linspace(0, len(points) - 1,
                               min(len(points), TRUSTED_DERIVED_MAX_SAMPLE_ROWS), dtype=int)
        rows = tuple((i + 1, *points[index]) for i, index in enumerate(selected))
        sampled = replace(observation, sample_columns=("id", *dependencies, dependent),
                          sample_rows=rows, validated_2d_layouts=())
        return _render_1d(sampled, dependencies[0], dependent, width, height, check)

    # Build cells from actual paired coordinates, never from planned shapes or
    # physical row IDs (one QCoDeS row can contain an entire oscilloscope trace).
    vertical = sorted({point[0] for point in points})
    horizontal = sorted({point[1] for point in points})
    if len(vertical) * len(horizontal) > MAX_SAMPLES:
        raise _UnsupportedNumericData("The array preview grid exceeds its cell budget.")
    cells = {}
    for y, x, z in points:
        if z is not None:
            cells.setdefault((y, x), []).append(z)
    if not cells:
        raise _UnsupportedNumericData("No finite array measurements are available.")
    values = {}
    for key, samples in cells.items():
        check()
        scale = max(abs(value) for value in samples)
        values[key] = (math.fsum(value / scale / len(samples) for value in samples)
                       * scale if scale else 0.)
    low, high = min(values.values()), max(values.values())

    def pixel_axis(axis, count):
        span = axis[-1] - axis[0]
        if not math.isfinite(span):
            raise _UnsupportedNumericData("Array coordinates exceed the finite range.")
        normalized = [(value - axis[0]) / (span or 1.) for value in axis]
        edges = [(left + right) / 2
                 for left, right in zip(normalized, normalized[1:], strict=False)]
        return [axis[bisect_left(edges, index / max(1, count - 1))]
                for index in range(count)]

    xs, ys = pixel_axis(horizontal, width), pixel_axis(vertical, height)[::-1]
    rgba = bytearray(width * height * 4)
    for py, y in enumerate(ys):
        check()
        for px, x in enumerate(xs):
            value = values.get((y, x))
            color = _MISSING_CELL_RGBA if value is None else _viridis_rgba(value, low, high)
            _pixel(rgba, width, px, py, color)
    return rgba, sum(len(samples) for samples in cells.values())
