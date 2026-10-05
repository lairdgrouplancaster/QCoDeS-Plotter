from __future__ import annotations

import json

import pytest
from qcodes import Station
from qcodes.parameters import ManualParameter
from qcodes.utils.json_utils import NumpyJSONEncoder

from qplot.datahandling.trusted_snapshot import (
    TRUSTED_SNAPSHOT_LOAD_MORE_TEXT,
    TRUSTED_SNAPSHOT_MAX_DEPTH,
    TRUSTED_SNAPSHOT_MAX_INPUT_BYTES,
    TRUSTED_SNAPSHOT_MAX_NODE_KEY_BYTES,
    TRUSTED_SNAPSHOT_MAX_NODE_VALUE_BYTES,
    TRUSTED_SNAPSHOT_MAX_RENDERED_NODES,
    TRUSTED_SNAPSHOT_MAX_RENDERED_TEXT_BYTES,
    TRUSTED_SNAPSHOT_MAX_SCALAR_BYTES,
    TRUSTED_SNAPSHOT_MAX_TOOLTIP_BYTES,
    TRUSTED_SNAPSHOT_PAGE_MAX_CHILDREN,
    TRUSTED_SNAPSHOT_PAGE_MAX_DATA_NODES_WITH_CONTINUATION,
    TRUSTED_SNAPSHOT_PAGE_MAX_TEXT_BYTES,
    TrustedSnapshotFullValue,
    normalize_trusted_snapshot,
)


def _qcodes_snapshot(value: float) -> str:
    station = Station(
        ManualParameter("sensor", initial_value=value),
        ManualParameter("reference", initial_value=2.5),
    )
    return json.dumps({"station": station.snapshot(update=False)}, cls=NumpyJSONEncoder)


def _rendered_bytes(view) -> int:
    return sum(
        len(node.key.encode("utf-8")) + len(node.value.encode("utf-8"))
        for node in view.nodes
    )


def _page_display_text_bytes(page) -> int:
    return page.rendered_text_bytes + (
        len(TRUSTED_SNAPSHOT_LOAD_MORE_TEXT.encode("utf-8"))
        if page.continuation is not None
        else 0
    )


def _assert_bounded(view) -> None:
    assert len(view.nodes) <= TRUSTED_SNAPSHOT_MAX_RENDERED_NODES
    assert _rendered_bytes(view) <= TRUSTED_SNAPSHOT_MAX_RENDERED_TEXT_BYTES
    assert all(
        len(node.key.encode("utf-8")) <= TRUSTED_SNAPSHOT_MAX_NODE_KEY_BYTES
        for node in view.nodes
    )
    assert all(
        len(node.value.encode("utf-8")) <= TRUSTED_SNAPSHOT_MAX_NODE_VALUE_BYTES
        and len(node.tooltip.encode("utf-8")) <= TRUSTED_SNAPSHOT_MAX_TOOLTIP_BYTES
        for node in view.nodes
    )
    assert view.page.rendered_text_bytes == _rendered_bytes(view)
    assert _page_display_text_bytes(view.page) <= TRUSTED_SNAPSHOT_PAGE_MAX_TEXT_BYTES
    assert len(view.nodes) <= (
        TRUSTED_SNAPSHOT_PAGE_MAX_DATA_NODES_WITH_CONTINUATION
        if view.continuation is not None
        else TRUSTED_SNAPSHOT_PAGE_MAX_CHILDREN
    )


def _all_child_nodes(view, parent):
    assert view.source is not None
    page = (
        view.page
        if view.page.parent_handle == parent and view.page.request_continuation is None
        else view.source.page(parent, None)
    )
    assert page.parent_handle == parent
    assert page.request_continuation is None
    nodes = list(page.nodes)
    cursor = page.continuation
    while cursor is not None:
        page = view.source.page(parent, cursor)
        assert page.parent_handle == parent
        assert page.request_continuation == cursor
        assert page.rendered_text_bytes == sum(
            len(node.key.encode("utf-8")) + len(node.value.encode("utf-8"))
            for node in page.nodes
        )
        assert _page_display_text_bytes(page) <= TRUSTED_SNAPSHOT_PAGE_MAX_TEXT_BYTES
        assert len(page.nodes) <= (
            TRUSTED_SNAPSHOT_PAGE_MAX_DATA_NODES_WITH_CONTINUATION
            if page.continuation is not None
            else TRUSTED_SNAPSHOT_PAGE_MAX_CHILDREN
        )
        nodes.extend(page.nodes)
        cursor = page.continuation
    return tuple(nodes)


def _all_snapshot_nodes(view):
    """Visit nested pages so scalar regressions exercise lazy reads too."""
    nodes = list(_all_child_nodes(view, view.root_handle))
    pending = list(nodes)
    while pending:
        node = pending.pop()
        if node.container_handle is not None:
            children = _all_child_nodes(view, node.container_handle)
            nodes.extend(children)
            pending.extend(children)
    return tuple(nodes)


def _large_qcodes_snapshot_json() -> str:
    parameters = {
        f"gate_{index:04d}": {
            "name": f"gate_{index:04d}",
            "full_name": f"dac_gate_{index:04d}",
            "label": f"Gate {index:04d} " + "x" * 16,
            "unit": "V",
            "value": index / 10,
        }
        for index in range(1_200)
    }
    snapshot = {
        "station": {
            "instruments": {
                "dac": {
                    "parameters": parameters,
                }
            }
        },
        "final_field": "reachable alongside paginated parameter data",
    }
    return json.dumps(snapshot, separators=(",", ":"))


def test_valid_large_qcodes_snapshot_is_not_reported_as_source_truncated() -> None:
    snapshot_json = _large_qcodes_snapshot_json()
    input_bytes = len(snapshot_json.encode("utf-8"))
    assert 130 * 1024 <= input_bytes <= 145 * 1024

    view = normalize_trusted_snapshot(snapshot_json)

    assert view.input_bytes == input_bytes
    assert view.status == "available"
    assert "loaded on demand" in view.message.lower()
    assert view.source is not None
    assert view.session_token == view.source.session_token
    assert view.root_handle is not None
    assert view.page.parent_handle == view.root_handle
    assert [node.key for node in view.nodes] == ["station", "final_field"]
    assert view.nodes[0].container_handle is not None
    assert all(
        len(node.value.encode("utf-8")) <= TRUSTED_SNAPSHOT_MAX_NODE_VALUE_BYTES
        for node in view.nodes
    )
    assert all(
        node.parent_index is None or 0 <= node.parent_index < node_index
        for node_index, node in enumerate(view.nodes)
    )

    station_nodes = _all_child_nodes(view, view.nodes[0].container_handle)
    assert tuple(node.key for node in station_nodes) == ("instruments",)
    instruments = station_nodes[0]
    assert instruments.container_handle is not None
    instrument_nodes = _all_child_nodes(view, instruments.container_handle)
    assert tuple(node.key for node in instrument_nodes) == ("dac",)
    dac = instrument_nodes[0]
    assert dac.container_handle is not None
    dac_nodes = _all_child_nodes(view, dac.container_handle)
    assert tuple(node.key for node in dac_nodes) == ("parameters",)
    parameters = dac_nodes[0]
    assert parameters.container_handle is not None

    parameter_nodes = _all_child_nodes(view, parameters.container_handle)
    parameter_keys = tuple(node.key for node in parameter_nodes)
    assert parameter_keys == tuple(f"gate_{index:04d}" for index in range(1_200))
    assert len(parameter_keys) == len(set(parameter_keys)) == 1_200
    assert parameter_keys[1022:1025] == (
        "gate_1022",
        "gate_1023",
        "gate_1024",
    )
    assert parameter_keys[1199] == "gate_1199"

    final_parameter = parameter_nodes[1199]
    assert final_parameter.container_handle is not None
    final_fields = _all_child_nodes(view, final_parameter.container_handle)
    assert tuple(node.key for node in final_fields) == (
        "name",
        "full_name",
        "label",
        "unit",
        "value",
    )
    final_value = final_fields[-1]
    assert final_value.value == "119.9"


def test_deep_legitimate_json_stops_before_python_or_qt_recursion() -> None:
    depth = TRUSTED_SNAPSHOT_MAX_DEPTH + 10_000
    snapshot_json = '{"child":' * depth + "0" + "}" * depth

    view = normalize_trusted_snapshot(snapshot_json)

    assert view.status == "truncated"
    assert "nesting" in view.message.lower()
    assert view.nodes[-1].key == "[truncated]"
    _assert_bounded(view)


def test_wide_near_scalar_limit_has_a_fixed_rendered_prefix() -> None:
    count = (TRUSTED_SNAPSHOT_MAX_INPUT_BYTES - 4_096 - 3) // 2
    snapshot_json = "[" + "0," * count + "0]"
    input_bytes = len(snapshot_json.encode("utf-8"))
    assert TRUSTED_SNAPSHOT_MAX_INPUT_BYTES - 512 * 1024 < input_bytes
    assert input_bytes <= TRUSTED_SNAPSHOT_MAX_INPUT_BYTES

    view = normalize_trusted_snapshot(snapshot_json)

    assert view.status == "available"
    assert view.input_bytes == input_bytes
    assert view.root_handle is not None
    assert view.continuation is not None
    assert [node.key for node in view.nodes] == [
        f"[{index}]"
        for index in range(TRUSTED_SNAPSHOT_PAGE_MAX_DATA_NODES_WITH_CONTINUATION)
    ]
    second = view.source.page(view.root_handle, view.continuation)
    assert [node.key for node in second.nodes[:3]] == ["[127]", "[128]", "[129]"]
    assert second.request_continuation == view.continuation
    _assert_bounded(view)


def test_oversized_string_never_reaches_a_node_or_parameter_tooltip_whole() -> None:
    oversized = "sensitive-value-" * 10_000
    snapshot_json = (
        '{"station":{"parameters":{"gate":{"full_name":"gate",'
        '"value":"' + oversized + '"}}}}'
    )
    assert len(oversized.encode("utf-8")) > TRUSTED_SNAPSHOT_MAX_SCALAR_BYTES

    view = normalize_trusted_snapshot(snapshot_json)

    assert view.status == "truncated"
    assert view.nodes[-1].key == "[truncated]"
    assert all(oversized not in node.value for node in view.nodes)
    assert all(
        oversized not in str(value)
        for parameter in view.parameters
        for _name, value in parameter.fields
    )
    _assert_bounded(view)


def test_multi_megabyte_malformed_json_returns_only_a_small_diagnostic() -> None:
    snapshot_json = '{"secret":"' + "x" * (2 * 1024 * 1024)

    view = normalize_trusted_snapshot(snapshot_json)

    assert view.status == "malformed"
    assert len(view.nodes) == 1
    assert view.nodes[0].key == "Snapshot unavailable"
    assert "Malformed" in view.nodes[0].value
    assert "x" * 100 not in view.nodes[0].value
    _assert_bounded(view)


def test_blank_stored_snapshot_is_malformed_not_reported_as_sql_null() -> None:
    view = normalize_trusted_snapshot("")

    assert view.status == "malformed"
    assert "Malformed" in view.message
    assert "No snapshot was stored" not in view.message
    _assert_bounded(view)


def test_input_over_byte_limit_is_not_decoded_or_echoed() -> None:
    snapshot_json = '"' + "x" * TRUSTED_SNAPSHOT_MAX_INPUT_BYTES + '"'

    view = normalize_trusted_snapshot(snapshot_json)

    assert view.status == "unavailable"
    assert "4194304-byte" in view.message
    assert len(view.nodes) == 1
    assert "x" * 100 not in view.nodes[0].value
    _assert_bounded(view)


def test_parameter_aliases_are_extracted_into_bounded_plain_fields() -> None:
    snapshot_json = """{
      "station": {
        "instruments": {
          "dac": {
            "parameters": {
              "gate": {
                "name": "gate",
                "full_name": "dac_gate",
                "label": "Gate",
                "unit": "V",
                "post_delay": 0.1,
                "value": 2.5
              }
            }
          }
        }
      }
    }"""

    view = normalize_trusted_snapshot(snapshot_json)
    parameters = {
        parameter.name: dict(parameter.fields) for parameter in view.parameters
    }

    assert view.status == "available"
    assert parameters["gate"]["value"] == 2.5
    assert parameters["dac_gate"]["label"] == "Gate"
    assert parameters["dac_gate"]["post_delay"] == 0.1
    _assert_bounded(view)


@pytest.mark.parametrize(
    ("value", "token"),
    [(float("nan"), "NaN"), (float("inf"), "Infinity"), (float("-inf"), "-Infinity")],
)
def test_qcodes_nonfinite_station_parameter_keeps_snapshot_and_other_parameters(
    value: float, token: str
) -> None:
    snapshot_json = _qcodes_snapshot(value)
    assert f'"value": {token}' in snapshot_json

    view = normalize_trusted_snapshot(snapshot_json)
    parameters = {parameter.name: dict(parameter.fields) for parameter in view.parameters}

    assert view.status == "available"
    assert parameters["sensor"]["value"] == token
    assert parameters["sensor"]["raw_value"] == token
    assert parameters["reference"]["value"] == 2.5
    displayed = [(node.key, node.value) for node in _all_snapshot_nodes(view)]
    assert ("value", token) in displayed
    assert ("value", "2.5") in displayed
    _assert_bounded(view)


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_tokens_work_in_nested_arrays_and_objects(token: str) -> None:
    view = normalize_trusted_snapshot(
        '{"station":{"metadata":{"samples":[' + token + ',{"again":' + token + '}]},'
        '"parameters":{"reference":{"value":1.25}}}}'
    )

    assert view.status == "available"
    assert sum(node.value == token for node in _all_snapshot_nodes(view)) == 2
    assert dict(view.parameters[0].fields)["value"] == 1.25
    _assert_bounded(view)


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize("suffix", ["x", "0", ".1", "_", "e2"])
def test_nonfinite_tokens_require_a_value_boundary(token: str, suffix: str) -> None:
    view = normalize_trusted_snapshot(
        '{"station":{"parameters":{"sensor":{"value":'
        + token + suffix + '},"reference":{"value":2.5}}}}'
    )

    assert view.status == "malformed"
    assert view.parameters == ()
    _assert_bounded(view)


def test_finite_qcodes_station_parameter_remains_numeric() -> None:
    view = normalize_trusted_snapshot(_qcodes_snapshot(-3.25))

    assert view.status == "available"
    assert dict(view.parameters[0].fields)["value"] == -3.25
    assert ("value", "-3.25") in [
        (node.key, node.value) for node in _all_snapshot_nodes(view)
    ]
    _assert_bounded(view)


def test_nonfinite_values_do_not_bypass_snapshot_budgets() -> None:
    token = "Infinity"
    nested = (
        '{"nested":' * (TRUSTED_SNAPSHOT_MAX_DEPTH + 1) + token
        + "}" * (TRUSTED_SNAPSHOT_MAX_DEPTH + 1)
    )
    view = normalize_trusted_snapshot(nested)
    assert view.status == "truncated"
    assert "nesting" in view.message
    _assert_bounded(view)

    # Wide arrays are available through bounded pages, rather than truncated.
    count = TRUSTED_SNAPSHOT_MAX_RENDERED_NODES + 1
    view = normalize_trusted_snapshot("[" + ",".join([token] * count) + "]")
    assert view.status == "available"
    assert view.continuation is not None
    _assert_bounded(view)
    nodes = _all_child_nodes(view, view.root_handle)
    assert len(nodes) == count
    assert all(node.value == token for node in nodes)

    oversized = '["' + "x" * TRUSTED_SNAPSHOT_MAX_INPUT_BYTES + '",' + token + "]"
    view = normalize_trusted_snapshot(oversized)
    assert view.status == "unavailable"
    _assert_bounded(view)


@pytest.mark.parametrize("container_kind", ("object", "array"))
def test_direct_children_paginate_without_omissions_or_duplicates(
    container_kind,
) -> None:
    if container_kind == "object":
        snapshot_json = json.dumps(
            {f"field_{index:04d}": index for index in range(350)},
            separators=(",", ":"),
        )
        expected_keys = tuple(f"field_{index:04d}" for index in range(350))
    else:
        snapshot_json = json.dumps(list(range(350)), separators=(",", ":"))
        expected_keys = tuple(f"[{index}]" for index in range(350))

    view = normalize_trusted_snapshot(snapshot_json)

    assert view.status == "available"
    assert view.root_handle is not None
    nodes = _all_child_nodes(view, view.root_handle)
    assert tuple(node.key for node in nodes) == expected_keys
    assert len(nodes) == len(set(node.key for node in nodes)) == 350


def test_load_more_text_is_reserved_inside_each_page_text_budget() -> None:
    snapshot_json = json.dumps(
        {f"field_{index:04d}": "x" * 1_000 for index in range(80)},
        separators=(",", ":"),
    )

    view = normalize_trusted_snapshot(snapshot_json)

    assert view.status == "available"
    assert view.continuation is not None
    marker_bytes = len(TRUSTED_SNAPSHOT_LOAD_MORE_TEXT.encode("utf-8"))
    assert view.page.rendered_text_bytes + marker_bytes <= (
        TRUSTED_SNAPSHOT_PAGE_MAX_TEXT_BYTES
    )
    assert view.root_handle is not None
    nodes = _all_child_nodes(view, view.root_handle)
    assert tuple(node.key for node in nodes) == tuple(
        f"field_{index:04d}" for index in range(80)
    )


def test_nested_pages_expose_late_root_siblings_and_only_direct_children() -> None:
    view = normalize_trusted_snapshot(_large_qcodes_snapshot_json())
    assert view.source is not None

    station = view.nodes[0]
    assert station.key == "station"
    assert station.container_handle is not None
    station_page = view.source.page(station.container_handle, None)
    assert [node.key for node in station_page.nodes] == ["instruments"]

    instruments = station_page.nodes[0]
    assert instruments.container_handle is not None
    instruments_page = view.source.page(instruments.container_handle, None)
    dac = instruments_page.nodes[0]
    assert dac.key == "dac"
    assert dac.container_handle is not None
    dac_page = view.source.page(dac.container_handle, None)
    parameters = dac_page.nodes[0]
    assert parameters.key == "parameters"
    assert parameters.container_handle is not None
    parameter_page = view.source.page(parameters.container_handle, None)
    assert len(parameter_page.nodes) == (
        TRUSTED_SNAPSHOT_PAGE_MAX_DATA_NODES_WITH_CONTINUATION
    )
    assert parameter_page.nodes[0].key == "gate_0000"
    assert parameter_page.continuation is not None


def test_shortened_key_and_scalar_resolve_exactly_from_repr_hidden_source() -> None:
    exact_key = "κ" * 200
    exact_value = "line-α\r\n" * 1_000
    assert TRUSTED_SNAPSHOT_MAX_NODE_VALUE_BYTES < len(exact_value.encode("utf-8"))
    assert len(exact_value.encode("utf-8")) < TRUSTED_SNAPSHOT_MAX_SCALAR_BYTES
    snapshot_json = json.dumps(
        {exact_key: exact_value},
        ensure_ascii=False,
        separators=(",", ":"),
    )

    view = normalize_trusted_snapshot(snapshot_json)

    assert view.status == "available"
    assert view.source is not None
    node = view.nodes[0]
    assert node.key_shortened
    assert node.value_shortened
    assert "[view full]" in node.value
    assert len(node.value.encode("utf-8")) <= TRUSTED_SNAPSHOT_MAX_NODE_VALUE_BYTES
    assert node.full_key_handle is not None
    assert node.full_value_handle is not None
    assert node.source_position > 0
    assert (
        node.source_key_bytes == node.key_utf8_bytes == len(exact_key.encode("utf-8"))
    )
    assert (
        node.source_value_bytes
        == node.value_utf8_bytes
        == len(exact_value.encode("utf-8"))
    )
    key_result = view.source.full_value(node.full_key_handle)
    value_result = view.source.full_value(node.full_value_handle)
    assert key_result == TrustedSnapshotFullValue(
        exact_key, len(exact_key.encode("utf-8"))
    )
    assert value_result == TrustedSnapshotFullValue(
        exact_value, len(exact_value.encode("utf-8"))
    )
    root_view = normalize_trusted_snapshot(json.dumps(exact_value, ensure_ascii=False))
    assert root_view.source is not None
    root_node = root_view.nodes[0]
    assert root_node.value_shortened
    assert "[view full]" in root_node.value
    assert root_node.full_value_handle is not None
    assert root_view.source.full_value(root_node.full_value_handle) == value_result
    assert snapshot_json not in repr(view)
    assert snapshot_json not in repr(view.source)
    _assert_bounded(view)


def test_handles_are_source_bound_and_page_and_value_work_are_cancellable() -> None:
    first = normalize_trusted_snapshot(json.dumps({"value": "x" * 2_000}))
    second = normalize_trusted_snapshot(json.dumps({"other": [1, 2, 3]}))
    assert first.source is not None
    assert second.source is not None
    assert first.root_handle is not None
    assert second.root_handle is not None
    value_handle = first.nodes[0].full_value_handle
    assert value_handle is not None

    with pytest.raises(ValueError, match="foreign"):
        first.source.page(second.root_handle, None)
    with pytest.raises(ValueError, match="foreign"):
        second.source.full_value(value_handle)

    def cancelled() -> None:
        raise InterruptedError("cancelled")

    with pytest.raises(InterruptedError, match="cancelled"):
        first.source.page(first.root_handle, None, cancel_check=cancelled)
    with pytest.raises(InterruptedError, match="cancelled"):
        first.source.full_value(value_handle, cancel_check=cancelled)
