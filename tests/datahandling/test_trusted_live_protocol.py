"""Strict generic JSON-boundary tests for trusted live-reader IPC."""

from __future__ import annotations

import math

import pytest

from qplot.datahandling import _trusted_live_protocol as protocol
from qplot.datahandling._trusted_live_protocol import (
    MAX_REPLY_BYTES,
    TrustedLiveProtocolValidationError,
    decode_reply_frame,
    decode_reply_payload,
    decode_request_frame,
    encode_query_results,
    encode_success_reply,
    validate_job_success,
)
from qplot.datahandling.trusted_live import TrustedQueryResult

_SESSION = "1" * 32


def _request_frame(payload: bytes) -> bytes:
    return (
        b'{"generation":1,"operation":"query","payload":'
        + payload
        + b',"protocol_version":1,"session":"'
        + _SESSION.encode("ascii")
        + b'"}'
    )


@pytest.mark.parametrize("number", [b"1e9999", b"-1e9999"])
def test_generic_decoder_rejects_exponent_overflow(number: bytes) -> None:
    with pytest.raises(
        TrustedLiveProtocolValidationError,
        match="non-finite JSON number",
    ):
        decode_request_frame(_request_frame(b'{"value":' + number + b"}"))


def test_generic_decoder_rejects_duplicate_json_keys() -> None:
    with pytest.raises(
        TrustedLiveProtocolValidationError,
        match="Duplicate JSON object key 'value'",
    ):
        decode_request_frame(_request_frame(b'{"value":1,"value":2}'))


def test_generic_decoder_enforces_aggregate_collection_item_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Five envelope fields, one payload field, and one list item are exactly
    # seven aggregate collection items.  The next list item must fail closed.
    monkeypatch.setattr(protocol, "MAX_JSON_COLLECTION_ITEMS", 7)

    assert decode_request_frame(_request_frame(b'{"values":[0]}')).payload == {
        "values": [0]
    }
    with pytest.raises(
        TrustedLiveProtocolValidationError,
        match="too many collection items",
    ):
        decode_request_frame(_request_frame(b'{"values":[0,1]}'))


def test_tagged_sqlite_reals_preserve_supported_canonical_values() -> None:
    values = (
        0.0,
        -0.0,
        float.fromhex("0x0.0000000000001p-1022"),
        float("inf"),
        float("-inf"),
        float("nan"),
    )
    result = TrustedQueryResult(("value",), tuple((value,) for value in values))
    frame = encode_success_reply(
        _SESSION,
        1,
        "query",
        {"results": encode_query_results((result,))},
    )
    assert len(frame) <= MAX_REPLY_BYTES

    envelope = decode_reply_frame(frame)
    status, payload = decode_reply_payload(envelope)
    decoded = validate_job_success(envelope.operation, payload)

    assert status == "ok"
    assert isinstance(decoded, TrustedQueryResult)
    for expected, row in zip(values, decoded.rows, strict=True):
        actual = row[0]
        assert isinstance(actual, float)
        if math.isnan(expected):
            assert math.isnan(actual)
        else:
            assert actual.hex() == expected.hex()
