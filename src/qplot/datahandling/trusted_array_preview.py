"""Bounded QCoDeS array previews using stored coordinates within each record."""

import io
import sys
from bisect import bisect_left
from fractions import Fraction

import numpy as np

from qplot.datahandling.complex_data import project_complex
from qplot.tools.sample_statistics import finite_mean

from ._numeric_arrays import read_npy_header
from ._trusted_preview_raster import (
    _MISSING_CELL_RGBA,
    _finite_number,
    _normalised_position,
    _pixel,
    _render_line_points,
    _UnsupportedNumericData,
    _viridis_rgba,
)
from .trusted_query_types import TRUSTED_DERIVED_MAX_SAMPLE_ROWS

MAX_SAMPLES = 131_072
# Exact extended ratios can be much larger than their stored NPY samples.
# Bound retained Fraction/integer storage before adding each decoded cell.
MAX_EXACT_SAMPLE_BYTES = 64 * 1024 * 1024


def _decode(value, *, allow_complex=False):
    if not isinstance(value, bytes):
        return np.asarray([value])
    try:
        stream = io.BytesIO(value)
        header = read_npy_header(stream)
        if header.dtype.kind not in ("biufc" if allow_complex else "biuf") or not 0 <= header.size <= MAX_SAMPLES:
            raise ValueError("Unsupported array dtype or size")
        if header.total_bytes != len(value):
            raise ValueError("Invalid array payload size")
        # Validate the header and exact payload length before allocating. Never
        # load pickled object arrays or allocate from an unchecked NPY shape.
        return np.frombuffer(value, dtype=header.dtype, offset=header.offset).reshape(
            header.shape, order="F" if header.fortran_order else "C",
        ).ravel()
    except (ValueError, KeyError, EOFError, OverflowError) as error:
        raise _UnsupportedNumericData("The array sample cannot be decoded safely.") from error


def render_array_preview(observation, dependencies, dependent, width, height, check,
                         *, complex_mode="magnitude"):
    return render_array_samples(
        observation.sample_columns, observation.sample_rows, dependencies, dependent,
        width, height, check, complex_mode=complex_mode,
    )


def render_array_samples(sample_columns, sample_rows, dependencies, dependent,
                         width, height, check, *, complex_mode="magnitude"):
    """Render bounded primitive/NPY samples without owning a database handle."""
    indexes = [sample_columns.index(name)
               for name in (*dependencies, dependent)]
    points = []
    expanded_count = 0
    exact_sample_bytes = 0
    complex_measurement = False

    def preview_number(value):
        nonlocal exact_sample_bytes
        if isinstance(value, np.floating):
            if not np.isfinite(value):
                return None
            narrowed = float(value)
            if value == narrowed:
                return narrowed
            # longdouble.item() remains a NumPy scalar on platforms with
            # extended precision. Preserve its ratio until normalization;
            # narrowing can merge cells or lose a finite exponent range.
            exact = Fraction(*value.as_integer_ratio())
            exact_sample_bytes += sum(map(sys.getsizeof, (
                exact, exact.numerator, exact.denominator,
            )))
            if exact_sample_bytes > MAX_EXACT_SAMPLE_BYTES:
                raise _UnsupportedNumericData("The array preview exceeds its precision storage budget.")
            return exact
        return int(value) if isinstance(value, (bool, int)) else _finite_number(value)

    for row in sample_rows:
        check()
        if any(row[index] is None for index in indexes):
            continue
        columns = [_decode(row[index], allow_complex=position == len(indexes) - 1)
                   for position, index in enumerate(indexes)]
        # QCoDeS expands scalar storage and singleton arrays to the largest
        # acquired array. Scalar storage does not make an empty array record
        # into one sample. Index singleton cells below without allocating the
        # repeated arrays; the sample budget applies to their expanded extent.
        array_sizes = [column.size for source, column in zip(indexes, columns, strict=True)
                       if isinstance(row[source], bytes)]
        # The selected output is first in QCoDeS' tree. A scalar output is
        # initially expanded to the first array dependency. An empty output
        # contributes no samples even when its setpoints are singleton arrays.
        first_size = (columns[-1].size if isinstance(row[indexes[-1]], bytes)
                      else array_sizes[0] if array_sizes else 1)
        size = 0 if first_size == 0 else max(array_sizes, default=1)
        if any(column.size not in (1, size) for column in columns):
            raise _UnsupportedNumericData("Array measurement and setpoint sizes differ.")
        if np.iscomplexobj(columns[-1]):
            complex_measurement = True
            values = columns[-1]
            if len(dependencies) == 1 and complex_mode == "imag_vs_real":
                columns[0] = project_complex(values, "real")
                columns[-1] = project_complex(values, "imag")
            else:
                columns[-1] = project_complex(values, complex_mode)
        expanded_count += size
        if expanded_count > MAX_SAMPLES:
            raise _UnsupportedNumericData("The array preview exceeds its sample budget.")
        for index in range(size):
            if index % 128 == 0:
                check()
            # NPY Boolean arrays are numeric acquired samples. Preserve
            # integers too: rounding before grouping can merge adjacent
            # recorded coordinates above float64's integer precision.
            record_samples = (column[0 if column.size == 1 else index].item()
                              for column in columns)
            values = tuple(preview_number(value) for value in record_samples)
            if all(value is not None for value in values[:-1]):
                points.append(values)
    if not points:
        raise _UnsupportedNumericData("No bounded numeric array samples are available.")
    if len(dependencies) == 1:
        # Keep both endpoints while limiting the ordinary line renderer's input.
        selected = np.linspace(0, len(points) - 1,
                               min(len(points), TRUSTED_DERIVED_MAX_SAMPLE_ROWS), dtype=int)
        pairs = [points[index] for index in selected if all(value is not None for value in points[index])]
        rgba, count = _render_line_points(pairs, width, height, check)
        return rgba, count, complex_mode if complex_measurement else None

    # Build cells from actual paired coordinates, never from planned shapes or
    # physical row IDs (one QCoDeS row can contain an entire oscilloscope trace).
    vertical = sorted({point[0] for point in points})
    horizontal = sorted({point[1] for point in points})
    if len(vertical) * len(horizontal) > MAX_SAMPLES:
        raise _UnsupportedNumericData("The array preview grid exceeds its cell budget.")
    cells: dict[tuple, list] = {}
    for y, x, z in points:
        if z is not None:
            cells.setdefault((y, x), []).append(z)
    if not cells:
        raise _UnsupportedNumericData("No finite array measurements are available.")
    values = {}
    for key, samples in cells.items():
        check()
        if len(samples) == 1:
            values[key] = samples[0]
            continue
        exact_samples = any(isinstance(value, (int, Fraction)) for value in samples)
        if exact_samples:
            # A float mean would erase the small differences between nearby
            # integer or extended cells. Keep their exact offset until color
            # normalization.
            total = Fraction()
            for index, value in enumerate(samples):
                if index % 1024 == 0:
                    check()
                total += Fraction(value)
            values[key] = total / len(samples)
            continue
        # Scaling every source cell by the largest loses small cancellation
        # residuals. The common finite mean retains the original samples.
        values[key] = finite_mean(np.array(samples), check_cancelled=check)
    low, high = min(values.values()), max(values.values())

    def pixel_axis(axis, count):
        normalized = [_normalised_position(value, axis[0], axis[-1]) for value in axis]
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
    return rgba, sum(len(samples) for samples in cells.values()), complex_mode if complex_measurement else None
