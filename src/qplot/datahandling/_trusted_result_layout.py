"""Bounded result-layout verification for current QCoDeS result tables.

Owns pending and completed layout evidence. All SQL goes through the supplied
executor, with the same finite primary-key probes and resumable page budgets."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field, replace
from typing import Any, cast

from qplot.datahandling._trusted_grid_sampling import (
    _symmetric_grid_indexes,
)
from qplot.datahandling._trusted_query_metadata import (
    array_parameter_names,
    quote_sqlite_identifier,
)
from qplot.datahandling.trusted_live import (
    TrustedQueryResult,
)
from qplot.datahandling.trusted_query_types import (
    TRUSTED_DERIVED_MAX_DEPENDENTS,
    TRUSTED_DERIVED_MAX_SAMPLE_CELLS,
    TRUSTED_DERIVED_MAX_SOURCE_COLUMNS,
    Trusted2DDependentRowPattern,
    Trusted2DLayoutProof,
    TrustedMetadataQueryError,
    TrustedQueryExecutor,
)

_TRUSTED_REGULAR_LAYOUT_PREFIX_ROWS = 64
_TRUSTED_REGULAR_LAYOUT_MAX_SEARCH_PROBES = 63
_TRUSTED_REGULAR_LAYOUT_ANCHOR_ROWS = 16
_TRUSTED_REGULAR_LAYOUT_MAX_DEPENDENTS = 8
_TRUSTED_PROGRESSIVE_LAYOUT_PAGE_ROWS = 4_096
_TRUSTED_VECTOR_FINGERPRINT_BITS = 256
_TRUSTED_VECTOR_FINGERPRINT_MASK = (1 << _TRUSTED_VECTOR_FINGERPRINT_BITS) - 1
_TRUSTED_VECTOR_FINGERPRINT_BASE = (
    0x1000000000000000000013B  # odd, so powers are invertible modulo 2**256
)


@dataclass(frozen=True, slots=True)
class _RegularLayoutRow:
    """One bounded numeric row retained only while proving a sweep layout."""

    row_id: int
    axes: tuple[int | float, ...]
    present: tuple[bool, ...]


@dataclass(frozen=True, slots=True)
class _RegularSetpointObservation:
    dependencies: tuple[str, ...]
    result_watermark: int
    count: int
    shape: tuple[int, ...]
    proof_2d: Trusted2DLayoutProof | None = None
    storage_kind: str = "dense"
    patterns: tuple[_RegularDependentPattern, ...] = ()


@dataclass(frozen=True, slots=True)
class _RegularDependentPattern:
    dependent: str | None
    present_index: int
    first_row_id: int
    fast_id_stride: int
    slow_id_stride: int


@dataclass(frozen=True, slots=True)
class _ProgressivePhysicalPattern:
    first_row_id: int
    fast_id_stride: int
    slow_id_stride: int
    owner_indexes: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _ProgressiveAxisState:
    """Constant-size verifier for one physical dependency grid."""

    next_slow_index: int = 0
    next_fast_index: int = 0
    current_slow_value: int | float | None = None
    previous_slow_value: int | float | None = None
    slow_direction: int = 0
    previous_fast_value: int | float | None = None
    current_fast_direction: int = 0
    current_forward: int = 0
    current_reverse: int = 0
    current_power: int = 1
    reference_forward: int | None = None
    reference_reverse: int | None = None
    reference_fast_direction: int = 0
    reference_checkpoints: tuple[tuple[int, int, int, int], ...] = ()
    row_major_possible: bool = True
    serpentine_possible: bool = True
    completed_rows: int = 0
    viable: bool = True


@dataclass(frozen=True, slots=True)
class _ProgressiveRegularLayout:
    """Constant-memory full-prefix proof for one regular-layout candidate."""

    dependencies: tuple[str, ...]
    dependents: tuple[str, ...]
    result_watermark: int
    proof_source: str
    observation: _RegularSetpointObservation
    physical_patterns: tuple[_ProgressivePhysicalPattern, ...]
    axis_states: tuple[_ProgressiveAxisState, ...]
    after_row_id: int = 0
    viable: bool = True
    pending_logical_index: int | None = None
    pending_axes: tuple[int | float, ...] | None = None
    pending_pattern_indexes: tuple[int, ...] = ()
    cross_slow_index: int | None = None
    cross_reference_slow: int | float | None = None
    cross_reference_forward: int | None = None
    cross_reference_reverse: int | None = None
    cross_next_pattern: int = 0


@dataclass(frozen=True, slots=True)
class _IndependentDependentLayout:
    """Constant-size verifier for one dependent's own physical grid."""

    dependencies: tuple[str, str]
    dependent: str
    planned_shape: tuple[int, int] | None
    first_row_id: int = 0
    first_axes: tuple[int | float, ...] = ()
    first_value_missing: bool = False
    fast_axis_index: int | None = None
    fast_count: int = 0
    fast_id_stride: int = 1
    slow_id_stride: int = 0
    count: int = 0
    axis: _ProgressiveAxisState = field(default_factory=_ProgressiveAxisState)
    viable: bool = True


@dataclass(frozen=True, slots=True)
class _IndependentRunLayouts:
    """One shared physical cursor, with independent proofs and failures."""

    signature: tuple[tuple[tuple[str, str], str, tuple[int, int] | None], ...]
    result_watermark: int
    states: tuple[_IndependentDependentLayout, ...]
    after_row_id: int = 0
    proofs: tuple[Trusted2DLayoutProof, ...] = ()


@dataclass(frozen=True, slots=True)
class _NDCountAxis:
    """Constant-size fingerprint of one monotonic axis cycle."""

    count: int = 0
    last: int | float | None = None
    direction: int = 0
    fingerprint: int = 0
    reference: tuple[int, int] | None = None


@dataclass(frozen=True, slots=True)
class _NDDependentCount:
    dependencies: tuple[str, ...]
    dependent: str
    axes: tuple[_NDCountAxis, ...]
    count: int = 0
    previous: tuple[int | float, ...] = ()
    viable: bool = True


@dataclass(frozen=True, slots=True)
class _NDRunCounts:
    signature: tuple[tuple[tuple[str, ...], str], ...]
    result_watermark: int
    states: tuple[_NDDependentCount, ...]
    after_row_id: int = 0


def _strict_numeric_direction(left: int | float, right: int | float) -> int:
    if right > left:
        return 1
    if right < left:
        return -1
    return 0


def _numeric_fingerprint_token(value: int | float) -> int:
    """Return a stable numeric token that treats equal ints/floats equally."""

    if type(value) is int:
        numerator, denominator = value, 1
    else:
        numerator, denominator = float(value).as_integer_ratio()
    encoded = f"{numerator}/{denominator}".encode("ascii")
    return int.from_bytes(hashlib.sha256(encoded).digest(), "big")


def _append_vector_fingerprint(
    forward: int,
    reverse: int,
    power: int,
    value: int | float,
) -> tuple[int, int, int]:
    token = _numeric_fingerprint_token(value)
    forward = (
        forward * _TRUSTED_VECTOR_FINGERPRINT_BASE + token
    ) & _TRUSTED_VECTOR_FINGERPRINT_MASK
    reverse = (reverse + token * power) & _TRUSTED_VECTOR_FINGERPRINT_MASK
    power = (
        power * _TRUSTED_VECTOR_FINGERPRINT_BASE
    ) & _TRUSTED_VECTOR_FINGERPRINT_MASK
    return forward, reverse, power


def _progressive_fast_row_matches(
    expected_count: int,
    expected_start: int | float | None,
    expected_end: int | float | None,
    observed_count: int,
    observed_start: int | float | None,
    observed_end: int | float | None,
) -> bool:
    return expected_count == observed_count and (
        (expected_start, expected_end) == (observed_start, observed_end)
        or (expected_start, expected_end) == (observed_end, observed_start)
    )


@dataclass(frozen=True, slots=True)
class _RunLayoutObservation:
    observations: tuple[tuple[tuple[str, ...], int, tuple[int, ...] | None], ...]
    nd_observations: tuple[tuple[tuple[str, ...], int, tuple[int, ...] | None], ...]
    nd_steps: dict[str, int]
    regular: _RegularSetpointObservation | None


class ResultLayoutQueries:
    """Bounded layout queries and resumable evidence for one reader session.

    The broker may replace its executor between attempts. Bind it before the
    next query without dropping completed proofs or partially verified pages.
    Clear all evidence on bootstrap and discard a run when its identity changes.
    """

    def __init__(self, executor: TrustedQueryExecutor) -> None:
        self._executor = executor
        self._observations: dict[int, _RegularSetpointObservation] = {}
        self._grid_proofs: dict[int, Trusted2DLayoutProof] = {}
        self._progress: dict[int, _ProgressiveRegularLayout] = {}
        self._verified: dict[int, _ProgressiveRegularLayout] = {}
        self._independent: dict[int, _IndependentRunLayouts] = {}
        self._nd_counts: dict[int, _NDRunCounts] = {}

    def bind_executor(self, executor: TrustedQueryExecutor) -> None:
        self._executor = executor

    def clear(self) -> None:
        """Forget all evidence at a new bootstrap boundary."""
        self._observations.clear()
        self._grid_proofs.clear()
        self._progress.clear()
        self._verified.clear()
        self._independent.clear()
        self._nd_counts.clear()

    def discard(self, run_id: int) -> None:
        """Forget pending and completed evidence for one replaced run."""
        self._observations.pop(run_id, None)
        self._grid_proofs.pop(run_id, None)
        self._progress.pop(run_id, None)
        self._verified.pop(run_id, None)
        self._independent.pop(run_id, None)
        self._nd_counts.pop(run_id, None)

    def grid_proof(self, run_id: int) -> Trusted2DLayoutProof | None:
        return self._grid_proofs.get(run_id)

    def independent_state(self, run_id: int) -> _IndependentRunLayouts | None:
        return self._independent.get(run_id)

    def pending_state(self, run_id: int) -> _ProgressiveRegularLayout | None:
        return self._progress.get(run_id)

    def nd_state(self, run_id: int) -> _NDRunCounts | None:
        return self._nd_counts.get(run_id)

    def observe_run(
        self,
        run_id: int,
        table_name: str,
        metadata: dict[str, Any],
        run_description: dict[str, Any],
        result_columns: tuple[str, ...],
        result_watermark: int,
        dependency_sets: tuple[tuple[tuple[str, ...], str | None], ...],
        has_array_records: bool,
    ) -> _RunLayoutObservation:
        """Advance bounded proofs without exposing mutable state to metadata code."""
        measure_parameters = tuple(metadata.get("measure_parameters") or ())
        independent_signature = self.independent_signature(
            dependency_sets, run_description
        )
        independent_page = None
        use_independent_layouts = (
            len({names for names, _dependent in dependency_sets}) > 1
            or len(independent_signature) > 1
            or (has_array_records and bool(independent_signature))
        )
        if use_independent_layouts:
            independent_page = self._advance_independent_layouts(
                run_id,
                table_name,
                dependency_sets,
                run_description,
                result_columns,
                result_watermark,
            )
        else:
            self._independent.pop(run_id, None)
        regular_observation = self._observations.get(run_id)
        if has_array_records:
            # Array cells can contain many samples. Even a scalar peer's
            # proven layout cannot establish a run-wide logical sample count.
            regular_observation = None
        expected_dependencies = (
            dependency_sets[0][0]
            if dependency_sets
            and len({dependencies for dependencies, _dependent in dependency_sets}) == 1
            else ()
        )
        expected_dependents = tuple(
            dict.fromkeys(
                dependent
                for _dependencies, dependent in dependency_sets
                if dependent is not None
            )
        )
        if (
            regular_observation is None
            or regular_observation.result_watermark != result_watermark
            or regular_observation.dependencies != expected_dependencies
        ):
            regular_observation = None
            self._observations.pop(run_id, None)
            self._grid_proofs.pop(run_id, None)
            shape_source = (
                "planned"
                if metadata.get("setpoint_shape_source") == "planned"
                else "observed"
            )
            progress = self._progress.get(run_id)
            if progress is not None and (
                progress.dependencies != expected_dependencies
                or progress.dependents != expected_dependents
            ):
                self._progress.pop(run_id, None)
                progress = None
            candidate = None
            # A shared grid is only an optional compatibility proof. Reuse the
            # independent scan page, and never restart a failed shared verifier
            # with a second full-prefix read in this request.
            can_advance_regular = not has_array_records and (
                not use_independent_layouts or (
                    independent_page is not None
                    and len(expected_dependencies) == 2
                    and len(expected_dependents) <= _TRUSTED_REGULAR_LAYOUT_MAX_DEPENDENTS
                )
            )
            if can_advance_regular and (
                progress is None or progress.result_watermark != result_watermark
            ):
                candidate = self._infer_regular_setpoint_observation(
                    table_name,
                    dependency_sets,
                    result_watermark,
                    proof_source=shape_source,
                )
                raw_planned_shape = metadata.get("setpoint_shape")
                planned_shape = (
                    tuple(raw_planned_shape)
                    if metadata.get("setpoint_shape_source") == "planned"
                    and isinstance(raw_planned_shape, (list, tuple))
                    and all(
                        type(value) is int and value > 0 for value in raw_planned_shape
                    )
                    else None
                )
                planned_candidate = self._planned_regular_setpoint_candidate(
                    table_name,
                    dependency_sets,
                    result_watermark,
                    planned_shape,
                )
                if planned_candidate is not None and (
                    candidate is None
                    or planned_candidate.count < math.prod(planned_candidate.shape)
                    or candidate.shape != planned_candidate.shape
                ):
                    candidate = planned_candidate
            if can_advance_regular:
                regular_observation = self._advance_progressive_regular_layout(
                    run_id,
                    table_name,
                    expected_dependencies,
                    expected_dependents,
                    result_watermark,
                    proof_source=shape_source,
                    candidate=candidate,
                    shared_page=(
                        self._materialize_regular_layout_rows(
                            independent_page,
                            len(expected_dependencies),
                            max(1, len(expected_dependents)),
                        )
                        if independent_page is not None and expected_dependencies
                        else None
                    ),
                    page_is_shared=use_independent_layouts,
                )
            if regular_observation is not None:
                self._observations[run_id] = regular_observation

        if regular_observation is not None and regular_observation.proof_2d is not None:
            if any(
                shape is not None and shape != regular_observation.shape
                for _dependencies, _dependent, shape in independent_signature
            ):
                # A common physical grid cannot override an individual
                # dependent's declaration. Its valid peers keep their proofs.
                regular_observation = None
                self._observations.pop(run_id, None)
                self._grid_proofs.pop(run_id, None)
                self._verified.pop(run_id, None)

        observations: list[tuple[tuple[str, ...], int, tuple[int, ...] | None]] = []
        raw_public_shape = metadata.get("setpoint_shape")
        public_shape = (
            tuple(
                value for value in raw_public_shape if type(value) is int and value > 0
            )
            if isinstance(raw_public_shape, (list, tuple))
            else None
        )
        if regular_observation is not None:
            if regular_observation.proof_2d is not None and (
                public_shape is None or public_shape == regular_observation.shape
            ):
                self._grid_proofs[run_id] = regular_observation.proof_2d
            observations.append(
                (
                    regular_observation.dependencies,
                    regular_observation.count,
                    regular_observation.shape,
                )
            )

        if not has_array_records and len(dependency_sets) == len(measure_parameters):
            nd_observations, nd_steps = self._advance_nd_counts(
                run_id, table_name, dependency_sets, result_columns, result_watermark,
                shared_page=independent_page,
            )
            observations.extend(nd_observations)
        else:
            self._nd_counts.pop(run_id, None)
            nd_observations = []
            nd_steps = {}

        # Separate dependent blocks can defeat a shared physical-row proof
        # even when every dependent's complete grid has been verified. Publish
        # their common logical shape only after all owners are accounted for.
        independent = self._independent.get(run_id)
        if (
            not observations and not has_array_records
            and len(expected_dependencies) == 2
            and independent is not None
            and independent.after_row_id == result_watermark
            and all(not state.first_value_missing for state in independent.states)
            and {pattern.dependent for proof in independent.proofs
                 for pattern in proof.dependent_rows} == set(expected_dependents)
            and len({proof.shape for proof in independent.proofs}) == 1
            and all(proof.dependencies == expected_dependencies for proof in independent.proofs)
        ):
            shape = independent.proofs[0].shape
            observations.append((expected_dependencies, math.prod(shape), shape))
            nd_steps.update(zip(expected_dependencies, shape, strict=True))

        return _RunLayoutObservation(
            tuple(observations), tuple(nd_observations), nd_steps, regular_observation
        )

    @staticmethod
    def _consume_nd_count(
        state: _NDDependentCount,
        values: tuple[int | float, ...],
    ) -> _NDDependentCount:
        # Dependencies are ordered outermost first by QCoDeS. Verify each
        # repeated inner-axis cycle, retaining fingerprints rather than rows
        # or sets whose size would grow with the acquired data.
        changed = next(
            (i for i, (a, b) in enumerate(zip(state.previous, values, strict=True))
             if a != b),
            None,
        ) if state.previous else 0
        if changed is None:
            return replace(state, viable=False)
        axes = list(state.axes)
        for index in range(changed, len(axes)):
            axis = axes[index]
            value = values[index]
            if index > changed and state.previous:
                signature = (axis.count, axis.fingerprint)
                if axis.reference is not None and signature != axis.reference:
                    return replace(state, viable=False)
                axis = _NDCountAxis(reference=axis.reference or signature)
            direction = 0 if axis.last is None else (1 if value > axis.last else -1)
            if axis.last is not None and (
                value == axis.last or (axis.direction and direction != axis.direction)
            ):
                return replace(state, viable=False)
            axes[index] = replace(
                axis,
                count=axis.count + 1,
                last=value,
                direction=axis.direction or direction,
                fingerprint=(
                    axis.fingerprint * _TRUSTED_VECTOR_FINGERPRINT_BASE
                    + _numeric_fingerprint_token(value)
                ) & _TRUSTED_VECTOR_FINGERPRINT_MASK,
            )
        return replace(state, axes=tuple(axes), previous=values, count=state.count + 1)

    def _advance_nd_counts(
        self,
        run_id: int,
        table_name: str,
        dependency_sets: tuple[tuple[tuple[str, ...], str | None], ...],
        result_columns: tuple[str, ...],
        result_watermark: int,
        *,
        shared_page: TrustedQueryResult | None = None,
    ) -> tuple[
        list[tuple[tuple[str, ...], int, tuple[int, ...] | None]], dict[str, int],
    ]:
        """Prove nD scalar grid counts with one bounded primary-key page.

        Mixed runs include every dependent's own grid. No multidimensional
        plot proof is created, and no measurement values are read or retained.
        Unsupported orderings remain unknown rather than using physical row
        counts as logical setpoint counts.
        """
        signature = tuple(
            (names, dependent) for names, dependent in dependency_sets
            if dependent is not None
        )
        columns = tuple(dict.fromkeys(
            name for names, dependent in signature for name in (*names, dependent)
        ))
        if (
            not any(len(names) > 2 for names, _dependent in signature)
            or len(signature) != len(dependency_sets)
            or len(signature) > TRUSTED_DERIVED_MAX_DEPENDENTS
            or len(columns) > TRUSTED_DERIVED_MAX_SOURCE_COLUMNS
            or any(name not in result_columns for name in columns)
            or result_watermark <= 0
        ):
            self._nd_counts.pop(run_id, None)
            return [], {}
        previous = self._nd_counts.get(run_id)
        if (
            previous is None or previous.signature != signature
            or previous.result_watermark > result_watermark
        ):
            previous = _NDRunCounts(
                signature, result_watermark,
                tuple(_NDDependentCount(names, dependent, tuple(
                    _NDCountAxis() for _name in names
                )) for names, dependent in signature),
            )
        if previous.after_row_id < result_watermark:
            names = tuple(dict.fromkeys(
                name for dependencies, _dependent in signature for name in dependencies
            ))
            dependents = tuple(dependent for _names, dependent in signature)
            expected_columns = (
                "id", *(f"qplot_axis_{i}" for i in range(len(names))),
                *(f"qplot_present_{i}" for i in range(len(dependents))),
            )
            page = shared_page
            if page is None:
                page = self._executor.query(
                    f"SELECT {self._regular_probe_projection(names, dependents)} "
                    f"FROM {quote_sqlite_identifier(table_name)} "
                    'WHERE "id" > ? AND "id" <= ? ORDER BY "id" LIMIT ?',
                    (previous.after_row_id, result_watermark, min(
                        _TRUSTED_PROGRESSIVE_LAYOUT_PAGE_ROWS,
                        TRUSTED_DERIVED_MAX_SAMPLE_CELLS // len(expected_columns),
                    )),
                )
            if page.columns != expected_columns or not page.rows:
                raise TrustedMetadataQueryError("An nD count page has an invalid schema.")
            states = list(previous.states)
            positions = {name: i + 1 for i, name in enumerate(names)}
            cursor = previous.after_row_id
            for row in page.rows:
                if (
                    len(row) != len(expected_columns) or type(row[0]) is not int
                    or row[0] != cursor + 1
                    or any(type(value) is not int or value not in (0, 1)
                           for value in row[1 + len(names):])
                ):
                    raise TrustedMetadataQueryError("An nD count page has invalid rows.")
                cursor = row[0]
                for index, state in enumerate(states):
                    if not state.viable or not row[1 + len(names) + index]:
                        continue
                    values = tuple(row[positions[name]] for name in state.dependencies)
                    if any(type(value) not in (int, float) or not math.isfinite(value)
                           for value in values):
                        states[index] = replace(state, viable=False)
                    else:
                        states[index] = self._consume_nd_count(
                            state, cast(tuple[int | float, ...], values),
                        )
            # Commit only after the complete page was accepted. A failed or
            # cancelled query leaves the previous immutable cursor untouched.
            previous = _NDRunCounts(signature, result_watermark, tuple(states), cursor)
            self._nd_counts[run_id] = previous
        if previous.after_row_id != result_watermark:
            return [], {}
        observations: list[tuple[tuple[str, ...], int, tuple[int, ...] | None]] = []
        steps: dict[str, int] = {}
        conflicts: set[str] = set()
        incomplete_shape = False
        for state in previous.states:
            if not state.viable or not state.count:
                return [], {}
            shape = tuple(axis.count for axis in state.axes)
            if math.prod(shape) != state.count or any(
                axis.reference is not None
                and axis.reference != (axis.count, axis.fingerprint)
                for axis in state.axes
            ):
                # A partial final slab still has unique ordered coordinates,
                # so its acquired count is proven without a complete shape.
                observations.append((state.dependencies, state.count, None))
                incomplete_shape = True
                continue
            observations.append((state.dependencies, state.count, shape))
            for name, size in zip(state.dependencies, shape, strict=True):
                if name in steps and steps[name] != size:
                    conflicts.add(name)
                steps[name] = size
        return observations, (
            {} if incomplete_shape else {name: size for name, size in steps.items()
                                         if name not in conflicts}
        )

    @staticmethod
    def independent_signature(
        dependency_sets: tuple[tuple[tuple[str, ...], str | None], ...],
        run_description: dict[str, Any],
    ) -> tuple[tuple[tuple[str, str], str, tuple[int, int] | None], ...]:
        shapes = run_description.get("shapes")
        array_parameters = array_parameter_names(run_description)
        # Keep array dependents in dependency_sets for row-ownership checks,
        # but only scalar trees can prove one sample per physical grid row.
        signatures = []
        for dependencies, dependent in dependency_sets[:TRUSTED_DERIVED_MAX_DEPENDENTS]:
            if (
                dependent is None
                or len(dependencies) != 2
                or len(set(dependencies)) != 2
                or array_parameters.intersection((dependent, *dependencies))
            ):
                continue
            raw_shape = shapes.get(dependent) if isinstance(shapes, dict) else None
            planned_shape = (
                cast(tuple[int, int], tuple(raw_shape))
                if isinstance(raw_shape, (list, tuple))
                and len(raw_shape) == 2
                and all(type(value) is int and value > 0 for value in raw_shape)
                and math.prod(raw_shape) <= (1 << 63) - 1
                else None
            )
            signatures.append(
                (cast(tuple[str, str], dependencies), dependent, planned_shape)
            )
        return tuple(signatures)

    def _advance_independent_layouts(
        self,
        run_id: int,
        table_name: str,
        dependency_sets: tuple[tuple[tuple[str, ...], str | None], ...],
        run_description: dict[str, Any],
        result_columns: tuple[str, ...],
        result_watermark: int,
    ) -> TrustedQueryResult | None:
        """Share one bounded keyset page across all independent verifiers.

        Filtering happens after the physical page is read, so a missing or
        invalid group cannot increase the scan budget or starve another group.
        State is committed only after the query succeeds; cancellation leaves
        the previous accepted cursor intact. Return the transient page for
        optional shared-grid verification; no result rows enter the cache.
        """

        signature = self.independent_signature(dependency_sets, run_description)
        columns = tuple(name for name in result_columns if name != "id")[
            :TRUSTED_DERIVED_MAX_SOURCE_COLUMNS
        ]
        signature = tuple(
            item
            for item in signature
            if all(name in columns for name in (*item[0], item[1]))
        )
        if not signature or result_watermark <= 0:
            self._independent.pop(run_id, None)
            return None
        previous = self._independent.get(run_id)
        if (
            previous is None
            or previous.signature != signature
            or previous.result_watermark > result_watermark
        ):
            previous = _IndependentRunLayouts(
                signature,
                result_watermark,
                tuple(_IndependentDependentLayout(*item) for item in signature),
            )
        if previous.after_row_id == result_watermark:
            return None
        table = quote_sqlite_identifier(table_name)
        owner_names = tuple(
            dependent
            for _names, dependent in dependency_sets
            if dependent is not None and dependent in columns
        )
        axis_names = tuple(
            dict.fromkeys(
                name
                for dependencies, _dependent, _shape in signature
                for name in dependencies
            )
        )
        # The nD count verifier consumes this same page. Project the union
        # of its axes too, so mixed runs still read only one physical prefix
        # page per request rather than duplicating the scan for counts.
        if any(len(names) > 2 for names, _dependent in dependency_sets):
            all_axes = tuple(dict.fromkeys(
                name for names, _dependent in dependency_sets for name in names
            ))
            if len(set((*all_axes, *owner_names))) <= TRUSTED_DERIVED_MAX_SOURCE_COLUMNS:
                axis_names = all_axes
        # Ownership comes from the stored value's presence, not its numeric
        # projection. QCoDeS can store NaN as non-numeric text: it still owns
        # its row even though it cannot supply a finite measured sample.
        projection = self._regular_probe_projection(axis_names, owner_names)
        expected_columns = (
            "id",
            *(f"qplot_axis_{index}" for index in range(len(axis_names))),
            *(f"qplot_present_{index}" for index in range(len(owner_names))),
        )
        page_rows = min(
            _TRUSTED_PROGRESSIVE_LAYOUT_PAGE_ROWS,
            # An axis can itself be a dependent, needing both projections.
            TRUSTED_DERIVED_MAX_SAMPLE_CELLS // len(expected_columns),
        )
        page = self._executor.query(
            f"SELECT {projection} FROM {table} "
            'WHERE "id" > ? AND "id" <= ? ORDER BY "id" LIMIT ?',
            (
                previous.after_row_id,
                result_watermark,
                page_rows,
            ),
        )
        if page.columns != expected_columns or not page.rows:
            raise TrustedMetadataQueryError(
                "An independent layout page has an invalid schema or cursor."
            )
        states = list(previous.states)
        positions = {name: index + 1 for index, name in enumerate(axis_names)}
        owner_positions = {
            name: 1 + len(axis_names) + index for index, name in enumerate(owner_names)
        }
        after_row_id = previous.after_row_id
        for row in page.rows:
            if (
                len(row) != len(page.columns)
                or type(row[0]) is not int
                or row[0] != after_row_id + 1
                or any(
                    type(row[position]) is not int or row[position] not in (0, 1)
                    for position in owner_positions.values()
                )
            ):
                raise TrustedMetadataQueryError(
                    "An independent layout page has invalid result ids or ownership markers."
                )
            after_row_id = row[0]
            present_owners = {
                name for name, position in owner_positions.items() if row[position]
            }
            for index, state in enumerate(states):
                if not state.viable:
                    continue
                axes = tuple(row[positions[name]] for name in state.dependencies)
                owns_row = bool(row[owner_positions[state.dependent]])
                if not owns_row:
                    # A peer-owned row cannot be a missing sample of this
                    # grid merely because dependency names or ids match.
                    # Only unowned rows can fill an inferred physical phase;
                    # shared verification separately proves compatible NULLs.
                    if present_owners:
                        continue
                    if state.fast_axis_index is not None:
                        expected_id = (
                            state.first_row_id
                            + state.axis.next_slow_index * state.slow_id_stride
                            + state.axis.next_fast_index * state.fast_id_stride
                        )
                        if state.fast_axis_index is None or expected_id != row[0]:
                            continue
                valid_axes = all(
                    type(value) in (int, float) and math.isfinite(float(value))
                    for value in axes
                )
                if not valid_axes:
                    if owns_row:
                        states[index] = replace(state, viable=False)
                    continue
                if (
                    state.count == 1
                    and state.first_value_missing
                    and axes == state.first_axes
                ):
                    # An initially unowned NULL phase has ambiguous
                    # ownership. A present value at the same coordinate
                    # identifies this dependent's actual first physical row.
                    if owns_row:
                        states[index] = replace(
                            state, first_row_id=row[0], first_value_missing=False
                        )
                    continue
                states[index] = self._consume_independent_layout_row(
                    state,
                    row[0],
                    cast(tuple[int | float, ...], axes),
                )
                if state.count == 0:
                    states[index] = replace(
                        states[index], first_value_missing=not owns_row
                    )
        proofs = (
            tuple(
                proof
                for state in states
                if (proof := self._finish_independent_layout(state)) is not None
            )
            if after_row_id == result_watermark
            else ()
        )
        self._independent[run_id] = _IndependentRunLayouts(
            signature,
            result_watermark,
            tuple(states),
            after_row_id,
            proofs,
        )
        return page

    @staticmethod
    def _finish_independent_first_row(
        axis: _ProgressiveAxisState,
    ) -> _ProgressiveAxisState:
        """Close an inferred first row once its actual fast extent is known."""

        return _ProgressiveAxisState(
            next_slow_index=1,
            previous_slow_value=axis.current_slow_value,
            reference_forward=axis.current_forward,
            reference_reverse=axis.current_reverse,
            reference_fast_direction=axis.current_fast_direction,
            completed_rows=1,
        )

    @classmethod
    def _consume_independent_layout_row(
        cls,
        state: _IndependentDependentLayout,
        row_id: int,
        axes: tuple[int | float, ...],
    ) -> _IndependentDependentLayout:
        if state.count == 0:
            return replace(state, first_row_id=row_id, first_axes=axes, count=1)
        if state.fast_axis_index is None:
            changed = cls._changed_axis_indexes(state.first_axes, axes)
            if len(changed) != 1:
                return replace(state, viable=False)
            initial_fast_axis = changed[0]
            if (
                state.planned_shape is not None
                and state.planned_shape[initial_fast_axis] == 1
            ):
                initial_fast_axis = 1 - initial_fast_axis
            fast_count = (
                state.planned_shape[initial_fast_axis] if state.planned_shape else 0
            )
            first_axis, _event = cls._consume_progressive_axis_value(
                _ProgressiveAxisState(),
                slow_index=0,
                fast_index=0,
                slow_value=state.first_axes[1 - initial_fast_axis],
                fast_value=state.first_axes[initial_fast_axis],
                fast_count=fast_count or (1 << 63) - 1,
                checkpoint_count=0,
            )
            state = replace(
                state,
                fast_axis_index=initial_fast_axis,
                fast_count=fast_count,
                fast_id_stride=(row_id - state.first_row_id if fast_count != 1 else 1),
                axis=first_axis,
            )
        fast_axis = state.fast_axis_index
        assert fast_axis is not None
        axis = state.axis
        if not state.fast_count and axes[1 - fast_axis] != axis.current_slow_value:
            state = replace(
                state,
                fast_count=axis.next_fast_index,
                axis=cls._finish_independent_first_row(axis),
            )
            axis = state.axis
        if (
            axis.next_slow_index == 1
            and axis.next_fast_index == 0
            and not state.slow_id_stride
        ):
            state = replace(state, slow_id_stride=row_id - state.first_row_id)
            if state.slow_id_stride <= (state.fast_count - 1) * state.fast_id_stride:
                return replace(state, viable=False)
        expected_id = (
            state.first_row_id
            + axis.next_slow_index * state.slow_id_stride
            + axis.next_fast_index * state.fast_id_stride
        )
        if row_id != expected_id:
            return replace(state, viable=False)
        updated, _event = cls._consume_progressive_axis_value(
            axis,
            slow_index=axis.next_slow_index,
            fast_index=axis.next_fast_index,
            slow_value=axes[1 - fast_axis],
            fast_value=axes[fast_axis],
            fast_count=state.fast_count or (1 << 63) - 1,
            checkpoint_count=0,
        )
        return replace(
            state, axis=updated, count=state.count + 1, viable=updated.viable
        )

    @classmethod
    def _finish_independent_layout(
        cls, state: _IndependentDependentLayout
    ) -> Trusted2DLayoutProof | None:
        if not state.viable or state.fast_axis_index is None:
            return None
        fast_count = state.fast_count or state.count
        axis = (
            state.axis
            if state.fast_count
            else cls._finish_independent_first_row(state.axis)
        )
        if axis.next_fast_index or state.count % fast_count:
            return None
        shape = [0, 0]
        shape[state.fast_axis_index] = fast_count
        shape[1 - state.fast_axis_index] = axis.completed_rows
        if state.planned_shape is not None and tuple(shape) != state.planned_shape:
            return None
        return Trusted2DLayoutProof(
            state.dependencies,
            cast(tuple[int, int], tuple(shape)),
            state.fast_axis_index,
            (
                Trusted2DDependentRowPattern(
                    state.dependent,
                    state.first_row_id,
                    state.fast_id_stride,
                    state.slow_id_stride or fast_count * state.fast_id_stride,
                ),
            ),
            "planned" if state.planned_shape else "observed",
        )

    def _infer_regular_setpoint_observation(
        self,
        table_name: str,
        dependency_sets: tuple[tuple[tuple[str, ...], str | None], ...],
        result_watermark: int,
        *,
        proof_source: str,
    ) -> _RegularSetpointObservation | None:
        """Prove one current-QCoDeS 1D/2D rectangular result layout.

        The common prefix classifies dense, per-point interleaved, or
        dependent-blocked storage.  Exponential and binary primary-key probes
        then find an arbitrarily large inner-axis period without scanning the
        intervening rows.  A fixed set of domain-wide probes rejects ordering,
        dependency-presence, or accepted-watermark inconsistencies before an
        exact shape is published.
        """

        if result_watermark <= 0 or not dependency_sets:
            return None
        dependency_names = {names for names, _dependent in dependency_sets}
        if len(dependency_names) != 1:
            return None
        dependencies = next(iter(dependency_names))
        if len(dependencies) not in (1, 2):
            return None
        raw_dependents = tuple(
            dependent for _names, dependent in dependency_sets if dependent is not None
        )
        dependents = tuple(dict.fromkeys(raw_dependents))
        if (
            len(dependents) != len(raw_dependents)
            or len(dependents) > _TRUSTED_REGULAR_LAYOUT_MAX_DEPENDENTS
            or (
                not dependents
                and any(dependent is not None for _n, dependent in dependency_sets)
            )
        ):
            return None

        prefix_through = min(
            result_watermark,
            _TRUSTED_REGULAR_LAYOUT_PREFIX_ROWS,
        )
        prefix = self._regular_layout_range(
            table_name,
            dependencies,
            dependents,
            0,
            prefix_through,
        )
        if prefix is None or tuple(row.row_id for row in prefix) != tuple(
            range(1, prefix_through + 1)
        ):
            return None
        classified = self._regular_storage_kind(prefix, dependents)
        if classified is None:
            return None
        storage_kind, phase_order = classified
        if storage_kind == "blocked":
            inferred = self._infer_blocked_regular_layout(
                table_name,
                dependencies,
                dependents,
                prefix,
                result_watermark,
            )
        else:
            inferred = self._infer_strided_regular_layout(
                table_name,
                dependencies,
                dependents,
                prefix,
                result_watermark,
                storage_kind=storage_kind,
                phase_order=phase_order,
            )
        if inferred is None:
            return None
        shape, fast_axis_index, patterns = inferred
        if not self._validate_regular_layout(
            table_name,
            dependencies,
            dependents,
            result_watermark,
            shape,
            fast_axis_index,
            patterns,
            storage_kind=storage_kind,
        ):
            return None

        proof = None
        if len(dependencies) == 2 and dependents and fast_axis_index is not None:
            proof = Trusted2DLayoutProof(
                dependencies=cast(tuple[str, str], dependencies),
                shape=cast(tuple[int, int], shape),
                fast_axis_index=fast_axis_index,
                dependent_rows=tuple(
                    Trusted2DDependentRowPattern(
                        pattern.dependent,
                        pattern.first_row_id,
                        pattern.fast_id_stride,
                        pattern.slow_id_stride,
                    )
                    for pattern in patterns
                    if pattern.dependent is not None
                ),
                source=proof_source,
            )
        return _RegularSetpointObservation(
            dependencies,
            result_watermark,
            math.prod(shape),
            shape,
            proof,
            storage_kind,
            patterns,
        )

    def _planned_regular_setpoint_candidate(
        self,
        table_name: str,
        dependency_sets: tuple[tuple[tuple[str, ...], str | None], ...],
        result_watermark: int,
        planned_shape: tuple[int, ...] | None,
    ) -> _RegularSetpointObservation | None:
        """Build a structurally bounded candidate for a live planned extent."""

        if (
            result_watermark <= 0
            or planned_shape is None
            or len(planned_shape) not in (1, 2)
            or any(type(value) is not int or value <= 0 for value in planned_shape)
            or not dependency_sets
        ):
            return None
        dependency_names = {names for names, _dependent in dependency_sets}
        if len(dependency_names) != 1:
            return None
        dependencies = next(iter(dependency_names))
        if len(dependencies) != len(planned_shape):
            return None
        raw_dependents = tuple(
            dependent for _names, dependent in dependency_sets if dependent is not None
        )
        dependents = tuple(dict.fromkeys(raw_dependents))
        if (
            len(dependents) != len(raw_dependents)
            or len(dependents) > _TRUSTED_REGULAR_LAYOUT_MAX_DEPENDENTS
        ):
            return None
        prefix_through = min(result_watermark, _TRUSTED_REGULAR_LAYOUT_PREFIX_ROWS)
        prefix = self._regular_layout_range(
            table_name,
            dependencies,
            dependents,
            0,
            prefix_through,
        )
        if prefix is None or tuple(row.row_id for row in prefix) != tuple(
            range(1, prefix_through + 1)
        ):
            return None
        classified = self._regular_storage_kind(prefix, dependents)
        if classified is None:
            return None
        storage_kind, phase_order = classified
        if storage_kind not in {"dense", "interleaved"}:
            # A dependent-blocked live prefix needs its first complete physical
            # slow row before phase starts and row strides can be proven.  The
            # ordinary bounded candidate path handles completed rectangles.
            return None
        multiplicity = len(dependents) if storage_kind == "interleaved" else 1
        if result_watermark % multiplicity:
            return None
        logical_count = result_watermark // multiplicity
        if not 0 < logical_count <= math.prod(planned_shape):
            return None

        if len(planned_shape) == 1:
            fast_axis_index = None
            fast_count = planned_shape[0]
        else:
            primary_first_id = 1
            if storage_kind == "interleaved":
                primary_first_id = phase_order.index(0) + 1
            first = self._regular_layout_direct(
                table_name,
                dependencies,
                dependents,
                (primary_first_id, primary_first_id + multiplicity),
                result_watermark,
            )
            if first is None or len(first) != 2:
                return None
            changed = self._changed_axis_indexes(first[0].axes, first[1].axes)
            if len(changed) != 1:
                return None
            fast_axis_index = changed[0]
            fast_count = planned_shape[fast_axis_index]

        if storage_kind == "interleaved":
            first_ids = {
                dependent_index: phase + 1
                for phase, dependent_index in enumerate(phase_order)
            }
            patterns = tuple(
                _RegularDependentPattern(
                    dependent,
                    dependent_index,
                    first_ids[dependent_index],
                    multiplicity,
                    fast_count * multiplicity,
                )
                for dependent_index, dependent in enumerate(dependents)
            )
        else:
            patterns = tuple(
                _RegularDependentPattern(
                    dependent if dependents else None,
                    dependent_index if dependents else 0,
                    1,
                    1,
                    fast_count,
                )
                for dependent_index, dependent in enumerate(dependents or ("",))
            )

        proof = None
        if len(planned_shape) == 2 and dependents and fast_axis_index is not None:
            proof = Trusted2DLayoutProof(
                dependencies=cast(tuple[str, str], dependencies),
                shape=cast(tuple[int, int], planned_shape),
                fast_axis_index=fast_axis_index,
                dependent_rows=tuple(
                    Trusted2DDependentRowPattern(
                        pattern.dependent,
                        pattern.first_row_id,
                        pattern.fast_id_stride,
                        pattern.slow_id_stride,
                    )
                    for pattern in patterns
                    if pattern.dependent is not None
                ),
                source="planned",
            )
        return _RegularSetpointObservation(
            dependencies,
            result_watermark,
            logical_count,
            planned_shape,
            proof,
            storage_kind,
            patterns,
        )

    @classmethod
    def _progressive_candidate_from_page(
        cls,
        dependencies: tuple[str, ...],
        dependents: tuple[str, ...],
        result_watermark: int,
        page: tuple[_RegularLayoutRow, ...],
        *,
        proof_source: str,
    ) -> _RegularSetpointObservation | None:
        """Derive a candidate from the first full-prefix page, never a proof."""

        prefix = page[:_TRUSTED_REGULAR_LAYOUT_PREFIX_ROWS]
        classified = cls._regular_storage_kind(prefix, dependents)
        if classified is None:
            return None
        storage_kind, phase_order = classified
        if storage_kind not in {"dense", "interleaved"}:
            return None
        multiplicity = len(dependents) if storage_kind == "interleaved" else 1
        if result_watermark % multiplicity:
            return None
        logical_count = result_watermark // multiplicity
        if logical_count <= 0:
            return None

        if storage_kind == "dense":
            logical_axes = tuple(row.axes for row in page)
        else:
            complete_groups = len(page) // multiplicity
            axes: list[tuple[int | float, ...]] = []
            for group_index in range(complete_groups):
                group = page[
                    group_index * multiplicity : (group_index + 1) * multiplicity
                ]
                if any(row.axes != group[0].axes for row in group):
                    return None
                for phase, row in enumerate(group):
                    present_count = sum(row.present)
                    if present_count not in (0, 1):
                        return None
                    if present_count == 1 and not row.present[phase_order[phase]]:
                        return None
                axes.append(group[0].axes)
            logical_axes = tuple(axes)
        if not logical_axes:
            return None

        if len(dependencies) == 1:
            shape: tuple[int, ...] = (logical_count,)
            fast_axis_index = None
            fast_count = logical_count
        else:
            if len(logical_axes) < 2:
                return None
            changed = cls._changed_axis_indexes(logical_axes[0], logical_axes[1])
            if len(changed) != 1:
                return None
            fast_axis_index = changed[0]
            slow_axis_index = 1 - fast_axis_index
            fast_count = next(
                (
                    index
                    for index, axes in enumerate(logical_axes[1:], start=1)
                    if axes[slow_axis_index] != logical_axes[0][slow_axis_index]
                ),
                0,
            )
            if fast_count == 0:
                if len(logical_axes) < logical_count:
                    return None
                fast_count = logical_count
            if logical_count % fast_count:
                return None
            shape_values = [0, 0]
            shape_values[fast_axis_index] = fast_count
            shape_values[slow_axis_index] = logical_count // fast_count
            shape = tuple(shape_values)

        if storage_kind == "interleaved":
            first_ids = {
                dependent_index: phase + 1
                for phase, dependent_index in enumerate(phase_order)
            }
            patterns = tuple(
                _RegularDependentPattern(
                    dependent,
                    dependent_index,
                    first_ids[dependent_index],
                    multiplicity,
                    fast_count * multiplicity,
                )
                for dependent_index, dependent in enumerate(dependents)
            )
        else:
            patterns = tuple(
                _RegularDependentPattern(
                    dependent if dependents else None,
                    dependent_index if dependents else 0,
                    1,
                    1,
                    fast_count,
                )
                for dependent_index, dependent in enumerate(dependents or ("",))
            )
        proof = None
        if len(shape) == 2 and dependents and fast_axis_index is not None:
            proof = Trusted2DLayoutProof(
                cast(tuple[str, str], dependencies),
                cast(tuple[int, int], shape),
                fast_axis_index,
                tuple(
                    Trusted2DDependentRowPattern(
                        pattern.dependent,
                        pattern.first_row_id,
                        pattern.fast_id_stride,
                        pattern.slow_id_stride,
                    )
                    for pattern in patterns
                    if pattern.dependent is not None
                ),
                proof_source,
            )
        return _RegularSetpointObservation(
            dependencies,
            result_watermark,
            logical_count,
            shape,
            proof,
            storage_kind,
            patterns,
        )

    def _advance_progressive_regular_layout(
        self,
        run_id: int,
        table_name: str,
        dependencies: tuple[str, ...],
        dependents: tuple[str, ...],
        result_watermark: int,
        *,
        proof_source: str,
        candidate: _RegularSetpointObservation | None = None,
        shared_page: tuple[_RegularLayoutRow, ...] | None = None,
        page_is_shared: bool = False,
    ) -> _RegularSetpointObservation | None:
        """Verify exactly one bounded full-prefix keyset page per invocation."""

        if result_watermark <= 0 or len(dependencies) not in (1, 2):
            self._progress.pop(run_id, None)
            self._verified.pop(run_id, None)
            return None
        state = self._progress.get(run_id)
        page = shared_page
        if page_is_shared and page is None:
            self._progress.pop(run_id, None)
            return None
        if (
            state is not None
            and state.result_watermark != result_watermark
            and candidate is not None
        ):
            state = self._resume_progressive_regular_layout(state, candidate)
            if state is None:
                self._progress.pop(run_id, None)
        if state is None:
            verified = self._verified.get(run_id)
            if verified is not None and candidate is not None:
                state = self._resume_progressive_regular_layout(verified, candidate)
            if state is None:
                self._verified.pop(run_id, None)
                if candidate is None:
                    if page is None:
                        page = self._progressive_regular_layout_page(
                            table_name,
                            dependencies,
                            dependents,
                            0,
                            result_watermark,
                        )
                    if not page or tuple(row.row_id for row in page) != tuple(
                        range(1, len(page) + 1)
                    ):
                        return None
                    candidate = self._progressive_candidate_from_page(
                        dependencies,
                        dependents,
                        result_watermark,
                        page,
                        proof_source=proof_source,
                    )
                if candidate is None:
                    return None
                state = self._new_progressive_regular_layout(candidate)
        if (
            state.dependencies != dependencies
            or state.dependents != dependents
            or state.result_watermark != result_watermark
        ):
            self._progress.pop(run_id, None)
            return None
        if page is None:
            page = self._progressive_regular_layout_page(
                table_name,
                dependencies,
                dependents,
                state.after_row_id,
                result_watermark,
            )
        if not page or tuple(row.row_id for row in page) != tuple(
            range(state.after_row_id + 1, state.after_row_id + len(page) + 1)
        ):
            self._progress.pop(run_id, None)
            return None
        state = self._consume_progressive_regular_page(state, page)
        if not state.viable:
            self._progress.pop(run_id, None)
            self._verified.pop(run_id, None)
            return None
        if state.after_row_id < result_watermark:
            self._progress[run_id] = state
            return None

        self._progress.pop(run_id, None)
        observation = self._finish_progressive_regular_layout(state)
        if observation is None:
            self._verified.pop(run_id, None)
            return None
        self._verified[run_id] = state
        return observation

    @staticmethod
    def _progressive_fast_axis_index(
        observation: _RegularSetpointObservation,
    ) -> int:
        if len(observation.shape) == 1:
            return 0
        if observation.proof_2d is None:
            raise ValueError("A two-dimensional candidate requires a physical proof.")
        return observation.proof_2d.fast_axis_index

    @classmethod
    def _progressive_fast_count(
        cls,
        observation: _RegularSetpointObservation,
    ) -> int:
        return observation.shape[cls._progressive_fast_axis_index(observation)]

    @staticmethod
    def _progressive_physical_patterns(
        observation: _RegularSetpointObservation,
    ) -> tuple[_ProgressivePhysicalPattern, ...]:
        grouped: dict[tuple[int, int, int], list[int]] = {}
        for pattern in observation.patterns:
            grouped.setdefault(
                (
                    pattern.first_row_id,
                    pattern.fast_id_stride,
                    pattern.slow_id_stride,
                ),
                [],
            ).append(pattern.present_index)
        return tuple(
            _ProgressivePhysicalPattern(*signature, tuple(sorted(owners)))
            for signature, owners in sorted(grouped.items())
        )

    @classmethod
    def _new_progressive_regular_layout(
        cls,
        observation: _RegularSetpointObservation,
    ) -> _ProgressiveRegularLayout:
        physical_patterns = cls._progressive_physical_patterns(observation)
        if not physical_patterns:
            raise ValueError("A progressive candidate requires physical patterns.")
        dependents = tuple(
            pattern.dependent
            for pattern in observation.patterns
            if pattern.dependent is not None
        )
        return _ProgressiveRegularLayout(
            observation.dependencies,
            dependents,
            observation.result_watermark,
            observation.proof_2d.source if observation.proof_2d else "observed",
            observation,
            physical_patterns,
            tuple(_ProgressiveAxisState() for _pattern in physical_patterns),
        )

    @classmethod
    def _progressive_candidates_compatible(
        cls,
        previous: _RegularSetpointObservation,
        current: _RegularSetpointObservation,
    ) -> bool:
        previous_fast_axis = cls._progressive_fast_axis_index(previous)
        current_fast_axis = cls._progressive_fast_axis_index(current)
        return (
            previous.dependencies == current.dependencies
            and previous.storage_kind == current.storage_kind
            and previous_fast_axis == current_fast_axis
            and previous.shape[previous_fast_axis] == current.shape[current_fast_axis]
            and cls._progressive_physical_patterns(previous)
            == cls._progressive_physical_patterns(current)
            and previous.count <= current.count
            and previous.result_watermark < current.result_watermark
        )

    @classmethod
    def _resume_progressive_regular_layout(
        cls,
        previous: _ProgressiveRegularLayout,
        candidate: _RegularSetpointObservation,
    ) -> _ProgressiveRegularLayout | None:
        if not cls._progressive_candidates_compatible(
            previous.observation,
            candidate,
        ):
            return None
        fast_count = cls._progressive_fast_count(candidate)
        previous_remainder = previous.observation.count % fast_count
        current_remainder = candidate.count % fast_count
        if previous_remainder and current_remainder not in {
            previous_remainder,
            0,
        }:
            # The reference row did not retain a checkpoint for an arbitrary
            # newly extended partial length.  Keep the older accepted prefix
            # rather than rescan it or make a guessed partial claim.
            return None
        return replace(
            previous,
            result_watermark=candidate.result_watermark,
            proof_source=(
                candidate.proof_2d.source
                if candidate.proof_2d is not None
                else previous.proof_source
            ),
            observation=candidate,
            viable=True,
        )

    def _progressive_regular_layout_page(
        self,
        table_name: str,
        dependencies: tuple[str, ...],
        dependents: tuple[str, ...],
        after_row_id: int,
        result_watermark: int,
    ) -> tuple[_RegularLayoutRow, ...] | None:
        table = quote_sqlite_identifier(table_name)
        result = self._executor.query(
            f"SELECT {self._regular_probe_projection(dependencies, dependents)} "
            f'FROM {table} WHERE "id" > ? AND "id" <= ? '
            f'ORDER BY "id" LIMIT ?',
            (
                after_row_id,
                result_watermark,
                _TRUSTED_PROGRESSIVE_LAYOUT_PAGE_ROWS,
            ),
        )
        return self._materialize_regular_layout_rows(
            result,
            len(dependencies),
            max(1, len(dependents)),
        )

    @classmethod
    def _progressive_pattern_grid_index(
        cls,
        observation: _RegularSetpointObservation,
        pattern: _ProgressivePhysicalPattern,
        row_id: int,
    ) -> tuple[int, int] | None:
        fast_axis_index = cls._progressive_fast_axis_index(observation)
        fast_count = observation.shape[fast_axis_index]
        slow_count = (
            1 if len(observation.shape) == 1 else observation.shape[1 - fast_axis_index]
        )
        delta = row_id - pattern.first_row_id
        if delta < 0:
            return None
        slow_index, within_slow = divmod(delta, pattern.slow_id_stride)
        if within_slow % pattern.fast_id_stride:
            return None
        fast_index = within_slow // pattern.fast_id_stride
        if slow_index >= slow_count or fast_index >= fast_count:
            return None
        return slow_index, fast_index

    @classmethod
    def _consume_progressive_regular_page(
        cls,
        state: _ProgressiveRegularLayout,
        page: tuple[_RegularLayoutRow, ...],
    ) -> _ProgressiveRegularLayout:
        axis_states = list(state.axis_states)
        current = state
        fast_axis_index = cls._progressive_fast_axis_index(state.observation)
        slow_axis_index = 0 if len(state.dependencies) == 1 else 1 - fast_axis_index
        for row in page:
            matches = tuple(
                (pattern_index, grid_index)
                for pattern_index, pattern in enumerate(state.physical_patterns)
                if (
                    grid_index := cls._progressive_pattern_grid_index(
                        state.observation,
                        pattern,
                        row.row_id,
                    )
                )
                is not None
            )
            if len(matches) != 1:
                return replace(current, after_row_id=row.row_id, viable=False)
            pattern_index, (slow_index, fast_index) = matches[0]
            pattern = state.physical_patterns[pattern_index]
            present_count = sum(row.present)
            if state.observation.storage_kind != "dense" and not (
                present_count == 0
                or (
                    present_count == 1
                    and any(row.present[index] for index in pattern.owner_indexes)
                )
            ):
                return replace(current, after_row_id=row.row_id, viable=False)

            if state.observation.storage_kind == "interleaved":
                logical_index = (
                    slow_index * cls._progressive_fast_count(state.observation)
                    + fast_index
                )
                if current.pending_logical_index is None:
                    if pattern_index != 0:
                        return replace(current, after_row_id=row.row_id, viable=False)
                    current = replace(
                        current,
                        pending_logical_index=logical_index,
                        pending_axes=row.axes,
                        pending_pattern_indexes=(pattern_index,),
                    )
                else:
                    expected_pattern = len(current.pending_pattern_indexes)
                    if (
                        logical_index != current.pending_logical_index
                        or row.axes != current.pending_axes
                        or pattern_index != expected_pattern
                    ):
                        return replace(current, after_row_id=row.row_id, viable=False)
                    pending = (*current.pending_pattern_indexes, pattern_index)
                    current = replace(current, pending_pattern_indexes=pending)
                if len(current.pending_pattern_indexes) == len(state.physical_patterns):
                    current = replace(
                        current,
                        pending_logical_index=None,
                        pending_axes=None,
                        pending_pattern_indexes=(),
                    )

            updated_axis, event = cls._consume_progressive_axis_value(
                axis_states[pattern_index],
                slow_index=slow_index,
                fast_index=fast_index,
                slow_value=(
                    0 if len(state.dependencies) == 1 else row.axes[slow_axis_index]
                ),
                fast_value=row.axes[fast_axis_index],
                fast_count=cls._progressive_fast_count(state.observation),
                checkpoint_count=(
                    state.observation.count
                    % cls._progressive_fast_count(state.observation)
                ),
            )
            axis_states[pattern_index] = updated_axis
            if not updated_axis.viable:
                return replace(current, after_row_id=row.row_id, viable=False)
            if event is not None:
                current = cls._consume_progressive_cross_event(
                    current,
                    pattern_index,
                    event,
                )
                if not current.viable:
                    return replace(current, after_row_id=row.row_id)
        return replace(
            current,
            after_row_id=page[-1].row_id,
            axis_states=tuple(axis_states),
        )

    @staticmethod
    def _consume_progressive_axis_value(
        state: _ProgressiveAxisState,
        *,
        slow_index: int,
        fast_index: int,
        slow_value: int | float,
        fast_value: int | float,
        fast_count: int,
        checkpoint_count: int,
    ) -> tuple[
        _ProgressiveAxisState,
        tuple[int, int | float, int, int] | None,
    ]:
        if (
            not state.viable
            or slow_index != state.next_slow_index
            or fast_index != state.next_fast_index
        ):
            return replace(state, viable=False), None
        if fast_index == 0:
            if state.current_slow_value is not None:
                return replace(state, viable=False), None
            slow_direction = state.slow_direction
            if state.previous_slow_value is not None:
                direction = _strict_numeric_direction(
                    state.previous_slow_value,
                    slow_value,
                )
                if direction == 0 or (slow_direction and direction != slow_direction):
                    return replace(state, viable=False), None
                slow_direction = direction
            state = replace(
                state,
                current_slow_value=slow_value,
                slow_direction=slow_direction,
            )
        elif slow_value != state.current_slow_value:
            return replace(state, viable=False), None

        fast_direction = state.current_fast_direction
        if state.previous_fast_value is not None:
            direction = _strict_numeric_direction(state.previous_fast_value, fast_value)
            if direction == 0 or (fast_direction and direction != fast_direction):
                return replace(state, viable=False), None
            fast_direction = direction
        forward, reverse, power = _append_vector_fingerprint(
            state.current_forward,
            state.current_reverse,
            state.current_power,
            fast_value,
        )
        count = fast_index + 1
        checkpoints = state.reference_checkpoints
        wanted = {
            value
            for value in (checkpoint_count, fast_count - checkpoint_count)
            if 0 < value < fast_count
        }
        if (
            state.reference_forward is None
            and count in wanted
            and all(item[0] != count for item in checkpoints)
        ):
            checkpoints = (*checkpoints, (count, forward, reverse, power))
        updated = replace(
            state,
            next_fast_index=count,
            previous_fast_value=fast_value,
            current_fast_direction=fast_direction,
            current_forward=forward,
            current_reverse=reverse,
            current_power=power,
            reference_checkpoints=checkpoints,
        )
        if count < fast_count:
            return updated, None

        reference_forward = updated.reference_forward
        reference_reverse = updated.reference_reverse
        reference_direction = updated.reference_fast_direction
        row_major_possible = updated.row_major_possible
        serpentine_possible = updated.serpentine_possible
        if fast_count > 1 and fast_direction == 0:
            return replace(updated, viable=False), None
        if reference_forward is None:
            reference_forward = forward
            reference_reverse = reverse
            reference_direction = fast_direction
        else:
            same = forward == reference_forward and reverse == reference_reverse
            reversed_row = forward == reference_reverse and reverse == reference_forward
            if same and reversed_row and fast_count > 1:
                same = fast_direction == reference_direction
                reversed_row = fast_direction == -reference_direction
            if not same and not reversed_row:
                return replace(updated, viable=False), None
            row_major_possible = row_major_possible and same
            serpentine_possible = serpentine_possible and (
                same if slow_index % 2 == 0 else reversed_row
            )
            if not row_major_possible and not serpentine_possible:
                return replace(updated, viable=False), None
        assert updated.current_slow_value is not None
        event = (
            slow_index,
            updated.current_slow_value,
            forward,
            reverse,
        )
        return (
            replace(
                updated,
                next_slow_index=slow_index + 1,
                next_fast_index=0,
                previous_slow_value=updated.current_slow_value,
                current_slow_value=None,
                previous_fast_value=None,
                current_fast_direction=0,
                current_forward=0,
                current_reverse=0,
                current_power=1,
                reference_forward=reference_forward,
                reference_reverse=reference_reverse,
                reference_fast_direction=reference_direction,
                row_major_possible=row_major_possible,
                serpentine_possible=serpentine_possible,
                completed_rows=updated.completed_rows + 1,
            ),
            event,
        )

    @staticmethod
    def _consume_progressive_cross_event(
        state: _ProgressiveRegularLayout,
        pattern_index: int,
        event: tuple[int, int | float, int, int],
    ) -> _ProgressiveRegularLayout:
        slow_index, slow_value, forward, reverse = event
        if pattern_index != state.cross_next_pattern:
            return replace(state, viable=False)
        if pattern_index == 0:
            state = replace(
                state,
                cross_slow_index=slow_index,
                cross_reference_slow=slow_value,
                cross_reference_forward=forward,
                cross_reference_reverse=reverse,
                cross_next_pattern=1,
            )
        elif (
            slow_index != state.cross_slow_index
            or slow_value != state.cross_reference_slow
            or not (
                (
                    forward == state.cross_reference_forward
                    and reverse == state.cross_reference_reverse
                )
                or (
                    forward == state.cross_reference_reverse
                    and reverse == state.cross_reference_forward
                )
            )
        ):
            return replace(state, viable=False)
        else:
            state = replace(state, cross_next_pattern=pattern_index + 1)
        if state.cross_next_pattern == len(state.physical_patterns):
            return replace(
                state,
                cross_slow_index=None,
                cross_reference_slow=None,
                cross_reference_forward=None,
                cross_reference_reverse=None,
                cross_next_pattern=0,
            )
        return state

    @staticmethod
    def _progressive_partial_axis_matches(
        state: _ProgressiveAxisState,
        fast_count: int,
        partial_count: int,
    ) -> bool:
        if (
            partial_count <= 0
            or state.reference_forward is None
            or state.reference_reverse is None
            or state.next_fast_index != partial_count
        ):
            return False
        checkpoints = {item[0]: item for item in state.reference_checkpoints}
        prefix = checkpoints.get(partial_count)
        before_suffix = checkpoints.get(fast_count - partial_count)
        if prefix is None or before_suffix is None:
            return False
        _count, prefix_forward, _prefix_reverse, _prefix_power = prefix
        _count, _before_forward, before_reverse, before_power = before_suffix
        inverse_power = pow(
            before_power,
            -1,
            1 << _TRUSTED_VECTOR_FINGERPRINT_BITS,
        )
        reversed_suffix = (
            (state.reference_reverse - before_reverse) * inverse_power
        ) & _TRUSTED_VECTOR_FINGERPRINT_MASK
        return state.current_forward in {prefix_forward, reversed_suffix}

    @classmethod
    def _finish_progressive_regular_layout(
        cls,
        state: _ProgressiveRegularLayout,
    ) -> _RegularSetpointObservation | None:
        observation = state.observation
        fast_count = cls._progressive_fast_count(observation)
        full_rows, partial_count = divmod(observation.count, fast_count)
        if (
            not state.viable
            or state.after_row_id != observation.result_watermark
            or state.pending_logical_index is not None
            or state.cross_next_pattern != 0
            or any(axis.completed_rows != full_rows for axis in state.axis_states)
        ):
            return None
        if partial_count:
            if observation.proof_2d is None or observation.proof_2d.source != "planned":
                return None
            if not all(
                cls._progressive_partial_axis_matches(
                    axis,
                    fast_count,
                    partial_count,
                )
                for axis in state.axis_states
            ):
                return None
            slow_values = {axis.current_slow_value for axis in state.axis_states}
            forward_values = {axis.current_forward for axis in state.axis_states}
            if len(slow_values) != 1 or len(forward_values) != 1:
                return None
        elif any(
            axis.next_fast_index != 0
            or axis.next_slow_index != full_rows
            or axis.current_slow_value is not None
            for axis in state.axis_states
        ):
            return None
        if (
            observation.proof_2d is not None
            and observation.proof_2d.source == "observed"
            and observation.count != math.prod(observation.shape)
        ):
            return None
        return observation

    @staticmethod
    def _regular_probe_projection(
        dependencies: tuple[str, ...],
        dependents: tuple[str, ...],
    ) -> str:
        axes = tuple(
            "CASE WHEN typeof({column}) IN ('integer', 'real') "
            "THEN {column} ELSE NULL END AS {alias}".format(
                column=quote_sqlite_identifier(name),
                alias=quote_sqlite_identifier(f"qplot_axis_{index}"),
            )
            for index, name in enumerate(dependencies)
        )
        if dependents:
            present = tuple(
                "CASE WHEN {column} IS NOT NULL THEN 1 ELSE 0 END AS {alias}".format(
                    column=quote_sqlite_identifier(name),
                    alias=quote_sqlite_identifier(f"qplot_present_{index}"),
                )
                for index, name in enumerate(dependents)
            )
        else:
            conditions = " AND ".join(
                f"{quote_sqlite_identifier(name)} IS NOT NULL" for name in dependencies
            )
            present = (
                f'CASE WHEN {conditions} THEN 1 ELSE 0 END AS "qplot_present_0"',
            )
        return ", ".join(('"id"', *axes, *present))

    def _regular_layout_range(
        self,
        table_name: str,
        dependencies: tuple[str, ...],
        dependents: tuple[str, ...],
        lower_id: int,
        upper_id: int,
    ) -> tuple[_RegularLayoutRow, ...] | None:
        if not 0 <= lower_id < upper_id:
            return None
        limit = min(
            _TRUSTED_REGULAR_LAYOUT_PREFIX_ROWS,
            upper_id - lower_id,
        )
        table = quote_sqlite_identifier(table_name)
        result = self._executor.query(
            f"SELECT {self._regular_probe_projection(dependencies, dependents)} "
            f'FROM {table} WHERE "id" > ? AND "id" <= ? '
            f'ORDER BY "id" LIMIT ?',
            (lower_id, upper_id, limit),
        )
        return self._materialize_regular_layout_rows(
            result,
            len(dependencies),
            max(1, len(dependents)),
        )

    def _regular_layout_direct(
        self,
        table_name: str,
        dependencies: tuple[str, ...],
        dependents: tuple[str, ...],
        row_ids: tuple[int, ...],
        result_watermark: int,
    ) -> tuple[_RegularLayoutRow, ...] | None:
        expected_ids = tuple(
            sorted(
                {
                    row_id
                    for row_id in row_ids
                    if type(row_id) is int and 0 < row_id <= result_watermark
                }
            )
        )
        if not expected_ids or len(expected_ids) > 1_024:
            return None
        placeholders = ", ".join("?" for _row_id in expected_ids)
        table = quote_sqlite_identifier(table_name)
        result = self._executor.query(
            f"SELECT {self._regular_probe_projection(dependencies, dependents)} "
            f'FROM {table} WHERE "id" IN ({placeholders}) '
            f'AND "id" <= ? ORDER BY "id"',
            (*expected_ids, result_watermark),
        )
        rows = self._materialize_regular_layout_rows(
            result,
            len(dependencies),
            max(1, len(dependents)),
        )
        if rows is None or tuple(row.row_id for row in rows) != expected_ids:
            return None
        return rows

    @staticmethod
    def _materialize_regular_layout_rows(
        result: TrustedQueryResult,
        axis_count: int,
        present_count: int,
    ) -> tuple[_RegularLayoutRow, ...] | None:
        expected_columns = (
            "id",
            *(f"qplot_axis_{index}" for index in range(axis_count)),
            *(f"qplot_present_{index}" for index in range(present_count)),
        )
        if tuple(result.columns) != expected_columns:
            return None
        output: list[_RegularLayoutRow] = []
        previous_id = 0
        for raw_row in result.rows:
            if len(raw_row) != len(expected_columns):
                return None
            row_id = raw_row[0]
            raw_axes = raw_row[1 : 1 + axis_count]
            raw_present = raw_row[1 + axis_count :]
            if type(row_id) is not int or row_id <= previous_id:
                return None
            axes: list[int | float] = []
            for value in raw_axes:
                if type(value) not in (int, float) or not math.isfinite(float(value)):
                    return None
                axes.append(cast(int | float, value))
            if any(
                type(value) is not int or value not in (0, 1) for value in raw_present
            ):
                return None
            output.append(
                _RegularLayoutRow(
                    row_id,
                    tuple(axes),
                    tuple(bool(value) for value in raw_present),
                )
            )
            previous_id = row_id
        return tuple(output)

    @staticmethod
    def _regular_storage_kind(
        prefix: tuple[_RegularLayoutRow, ...],
        dependents: tuple[str, ...],
    ) -> tuple[str, tuple[int, ...]] | None:
        dependent_count = max(1, len(dependents))
        all_present = (True,) * dependent_count
        if len(dependents) == 1:
            # A single registered dependent owns every physical QCoDeS row.
            # Its value may legitimately be SQL NULL at a measured grid cell;
            # the numeric dependency columns, not z-value presence, prove the
            # row's coordinate.  Multiple dependents need additional shared-row
            # evidence below because NULL markers can also encode sparse storage.
            return "dense", (0,)
        if not dependents:
            return (
                ("dense", (0,))
                if all(row.present == all_present for row in prefix)
                else None
            )
        # One row containing multiple registered outputs proves that those
        # outputs share a physical coordinate row.  Other outputs may be SQL
        # NULL at genuinely unmeasured cells; dependency coordinates, row ids,
        # and sweep ordering remain the structural evidence.  If every row has
        # at most one output, storage is still ambiguous and must pass the
        # conservative interleaved/blocked classification below.
        if any(sum(row.present) > 1 for row in prefix):
            return "dense", tuple(range(dependent_count))

        # Interleaved QCoDeS rows repeat one fixed dependent phase order at each
        # coordinate.  A phase's z value may be NULL, so learn its owner from
        # another complete prefix group while requiring every observed owner to
        # agree and the final phase mapping to be a full permutation.
        phase_owners: list[int | None] = [None] * dependent_count
        complete_groups = len(prefix) // dependent_count
        interleaved_possible = complete_groups > 0
        for group_index in range(complete_groups):
            start = group_index * dependent_count
            group = prefix[start : start + dependent_count]
            if any(row.axes != group[0].axes for row in group):
                interleaved_possible = False
                break
            for phase, row in enumerate(group):
                present_count = sum(row.present)
                if present_count == 0:
                    continue
                if present_count != 1:
                    interleaved_possible = False
                    break
                owner = row.present.index(True)
                known_owner = phase_owners[phase]
                if known_owner is None:
                    phase_owners[phase] = owner
                elif known_owner != owner:
                    interleaved_possible = False
                    break
            if not interleaved_possible:
                break
        if interleaved_possible and None not in phase_owners:
            phase_order = tuple(cast(int, owner) for owner in phase_owners)
            if set(phase_order) == set(range(dependent_count)):
                return "interleaved", phase_order

        first_present = prefix[0].present
        if sum(first_present) != 1:
            return None
        if len(prefix) == 1 or (
            prefix[1].present == first_present and prefix[1].axes != prefix[0].axes
        ):
            return "blocked", (first_present.index(True),)
        return None

    def _infer_strided_regular_layout(
        self,
        table_name: str,
        dependencies: tuple[str, ...],
        dependents: tuple[str, ...],
        prefix: tuple[_RegularLayoutRow, ...],
        result_watermark: int,
        *,
        storage_kind: str,
        phase_order: tuple[int, ...],
    ) -> (
        tuple[
            tuple[int, ...],
            int | None,
            tuple[_RegularDependentPattern, ...],
        ]
        | None
    ):
        dependent_count = max(1, len(dependents))
        multiplicity = dependent_count if storage_kind == "interleaved" else 1
        if result_watermark % multiplicity:
            return None
        logical_count = result_watermark // multiplicity
        if logical_count <= 0:
            return None
        if storage_kind == "interleaved":
            first_ids = {
                dependent_index: phase + 1
                for phase, dependent_index in enumerate(phase_order)
            }
            patterns = tuple(
                _RegularDependentPattern(
                    dependent,
                    dependent_index,
                    first_ids[dependent_index],
                    multiplicity,
                    logical_count * multiplicity,
                )
                for dependent_index, dependent in enumerate(dependents)
            )
        else:
            patterns = tuple(
                _RegularDependentPattern(
                    dependent if dependents else None,
                    dependent_index if dependents else 0,
                    1,
                    1,
                    logical_count,
                )
                for dependent_index, dependent in enumerate(dependents or ("",))
            )
        primary = patterns[0]
        first_row = prefix[primary.first_row_id - 1]
        if len(dependencies) == 1:
            if logical_count > 1:
                second_id = primary.first_row_id + primary.fast_id_stride
                rows = self._regular_layout_direct(
                    table_name,
                    dependencies,
                    dependents,
                    (second_id,),
                    result_watermark,
                )
                if rows is None or rows[0].axes == first_row.axes:
                    return None
            patterns = tuple(
                _RegularDependentPattern(
                    pattern.dependent,
                    pattern.present_index,
                    pattern.first_row_id,
                    pattern.fast_id_stride,
                    logical_count * pattern.fast_id_stride,
                )
                for pattern in patterns
            )
            return (logical_count,), None, patterns

        if logical_count == 1:
            fast_axis_index = 1
            inner_count = 1
        else:
            second_id = primary.first_row_id + primary.fast_id_stride
            second = self._regular_layout_direct(
                table_name,
                dependencies,
                dependents,
                (second_id,),
                result_watermark,
            )
            if second is None:
                return None
            changed = self._changed_axis_indexes(first_row.axes, second[0].axes)
            if len(changed) != 1:
                return None
            fast_axis_index = changed[0]
            outer_axis_index = 1 - fast_axis_index
            observed_inner_count = self._first_outer_axis_transition(
                table_name,
                dependencies,
                dependents,
                primary,
                logical_count,
                outer_axis_index,
                first_row.axes[outer_axis_index],
                result_watermark,
                storage_kind=storage_kind,
            )
            if observed_inner_count is None:
                return None
            inner_count = observed_inner_count
        if logical_count % inner_count:
            return None
        slow_count = logical_count // inner_count
        shape_values = [0, 0]
        shape_values[fast_axis_index] = inner_count
        shape_values[1 - fast_axis_index] = slow_count
        patterns = tuple(
            _RegularDependentPattern(
                pattern.dependent,
                pattern.present_index,
                pattern.first_row_id,
                pattern.fast_id_stride,
                inner_count * pattern.fast_id_stride,
            )
            for pattern in patterns
        )
        return cast(tuple[int, int], tuple(shape_values)), fast_axis_index, patterns

    def _infer_blocked_regular_layout(
        self,
        table_name: str,
        dependencies: tuple[str, ...],
        dependents: tuple[str, ...],
        prefix: tuple[_RegularLayoutRow, ...],
        result_watermark: int,
    ) -> (
        tuple[
            tuple[int, ...],
            int | None,
            tuple[_RegularDependentPattern, ...],
        ]
        | None
    ):
        if len(dependents) < 2:
            return None
        initial_present = prefix[0].present
        boundary = self._first_block_boundary(
            table_name,
            dependencies,
            dependents,
            initial_present,
            result_watermark,
        )
        if boundary is None:
            return None
        inner_count = boundary - 1
        dependent_count = len(dependents)
        slow_id_stride = dependent_count * inner_count
        if inner_count <= 0 or result_watermark % slow_id_stride:
            return None
        slow_count = result_watermark // slow_id_stride
        block_ids = tuple(
            row_id
            for block_index in range(dependent_count)
            for row_id in (
                block_index * inner_count + 1,
                (block_index + 1) * inner_count,
            )
        )
        block_rows = self._regular_layout_direct(
            table_name,
            dependencies,
            dependents,
            block_ids,
            result_watermark,
        )
        if block_rows is None:
            return None
        by_id = {row.row_id: row for row in block_rows}
        block_order: list[int] = []
        for block_index in range(dependent_count):
            start = by_id[block_index * inner_count + 1]
            end = by_id[(block_index + 1) * inner_count]
            if (
                sum(start.present) != 1
                or start.present != end.present
                or start.axes != prefix[0].axes
                or (inner_count > 1 and end.axes == start.axes)
            ):
                return None
            block_order.append(start.present.index(True))
        if set(block_order) != set(range(dependent_count)):
            return None
        patterns = tuple(
            _RegularDependentPattern(
                dependents[dependent_index],
                dependent_index,
                block_order.index(dependent_index) * inner_count + 1,
                1,
                slow_id_stride,
            )
            for dependent_index in range(dependent_count)
        )
        logical_count = inner_count * slow_count
        if len(dependencies) == 1:
            if slow_count != 1:
                return None
            return (logical_count,), None, patterns

        if inner_count > 1:
            second = self._regular_layout_direct(
                table_name,
                dependencies,
                dependents,
                (2,),
                result_watermark,
            )
            if second is None:
                return None
            changed = self._changed_axis_indexes(prefix[0].axes, second[0].axes)
            if len(changed) != 1:
                return None
            fast_axis_index = changed[0]
        elif slow_count > 1:
            next_slow = self._regular_layout_direct(
                table_name,
                dependencies,
                dependents,
                (slow_id_stride + 1,),
                result_watermark,
            )
            if next_slow is None:
                return None
            changed = self._changed_axis_indexes(prefix[0].axes, next_slow[0].axes)
            if len(changed) != 1:
                return None
            fast_axis_index = 1 - changed[0]
        else:
            fast_axis_index = 1
        if slow_count > 1:
            next_slow = self._regular_layout_direct(
                table_name,
                dependencies,
                dependents,
                (slow_id_stride + 1,),
                result_watermark,
            )
            if next_slow is None:
                return None
            changed = self._changed_axis_indexes(prefix[0].axes, next_slow[0].axes)
            if changed != (1 - fast_axis_index,):
                return None
        shape_values = [0, 0]
        shape_values[fast_axis_index] = inner_count
        shape_values[1 - fast_axis_index] = slow_count
        return cast(tuple[int, int], tuple(shape_values)), fast_axis_index, patterns

    def _first_block_boundary(
        self,
        table_name: str,
        dependencies: tuple[str, ...],
        dependents: tuple[str, ...],
        initial_present: tuple[bool, ...],
        result_watermark: int,
    ) -> int | None:
        candidates = self._exponential_probe_values(result_watermark, minimum=2)
        rows = self._regular_layout_direct(
            table_name,
            dependencies,
            dependents,
            candidates,
            result_watermark,
        )
        if rows is None:
            return None
        lower = 1
        upper = None
        for row in rows:
            if row.present == initial_present:
                lower = row.row_id
            else:
                upper = row.row_id
                break
        if upper is None:
            return None
        for _probe in range(_TRUSTED_REGULAR_LAYOUT_MAX_SEARCH_PROBES):
            if upper - lower <= 1:
                return upper
            middle = lower + ((upper - lower) // 2)
            middle_rows = self._regular_layout_direct(
                table_name,
                dependencies,
                dependents,
                (middle,),
                result_watermark,
            )
            if middle_rows is None:
                return None
            if middle_rows[0].present == initial_present:
                lower = middle
            else:
                upper = middle
        return None

    def _first_outer_axis_transition(
        self,
        table_name: str,
        dependencies: tuple[str, ...],
        dependents: tuple[str, ...],
        pattern: _RegularDependentPattern,
        logical_count: int,
        outer_axis_index: int,
        initial_outer_value: int | float,
        result_watermark: int,
        *,
        storage_kind: str,
    ) -> int | None:
        maximum_index = logical_count - 1
        indexes = self._exponential_probe_values(maximum_index, minimum=1)
        row_ids = tuple(
            pattern.first_row_id + index * pattern.fast_id_stride for index in indexes
        )
        rows = self._regular_layout_direct(
            table_name,
            dependencies,
            dependents,
            row_ids,
            result_watermark,
        )
        if rows is None:
            return None
        by_id = {row.row_id: row for row in rows}
        lower = 0
        upper = None
        for index, row_id in zip(indexes, row_ids, strict=True):
            row = by_id[row_id]
            if not self._regular_row_matches_pattern(
                row, pattern, storage_kind, len(dependents)
            ):
                return None
            if row.axes[outer_axis_index] == initial_outer_value:
                lower = index
            else:
                upper = index
                break
        if upper is None:
            return logical_count
        for _probe in range(_TRUSTED_REGULAR_LAYOUT_MAX_SEARCH_PROBES):
            if upper - lower <= 1:
                return upper
            middle = lower + ((upper - lower) // 2)
            row_id = pattern.first_row_id + middle * pattern.fast_id_stride
            middle_rows = self._regular_layout_direct(
                table_name,
                dependencies,
                dependents,
                (row_id,),
                result_watermark,
            )
            if middle_rows is None or not self._regular_row_matches_pattern(
                middle_rows[0], pattern, storage_kind, len(dependents)
            ):
                return None
            if middle_rows[0].axes[outer_axis_index] == initial_outer_value:
                lower = middle
            else:
                upper = middle
        return None

    @staticmethod
    def _exponential_probe_values(maximum: int, *, minimum: int) -> tuple[int, ...]:
        if maximum < minimum:
            return ()
        values: list[int] = []
        value = 1
        while value < minimum:
            value *= 2
        while (
            value <= maximum and len(values) < _TRUSTED_REGULAR_LAYOUT_MAX_SEARCH_PROBES
        ):
            values.append(value)
            value *= 2
        if not values or values[-1] != maximum:
            values.append(maximum)
        return tuple(dict.fromkeys(values))

    @staticmethod
    def _changed_axis_indexes(
        first: tuple[int | float, ...],
        second: tuple[int | float, ...],
    ) -> tuple[int, ...]:
        return tuple(
            index
            for index, (left, right) in enumerate(zip(first, second, strict=True))
            if left != right
        )

    @staticmethod
    def _regular_row_matches_pattern(
        row: _RegularLayoutRow,
        pattern: _RegularDependentPattern,
        storage_kind: str,
        dependent_count: int,
    ) -> bool:
        if dependent_count == 0:
            return row.present == (True,)
        if dependent_count == 1:
            return len(row.present) == 1
        if storage_kind == "dense":
            # Dense/shared ownership was established by the bounded prefix.
            # Missing measured values do not remove the physical grid row.
            return len(row.present) == dependent_count
        # The proven physical phase/stride owns this sparse row even when its
        # one measured value is NULL.  A value belonging to another dependent,
        # or multiple present dependents, still contradicts the row pattern.
        present_count = sum(row.present)
        return present_count == 0 or (
            present_count == 1 and row.present[pattern.present_index]
        )

    def _validate_regular_layout(
        self,
        table_name: str,
        dependencies: tuple[str, ...],
        dependents: tuple[str, ...],
        result_watermark: int,
        shape: tuple[int, ...],
        fast_axis_index: int | None,
        patterns: tuple[_RegularDependentPattern, ...],
        *,
        storage_kind: str,
    ) -> bool:
        if math.prod(shape) <= 0 or not patterns:
            return False
        slow_indexes: tuple[int, ...]
        if len(shape) == 1:
            slow_indexes = (0,)
            fast_indexes = _symmetric_grid_indexes(
                shape[0],
                min(shape[0], _TRUSTED_REGULAR_LAYOUT_ANCHOR_ROWS),
            )
            fast_count = shape[0]
        else:
            assert fast_axis_index is not None
            slow_count = shape[1 - fast_axis_index]
            fast_count = shape[fast_axis_index]
            slow_indexes = _symmetric_grid_indexes(
                slow_count,
                min(slow_count, _TRUSTED_REGULAR_LAYOUT_ANCHOR_ROWS),
            )
            fast_indexes = tuple(sorted({0, fast_count // 2, fast_count - 1}))
        requests = tuple(
            (
                pattern,
                slow_index,
                fast_index,
                pattern.first_row_id
                + slow_index * pattern.slow_id_stride
                + fast_index * pattern.fast_id_stride,
            )
            for pattern in patterns
            for slow_index in slow_indexes
            for fast_index in fast_indexes
        )
        rows = self._regular_layout_direct(
            table_name,
            dependencies,
            dependents,
            tuple(request[3] for request in requests),
            result_watermark,
        )
        if rows is None:
            return False
        by_id = {row.row_id: row for row in rows}
        coordinates: dict[tuple[int, int], tuple[int | float, ...]] = {}
        for pattern, slow_index, fast_index, row_id in requests:
            row = by_id[row_id]
            if not self._regular_row_matches_pattern(
                row,
                pattern,
                storage_kind,
                len(dependents),
            ):
                return False
            grid_index = (slow_index, fast_index)
            known = coordinates.setdefault(grid_index, row.axes)
            if known != row.axes:
                return False
        maximum_id = max(
            pattern.first_row_id
            + (slow_indexes[-1] if len(shape) == 2 else 0) * pattern.slow_id_stride
            + (fast_count - 1) * pattern.fast_id_stride
            for pattern in patterns
        )
        full_maximum_id = max(
            pattern.first_row_id
            + ((shape[1 - cast(int, fast_axis_index)] - 1) if len(shape) == 2 else 0)
            * pattern.slow_id_stride
            + (fast_count - 1) * pattern.fast_id_stride
            for pattern in patterns
        )
        if maximum_id > result_watermark or full_maximum_id != result_watermark:
            return False
        if len(shape) == 1:
            values = [coordinates[(0, index)][0] for index in fast_indexes]
            return len(set(values)) == len(values)

        assert fast_axis_index is not None
        slow_axis_index = 1 - fast_axis_index
        slow_values: list[int | float] = []
        baseline_fast: tuple[int | float, ...] | None = None
        for slow_index in slow_indexes:
            row_coordinates = tuple(
                coordinates[(slow_index, fast_index)] for fast_index in fast_indexes
            )
            row_slow_values = tuple(
                coordinate[slow_axis_index] for coordinate in row_coordinates
            )
            if len(set(row_slow_values)) != 1:
                return False
            slow_values.append(row_slow_values[0])
            row_fast = tuple(
                coordinate[fast_axis_index] for coordinate in row_coordinates
            )
            if len(set(row_fast)) != len(row_fast):
                return False
            if baseline_fast is None:
                baseline_fast = row_fast
            elif row_fast not in (baseline_fast, tuple(reversed(baseline_fast))):
                return False
        return len(set(slow_values)) == len(slow_values)
