"""Startup payloads keep their version-one schema and endpoint policy."""

import json

import pytest

from qplot import _shutdown_protocol as protocol
from qplot import _shutdown_supervisor as supervisor

pytestmark = pytest.mark.pure

_KEY = bytes(range(32))
_NONCE = b"n" * 32


def _gui_bootstrap(**changes):
    return protocol._Bootstrap(
        **dict(host="127.0.0.1", port=1234, authentication_key=_KEY,
               session_nonce=_NONCE, startup_timeout=5.0, database_path="μ.db") | changes,
    )


def _api_bootstrap(**changes):
    return protocol._ApiLauncherBootstrap(
        **dict(host="127.0.0.1", port=1234, authentication_key=_KEY,
               session_nonce=_NONCE, startup_deadline=12.5, caller_pid=42,
               child_argv=("python", "-m", "qplot"), database_path=None) | changes,
    )


def test_gui_bootstrap_preserves_version_one_json():
    bootstrap = _gui_bootstrap()
    encoded = protocol._encode_bootstrap(bootstrap)
    assert encoded == (
        '{"authentication_key":"000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f",'
        '"database_path":"\\u03bc.db","host":"127.0.0.1","port":1234,'
        '"session_nonce":"' + "6e" * 32 + '","startup_timeout":5.0,"version":1}'
    )
    assert protocol._decode_bootstrap(encoded) == bootstrap


def test_api_bootstrap_preserves_version_one_json():
    bootstrap = _api_bootstrap()
    encoded = protocol._encode_api_launcher_bootstrap(bootstrap)
    assert encoded == (
        '{"authentication_key":"000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f",'
        '"caller_pid":42,"child_argv":["python","-m","qplot"],"database_path":null,'
        '"host":"127.0.0.1","port":1234,"session_nonce":"' + "6e" * 32 + '",'
        '"startup_deadline":12.5,"version":1}'
    )
    assert protocol._decode_api_launcher_bootstrap(encoded) == bootstrap


@pytest.mark.parametrize("kind", ["gui", "api"])
@pytest.mark.parametrize("changes, message", [
    ({"extra": 1}, "fields are invalid"),
    ({"version": 2}, "version is unsupported"),
    ({"host": "192.0.2.1"}, "endpoint is not loopback"),
    ({"port": True}, "port is invalid"),
    ({"port": 65536}, "port is invalid"),
    ({"database_path": []}, "database-path override is invalid"),
    ({"authentication_key": "not hex"}, "nonce decoding raised ValueError"),
    ({"authentication_key": "01"}, "authentication key has the wrong length"),
    ({"session_nonce": "01"}, "session nonce has the wrong length"),
])
def test_bootstrap_rejects_invalid_fields(kind, changes, message):
    if kind == "gui":
        data = json.loads(protocol._encode_bootstrap(_gui_bootstrap()))
        decode = protocol._decode_bootstrap
    else:
        data = json.loads(protocol._encode_api_launcher_bootstrap(_api_bootstrap()))
        decode = protocol._decode_api_launcher_bootstrap
    with pytest.raises(protocol.ShutdownSupervisorError, match=message):
        decode(json.dumps(data | changes))


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf")])
def test_gui_requires_a_positive_finite_timeout(timeout):
    encoded = protocol._encode_bootstrap(_gui_bootstrap(startup_timeout=timeout))
    with pytest.raises(protocol.ShutdownSupervisorError, match="timeout is invalid"):
        protocol._decode_bootstrap(encoded)


@pytest.mark.parametrize("deadline", [0.0, -1.0])
def test_api_accepts_past_absolute_deadlines(deadline):
    encoded = protocol._encode_api_launcher_bootstrap(_api_bootstrap(startup_deadline=deadline))
    assert protocol._decode_api_launcher_bootstrap(encoded).startup_deadline == deadline


@pytest.mark.parametrize("kind", ["gui", "api"])
def test_environment_marker_is_consumed_even_if_decoding_fails(monkeypatch, kind):
    if kind == "gui":
        marker = supervisor._BOOTSTRAP_ENVIRONMENT_KEY
        consume = supervisor.ShutdownSupervisorClient.from_environment
    else:
        marker = supervisor._API_LAUNCHER_BOOTSTRAP_ENVIRONMENT_KEY
        consume = supervisor._api_launcher_bootstrap_from_environment
    monkeypatch.setenv(marker, "{")
    with pytest.raises(protocol.ShutdownSupervisorError, match="bootstrap decoding"):
        consume()
    with pytest.raises(protocol.ShutdownSupervisorError, match="marker is missing"):
        consume()
