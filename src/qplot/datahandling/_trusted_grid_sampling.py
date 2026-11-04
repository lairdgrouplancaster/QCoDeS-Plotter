"""Pure bounded grid sampling and orientation validation.

Physical row-layout proofs come from the result-layout verifier. Sampling keeps
its row budget and validates scientific axes independently of SQL execution."""

from __future__ import annotations

import math
from dataclasses import dataclass

from qplot.datahandling.trusted_query_types import (
    TRUSTED_DERIVED_MAX_GRID_SAMPLE_ROWS,
    PrimitiveScalar,
    Trusted2DDependentRowPattern,
    Trusted2DGridLayout,
    Trusted2DLayoutProof,
)


def _balanced_grid_sample_shape(
    slow_count: int,
    fast_count: int,
    maximum: int,
) -> tuple[int, int]:
    """Choose similarly downsampled axis counts within one exact row budget."""

    if (
        type(slow_count) is not int
        or type(fast_count) is not int
        or slow_count <= 0
        or fast_count <= 0
        or type(maximum) is not int
        or maximum <= 0
    ):
        raise ValueError("Grid sample dimensions and budget must be positive integers.")
    if slow_count * fast_count <= maximum:
        return slow_count, fast_count

    # A non-singleton axis must contribute both endpoints.  When an entire
    # small axis fits below the square-root budget, preserve it before using
    # the remaining rows proportionally along the large axis.  This retains
    # every slow step in very wide scientific sweeps without changing the
    # balanced allocation used for ordinary aspect ratios.
    # Four slow-axis samples (when available) are the smallest mirror-symmetric
    # set that contains both acquisition parities.  That lets a bounded sample
    # distinguish row-major from serpentine storage even for an extremely wide
    # grid whose proportional allocation would otherwise choose only its two
    # same-parity endpoints.
    sampled_slow = min(slow_count, 4)
    sampled_fast = min(fast_count, 2)
    if sampled_slow * sampled_fast > maximum:
        # Preserve the helper's small-budget behaviour for defensive callers.
        # Production per-pattern budgets are always large enough for the
        # parity-capable minimum; a smaller caller will simply fail orientation
        # validation if its two endpoints have the same parity.
        sampled_slow = min(slow_count, 2)
    complete_axis_limit = math.isqrt(maximum)
    if slow_count <= fast_count and slow_count <= complete_axis_limit:
        sampled_slow = slow_count
    elif fast_count < slow_count and fast_count <= complete_axis_limit:
        sampled_fast = fast_count
    if sampled_slow * sampled_fast > maximum:
        raise ValueError("The grid sample budget cannot cover both axis extents.")
    while True:
        can_add_slow = (
            sampled_slow < slow_count and (sampled_slow + 1) * sampled_fast <= maximum
        )
        can_add_fast = (
            sampled_fast < fast_count and sampled_slow * (sampled_fast + 1) <= maximum
        )
        if not can_add_slow and not can_add_fast:
            break
        if can_add_slow and (
            not can_add_fast or sampled_slow * fast_count <= sampled_fast * slow_count
        ):
            sampled_slow += 1
        else:
            sampled_fast += 1
    return sampled_slow, sampled_fast


def _symmetric_grid_indexes(length: int, count: int) -> tuple[int, ...]:
    if not 1 <= count <= length:
        raise ValueError("A sampled axis count must fit its source axis.")
    if count == length:
        return tuple(range(length))
    if count == 1:
        return (0,)

    # An even-length integer axis has no self-mirroring centre cell, so an odd
    # number of samples cannot be exactly mirror-symmetric.  Use one fewer
    # sample in that case; ``count`` is an upper bound and both endpoints remain
    # represented.
    sample_count = count - 1 if length % 2 == 0 and count % 2 else count
    indexes = [0] * sample_count
    pair_count = sample_count // 2
    denominator = sample_count - 1
    for index in range(pair_count):
        lower = (index * (length - 1)) // denominator
        indexes[index] = lower
        indexes[sample_count - 1 - index] = length - 1 - lower
    if sample_count % 2:
        indexes[pair_count] = (length - 1) // 2
    result = tuple(indexes)
    if length > 3 and len(result) >= 4 and 1 not in result:
        # Adjacent rows expose acquisition direction and serpentine parity
        # early.  Replace the second sample and its mirror together: changing
        # only one side would make bin-rank reversal mix different scientific
        # coordinates on alternating rows.
        result = (result[0], 1, *result[2:-2], length - 2, result[-1])
    if tuple(sorted(set(result))) != result or result != tuple(
        length - 1 - value for value in reversed(result)
    ):
        raise RuntimeError("A representative axis sample lost mirror symmetry.")
    return result


def _representative_grid_sample(
    slow_count: int,
    fast_count: int,
    *,
    maximum_rows: int = TRUSTED_DERIVED_MAX_GRID_SAMPLE_ROWS,
    first_row_id: int = 1,
    fast_id_stride: int = 1,
    slow_id_stride: int | None = None,
) -> tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...]]:
    """Return bounded physical PK probes spread across a complete 2D grid."""

    sampled_slow_count, sampled_fast_count = _balanced_grid_sample_shape(
        slow_count,
        fast_count,
        maximum_rows,
    )
    slow_indexes = _symmetric_grid_indexes(slow_count, sampled_slow_count)
    fast_indexes = _symmetric_grid_indexes(fast_count, sampled_fast_count)
    if slow_id_stride is None:
        slow_id_stride = fast_count * fast_id_stride
    ids = tuple(
        first_row_id + slow_index * slow_id_stride + fast_index * fast_id_stride
        for slow_index in slow_indexes
        for fast_index in fast_indexes
    )
    if (
        type(first_row_id) is not int
        or first_row_id <= 0
        or type(fast_id_stride) is not int
        or fast_id_stride <= 0
        or type(slow_id_stride) is not int
        or slow_id_stride <= 0
        or len(ids) > maximum_rows
        or ids[-1] > (1 << 63) - 1
    ):
        raise ValueError("A representative grid sample exceeds bounded row ids.")
    return ids, slow_indexes, fast_indexes


@dataclass(frozen=True, slots=True)
class _Trusted2DSamplePlan:
    proof: Trusted2DLayoutProof
    pattern: Trusted2DDependentRowPattern
    row_ids: tuple[int, ...]
    slow_indexes: tuple[int, ...]
    fast_indexes: tuple[int, ...]


def _trusted_2d_sample_plans(
    proof: Trusted2DLayoutProof,
    *,
    maximum_rows: int = TRUSTED_DERIVED_MAX_GRID_SAMPLE_ROWS,
) -> tuple[_Trusted2DSamplePlan, ...]:
    physical_patterns = {
        (
            pattern.first_row_id,
            pattern.fast_id_stride,
            pattern.slow_id_stride,
        )
        for pattern in proof.dependent_rows
    }
    per_pattern_budget = max(
        1,
        maximum_rows // len(physical_patterns),
    )
    slow_count = proof.shape[1 - proof.fast_axis_index]
    fast_count = proof.shape[proof.fast_axis_index]
    samples: dict[
        tuple[int, int, int],
        tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...]],
    ] = {}
    plans: list[_Trusted2DSamplePlan] = []
    for pattern in proof.dependent_rows:
        physical_pattern = (
            pattern.first_row_id,
            pattern.fast_id_stride,
            pattern.slow_id_stride,
        )
        sample = samples.get(physical_pattern)
        if sample is None:
            sample = _representative_grid_sample(
                slow_count,
                fast_count,
                maximum_rows=per_pattern_budget,
                first_row_id=pattern.first_row_id,
                fast_id_stride=pattern.fast_id_stride,
                slow_id_stride=pattern.slow_id_stride,
            )
            samples[physical_pattern] = sample
        plans.append(_Trusted2DSamplePlan(proof, pattern, *sample))
    return tuple(plans)


def _pattern_grid_index(
    pattern: Trusted2DDependentRowPattern,
    row_id: int,
    *,
    slow_count: int,
    fast_count: int,
) -> tuple[int, int] | None:
    relative = row_id - pattern.first_row_id
    if relative < 0:
        return None
    slow_index, remainder = divmod(relative, pattern.slow_id_stride)
    if remainder % pattern.fast_id_stride:
        return None
    fast_index = remainder // pattern.fast_id_stride
    if slow_index >= slow_count or fast_index >= fast_count:
        return None
    return slow_index, fast_index


def _strict_axis_reversed(values: list[tuple[int, float]]) -> bool | None:
    ordered = sorted(values)
    if len(ordered) < 2:
        return False
    deltas = tuple(
        later[1] - earlier[1]
        for earlier, later in zip(ordered, ordered[1:], strict=False)
    )
    if all(delta > 0.0 and math.isfinite(delta) for delta in deltas):
        return False
    if all(delta < 0.0 and math.isfinite(delta) for delta in deltas):
        return True
    return None


def _sampled_fast_values_match(
    reference: dict[int, float],
    current: dict[int, float],
    fast_count: int,
    *,
    reversed_axis: bool,
) -> bool:
    for fast_index, fast_value in current.items():
        reference_index = fast_count - 1 - fast_index if reversed_axis else fast_index
        reference_value = reference.get(reference_index)
        if reference_value is None or not math.isclose(
            fast_value,
            reference_value,
            rel_tol=1e-12,
            abs_tol=0.0,
        ):
            return False
    return True


def _validated_sampled_grid_layout(
    plan: _Trusted2DSamplePlan,
    *,
    sample_columns: tuple[str, ...],
    sample_rows: tuple[tuple[PrimitiveScalar, ...], ...],
    result_watermark: int,
    completed: bool,
) -> Trusted2DGridLayout | None:
    proof = plan.proof
    pattern = plan.pattern
    fast_axis = proof.dependencies[proof.fast_axis_index]
    slow_axis = proof.dependencies[1 - proof.fast_axis_index]
    indexes = {name: index for index, name in enumerate(sample_columns)}
    if any(name not in indexes for name in (slow_axis, fast_axis, pattern.dependent)):
        return None
    slow_count = proof.shape[1 - proof.fast_axis_index]
    fast_count = proof.shape[proof.fast_axis_index]
    returned = {row[0]: row for row in sample_rows if row and type(row[0]) is int}
    expected = {row_id for row_id in plan.row_ids if row_id <= result_watermark}
    if not expected.issubset(returned):
        return None

    grouped: dict[int, list[tuple[int, float, float]]] = {}
    for row_id, row in returned.items():
        # The shared extraction can also contain another layout's samples or
        # 1D windows. Orientation belongs to this plan's Cartesian sample;
        # extra rows must not change its per-row coordinate-vector coverage.
        if row_id not in expected:
            continue
        grid_index = _pattern_grid_index(
            pattern,
            row_id,
            slow_count=slow_count,
            fast_count=fast_count,
        )
        if grid_index is None:
            continue
        if len(row) != len(sample_columns):
            return None
        slow_value = row[indexes[slow_axis]]
        fast_value = row[indexes[fast_axis]]
        if (
            isinstance(slow_value, bool)
            or not isinstance(slow_value, (int, float))
            or isinstance(fast_value, bool)
            or not isinstance(fast_value, (int, float))
        ):
            return None
        finite_slow = float(slow_value)
        finite_fast = float(fast_value)
        if not math.isfinite(finite_slow) or not math.isfinite(finite_fast):
            return None
        slow_index, fast_index = grid_index
        grouped.setdefault(slow_index, []).append(
            (fast_index, finite_slow, finite_fast)
        )
    if not grouped:
        return None

    slow_values: list[tuple[int, float]] = []
    fast_directions: list[tuple[int, bool]] = []
    reference_fast_values: dict[int, float] | None = None
    for slow_index, values in sorted(grouped.items()):
        reference_slow = values[0][1]
        if any(
            not math.isclose(value[1], reference_slow, rel_tol=1e-12, abs_tol=0.0)
            for value in values[1:]
        ):
            return None
        slow_values.append((slow_index, reference_slow))
        ordered_fast = tuple(
            sorted((fast_index, fast_value) for fast_index, _slow, fast_value in values)
        )
        direction = _strict_axis_reversed(list(ordered_fast))
        if direction is None:
            return None
        # One value cannot prove which end of a non-singleton fast axis it
        # represents.  In a live serpentine sweep, accepting a newly started
        # row here would place its first point on the wrong horizontal edge.
        if fast_count > 1 and len(values) < 2:
            return None
        row_fast_values = dict(ordered_fast)
        if reference_fast_values is None:
            reference_fast_values = row_fast_values
        else:
            if completed and row_fast_values.keys() != reference_fast_values.keys():
                return None

            # A rectangular row has one canonical fast-coordinate vector.
            # Serpentine acquisition may store that vector in reverse, but a
            # slow-dependent shift or warp is not a Cartesian heatmap and must
            # remain an honest placeholder.
            if not (
                _sampled_fast_values_match(
                    reference_fast_values,
                    row_fast_values,
                    fast_count,
                    reversed_axis=False,
                )
                or _sampled_fast_values_match(
                    reference_fast_values,
                    row_fast_values,
                    fast_count,
                    reversed_axis=True,
                )
            ):
                return None
        if fast_count > 1:
            fast_directions.append((slow_index, direction))

    # Likewise, one represented row cannot prove whether a non-singleton slow
    # axis ascends or descends.  Keep an honest placeholder until the captured
    # prefix contains direction evidence for both scientific axes.
    if slow_count > 1 and len(slow_values) < 2:
        return None
    slow_reversed = _strict_axis_reversed(slow_values)
    if slow_reversed is None:
        return None
    if (
        slow_count > 1
        and fast_count > 1
        and {slow_index % 2 for slow_index, _reversed_axis in fast_directions} != {0, 1}
    ):
        # Equal directions on two even (or two odd) sampled rows cannot
        # distinguish row-major acquisition from serpentine acquisition.  Both
        # parities are required before publishing orientation for future rows.
        return None
    fast_reversed = fast_directions[0][1] if fast_directions else False
    serpentine = False
    if len(fast_directions) >= 2:
        row_major = all(
            reversed_axis == fast_reversed
            for _slow_index, reversed_axis in fast_directions
        )
        serpentine = all(
            reversed_axis == (fast_reversed ^ bool(slow_index % 2))
            for slow_index, reversed_axis in fast_directions
        )
        if not row_major and not serpentine:
            return None
        if row_major:
            serpentine = False
        elif fast_directions[0][0] % 2:
            fast_reversed = not fast_reversed

    last_row_id = (
        pattern.first_row_id
        + (slow_count - 1) * pattern.slow_id_stride
        + (fast_count - 1) * pattern.fast_id_stride
    )
    if completed and last_row_id > result_watermark:
        return None
    return Trusted2DGridLayout(
        dependent=pattern.dependent,
        dependencies=proof.dependencies,
        shape=proof.shape,
        fast_axis_index=proof.fast_axis_index,
        first_row_id=pattern.first_row_id,
        fast_id_stride=pattern.fast_id_stride,
        slow_id_stride=pattern.slow_id_stride,
        sample_slow_indexes=plan.slow_indexes,
        sample_fast_indexes=plan.fast_indexes,
        slow_reversed=slow_reversed,
        fast_reversed=fast_reversed,
        serpentine=serpentine,
        complete=bool(completed and last_row_id <= result_watermark),
        source=proof.source,
    )
