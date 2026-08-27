"""Bounded, Qt-independent decoding of selected-run snapshot JSON."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from json import decoder as json_decoder
from typing import Literal, TypeAlias, cast

TRUSTED_SNAPSHOT_MAX_INPUT_BYTES = 4 * 1024 * 1024
TRUSTED_SNAPSHOT_MAX_DEPTH = 32
TRUSTED_SNAPSHOT_MAX_CONTAINER_ITEMS = 4_096
TRUSTED_SNAPSHOT_MAX_RENDERED_NODES = 1_024
TRUSTED_SNAPSHOT_MAX_RENDERED_TEXT_BYTES = 64 * 1024
TRUSTED_SNAPSHOT_MAX_NODE_KEY_BYTES = 256
TRUSTED_SNAPSHOT_MAX_NODE_VALUE_BYTES = 1_024
TRUSTED_SNAPSHOT_MAX_PARAMETER_VIEWS = 256
TRUSTED_SNAPSHOT_MAX_PARAMETER_VALUE_BYTES = 512

_MARKER_KEY = "[truncated]"
_MARKER_TEXT_RESERVE_BYTES = 512
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_SCAN_STRING = cast(
    Callable[[str, int, bool], tuple[str, int]],
    json_decoder.scanstring,  # type: ignore[attr-defined]
)
_PARAMETER_FIELDS = frozenset(
    {
        "name",
        "full_name",
        "label",
        "unit",
        "post_delay",
        "instrument_name",
        "instrument",
        "value",
        "raw_value",
    }
)

SnapshotScalar: TypeAlias = None | bool | int | float | str
FrozenSnapshotFields: TypeAlias = tuple[tuple[str, SnapshotScalar], ...]
SnapshotStatus: TypeAlias = Literal[
    "available",
    "empty",
    "truncated",
    "malformed",
    "unavailable",
]
SnapshotOmissionKind: TypeAlias = Literal[
    "payload_limit",
    "detail_budget",
    "changed_during_read",
]


@dataclass(frozen=True, slots=True)
class TrustedSnapshotOmission:
    """Why a present snapshot payload was not delivered to the normalizer."""

    kind: SnapshotOmissionKind
    input_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class TrustedSnapshotNode:
    """One already-formatted tree row; parents always precede children."""

    key: str
    value: str
    parent_index: int | None


@dataclass(frozen=True, slots=True)
class TrustedSnapshotParameterView:
    """Bounded snapshot fields used by the selected-run parameter table."""

    name: str
    fields: FrozenSnapshotFields


@dataclass(frozen=True, slots=True)
class TrustedSnapshotView:
    """Immutable snapshot model safe to pass to and render on the Qt thread."""

    nodes: tuple[TrustedSnapshotNode, ...]
    parameters: tuple[TrustedSnapshotParameterView, ...]
    status: SnapshotStatus
    message: str
    input_bytes: int


@dataclass(slots=True)
class _NodeBuilder:
    key: str
    value: str
    parent_index: int | None


class _SnapshotSyntaxError(ValueError):
    def __init__(self, position: int, message: str) -> None:
        super().__init__(message)
        self.position = max(0, position)


class _SnapshotBudgetExceeded(RuntimeError):
    def __init__(self, message: str, parent_index: int | None) -> None:
        super().__init__(message)
        self.parent_index = parent_index


class _BoundedSnapshotDecoder:
    """Parse only the bounded prefix needed for the published view model."""

    def __init__(self, source: str, input_bytes: int) -> None:
        self.source = source
        self.input_bytes = input_bytes
        self.nodes: list[_NodeBuilder] = []
        self.container_items = 0
        self.rendered_text_bytes = 0
        self.parameter_fields: dict[tuple[str, ...], dict[str, SnapshotScalar]] = {}
        self.parameter_names: dict[tuple[str, ...], str] = {}

    def decode(self) -> TrustedSnapshotView:
        try:
            position = self._skip_space(0)
            if position >= len(self.source):
                raise _SnapshotSyntaxError(position, "Snapshot JSON is empty.")
            position = self._parse_value(
                position,
                key="Value",
                parent_index=None,
                path=(),
                depth=0,
                root=True,
            )
            position = self._skip_space(position)
            if position != len(self.source):
                raise _SnapshotSyntaxError(
                    position, "Unexpected content follows the JSON value."
                )
        except _SnapshotBudgetExceeded as error:
            message = str(error)
            self._append_marker(message, error.parent_index)
            return self._view("truncated", message)
        except (_SnapshotSyntaxError, ValueError) as error:
            position = getattr(error, "position", 0)
            message = f"Malformed snapshot JSON near character {position}."
            return _single_message_view(
                "Snapshot unavailable",
                message,
                status="malformed",
                input_bytes=self.input_bytes,
            )

        if not self.nodes:
            return _single_message_view(
                "Snapshot",
                "The snapshot contains an empty JSON container.",
                status="empty",
                input_bytes=self.input_bytes,
            )
        return self._view("available", "Snapshot decoded within all limits.")

    def _view(self, status: SnapshotStatus, message: str) -> TrustedSnapshotView:
        nodes = tuple(
            TrustedSnapshotNode(node.key, node.value, node.parent_index)
            for node in self.nodes
        )
        return TrustedSnapshotView(
            nodes=nodes,
            parameters=self._frozen_parameters(),
            status=status,
            message=message,
            input_bytes=self.input_bytes,
        )

    def _parse_value(
        self,
        position: int,
        *,
        key: str,
        parent_index: int | None,
        path: tuple[str, ...],
        depth: int,
        root: bool = False,
    ) -> int:
        position = self._skip_space(position)
        if position >= len(self.source):
            raise _SnapshotSyntaxError(position, "A JSON value is missing.")
        token = self.source[position]
        if token in "{[":
            container_depth = depth + 1
            if container_depth > TRUSTED_SNAPSHOT_MAX_DEPTH:
                raise _SnapshotBudgetExceeded(
                    "Snapshot nesting exceeds the 32-level display limit.",
                    parent_index,
                )
            container_parent = parent_index
            if not root:
                container_parent = self._add_node(key, "", parent_index)
            if token == "{":
                return self._parse_object(
                    position,
                    parent_index=container_parent,
                    path=path,
                    depth=container_depth,
                )
            return self._parse_array(
                position,
                parent_index=container_parent,
                path=path,
                depth=container_depth,
            )

        value, display, end = self._parse_scalar(position)
        node_key = key if not root else "Value"
        self._add_node(node_key, display, parent_index)
        self._record_parameter_value(path, value)
        return end

    def _parse_object(
        self,
        position: int,
        *,
        parent_index: int | None,
        path: tuple[str, ...],
        depth: int,
    ) -> int:
        position = self._skip_space(position + 1)
        if position < len(self.source) and self.source[position] == "}":
            return position + 1
        while True:
            self._count_container_item(parent_index)
            if position >= len(self.source) or self.source[position] != '"':
                raise _SnapshotSyntaxError(position, "An object key must be a string.")
            object_key, position = self._scan_string(position)
            position = self._skip_space(position)
            if position >= len(self.source) or self.source[position] != ":":
                raise _SnapshotSyntaxError(position, "An object key needs a value.")
            position = self._parse_value(
                position + 1,
                key=object_key,
                parent_index=parent_index,
                path=(*path, object_key),
                depth=depth,
            )
            position = self._skip_space(position)
            if position >= len(self.source):
                raise _SnapshotSyntaxError(position, "An object is not closed.")
            if self.source[position] == "}":
                return position + 1
            if self.source[position] != ",":
                raise _SnapshotSyntaxError(position, "Object members need a comma.")
            position = self._skip_space(position + 1)

    def _parse_array(
        self,
        position: int,
        *,
        parent_index: int | None,
        path: tuple[str, ...],
        depth: int,
    ) -> int:
        position = self._skip_space(position + 1)
        if position < len(self.source) and self.source[position] == "]":
            return position + 1
        array_index = 0
        while True:
            self._count_container_item(parent_index)
            position = self._parse_value(
                position,
                key=f"[{array_index}]",
                parent_index=parent_index,
                path=(*path, f"[{array_index}]"),
                depth=depth,
            )
            array_index += 1
            position = self._skip_space(position)
            if position >= len(self.source):
                raise _SnapshotSyntaxError(position, "An array is not closed.")
            if self.source[position] == "]":
                return position + 1
            if self.source[position] != ",":
                raise _SnapshotSyntaxError(position, "Array values need a comma.")
            position = self._skip_space(position + 1)

    def _parse_scalar(self, position: int) -> tuple[SnapshotScalar, str, int]:
        token = self.source[position]
        if token == '"':
            value, end = self._scan_string(position)
            return value, value, end
        literals: tuple[tuple[str, SnapshotScalar, str], ...] = (
            ("true", True, "True"),
            ("false", False, "False"),
            ("null", None, ""),
        )
        for literal, literal_value, display in literals:
            if self.source.startswith(literal, position):
                return literal_value, display, position + len(literal)
        match = _NUMBER.match(self.source, position)
        if match is None:
            raise _SnapshotSyntaxError(position, "The JSON value is invalid.")
        end = match.end()
        number_text = match.group(0)
        if len(number_text) <= 128:
            try:
                number_value: SnapshotScalar = (
                    float(number_text)
                    if any(marker in number_text for marker in ".eE")
                    else int(number_text)
                )
            except (OverflowError, ValueError):
                number_value = number_text
        else:
            number_value = number_text
        return number_value, number_text, end

    def _scan_string(self, position: int) -> tuple[str, int]:
        try:
            value, end = _SCAN_STRING(self.source, position + 1, True)
        except (UnicodeDecodeError, ValueError) as error:
            raise _SnapshotSyntaxError(
                position, "The JSON string is invalid."
            ) from error
        return value, end

    def _count_container_item(self, parent_index: int | None) -> None:
        self.container_items += 1
        if self.container_items > TRUSTED_SNAPSHOT_MAX_CONTAINER_ITEMS:
            raise _SnapshotBudgetExceeded(
                "Snapshot containers exceed the 4096-item inspection limit.",
                parent_index,
            )

    def _add_node(self, key: object, value: object, parent_index: int | None) -> int:
        if len(self.nodes) >= TRUSTED_SNAPSHOT_MAX_RENDERED_NODES - 1:
            raise _SnapshotBudgetExceeded(
                "Snapshot display exceeds the 1024-node rendering limit.",
                parent_index,
            )
        key_text, key_truncated = _truncate_utf8(
            str(key), TRUSTED_SNAPSHOT_MAX_NODE_KEY_BYTES
        )
        value_text, value_truncated = _truncate_utf8(
            str(value), TRUSTED_SNAPSHOT_MAX_NODE_VALUE_BYTES
        )
        remaining = (
            TRUSTED_SNAPSHOT_MAX_RENDERED_TEXT_BYTES
            - _MARKER_TEXT_RESERVE_BYTES
            - self.rendered_text_bytes
        )
        if remaining <= 0:
            raise _SnapshotBudgetExceeded(
                "Snapshot display exceeds the 65536-byte rendered-text limit.",
                parent_index,
            )
        key_bytes = len(key_text.encode("utf-8"))
        if key_bytes > remaining:
            key_text, _ = _truncate_utf8(key_text, remaining)
            value_text = ""
            text_truncated = True
        else:
            value_text, text_truncated = _truncate_utf8(
                value_text, remaining - key_bytes
            )
        node_index = len(self.nodes)
        self.nodes.append(_NodeBuilder(key_text, value_text, parent_index))
        self.rendered_text_bytes += len(key_text.encode("utf-8")) + len(
            value_text.encode("utf-8")
        )
        if key_truncated or value_truncated or text_truncated:
            raise _SnapshotBudgetExceeded(
                "Snapshot text was truncated to bounded display limits.",
                parent_index,
            )
        return node_index

    def _append_marker(self, message: str, parent_index: int | None) -> None:
        if len(self.nodes) >= TRUSTED_SNAPSHOT_MAX_RENDERED_NODES:
            return
        marker, _ = _truncate_utf8(message, _MARKER_TEXT_RESERVE_BYTES - 32)
        self.nodes.append(_NodeBuilder(_MARKER_KEY, marker, parent_index))
        self.rendered_text_bytes += len(_MARKER_KEY.encode("utf-8")) + len(
            marker.encode("utf-8")
        )

    def _record_parameter_value(
        self, path: tuple[str, ...], value: SnapshotScalar
    ) -> None:
        location = _parameter_location(path)
        if location is None:
            return
        identity, parameter_name, field = location
        if field not in _PARAMETER_FIELDS:
            return
        if identity not in self.parameter_fields:
            if len(self.parameter_fields) >= TRUSTED_SNAPSHOT_MAX_PARAMETER_VIEWS:
                return
            self.parameter_fields[identity] = {}
            self.parameter_names[identity] = parameter_name
        if isinstance(value, str):
            value, _ = _truncate_utf8(value, TRUSTED_SNAPSHOT_MAX_PARAMETER_VALUE_BYTES)
        self.parameter_fields[identity][field] = value

    def _frozen_parameters(self) -> tuple[TrustedSnapshotParameterView, ...]:
        views: list[TrustedSnapshotParameterView] = []
        seen_names: set[str] = set()
        for identity, fields in self.parameter_fields.items():
            aliases = [self.parameter_names[identity]]
            aliases.extend(
                value
                for field in ("name", "full_name")
                if isinstance((value := fields.get(field)), str) and value
            )
            frozen_fields: FrozenSnapshotFields = tuple(fields.items())
            for alias in aliases:
                bounded_alias, _ = _truncate_utf8(
                    alias, TRUSTED_SNAPSHOT_MAX_PARAMETER_VALUE_BYTES
                )
                if not bounded_alias or bounded_alias in seen_names:
                    continue
                seen_names.add(bounded_alias)
                views.append(TrustedSnapshotParameterView(bounded_alias, frozen_fields))
                if len(views) >= TRUSTED_SNAPSHOT_MAX_PARAMETER_VIEWS:
                    return tuple(views)
        return tuple(views)

    def _skip_space(self, position: int) -> int:
        while position < len(self.source) and self.source[position] in " \t\r\n":
            position += 1
        return position


def _parameter_location(
    path: tuple[str, ...],
) -> tuple[tuple[str, ...], str, str] | None:
    if len(path) == 3 and path[0] == "parameters":
        return path[:2], path[1], path[2]
    if len(path) == 4 and path[:2] == ("station", "parameters"):
        return path[:3], path[2], path[3]
    if (
        len(path) == 6
        and path[:2] == ("station", "instruments")
        and path[3] == "parameters"
    ):
        return path[:5], path[4], path[5]
    return None


def _truncate_utf8(text: str, limit: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return text, False
    if limit <= 0:
        return "", True
    marker = b"..." if limit >= 3 else b""
    prefix = encoded[: max(0, limit - len(marker))]
    while prefix:
        try:
            decoded = prefix.decode("utf-8")
            break
        except UnicodeDecodeError as error:
            prefix = prefix[: error.start]
    else:
        decoded = ""
    return decoded + marker.decode("ascii"), True


def _single_message_view(
    key: str,
    message: str,
    *,
    status: SnapshotStatus,
    input_bytes: int,
) -> TrustedSnapshotView:
    bounded_key, _ = _truncate_utf8(key, TRUSTED_SNAPSHOT_MAX_NODE_KEY_BYTES)
    bounded_message, _ = _truncate_utf8(message, TRUSTED_SNAPSHOT_MAX_NODE_VALUE_BYTES)
    return TrustedSnapshotView(
        nodes=(TrustedSnapshotNode(bounded_key, bounded_message, None),),
        parameters=(),
        status=status,
        message=bounded_message,
        input_bytes=input_bytes,
    )


def normalize_trusted_snapshot(
    snapshot_json: object,
    *,
    omission: TrustedSnapshotOmission | None = None,
) -> TrustedSnapshotView:
    """Decode one snapshot into an immutable, strictly bounded flat view."""

    if omission is not None:
        input_bytes = max(0, omission.input_bytes or 0)
        if omission.kind == "payload_limit":
            size = (
                f"its {input_bytes}-byte payload"
                if omission.input_bytes is not None
                else "its payload"
            )
            message = (
                f"A snapshot was stored, but {size} exceeds the snapshot viewing limit."
            )
        elif omission.kind == "detail_budget":
            message = (
                "A snapshot was stored, but it could not be retained within the "
                "selected-detail viewing budget."
            )
        else:
            message = (
                "A snapshot was stored, but it changed while the bounded reader "
                "was fetching it."
            )
        return _single_message_view(
            "Snapshot unavailable",
            message,
            status="unavailable",
            input_bytes=input_bytes,
        )

    if snapshot_json is None:
        return _single_message_view(
            "Snapshot",
            "No snapshot was stored for this run.",
            status="empty",
            input_bytes=0,
        )
    if not isinstance(snapshot_json, str):
        return _single_message_view(
            "Snapshot unavailable",
            "The stored snapshot is not JSON text.",
            status="unavailable",
            input_bytes=0,
        )
    try:
        input_bytes = len(snapshot_json.encode("utf-8"))
    except UnicodeEncodeError:
        return _single_message_view(
            "Snapshot unavailable",
            "The stored snapshot contains invalid Unicode text.",
            status="malformed",
            input_bytes=0,
        )
    if input_bytes > TRUSTED_SNAPSHOT_MAX_INPUT_BYTES:
        return _single_message_view(
            "Snapshot unavailable",
            "Snapshot JSON exceeds the 4194304-byte decode limit.",
            status="unavailable",
            input_bytes=input_bytes,
        )
    return _BoundedSnapshotDecoder(snapshot_json, input_bytes).decode()


__all__ = [
    "TRUSTED_SNAPSHOT_MAX_CONTAINER_ITEMS",
    "TRUSTED_SNAPSHOT_MAX_DEPTH",
    "TRUSTED_SNAPSHOT_MAX_INPUT_BYTES",
    "TRUSTED_SNAPSHOT_MAX_NODE_KEY_BYTES",
    "TRUSTED_SNAPSHOT_MAX_NODE_VALUE_BYTES",
    "TRUSTED_SNAPSHOT_MAX_PARAMETER_VALUE_BYTES",
    "TRUSTED_SNAPSHOT_MAX_PARAMETER_VIEWS",
    "TRUSTED_SNAPSHOT_MAX_RENDERED_NODES",
    "TRUSTED_SNAPSHOT_MAX_RENDERED_TEXT_BYTES",
    "TrustedSnapshotNode",
    "TrustedSnapshotOmission",
    "TrustedSnapshotParameterView",
    "TrustedSnapshotView",
    "normalize_trusted_snapshot",
]
