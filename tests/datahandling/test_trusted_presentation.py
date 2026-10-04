from __future__ import annotations

import pytest

from qplot.datahandling.trusted_live_queries import (
    TrustedParameterView,
    bounded_parameter_presentation,
)
from qplot.datahandling.trusted_presentation import (
    TRUSTED_PRESENTATION_MAX_COMPATIBILITY_TEXT_BYTES,
    TRUSTED_PRESENTATION_MAX_CONTAINER_ITEMS,
    TRUSTED_PRESENTATION_MAX_DEPTH,
    TRUSTED_PRESENTATION_MAX_ERROR_BYTES,
    TRUSTED_PRESENTATION_MAX_KEY_BYTES,
    TRUSTED_PRESENTATION_MAX_METADATA_FIELDS,
    TRUSTED_PRESENTATION_MAX_RENDERED_NODES,
    TRUSTED_PRESENTATION_MAX_RENDERED_TEXT_BYTES,
    TRUSTED_PRESENTATION_MAX_TOOLTIP_BYTES,
    TRUSTED_PRESENTATION_MAX_TOOLTIP_TEXT_BYTES,
    TRUSTED_PRESENTATION_MAX_UNAVAILABLE_FIELDS,
    TRUSTED_PRESENTATION_MAX_VALUE_BYTES,
    bounded_metadata_fields,
    bounded_presentation_error,
    bounded_presentation_names,
    build_selected_run_presentation,
    normalize_presentation_tree,
)


def _ascii_value(total_bytes: int, *, prefix: str, suffix: str) -> str:
    padding = total_bytes - len(prefix.encode("utf-8")) - len(suffix.encode("utf-8"))
    assert padding >= 0
    value = f"{prefix}{'x' * padding}{suffix}"
    assert len(value.encode("utf-8")) == total_bytes
    return value


def _node_with_key(view, key: str):
    return next(node for node in view.nodes if node.key == key)


def _assert_view_bounded(view) -> None:
    assert len(view.nodes) <= TRUSTED_PRESENTATION_MAX_RENDERED_NODES
    assert view.inspected_items <= TRUSTED_PRESENTATION_MAX_CONTAINER_ITEMS
    assert view.rendered_text_bytes <= TRUSTED_PRESENTATION_MAX_RENDERED_TEXT_BYTES
    assert view.tooltip_text_bytes <= TRUSTED_PRESENTATION_MAX_TOOLTIP_TEXT_BYTES
    assert all(
        len(node.key.encode("utf-8")) <= TRUSTED_PRESENTATION_MAX_KEY_BYTES
        for node in view.nodes
    )
    assert all(
        len(node.value.encode("utf-8")) <= TRUSTED_PRESENTATION_MAX_VALUE_BYTES
        for node in view.nodes
    )
    assert all(
        len(node.tooltip.encode("utf-8")) <= TRUSTED_PRESENTATION_MAX_TOOLTIP_BYTES
        for node in view.nodes
    )
    assert all(
        node.parent_index is None or 0 <= node.parent_index < index
        for index, node in enumerate(view.nodes)
    )


def test_deep_nested_presentation_is_iterative_and_explicitly_truncated() -> None:
    nested: object = "leaf"
    for _index in range(TRUSTED_PRESENTATION_MAX_DEPTH + 10_000):
        nested = {"child": nested}

    view = normalize_presentation_tree(nested)

    assert view.status == "truncated"
    assert "nesting" in view.message
    assert any(node.key == "[truncated]" for node in view.nodes)
    _assert_view_bounded(view)


def test_wide_mapping_stops_at_fixed_nodes_and_items() -> None:
    value = {f"dynamic-{index}": index for index in range(20_000)}

    view = normalize_presentation_tree(value)

    assert view.status == "truncated"
    assert any(node.key == "[truncated]" for node in view.nodes)
    _assert_view_bounded(view)


def test_shared_empty_tuple_alias_is_not_reported_as_a_cycle() -> None:
    shared: tuple[()] = ()

    view = normalize_presentation_tree(
        {
            "Run": {"shape": shared},
            "Derived metadata": {"planned_shape": shared},
        }
    )

    assert view.status == "available"
    assert "cyclic" not in view.message.lower()
    assert [node.key for node in view.nodes] == [
        "Run",
        "shape",
        "Derived metadata",
        "planned_shape",
    ]
    assert all("cyclic" not in node.value.lower() for node in view.nodes)
    _assert_view_bounded(view)


@pytest.mark.parametrize("indirect", (False, True))
def test_genuine_container_cycles_remain_bounded(indirect: bool) -> None:
    root: dict[str, object] = {}
    if indirect:
        child: dict[str, object] = {"back": root}
        root["child"] = child
    else:
        root["self"] = root

    view = normalize_presentation_tree(root)

    assert view.status == "truncated"
    assert "cyclic container" in view.message.lower()
    assert any("cyclic container" in node.value.lower() for node in view.nodes)
    _assert_view_bounded(view)


def test_scalar_display_shortening_is_local_and_retains_exact_backing_values() -> None:
    run_description = _ascii_value(
        1_258,
        prefix='{"interdependencies_":{"payload":"',
        suffix='"}}',
    )
    measurement_exception = _ascii_value(
        1_255,
        prefix="Traceback (most recent call last):\n",
        suffix="\nKeyboardInterrupt",
    )

    presentation = build_selected_run_presentation(
        run_fields={
            "run_id": 2,
            "guid": "guid-2",
            "run_description": run_description,
            "measurement_exception": measurement_exception,
        },
        metadata_fields={"operator": "Ada"},
        parameters=(),
        snapshot_summary={"Status": "available"},
        setpoint_summaries=(),
        unavailable_fields=(),
    )

    assert presentation.raw.status == "available"
    assert not any(node.key == "[truncated]" for node in presentation.raw.nodes)
    assert presentation.raw.shortened_value_count == 2
    summary = _node_with_key(presentation.raw, "[display]")
    assert "2 values shortened for display" in summary.value

    run_description_node = _node_with_key(presentation.raw, "run_description")
    exception_node = _node_with_key(presentation.raw, "measurement_exception")
    assert run_description_node.value_shortened
    assert exception_node.value_shortened
    assert run_description_node.path.endswith("/run_description")
    assert exception_node.path.endswith("/measurement_exception")
    assert "1258 UTF-8 bytes" in run_description_node.tooltip
    assert "1255 UTF-8 bytes" in exception_node.tooltip
    assert "activate" in run_description_node.tooltip.lower()
    assert "KeyboardInterrupt" in exception_node.value

    full_values = {value.identifier: value.text for value in presentation.full_values}
    assert full_values[run_description_node.full_value_id] == run_description
    assert full_values[exception_node.full_value_id] == measurement_exception
    assert len(full_values) == 2
    _assert_view_bounded(presentation.raw)


def test_multiple_shortened_rows_have_exact_local_markers_and_stable_paths() -> None:
    values = {f"field-{index}": str(index) * 600 for index in range(3)}

    presentation = build_selected_run_presentation(
        run_fields={"run_id": 3, "guid": "guid-3"},
        metadata_fields=values,
        parameters=(),
        snapshot_summary={"Status": "available"},
        setpoint_summaries=(),
        unavailable_fields=(),
    )

    assert presentation.metadata.status == "available"
    assert presentation.metadata.shortened_value_count == 3
    assert not any(node.key == "[truncated]" for node in presentation.metadata.nodes)
    shortened = [node for node in presentation.metadata.nodes if node.value_shortened]
    assert [node.key for node in shortened] == list(values)
    assert len({node.path for node in shortened}) == 3
    assert all(node.full_value_id for node in shortened)
    assert all("[view full]" in node.value for node in shortened)
    assert (
        "3 values shortened" in _node_with_key(presentation.metadata, "[display]").value
    )


def test_long_key_and_value_mark_their_exact_cells_and_retain_both_texts() -> None:
    long_key = "metadata-key-" + "k" * 600
    long_value = "metadata-value-" + "v" * 600

    presentation = build_selected_run_presentation(
        run_fields={"run_id": 31, "guid": "guid-31"},
        metadata_fields={long_key: long_value},
        parameters=(),
        snapshot_summary={"Status": "available"},
        setpoint_summaries=(),
        unavailable_fields=(),
    )

    node = next(node for node in presentation.metadata.nodes if node.key_shortened)
    assert presentation.metadata.status == "available"
    assert presentation.metadata.shortened_key_count == 1
    assert presentation.metadata.shortened_value_count == 1
    assert "[view full key]" in node.key
    assert "[view full]" in node.value
    assert "key: 613 UTF-8 bytes" in node.tooltip
    assert "value: 615 UTF-8 bytes" in node.tooltip
    assert node.full_key_id is not None
    assert node.full_value_id is not None
    assert node.full_key_id != node.full_value_id

    full_values = {value.identifier: value.text for value in presentation.full_values}
    assert full_values[node.full_key_id] == long_key
    assert full_values[node.full_value_id] == long_value
    _assert_view_bounded(presentation.metadata)


def test_local_summary_and_structural_aggregate_marker_both_fit_hard_limits() -> None:
    metadata = {f"aggregate-{index}": f"{index}-" + "a" * 600 for index in range(400)}

    presentation = build_selected_run_presentation(
        run_fields={"run_id": 32, "guid": "guid-32"},
        metadata_fields=metadata,
        parameters=(),
        snapshot_summary={"Status": "available"},
        setpoint_summaries=(),
        unavailable_fields=(),
    )

    assert presentation.metadata.status == "truncated"
    assert presentation.metadata.shortened_value_count > 1
    assert _node_with_key(presentation.metadata, "[display]")
    marker = _node_with_key(presentation.metadata, "[truncated]")
    assert "presentation text limit" in marker.value
    referenced = {
        identifier
        for view in (presentation.metadata, presentation.raw)
        for node in view.nodes
        for identifier in (node.full_key_id, node.full_value_id)
        if identifier is not None
    }
    assert {value.identifier for value in presentation.full_values} == referenced
    _assert_view_bounded(presentation.metadata)


def test_upstream_unavailable_fields_remain_global_omissions_not_local_shortening() -> (
    None
):
    presentation = build_selected_run_presentation(
        run_fields={"run_id": 4, "guid": "guid-4", "name": "complete"},
        metadata_fields={"operator": "Ada"},
        parameters=(),
        snapshot_summary={"Status": "unavailable"},
        setpoint_summaries=(),
        unavailable_fields=("snapshot",),
    )

    assert presentation.raw.status == "truncated"
    assert presentation.raw.shortened_value_count == 0
    marker = _node_with_key(presentation.raw, "[truncated]")
    assert "unavailable upstream" in marker.value.lower()
    assert not presentation.full_values
    _assert_view_bounded(presentation.raw)


@pytest.mark.parametrize(
    ("snapshot_status", "parameters_truncated", "expected"),
    (
        ("available", True, "presentation limits"),
        ("truncated", False, "snapshot source data"),
        ("malformed", False, "snapshot source data"),
        ("unavailable", False, "snapshot source data"),
    ),
)
def test_structural_provenance_is_explicit_without_unavailable_field_names(
    snapshot_status: str,
    parameters_truncated: bool,
    expected: str,
) -> None:
    presentation = build_selected_run_presentation(
        run_fields={"run_id": 41, "guid": "guid-41"},
        metadata_fields={},
        parameters=(),
        snapshot_summary={"Status": snapshot_status},
        setpoint_summaries=(),
        unavailable_fields=(),
        parameters_truncated=parameters_truncated,
    )

    assert presentation.raw.status == "truncated"
    marker = _node_with_key(presentation.raw, "[truncated]")
    assert expected in marker.value.lower()
    _assert_view_bounded(presentation.raw)


def test_multibyte_shortening_reports_utf8_bytes_not_characters() -> None:
    long_key = "é" * 200
    long_value = "界" * 200

    presentation = build_selected_run_presentation(
        run_fields={"run_id": 42, "guid": "guid-42"},
        metadata_fields={long_key: long_value},
        parameters=(),
        snapshot_summary={"Status": "available"},
        setpoint_summaries=(),
        unavailable_fields=(),
    )

    node = next(node for node in presentation.metadata.nodes if node.key_shortened)
    assert "key: 400 UTF-8 bytes" in node.tooltip
    assert "value: 600 UTF-8 bytes" in node.tooltip
    full_values = {value.identifier: value for value in presentation.full_values}
    assert full_values[node.full_key_id].utf8_bytes == 400
    assert full_values[node.full_value_id].utf8_bytes == 600
    assert full_values[node.full_key_id].text == long_key
    assert full_values[node.full_value_id].text == long_value


def test_near_limit_strings_are_not_retained_in_cells_or_tooltips() -> None:
    raw = "private-run-description-" * 175_000
    assert len(raw.encode("utf-8")) > 3 * 1024 * 1024

    presentation = build_selected_run_presentation(
        run_fields={
            "run_id": 7,
            "guid": "guid-7",
            "run_description": raw,
            "name": raw,
        },
        metadata_fields={"dynamic": raw},
        parameters=(),
        snapshot_summary={"Status": "available"},
        setpoint_summaries=(),
        unavailable_fields=(),
    )

    assert "run_description" not in dict(presentation.run_fields)
    assert raw not in dict(presentation.run_fields).values()
    assert raw not in dict(presentation.metadata_fields).values()
    assert all(raw not in node.value for node in presentation.metadata.nodes)
    assert all(raw not in node.tooltip for node in presentation.metadata.nodes)
    assert all(raw not in node.value for node in presentation.raw.nodes)
    assert all(raw not in node.tooltip for node in presentation.raw.nodes)
    assert presentation.metadata.status == "truncated"
    assert presentation.raw.status == "truncated"
    _assert_view_bounded(presentation.metadata)
    _assert_view_bounded(presentation.raw)


def test_nested_dynamic_metadata_gets_bounded_compatibility_and_tree_views() -> None:
    presentation = build_selected_run_presentation(
        run_fields={"run_id": 8, "guid": "guid-8"},
        metadata_fields={
            "nested": {"values": list(range(10_000))},
            "binary": b"x" * (2 * 1024 * 1024),
            "oversized-key-" * 10_000: "bounded value",
        },
        parameters=(),
        snapshot_summary={"Status": "available"},
        setpoint_summaries=(),
        unavailable_fields=(),
    )

    metadata_fields = dict(presentation.metadata_fields)
    assert metadata_fields["nested"] == "[nested value omitted; see Raw tab]"
    assert metadata_fields["binary"] == "[binary value omitted: 2097152 bytes]"
    assert all(len(name.encode("utf-8")) <= 256 for name in metadata_fields)
    assert presentation.metadata.status == "truncated"
    _assert_view_bounded(presentation.metadata)
    _assert_view_bounded(presentation.raw)


def test_compatibility_markers_consume_hard_byte_and_item_budgets() -> None:
    tuple_value = tuple("x" * 512 for _index in range(32))
    metadata = {
        **{f"tuple-{index}": tuple_value for index in range(3)},
        **{f"scalar-{index}": "y" * 512 for index in range(100)},
    }

    bounded = bounded_metadata_fields(metadata)
    retained_bytes = sum(
        len(name.encode("utf-8"))
        + sum(len(str(item).encode("utf-8")) for item in value)
        if isinstance(value, tuple)
        else len(name.encode("utf-8")) + len(str(value).encode("utf-8"))
        for name, value in bounded
    )

    assert len(bounded) <= TRUSTED_PRESENTATION_MAX_METADATA_FIELDS
    assert retained_bytes <= TRUSTED_PRESENTATION_MAX_COMPATIBILITY_TEXT_BYTES
    assert bounded[-1][0].startswith("[truncated]")

    unavailable, truncated = bounded_presentation_names(
        tuple(f"field-{index}" for index in range(10_000))
    )
    assert truncated
    assert len(unavailable) <= TRUSTED_PRESENTATION_MAX_UNAVAILABLE_FIELDS
    assert unavailable[-1] == "[additional unavailable fields omitted]"


def test_error_publication_text_is_bounded() -> None:
    marker = "private-error-value-" * 100_000

    bounded = bounded_presentation_error(RuntimeError(marker))

    assert len(bounded.encode("utf-8")) <= TRUSTED_PRESENTATION_MAX_ERROR_BYTES
    assert marker not in bounded


def test_parameter_omission_marker_covers_every_presentation_limit() -> None:
    cases = (
        (
            TrustedParameterView(
                "parameter",
                "long-label-" * 100,
                "V",
                (),
                "numeric",
            ),
        ),
        (
            TrustedParameterView(
                "parameter",
                "label",
                "V",
                tuple(f"axis-{index}" for index in range(33)),
                "numeric",
            ),
        ),
        tuple(
            TrustedParameterView(
                f"parameter-{index}",
                "l" * 256,
                "u" * 256,
                tuple("a" * 256 for _axis in range(8)),
                "t" * 256,
            )
            for index in range(256)
        ),
        tuple(
            TrustedParameterView(f"parameter-{index}", "", "", (), "numeric")
            for index in range(257)
        ),
    )

    for raw_parameters in cases:
        _parameters, truncated = bounded_parameter_presentation(raw_parameters)
        assert truncated
        presentation = build_selected_run_presentation(
            run_fields={"run_id": 7},
            metadata_fields={},
            parameters=(),
            snapshot_summary={"Status": "empty"},
            setpoint_summaries=(),
            unavailable_fields=("parameters.presentation",),
            parameters_truncated=truncated,
        )
        marker = next(
            node.value
            for node in presentation.raw.nodes
            if node.key == "Parameters status"
        )
        assert "presentation limits" in marker
        assert "256-parameter limit" not in marker
