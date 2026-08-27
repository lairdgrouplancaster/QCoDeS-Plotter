"""Bounded selected-detail presentation models created before Qt publication."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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

_TEXT_RESERVE_BYTES = 512
_TOOLTIP_RESERVE_BYTES = 512
_TRUNCATION_KEY = "[truncated]"
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


@dataclass(frozen=True, slots=True)
class TrustedPresentationView:
    """Immutable flat tree safe for iterative construction by Qt."""

    nodes: tuple[TrustedPresentationNode, ...]
    status: PresentationStatus
    message: str
    inspected_items: int
    rendered_text_bytes: int
    tooltip_text_bytes: int


@dataclass(frozen=True, slots=True)
class TrustedSelectedRunPresentation:
    """All non-snapshot selected-detail values that may cross into Qt."""

    run_fields: FrozenPresentationFields
    metadata_fields: FrozenPresentationFields
    metadata: TrustedPresentationView
    raw: TrustedPresentationView
    parameters_truncated: bool = False


@dataclass(slots=True)
class _NodeBuilder:
    key: str
    value: str
    tooltip: str
    parent_index: int | None


@dataclass(frozen=True, slots=True)
class _Task:
    key: object
    value: object
    parent_index: int | None
    depth: int
    root: bool = False


class _PresentationNormalizer:
    def __init__(self, value: object) -> None:
        self.value = value
        self.nodes: list[_NodeBuilder] = []
        self.inspected_items = 0
        self.rendered_text_bytes = 0
        self.tooltip_text_bytes = 0
        self.reasons: list[str] = []
        self.seen_containers: set[int] = set()
        self.halted = False

    def normalize(self) -> TrustedPresentationView:
        tasks = [_Task("Value", self.value, None, 0, True)]
        while tasks and not self.halted:
            if len(self.nodes) >= TRUSTED_PRESENTATION_MAX_RENDERED_NODES - 1:
                self._reason("The 512-node rendering limit was reached.")
                break
            task = tasks.pop()
            if _is_container(task.value):
                self._schedule_container(task, tasks)
            else:
                self._add_scalar(task)

        if tasks:
            self._reason("Additional values were omitted from the bounded view.")
        if self.reasons:
            self._append_marker()
            status: PresentationStatus = "truncated"
            message = " ".join(dict.fromkeys(self.reasons))
        elif self.nodes:
            status = "available"
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
                )
                for node in self.nodes
            ),
            status=status,
            message=message,
            inspected_items=self.inspected_items,
            rendered_text_bytes=self.rendered_text_bytes,
            tooltip_text_bytes=self.tooltip_text_bytes,
        )

    def _schedule_container(self, task: _Task, tasks: list[_Task]) -> None:
        container_id = id(task.value)
        if container_id in self.seen_containers:
            self._add_node(
                task.key,
                "[cyclic container unavailable]",
                "[cyclic container unavailable]",
                task.parent_index,
            )
            self._reason("A cyclic container was omitted.")
            return
        self.seen_containers.add(container_id)

        parent_index = task.parent_index
        if not task.root:
            parent_index = self._add_node(task.key, "", "", task.parent_index)
            if parent_index is None:
                return
        if task.depth >= TRUSTED_PRESENTATION_MAX_DEPTH:
            self._reason("The 16-level nesting limit was reached.")
            self._add_node(
                _TRUNCATION_KEY,
                "Nested values omitted.",
                "Nested values omitted.",
                parent_index,
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
        self._add_node(node_key, text, text, task.parent_index)

    def _add_node(
        self,
        key: object,
        value: str,
        tooltip: str,
        parent_index: int | None,
    ) -> int | None:
        if len(self.nodes) >= TRUSTED_PRESENTATION_MAX_RENDERED_NODES - 1:
            self._reason("The 512-node rendering limit was reached.")
            self.halted = True
            return None

        key_text, key_truncated = _truncate_utf8(
            _key_text(key), TRUSTED_PRESENTATION_MAX_KEY_BYTES
        )
        value_text, value_truncated = _truncate_utf8(
            value, TRUSTED_PRESENTATION_MAX_VALUE_BYTES
        )
        tooltip_text, tooltip_truncated = _truncate_utf8(
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
        if rendered_remaining <= key_bytes or tooltip_remaining <= 0:
            self._reason("The 65536-byte presentation text limit was reached.")
            self.halted = True
            return None
        value_text, rendered_total_truncated = _truncate_utf8(
            value_text, rendered_remaining - key_bytes
        )
        tooltip_text, tooltip_total_truncated = _truncate_utf8(
            tooltip_text, tooltip_remaining
        )

        node_index = len(self.nodes)
        self.nodes.append(
            _NodeBuilder(key_text, value_text, tooltip_text, parent_index)
        )
        self.rendered_text_bytes += key_bytes + len(value_text.encode("utf-8"))
        self.tooltip_text_bytes += len(tooltip_text.encode("utf-8"))
        if (
            key_truncated
            or value_truncated
            or tooltip_truncated
            or rendered_total_truncated
            or tooltip_total_truncated
        ):
            self._reason("Keys, values, or tooltips were truncated to display limits.")
        return node_index

    def _append_marker(self) -> None:
        if len(self.nodes) >= TRUSTED_PRESENTATION_MAX_RENDERED_NODES:
            return
        message = " ".join(dict.fromkeys(self.reasons))
        message, _ = _truncate_utf8(message, _TEXT_RESERVE_BYTES - 32)
        tooltip, _ = _truncate_utf8(message, _TOOLTIP_RESERVE_BYTES)
        self.nodes.append(_NodeBuilder(_TRUNCATION_KEY, message, tooltip, None))
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
    return TrustedSelectedRunPresentation(
        run_fields=bounded_selected_run_fields(run_fields),
        metadata_fields=bounded_metadata_fields(metadata_fields),
        metadata=normalize_presentation_tree(metadata_fields),
        raw=normalize_presentation_tree(raw_value),
        parameters_truncated=parameters_truncated,
    )


__all__ = [
    "TRUSTED_PRESENTATION_MAX_CONTAINER_ITEMS",
    "TRUSTED_PRESENTATION_MAX_COMPATIBILITY_TEXT_BYTES",
    "TRUSTED_PRESENTATION_MAX_DEPTH",
    "TRUSTED_PRESENTATION_MAX_ERROR_BYTES",
    "TRUSTED_PRESENTATION_MAX_FIELD_VALUE_BYTES",
    "TRUSTED_PRESENTATION_MAX_KEY_BYTES",
    "TRUSTED_PRESENTATION_MAX_METADATA_FIELDS",
    "TRUSTED_PRESENTATION_MAX_PARAMETERS",
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
