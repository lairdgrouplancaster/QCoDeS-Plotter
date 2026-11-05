"""Wire compatibility and rejection cases, independent of process supervision."""

import hashlib
import hmac
import json

import pytest

from qplot import _shutdown_protocol as protocol

_KEY = bytes(range(32))
_SESSION = bytes(range(32, 64))
_MESSAGE = bytes(range(64, 96))

# Version-one records retained across the protocol/lifecycle module split.
_HELLO = bytes.fromhex(
    "51504c54535631000101000000000000"
    "202122232425262728292a2b2c2d2e2f303132333435363738393a3b3c3d3e3f"
    "404142434445464748494a4b4c4d4e4f505152535455565758595a5b5c5d5e5f"
    "00000000000004d2"
    "d021de73adede4e2c74e247395e274fc32e565a3e32e5bf8539b439c12ff8d6d"
)
_RESULT = bytes.fromhex(
    "51504c54535631000107000000000000"
    "202122232425262728292a2b2c2d2e2f303132333435363738393a3b3c3d3e3f"
    "00000054"
    "7b22646961676e6f7374696373223a5b227265616465722073746f70706564225d2c"
    "22666f72636564223a747275652c2272657475726e5f636f6465223a37302c227369"
    "676e616c5f6e756d626572223a31357d"
    "75f091f11e09486e6d22f0b06a4f9ccfa77071b0d9d1eabd0f921a6bcb94cfb9"
)


def _authenticate(body: bytes) -> bytes:
    """Act as an authenticated peer sending deliberately malformed records."""
    return body + hmac.digest(_KEY, body, hashlib.sha256)


def test_version_one_control_record_bytes_are_preserved():
    payload = (1234).to_bytes(8, "big")
    assert protocol._encode_frame(
        protocol._HELLO,
        authentication_key=_KEY,
        session_nonce=_SESSION,
        message_nonce=_MESSAGE,
        payload=payload,
    ) == _HELLO
    assert protocol._decode_frame(
        _HELLO, authentication_key=_KEY, session_nonce=_SESSION
    ) == (protocol._HELLO, _MESSAGE, payload)


def test_version_one_result_record_bytes_are_preserved():
    outcome = protocol._SupervisionOutcome(
        70, forced=True, signal_number=15, diagnostics=("reader stopped",)
    )
    assert protocol._encode_api_launcher_outcome(
        outcome, authentication_key=_KEY, session_nonce=_SESSION
    ) == _RESULT
    assert protocol._decode_api_launcher_outcome(
        _RESULT, authentication_key=_KEY, session_nonce=_SESSION
    ) == outcome


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("authentication_key", b"short", "authentication key"),
        ("session_nonce", b"short", "session nonce"),
        ("message_nonce", b"short", "message nonce"),
        ("payload", b"short", "payload"),
    ],
)
def test_control_encoder_rejects_invalid_field_lengths(field, value, message):
    fields = dict(
        authentication_key=_KEY,
        session_nonce=_SESSION,
        message_nonce=_MESSAGE,
        payload=(1234).to_bytes(8, "big"),
    )
    fields[field] = value
    with pytest.raises(ValueError, match=message):
        protocol._encode_frame(protocol._HELLO, **fields)


@pytest.mark.parametrize("frame", [_HELLO[:-1], _HELLO + b"x", b""])
def test_control_decoder_requires_exact_length(frame):
    with pytest.raises(protocol.ShutdownSupervisorError, match="frame length"):
        protocol._decode_frame(frame, authentication_key=_KEY, session_nonce=_SESSION)


@pytest.mark.parametrize("offset", [0, 16, 48, 80, -1])
def test_control_decoder_rejects_tampering(offset):
    frame = bytearray(_HELLO)
    frame[offset] ^= 1
    with pytest.raises(protocol.ShutdownSupervisorError, match="authentication"):
        protocol._decode_frame(bytes(frame), authentication_key=_KEY, session_nonce=_SESSION)


@pytest.mark.parametrize(
    ("offset", "message"), [(0, "magic"), (8, "version"), (16, "session nonce")]
)
def test_control_decoder_validates_authenticated_envelope(offset, message):
    body = bytearray(_HELLO[:-32])
    body[offset] ^= 1
    with pytest.raises(protocol.ShutdownSupervisorError, match=message):
        protocol._decode_frame(
            _authenticate(body), authentication_key=_KEY, session_nonce=_SESSION
        )


@pytest.mark.parametrize("deadline", [float("nan"), float("inf"), -float("inf")])
def test_arm_rejects_nonfinite_deadlines(deadline):
    with pytest.raises(ValueError, match="deadline must be finite"):
        protocol._encode_arm(_KEY, _SESSION, _MESSAGE, deadline)


@pytest.mark.parametrize(
    ("offset", "message"),
    [(0, "magic"), (8, "version"), (9, "type"), (16, "session nonce")],
)
def test_result_decoder_validates_authenticated_envelope(offset, message):
    body = bytearray(_RESULT[:-32])
    body[offset] ^= 1
    with pytest.raises(protocol.ShutdownSupervisorError, match=message):
        protocol._decode_api_launcher_outcome(
            _authenticate(body), authentication_key=_KEY, session_nonce=_SESSION
        )


@pytest.mark.parametrize("frame", [_RESULT[:-1], _RESULT + b"x", b""])
def test_result_decoder_requires_exact_length(frame):
    with pytest.raises(protocol.ShutdownSupervisorError, match="(incomplete|length)"):
        protocol._decode_api_launcher_outcome(
            frame, authentication_key=_KEY, session_nonce=_SESSION
        )


def test_result_decoder_rejects_tampered_payload():
    frame = bytearray(_RESULT)
    frame[60] ^= 1
    with pytest.raises(protocol.ShutdownSupervisorError, match="authentication"):
        protocol._decode_api_launcher_outcome(
            bytes(frame), authentication_key=_KEY, session_nonce=_SESSION
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"return_code": True}, "status"),
        ({"forced": 1}, "forced flag"),
        ({"signal_number": 0}, "signal"),
        ({"signal_number": True}, "signal"),
        ({"diagnostics": [1]}, "diagnostics"),
        ({"diagnostics": ["x"] * 65}, "diagnostics"),
        ({"extra": None}, "fields"),
    ],
)
def test_result_decoder_validates_authenticated_payload(changes, message):
    raw = dict(return_code=0, forced=False, signal_number=None, diagnostics=[])
    raw.update(changes)
    payload = json.dumps(raw).encode()
    # Keep the independently retained v1 header, replacing its payload length.
    header = _RESULT[:48] + len(payload).to_bytes(4, "big")
    with pytest.raises(protocol.ShutdownSupervisorError, match=message):
        protocol._decode_api_launcher_outcome(
            _authenticate(header + payload), authentication_key=_KEY, session_nonce=_SESSION
        )


def test_result_decoder_rejects_oversized_declared_payload_before_reading_it():
    header = _RESULT[:48] + (256 * 1024 + 1).to_bytes(4, "big")
    with pytest.raises(protocol.ShutdownSupervisorError, match="bounded result size"):
        protocol._decode_api_launcher_outcome(
            _authenticate(header), authentication_key=_KEY, session_nonce=_SESSION
        )
