"""Pure metadata normalization shared by snapshot and trusted readers.

Parameter ordering, declared shapes, and presentation budgets live here; no
query adapter or reader session is constructed by these transformations."""

from __future__ import annotations

import json
from itertools import islice
from typing import Any, cast

from qplot.datahandling.trusted_live import (
    TrustedQueryResult,
)
from qplot.datahandling.trusted_presentation import (
    TRUSTED_PRESENTATION_MAX_PARAMETER_DEPENDENCIES,
    TRUSTED_PRESENTATION_MAX_PARAMETER_TEXT_BYTES,
    TRUSTED_PRESENTATION_MAX_PARAMETER_TOTAL_TEXT_BYTES,
    TRUSTED_PRESENTATION_MAX_PARAMETERS,
    bounded_presentation_scalar,
    bounded_presentation_text,
)
from qplot.datahandling.trusted_query_types import (
    PrimitiveScalar,
    TrustedMetadataQueryError,
    TrustedParameterView,
    TrustedSetpointSummary,
)

_TRUSTED_OBSERVATION_MAX_SETPOINTS = 32


def bounded_parameter_presentation(
    parameters: tuple[TrustedParameterView, ...],
) -> tuple[tuple[TrustedParameterView, ...], bool]:
    truncated = len(parameters) > TRUSTED_PRESENTATION_MAX_PARAMETERS
    output: list[TrustedParameterView] = []
    retained_text_bytes = 0
    for parameter in parameters[:TRUSTED_PRESENTATION_MAX_PARAMETERS]:
        name, name_truncated = bounded_presentation_text(parameter.name)
        label, label_truncated = bounded_presentation_text(parameter.label)
        unit, unit_truncated = bounded_presentation_text(parameter.unit)
        paramtype, type_truncated = bounded_presentation_text(parameter.paramtype)
        dependencies = parameter.depends_on[
            :TRUSTED_PRESENTATION_MAX_PARAMETER_DEPENDENCIES
        ]
        bounded_dependencies: list[str] = []
        dependency_truncated = (
            len(parameter.depends_on) > TRUSTED_PRESENTATION_MAX_PARAMETER_DEPENDENCIES
        )
        for dependency in dependencies:
            bounded, was_truncated = bounded_presentation_text(dependency)
            bounded_dependencies.append(bounded)
            dependency_truncated = dependency_truncated or was_truncated
        bounded_parameter = TrustedParameterView(
            name,
            label,
            unit,
            tuple(bounded_dependencies),
            paramtype,
        )
        parameter_text_bytes = sum(
            len(value.encode("utf-8"))
            for value in (
                bounded_parameter.name,
                bounded_parameter.label,
                bounded_parameter.unit,
                bounded_parameter.paramtype,
                *bounded_parameter.depends_on,
            )
        )
        if (
            retained_text_bytes + parameter_text_bytes
            > TRUSTED_PRESENTATION_MAX_PARAMETER_TOTAL_TEXT_BYTES
        ):
            truncated = True
            break
        output.append(bounded_parameter)
        retained_text_bytes += parameter_text_bytes
        truncated = truncated or any(
            (
                name_truncated,
                label_truncated,
                unit_truncated,
                type_truncated,
                dependency_truncated,
            )
        )
    return tuple(output), truncated


def bounded_setpoint_presentation(
    summaries: tuple[TrustedSetpointSummary, ...],
) -> tuple[tuple[TrustedSetpointSummary, ...], bool]:
    output: list[TrustedSetpointSummary] = []
    truncated = False
    for summary in summaries:
        name, name_truncated = bounded_presentation_text(summary.name)
        first, first_truncated = bounded_presentation_scalar(summary.first)
        last, last_truncated = bounded_presentation_scalar(summary.last)
        output.append(
            TrustedSetpointSummary(
                name,
                cast(PrimitiveScalar, first),
                cast(PrimitiveScalar, last),
                summary.steps,
            )
        )
        truncated = truncated or any((name_truncated, first_truncated, last_truncated))
    return tuple(output), truncated


def quote_sqlite_identifier(identifier: object) -> str:
    """Quote one database-derived SQLite identifier without interpretation."""

    if not isinstance(identifier, str) or not identifier or "\x00" in identifier:
        raise TrustedMetadataQueryError(
            "A database-derived table or column identifier is invalid."
        )
    return '"' + identifier.replace('"', '""') + '"'


def parameter_views_from_run_metadata(
    metadata: dict[str, Any],
    layout_rows: tuple[tuple[Any, ...], ...] = (),
) -> tuple[TrustedParameterView, ...]:
    """Build immutable parameter views from bounded run/layout primitives."""
    result = TrustedQueryResult(
        ("layout_id", "parameter", "label", "unit", "inferred_from"),
        tuple(tuple(row) for row in layout_rows),
    )
    return parameter_views(dict(metadata), result)


def bounded_parameter_views_from_run_metadata(
    metadata: dict[str, Any],
    layout_rows: tuple[tuple[Any, ...], ...] = (),
) -> tuple[tuple[TrustedParameterView, ...], bool]:
    """Build parameter rows and report every source-presentation truncation."""

    result = TrustedQueryResult(
        ("layout_id", "parameter", "label", "unit", "inferred_from"),
        tuple(tuple(row) for row in layout_rows),
    )
    return parameter_views_bounded(
        dict(metadata),
        result,
    )


def _json_object(value: object) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        return {}
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _bounded_parameter_view_identifier(value: object) -> tuple[str, bool]:
    if not isinstance(value, str):
        return "", value is not None
    return bounded_presentation_text(
        value,
        limit=TRUSTED_PRESENTATION_MAX_PARAMETER_TEXT_BYTES,
    )


def _bounded_parameter_view_text(
    value: object,
    *,
    fallback: str = "",
) -> tuple[str, bool]:
    if value is None or value == "":
        value = fallback
    if isinstance(value, str):
        return bounded_presentation_text(
            value,
            limit=TRUSTED_PRESENTATION_MAX_PARAMETER_TEXT_BYTES,
        )
    if isinstance(value, bytes):
        return f"[binary value omitted: {len(value)} bytes]", True
    if isinstance(value, (bool, int, float)):
        return bounded_presentation_text(
            str(value),
            limit=TRUSTED_PRESENTATION_MAX_PARAMETER_TEXT_BYTES,
        )
    return f"[{type(value).__name__} value unavailable]", True


def _positive_int(value: object) -> int | None:
    if type(value) is not int:
        return None
    return value if value > 0 else None


def array_parameter_names(run_description: dict[str, Any]) -> set[str]:
    interdependencies = run_description.get("interdependencies_")
    parameters = (
        interdependencies.get("parameters")
        if isinstance(interdependencies, dict) else None
    )
    if not isinstance(parameters, dict):
        return set()
    return {
        name for name, specification in parameters.items()
        if isinstance(specification, dict)
        and specification.get("paramtype", specification.get("type")) == "array"
    }


def parameter_dependency_sets(
    run_description: dict[str, Any],
    measure_parameters: tuple[str, ...],
    sweep_parameters: tuple[str, ...],
    result_columns: tuple[str, ...],
) -> tuple[tuple[tuple[str, ...], str | None], ...]:
    available = set(result_columns)
    dependencies = (
        run_description.get("interdependencies_", {}).get("dependencies", {})
        if isinstance(run_description.get("interdependencies_"), dict)
        else {}
    )
    planned: list[tuple[tuple[str, ...], str | None]] = []
    if isinstance(dependencies, dict):
        for dependent in measure_parameters:
            names = dependencies.get(dependent)
            if not isinstance(names, (list, tuple)) or not names:
                continue
            raw_setpoints = tuple(
                islice(names, _TRUSTED_OBSERVATION_MAX_SETPOINTS + 1)
            )
            if len(raw_setpoints) > _TRUSTED_OBSERVATION_MAX_SETPOINTS:
                continue
            setpoints_list: list[str] = []
            invalid = False
            for raw_name in raw_setpoints:
                if not isinstance(raw_name, str):
                    invalid = True
                    break
                name, was_truncated = bounded_presentation_text(raw_name)
                if was_truncated:
                    invalid = True
                    break
                setpoints_list.append(name)
            if invalid:
                continue
            setpoints = tuple(setpoints_list)
            if dependent in available and all(
                name in available for name in setpoints
            ):
                planned.append((setpoints, dependent))
    if (
        not planned
        and sweep_parameters
        and len(sweep_parameters) <= _TRUSTED_OBSERVATION_MAX_SETPOINTS
        and all(name in available for name in sweep_parameters)
    ):
        planned.append((sweep_parameters, None))
    return tuple(planned)


def planned_setpoint_steps(
    metadata: dict[str, Any],
    summary_names: tuple[str, ...],
) -> dict[str, int]:
    run_description = _json_object(metadata.get("run_description"))
    interdependencies = run_description.get("interdependencies_")
    if not isinstance(interdependencies, dict):
        return {}
    dependencies = interdependencies.get("dependencies")
    shapes = run_description.get("shapes")
    if not isinstance(dependencies, dict) or not isinstance(shapes, dict):
        return {}

    # A QCoDeS shape belongs to one dependent, and its dimensions follow
    # that dependent's dependency order.  The public run-wide point shape
    # is only the largest declared shape; pairing it with the union of all
    # setpoints loses both associations and produced arbitrary counts for
    # independent sweeps.
    requested = set(summary_names)
    planned: dict[str, int] = {}
    conflicts: set[str] = set()
    measure_parameters = tuple(metadata.get("measure_parameters") or ())
    for dependent in measure_parameters:
        if not isinstance(dependent, str):
            continue
        raw_dependencies = dependencies.get(dependent)
        raw_shape = shapes.get(dependent)
        if not isinstance(raw_dependencies, (list, tuple)) or not isinstance(
            raw_shape, (list, tuple)
        ):
            continue
        ordered_dependencies = tuple(
            islice(raw_dependencies, _TRUSTED_OBSERVATION_MAX_SETPOINTS + 1)
        )
        ordered_shape = tuple(
            islice(raw_shape, _TRUSTED_OBSERVATION_MAX_SETPOINTS + 1)
        )
        if (
            len(ordered_dependencies) > _TRUSTED_OBSERVATION_MAX_SETPOINTS
            or len(ordered_shape) > _TRUSTED_OBSERVATION_MAX_SETPOINTS
            or len(ordered_dependencies) != len(ordered_shape)
        ):
            continue
        for name, raw_step in zip(
            ordered_dependencies,
            ordered_shape,
            strict=True,
        ):
            step = _positive_int(raw_step)
            if not isinstance(name, str) or name not in requested or step is None:
                continue
            prior = planned.get(name)
            if prior is None:
                planned[name] = step
            elif prior != step:
                conflicts.add(name)

    # A shared setpoint has one meaningful planned count only when every
    # dependent declaration agrees.  Absence represents an explicit
    # unknown in TrustedSetpointSummary rather than choosing by order.
    for name in conflicts:
        planned.pop(name, None)
    return planned


def estimated_result_storage_bytes(
    result_count: int,
    result_columns: tuple[str, ...],
    run_description: dict[str, Any],
) -> int:
    interdependencies = run_description.get("interdependencies_")
    parameters = (
        interdependencies.get("parameters")
        if isinstance(interdependencies, dict)
        else None
    )
    if not isinstance(parameters, dict):
        parameters = {}

    # Mirror the snapshot reader's deliberately rough per-row estimate,
    # but derive types from the already-loaded run description instead of
    # issuing PRAGMA table_info.  Variable-width fields stay conservative
    # heuristics and the public flag always labels this value as estimated.
    row_bytes = len(result_columns) + 2
    for name in result_columns:
        specification = parameters.get(name)
        raw_parameter_type = (
            specification.get("paramtype", specification.get("type"))
            if isinstance(specification, dict) else None
        )
        parameter_type = (
            raw_parameter_type.lower()
            if isinstance(raw_parameter_type, str)
            else ""
        )
        if name == "id" or parameter_type == "numeric":
            row_bytes += 8
        elif parameter_type == "complex":
            row_bytes += 16
        else:
            row_bytes += 32
    return max(0, result_count) * row_bytes


def parameter_views(
    metadata: dict[str, Any],
    layout_result: TrustedQueryResult,
) -> tuple[TrustedParameterView, ...]:
    views, _truncated = parameter_views_bounded(
        metadata,
        layout_result,
    )
    return views


def parameter_views_bounded(
    metadata: dict[str, Any],
    layout_result: TrustedQueryResult,
) -> tuple[tuple[TrustedParameterView, ...], bool]:
    run_description = _json_object(metadata.get("run_description"))
    interdependencies = run_description.get("interdependencies_")
    if not isinstance(interdependencies, dict):
        interdependencies = {}
    raw_parameters = interdependencies.get("parameters")
    dependencies = interdependencies.get("dependencies")
    if not isinstance(raw_parameters, dict):
        raw_parameters = {}
    if not isinstance(dependencies, dict):
        dependencies = {}

    truncated = False
    layouts: dict[str, tuple[str, str]] = {}
    for index, row in enumerate(
        islice(layout_result.rows, TRUSTED_PRESENTATION_MAX_PARAMETERS + 1)
    ):
        if index >= TRUSTED_PRESENTATION_MAX_PARAMETERS:
            truncated = True
            break
        if len(row) < 4 or not isinstance(row[1], str):
            truncated = True
            continue
        name, name_truncated = bounded_presentation_text(
            row[1],
            limit=TRUSTED_PRESENTATION_MAX_PARAMETER_TEXT_BYTES,
        )
        label, label_truncated = _bounded_parameter_view_text(row[2])
        unit, unit_truncated = _bounded_parameter_view_text(row[3])
        truncated = bool(
            truncated or name_truncated or label_truncated or unit_truncated
        )
        layouts.setdefault(name, (label, unit))

    ordered: list[tuple[str, dict[str, Any], object]] = []
    seen_names: set[str] = set()

    def add_parameter(raw_name: object, specification: object) -> None:
        nonlocal truncated
        if not isinstance(raw_name, str):
            truncated = truncated or raw_name is not None
            return
        name, name_truncated = bounded_presentation_text(
            raw_name,
            limit=TRUSTED_PRESENTATION_MAX_PARAMETER_TEXT_BYTES,
        )
        truncated = truncated or name_truncated
        if not name or name in seen_names:
            return
        if len(ordered) >= TRUSTED_PRESENTATION_MAX_PARAMETERS:
            truncated = True
            return
        bounded_specification = (
            specification if isinstance(specification, dict) else {}
        )
        ordered.append((name, bounded_specification, dependencies.get(raw_name)))
        seen_names.add(name)

    for index, (raw_name, specification) in enumerate(
        islice(
            raw_parameters.items(),
            TRUSTED_PRESENTATION_MAX_PARAMETERS + 1,
        )
    ):
        if index >= TRUSTED_PRESENTATION_MAX_PARAMETERS:
            truncated = True
            break
        add_parameter(raw_name, specification)

    for role_name in ("sweep_parameters", "measure_parameters"):
        names = metadata.get(role_name) or ()
        if not isinstance(names, (list, tuple)):
            truncated = truncated or bool(names)
            continue
        for index, raw_name in enumerate(
            islice(names, TRUSTED_PRESENTATION_MAX_PARAMETERS + 1)
        ):
            if index >= TRUSTED_PRESENTATION_MAX_PARAMETERS:
                truncated = True
                break
            specification = (
                raw_parameters.get(raw_name) if isinstance(raw_name, str) else None
            )
            add_parameter(raw_name, specification)

    views: list[TrustedParameterView] = []
    for name, specification, raw_dependencies in ordered:
        layout_label, layout_unit = layouts.get(name, ("", ""))
        depends_on: list[str] = []
        dependency_seen: set[str] = set()
        if isinstance(raw_dependencies, (list, tuple)):
            for dependency_index, raw_dependency in enumerate(
                islice(
                    raw_dependencies,
                    TRUSTED_PRESENTATION_MAX_PARAMETER_DEPENDENCIES + 1,
                )
            ):
                if (
                    dependency_index
                    >= TRUSTED_PRESENTATION_MAX_PARAMETER_DEPENDENCIES
                ):
                    truncated = True
                    break
                dependency, dependency_truncated = (
                    _bounded_parameter_view_identifier(raw_dependency)
                )
                truncated = truncated or dependency_truncated
                if dependency and dependency not in dependency_seen:
                    depends_on.append(dependency)
                    dependency_seen.add(dependency)
        elif raw_dependencies:
            truncated = True

        label, label_truncated = _bounded_parameter_view_text(
            specification.get("label"),
            fallback=layout_label,
        )
        unit, unit_truncated = _bounded_parameter_view_text(
            specification.get("unit"),
            fallback=layout_unit,
        )
        paramtype, type_truncated = _bounded_parameter_view_text(
            specification.get("paramtype", specification.get("type")),
            fallback="numeric",
        )
        truncated = bool(
            truncated or label_truncated or unit_truncated or type_truncated
        )
        views.append(
            TrustedParameterView(
                name=name,
                label=label,
                unit=unit,
                depends_on=tuple(depends_on),
                paramtype=paramtype,
            )
        )
    return tuple(views), truncated
