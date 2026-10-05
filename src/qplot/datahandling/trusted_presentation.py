"""Bounded selected-detail presentation models created before Qt publication."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, TypeAlias

TRUSTED_PRESENTATION_MAX_DEPTH = 16
TRUSTED_PRESENTATION_MAX_CONTAINER_ITEMS = 2_048
TRUSTED_PRESENTATION_MAX_RENDERED_NODES = 512
TRUSTED_PRESENTATION_MAX_RENDERED_TEXT_BYTES = 64 * 1024
TRUSTED_PRESENTATION_MAX_TOOLTIP_TEXT_BYTES = 64 * 1024
TRUSTED_PRESENTATION_MAX_KEY_BYTES = 256
TRUSTED_PRESENTATION_MAX_VALUE_BYTES = 256
TRUSTED_PRESENTATION_MAX_TOOLTIP_BYTES = 1_024
TRUSTED_PRESENTATION_MAX_RUN_FIELDS = 64
TRUSTED_PRESENTATION_MAX_METADATA_FIELDS = 64
TRUSTED_PRESENTATION_MAX_SEQUENCE_ITEMS = 32
TRUSTED_PRESENTATION_MAX_FIELD_VALUE_BYTES = 512
TRUSTED_PRESENTATION_MAX_COMPATIBILITY_TEXT_BYTES = 64 * 1024
TRUSTED_PRESENTATION_MAX_PARAMETERS = 256
TRUSTED_PRESENTATION_MAX_PARAMETER_TOTAL_TEXT_BYTES = 128 * 1024
TRUSTED_PRESENTATION_MAX_PARAMETER_TEXT_BYTES = 256
TRUSTED_PRESENTATION_MAX_PARAMETER_DEPENDENCIES = 32
TRUSTED_PRESENTATION_MAX_UNAVAILABLE_FIELDS = 256
TRUSTED_PRESENTATION_MAX_ERROR_BYTES = 1_024
TRUSTED_PRESENTATION_MAX_FULL_VALUE_TEXT_BYTES = 4 * 1024 * 1024
TRUSTED_PRESENTATION_MAX_PATH_BYTES = 1_024

_FINAL_ROW_COUNT = 2
_TEXT_RESERVE_BYTES = _FINAL_ROW_COUNT * (
    TRUSTED_PRESENTATION_MAX_KEY_BYTES + TRUSTED_PRESENTATION_MAX_VALUE_BYTES
)
_TOOLTIP_RESERVE_BYTES = _FINAL_ROW_COUNT * TRUSTED_PRESENTATION_MAX_TOOLTIP_BYTES
_TRUNCATION_KEY = "[truncated]"
_DISPLAY_KEY = "[display]"
_VIEW_FULL_SUFFIX = " ... [view full]"
_VIEW_FULL_KEY_SUFFIX = " ... [view full key]"
_RUN_TRUNCATION_FIELD = (
    "presentation_status",
    "Some run fields were truncated; see Raw tab.",
)
_METADATA_TRUNCATION_VALUE = "Additional or oversized metadata is shown as truncated."

PresentationScalar: TypeAlias = None | bool | int | float | str
PresentationValue: TypeAlias = PresentationScalar | tuple[PresentationScalar, ...]
FrozenPresentationFields: TypeAlias = tuple[tuple[str, PresentationValue], ...]
PresentationStatus: TypeAlias = Literal["available", "empty", "truncated"]

_SELECTED_RUN_FIELDS = (
    "run_id",
    "exp_id",
    "name",
    "result_table_name",
    "result_counter",
    "run_timestamp",
    "completed_timestamp",
    "is_completed",
    "guid",
    "captured_run_id",
    "captured_counter",
    "database_modified_timestamp",
    "expected_results",
    "expected_results_source",
    "measure_parameters",
    "preview_dimensions",
    "measurement_exception",
    "parameters_truncated",
    "point_shape",
    "read_setpoint_count",
    "result_count",
    "setpoint_count",
    "setpoint_count_source",
    "setpoint_shape",
    "setpoint_shape_source",
    "storage_bytes",
    "storage_bytes_estimated",
    "sweep_parameters",
    "exp_name",
    "sample_name",
)


@dataclass(frozen=True, slots=True)
class TrustedPresentationNode:
    """One bounded tree row with a separately bounded tooltip."""

    key: str
    value: str
    tooltip: str
    parent_index: int | None
    path: str = ""
    key_shortened: bool = False
    value_shortened: bool = False
    full_key_id: str | None = None
    full_value_id: str | None = None
    source_key_bytes: int = 0
    source_value_bytes: int = 0


@dataclass(frozen=True, slots=True)
class TrustedPresentationFullValue:
    """One exact scalar retained once for the current bounded detail."""

    identifier: str
    text: str = field(repr=False)
    utf8_bytes: int


@dataclass(frozen=True, slots=True)
class TrustedPresentationView:
    """Immutable flat tree safe for iterative construction by Qt."""

    nodes: tuple[TrustedPresentationNode, ...]
    status: PresentationStatus
    message: str
    inspected_items: int
    rendered_text_bytes: int
    tooltip_text_bytes: int
    shortened_key_count: int = 0
    shortened_value_count: int = 0


@dataclass(frozen=True, slots=True)
class TrustedSelectedRunPresentation:
    """All non-snapshot selected-detail values that may cross into Qt."""

    run_fields: FrozenPresentationFields
    metadata_fields: FrozenPresentationFields
    metadata: TrustedPresentationView
    raw: TrustedPresentationView
    parameters_truncated: bool = False
    full_values: tuple[TrustedPresentationFullValue, ...] = field(
        default=(),
        repr=False,
    )


@dataclass(slots=True)
class _NodeBuilder:
    key: str
    value: str
    tooltip: str
    parent_index: int | None
    path: str = ""
    key_shortened: bool = False
    value_shortened: bool = False
    full_key_id: str | None = None
    full_value_id: str | None = None
    source_key_bytes: int = 0
    source_value_bytes: int = 0


@dataclass(frozen=True, slots=True)
class _Task:
    key: object
    value: object
    parent_index: int | None
    depth: int
    root: bool = False
    path: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _ExitContainer:
    container_id: int


class _FullValueRegistry:
    """Deduplicate exact selected-detail strings under one aggregate bound."""

    def __init__(self) -> None:
        self._values_by_identity: dict[int, TrustedPresentationFullValue] = {}
        self._values: list[TrustedPresentationFullValue] = []
        self.text_bytes = 0

    def retain(self, text: str) -> TrustedPresentationFullValue | None:
        retained = self._values_by_identity.get(id(text))
        if retained is not None and retained.text is text:
            return retained
        encoded = text.encode("utf-8", errors="replace")
        if (
            len(encoded) > TRUSTED_PRESENTATION_MAX_FULL_VALUE_TEXT_BYTES
            or self.text_bytes + len(encoded)
            > TRUSTED_PRESENTATION_MAX_FULL_VALUE_TEXT_BYTES
        ):
            return None
        identifier = f"value:{len(self._values)}"
        retained = TrustedPresentationFullValue(identifier, text, len(encoded))
        self._values_by_identity[id(text)] = retained
        self._values.append(retained)
        self.text_bytes += len(encoded)
        return retained

    def checkpoint(self) -> int:
        return len(self._values)

    def rollback(self, checkpoint: int) -> None:
        while len(self._values) > checkpoint:
            retained = self._values.pop()
            if self._values_by_identity.get(id(retained.text)) is retained:
                del self._values_by_identity[id(retained.text)]
            self.text_bytes -= retained.utf8_bytes

    def freeze(self) -> tuple[TrustedPresentationFullValue, ...]:
        return tuple(self._values)


class _PresentationNormalizer:
    def __init__(
        self,
        value: object,
        *,
        full_values: _FullValueRegistry | None = None,
        path_prefix: str = "",
        initial_reasons: Sequence[str] = (),
    ) -> None:
        self.value = value
        self.full_values = full_values
        self.path_prefix = _bounded_path_prefix(path_prefix)
        self.nodes: list[_NodeBuilder] = []
        self.inspected_items = 0
        self.rendered_text_bytes = 0
        self.tooltip_text_bytes = 0
        self.reasons: list[str] = list(dict.fromkeys(initial_reasons))
        self.shortened_key_count = 0
        self.shortened_value_count = 0
        self.ancestor_containers: set[int] = set()
        self.halted = False

    def normalize(self) -> TrustedPresentationView:
        tasks: list[_Task | _ExitContainer] = [
            _Task("Value", self.value, None, 0, True, ())
        ]
        while tasks and not self.halted:
            if (
                len(self.nodes)
                >= TRUSTED_PRESENTATION_MAX_RENDERED_NODES - _FINAL_ROW_COUNT
            ):
                self._reason("The 512-node rendering limit was reached.")
                break
            task = tasks.pop()
            if isinstance(task, _ExitContainer):
                self.ancestor_containers.discard(task.container_id)
                continue
            if _is_container(task.value):
                self._schedule_container(task, tasks)
            else:
                self._add_scalar(task)

        if tasks:
            self._reason("Additional values were omitted from the bounded view.")
        if self.shortened_key_count or self.shortened_value_count:
            self._append_display_summary()
        if self.reasons:
            self._append_marker()
            status: PresentationStatus = "truncated"
            message = " ".join(dict.fromkeys(self.reasons))
        elif self.nodes:
            status = "available"
            if self.shortened_key_count or self.shortened_value_count:
                message = self._display_summary_text()
            else:
                message = "Selected detail normalized within all presentation limits."
        else:
            self._add_node("No data", "", "", None)
            status = "empty"
            message = "No selected-detail values were available."
        return TrustedPresentationView(
            nodes=tuple(
                TrustedPresentationNode(
                    node.key,
                    node.value,
                    node.tooltip,
                    node.parent_index,
                    node.path,
                    node.key_shortened,
                    node.value_shortened,
                    node.full_key_id,
                    node.full_value_id,
                    node.source_key_bytes,
                    node.source_value_bytes,
                )
                for node in self.nodes
            ),
            status=status,
            message=message,
            inspected_items=self.inspected_items,
            rendered_text_bytes=self.rendered_text_bytes,
            tooltip_text_bytes=self.tooltip_text_bytes,
            shortened_key_count=self.shortened_key_count,
            shortened_value_count=self.shortened_value_count,
        )

    def _schedule_container(
        self,
        task: _Task,
        tasks: list[_Task | _ExitContainer],
    ) -> None:
        container_id = id(task.value)
        if container_id in self.ancestor_containers:
            self._add_node(
                task.key,
                "[cyclic container unavailable]",
                "[cyclic container unavailable]",
                task.parent_index,
                path=task.path,
            )
            self._reason("A cyclic container was omitted.")
            return
        self.ancestor_containers.add(container_id)
        tasks.append(_ExitContainer(container_id))

        parent_index = task.parent_index
        if not task.root:
            parent_index = self._add_node(
                task.key,
                "",
                "",
                task.parent_index,
                path=task.path,
            )
            if parent_index is None:
                return
        if task.depth >= TRUSTED_PRESENTATION_MAX_DEPTH:
            self._reason("The 16-level nesting limit was reached.")
            self._add_node(
                _TRUNCATION_KEY,
                "Nested values omitted.",
                "Nested values omitted.",
                parent_index,
                path=(*task.path, _TRUNCATION_KEY),
            )
            return

        children, has_more = self._bounded_children(task.value)
        if has_more:
            self._reason("The 2048-item container inspection limit was reached.")
        for key, value in reversed(children):
            tasks.append(
                _Task(
                    key=key,
                    value=value,
                    parent_index=parent_index,
                    depth=task.depth + 1,
                    path=(*task.path, _path_segment(key)),
                )
            )

    def _bounded_children(
        self, value: object
    ) -> tuple[list[tuple[object, object]], bool]:
        remaining = TRUSTED_PRESENTATION_MAX_CONTAINER_ITEMS - self.inspected_items
        if remaining <= 0:
            return [], True
        if isinstance(value, Mapping):
            iterator = iter(value.items())
        else:
            iterator = enumerate(value)  # type: ignore[arg-type]

        children: list[tuple[object, object]] = []
        for _index in range(remaining + 1):
            try:
                child = next(iterator)
            except StopIteration:
                return children, False
            if len(children) >= remaining:
                return children, True
            children.append(child)
            self.inspected_items += 1
        return children, False

    def _add_scalar(self, task: _Task) -> None:
        text = _scalar_text(task.value)
        node_key = "Value" if task.root else task.key
        full_value = task.value if isinstance(task.value, str) else None
        if isinstance(task.value, bytes) or not (
            task.value is None or isinstance(task.value, (bool, int, float, str))
        ):
            self._reason("A binary or unsupported scalar value was unavailable.")
        self._add_node(
            node_key,
            text,
            text,
            task.parent_index,
            path=task.path or (_path_segment(node_key),),
            full_value=full_value,
            head_and_tail=_key_text(node_key).casefold()
            in {"measurement_exception", "exception", "traceback"},
        )

    def _add_node(
        self,
        key: object,
        value: str,
        tooltip: str,
        parent_index: int | None,
        *,
        path: tuple[str, ...] = (),
        full_value: str | None = None,
        head_and_tail: bool = False,
    ) -> int | None:
        if (
            len(self.nodes)
            >= TRUSTED_PRESENTATION_MAX_RENDERED_NODES - _FINAL_ROW_COUNT
        ):
            self._reason("The 512-node rendering limit was reached.")
            self.halted = True
            return None

        source_key = _key_text(key)
        source_value = full_value if full_value is not None else value
        source_key_bytes = len(source_key.encode("utf-8", errors="replace"))
        source_value_bytes = len(source_value.encode("utf-8", errors="replace"))
        backing_checkpoint = (
            self.full_values.checkpoint() if self.full_values is not None else 0
        )
        key_shortened = source_key_bytes > TRUSTED_PRESENTATION_MAX_KEY_BYTES
        value_shortened = bool(
            full_value is not None
            and (
                source_value != value
                or len(value.encode("utf-8", errors="replace"))
                > TRUSTED_PRESENTATION_MAX_VALUE_BYTES
            )
        )
        full_key_id = None
        full_value_id = None
        if key_shortened:
            retained_key = self._retain_full_text(source_key)
            if retained_key is not None:
                full_key_id = retained_key.identifier
            else:
                self._reason(
                    "A complete scalar key was omitted at the 4194304-byte "
                    "selected-detail backing limit."
                )
        if value_shortened:
            retained_value = self._retain_full_text(source_value)
            if retained_value is not None:
                full_value_id = retained_value.identifier
            else:
                self._reason(
                    "A complete scalar value was omitted at the 4194304-byte "
                    "selected-detail backing limit."
                )

        if key_shortened:
            key_text = _shortened_key_text(
                source_key,
                TRUSTED_PRESENTATION_MAX_KEY_BYTES,
                available=full_key_id is not None,
            )
        else:
            key_text = source_key
        if value_shortened:
            value_text = _shortened_value_text(
                value,
                TRUSTED_PRESENTATION_MAX_VALUE_BYTES,
                head_and_tail=head_and_tail,
                available=full_value_id is not None,
            )
        else:
            value_text, _value_cell_truncated = _truncate_utf8(
                value, TRUSTED_PRESENTATION_MAX_VALUE_BYTES
            )
        if key_shortened or value_shortened:
            parts = []
            actions = []
            if key_shortened:
                parts.append(f"key: {source_key_bytes} UTF-8 bytes")
                if full_key_id is not None:
                    actions.append(
                        "Activate the marked key cell to view or explicitly copy "
                        "the complete key."
                    )
                else:
                    actions.append(
                        "The complete key was not retained at the bounded detail limit."
                    )
            if value_shortened:
                parts.append(f"value: {source_value_bytes} UTF-8 bytes")
                if full_value_id is not None:
                    actions.append(
                        "Activate the marked value cell to view or explicitly copy "
                        "the complete value."
                    )
                else:
                    actions.append(
                        "The complete value was not retained at the bounded detail "
                        "limit."
                    )
            tooltip_text, _tooltip_truncated = _truncate_utf8(
                f"Shortened for display ({'; '.join(parts)}). {' '.join(actions)}",
                TRUSTED_PRESENTATION_MAX_TOOLTIP_BYTES,
            )
        else:
            tooltip_text, _tooltip_truncated = _truncate_utf8(
                tooltip, TRUSTED_PRESENTATION_MAX_TOOLTIP_BYTES
            )

        rendered_remaining = (
            TRUSTED_PRESENTATION_MAX_RENDERED_TEXT_BYTES
            - _TEXT_RESERVE_BYTES
            - self.rendered_text_bytes
        )
        tooltip_remaining = (
            TRUSTED_PRESENTATION_MAX_TOOLTIP_TEXT_BYTES
            - _TOOLTIP_RESERVE_BYTES
            - self.tooltip_text_bytes
        )
        key_bytes = len(key_text.encode("utf-8"))
        value_bytes = len(value_text.encode("utf-8"))
        tooltip_bytes = len(tooltip_text.encode("utf-8"))
        if (
            rendered_remaining < key_bytes + value_bytes
            or tooltip_remaining < tooltip_bytes
        ):
            if self.full_values is not None:
                self.full_values.rollback(backing_checkpoint)
            self._reason("The 65536-byte presentation text limit was reached.")
            self.halted = True
            return None

        node_index = len(self.nodes)
        self.nodes.append(
            _NodeBuilder(
                key_text,
                value_text,
                tooltip_text,
                parent_index,
                _bounded_path(self.path_prefix, path),
                key_shortened,
                value_shortened,
                full_key_id,
                full_value_id,
                source_key_bytes,
                source_value_bytes,
            )
        )
        self.rendered_text_bytes += key_bytes + value_bytes
        self.tooltip_text_bytes += tooltip_bytes
        self.shortened_key_count += int(key_shortened)
        self.shortened_value_count += int(value_shortened)
        return node_index

    def _retain_full_text(self, text: str) -> TrustedPresentationFullValue | None:
        if self.full_values is None:
            return None
        return self.full_values.retain(text)

    def _display_summary_text(self) -> str:
        parts = []
        if self.shortened_key_count:
            noun = "key" if self.shortened_key_count == 1 else "keys"
            parts.append(f"{self.shortened_key_count} {noun}")
        if self.shortened_value_count:
            noun = "value" if self.shortened_value_count == 1 else "values"
            parts.append(f"{self.shortened_value_count} {noun}")
        subject = " and ".join(parts)
        return (
            f"{subject} shortened for display. Marked rows identify the affected "
            "text; activate rows labelled 'view full' to view or explicitly copy "
            "the complete text."
        )

    def _append_display_summary(self) -> None:
        if len(self.nodes) >= TRUSTED_PRESENTATION_MAX_RENDERED_NODES:
            return
        message, _ = _truncate_utf8(
            self._display_summary_text(),
            TRUSTED_PRESENTATION_MAX_VALUE_BYTES,
        )
        tooltip, _ = _truncate_utf8(message, _TOOLTIP_RESERVE_BYTES)
        self.nodes.append(
            _NodeBuilder(
                _DISPLAY_KEY,
                message,
                tooltip,
                None,
                _bounded_path(self.path_prefix, (_DISPLAY_KEY,)),
            )
        )
        self.rendered_text_bytes += len(_DISPLAY_KEY.encode("utf-8")) + len(
            message.encode("utf-8")
        )
        self.tooltip_text_bytes += len(tooltip.encode("utf-8"))

    def _append_marker(self) -> None:
        if len(self.nodes) >= TRUSTED_PRESENTATION_MAX_RENDERED_NODES:
            return
        message = " ".join(dict.fromkeys(self.reasons))
        message, _ = _truncate_utf8(
            message,
            TRUSTED_PRESENTATION_MAX_VALUE_BYTES,
        )
        tooltip, _ = _truncate_utf8(message, _TOOLTIP_RESERVE_BYTES)
        self.nodes.append(
            _NodeBuilder(
                _TRUNCATION_KEY,
                message,
                tooltip,
                None,
                _bounded_path(self.path_prefix, (_TRUNCATION_KEY,)),
            )
        )
        self.rendered_text_bytes += len(_TRUNCATION_KEY.encode("utf-8")) + len(
            message.encode("utf-8")
        )
        self.tooltip_text_bytes += len(tooltip.encode("utf-8"))

    def _reason(self, message: str) -> None:
        if message not in self.reasons:
            self.reasons.append(message)


def _is_container(value: object) -> bool:
    return isinstance(value, (Mapping, list, tuple))


def _key_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return str(value)
    return f"<{type(value).__name__} key>"


def _scalar_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return f"[binary value omitted: {len(value)} bytes]"
    if isinstance(value, str):
        return value.replace("\r", " ").replace("\n", " ")
    if isinstance(value, float):
        return f"{value:.6g}"
    if isinstance(value, (bool, int)):
        return str(value)
    return f"[{type(value).__name__} value unavailable]"


def _bounded_path_prefix(value: str) -> str:
    if not value:
        return ""
    return _bounded_path("", (_path_segment(value),))


def _path_segment(value: object) -> str:
    text = _key_text(value).replace("~", "~0").replace("/", "~1")
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= TRUSTED_PRESENTATION_MAX_KEY_BYTES:
        return text
    digest = hashlib.sha256(encoded).hexdigest()[:16]
    prefix, _ = _truncate_utf8(
        text,
        TRUSTED_PRESENTATION_MAX_KEY_BYTES - len(digest) - 2,
    )
    return f"{prefix}~{digest}"


def _bounded_path(prefix: str, segments: Sequence[str]) -> str:
    joined = "/".join((*((prefix.lstrip("/"),) if prefix else ()), *segments))
    path = f"/{joined}" if joined else "/"
    encoded = path.encode("utf-8", errors="replace")
    if len(encoded) <= TRUSTED_PRESENTATION_MAX_PATH_BYTES:
        return path
    digest = hashlib.sha256(encoded).hexdigest()
    retained, _ = _truncate_utf8(
        path,
        TRUSTED_PRESENTATION_MAX_PATH_BYTES - len(digest) - 2,
    )
    return f"{retained}~{digest}"


def _utf8_tail(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return text
    tail = encoded[-limit:]
    while tail:
        try:
            return tail.decode("utf-8")
        except UnicodeDecodeError as error:
            tail = tail[error.end :]
    return ""


def _shortened_value_text(
    text: str,
    limit: int,
    *,
    head_and_tail: bool,
    available: bool,
) -> str:
    suffix = _VIEW_FULL_SUFFIX if available else " ... [complete value unavailable]"
    suffix_bytes = len(suffix.encode("utf-8"))
    if suffix_bytes >= limit:
        return _truncate_utf8(suffix, limit)[0]
    body_limit = limit - suffix_bytes
    if not head_and_tail:
        prefix, _ = _truncate_utf8(text, body_limit)
        return f"{prefix}{suffix}"

    separator = " ... "
    separator_bytes = len(separator.encode("utf-8"))
    if separator_bytes >= body_limit:
        prefix, _ = _truncate_utf8(text, body_limit)
        return f"{prefix}{suffix}"
    remaining = body_limit - separator_bytes
    head_limit = max(1, remaining // 3)
    tail_limit = remaining - head_limit
    head, _ = _truncate_utf8(text, head_limit)
    if head.endswith("..."):
        head = head[:-3]
    tail = _utf8_tail(text, tail_limit)
    return f"{head}{separator}{tail}{suffix}"


def _shortened_key_text(text: str, limit: int, *, available: bool) -> str:
    suffix = _VIEW_FULL_KEY_SUFFIX if available else " ... [complete key unavailable]"
    suffix_bytes = len(suffix.encode("utf-8"))
    if suffix_bytes >= limit:
        return _truncate_utf8(suffix, limit)[0]
    prefix, _ = _truncate_utf8(text, limit - suffix_bytes)
    if prefix.endswith("..."):
        prefix = prefix[:-3]
    return f"{prefix}{suffix}"


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


def _bounded_field_value(value: object) -> tuple[PresentationValue, bool]:
    if value is None or isinstance(value, (bool, int, float)):
        return value, False
    if isinstance(value, str):
        return _truncate_utf8(value, TRUSTED_PRESENTATION_MAX_FIELD_VALUE_BYTES)
    if isinstance(value, bytes):
        return f"[binary value omitted: {len(value)} bytes]", True
    if isinstance(value, (list, tuple)):
        output: list[PresentationScalar] = []
        truncated = len(value) > TRUSTED_PRESENTATION_MAX_SEQUENCE_ITEMS
        for item in value[:TRUSTED_PRESENTATION_MAX_SEQUENCE_ITEMS]:
            if item is None or isinstance(item, (bool, int, float)):
                output.append(item)
            elif isinstance(item, str):
                bounded, was_truncated = _truncate_utf8(
                    item, TRUSTED_PRESENTATION_MAX_FIELD_VALUE_BYTES
                )
                output.append(bounded)
                truncated = truncated or was_truncated
            elif isinstance(item, bytes):
                output.append(f"[binary value omitted: {len(item)} bytes]")
                truncated = True
            else:
                output.append("[nested value omitted; see Raw tab]")
                truncated = True
        return tuple(output), truncated
    return "[nested value omitted; see Raw tab]", True


def bounded_presentation_text(
    value: object,
    *,
    limit: int = TRUSTED_PRESENTATION_MAX_PARAMETER_TEXT_BYTES,
) -> tuple[str, bool]:
    """Format one table identifier/label without retaining unbounded text."""

    if isinstance(value, bytes):
        return f"[binary value omitted: {len(value)} bytes]", True
    if not isinstance(value, str):
        value = "" if value is None else str(value)
    return _truncate_utf8(value, limit)


def bounded_presentation_scalar(
    value: object,
) -> tuple[PresentationScalar, bool]:
    """Bound one scalar used by a Qt cell and its tooltip."""

    if value is None or isinstance(value, (bool, int, float)):
        return value, False
    if isinstance(value, bytes):
        return f"[binary value omitted: {len(value)} bytes]", True
    if isinstance(value, str):
        return _truncate_utf8(value, TRUSTED_PRESENTATION_MAX_FIELD_VALUE_BYTES)
    return f"[{type(value).__name__} value unavailable]", True


def bounded_presentation_names(
    values: Sequence[object],
) -> tuple[tuple[str, ...], bool]:
    """Bound an identifier list retained by the GUI-delivered detail object."""

    output: list[str] = []
    truncated = len(values) > TRUSTED_PRESENTATION_MAX_UNAVAILABLE_FIELDS
    for value in values[:TRUSTED_PRESENTATION_MAX_UNAVAILABLE_FIELDS]:
        bounded, was_truncated = bounded_presentation_text(value)
        output.append(bounded)
        truncated = truncated or was_truncated
    if truncated:
        if len(output) >= TRUSTED_PRESENTATION_MAX_UNAVAILABLE_FIELDS:
            output.pop()
        output.append("[additional unavailable fields omitted]")
    return tuple(dict.fromkeys(output)), truncated


def bounded_presentation_error(value: object) -> str:
    """Return one bounded failure string safe for queued Qt publication."""

    try:
        message = str(value).strip()
    except Exception:
        message = f"{type(value).__name__} details are unavailable."
    if not message:
        message = "Run details are unavailable."
    bounded, _truncated = _truncate_utf8(
        message,
        TRUSTED_PRESENTATION_MAX_ERROR_BYTES,
    )
    return bounded


def bounded_selected_run_fields(
    fields: Mapping[str, object],
) -> FrozenPresentationFields:
    """Retain only bounded run fields needed by overview, actions, and rows."""

    output: list[tuple[str, PresentationValue]] = []
    truncated = False
    retained_bytes = 0
    for name in _SELECTED_RUN_FIELDS[:TRUSTED_PRESENTATION_MAX_RUN_FIELDS]:
        if name not in fields:
            continue
        bounded, was_truncated = _bounded_field_value(fields[name])
        field_bytes = len(name.encode("utf-8")) + _presentation_value_bytes(bounded)
        if (
            retained_bytes + field_bytes
            > TRUSTED_PRESENTATION_MAX_COMPATIBILITY_TEXT_BYTES
        ):
            truncated = True
            break
        output.append((name, bounded))
        retained_bytes += field_bytes
        truncated = truncated or was_truncated
    if truncated:
        _make_room_for_field_marker(
            output,
            retained_bytes,
            _RUN_TRUNCATION_FIELD,
            max_items=TRUSTED_PRESENTATION_MAX_RUN_FIELDS,
        )
        output.append(_RUN_TRUNCATION_FIELD)
    return tuple(output)


def bounded_metadata_fields(
    fields: Mapping[str, object],
) -> FrozenPresentationFields:
    """Return a small compatibility mapping without retaining raw metadata."""

    output: list[tuple[str, PresentationValue]] = []
    truncated = len(fields) > TRUSTED_PRESENTATION_MAX_METADATA_FIELDS
    seen_keys: set[str] = set()
    retained_bytes = 0
    for index, (name, value) in enumerate(fields.items()):
        if index >= TRUSTED_PRESENTATION_MAX_METADATA_FIELDS:
            break
        bounded_name, key_truncated = _truncate_utf8(
            _key_text(name), TRUSTED_PRESENTATION_MAX_KEY_BYTES
        )
        if bounded_name in seen_keys:
            truncated = True
            continue
        seen_keys.add(bounded_name)
        bounded_value, value_truncated = _bounded_field_value(value)
        field_bytes = len(bounded_name.encode("utf-8")) + _presentation_value_bytes(
            bounded_value
        )
        if (
            retained_bytes + field_bytes
            > TRUSTED_PRESENTATION_MAX_COMPATIBILITY_TEXT_BYTES
        ):
            truncated = True
            break
        output.append((bounded_name, bounded_value))
        retained_bytes += field_bytes
        truncated = truncated or key_truncated or value_truncated
    if truncated:
        marker_name = _TRUNCATION_KEY
        while marker_name in seen_keys:
            marker_name += "*"
        marker = (marker_name, _METADATA_TRUNCATION_VALUE)
        _make_room_for_field_marker(
            output,
            retained_bytes,
            marker,
            max_items=TRUSTED_PRESENTATION_MAX_METADATA_FIELDS,
        )
        output.append(marker)
    return tuple(output)


def _make_room_for_field_marker(
    output: list[tuple[str, PresentationValue]],
    retained_bytes: int,
    marker: tuple[str, PresentationValue],
    *,
    max_items: int,
) -> None:
    marker_bytes = _presentation_field_bytes(*marker)
    while output and (
        len(output) >= max_items
        or retained_bytes + marker_bytes
        > TRUSTED_PRESENTATION_MAX_COMPATIBILITY_TEXT_BYTES
    ):
        removed_name, removed_value = output.pop()
        retained_bytes -= _presentation_field_bytes(removed_name, removed_value)


def _presentation_field_bytes(name: str, value: PresentationValue) -> int:
    return len(name.encode("utf-8")) + _presentation_value_bytes(value)


def _presentation_value_bytes(value: PresentationValue) -> int:
    if isinstance(value, tuple):
        return sum(len(_scalar_text(item).encode("utf-8")) for item in value)
    return len(_scalar_text(value).encode("utf-8"))


def normalize_presentation_tree(value: object) -> TrustedPresentationView:
    """Flatten a nested value iteratively under all presentation budgets."""

    return _PresentationNormalizer(value).normalize()


def build_selected_run_presentation(
    *,
    run_fields: Mapping[str, object],
    metadata_fields: Mapping[str, object],
    parameters: Sequence[Mapping[str, object]],
    snapshot_summary: Mapping[str, object],
    setpoint_summaries: Sequence[Mapping[str, object]],
    unavailable_fields: Sequence[str],
    parameters_truncated: bool = False,
) -> TrustedSelectedRunPresentation:
    """Build every non-snapshot tree/table input before crossing into Qt."""

    raw_run = {name: value for name, value in run_fields.items() if name != "snapshot"}
    raw_value = {
        "Run": raw_run,
        "Metadata": metadata_fields,
        "Snapshot": snapshot_summary,
        "Parameters": parameters,
        "Setpoint summaries": setpoint_summaries,
        "Unavailable fields": unavailable_fields,
    }
    if parameters_truncated:
        raw_value["Parameters status"] = (
            "Additional or oversized parameter details were omitted at "
            "presentation limits."
        )
    full_values = _FullValueRegistry()
    metadata = _PresentationNormalizer(
        metadata_fields,
        full_values=full_values,
        path_prefix="Metadata",
    ).normalize()
    initial_reasons: list[str] = []
    presentation_omissions = parameters_truncated or any(
        name.endswith(".presentation") for name in unavailable_fields
    )
    upstream_unavailable = any(
        not name.endswith(".presentation") for name in unavailable_fields
    )
    if presentation_omissions:
        initial_reasons.append(
            "One or more selected-run structures were omitted at presentation limits."
        )
    if upstream_unavailable:
        initial_reasons.append(
            "One or more selected-run fields were unavailable upstream; complete "
            "source data was omitted."
        )
    snapshot_status = snapshot_summary.get("Status")
    if isinstance(snapshot_status, str) and snapshot_status.casefold() in {
        "truncated",
        "malformed",
        "unavailable",
    }:
        initial_reasons.append(
            "Snapshot source data was truncated, malformed, or unavailable."
        )
    raw = _PresentationNormalizer(
        raw_value,
        full_values=full_values,
        path_prefix="Raw",
        initial_reasons=initial_reasons,
    ).normalize()
    return TrustedSelectedRunPresentation(
        run_fields=bounded_selected_run_fields(run_fields),
        metadata_fields=bounded_metadata_fields(metadata_fields),
        metadata=metadata,
        raw=raw,
        parameters_truncated=parameters_truncated,
        full_values=full_values.freeze(),
    )


__all__ = [
    "TRUSTED_PRESENTATION_MAX_CONTAINER_ITEMS",
    "TRUSTED_PRESENTATION_MAX_COMPATIBILITY_TEXT_BYTES",
    "TRUSTED_PRESENTATION_MAX_DEPTH",
    "TRUSTED_PRESENTATION_MAX_ERROR_BYTES",
    "TRUSTED_PRESENTATION_MAX_FIELD_VALUE_BYTES",
    "TRUSTED_PRESENTATION_MAX_FULL_VALUE_TEXT_BYTES",
    "TRUSTED_PRESENTATION_MAX_KEY_BYTES",
    "TRUSTED_PRESENTATION_MAX_METADATA_FIELDS",
    "TRUSTED_PRESENTATION_MAX_PARAMETERS",
    "TRUSTED_PRESENTATION_MAX_PATH_BYTES",
    "TRUSTED_PRESENTATION_MAX_PARAMETER_TOTAL_TEXT_BYTES",
    "TRUSTED_PRESENTATION_MAX_PARAMETER_DEPENDENCIES",
    "TRUSTED_PRESENTATION_MAX_PARAMETER_TEXT_BYTES",
    "TRUSTED_PRESENTATION_MAX_RENDERED_NODES",
    "TRUSTED_PRESENTATION_MAX_RENDERED_TEXT_BYTES",
    "TRUSTED_PRESENTATION_MAX_RUN_FIELDS",
    "TRUSTED_PRESENTATION_MAX_SEQUENCE_ITEMS",
    "TRUSTED_PRESENTATION_MAX_TOOLTIP_BYTES",
    "TRUSTED_PRESENTATION_MAX_TOOLTIP_TEXT_BYTES",
    "TRUSTED_PRESENTATION_MAX_UNAVAILABLE_FIELDS",
    "TRUSTED_PRESENTATION_MAX_VALUE_BYTES",
    "TrustedPresentationNode",
    "TrustedPresentationFullValue",
    "TrustedPresentationView",
    "TrustedSelectedRunPresentation",
    "bounded_metadata_fields",
    "bounded_presentation_scalar",
    "bounded_presentation_names",
    "bounded_presentation_error",
    "bounded_presentation_text",
    "bounded_selected_run_fields",
    "build_selected_run_presentation",
    "normalize_presentation_tree",
]
