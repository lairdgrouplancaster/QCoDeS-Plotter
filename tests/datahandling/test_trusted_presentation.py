from __future__ import annotations

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
