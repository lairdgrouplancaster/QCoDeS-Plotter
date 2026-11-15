"""Shared fixtures and assertions for trusted array preview."""

import io

import numpy as np

__all__ = [
    '_blob',
]


def _blob(values, version=(3, 0)):
    stream = io.BytesIO()
    np.lib.format.write_array(stream, np.asarray(values), version=version)
    return stream.getvalue()
