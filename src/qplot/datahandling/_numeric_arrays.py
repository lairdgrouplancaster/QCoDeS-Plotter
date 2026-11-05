"""NPY header inspection and bounded numeric slices for caller-owned streams.

The caller owns the stream and chooses its dtype and allocation limits. Header
inspection never loads the payload or unpickles object arrays.
"""

from collections.abc import Callable
from dataclasses import dataclass
from math import prod
from typing import Any, Protocol

import numpy as np
from numpy.typing import NDArray


class ArrayStream(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...
    def tell(self) -> int: ...
    def seek(self, offset: int, origin: int = 0, /) -> object: ...


class ArrayBlob(ArrayStream, Protocol):
    def __len__(self) -> int: ...


class UnsupportedNpyVersionError(ValueError):
    """The array uses a version outside QCoDeS' supported numeric formats."""


@dataclass(frozen=True)
class NpyHeader:
    shape: tuple[int, ...]
    fortran_order: bool
    dtype: np.dtype[Any]
    offset: int
    size: int

    @property
    def total_bytes(self) -> int:
        """Exact stream size implied by this header, before any allocation."""
        return self.offset + self.size * self.dtype.itemsize


def read_npy_header(stream: ArrayStream) -> NpyHeader:
    """Inspect v1/v2/v3 metadata; leave policy and payload reads to callers."""
    version = np.lib.format.read_magic(stream)
    if version == (1, 0):
        read_header = np.lib.format.read_array_header_1_0
    elif version in ((2, 0), (3, 0)):
        # QCoDeS writes v3. Numeric dtype headers are ASCII in v2 and v3;
        # only structured field-name encoding differs, and callers reject it.
        read_header = np.lib.format.read_array_header_2_0
    else:
        raise UnsupportedNpyVersionError("Unsupported NPY version")
    shape, fortran_order, dtype = read_header(stream)
    return NpyHeader(shape, fortran_order, dtype, stream.tell(), prod(shape))


class NumericArrayReader:
    """Read C-ordered numeric slices from a caller-owned, read-only BLOB.

    The worker opens the SQLite BLOB with ``readonly=True`` and bounds each
    requested slice. This reader neither opens nor closes that resource.
    """

    def __init__(self, blob: ArrayBlob):
        self.blob = blob
        try:
            header = read_npy_header(blob)
        except UnsupportedNpyVersionError:
            raise ValueError("Unsupported heatmap array format.") from None
        self.shape = header.shape
        self.fortran_order = header.fortran_order
        self.dtype = header.dtype
        self.offset = header.offset
        self.size = header.size
        if self.dtype.kind not in "biufc":
            raise ValueError("Heatmap arrays must contain numeric values.")
        if header.total_bytes != len(blob):
            raise ValueError("Heatmap array payload does not match its shape.")

    def read(
        self, start: int, stop: int, check_cancelled: Callable[[], None],
    ) -> NDArray:
        check_cancelled()
        if not self.fortran_order or len(self.shape) <= 1:
            self.blob.seek(self.offset + start * self.dtype.itemsize)
            return np.frombuffer(
                self.blob.read((stop - start) * self.dtype.itemsize),
                dtype=self.dtype,
            )

        # Flatten every column in the same logical (C) order, even when
        # signal and setpoints were saved with different memory layouts.
        positions = np.ravel_multi_index(
            np.unravel_index(np.arange(start, stop), self.shape),
            self.shape, order="F",
        )
        order = np.argsort(positions)
        ordered_positions = positions[order]
        boundaries = np.r_[0, np.flatnonzero(np.diff(ordered_positions) != 1) + 1,
                           stop - start]
        values = np.empty(stop - start, dtype=self.dtype)
        for first, last in zip(boundaries[:-1], boundaries[1:], strict=True):
            check_cancelled()
            self.blob.seek(self.offset + int(ordered_positions[first]) * self.dtype.itemsize)
            values[order[first:last]] = np.frombuffer(
                self.blob.read(int(last - first) * self.dtype.itemsize),
                dtype=self.dtype,
            )
        return values
