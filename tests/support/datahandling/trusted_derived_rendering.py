"""Shared fixtures and assertions for trusted derived rendering."""

from __future__ import annotations

import struct
import zlib

from qplot.datahandling.file_identity import DatabaseInstance
from qplot.datahandling.trusted_query_types import (
    Trusted2DGridLayout,
    TrustedDerivedSourceObservation,
    TrustedParameterView,
)

__all__ = [
    '_decode_png_rgba',
    '_observation',
    '_rgba_at',
]


def _observation(*, dimensions: int = 1, unsupported: str | None = None):
    instance = DatabaseInstance("/data/test.db", "/data/test.db", (7, 11))
    if dimensions == 1:
        columns = ("id", "x", "signal")
        rows = tuple(
            (index, float(index), float(index * index)) for index in range(1, 9)
        )
        parameters = (
            TrustedParameterView("x", "X", "V", (), "numeric"),
            TrustedParameterView("signal", "Signal", "A", ("x",), "numeric"),
        )
    else:
        columns = ("id", "x", "y", "signal")
        rows = tuple(
            (index + 1, float(index % 4), float(index // 4), float(index))
            for index in range(16)
        )
        parameters = (
            TrustedParameterView("x", "X", "V", (), "numeric"),
            TrustedParameterView("y", "Y", "V", (), "numeric"),
            TrustedParameterView("signal", "Signal", "A", ("x", "y"), "numeric"),
        )
    return TrustedDerivedSourceObservation(
        1,
        instance,
        1,
        "guid-1",
        b"service",
        2,
        3,
        "results-1-1",
        columns,
        b"schema-digest",
        len(rows),
        parameters,
        ("signal",),
        (4, 4) if dimensions == 2 else (8,),
        columns,
        rows,
        unsupported,
        validated_2d_layouts=(
            (
                Trusted2DGridLayout(
                    dependent="signal",
                    dependencies=("x", "y"),
                    shape=(4, 4),
                    fast_axis_index=0,
                    first_row_id=1,
                    fast_id_stride=1,
                    slow_id_stride=4,
                    sample_slow_indexes=(0, 1, 2, 3),
                    sample_fast_indexes=(0, 1, 2, 3),
                    slow_reversed=False,
                    fast_reversed=False,
                    serpentine=False,
                    complete=True,
                    source="planned",
                ),
            )
            if dimensions == 2
            else ()
        ),
    )


def _decode_png_rgba(encoded: bytes) -> tuple[int, int, bytes]:
    assert encoded.startswith(b"\x89PNG\r\n\x1a\n")
    position = 8
    compressed = bytearray()
    width = height = 0
    while position < len(encoded):
        length = struct.unpack(">I", encoded[position : position + 4])[0]
        kind = encoded[position + 4 : position + 8]
        data = encoded[position + 8 : position + 8 + length]
        position += 12 + length
        if kind == b"IHDR":
            width, height = struct.unpack(">II", data[:8])
        elif kind == b"IDAT":
            compressed.extend(data)
        elif kind == b"IEND":
            break
    rows = zlib.decompress(bytes(compressed))
    stride = width * 4 + 1
    assert len(rows) == height * stride
    assert all(rows[offset] == 0 for offset in range(0, len(rows), stride))
    rgba = b"".join(
        rows[offset + 1 : offset + stride] for offset in range(0, len(rows), stride)
    )
    return width, height, rgba


def _rgba_at(rgba: bytes, width: int, x: int, y: int) -> tuple[int, int, int, int]:
    offset = (y * width + x) * 4
    return tuple(rgba[offset : offset + 4])  # type: ignore[return-value]
