"""Bounded, Qt-independent decoding of selected-run snapshot JSON."""

from __future__ import annotations

import hashlib
import hmac
import math
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from json import decoder as json_decoder
from typing import Literal, TypeAlias, cast

TRUSTED_SNAPSHOT_MAX_INPUT_BYTES = 4 * 1024 * 1024
TRUSTED_SNAPSHOT_MAX_DEPTH = 32
TRUSTED_SNAPSHOT_MAX_CONTAINER_ITEMS = TRUSTED_SNAPSHOT_MAX_INPUT_BYTES
TRUSTED_SNAPSHOT_PAGE_MAX_CHILDREN = 128
TRUSTED_SNAPSHOT_PAGE_MAX_DATA_NODES_WITH_CONTINUATION = 127
TRUSTED_SNAPSHOT_PAGE_MAX_TEXT_BYTES = 32 * 1024
TRUSTED_SNAPSHOT_LOAD_MORE_TEXT = "Load more…"
TRUSTED_SNAPSHOT_MAX_RENDERED_NODES = TRUSTED_SNAPSHOT_PAGE_MAX_CHILDREN
TRUSTED_SNAPSHOT_MAX_RENDERED_TEXT_BYTES = TRUSTED_SNAPSHOT_PAGE_MAX_TEXT_BYTES
TRUSTED_SNAPSHOT_MAX_NODE_KEY_BYTES = 256
TRUSTED_SNAPSHOT_MAX_NODE_VALUE_BYTES = 1_024
TRUSTED_SNAPSHOT_MAX_TOOLTIP_BYTES = 2 * 1024
TRUSTED_SNAPSHOT_MAX_SCALAR_BYTES = 128 * 1024
TRUSTED_SNAPSHOT_MAX_PARAMETER_VIEWS = 256
TRUSTED_SNAPSHOT_MAX_PARAMETER_VALUE_BYTES = 512

_MARKER_KEY = "[truncated]"
_VIEW_FULL_SUFFIX = " [view full]"
_PAGE_MAX_DATA_TEXT_BYTES = TRUSTED_SNAPSHOT_PAGE_MAX_TEXT_BYTES - len(
    TRUSTED_SNAPSHOT_LOAD_MORE_TEXT.encode("utf-8")
)
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
class TrustedSnapshotParameterView:
    """Bounded snapshot fields used by the selected-run parameter table."""

    name: str
    fields: FrozenSnapshotFields


class _SnapshotSyntaxError(ValueError):
    def __init__(self, position: int, message: str) -> None:
        super().__init__(message)
        self.position = max(0, position)


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


def _snapshot_value_preview(text: str) -> tuple[str, bool]:
    """Bound one scalar preview while preserving its exact-value affordance."""

    if len(text.encode("utf-8")) <= TRUSTED_SNAPSHOT_MAX_NODE_VALUE_BYTES:
        return text, False
    prefix_limit = TRUSTED_SNAPSHOT_MAX_NODE_VALUE_BYTES - len(
        _VIEW_FULL_SUFFIX.encode("utf-8")
    )
    prefix, _shortened = _truncate_utf8(text, prefix_limit)
    return prefix + _VIEW_FULL_SUFFIX, True


@dataclass(frozen=True, slots=True)
class TrustedSnapshotContainerHandle:
    """Opaque source-bound reference to one JSON object or array."""

    token: str
    _session: str = field(repr=False)
    _start: int = field(repr=False)
    _end: int = field(repr=False)
    _depth: int = field(repr=False)


@dataclass(frozen=True, slots=True)
class TrustedSnapshotContinuation:
    """Opaque source- and parent-bound cursor for a subsequent child page."""

    token: str
    _session: str = field(repr=False)
    _parent_token: str = field(repr=False)
    _position: int = field(repr=False)
    _item_index: int = field(repr=False)


@dataclass(frozen=True, slots=True)
class TrustedSnapshotFullValueHandle:
    """Opaque source-bound reference to an exact shortened key or scalar."""

    token: str
    _session: str = field(repr=False)
    _start: int = field(repr=False)
    _end: int = field(repr=False)
    _kind: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class TrustedSnapshotNode:
    """One bounded direct-child row returned by the lazy scanner."""

    key: str
    value: str
    parent_index: int | None = None
    tooltip: str = ""
    container_handle: TrustedSnapshotContainerHandle | None = None
    full_key_handle: TrustedSnapshotFullValueHandle | None = None
    full_value_handle: TrustedSnapshotFullValueHandle | None = None
    key_shortened: bool = False
    value_shortened: bool = False
    key_utf8_bytes: int = 0
    value_utf8_bytes: int = 0
    source_position: int = 0
    source_key_bytes: int = 0
    source_value_bytes: int = 0


@dataclass(frozen=True, slots=True)
class TrustedSnapshotPage:
    """A bounded direct-children page which echoes its request identity."""

    parent_handle: TrustedSnapshotContainerHandle | None
    request_continuation: TrustedSnapshotContinuation | None
    nodes: tuple[TrustedSnapshotNode, ...]
    continuation: TrustedSnapshotContinuation | None
    rendered_text_bytes: int
    message: str

    @property
    def parent(self) -> TrustedSnapshotContainerHandle | None:
        return self.parent_handle

    @property
    def cursor(self) -> TrustedSnapshotContinuation | None:
        return self.request_continuation


@dataclass(frozen=True, slots=True)
class TrustedSnapshotFullValue:
    """One exact value resolved away from the Qt owner thread."""

    text: str
    utf8_bytes: int


@dataclass(frozen=True, slots=True)
class TrustedSnapshotView:
    """The initial root page and its exact, Qt-free retained source."""

    page: TrustedSnapshotPage
    parameters: tuple[TrustedSnapshotParameterView, ...]
    status: SnapshotStatus
    message: str
    input_bytes: int
    session_token: str
    root_handle: TrustedSnapshotContainerHandle | None
    continuation: TrustedSnapshotContinuation | None
    source: TrustedSnapshotSource | None = field(repr=False, compare=False)

    @property
    def nodes(self) -> tuple[TrustedSnapshotNode, ...]:
        """Compatibility alias for the initial page's nodes."""

        return self.page.nodes

    @property
    def initial_page(self) -> TrustedSnapshotPage:
        return self.page

    @property
    def session(self) -> str:
        return self.session_token

    @property
    def root(self) -> TrustedSnapshotContainerHandle | None:
        return self.root_handle

    @property
    def cursor(self) -> TrustedSnapshotContinuation | None:
        return self.continuation


@dataclass(frozen=True, slots=True)
class _LazyScalar:
    start: int
    end: int
    kind: str
    value: SnapshotScalar
    display: str


@dataclass(frozen=True, slots=True)
class _LazyValidated:
    start: int
    end: int
    container: bool
    empty_container: bool
    parameters: tuple[TrustedSnapshotParameterView, ...]


class _LazySnapshotLimit(RuntimeError):
    pass


def _lazy_cancel(cancel_check: Callable[[], None] | None) -> None:
    if cancel_check is not None:
        cancel_check()


def _lazy_space(source: str, position: int) -> int:
    while position < len(source) and source[position] in " \t\r\n":
        position += 1
    return position


def _lazy_scan_string(source: str, position: int) -> tuple[str, int]:
    try:
        value, end = _SCAN_STRING(source, position + 1, True)
        value_bytes = len(value.encode("utf-8"))
    except (UnicodeDecodeError, UnicodeEncodeError, ValueError) as error:
        raise _SnapshotSyntaxError(position, "The JSON string is invalid.") from error
    if value_bytes > TRUSTED_SNAPSHOT_MAX_SCALAR_BYTES:
        raise _LazySnapshotLimit(
            "Snapshot scalar text exceeds the 131072-byte inspection limit."
        )
    return value, end


def _lazy_scalar(source: str, position: int) -> _LazyScalar:
    if position >= len(source):
        raise _SnapshotSyntaxError(position, "A JSON value is missing.")
    if source[position] == '"':
        string_value, end = _lazy_scan_string(source, position)
        return _LazyScalar(position, end, "string", string_value, string_value)
    literals: tuple[tuple[str, SnapshotScalar, str, str], ...] = (
        ("true", True, "True", "true"),
        ("false", False, "False", "false"),
        ("null", None, "", "null"),
    )
    for literal, literal_value, display, kind in literals:
        if source.startswith(literal, position):
            return _LazyScalar(
                position,
                position + len(literal),
                kind,
                literal_value,
                display,
            )
    match = _NUMBER.match(source, position)
    if match is None:
        raise _SnapshotSyntaxError(position, "The JSON value is invalid.")
    number_text = match.group(0)
    if len(number_text.encode("utf-8")) > TRUSTED_SNAPSHOT_MAX_SCALAR_BYTES:
        raise _LazySnapshotLimit(
            "Snapshot scalar text exceeds the 131072-byte inspection limit."
        )
    number_value: SnapshotScalar = number_text
    if len(number_text) <= 128:
        try:
            parsed: int | float = (
                float(number_text)
                if any(marker in number_text for marker in ".eE")
                else int(number_text)
            )
            if not isinstance(parsed, float) or math.isfinite(parsed):
                number_value = parsed
        except (OverflowError, ValueError):
            pass
    return _LazyScalar(position, match.end(), "number", number_value, number_text)


class _LazySnapshotValidator:
    """Validate every token while retaining no general-purpose Python tree."""

    def __init__(
        self,
        source: str,
        cancel_check: Callable[[], None] | None,
    ) -> None:
        self.source = source
        self.cancel_check = cancel_check
        self.container_items = 0
        self.parameter_fields: dict[tuple[str, ...], dict[str, SnapshotScalar]] = {}
        self.parameter_names: dict[tuple[str, ...], str] = {}

    def validate(self) -> _LazyValidated:
        _lazy_cancel(self.cancel_check)
        start = _lazy_space(self.source, 0)
        if start >= len(self.source):
            raise _SnapshotSyntaxError(start, "Snapshot JSON is empty.")
        end = self._value(start, path=(), depth=0)
        trailing = _lazy_space(self.source, end)
        if trailing != len(self.source):
            raise _SnapshotSyntaxError(
                trailing, "Unexpected content follows the JSON value."
            )
        _lazy_cancel(self.cancel_check)
        container = self.source[start] in "{["
        child = _lazy_space(self.source, start + 1) if container else start
        empty = container and child < end and self.source[child] in "}]"
        return _LazyValidated(
            start,
            end,
            container,
            empty,
            self._parameters(),
        )

    def _count(self) -> None:
        self.container_items += 1
        if self.container_items > TRUSTED_SNAPSHOT_MAX_CONTAINER_ITEMS:
            raise _LazySnapshotLimit(
                "Snapshot containers exceed the bounded inspection limit."
            )
        if self.container_items % 4_096 == 0:
            _lazy_cancel(self.cancel_check)

    @staticmethod
    def _depth(depth: int) -> None:
        if depth > TRUSTED_SNAPSHOT_MAX_DEPTH:
            raise _LazySnapshotLimit(
                "Snapshot nesting exceeds the 32-level display limit."
            )

    def _value(
        self,
        position: int,
        *,
        path: tuple[str, ...] | None,
        depth: int,
    ) -> int:
        position = _lazy_space(self.source, position)
        if position >= len(self.source):
            raise _SnapshotSyntaxError(position, "A JSON value is missing.")
        if self.source[position] == "{":
            return self._object(position, path=path, depth=depth + 1)
        if self.source[position] == "[":
            return self._array(position, depth=depth + 1)
        scalar = _lazy_scalar(self.source, position)
        self._record(path, scalar.value)
        return scalar.end

    def _object(
        self,
        position: int,
        *,
        path: tuple[str, ...] | None,
        depth: int,
    ) -> int:
        self._depth(depth)
        position = _lazy_space(self.source, position + 1)
        if position < len(self.source) and self.source[position] == "}":
            return position + 1
        while True:
            self._count()
            if position >= len(self.source) or self.source[position] != '"':
                raise _SnapshotSyntaxError(position, "An object key must be a string.")
            key, position = _lazy_scan_string(self.source, position)
            position = _lazy_space(self.source, position)
            if position >= len(self.source) or self.source[position] != ":":
                raise _SnapshotSyntaxError(position, "An object key needs a value.")
            child_path = (*path, key) if path is not None and len(path) < 6 else None
            position = self._value(position + 1, path=child_path, depth=depth)
            position = _lazy_space(self.source, position)
            if position >= len(self.source):
                raise _SnapshotSyntaxError(position, "An object is not closed.")
            if self.source[position] == "}":
                return position + 1
            if self.source[position] != ",":
                raise _SnapshotSyntaxError(position, "Object members need a comma.")
            position = _lazy_space(self.source, position + 1)

    def _array(self, position: int, *, depth: int) -> int:
        self._depth(depth)
        position = _lazy_space(self.source, position + 1)
        if position < len(self.source) and self.source[position] == "]":
            return position + 1
        while True:
            self._count()
            position = self._value(position, path=None, depth=depth)
            position = _lazy_space(self.source, position)
            if position >= len(self.source):
                raise _SnapshotSyntaxError(position, "An array is not closed.")
            if self.source[position] == "]":
                return position + 1
            if self.source[position] != ",":
                raise _SnapshotSyntaxError(position, "Array values need a comma.")
            position = _lazy_space(self.source, position + 1)

    def _record(
        self,
        path: tuple[str, ...] | None,
        value: SnapshotScalar,
    ) -> None:
        if path is None or (location := _parameter_location(path)) is None:
            return
        raw_identity, raw_name, parameter_field = location
        if parameter_field not in _PARAMETER_FIELDS:
            return
        identity = tuple(
            _truncate_utf8(part, TRUSTED_SNAPSHOT_MAX_PARAMETER_VALUE_BYTES)[0]
            for part in raw_identity
        )
        parameter_name = _truncate_utf8(
            raw_name, TRUSTED_SNAPSHOT_MAX_PARAMETER_VALUE_BYTES
        )[0]
        if identity not in self.parameter_fields:
            if len(self.parameter_fields) >= TRUSTED_SNAPSHOT_MAX_PARAMETER_VIEWS:
                return
            self.parameter_fields[identity] = {}
            self.parameter_names[identity] = parameter_name
        if isinstance(value, str):
            value = _truncate_utf8(value, TRUSTED_SNAPSHOT_MAX_PARAMETER_VALUE_BYTES)[0]
        self.parameter_fields[identity][parameter_field] = value

    def _parameters(self) -> tuple[TrustedSnapshotParameterView, ...]:
        views: list[TrustedSnapshotParameterView] = []
        seen: set[str] = set()
        for identity, fields in self.parameter_fields.items():
            aliases = [self.parameter_names[identity]]
            aliases.extend(
                value
                for name in ("name", "full_name")
                if isinstance((value := fields.get(name)), str) and value
            )
            frozen: FrozenSnapshotFields = tuple(fields.items())
            for alias in aliases:
                alias = _truncate_utf8(
                    alias, TRUSTED_SNAPSHOT_MAX_PARAMETER_VALUE_BYTES
                )[0]
                if not alias or alias in seen:
                    continue
                seen.add(alias)
                views.append(TrustedSnapshotParameterView(alias, frozen))
                if len(views) >= TRUSTED_SNAPSHOT_MAX_PARAMETER_VIEWS:
                    return tuple(views)
        return tuple(views)


def _lazy_skip_value(
    source: str,
    position: int,
    *,
    depth: int,
    cancel_check: Callable[[], None] | None,
) -> int:
    """Find one value's end without retaining any descendant values."""

    position = _lazy_space(source, position)
    if position >= len(source):
        raise _SnapshotSyntaxError(position, "A JSON value is missing.")
    token = source[position]
    if token not in "{[":
        return _lazy_scalar(source, position).end
    depth += 1
    if depth > TRUSTED_SNAPSHOT_MAX_DEPTH:
        raise _LazySnapshotLimit("Snapshot nesting exceeds the 32-level display limit.")
    closing = "}" if token == "{" else "]"
    position = _lazy_space(source, position + 1)
    if position < len(source) and source[position] == closing:
        return position + 1
    item_index = 0
    while True:
        item_index += 1
        if item_index % 4_096 == 0:
            _lazy_cancel(cancel_check)
        if token == "{":
            if position >= len(source) or source[position] != '"':
                raise _SnapshotSyntaxError(position, "An object key must be a string.")
            _key, position = _lazy_scan_string(source, position)
            position = _lazy_space(source, position)
            if position >= len(source) or source[position] != ":":
                raise _SnapshotSyntaxError(position, "An object key needs a value.")
            position += 1
        position = _lazy_skip_value(
            source,
            position,
            depth=depth,
            cancel_check=cancel_check,
        )
        position = _lazy_space(source, position)
        if position >= len(source):
            raise _SnapshotSyntaxError(position, "A container is not closed.")
        if source[position] == closing:
            return position + 1
        if source[position] != ",":
            raise _SnapshotSyntaxError(position, "Container items need a comma.")
        position = _lazy_space(source, position + 1)


@dataclass(frozen=True, slots=True)
class TrustedSnapshotSource:
    """One exact bounded source retained outside Qt's item model."""

    session_token: str
    input_bytes: int
    root: TrustedSnapshotContainerHandle | None
    parameters: tuple[TrustedSnapshotParameterView, ...]
    _text: str = field(repr=False, compare=False)
    _secret: bytes = field(repr=False, compare=False)
    _root_scalar: _LazyScalar | None = field(repr=False, compare=False)

    @property
    def session(self) -> str:
        return self.session_token

    @classmethod
    def _create(
        cls,
        text: str,
        input_bytes: int,
        validated: _LazyValidated,
    ) -> TrustedSnapshotSource:
        source = cls(
            session_token=secrets.token_hex(16),
            input_bytes=input_bytes,
            root=None,
            parameters=validated.parameters,
            _text=text,
            _secret=secrets.token_bytes(32),
            _root_scalar=None,
        )
        if validated.container:
            object.__setattr__(
                source,
                "root",
                source._container(validated.start, validated.end, depth=1),
            )
        else:
            object.__setattr__(
                source,
                "_root_scalar",
                _lazy_scalar(text, validated.start),
            )
        return source

    def _signature(self, kind: str, *parts: object) -> str:
        payload = repr((kind, self.session, *parts)).encode("ascii")
        return hmac.new(self._secret, payload, hashlib.sha256).hexdigest()

    def _container(
        self,
        start: int,
        end: int,
        *,
        depth: int,
    ) -> TrustedSnapshotContainerHandle:
        return TrustedSnapshotContainerHandle(
            self._signature("container", start, end, depth),
            self.session,
            start,
            end,
            depth,
        )

    def _next(
        self,
        parent: TrustedSnapshotContainerHandle,
        position: int,
        item_index: int,
    ) -> TrustedSnapshotContinuation:
        return TrustedSnapshotContinuation(
            self._signature("continuation", parent.token, position, item_index),
            self.session,
            parent.token,
            position,
            item_index,
        )

    def _full(
        self,
        start: int,
        end: int,
        kind: str,
    ) -> TrustedSnapshotFullValueHandle:
        return TrustedSnapshotFullValueHandle(
            self._signature("full", start, end, kind),
            self.session,
            start,
            end,
            kind,
        )

    def _check_parent(self, parent: TrustedSnapshotContainerHandle) -> None:
        if not isinstance(parent, TrustedSnapshotContainerHandle):
            raise TypeError("parent must be a TrustedSnapshotContainerHandle.")
        expected = self._signature(
            "container", parent._start, parent._end, parent._depth
        )
        if (
            parent._session != self.session
            or not secrets.compare_digest(parent.token, expected)
            or not 0 <= parent._start < parent._end <= len(self._text)
            or self._text[parent._start] not in "{["
        ):
            raise ValueError("The snapshot container handle is stale or foreign.")

    def _check_cursor(
        self,
        parent: TrustedSnapshotContainerHandle,
        cursor: TrustedSnapshotContinuation,
    ) -> None:
        if not isinstance(cursor, TrustedSnapshotContinuation):
            raise TypeError("cursor must be a TrustedSnapshotContinuation or None.")
        expected = self._signature(
            "continuation",
            parent.token,
            cursor._position,
            cursor._item_index,
        )
        if (
            cursor._session != self.session
            or cursor._parent_token != parent.token
            or not secrets.compare_digest(cursor.token, expected)
            or not parent._start < cursor._position < parent._end
            or cursor._item_index < 0
        ):
            raise ValueError("The snapshot continuation is stale or foreign.")

    def page(
        self,
        parent: TrustedSnapshotContainerHandle,
        cursor: TrustedSnapshotContinuation | None,
        *,
        cancel_check: Callable[[], None] | None = None,
    ) -> TrustedSnapshotPage:
        """Return one page of direct children without constructing a subtree."""

        self._check_parent(parent)
        if cursor is None:
            position = _lazy_space(self._text, parent._start + 1)
            item_index = 0
        else:
            self._check_cursor(parent, cursor)
            position = cursor._position
            item_index = cursor._item_index
        _lazy_cancel(cancel_check)
        closing = "}" if self._text[parent._start] == "{" else "]"
        if position < parent._end and self._text[position] == closing:
            return TrustedSnapshotPage(
                parent,
                cursor,
                (),
                None,
                0,
                "This snapshot container is empty.",
            )

        nodes: list[TrustedSnapshotNode] = []
        rendered_text_bytes = 0
        continuation: TrustedSnapshotContinuation | None = None
        while position < parent._end:
            # The unprocessed item proves a continuation is needed.  Reserving
            # one UI child keeps data rows + Load more at no more than 128.
            if len(nodes) >= TRUSTED_SNAPSHOT_PAGE_MAX_DATA_NODES_WITH_CONTINUATION:
                continuation = self._next(parent, position, item_index)
                break
            if item_index % 4_096 == 0:
                _lazy_cancel(cancel_check)
            item_start = position
            if closing == "}":
                if self._text[position] != '"':
                    raise _SnapshotSyntaxError(
                        position, "An object key must be a string."
                    )
                key_start = position
                exact_key, key_end = _lazy_scan_string(self._text, position)
                position = _lazy_space(self._text, key_end)
                if position >= parent._end or self._text[position] != ":":
                    raise _SnapshotSyntaxError(position, "An object key needs a value.")
                value_start = _lazy_space(self._text, position + 1)
            else:
                exact_key = f"[{item_index}]"
                key_start = -1
                key_end = -1
                value_start = position

            value_end = _lazy_skip_value(
                self._text,
                value_start,
                depth=parent._depth,
                cancel_check=cancel_check,
            )
            node = self._node(
                exact_key,
                key_start,
                key_end,
                value_start,
                value_end,
                child_depth=parent._depth + 1,
            )
            node_bytes = len(node.key.encode("utf-8")) + len(node.value.encode("utf-8"))
            if nodes and (rendered_text_bytes + node_bytes > _PAGE_MAX_DATA_TEXT_BYTES):
                continuation = self._next(parent, item_start, item_index)
                break
            nodes.append(node)
            rendered_text_bytes += node_bytes

            position = _lazy_space(self._text, value_end)
            item_index += 1
            if position >= parent._end:
                raise _SnapshotSyntaxError(position, "A container is not closed.")
            if self._text[position] == closing:
                break
            if self._text[position] != ",":
                raise _SnapshotSyntaxError(position, "Container items need a comma.")
            position = _lazy_space(self._text, position + 1)

        _lazy_cancel(cancel_check)
        return TrustedSnapshotPage(
            parent,
            cursor,
            tuple(nodes),
            continuation,
            rendered_text_bytes,
            (
                "More children are available on demand."
                if continuation is not None
                else "All children in this snapshot container are loaded."
            ),
        )

    def _node(
        self,
        exact_key: str,
        key_start: int,
        key_end: int,
        value_start: int,
        value_end: int,
        *,
        child_depth: int,
    ) -> TrustedSnapshotNode:
        key_utf8_bytes = len(exact_key.encode("utf-8"))
        key, key_shortened = _truncate_utf8(
            exact_key, TRUSTED_SNAPSHOT_MAX_NODE_KEY_BYTES
        )
        full_key = (
            self._full(key_start, key_end, "string")
            if key_shortened and key_start >= 0
            else None
        )
        if self._text[value_start] in "{[":
            value = ""
            value_utf8_bytes = 0
            source_value_bytes = len(self._text[value_start:value_end].encode("utf-8"))
            value_shortened = False
            full_value = None
            container = self._container(
                value_start,
                value_end,
                depth=child_depth,
            )
        else:
            scalar = _lazy_scalar(self._text, value_start)
            if scalar.end != value_end:
                raise _SnapshotSyntaxError(value_start, "A scalar is invalid.")
            value_utf8_bytes = len(scalar.display.encode("utf-8"))
            source_value_bytes = value_utf8_bytes
            value, value_shortened = _snapshot_value_preview(scalar.display)
            full_value = (
                self._full(scalar.start, scalar.end, scalar.kind)
                if value_shortened
                else None
            )
            container = None
        tooltip_parts = []
        if container is not None:
            tooltip_parts.append("Expand to load this container's children on demand.")
        elif value:
            tooltip_parts.append(value)
        if key_shortened:
            tooltip_parts.append(
                f"Key shortened from {key_utf8_bytes} UTF-8 bytes; "
                "exact text is available on demand."
            )
        if value_shortened:
            tooltip_parts.append(
                f"Value shortened from {value_utf8_bytes} UTF-8 bytes; "
                "exact text is available on demand."
            )
        tooltip = _truncate_utf8(
            "\n".join(tooltip_parts), TRUSTED_SNAPSHOT_MAX_TOOLTIP_BYTES
        )[0]
        return TrustedSnapshotNode(
            key=key,
            value=value,
            tooltip=tooltip,
            container_handle=container,
            full_key_handle=full_key,
            full_value_handle=full_value,
            key_shortened=key_shortened,
            value_shortened=value_shortened,
            key_utf8_bytes=key_utf8_bytes,
            value_utf8_bytes=value_utf8_bytes,
            source_position=value_start,
            source_key_bytes=key_utf8_bytes,
            source_value_bytes=source_value_bytes,
        )

    def root_scalar_node(self) -> TrustedSnapshotNode:
        scalar = self._root_scalar
        if scalar is None:
            raise ValueError("This snapshot has a container root.")
        value_utf8_bytes = len(scalar.display.encode("utf-8"))
        value, shortened = _snapshot_value_preview(scalar.display)
        full_value = (
            self._full(scalar.start, scalar.end, scalar.kind) if shortened else None
        )
        tooltip = _truncate_utf8(
            value
            + (
                f"\nValue shortened from {value_utf8_bytes} UTF-8 bytes; "
                "exact text is available on demand."
                if shortened
                else ""
            ),
            TRUSTED_SNAPSHOT_MAX_TOOLTIP_BYTES,
        )[0]
        return TrustedSnapshotNode(
            "Value",
            value,
            tooltip=tooltip,
            full_value_handle=full_value,
            value_shortened=shortened,
            key_utf8_bytes=len(b"Value"),
            value_utf8_bytes=value_utf8_bytes,
            source_position=scalar.start,
            source_key_bytes=len(b"Value"),
            source_value_bytes=value_utf8_bytes,
        )

    def full_value(
        self,
        handle: TrustedSnapshotFullValueHandle,
        *,
        cancel_check: Callable[[], None] | None = None,
    ) -> TrustedSnapshotFullValue:
        """Resolve one exact shortened key or scalar from the retained source."""

        if not isinstance(handle, TrustedSnapshotFullValueHandle):
            raise TypeError("handle must be a TrustedSnapshotFullValueHandle.")
        expected = self._signature("full", handle._start, handle._end, handle._kind)
        if (
            handle._session != self.session
            or not secrets.compare_digest(handle.token, expected)
            or not 0 <= handle._start < handle._end <= len(self._text)
        ):
            raise ValueError("The snapshot full-value handle is stale or foreign.")
        _lazy_cancel(cancel_check)
        if handle._kind == "string":
            value, end = _lazy_scan_string(self._text, handle._start)
            if end != handle._end:
                raise ValueError("The snapshot full-value handle is invalid.")
        else:
            scalar = _lazy_scalar(self._text, handle._start)
            if scalar.end != handle._end or scalar.kind != handle._kind:
                raise ValueError("The snapshot full-value handle is invalid.")
            value = scalar.display
        _lazy_cancel(cancel_check)
        return TrustedSnapshotFullValue(value, len(value.encode("utf-8")))


def _lazy_diagnostic(
    key: str,
    message: str,
    *,
    status: SnapshotStatus,
    input_bytes: int,
) -> TrustedSnapshotView:
    key_text = _truncate_utf8(key, TRUSTED_SNAPSHOT_MAX_NODE_KEY_BYTES)[0]
    value_text = _truncate_utf8(message, TRUSTED_SNAPSHOT_MAX_NODE_VALUE_BYTES)[0]
    node = TrustedSnapshotNode(
        key_text,
        value_text,
        tooltip=_truncate_utf8(message, TRUSTED_SNAPSHOT_MAX_TOOLTIP_BYTES)[0],
        key_utf8_bytes=len(key.encode("utf-8")),
        value_utf8_bytes=len(message.encode("utf-8")),
        source_position=0,
        source_key_bytes=len(key.encode("utf-8")),
        source_value_bytes=len(message.encode("utf-8")),
    )
    rendered = len(key_text.encode("utf-8")) + len(value_text.encode("utf-8"))
    page = TrustedSnapshotPage(None, None, (node,), None, rendered, value_text)
    return TrustedSnapshotView(
        page,
        (),
        status,
        value_text,
        input_bytes,
        "",
        None,
        None,
        None,
    )


def normalize_trusted_snapshot(
    snapshot_json: object,
    *,
    omission: TrustedSnapshotOmission | None = None,
    cancel_check: Callable[[], None] | None = None,
) -> TrustedSnapshotView:
    """Validate a bounded source and return only its initial root page."""

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
        return _lazy_diagnostic(
            "Snapshot unavailable",
            message,
            status="unavailable",
            input_bytes=input_bytes,
        )
    if snapshot_json is None:
        return _lazy_diagnostic(
            "Snapshot",
            "No snapshot was stored for this run.",
            status="empty",
            input_bytes=0,
        )
    if not isinstance(snapshot_json, str):
        return _lazy_diagnostic(
            "Snapshot unavailable",
            "The stored snapshot is not JSON text.",
            status="unavailable",
            input_bytes=0,
        )
    try:
        input_bytes = len(snapshot_json.encode("utf-8"))
    except UnicodeEncodeError:
        return _lazy_diagnostic(
            "Snapshot unavailable",
            "The stored snapshot contains invalid Unicode text.",
            status="malformed",
            input_bytes=0,
        )
    if input_bytes > TRUSTED_SNAPSHOT_MAX_INPUT_BYTES:
        return _lazy_diagnostic(
            "Snapshot unavailable",
            "Snapshot JSON exceeds the 4194304-byte decode limit.",
            status="unavailable",
            input_bytes=input_bytes,
        )
    try:
        validated = _LazySnapshotValidator(snapshot_json, cancel_check).validate()
    except _LazySnapshotLimit as error:
        return _lazy_diagnostic(
            _MARKER_KEY,
            str(error),
            status="truncated",
            input_bytes=input_bytes,
        )
    except (_SnapshotSyntaxError, ValueError) as error:
        return _lazy_diagnostic(
            "Snapshot unavailable",
            f"Malformed snapshot JSON near character {getattr(error, 'position', 0)}.",
            status="malformed",
            input_bytes=input_bytes,
        )

    source = TrustedSnapshotSource._create(snapshot_json, input_bytes, validated)
    if validated.empty_container:
        root = cast(TrustedSnapshotContainerHandle, source.root)
        page = source.page(root, None, cancel_check=cancel_check)
        return TrustedSnapshotView(
            page,
            source.parameters,
            "empty",
            "The snapshot contains an empty JSON container.",
            input_bytes,
            source.session,
            root,
            None,
            source,
        )
    if source.root is None:
        node = source.root_scalar_node()
        rendered = len(node.key.encode("utf-8")) + len(node.value.encode("utf-8"))
        page = TrustedSnapshotPage(
            None,
            None,
            (node,),
            None,
            rendered,
            "The scalar snapshot root is loaded.",
        )
    else:
        page = source.page(source.root, None, cancel_check=cancel_check)
    message = "Snapshot validated; nested content is loaded on demand."
    return TrustedSnapshotView(
        page,
        source.parameters,
        "available",
        message,
        input_bytes,
        source.session,
        source.root,
        page.continuation,
        source,
    )


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
    "TRUSTED_SNAPSHOT_MAX_SCALAR_BYTES",
    "TRUSTED_SNAPSHOT_MAX_TOOLTIP_BYTES",
    "TRUSTED_SNAPSHOT_LOAD_MORE_TEXT",
    "TRUSTED_SNAPSHOT_PAGE_MAX_CHILDREN",
    "TRUSTED_SNAPSHOT_PAGE_MAX_DATA_NODES_WITH_CONTINUATION",
    "TRUSTED_SNAPSHOT_PAGE_MAX_TEXT_BYTES",
    "TrustedSnapshotContainerHandle",
    "TrustedSnapshotContinuation",
    "TrustedSnapshotFullValue",
    "TrustedSnapshotFullValueHandle",
    "TrustedSnapshotNode",
    "TrustedSnapshotOmission",
    "TrustedSnapshotPage",
    "TrustedSnapshotParameterView",
    "TrustedSnapshotSource",
    "TrustedSnapshotView",
    "normalize_trusted_snapshot",
]
