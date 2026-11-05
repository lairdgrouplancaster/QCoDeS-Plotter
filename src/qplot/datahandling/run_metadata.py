"""Pure run metadata shared by snapshot and trusted database readers.

Readers supply primitive run fields and bounded observations. This module
interprets parameter declarations, planned shapes and observed counts without
opening databases or importing QCoDeS. Keep source labels and limits identical
for both reading paths.
"""

import json
import math
from collections.abc import Mapping
from itertools import islice
from typing import Any

from qplot.datahandling.trusted_presentation import (
    TRUSTED_PRESENTATION_MAX_PARAMETER_DEPENDENCIES,
    TRUSTED_PRESENTATION_MAX_PARAMETER_TEXT_BYTES,
    TRUSTED_PRESENTATION_MAX_PARAMETERS,
    bounded_presentation_text,
)

MAX_RUN_DESCRIPTION_SHAPE_DIMENSIONS = 32
_OBSERVATION_UNSET = object()


def _add_run_basic_fields(metadata):
    run_description = _json_dict(metadata.get("run_description"))
    measure_parameters, sweep_parameters, parameters_truncated = (
        _bounded_parameter_roles(
            run_description,
            metadata.get("parameters"),
        )
    )

    point_shape, shape_truncated = _bounded_point_shape(
        run_description,
        measure_parameters,
    )

    metadata["measure_parameters"] = measure_parameters
    metadata["sweep_parameters"] = sweep_parameters
    metadata["parameters_truncated"] = bool(parameters_truncated or shape_truncated)
    metadata["point_shape"] = point_shape
    metadata["setpoint_shape"] = metadata["point_shape"]
    shape_source = "planned" if metadata["setpoint_shape"] else None
    metadata["setpoint_shape_source"] = shape_source
    expected_results, expected_shape_truncated = _bounded_expected_results_from_shapes(
        run_description,
        measure_parameters,
    )
    metadata["parameters_truncated"] = bool(
        metadata["parameters_truncated"] or expected_shape_truncated
    )
    metadata["expected_results"] = expected_results
    metadata["expected_results_source"] = (
        "planned" if expected_results is not None else None
    )
    metadata["setpoint_count"] = _shape_size(metadata["setpoint_shape"])
    metadata["setpoint_count_source"] = shape_source


def materialize_run_basic_fields(
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return one run mapping with qPlot's shared derived basic fields.

    Snapshot readers and the trusted fixed-query adapter deliberately share
    this transformation.  Keeping it independent of cursors, connections,
    QCoDeS objects, and filesystem access prevents the two read paths from
    drifting while allowing the trusted path to materialise only primitive
    query results.
    """
    materialized = dict(metadata or {})
    _add_run_basic_fields(materialized)
    return materialized


def materialize_run_observation(
    metadata: Mapping[str, Any] | None,
    *,
    result_count=None,
    setpoint_shape=None,
    setpoint_count=None,
    storage_bytes=None,
    storage_bytes_estimated=None,
    read_setpoint_count=_OBSERVATION_UNSET,
) -> dict[str, Any]:
    """Return shared detail fields from already-bounded observations.

    All database work happens before this function.  The snapshot path can
    continue collecting observations with cursors, while the trusted adapter
    supplies the same values from fixed supervisor queries.
    An omitted acquired count retains the prior observation; explicit ``None``
    clears a count whose structural evidence is no longer available.
    """
    materialized = materialize_run_basic_fields(metadata)
    if result_count is not None:
        materialized["result_count"] = int(result_count)
    elif "result_count" not in materialized:
        materialized["result_count"] = None

    if setpoint_count is not None or setpoint_shape is not None:
        normalized_shape = (
            [int(size) for size in setpoint_shape] if setpoint_shape else None
        )
        update_observed_setpoint_fields(
            materialized,
            normalized_shape,
            int(setpoint_count) if setpoint_count is not None else None,
        )

    if read_setpoint_count is not _OBSERVATION_UNSET:
        materialized["read_setpoint_count"] = (
            int(read_setpoint_count) if read_setpoint_count is not None else None
        )

    if storage_bytes is not None or storage_bytes_estimated is not None:
        materialized["storage_bytes"] = (
            int(storage_bytes) if storage_bytes is not None else None
        )
        materialized["storage_bytes_estimated"] = storage_bytes_estimated

    _add_completed_observed_result_count(materialized)
    return materialized


def update_observed_setpoint_fields(
    metadata: dict[str, Any],
    shape: list[int] | None,
    count: int | None,
) -> None:
    """Replace planned fields with already-normalized setpoint observations.

    A global observed shape may be unknown even when the acquired count is
    known. Readers retain responsibility for collecting and normalizing those
    observations; completed-run result counts are applied separately.
    """
    metadata["setpoint_shape"] = shape
    metadata["point_shape"] = _point_shape_from_setpoint_shape(
        shape,
        metadata.get("measure_parameters"),
        metadata.get("result_count"),
    )
    metadata["setpoint_shape_source"] = "observed" if shape else None
    metadata["setpoint_count"] = count
    metadata["setpoint_count_source"] = "observed" if count is not None else None
    metadata["expected_results"] = None
    metadata["expected_results_source"] = None


def _add_completed_observed_result_count(metadata):
    if (
        metadata.get("expected_results") is None
        and _run_is_complete(metadata)
        and metadata.get("result_count") is not None
    ):
        metadata["expected_results"] = metadata["result_count"]
        metadata["expected_results_source"] = "observed"


def _json_dict(value):
    if not value:
        return {}

    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError, RecursionError):
        return {}

    return decoded if isinstance(decoded, dict) else {}


def _parameter_roles(run_description, parameter_text):
    measure, sweep, _truncated = _bounded_parameter_roles(
        run_description,
        parameter_text,
    )
    return measure, sweep


def _bounded_parameter_roles(run_description, parameter_text):
    dependencies, truncated = _bounded_parameter_dependencies(run_description)
    measure_parameters: list[str] = []
    sweep_parameters: list[str] = []
    measure_seen: set[str] = set()
    sweep_seen: set[str] = set()
    all_seen: set[str] = set()

    def add(target, seen, name):
        nonlocal truncated
        if name in seen:
            return
        if (
            name not in all_seen
            and len(all_seen) >= TRUSTED_PRESENTATION_MAX_PARAMETERS
        ):
            truncated = True
            return
        target.append(name)
        seen.add(name)
        all_seen.add(name)

    for parameter, dependents in dependencies.items():
        add(measure_parameters, measure_seen, parameter)
        for name in dependents:
            add(sweep_parameters, sweep_seen, name)

    if isinstance(parameter_text, str):
        raw_parameters = parameter_text.split(",", TRUSTED_PRESENTATION_MAX_PARAMETERS)
        if len(raw_parameters) > TRUSTED_PRESENTATION_MAX_PARAMETERS:
            truncated = True
            raw_parameters.pop()
        for raw_parameter in raw_parameters:
            parameter, was_truncated = _bounded_parameter_identifier(
                raw_parameter.strip()
            )
            truncated = truncated or was_truncated
            if parameter and parameter not in sweep_seen:
                add(measure_parameters, measure_seen, parameter)
    elif parameter_text:
        truncated = True

    return measure_parameters, sweep_parameters, truncated


def _parameter_dependencies(run_description):
    dependencies, _truncated = _bounded_parameter_dependencies(run_description)
    return dependencies


def _bounded_parameter_dependencies(run_description):
    truncated = False
    interdependencies = run_description.get("interdependencies_")
    if not isinstance(interdependencies, dict):
        interdependencies = {}
    dependencies = interdependencies.get("dependencies", {})
    if not isinstance(dependencies, dict) or not dependencies:
        return _bounded_legacy_dependencies(run_description)

    output: dict[str, list[str]] = {}
    items = islice(
        dependencies.items(),
        TRUSTED_PRESENTATION_MAX_PARAMETERS + 1,
    )
    for index, (raw_parameter, setpoints) in enumerate(items):
        if index >= TRUSTED_PRESENTATION_MAX_PARAMETERS:
            truncated = True
            break
        parameter, name_truncated = _bounded_parameter_identifier(raw_parameter)
        truncated = truncated or name_truncated
        if not parameter or not isinstance(setpoints, (list, tuple)):
            truncated = truncated or bool(setpoints)
            continue
        bounded_setpoints = []
        seen = set()
        for dependency_index, raw_setpoint in enumerate(
            islice(
                setpoints,
                TRUSTED_PRESENTATION_MAX_PARAMETER_DEPENDENCIES + 1,
            )
        ):
            if dependency_index >= TRUSTED_PRESENTATION_MAX_PARAMETER_DEPENDENCIES:
                truncated = True
                break
            setpoint, dependency_truncated = _bounded_parameter_identifier(raw_setpoint)
            truncated = truncated or dependency_truncated
            if setpoint and setpoint not in seen:
                bounded_setpoints.append(setpoint)
                seen.add(setpoint)
        if bounded_setpoints:
            output[parameter] = bounded_setpoints
    return output, truncated


def _legacy_dependencies(run_description):
    dependencies, _truncated = _bounded_legacy_dependencies(run_description)
    return dependencies


def _bounded_legacy_dependencies(run_description):
    out: dict[str, list[str]] = {}
    truncated = False
    interdependencies = run_description.get("interdependencies")
    paramspecs = (
        interdependencies.get("paramspecs", [])
        if isinstance(interdependencies, dict)
        else []
    )
    if not isinstance(paramspecs, (list, tuple)):
        return out, bool(paramspecs)
    for index, paramspec in enumerate(
        islice(paramspecs, TRUSTED_PRESENTATION_MAX_PARAMETERS + 1)
    ):
        if index >= TRUSTED_PRESENTATION_MAX_PARAMETERS:
            truncated = True
            break
        if not isinstance(paramspec, dict):
            truncated = True
            continue
        depends_on = paramspec.get("depends_on") or []
        name, name_truncated = _bounded_parameter_identifier(paramspec.get("name"))
        truncated = truncated or name_truncated
        if not name or not isinstance(depends_on, (list, tuple)):
            truncated = truncated or bool(depends_on)
            continue
        bounded_dependencies = []
        seen = set()
        for dependency_index, raw_dependency in enumerate(
            islice(
                depends_on,
                TRUSTED_PRESENTATION_MAX_PARAMETER_DEPENDENCIES + 1,
            )
        ):
            if dependency_index >= TRUSTED_PRESENTATION_MAX_PARAMETER_DEPENDENCIES:
                truncated = True
                break
            dependency, dependency_truncated = _bounded_parameter_identifier(
                raw_dependency
            )
            truncated = truncated or dependency_truncated
            if dependency and dependency not in seen:
                bounded_dependencies.append(dependency)
                seen.add(dependency)
        if bounded_dependencies:
            out[name] = bounded_dependencies
    return out, truncated


def _bounded_parameter_identifier(value):
    if not isinstance(value, str):
        return "", value is not None
    return bounded_presentation_text(
        value,
        limit=TRUSTED_PRESENTATION_MAX_PARAMETER_TEXT_BYTES,
    )


def _point_shape(run_description, measure_parameters):
    shape, _truncated = _bounded_point_shape(run_description, measure_parameters)
    return shape


def _bounded_point_shape(run_description, measure_parameters):
    shapes = run_description.get("shapes")
    if not isinstance(shapes, dict):
        return None, False

    best_shape = None
    best_size = 0
    truncated = False
    for parameter in measure_parameters:
        shape = shapes.get(parameter)
        if isinstance(shape, list) and shape:
            dimensions = list(islice(shape, MAX_RUN_DESCRIPTION_SHAPE_DIMENSIONS + 1))
            if len(dimensions) > MAX_RUN_DESCRIPTION_SHAPE_DIMENSIONS:
                truncated = True
                continue
            try:
                point_shape = [int(size) for size in dimensions]
            except (TypeError, ValueError):
                continue

            size = _shape_size(point_shape) or 0
            if size > best_size:
                best_shape = point_shape
                best_size = size

    return best_shape, truncated


def _expected_results_from_shapes(run_description, measure_parameters):
    expected, _truncated = _bounded_expected_results_from_shapes(
        run_description,
        measure_parameters,
    )
    return expected


def _bounded_expected_results_from_shapes(run_description, measure_parameters):
    shapes = run_description.get("shapes")
    if not isinstance(shapes, dict) or not measure_parameters:
        return None, False

    sizes = {}
    truncated = False
    for index, parameter in enumerate(
        islice(measure_parameters, TRUSTED_PRESENTATION_MAX_PARAMETERS + 1)
    ):
        if index >= TRUSTED_PRESENTATION_MAX_PARAMETERS:
            return None, True
        shape = shapes.get(parameter)
        if not isinstance(shape, list) or not shape:
            return None, truncated

        dimensions = list(islice(shape, MAX_RUN_DESCRIPTION_SHAPE_DIMENSIONS + 1))
        if len(dimensions) > MAX_RUN_DESCRIPTION_SHAPE_DIMENSIONS:
            truncated = True
            return None, truncated

        size = _shape_size(dimensions)
        if size is None:
            return None, truncated
        sizes[parameter] = size

    interdependencies = run_description.get("interdependencies_")
    inferences = (
        interdependencies.get("inferences", {})
        if isinstance(interdependencies, dict) else {}
    )
    if not isinstance(inferences, dict):
        return None, truncated
    if len(inferences) > TRUSTED_PRESENTATION_MAX_PARAMETERS:
        return None, True
    if not inferences:
        return (sum(sizes.values()) if sizes else None), truncated

    # Current QCoDeS writes each top-level parameter tree separately. An
    # inferred channel is stored with its basis, rather than adding another
    # row. Multiple basis roots still contribute one tree each. Only infer a
    # planned row count when the complete bounded graph lies among measured
    # channels and its linked shape sizes agree; setpoint inference and other
    # ambiguous declarations retain an unknown count.
    inferred = set()
    followers: dict[str, set[str]] = {name: set() for name in sizes}
    remaining_bases = {name: 0 for name in sizes}
    for index, (name, bases) in enumerate(
        islice(inferences.items(), TRUSTED_PRESENTATION_MAX_PARAMETERS + 1)
    ):
        if index >= TRUSTED_PRESENTATION_MAX_PARAMETERS:
            return None, True
        if name not in sizes or not isinstance(bases, (list, tuple)) or not bases:
            return None, truncated
        inferred.add(name)
        for basis_index, basis in enumerate(
            islice(bases, TRUSTED_PRESENTATION_MAX_PARAMETER_DEPENDENCIES + 1)
        ):
            if basis_index >= TRUSTED_PRESENTATION_MAX_PARAMETER_DEPENDENCIES:
                return None, True
            if not isinstance(basis, str) or basis not in sizes:
                return None, truncated
            if sizes[name] != sizes[basis]:
                return None, truncated
            if name not in followers[basis]:
                followers[basis].add(name)
                remaining_bases[name] += 1

    ready = [name for name, count in remaining_bases.items() if count == 0]
    visited = 0
    while ready:
        name = ready.pop()
        visited += 1
        for follower in followers[name]:
            remaining_bases[follower] -= 1
            if remaining_bases[follower] == 0:
                ready.append(follower)
    if visited != len(sizes):
        return None, truncated
    roots = sizes.keys() - inferred
    return (sum(sizes[name] for name in roots) if roots else None), truncated


def _shape_size(shape):
    if not shape:
        return None

    try:
        dimensions = list(islice(iter(shape), MAX_RUN_DESCRIPTION_SHAPE_DIMENSIONS + 1))
        if len(dimensions) > MAX_RUN_DESCRIPTION_SHAPE_DIMENSIONS:
            return None
        return math.prod(int(size) for size in dimensions)
    except (TypeError, ValueError):
        return None


def _is_keyboard_interrupt(value):
    return bool(value and "KeyboardInterrupt" in str(value))


def _run_is_complete(metadata):
    return bool(metadata.get("completed_timestamp") or metadata.get("is_completed"))


def _point_shape_from_setpoint_shape(shape, measure_parameters=None, result_count=None):
    if not shape:
        return None

    shape_size = _shape_size(shape)
    measure_count = len(measure_parameters or [])
    if shape_size and result_count and measure_count > 1:
        try:
            result_count = int(result_count)
        except (TypeError, ValueError):
            result_count = None

        if (
            result_count
            and result_count % shape_size == 0
            and result_count // shape_size == measure_count
        ):
            shape = shape + [measure_count]

    return shape
