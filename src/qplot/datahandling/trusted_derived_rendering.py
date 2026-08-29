"""Deterministic, bounded, Qt-independent Stage 5B derived rendering."""

from __future__ import annotations

import binascii
import math
import struct
import zlib
from collections.abc import Callable
from typing import Any, TypeAlias, cast

from qplot.datahandling.trusted_live_queries import TrustedDerivedSourceObservation
from qplot.datahandling.trusted_work_scheduler import (
    RenderingOptions,
    TrustedWorkKind,
)

TRUSTED_DERIVED_RENDERER_VERSION = "trusted-derived-renderer-v1"
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
                rgba, points = _render_2d(
                    observation,
                    dependencies[0],
                    dependencies[1],
                    dependent,
                    width,
                    height,
                    cancel_check,
                )
                dimensionality = 2
            else:
                continue
        except _UnsupportedNumericData as error:
            return _base_payload(
                observation,
                kind,
                status="unsupported",
                description=str(error),
            )
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
            description="No bounded numeric 1D or 2D dependent data was available.",
        )
    payload = _base_payload(
        observation,
        kind,
        status="ok",
        description="Bounded trusted result-prefix rendering.",
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
    return rgba, len(points)


def _render_2d(
    observation: TrustedDerivedSourceObservation,
    x_name: str,
    y_name: str,
    z_name: str,
    width: int,
    height: int,
    cancel_check: CancelCheck,
) -> tuple[bytearray, int]:
    indexes = {name: index for index, name in enumerate(observation.sample_columns)}
    points: list[tuple[float, float, float]] = []
    for index, row in enumerate(observation.sample_rows):
        if index % 128 == 0:
            cancel_check()
        if len(row) != len(observation.sample_columns):
            raise _UnsupportedNumericData(
                "The bounded numeric sample has inconsistent vector lengths."
            )
        x = _finite_number(row[indexes[x_name]])
        y = _finite_number(row[indexes[y_name]])
        z = _finite_number(row[indexes[z_name]])
        if x is not None and y is not None and z is not None:
            points.append((x, y, z))
    rgba = bytearray(b"\xff" * (width * height * 4))
    if not points:
        return rgba, 0
    x_min, x_max = min(point[0] for point in points), max(point[0] for point in points)
    y_min, y_max = min(point[1] for point in points), max(point[1] for point in points)
    z_min, z_max = min(point[2] for point in points), max(point[2] for point in points)
    x_span, y_span, z_span = x_max - x_min, y_max - y_min, z_max - z_min
    if not all(math.isfinite(span) for span in (x_span, y_span, z_span)):
        raise _UnsupportedNumericData(
            "The bounded numeric sample exceeds the supported finite range."
        )
    x_span, y_span, z_span = x_span or 1.0, y_span or 1.0, z_span or 1.0
    radius = max(1, min(width, height) // 100)
    for index, (x, y, z) in enumerate(points):
        if index % 128 == 0:
            cancel_check()
        raw_px = (x - x_min) * (width - 1) / x_span
        raw_py = (y - y_min) * (height - 1) / y_span
        raw_fraction = (z - z_min) / z_span
        if not all(math.isfinite(value) for value in (raw_px, raw_py, raw_fraction)):
            raise _UnsupportedNumericData(
                "The bounded numeric sample exceeds the supported finite range."
            )
        px = round(raw_px)
        py = height - 1 - round(raw_py)
        fraction = max(0.0, min(1.0, raw_fraction))
        color = (
            round(255 * fraction),
            round(96 * (1.0 - abs(2.0 * fraction - 1.0))),
            round(255 * (1.0 - fraction)),
            255,
        )
        for yy in range(max(0, py - radius), min(height, py + radius + 1)):
            for xx in range(max(0, px - radius), min(width, px + radius + 1)):
                _pixel(rgba, width, xx, yy, color)
    return rgba, len(points)


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
