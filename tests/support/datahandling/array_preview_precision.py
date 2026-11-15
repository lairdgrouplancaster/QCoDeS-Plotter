"""Shared fixtures and assertions for array preview precision."""

from tests.support.datahandling.trusted_derived_rendering import (
    _decode_png_rgba,
)

__all__ = [
    '_image',
]


def _image(payload):
    assert payload["status"] == "ok", payload
    image = dict(payload["images"][0])
    return _decode_png_rgba(image["bytes"]), image["sampled_points"]
