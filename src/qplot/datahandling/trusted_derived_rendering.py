"""Deterministic, bounded, Qt-independent Stage 5C derived rendering."""

from __future__ import annotations

import binascii
import math
import struct
import zlib
from bisect import bisect_left
from collections.abc import Callable
from typing import Any, TypeAlias, cast

from qplot.datahandling.trusted_live_queries import (
    Trusted2DGridLayout,
    TrustedDerivedSourceObservation,
)
from qplot.datahandling.trusted_work_scheduler import (
    RenderingOptions,
    TrustedWorkKind,
)

TRUSTED_DERIVED_RENDERER_VERSION = "trusted-derived-renderer-v3"
TRUSTED_DERIVED_MAX_IMAGES = 8
TRUSTED_DERIVED_MAX_IMAGE_WIDTH = 2_048
TRUSTED_DERIVED_MAX_IMAGE_HEIGHT = 2_048
TRUSTED_DERIVED_MAX_DECODED_IMAGE_BYTES = 16 * 1024 * 1024
TRUSTED_DERIVED_MAX_ENCODED_IMAGE_BYTES = 8 * 1024 * 1024
TRUSTED_DERIVED_MAX_RETAINED_PAYLOAD_BYTES = 16 * 1024 * 1024
TRUSTED_DERIVED_MAX_PAYLOAD_DEPTH = 32
TRUSTED_DERIVED_MAX_PAYLOAD_NODES = 131_072
TRUSTED_DERIVED_MAX_PAYLOAD_ITEMS = 65_536
TRUSTED_DERIVED_MAX_TEXT_BYTES = 1024 * 1024

DerivedScalar: TypeAlias = None | bool | int | float | str | bytes
DerivedValue: TypeAlias = DerivedScalar | tuple["DerivedValue", ...]
DerivedPayload: TypeAlias = dict[str, DerivedValue]
CancelCheck: TypeAlias = Callable[[], None]
_DEFAULT_RENDERING_OPTIONS = RenderingOptions()
_VIRIDIS_RGB = bytes.fromhex(
    "44015444025544035745055845065a45085b46095c460b5e460c5f460e61470f62471163471265471466471567471669"
    "47186a48196b481a6c481c6e481d6f481e70482071482172482273482374472575472676472777472878472a79472b7a"
    "472c7b462d7c462f7c46307d46317e45327f45347f453580453681443781443982433a83433b83433c84423d84423e85"
    "4240854141864142864043874044873f45873f47883e48883e49893d4a893d4b893d4c893c4d8a3c4e8a3b508a3b518a"
    "3a528b3a538b39548b39558b38568b38578c37588c37598c365a8c365b8c355c8c355d8c345e8d345f8d33608d33618d"
    "32628d32638d31648d31658d31668d30678d30688d2f698d2f6a8d2e6b8e2e6c8e2e6d8e2d6e8e2d6f8e2c708e2c718e"
    "2c728e2b738e2b748e2a758e2a768e2a778e29788e29798e287a8e287a8e287b8e277c8e277d8e277e8e267f8e26808e"
    "26818e25828e25838d24848d24858d24868d23878d23888d23898d22898d228a8d228b8d218c8d218d8c218e8c208f8c"
    "20908c20918c1f928c1f938b1f948b1f958b1f968b1e978a1e988a1e998a1e998a1e9a891e9b891e9c891e9d881e9e88"
    "1e9f881ea0871fa1871fa2861fa38620a48520a58521a68521a78422a78423a88323a98224aa8225ab8126ac8127ad80"
    "28ae7f29af7f2ab07e2bb17d2cb17d2eb27c2fb37b30b47a32b57a33b67935b77836b87738b97639b9763bba753dbb74"
    "3ebc7340bd7242be7144be7045bf6f47c06e49c16d4bc26c4dc26b4fc36951c46853c56755c66657c66559c7645bc862"
    "5ec96160c96062ca5f64cb5d67cc5c69cc5b6bcd596dce5870ce5672cf5574d05477d05279d1517cd24f7ed24e81d34c"
    "83d34b86d44988d5478bd5468dd64490d64392d74195d73f97d83e9ad83c9dd93a9fd938a2da37a5da35a7db33aadb32"
    "addc30afdc2eb2dd2cb5dd2bb7dd29bade27bdde26bfdf24c2df22c5df21c7e01fcae01ecde01dcfe11cd2e11bd4e11a"
    "d7e219dae218dce218dfe318e1e318e4e318e7e419e9e419ece41aeee51bf1e51cf3e51ef6e61ff8e621fae622fde724"
)
_MISSING_CELL_RGBA = (230, 230, 230, 255)


class TrustedDerivedRenderingError(RuntimeError):
    """A bounded source could not be converted into a safe payload."""


class _UnsupportedNumericData(TrustedDerivedRenderingError):
    pass


def _option_integer(
    options: RenderingOptions,
    name: str,
    default: int,
    maximum: int,
) -> int:
    value = dict(options.values).get(name, default)
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"Rendering option {name!r} must be from 1 through {maximum}.")
    return value


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    converted = float(value)
    return converted if math.isfinite(converted) else None


def _source_identity(observation: TrustedDerivedSourceObservation) -> DerivedValue:
    return (
        ("run_id", observation.run_id),
        ("run_guid", observation.run_guid),
        ("result_table", observation.result_table_name),
        ("result_watermark", observation.result_watermark),
        ("helper_incarnation", observation.helper_incarnation),
        ("data_version", observation.data_version),
        ("schema_sha256", observation.result_schema_sha256),
    )


def _base_payload(
    observation: TrustedDerivedSourceObservation,
    kind: TrustedWorkKind,
    *,
    status: str,
    description: str,
) -> DerivedPayload:
    return {
        "format": "qplot-trusted-derived-payload-v1",
        "kind": kind.name.lower(),
        "status": status,
        "description": description,
        "source": _source_identity(observation),
        "images": (),
    }


def _metadata_payload(
    observation: TrustedDerivedSourceObservation,
) -> DerivedPayload:
    payload = _base_payload(
        observation,
        TrustedWorkKind.METADATA,
        status="ok",
        description="Bounded trusted run metadata.",
    )
    payload["metadata"] = (
        ("run_id", observation.run_id),
        ("guid", observation.run_guid),
        ("result_table_name", observation.result_table_name),
        ("result_count", observation.result_watermark),
        ("planned_shape", cast(DerivedValue, observation.planned_shape)),
        ("sampled_rows", len(observation.sample_rows)),
        ("sampled_columns", len(observation.sample_columns)),
        (
            "run_fields",
            cast(
                DerivedValue,
                observation.run_fields
                or (
                    ("run_id", observation.run_id),
                    ("guid", observation.run_guid),
                    ("result_table_name", observation.result_table_name),
                    ("result_count", observation.result_watermark),
                ),
            ),
        ),
        (
            "parameters",
            tuple(
                (
                    parameter.name,
                    parameter.label,
                    parameter.unit,
                    parameter.depends_on,
                    parameter.paramtype,
                )
                for parameter in observation.parameters
            ),
        ),
        (
            "setpoint_summaries",
            tuple(
                (summary.name, summary.first, summary.last, summary.steps)
                for summary in observation.setpoint_summaries
            ),
        ),
    )
    return payload


def render_trusted_derived_payload(
    observation: TrustedDerivedSourceObservation,
    kind: TrustedWorkKind,
    options: RenderingOptions = _DEFAULT_RENDERING_OPTIONS,
    *,
    cancel_check: CancelCheck = lambda: None,
) -> DerivedPayload:
    """Render one bounded observation into versioned primitive/PNG payloads."""

    if not isinstance(observation, TrustedDerivedSourceObservation):
        raise TypeError("observation must be TrustedDerivedSourceObservation.")
    if not isinstance(kind, TrustedWorkKind):
        raise TypeError("kind must be TrustedWorkKind.")
    if not isinstance(options, RenderingOptions):
        raise TypeError("options must be RenderingOptions.")
    cancel_check()
    if kind is TrustedWorkKind.METADATA:
        return _metadata_payload(observation)
    if observation.unsupported_reason:
        return _base_payload(
            observation,
            kind,
            status="unsupported",
            description=observation.unsupported_reason,
        )
    if observation.result_watermark == 0 or not observation.sample_rows:
        return _base_payload(
            observation,
            kind,
            status="empty",
            description="The captured result prefix is empty.",
        )

    default_width, default_height = (
        (160, 96) if kind is TrustedWorkKind.THUMBNAIL else (800, 500)
    )
    width = _option_integer(
        options, "width", default_width, TRUSTED_DERIVED_MAX_IMAGE_WIDTH
    )
    height = _option_integer(
        options, "height", default_height, TRUSTED_DERIVED_MAX_IMAGE_HEIGHT
    )
    if width * height * 4 > TRUSTED_DERIVED_MAX_DECODED_IMAGE_BYTES:
        raise TrustedDerivedRenderingError("The decoded image budget was exceeded.")

    column_indexes = {
        name: index for index, name in enumerate(observation.sample_columns)
    }
    parameter_by_name = {
        parameter.name: parameter for parameter in observation.parameters
    }
    dependents = tuple(
        name for name in observation.dependent_parameters if name in column_indexes
    )
    if not dependents:
        dependents = tuple(observation.sample_columns[-1:])
    dependents = dependents[:TRUSTED_DERIVED_MAX_IMAGES]
    images: list[DerivedValue] = []
    unavailable_descriptions: list[str] = []
    encoded_total = 0
    for dependent_index, dependent in enumerate(dependents):
        cancel_check()
        parameter = parameter_by_name.get(dependent)
        dependencies = (
            tuple(name for name in parameter.depends_on if name in column_indexes)
            if parameter is not None
            else ()
        )
        if not dependencies:
            candidates = tuple(
                name for name in observation.sample_columns[1:] if name != dependent
            )
            dependencies = candidates[:1]
        try:
            if len(dependencies) == 1:
                rgba, points = _render_1d(
                    observation,
                    dependencies[0],
                    dependent,
                    width,
                    height,
                    cancel_check,
                )
                dimensionality = 1
            elif len(dependencies) == 2:
                layout = next(
                    (
                        candidate
                        for candidate in observation.validated_2d_layouts
                        if candidate.dependent == dependent
                        and candidate.dependencies == dependencies
                    ),
                    None,
                )
                if layout is None:
                    raise _UnsupportedNumericData(
                        "A validated rectangular 2D layout is not yet available."
                    )
                rgba, points = _render_2d(
                    observation,
                    layout,
                    width,
                    height,
                    cancel_check,
                )
                dimensionality = 2
            else:
                continue
        except _UnsupportedNumericData as error:
            unavailable_descriptions.append(str(error))
            continue
        cancel_check()
        if points == 0:
            continue
        encoded = _encode_png_rgba(width, height, rgba, cancel_check)
        encoded_total += len(encoded)
        if encoded_total > TRUSTED_DERIVED_MAX_ENCODED_IMAGE_BYTES:
            raise TrustedDerivedRenderingError("The encoded image budget was exceeded.")
        images.append(
            (
                ("encoding", "png"),
                ("width", width),
                ("height", height),
                ("dependent", dependent),
                ("dimensions", dimensionality),
                ("sampled_points", points),
                ("bytes", encoded),
            )
        )
        if dependent_index + 1 >= TRUSTED_DERIVED_MAX_IMAGES:
            break
    if not images:
        return _base_payload(
            observation,
            kind,
            status="unsupported",
            description=(
                unavailable_descriptions[0]
                if unavailable_descriptions
                else "No bounded numeric 1D or 2D dependent data was available."
            ),
        )
    payload = _base_payload(
        observation,
        kind,
        status="ok",
        description=(
            "Bounded trusted result-domain rendering; some dependents remain "
            "unavailable."
            if unavailable_descriptions
            else "Bounded trusted result-domain rendering."
        ),
    )
    payload["images"] = tuple(images)
    return payload


def _render_1d(
    observation: TrustedDerivedSourceObservation,
    x_name: str,
    y_name: str,
    width: int,
    height: int,
    cancel_check: CancelCheck,
) -> tuple[bytearray, int]:
    indexes = {name: index for index, name in enumerate(observation.sample_columns)}
    points: list[tuple[float, float]] = []
    for index, row in enumerate(observation.sample_rows):
        if index % 128 == 0:
            cancel_check()
        if len(row) != len(observation.sample_columns):
            raise _UnsupportedNumericData(
                "The bounded numeric sample has inconsistent vector lengths."
            )
        x = _finite_number(row[indexes[x_name]])
        y = _finite_number(row[indexes[y_name]])
        if x is not None and y is not None:
            points.append((x, y))
    rgba = bytearray(b"\xff" * (width * height * 4))
    if not points:
        return rgba, 0
    x_values = tuple(point[0] for point in points)
    y_values = tuple(point[1] for point in points)
    x_min, x_max = min(x_values), max(x_values)
    y_min, y_max = min(y_values), max(y_values)
    x_span = x_max - x_min
    y_span = y_max - y_min
    if not math.isfinite(x_span) or not math.isfinite(y_span):
        raise _UnsupportedNumericData(
            "The bounded numeric sample exceeds the supported finite range."
        )
    x_span = x_span or 1.0
    y_span = y_span or 1.0
    margin = max(2, min(width, height) // 20)
    prior: tuple[int, int] | None = None
    for index, (x, y) in enumerate(points):
        if index % 128 == 0:
            cancel_check()
        raw_px = (x - x_min) * (width - 1 - 2 * margin) / x_span
        raw_py = (y - y_min) * (height - 1 - 2 * margin) / y_span
        if not math.isfinite(raw_px) or not math.isfinite(raw_py):
            raise _UnsupportedNumericData(
                "The bounded numeric sample exceeds the supported finite range."
            )
        px = margin + round(raw_px)
        py = height - 1 - margin - round(raw_py)
        if prior is not None:
            _line(rgba, width, height, prior[0], prior[1], px, py, (24, 92, 170, 255))
        prior = (px, py)
    if len(points) == 1 and prior is not None:
        _pixel(
            rgba,
            width,
            min(width - 1, max(0, prior[0])),
            min(height - 1, max(0, prior[1])),
            (24, 92, 170, 255),
        )
    return rgba, len(points)


def _render_2d(
    observation: TrustedDerivedSourceObservation,
    layout: Trusted2DGridLayout,
    width: int,
    height: int,
    cancel_check: CancelCheck,
) -> tuple[bytearray, int]:
    indexes = {name: index for index, name in enumerate(observation.sample_columns)}
    fast_name = layout.dependencies[layout.fast_axis_index]
    slow_name = layout.dependencies[1 - layout.fast_axis_index]
    z_name = layout.dependent
    if any(name not in indexes for name in (slow_name, fast_name, z_name)):
        raise _UnsupportedNumericData(
            "The validated 2D layout is absent from the bounded numeric sample."
        )
    slow_count = layout.shape[1 - layout.fast_axis_index]
    fast_count = layout.shape[layout.fast_axis_index]
    sampled_slow = layout.sample_slow_indexes
    sampled_fast = layout.sample_fast_indexes
    slow_bins = {value: index for index, value in enumerate(sampled_slow)}
    fast_bins = {value: index for index, value in enumerate(sampled_fast)}
    if layout.fast_axis_index == 0:
        sampled_vertical = sampled_fast
        sampled_horizontal = sampled_slow
        vertical_count = fast_count
        horizontal_count = slow_count
    else:
        sampled_vertical = sampled_slow
        sampled_horizontal = sampled_fast
        vertical_count = slow_count
        horizontal_count = fast_count
    grid_width = len(sampled_horizontal)
    grid_size = len(sampled_vertical) * grid_width
    grid_sum = [0.0] * grid_size
    grid_count = [0] * grid_size
    points = 0
    for index, row in enumerate(observation.sample_rows):
        if index % 128 == 0:
            cancel_check()
        if len(row) != len(observation.sample_columns):
            raise _UnsupportedNumericData(
                "The bounded numeric sample has inconsistent vector lengths."
            )
        row_id = row[0]
        if type(row_id) is not int:
            raise _UnsupportedNumericData(
                "The bounded grid sample has an invalid result id."
            )
        grid_index = _layout_grid_index(
            layout,
            row_id,
            slow_count=slow_count,
            fast_count=fast_count,
        )
        if grid_index is None:
            continue
        slow = _finite_number(row[indexes[slow_name]])
        fast = _finite_number(row[indexes[fast_name]])
        z = _finite_number(row[indexes[z_name]])
        if slow is None or fast is None:
            raise _UnsupportedNumericData(
                "The validated grid sample has a non-numeric dependency."
            )
        if z is None:
            continue
        slow_index, fast_index = grid_index
        slow_bin = slow_bins.get(slow_index)
        if slow_bin is None:
            slow_bin = _scaled_grid_index(
                slow_index,
                slow_count,
                len(sampled_slow),
            )
        fast_bin = fast_bins.get(fast_index)
        if fast_bin is None:
            fast_bin = _scaled_grid_index(
                fast_index,
                fast_count,
                len(sampled_fast),
            )
        scientific_slow_bin = (
            len(sampled_slow) - 1 - slow_bin if layout.slow_reversed else slow_bin
        )
        row_fast_reversed = layout.fast_reversed ^ (
            layout.serpentine and bool(slow_index % 2)
        )
        scientific_fast_bin = (
            len(sampled_fast) - 1 - fast_bin if row_fast_reversed else fast_bin
        )
        if layout.fast_axis_index == 0:
            vertical_bin = scientific_fast_bin
            horizontal_bin = scientific_slow_bin
        else:
            vertical_bin = scientific_slow_bin
            horizontal_bin = scientific_fast_bin
        display_row = len(sampled_vertical) - 1 - vertical_bin
        target = display_row * grid_width + horizontal_bin
        combined = grid_sum[target] + z
        if not math.isfinite(combined):
            raise _UnsupportedNumericData(
                "The bounded numeric sample exceeds the supported finite range."
            )
        grid_sum[target] = combined
        grid_count[target] += 1
        points += 1
    rgba = bytearray(width * height * 4)
    if points == 0:
        for offset in range(0, len(rgba), 4):
            rgba[offset : offset + 4] = bytes(_MISSING_CELL_RGBA)
        return rgba, 0
    grid = tuple(
        grid_sum[index] / count if count else None
        for index, count in enumerate(grid_count)
    )
    finite = tuple(value for value in grid if value is not None)
    z_min, z_max = min(finite), max(finite)
    z_span = z_max - z_min
    if not math.isfinite(z_span):
        raise _UnsupportedNumericData(
            "The bounded numeric sample exceeds the supported finite range."
        )
    source_rows = _nearest_sample_bins(
        height,
        vertical_count,
        sampled_vertical,
    )
    source_columns = _nearest_sample_bins(
        width,
        horizontal_count,
        sampled_horizontal,
    )
    for py in range(height):
        if py % 32 == 0:
            cancel_check()
        source_row = source_rows[py]
        for px, source_column in enumerate(source_columns):
            value = grid[source_row * grid_width + source_column]
            color = (
                _MISSING_CELL_RGBA
                if value is None
                else _viridis_rgba(value, z_min, z_max)
            )
            _pixel(rgba, width, px, py, color)
    return rgba, points


def _layout_grid_index(
    layout: Trusted2DGridLayout,
    row_id: int,
    *,
    slow_count: int,
    fast_count: int,
) -> tuple[int, int] | None:
    relative = row_id - layout.first_row_id
    if relative < 0:
        return None
    slow_index, remainder = divmod(relative, layout.slow_id_stride)
    if remainder % layout.fast_id_stride:
        return None
    fast_index = remainder // layout.fast_id_stride
    if slow_index >= slow_count or fast_index >= fast_count:
        return None
    return slow_index, fast_index


def _scaled_grid_index(index: int, source_count: int, target_count: int) -> int:
    if source_count <= 1 or target_count <= 1:
        return 0
    return min(target_count - 1, index * (target_count - 1) // (source_count - 1))


def _nearest_sample_bins(
    pixel_count: int,
    source_count: int,
    sampled_indexes: tuple[int, ...],
) -> tuple[int, ...]:
    """Map display pixels to their nearest sampled source-domain coordinate.

    Representative samples are almost uniform, but they also contain adjacent
    edge anchors used to prove acquisition direction.  Treating every sample
    rank as equally wide would make a single edge cell occupy the same image
    width as a sample representing thousands of source cells.  Integer-scaled
    nearest-neighbour lookup preserves the true source-index spacing without
    losing either endpoint or using imprecise large-axis floats.
    """

    if pixel_count <= 0 or source_count <= 0 or not sampled_indexes:
        raise ValueError("Source-domain sampling requires positive dimensions.")
    targets: tuple[int, ...]
    if pixel_count == 1:
        denominator = 2
        targets = (source_count - 1,)
    else:
        denominator = pixel_count - 1
        targets = tuple(pixel * (source_count - 1) for pixel in range(pixel_count))
    scaled_samples = tuple(index * denominator for index in sampled_indexes)
    output: list[int] = []
    for target in targets:
        upper = bisect_left(scaled_samples, target)
        if upper == 0:
            output.append(0)
        elif upper == len(scaled_samples):
            output.append(len(scaled_samples) - 1)
        else:
            lower = upper - 1
            output.append(
                lower
                if target - scaled_samples[lower] <= scaled_samples[upper] - target
                else upper
            )
    return tuple(output)


def _viridis_rgba(
    value: float,
    low: float,
    high: float,
) -> tuple[int, int, int, int]:
    fraction = 0.5 if high == low else (value - low) / (high - low)
    fraction = max(0.0, min(1.0, fraction))
    color_index = min(255, int(fraction * 256.0))
    offset = color_index * 3
    return (
        _VIRIDIS_RGB[offset],
        _VIRIDIS_RGB[offset + 1],
        _VIRIDIS_RGB[offset + 2],
        255,
    )


def _pixel(
    rgba: bytearray,
    width: int,
    x: int,
    y: int,
    color: tuple[int, int, int, int],
) -> None:
    offset = (y * width + x) * 4
    rgba[offset : offset + 4] = bytes(color)


def _line(
    rgba: bytearray,
    width: int,
    height: int,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    color: tuple[int, int, int, int],
) -> None:
    dx, sx = abs(x1 - x0), 1 if x0 < x1 else -1
    dy, sy = -abs(y1 - y0), 1 if y0 < y1 else -1
    error = dx + dy
    while True:
        if 0 <= x0 < width and 0 <= y0 < height:
            _pixel(rgba, width, x0, y0, color)
        if x0 == x1 and y0 == y1:
            break
        doubled = 2 * error
        if doubled >= dy:
            error += dy
            x0 += sx
        if doubled <= dx:
            error += dx
            y0 += sy


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    checksum = binascii.crc32(kind)
    checksum = binascii.crc32(data, checksum) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum)


def _encode_png_rgba(
    width: int,
    height: int,
    rgba: bytearray,
    cancel_check: CancelCheck,
) -> bytes:
    expected = width * height * 4
    if len(rgba) != expected or expected > TRUSTED_DERIVED_MAX_DECODED_IMAGE_BYTES:
        raise TrustedDerivedRenderingError("The decoded image has invalid bounds.")
    rows = bytearray(height * (width * 4 + 1))
    source_stride = width * 4
    target_stride = source_stride + 1
    for y in range(height):
        if y % 32 == 0:
            cancel_check()
        source = y * source_stride
        target = y * target_stride
        rows[target] = 0
        rows[target + 1 : target + target_stride] = rgba[
            source : source + source_stride
        ]
    cancel_check()
    compressed = zlib.compress(bytes(rows), level=9)
    result = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + _png_chunk(b"IDAT", compressed)
        + _png_chunk(b"IEND", b"")
    )
    if len(result) > TRUSTED_DERIVED_MAX_ENCODED_IMAGE_BYTES:
        raise TrustedDerivedRenderingError("The encoded image is too large.")
    return result


def trusted_derived_payload_size(payload: DerivedPayload) -> int:
    """Conservatively size the retained primitive graph without serialising it."""

    total = 0
    nodes = 0
    active_containers: set[int] = set()
    stack: list[tuple[bool, Any, int]] = [(False, payload, 0)]
    while stack:
        leaving, value, depth = stack.pop()
        if leaving:
            active_containers.discard(cast(int, value))
            continue
        if depth > TRUSTED_DERIVED_MAX_PAYLOAD_DEPTH:
            raise TrustedDerivedRenderingError(
                "A derived payload is too deeply nested."
            )
        nodes += 1
        if nodes > TRUSTED_DERIVED_MAX_PAYLOAD_NODES:
            raise TrustedDerivedRenderingError("A derived payload has too many nodes.")
        if value is None or isinstance(value, bool):
            total += 8
        elif isinstance(value, (int, float)):
            if isinstance(value, float) and not math.isfinite(value):
                raise TrustedDerivedRenderingError(
                    "A derived payload contains a non-finite scalar."
                )
            total += 32
        elif isinstance(value, str):
            encoded = value.encode("utf-8")
            if len(encoded) > TRUSTED_DERIVED_MAX_TEXT_BYTES:
                raise TrustedDerivedRenderingError(
                    "A derived payload text value is oversized."
                )
            total += len(encoded) + 32
        elif isinstance(value, bytes):
            if len(value) > TRUSTED_DERIVED_MAX_RETAINED_PAYLOAD_BYTES:
                raise TrustedDerivedRenderingError(
                    "A derived payload byte value is oversized."
                )
            total += len(value) + 32
        elif isinstance(value, tuple):
            if len(value) > TRUSTED_DERIVED_MAX_PAYLOAD_ITEMS:
                raise TrustedDerivedRenderingError(
                    "A derived payload tuple has too many items."
                )
            identity = id(value)
            if identity in active_containers:
                raise TrustedDerivedRenderingError("A derived payload is cyclic.")
            active_containers.add(identity)
            total += 32 + len(value) * 8
            stack.append((True, identity, depth))
            stack.extend((False, item, depth + 1) for item in value)
        elif isinstance(value, dict):
            if len(value) > TRUSTED_DERIVED_MAX_PAYLOAD_ITEMS:
                raise TrustedDerivedRenderingError(
                    "A derived payload dictionary has too many items."
                )
            identity = id(value)
            if identity in active_containers:
                raise TrustedDerivedRenderingError("A derived payload is cyclic.")
            active_containers.add(identity)
            total += 64 + len(value) * 16
            stack.append((True, identity, depth))
            stack.extend((False, item, depth + 1) for item in value.keys())
            stack.extend((False, item, depth + 1) for item in value.values())
        else:
            raise TrustedDerivedRenderingError("A derived payload is not primitive.")
        if total > TRUSTED_DERIVED_MAX_RETAINED_PAYLOAD_BYTES:
            raise TrustedDerivedRenderingError(
                "The retained payload budget was exceeded."
            )
    return total


def _pair_mapping(value: object, *, name: str, maximum: int) -> dict[str, object]:
    if not isinstance(value, tuple) or len(value) > maximum:
        raise TrustedDerivedRenderingError(f"The derived {name} structure is invalid.")
    output: dict[str, object] = {}
    for item in value:
        if (
            not isinstance(item, tuple)
            or len(item) != 2
            or not isinstance(item[0], str)
            or item[0] in output
        ):
            raise TrustedDerivedRenderingError(
                f"The derived {name} structure is invalid."
            )
        output[item[0]] = item[1]
    return output


def validate_trusted_derived_payload(payload: DerivedPayload) -> int:
    """Validate the retained/cache schema and return its conservative size."""

    if not isinstance(payload, dict) or any(
        not isinstance(name, str) for name in payload
    ):
        raise TrustedDerivedRenderingError("A derived payload root is invalid.")
    allowed = {
        "format",
        "kind",
        "status",
        "description",
        "source",
        "images",
        "metadata",
    }
    required = {"format", "kind", "status", "description", "source", "images"}
    if set(payload) - allowed or not required.issubset(payload):
        raise TrustedDerivedRenderingError("A derived payload field is invalid.")
    if payload["format"] != "qplot-trusted-derived-payload-v1":
        raise TrustedDerivedRenderingError("A derived payload format is unsupported.")
    if payload["kind"] not in {"thumbnail", "preview", "metadata"}:
        raise TrustedDerivedRenderingError("A derived payload kind is invalid.")
    if payload["status"] not in {"ok", "empty", "unsupported", "error"}:
        raise TrustedDerivedRenderingError("A derived payload status is invalid.")
    if not isinstance(payload["description"], str):
        raise TrustedDerivedRenderingError("A derived payload description is invalid.")
    _pair_mapping(payload["source"], name="source", maximum=32)
    images = payload["images"]
    if not isinstance(images, tuple) or len(images) > TRUSTED_DERIVED_MAX_IMAGES:
        raise TrustedDerivedRenderingError("The derived image collection is invalid.")
    encoded_total = 0
    for image in images:
        fields = _pair_mapping(image, name="image", maximum=16)
        if set(fields) != {
            "encoding",
            "width",
            "height",
            "dependent",
            "dimensions",
            "sampled_points",
            "bytes",
        }:
            raise TrustedDerivedRenderingError("A derived image field is invalid.")
        width, height = fields["width"], fields["height"]
        encoded = fields["bytes"]
        if (
            fields["encoding"] != "png"
            or type(width) is not int
            or type(height) is not int
            or not 1 <= width <= TRUSTED_DERIVED_MAX_IMAGE_WIDTH
            or not 1 <= height <= TRUSTED_DERIVED_MAX_IMAGE_HEIGHT
            or width * height * 4 > TRUSTED_DERIVED_MAX_DECODED_IMAGE_BYTES
            or not isinstance(fields["dependent"], str)
            or fields["dimensions"] not in (1, 2)
            or type(fields["sampled_points"]) is not int
            or fields["sampled_points"] < 0
            or not isinstance(encoded, bytes)
            or not encoded.startswith(b"\x89PNG\r\n\x1a\n")
        ):
            raise TrustedDerivedRenderingError("A derived image value is invalid.")
        encoded_total += len(encoded)
        if encoded_total > TRUSTED_DERIVED_MAX_ENCODED_IMAGE_BYTES:
            raise TrustedDerivedRenderingError("The encoded image budget was exceeded.")
    metadata = payload.get("metadata")
    if metadata is not None:
        if payload["kind"] != "metadata":
            raise TrustedDerivedRenderingError("Derived metadata has the wrong kind.")
        _pair_mapping(metadata, name="metadata", maximum=64)
    return trusted_derived_payload_size(payload)
