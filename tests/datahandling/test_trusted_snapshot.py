from __future__ import annotations

from qplot.datahandling.trusted_snapshot import (
    TRUSTED_SNAPSHOT_MAX_DEPTH,
    TRUSTED_SNAPSHOT_MAX_INPUT_BYTES,
    TRUSTED_SNAPSHOT_MAX_NODE_KEY_BYTES,
    TRUSTED_SNAPSHOT_MAX_NODE_VALUE_BYTES,
    TRUSTED_SNAPSHOT_MAX_RENDERED_NODES,
    TRUSTED_SNAPSHOT_MAX_RENDERED_TEXT_BYTES,
    normalize_trusted_snapshot,
)


def _rendered_bytes(view) -> int:
    return sum(
        len(node.key.encode("utf-8")) + len(node.value.encode("utf-8"))
        for node in view.nodes
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
        for node in view.nodes
    )
    assert all(
        node.parent_index is None or 0 <= node.parent_index < node_index
        for node_index, node in enumerate(view.nodes)
    )


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

    assert view.status == "truncated"
    assert view.input_bytes == input_bytes
    assert view.nodes[-1].key == "[truncated]"
    _assert_bounded(view)


def test_oversized_string_never_reaches_a_node_or_parameter_tooltip_whole() -> None:
    oversized = "sensitive-value-" * 10_000
    snapshot_json = (
        '{"station":{"parameters":{"gate":{"full_name":"gate",'
        '"value":"' + oversized + '"}}}}'
    )

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
