"""Pure canonical values and bounded JSON decoding for derived cache entries.

Encoding and validation preserve the on-disk format. Filesystem ownership,
publication, eviction and index transactions remain in trusted_derived_cache.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
from typing import cast

from .trusted_derived_rendering import TRUSTED_DERIVED_MAX_ENCODED_IMAGE_BYTES
from .trusted_work_scheduler import TrustedCacheWorkKey

TRUSTED_DERIVED_CACHE_MAX_ENTRY_BYTES = 16 * 1024 * 1024
TRUSTED_DERIVED_CACHE_MAX_JSON_DEPTH = 32
TRUSTED_DERIVED_CACHE_MAX_JSON_NODES = 131_072
TRUSTED_DERIVED_CACHE_MAX_CONTAINER_ITEMS = 65_536
TRUSTED_DERIVED_CACHE_MAX_TEXT_BYTES = 1024 * 1024


def _valid_text(value: str, *, maximum: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) > maximum:
        raise ValueError("A cache text value is oversized.")
    return value


def _canonical_value(value: object) -> object:
    if value is None:
        return ["none"]
    if isinstance(value, bool):
        return ["bool", value]
    if type(value) is int:
        return ["int", str(value)]
    if isinstance(value, float):
        return ["float", value.hex()]
    if isinstance(value, str):
        return ["str", _valid_text(value, maximum=TRUSTED_DERIVED_CACHE_MAX_TEXT_BYTES)]
    if isinstance(value, bytes):
        return ["bytes", base64.b64encode(value).decode("ascii")]
    if isinstance(value, tuple):
        return ["tuple", [_canonical_value(item) for item in value]]
    if isinstance(value, dict):
        if any(not isinstance(name, str) for name in value):
            raise TypeError("Derived payload dictionary keys must be text.")
        return [
            "dict",
            [
                [name, _canonical_value(value[name])]
                for name in sorted(cast(dict[str, object], value))
            ],
        ]
    raise TypeError(f"Unsupported canonical cache value {type(value).__name__}.")


def _restore_value(
    value: object,
    *,
    depth: int = 0,
    budget: list[int] | None = None,
) -> object:
    if budget is None:
        budget = [0]
    budget[0] += 1
    if budget[0] > TRUSTED_DERIVED_CACHE_MAX_JSON_NODES:
        raise ValueError("A cache value has too many nodes.")
    if depth > 16 or not isinstance(value, list) or not value:
        raise ValueError("A cache value has an invalid canonical structure.")
    tag = value[0]
    if tag == "none" and len(value) == 1:
        return None
    if tag == "bool" and len(value) == 2 and isinstance(value[1], bool):
        return value[1]
    if tag == "int" and len(value) == 2 and isinstance(value[1], str):
        _valid_text(value[1], maximum=128)
        return int(value[1])
    if tag == "float" and len(value) == 2 and isinstance(value[1], str):
        _valid_text(value[1], maximum=128)
        restored = float.fromhex(value[1])
        if not math.isfinite(restored):
            raise ValueError("A cache float is not finite.")
        return restored
    if tag == "str" and len(value) == 2 and isinstance(value[1], str):
        return _valid_text(value[1], maximum=TRUSTED_DERIVED_CACHE_MAX_TEXT_BYTES)
    if tag == "bytes" and len(value) == 2 and isinstance(value[1], str):
        maximum_encoded = 4 * ((TRUSTED_DERIVED_MAX_ENCODED_IMAGE_BYTES + 2) // 3)
        if len(value[1]) > maximum_encoded:
            raise ValueError("A cache byte scalar is oversized.")
        restored_bytes = base64.b64decode(value[1], validate=True)
        if len(restored_bytes) > TRUSTED_DERIVED_MAX_ENCODED_IMAGE_BYTES:
            raise ValueError("A cache byte scalar is oversized.")
        return restored_bytes
    if tag == "tuple" and len(value) == 2 and isinstance(value[1], list):
        if len(value[1]) > TRUSTED_DERIVED_CACHE_MAX_CONTAINER_ITEMS:
            raise ValueError("A cache tuple has too many items.")
        return tuple(
            _restore_value(item, depth=depth + 1, budget=budget) for item in value[1]
        )
    if tag == "dict" and len(value) == 2 and isinstance(value[1], list):
        if len(value[1]) > TRUSTED_DERIVED_CACHE_MAX_CONTAINER_ITEMS:
            raise ValueError("A cache dictionary has too many items.")
        restored_dict: dict[str, object] = {}
        for item in value[1]:
            if (
                not isinstance(item, list)
                or len(item) != 2
                or not isinstance(item[0], str)
                or item[0] in restored_dict
            ):
                raise ValueError("A cache dictionary item is invalid.")
            _valid_text(item[0], maximum=256)
            restored_dict[item[0]] = _restore_value(
                item[1], depth=depth + 1, budget=budget
            )
        return restored_dict
    raise ValueError("A cache value has an unknown canonical tag.")


def canonical_trusted_cache_key(key: TrustedCacheWorkKey) -> bytes:
    """Return a type-preserving deterministic cache-key encoding."""

    if not isinstance(key, TrustedCacheWorkKey):
        raise TypeError("key must be TrustedCacheWorkKey.")
    value = (
        "qplot-trusted-cache-key-v1",
        (
            key.database_instance.logical_path,
            key.database_instance.resolved_path,
            cast(tuple[object, ...], key.database_instance.identity),
        ),
        key.run_guid,
        int(key.kind),
        key.source_revision.fingerprint,
        key.renderer_version,
        key.rendering_options.canonical_values,
    )
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def trusted_cache_filename(key: TrustedCacheWorkKey) -> str:
    return f"{hashlib.sha256(canonical_trusted_cache_key(key)).hexdigest()}.qdc"


class _JsonPreflightParser:
    """Count JSON structure without materialising its container graph."""

    def __init__(self, text: str) -> None:
        self._text = text
        self._length = len(text)
        self._position = 0
        self._nodes = 0

    def parse(self) -> None:
        self._skip_whitespace()
        self._parse_value(depth=0)
        self._skip_whitespace()
        if self._position != self._length:
            raise ValueError("A cache JSON document has trailing content.")

    def _skip_whitespace(self) -> None:
        while self._position < self._length and self._text[self._position] in " \t\r\n":
            self._position += 1

    def _count_node(self) -> None:
        self._nodes += 1
        if self._nodes > TRUSTED_DERIVED_CACHE_MAX_JSON_NODES:
            raise ValueError("A cache JSON document has too many nodes.")

    def _parse_value(self, *, depth: int, binary: bool = False) -> str | None:
        self._skip_whitespace()
        if self._position >= self._length:
            raise ValueError("A cache JSON document is incomplete.")
        self._count_node()
        character = self._text[self._position]
        if character == '"':
            return self._parse_string(binary=binary)
        if character == "[":
            self._parse_array(depth=depth + 1)
            return None
        if character == "{":
            self._parse_object(depth=depth + 1)
            return None
        start = self._position
        while (
            self._position < self._length
            and self._text[self._position] not in " \t\r\n,]}"
        ):
            self._position += 1
        if self._position == start:
            raise ValueError("A cache JSON scalar is invalid.")
        return None

    def _parse_array(self, *, depth: int) -> None:
        if depth > TRUSTED_DERIVED_CACHE_MAX_JSON_DEPTH:
            raise ValueError("A cache JSON document is too deeply nested.")
        self._position += 1
        self._skip_whitespace()
        count = 0
        first_value: str | None = None
        while self._position >= self._length or self._text[self._position] != "]":
            count += 1
            if count > TRUSTED_DERIVED_CACHE_MAX_CONTAINER_ITEMS:
                raise ValueError("A cache JSON container has too many items.")
            value = self._parse_value(
                depth=depth,
                binary=count == 2 and first_value == "bytes",
            )
            if count == 1:
                first_value = value
            self._skip_whitespace()
            if self._position >= self._length:
                raise ValueError("A cache JSON document is incomplete.")
            if self._text[self._position] == "]":
                break
            if self._text[self._position] != ",":
                raise ValueError("A cache JSON array is invalid.")
            self._position += 1
            self._skip_whitespace()
        self._position += 1

    def _parse_object(self, *, depth: int) -> None:
        if depth > TRUSTED_DERIVED_CACHE_MAX_JSON_DEPTH:
            raise ValueError("A cache JSON document is too deeply nested.")
        self._position += 1
        self._skip_whitespace()
        count = 0
        while self._position >= self._length or self._text[self._position] != "}":
            count += 1
            if count > TRUSTED_DERIVED_CACHE_MAX_CONTAINER_ITEMS:
                raise ValueError("A cache JSON container has too many items.")
            if self._position >= self._length or self._text[self._position] != '"':
                raise ValueError("A cache JSON object key is invalid.")
            self._parse_string(binary=False)
            self._skip_whitespace()
            if self._position >= self._length or self._text[self._position] != ":":
                raise ValueError("A cache JSON object is invalid.")
            self._position += 1
            self._parse_value(depth=depth)
            self._skip_whitespace()
            if self._position >= self._length:
                raise ValueError("A cache JSON document is incomplete.")
            if self._text[self._position] == "}":
                break
            if self._text[self._position] != ",":
                raise ValueError("A cache JSON object is invalid.")
            self._position += 1
            self._skip_whitespace()
        self._position += 1

    def _parse_string(self, *, binary: bool) -> str | None:
        start = self._position + 1
        self._position = start
        escaped = False
        byte_count = 0
        while self._position < self._length:
            character = self._text[self._position]
            if not escaped and character == '"':
                break
            if binary:
                if escaped or character == "\\" or not character.isascii():
                    raise ValueError("A cache byte scalar is invalid.")
                if not (character.isalnum() or character in "+/="):
                    raise ValueError("A cache byte scalar is invalid.")
                byte_count += 1
            else:
                byte_count += len(character.encode("utf-8"))
                if byte_count > TRUSTED_DERIVED_CACHE_MAX_TEXT_BYTES:
                    raise ValueError("A cache JSON string is oversized.")
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            self._position += 1
        if self._position >= self._length or escaped:
            raise ValueError("A cache JSON document is incomplete.")
        end = self._position
        self._position += 1
        if binary:
            maximum_encoded = 4 * ((TRUSTED_DERIVED_MAX_ENCODED_IMAGE_BYTES + 2) // 3)
            if byte_count > maximum_encoded or byte_count % 4:
                raise ValueError("A cache byte scalar is oversized.")
            padding = int(byte_count > 0 and self._text[end - 1] == "=")
            padding += int(byte_count > 1 and self._text[end - 2] == "=")
            if self._text.find("=", start, end - padding) != -1:
                raise ValueError("A cache byte scalar is invalid.")
            decoded_bytes = byte_count // 4 * 3 - padding
            if decoded_bytes > TRUSTED_DERIVED_MAX_ENCODED_IMAGE_BYTES:
                raise ValueError("A cache byte scalar is oversized.")
            return None
        if end - start == 5 and self._text.startswith("bytes", start):
            return "bytes"
        return None


def _preflight_json(data: bytes) -> None:
    """Bound JSON structure and canonical binary strings before ``json.loads``."""

    if len(data) > TRUSTED_DERIVED_CACHE_MAX_ENTRY_BYTES:
        raise ValueError("A cache JSON document is oversized.")
    _JsonPreflightParser(data.decode("utf-8")).parse()
