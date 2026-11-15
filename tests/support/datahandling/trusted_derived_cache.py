"""Shared fixtures and assertions for trusted derived cache."""

from __future__ import annotations

from qplot.datahandling.file_identity import DatabaseInstance
from qplot.datahandling.trusted_query_types import (
    TrustedSourceRevision,
)
from qplot.datahandling.trusted_work_scheduler import (
    RenderingOptions,
    TrustedCacheWorkKey,
    TrustedWorkKind,
)

__all__ = [
    '_key',
    '_payload',
]


_DEFAULT_OPTIONS = RenderingOptions()


def _key(
    value: int = 1,
    *,
    options: RenderingOptions = _DEFAULT_OPTIONS,
) -> TrustedCacheWorkKey:
    instance = DatabaseInstance("/data/test.db", "/data/test.db", (7, 11))
    return TrustedCacheWorkKey(
        instance,
        "guid-1",
        TrustedWorkKind.PREVIEW,
        TrustedSourceRevision(f"revision-{value}".encode()),
        "renderer-v1",
        options,
    )


def _payload(value: str = "ok"):
    return {
        "format": "qplot-trusted-derived-payload-v1",
        "kind": "preview",
        "status": "ok",
        "description": value,
        "source": (("revision", value.encode()),),
        "images": (),
    }
