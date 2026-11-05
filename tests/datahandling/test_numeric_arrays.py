"""Shared NPY inspection must retain dtype, layout, and bounded reads."""

import io

import numpy as np
import pytest

from qplot.datahandling._numeric_arrays import (
    NumericArrayReader,
    read_npy_header,
)
from qplot.datahandling.trusted_array_preview import MAX_SAMPLES, _decode
from qplot.datahandling.trusted_derived_rendering import _UnsupportedNumericData


class _Blob(io.BytesIO):
    def __init__(self, payload):
        super().__init__(payload)
        self.reads = []

    def __len__(self):
        return self.getbuffer().nbytes

    def read(self, size=-1):
        assert size >= 0, "array decoding must always request a bounded read"
        self.reads.append((self.tell(), size))
        return super().read(size)


def _encoded(values, version=(3, 0)):
    stream = io.BytesIO()
    np.lib.format.write_array(stream, values, version=version)
    return stream.getvalue()


def _check():
    pass


@pytest.mark.parametrize("version", [(1, 0), (2, 0), (3, 0)])
@pytest.mark.parametrize("order", ["C", "F"])
@pytest.mark.parametrize("dtype", [np.int64, np.uint64, np.longdouble, np.complex128])
def test_slices_retain_logical_order_and_dtype(version, order, dtype):
    values = np.arange(24, dtype=dtype).reshape((2, 3, 4), order=order)
    if np.dtype(dtype).kind in "iu":
        values += 2**60
    elif np.dtype(dtype).kind == "c":
        values += 1j
    blob = _Blob(_encoded(values, version))
    reader = NumericArrayReader(blob)
    blob.reads.clear()

    result = reader.read(2, 17, _check)

    assert result.dtype == values.dtype
    np.testing.assert_array_equal(result, values.ravel()[2:17])
    assert sum(size for _offset, size in blob.reads) == result.nbytes
    assert not blob.closed, "the caller retains ownership of the BLOB"


@pytest.mark.parametrize("values", [np.array(42), np.array([], dtype=bool), np.array([True, False])])
def test_scalar_empty_and_boolean_arrays(values):
    reader = NumericArrayReader(_Blob(_encoded(values)))
    np.testing.assert_array_equal(reader.read(0, values.size, _check), values.ravel())


@pytest.mark.parametrize("payload_change", [lambda payload: payload[:-1], lambda payload: payload + b"x"])
def test_payload_length_is_exact_before_reading_samples(payload_change):
    payload = _encoded(np.arange(3))
    header = read_npy_header(io.BytesIO(payload))
    blob = _Blob(payload_change(payload))
    with pytest.raises(ValueError, match="payload does not match its shape"):
        NumericArrayReader(blob)
    assert all(offset + size <= header.offset for offset, size in blob.reads)
    with pytest.raises(_UnsupportedNumericData, match="cannot be decoded safely"):
        _decode(blob.getvalue())


@pytest.mark.parametrize("dtype", [np.dtype("O"), np.dtype("U1"), np.dtype([("value", "<i8")])])
def test_non_numeric_headers_never_read_or_unpickle_the_payload(dtype):
    stream = io.BytesIO()
    np.lib.format.write_array_header_2_0(
        stream, {"descr": np.lib.format.dtype_to_descr(dtype), "shape": (1,), "fortran_order": False},
    )
    header_size = stream.tell()
    stream.write(b"not a pickle or an array payload")
    blob = _Blob(stream.getvalue())
    with pytest.raises(ValueError, match="must contain numeric values"):
        NumericArrayReader(blob)
    assert all(offset + size <= header_size for offset, size in blob.reads)
    with pytest.raises(_UnsupportedNumericData):
        _decode(blob.getvalue())


def test_preview_retains_real_only_and_sample_budget_policies():
    for values in (np.array([1 + 2j]), np.ones(MAX_SAMPLES + 1, dtype=bool)):
        payload = _encoded(values)
        assert NumericArrayReader(_Blob(payload)).size == values.size
        with pytest.raises(_UnsupportedNumericData):
            _decode(payload)


def test_unsupported_version_preserves_caller_errors():
    payload = np.lib.format.magic(9, 0)
    with pytest.raises(ValueError, match="Unsupported heatmap array format"):
        NumericArrayReader(_Blob(payload))
    with pytest.raises(_UnsupportedNumericData, match="cannot be decoded safely"):
        _decode(payload)


def test_cancellation_prevents_any_slice_read():
    blob = _Blob(_encoded(np.arange(8)))
    reader = NumericArrayReader(blob)
    blob.reads.clear()

    def cancel():
        raise InterruptedError("cancelled")

    with pytest.raises(InterruptedError):
        reader.read(0, 4, cancel)
    assert blob.reads == []


def test_fortran_slices_check_cancellation_between_read_spans():
    blob = _Blob(_encoded(np.asfortranarray(np.arange(12).reshape(3, 4))))
    reader = NumericArrayReader(blob)
    blob.reads.clear()
    checks = 0

    def cancel_after_first_span():
        nonlocal checks
        checks += 1
        if checks == 3:
            raise InterruptedError("cancelled")

    with pytest.raises(InterruptedError):
        reader.read(0, 4, cancel_after_first_span)
    assert len(blob.reads) == 1
    assert blob.reads[0][1] == reader.dtype.itemsize
