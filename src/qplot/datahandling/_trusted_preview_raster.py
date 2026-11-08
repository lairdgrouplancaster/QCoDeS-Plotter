"""Shared numeric raster primitives for scalar and array trusted previews.

The renderers share exact normalization, colors and line drawing without
importing one another. This module does not open databases or create payloads.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from fractions import Fraction
from typing import TypeAlias

from .trusted_query_types import TrustedDerivedSourceObservation

CancelCheck: TypeAlias = Callable[[], None]

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


def _finite_number(value: object) -> int | float | Fraction | None:
    # Array previews construct exact finite ratios internally. These never
    # enter the captured source or exported primitive/PNG payload format.
    if isinstance(value, Fraction):
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _render_1d(
    observation: TrustedDerivedSourceObservation,
    x_name: str,
    y_name: str,
    width: int,
    height: int,
    cancel_check: CancelCheck,
) -> tuple[bytearray, int]:
    indexes = {name: index for index, name in enumerate(observation.sample_columns)}
    points: list[tuple[int | float | Fraction, int | float | Fraction]] = []
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
    return _render_line_points(points, width, height, cancel_check)


def _render_line_points(points, width, height, cancel_check):
    """Rasterise finite acquired pairs in their original order."""
    rgba = bytearray(b"\xff" * (width * height * 4))
    if not points:
        return rgba, 0
    x_values = tuple(point[0] for point in points)
    y_values = tuple(point[1] for point in points)
    x_min, x_max = min(x_values), max(x_values)
    y_min, y_max = min(y_values), max(y_values)
    margin = max(2, min(width, height) // 20)
    prior: tuple[int, int] | None = None
    for index, (x, y) in enumerate(points):
        if index % 128 == 0:
            cancel_check()
        raw_px = _normalised_position(x, x_min, x_max) * (width - 1 - 2 * margin)
        raw_py = _normalised_position(y, y_min, y_max) * (height - 1 - 2 * margin)
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


def _normalised_position(value, low, high):
    """Map finite samples before pixel scaling can overflow their range."""
    if high == low:
        return 0.
    samples = (value, low, high)
    integer_count = sum(isinstance(sample, int) for sample in samples)
    if (any(isinstance(sample, Fraction) for sample in samples)
            or (0 < integer_count < 3 and max(map(abs, samples)) >= 2**53)):
        # Mixed array records can retain an integer between adjacent floats.
        # Subtract exactly before normalizing, rather than rounding that
        # integer to its neighbouring float and losing a recorded step.
        # Repeated integer heatmap cells retain their exact fractional means.
        value, low, high = map(Fraction, samples)
        return float((value - low) / (high - low))
    span = high - low
    if math.isfinite(span):
        return (value - low) / span
    scale = max(abs(low), abs(high))
    return (value / scale - low / scale) / (high / scale - low / scale)


def _viridis_rgba(
    value: float,
    low: float,
    high: float,
) -> tuple[int, int, int, int]:
    fraction = 0.5 if high == low else _normalised_position(value, low, high)
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

