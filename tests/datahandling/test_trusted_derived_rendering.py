from __future__ import annotations

import struct
import threading
import zlib
from dataclasses import replace

import pytest

from qplot.datahandling.file_identity import DatabaseInstance
from qplot.datahandling.trusted_derived_rendering import (
    TRUSTED_DERIVED_MAX_IMAGE_WIDTH,
    TRUSTED_DERIVED_RENDERER_VERSION,
    _nearest_sample_bins,
    _viridis_rgba,
    render_trusted_derived_payload,
    trusted_derived_payload_size,
)
from qplot.datahandling.trusted_live_queries import (
    Trusted2DDependentRowPattern,
    Trusted2DGridLayout,
    Trusted2DLayoutProof,
    TrustedDerivedSourceObservation,
    TrustedParameterView,
    _planned_dense_2d_layout_proof,
    _representative_grid_sample,
    _trusted_2d_sample_plans,
    _validated_sampled_grid_layout,
)
from qplot.datahandling.trusted_work_scheduler import (
    RenderingOptions,
    TrustedWorkKind,
)


def _observation(*, dimensions: int = 1, unsupported: str | None = None):
    instance = DatabaseInstance("/data/test.db", "/data/test.db", (7, 11))
    if dimensions == 1:
        columns = ("id", "x", "signal")
        rows = tuple(
            (index, float(index), float(index * index)) for index in range(1, 9)
        )
        parameters = (
            TrustedParameterView("x", "X", "V", (), "numeric"),
            TrustedParameterView("signal", "Signal", "A", ("x",), "numeric"),
        )
    else:
        columns = ("id", "x", "y", "signal")
        rows = tuple(
            (index + 1, float(index % 4), float(index // 4), float(index))
            for index in range(16)
        )
        parameters = (
            TrustedParameterView("x", "X", "V", (), "numeric"),
            TrustedParameterView("y", "Y", "V", (), "numeric"),
            TrustedParameterView("signal", "Signal", "A", ("x", "y"), "numeric"),
        )
    return TrustedDerivedSourceObservation(
        1,
        instance,
        1,
        "guid-1",
        b"service",
        2,
        3,
        "results-1-1",
        columns,
        b"schema-digest",
        len(rows),
        parameters,
        ("signal",),
        (4, 4) if dimensions == 2 else (8,),
        columns,
        rows,
        unsupported,
        validated_2d_layouts=(
            (
                Trusted2DGridLayout(
                    dependent="signal",
                    dependencies=("x", "y"),
                    shape=(4, 4),
                    fast_axis_index=0,
                    first_row_id=1,
                    fast_id_stride=1,
                    slow_id_stride=4,
                    sample_slow_indexes=(0, 1, 2, 3),
                    sample_fast_indexes=(0, 1, 2, 3),
                    slow_reversed=False,
                    fast_reversed=False,
                    serpentine=False,
                    complete=True,
                    source="planned",
                ),
            )
            if dimensions == 2
            else ()
        ),
    )


def _decode_png_rgba(encoded: bytes) -> tuple[int, int, bytes]:
    assert encoded.startswith(b"\x89PNG\r\n\x1a\n")
    position = 8
    compressed = bytearray()
    width = height = 0
    while position < len(encoded):
        length = struct.unpack(">I", encoded[position : position + 4])[0]
        kind = encoded[position + 4 : position + 8]
        data = encoded[position + 8 : position + 8 + length]
        position += 12 + length
        if kind == b"IHDR":
            width, height = struct.unpack(">II", data[:8])
        elif kind == b"IDAT":
            compressed.extend(data)
        elif kind == b"IEND":
            break
    rows = zlib.decompress(bytes(compressed))
    stride = width * 4 + 1
    assert len(rows) == height * stride
    assert all(rows[offset] == 0 for offset in range(0, len(rows), stride))
    rgba = b"".join(
        rows[offset + 1 : offset + stride] for offset in range(0, len(rows), stride)
    )
    return width, height, rgba


def _rgba_at(rgba: bytes, width: int, x: int, y: int) -> tuple[int, int, int, int]:
    offset = (y * width + x) * 4
    return tuple(rgba[offset : offset + 4])  # type: ignore[return-value]


def _large_grid_observation(*, missing: tuple[int, int] | None = None):
    slow_count, fast_count = 301, 451
    ids, sample_slow, sample_fast = _representative_grid_sample(
        slow_count,
        fast_count,
    )
    rows = []
    for row_id in ids:
        offset = row_id - 1
        slow_index, storage_fast_index = divmod(offset, fast_count)
        # Exercise the ordinary QCoDeS serpentine layout.  The axis value,
        # rather than acquisition direction, owns horizontal orientation.
        fast_index = (
            fast_count - 1 - storage_fast_index
            if slow_index % 2
            else storage_fast_index
        )
        value = (
            None
            if missing == (slow_index, fast_index)
            else (
                0.25 * slow_index / (slow_count - 1)
                + 0.75 * fast_index / (fast_count - 1)
            )
        )
        rows.append((row_id, float(slow_index), float(fast_index), value))
    instance = DatabaseInstance("/data/grid.db", "/data/grid.db", (7, 12))
    return TrustedDerivedSourceObservation(
        1,
        instance,
        1,
        "grid-guid",
        b"service",
        2,
        3,
        "results-grid",
        ("id", "slow", "fast", "signal"),
        b"grid-schema",
        slow_count * fast_count,
        (
            TrustedParameterView("slow", "Slow", "V", (), "numeric"),
            TrustedParameterView("fast", "Fast", "V", (), "numeric"),
            TrustedParameterView("signal", "Signal", "A", ("slow", "fast"), "numeric"),
        ),
        ("signal",),
        (slow_count, fast_count),
        ("id", "slow", "fast", "signal"),
        tuple(rows),
        validated_2d_layouts=(
            Trusted2DGridLayout(
                dependent="signal",
                dependencies=("slow", "fast"),
                shape=(slow_count, fast_count),
                fast_axis_index=1,
                first_row_id=1,
                fast_id_stride=1,
                slow_id_stride=fast_count,
                sample_slow_indexes=sample_slow,
                sample_fast_indexes=sample_fast,
                slow_reversed=False,
                fast_reversed=False,
                serpentine=True,
                complete=True,
                source="observed",
            ),
        ),
    )


def _oriented_grid_observation(
    *,
    slow_count: int,
    fast_count: int,
    slow_reversed: bool = False,
    fast_reversed: bool = False,
    serpentine: bool = False,
) -> TrustedDerivedSourceObservation:
    ids, sample_slow, sample_fast = _representative_grid_sample(
        slow_count,
        fast_count,
    )
    rows = []
    for row_id in ids:
        storage_slow, storage_fast = divmod(row_id - 1, fast_count)
        scientific_slow = (
            slow_count - 1 - storage_slow if slow_reversed else storage_slow
        )
        row_fast_reversed = fast_reversed ^ (serpentine and bool(storage_slow % 2))
        scientific_fast = (
            fast_count - 1 - storage_fast if row_fast_reversed else storage_fast
        )
        value = 0.25 * scientific_slow / (slow_count - 1) + 0.75 * scientific_fast / (
            fast_count - 1
        )
        rows.append(
            (
                row_id,
                float(scientific_slow),
                float(scientific_fast),
                value,
            )
        )
    return TrustedDerivedSourceObservation(
        1,
        DatabaseInstance("/data/oriented.db", "/data/oriented.db", (7, 14)),
        1,
        "oriented-guid",
        b"service",
        2,
        3,
        "results-oriented",
        ("id", "slow", "fast", "signal"),
        b"oriented-schema",
        slow_count * fast_count,
        (
            TrustedParameterView("slow", "Slow", "V", (), "numeric"),
            TrustedParameterView("fast", "Fast", "V", (), "numeric"),
            TrustedParameterView("signal", "Signal", "A", ("slow", "fast"), "numeric"),
        ),
        ("signal",),
        (slow_count, fast_count),
        ("id", "slow", "fast", "signal"),
        tuple(rows),
        validated_2d_layouts=(
            Trusted2DGridLayout(
                dependent="signal",
                dependencies=("slow", "fast"),
                shape=(slow_count, fast_count),
                fast_axis_index=1,
                first_row_id=1,
                fast_id_stride=1,
                slow_id_stride=fast_count,
                sample_slow_indexes=sample_slow,
                sample_fast_indexes=sample_fast,
                slow_reversed=slow_reversed,
                fast_reversed=fast_reversed,
                serpentine=serpentine,
                complete=True,
                source="observed",
            ),
        ),
    )


def _fast_first_blocked_observation() -> TrustedDerivedSourceObservation:
    fast_count, slow_count = 5, 3
    rows = []
    for slow_index in range(slow_count):
        block_start = slow_index * fast_count * 2
        for dependent_index, dependent in enumerate(("signal_a", "signal_b")):
            for fast_index in range(fast_count):
                row_id = block_start + dependent_index * fast_count + fast_index + 1
                value = 0.75 * fast_index / (fast_count - 1) + 0.25 * slow_index / (
                    slow_count - 1
                )
                rows.append(
                    (
                        row_id,
                        float(fast_index),
                        float(slow_index),
                        value if dependent == "signal_a" else None,
                        value if dependent == "signal_b" else None,
                    )
                )
    layouts = tuple(
        Trusted2DGridLayout(
            dependent=dependent,
            dependencies=("fast", "slow"),
            shape=(fast_count, slow_count),
            fast_axis_index=0,
            first_row_id=1 + dependent_index * fast_count,
            fast_id_stride=1,
            slow_id_stride=fast_count * 2,
            sample_slow_indexes=tuple(range(slow_count)),
            sample_fast_indexes=tuple(range(fast_count)),
            slow_reversed=False,
            fast_reversed=False,
            serpentine=False,
            complete=True,
            source="observed",
        )
        for dependent_index, dependent in enumerate(("signal_a", "signal_b"))
    )
    return TrustedDerivedSourceObservation(
        1,
        DatabaseInstance("/data/blocked.db", "/data/blocked.db", (8, 13)),
        2,
        "blocked-guid",
        b"service",
        2,
        3,
        "results-blocked",
        ("id", "fast", "slow", "signal_a", "signal_b"),
        b"blocked-schema",
        len(rows),
        (
            TrustedParameterView("fast", "Fast", "Hz", (), "numeric"),
            TrustedParameterView("slow", "Slow", "V", (), "numeric"),
            TrustedParameterView(
                "signal_a", "Signal A", "A", ("fast", "slow"), "numeric"
            ),
            TrustedParameterView(
                "signal_b", "Signal B", "A", ("fast", "slow"), "numeric"
            ),
        ),
        ("signal_a", "signal_b"),
        (fast_count, slow_count),
        ("id", "fast", "slow", "signal_a", "signal_b"),
        tuple(sorted(rows)),
        validated_2d_layouts=layouts,
    )


@pytest.mark.parametrize("kind", [TrustedWorkKind.THUMBNAIL, TrustedWorkKind.PREVIEW])
def test_1d_rendering_is_deterministic_bounded_png(kind: TrustedWorkKind) -> None:
    observation = _observation()

    first = render_trusted_derived_payload(observation, kind)
    second = render_trusted_derived_payload(observation, kind)

    assert first == second
    assert first["status"] == "ok"
    images = first["images"]
    assert isinstance(images, tuple) and len(images) == 1
    image = dict(images[0])
    assert image["bytes"].startswith(b"\x89PNG\r\n\x1a\n")
    assert image["dimensions"] == 1
    assert trusted_derived_payload_size(first) < 16 * 1024 * 1024


def test_single_finite_1d_point_is_visibly_rendered() -> None:
    observation = _observation()
    observation = replace(
        observation,
        result_watermark=1,
        planned_shape=(1,),
        sample_rows=observation.sample_rows[:1],
    )

    payload = render_trusted_derived_payload(
        observation,
        TrustedWorkKind.PREVIEW,
        RenderingOptions.from_mapping({"width": 40, "height": 30}),
    )
    image = dict(payload["images"][0])
    width, height, rgba = _decode_png_rgba(image["bytes"])

    assert image["sampled_points"] == 1
    assert len(rgba) == width * height * 4
    assert rgba.count(bytes((24, 92, 170, 255))) == 1
    assert rgba.count(b"\xff\xff\xff\xff") == width * height - 1


def test_dependency_aware_renderer_invalidates_v3_cache_entries() -> None:
    renderer_prefix = "trusted-derived-renderer-v"
    assert TRUSTED_DERIVED_RENDERER_VERSION.startswith(renderer_prefix)
    assert int(TRUSTED_DERIVED_RENDERER_VERSION.removeprefix(renderer_prefix)) > 3


def test_2d_rendering_uses_two_dependencies() -> None:
    payload = render_trusted_derived_payload(
        _observation(dimensions=2), TrustedWorkKind.PREVIEW
    )

    image = dict(payload["images"][0])
    assert image["dimensions"] == 2
    assert image["sampled_points"] == 16


def test_large_asymmetric_grid_sampling_covers_both_complete_extents() -> None:
    ids, slow_indexes, fast_indexes = _representative_grid_sample(301, 451)

    assert len(ids) <= 4_095
    assert len(ids) == len(slow_indexes) * len(fast_indexes)
    assert slow_indexes[0] == 0 and slow_indexes[-1] == 300
    assert fast_indexes[0] == 0 and fast_indexes[-1] == 450
    assert len(slow_indexes) > 40
    assert len(fast_indexes) > 60
    assert slow_indexes == tuple(300 - index for index in reversed(slow_indexes))
    assert fast_indexes == tuple(450 - index for index in reversed(fast_indexes))
    assert {divmod(row_id - 1, 451)[0] for row_id in ids} == set(slow_indexes)
    assert {divmod(row_id - 1, 451)[1] for row_id in ids} == set(fast_indexes)
    # The old 15 contiguous windows represented only a few slow rows.  A
    # representative sample instead covers dozens of rows and every quadrant.
    quadrants = {
        (slow_index >= 150, fast_index >= 225)
        for row_id in ids
        for slow_index, fast_index in (divmod(row_id - 1, 451),)
    }
    assert quadrants == {(False, False), (False, True), (True, False), (True, True)}


def test_extreme_asymmetric_grid_sampling_preserves_both_axis_extents() -> None:
    ids, slow_indexes, fast_indexes = _representative_grid_sample(16, 7_900_079)

    assert len(ids) == 16 * 255 <= 4_095
    assert len(ids) == len(slow_indexes) * len(fast_indexes)
    assert slow_indexes == tuple(range(16))
    assert len(fast_indexes) == 255
    assert (fast_indexes[0], fast_indexes[-1]) == (0, 7_900_078)
    assert fast_indexes == tuple(7_900_078 - index for index in reversed(fast_indexes))
    assert ids[0] == 1
    assert ids[-1] == 16 * 7_900_079


def test_extreme_odd_slow_axis_samples_both_serpentine_parities_symmetrically() -> None:
    ids, slow_indexes, fast_indexes = _representative_grid_sample(301, 7_900_079)

    assert len(ids) <= 4_095
    assert slow_indexes == (0, 1, 299, 300)
    assert slow_indexes == tuple(300 - index for index in reversed(slow_indexes))
    assert fast_indexes == tuple(7_900_078 - index for index in reversed(fast_indexes))
    assert {index % 2 for index in slow_indexes} == {0, 1}


def test_grid_sampling_supports_fast_first_dependent_block_row_ids() -> None:
    first_dependent, slow_indexes, fast_indexes = _representative_grid_sample(
        16,
        251,
        first_row_id=1,
        fast_id_stride=1,
        slow_id_stride=502,
    )
    second_dependent, second_slow, second_fast = _representative_grid_sample(
        16,
        251,
        first_row_id=252,
        fast_id_stride=1,
        slow_id_stride=502,
    )

    assert slow_indexes == second_slow == tuple(range(16))
    assert fast_indexes == second_fast == tuple(range(251))
    assert first_dependent[0] == 1 and first_dependent[-1] == 7_781
    assert second_dependent[0] == 252 and second_dependent[-1] == 8_032
    assert set(first_dependent).isdisjoint(second_dependent)


def test_grid_budget_counts_distinct_physical_patterns() -> None:
    shared_proof = Trusted2DLayoutProof(
        dependencies=("slow", "fast"),
        shape=(301, 451),
        fast_axis_index=1,
        dependent_rows=(
            Trusted2DDependentRowPattern("signal_a", 1, 1, 451),
            Trusted2DDependentRowPattern("signal_b", 1, 1, 451),
        ),
        source="observed",
    )
    shared_plans = _trusted_2d_sample_plans(shared_proof)

    assert len(shared_plans[0].row_ids) == 4_056
    assert shared_plans[0].row_ids == shared_plans[1].row_ids
    assert len({row_id for plan in shared_plans for row_id in plan.row_ids}) == 4_056

    blocked_proof = Trusted2DLayoutProof(
        dependencies=("fast", "slow"),
        shape=(251, 16),
        fast_axis_index=0,
        dependent_rows=(
            Trusted2DDependentRowPattern("signal_a", 1, 1, 502),
            Trusted2DDependentRowPattern("signal_b", 252, 1, 502),
        ),
        source="observed",
    )
    blocked_plans = _trusted_2d_sample_plans(blocked_proof)
    blocked_ids = {row_id for plan in blocked_plans for row_id in plan.row_ids}

    assert tuple(len(plan.row_ids) for plan in blocked_plans) == (2_032, 2_032)
    assert all(plan.slow_indexes == tuple(range(16)) for plan in blocked_plans)
    assert len(blocked_ids) == 4_064 <= 4_095
    assert set(blocked_plans[0].row_ids).isdisjoint(blocked_plans[1].row_ids)


def test_sampled_layout_rejects_a_grid_over_the_cell_budget() -> None:
    with pytest.raises(ValueError, match="invalid grid indexes"):
        Trusted2DGridLayout(
            dependent="signal",
            dependencies=("slow", "fast"),
            shape=(64, 64),
            fast_axis_index=1,
            first_row_id=1,
            fast_id_stride=1,
            slow_id_stride=64,
            sample_slow_indexes=tuple(range(64)),
            sample_fast_indexes=tuple(range(64)),
            slow_reversed=False,
            fast_reversed=False,
            serpentine=False,
            complete=True,
            source="planned",
        )


def test_partial_planned_multi_dependent_layout_requires_a_structural_proof() -> None:
    proof = _planned_dense_2d_layout_proof(
        {
            "setpoint_shape": (4, 4),
            "setpoint_shape_source": "planned",
            "result_count": 6,
            "is_completed": False,
        },
        (
            (("slow", "fast"), "signal_a"),
            (("slow", "fast"), "signal_b"),
        ),
    )

    assert proof is None


def test_incomplete_descending_grid_waits_for_both_axis_directions() -> None:
    proof = Trusted2DLayoutProof(
        dependencies=("slow", "fast"),
        shape=(4, 4),
        fast_axis_index=1,
        dependent_rows=(Trusted2DDependentRowPattern("signal", 1, 1, 4),),
        source="planned",
    )
    plan = _trusted_2d_sample_plans(proof)[0]

    def validate(through_row_id: int) -> Trusted2DGridLayout | None:
        rows = tuple(
            (
                row_id,
                float(3 - (row_id - 1) // 4),
                float(3 - (row_id - 1) % 4),
                float(row_id),
            )
            for row_id in range(1, through_row_id + 1)
        )
        return _validated_sampled_grid_layout(
            plan,
            sample_columns=("id", "slow", "fast", "signal"),
            sample_rows=rows,
            result_watermark=through_row_id,
            completed=False,
        )

    # A point, one complete fast row, and the first point of the next row all
    # leave at least one scientific direction or its serpentine parity unknown.
    assert validate(1) is None
    assert validate(4) is None
    assert validate(5) is None

    accepted = validate(6)
    assert accepted is not None
    assert accepted.slow_reversed
    assert accepted.fast_reversed
    assert not accepted.serpentine


def test_sampled_layout_rejects_row_varying_fast_coordinate_vectors() -> None:
    proof = Trusted2DLayoutProof(
        dependencies=("slow", "fast"),
        shape=(5, 7),
        fast_axis_index=1,
        dependent_rows=(Trusted2DDependentRowPattern("signal", 1, 1, 7),),
        source="observed",
    )
    plan = _trusted_2d_sample_plans(proof)[0]
    rows = tuple(
        (
            row_id,
            float(slow_index),
            # A nested/adaptive sweep can remain monotonic in every row while
            # shifting its fast setpoints with the slow coordinate.  It is not
            # one Cartesian heatmap and must fail closed.
            float(fast_index) + 0.125 * slow_index,
            float(slow_index + fast_index),
        )
        for row_id in plan.row_ids
        for slow_index, fast_index in (divmod(row_id - 1, 7),)
    )

    assert (
        _validated_sampled_grid_layout(
            plan,
            sample_columns=("id", "slow", "fast", "signal"),
            sample_rows=rows,
            result_watermark=35,
            completed=True,
        )
        is None
    )


def test_2d_rendering_is_dense_viridis_oriented_and_marks_missing_cells() -> None:
    _ids, slow_indexes, fast_indexes = _representative_grid_sample(301, 451)
    missing_source = (slow_indexes[len(slow_indexes) // 2], fast_indexes[10])
    observation = _large_grid_observation(missing=missing_source)

    preview = render_trusted_derived_payload(
        observation,
        TrustedWorkKind.PREVIEW,
        RenderingOptions.from_mapping({"width": 780, "height": 520}),
    )
    image = dict(preview["images"][0])
    width, height, rgba = _decode_png_rgba(image["bytes"])

    assert image["dimensions"] == 2
    assert image["sampled_points"] == len(observation.sample_rows) - 1
    assert _rgba_at(rgba, width, 0, height - 1) == (68, 1, 84, 255)
    assert _rgba_at(rgba, width, width - 1, 0) == (253, 231, 36, 255)
    assert _viridis_rgba(0.5, 0.0, 1.0) == (32, 144, 140, 255)
    # The deliberately asymmetric surface gives horizontal position three
    # times the weight of vertical position, proving slow is vertical and fast
    # is horizontal rather than transposed.
    top_left = _rgba_at(rgba, width, 0, 0)
    bottom_right = _rgba_at(rgba, width, width - 1, height - 1)
    assert top_left != bottom_right
    assert sum(bottom_right[:3]) > sum(top_left[:3])

    missing_slow_bin = slow_indexes.index(missing_source[0])
    missing_fast_bin = fast_indexes.index(missing_source[1])
    missing_x = (2 * missing_fast_bin + 1) * width // (2 * len(fast_indexes))
    # Increasing slow values render toward the top of the image.
    display_slow_bin = len(slow_indexes) - 1 - missing_slow_bin
    missing_y = (2 * display_slow_bin + 1) * height // (2 * len(slow_indexes))
    assert _rgba_at(rgba, width, missing_x, missing_y) == (230, 230, 230, 255)
    assert rgba.count(b"\xff\xff\xff\xff") == 0


@pytest.mark.parametrize(
    ("slow_reversed", "fast_reversed"),
    ((True, False), (False, True), (True, True)),
)
def test_reversed_axes_keep_slow_vertical_and_fast_horizontal(
    slow_reversed: bool,
    fast_reversed: bool,
) -> None:
    observation = _oriented_grid_observation(
        slow_count=5,
        fast_count=7,
        slow_reversed=slow_reversed,
        fast_reversed=fast_reversed,
    )

    payload = render_trusted_derived_payload(
        observation,
        TrustedWorkKind.PREVIEW,
        RenderingOptions.from_mapping({"width": 70, "height": 50}),
    )
    image = dict(payload["images"][0])
    width, height, rgba = _decode_png_rgba(image["bytes"])

    assert _rgba_at(rgba, width, 0, height - 1) == _viridis_rgba(0.0, 0.0, 1.0)
    assert _rgba_at(rgba, width, width - 1, 0) == _viridis_rgba(1.0, 0.0, 1.0)


def test_extreme_serpentine_rows_share_canonical_horizontal_slices() -> None:
    observation = _oriented_grid_observation(
        slow_count=301,
        fast_count=7_900_079,
        serpentine=True,
    )
    proof = Trusted2DLayoutProof(
        dependencies=("slow", "fast"),
        shape=(301, 7_900_079),
        fast_axis_index=1,
        dependent_rows=(Trusted2DDependentRowPattern("signal", 1, 1, 7_900_079),),
        source="observed",
    )
    plan = _trusted_2d_sample_plans(proof)[0]
    validated = _validated_sampled_grid_layout(
        plan,
        sample_columns=observation.sample_columns,
        sample_rows=observation.sample_rows,
        result_watermark=observation.result_watermark,
        completed=True,
    )

    assert validated is not None
    assert validated.sample_slow_indexes == (0, 1, 299, 300)
    assert validated.serpentine
    observation = replace(observation, validated_2d_layouts=(validated,))
    payload = render_trusted_derived_payload(
        observation,
        TrustedWorkKind.PREVIEW,
        RenderingOptions.from_mapping({"width": 1_024, "height": 40}),
    )
    image = dict(payload["images"][0])
    width, height, rgba = _decode_png_rgba(image["bytes"])
    slow_indexes = validated.sample_slow_indexes
    fast_indexes = validated.sample_fast_indexes
    rendered_slow_bins = _nearest_sample_bins(height, 301, slow_indexes)
    rendered_fast_bins = _nearest_sample_bins(width, 7_900_079, fast_indexes)

    for scientific_slow in (0, 1):
        slow_bin = slow_indexes.index(scientific_slow)
        display_slow_bin = len(slow_indexes) - 1 - slow_bin
        y = rendered_slow_bins.index(display_slow_bin)
        for fast_bin in (2, len(fast_indexes) // 2, len(fast_indexes) - 3):
            scientific_fast = fast_indexes[fast_bin]
            x = rendered_fast_bins.index(fast_bin)
            expected_value = (
                0.25 * scientific_slow / 300 + 0.75 * scientific_fast / 7_900_078
            )
            assert _rgba_at(rgba, width, x, y) == _viridis_rgba(
                expected_value,
                0.0,
                1.0,
            )


def test_extreme_edge_anchors_use_source_domain_not_equal_rank_widths() -> None:
    observation = _oriented_grid_observation(
        slow_count=16,
        fast_count=7_900_079,
    )
    layout = observation.validated_2d_layouts[0]
    rank = {
        source_index: index
        for index, source_index in enumerate(layout.sample_fast_indexes)
    }
    last_rank = len(rank) - 1
    rows = tuple(
        (*row[:-1], rank[int(row[2])] / last_rank) for row in observation.sample_rows
    )
    observation = replace(observation, sample_rows=rows)

    payload = render_trusted_derived_payload(
        observation,
        TrustedWorkKind.PREVIEW,
        RenderingOptions.from_mapping({"width": 1_024, "height": 32}),
    )
    image = dict(payload["images"][0])
    width, _height, rgba = _decode_png_rgba(image["bytes"])

    # The exact endpoint owns the endpoint pixel only.  Equal-rank bands would
    # incorrectly paint roughly four pixels with this single-cell anchor.
    assert _rgba_at(rgba, width, 0, 0) == _viridis_rgba(0.0, 0.0, 1.0)
    assert _rgba_at(rgba, width, 1, 0) != _rgba_at(rgba, width, 0, 0)


def test_thumbnail_and_preview_share_the_same_grid_structure() -> None:
    observation = _large_grid_observation()
    thumbnail = render_trusted_derived_payload(observation, TrustedWorkKind.THUMBNAIL)
    preview = render_trusted_derived_payload(observation, TrustedWorkKind.PREVIEW)
    thumbnail_image = dict(thumbnail["images"][0])
    preview_image = dict(preview["images"][0])
    thumb_width, thumb_height, thumb_rgba = _decode_png_rgba(thumbnail_image["bytes"])
    full_width, full_height, full_rgba = _decode_png_rgba(preview_image["bytes"])

    for x_fraction, y_fraction in (
        (0.05, 0.05),
        (0.25, 0.75),
        (0.55, 0.35),
        (0.95, 0.95),
    ):
        thumb_color = _rgba_at(
            thumb_rgba,
            thumb_width,
            min(thumb_width - 1, int(x_fraction * thumb_width)),
            min(thumb_height - 1, int(y_fraction * thumb_height)),
        )
        full_color = _rgba_at(
            full_rgba,
            full_width,
            min(full_width - 1, int(x_fraction * full_width)),
            min(full_height - 1, int(y_fraction * full_height)),
        )
        assert (
            max(abs(a - b) for a, b in zip(thumb_color, full_color, strict=True)) <= 8
        )


def test_fast_first_dependent_blocks_render_in_declared_dependency_order() -> None:
    payload = render_trusted_derived_payload(
        _fast_first_blocked_observation(),
        TrustedWorkKind.PREVIEW,
        RenderingOptions.from_mapping({"width": 100, "height": 60}),
    )

    images = tuple(dict(image) for image in payload["images"])
    assert tuple(image["dependent"] for image in images) == ("signal_a", "signal_b")
    assert tuple(image["sampled_points"] for image in images) == (15, 15)
    for image in images:
        width, height, rgba = _decode_png_rgba(image["bytes"])
        assert _rgba_at(rgba, width, 0, height - 1) == (68, 1, 84, 255)
        assert _rgba_at(rgba, width, width - 1, 0) == (253, 231, 36, 255)


def test_declared_dependency_zero_is_vertical_even_when_physically_fast() -> None:
    payload = render_trusted_derived_payload(
        _fast_first_blocked_observation(),
        TrustedWorkKind.PREVIEW,
        RenderingOptions.from_mapping({"width": 60, "height": 100}),
    )

    image = dict(payload["images"][0])
    width, height, rgba = _decode_png_rgba(image["bytes"])

    # The asymmetric surface is 0.75 * dependency_0 + 0.25 * dependency_1.
    # Under the established declared-dependency convention, dependency 0 is
    # vertical and dependency 1 horizontal even though dependency 0 changes
    # fastest in physical row order.  A physical-fast-axis rendering transposes
    # these two diagnostic corners.
    assert _rgba_at(rgba, width, 0, 0) == _viridis_rgba(0.75, 0.0, 1.0)
    assert _rgba_at(rgba, width, width - 1, height - 1) == _viridis_rgba(
        0.25,
        0.0,
        1.0,
    )


def test_shared_physical_rows_keep_each_dependents_missing_cells_independent() -> None:
    rows = tuple(
        (
            row_id,
            float((row_id - 1) // 3),
            float((row_id - 1) % 3),
            None if row_id == 2 else float(row_id),
            float(10 + row_id),
        )
        for row_id in range(1, 7)
    )
    layouts = tuple(
        Trusted2DGridLayout(
            dependent=dependent,
            dependencies=("slow", "fast"),
            shape=(2, 3),
            fast_axis_index=1,
            first_row_id=1,
            fast_id_stride=1,
            slow_id_stride=3,
            sample_slow_indexes=(0, 1),
            sample_fast_indexes=(0, 1, 2),
            slow_reversed=False,
            fast_reversed=False,
            serpentine=False,
            complete=True,
            source="observed",
        )
        for dependent in ("signal_a", "signal_b")
    )
    observation = TrustedDerivedSourceObservation(
        1,
        DatabaseInstance("/data/shared.db", "/data/shared.db", (8, 15)),
        3,
        "shared-guid",
        b"service",
        2,
        3,
        "results-shared",
        ("id", "slow", "fast", "signal_a", "signal_b"),
        b"shared-schema",
        6,
        (
            TrustedParameterView("slow", "Slow", "V", (), "numeric"),
            TrustedParameterView("fast", "Fast", "Hz", (), "numeric"),
            TrustedParameterView(
                "signal_a", "Signal A", "A", ("slow", "fast"), "numeric"
            ),
            TrustedParameterView(
                "signal_b", "Signal B", "A", ("slow", "fast"), "numeric"
            ),
        ),
        ("signal_a", "signal_b"),
        (2, 3),
        ("id", "slow", "fast", "signal_a", "signal_b"),
        rows,
        validated_2d_layouts=layouts,
    )

    payload = render_trusted_derived_payload(
        observation,
        TrustedWorkKind.PREVIEW,
        RenderingOptions.from_mapping({"width": 60, "height": 40}),
    )
    images = {dict(item)["dependent"]: dict(item) for item in payload["images"]}
    decoded = {
        dependent: _decode_png_rgba(image["bytes"])
        for dependent, image in images.items()
    }
    x = _nearest_sample_bins(60, 3, (0, 1, 2)).index(1)
    y = _nearest_sample_bins(40, 2, (0, 1)).index(1)

    width_a, _height_a, rgba_a = decoded["signal_a"]
    width_b, _height_b, rgba_b = decoded["signal_b"]
    assert _rgba_at(rgba_a, width_a, x, y) == (230, 230, 230, 255)
    assert _rgba_at(rgba_b, width_b, x, y) != (230, 230, 230, 255)


def test_valid_dependent_survives_another_dependent_without_a_layout() -> None:
    observation = _fast_first_blocked_observation()
    observation = replace(
        observation,
        validated_2d_layouts=observation.validated_2d_layouts[:1],
    )

    payload = render_trusted_derived_payload(observation, TrustedWorkKind.PREVIEW)

    assert payload["status"] == "ok"
    assert "some dependents" in str(payload["description"])
    assert tuple(dict(image)["dependent"] for image in payload["images"]) == (
        "signal_a",
    )


def test_incomplete_planned_grid_leaves_unmeasured_future_cells_neutral() -> None:
    complete = _observation(dimensions=2)
    partial_layout = replace(complete.validated_2d_layouts[0], complete=False)
    partial = replace(
        complete,
        result_watermark=6,
        sample_rows=complete.sample_rows[:6],
        validated_2d_layouts=(partial_layout,),
    )

    payload = render_trusted_derived_payload(
        partial,
        TrustedWorkKind.PREVIEW,
        RenderingOptions.from_mapping({"width": 40, "height": 40}),
    )
    image = dict(payload["images"][0])
    width, _height, rgba = _decode_png_rgba(image["bytes"])

    assert _rgba_at(rgba, width, 5, 35) != (230, 230, 230, 255)
    assert _rgba_at(rgba, width, 5, 25) != (230, 230, 230, 255)
    assert _rgba_at(rgba, width, 35, 25) == (230, 230, 230, 255)
    # Dependency 0 (the physically fast x axis) is vertical, so its complete
    # first physical row fills the left side from bottom to top.
    assert _rgba_at(rgba, width, 5, 5) != (230, 230, 230, 255)
    assert _rgba_at(rgba, width, 35, 5) == (230, 230, 230, 255)


def test_unvalidated_2d_layout_is_an_honest_placeholder() -> None:
    observation = replace(_observation(dimensions=2), validated_2d_layouts=())

    payload = render_trusted_derived_payload(observation, TrustedWorkKind.PREVIEW)

    assert payload["status"] == "unsupported"
    assert payload["images"] == ()
    assert "validated" in str(payload["description"]).lower()


def test_missing_dependency_cannot_be_projected_to_one_dimension() -> None:
    observation = _observation(dimensions=2)
    signal = observation.parameters[-1]
    missing = replace(
        observation,
        parameters=(
            *observation.parameters[:-1],
            replace(signal, depends_on=("x", "absent")),
        ),
        validated_2d_layouts=(),
    )

    payload = render_trusted_derived_payload(missing, TrustedWorkKind.PREVIEW)

    assert payload["status"] == "unsupported"
    assert payload["images"] == ()
    assert "sweep parameters is unavailable" in str(payload["description"])


def test_validated_2d_dependent_renders_beside_unsupported_3d() -> None:
    observation = _observation(dimensions=2)
    mixed = replace(
        observation,
        result_columns=(*observation.result_columns, "z", "volume"),
        sample_columns=(*observation.sample_columns, "z", "volume"),
        sample_rows=tuple(
            (*row, float(index), float(index))
            for index, row in enumerate(observation.sample_rows)
        ),
        parameters=(
            *observation.parameters,
            TrustedParameterView("z", "Z", "V", (), "numeric"),
            TrustedParameterView("volume", "Volume", "A", ("x", "y", "z"), "numeric"),
        ),
        dependent_parameters=("signal", "volume"),
    )

    payload = render_trusted_derived_payload(mixed, TrustedWorkKind.PREVIEW)

    assert payload["status"] == "ok"
    assert tuple(dict(image)["dependent"] for image in payload["images"]) == ("signal",)
    assert dict(payload["images"][0])["dimensions"] == 2
    assert "more than two sweep dimensions" in str(payload["description"])


def test_metadata_payload_carries_self_contained_run_fields() -> None:
    payload = render_trusted_derived_payload(_observation(), TrustedWorkKind.METADATA)

    metadata = dict(payload["metadata"])
    assert "run_fields" in metadata
    assert dict(metadata["run_fields"])["result_count"] == 8


def test_metadata_status_remains_ok_when_only_image_rendering_is_unsupported() -> None:
    payload = render_trusted_derived_payload(
        _observation(unsupported="three-dimensional result"),
        TrustedWorkKind.METADATA,
    )

    assert payload["status"] == "ok"
    assert payload["description"] == "Bounded trusted run metadata."


def test_empty_and_unsupported_results_are_descriptions_without_images() -> None:
    unsupported = render_trusted_derived_payload(
        _observation(unsupported="three-dimensional result"),
        TrustedWorkKind.PREVIEW,
    )
    empty_observation = _observation()
    empty_observation = TrustedDerivedSourceObservation(
        empty_observation.format_version,
        empty_observation.database_instance,
        empty_observation.run_id,
        empty_observation.run_guid,
        empty_observation.service_namespace,
        empty_observation.helper_incarnation,
        empty_observation.data_version,
        empty_observation.result_table_name,
        empty_observation.result_columns,
        empty_observation.result_schema_sha256,
        0,
        empty_observation.parameters,
        empty_observation.dependent_parameters,
        empty_observation.planned_shape,
        empty_observation.sample_columns,
        (),
    )
    empty = render_trusted_derived_payload(empty_observation, TrustedWorkKind.THUMBNAIL)

    assert unsupported["status"] == "unsupported"
    assert unsupported["images"] == ()
    assert empty["status"] == "empty"
    assert empty["images"] == ()


def test_rendering_bounds_and_cancellation_are_enforced() -> None:
    with pytest.raises(ValueError, match="width"):
        render_trusted_derived_payload(
            _observation(),
            TrustedWorkKind.PREVIEW,
            RenderingOptions.from_mapping(
                {"width": TRUSTED_DERIVED_MAX_IMAGE_WIDTH + 1}
            ),
        )

    cancelled = threading.Event()
    cancelled.set()

    def check() -> None:
        if cancelled.is_set():
            raise InterruptedError("cancelled at phase boundary")

    with pytest.raises(InterruptedError, match="phase boundary"):
        render_trusted_derived_payload(
            _observation(), TrustedWorkKind.PREVIEW, cancel_check=check
        )


@pytest.mark.parametrize("cancel_at", [2, 4, 8, 16])
def test_cancellation_interrupts_sampling_rendering_and_encoding(
    cancel_at: int,
) -> None:
    checks = 0

    def check() -> None:
        nonlocal checks
        checks += 1
        if checks == cancel_at:
            raise InterruptedError(f"phase {cancel_at}")

    with pytest.raises(InterruptedError, match=f"phase {cancel_at}"):
        render_trusted_derived_payload(
            _observation(), TrustedWorkKind.PREVIEW, cancel_check=check
        )


@pytest.mark.parametrize(
    "rows",
    [
        ((1, -1e308, 0.0), (2, 1e308, 1.0)),
        ((1, 0.0), (2, 1.0, 2.0)),
        ((1, float("nan"), 1.0), (2, float("inf"), 2.0)),
    ],
    ids=("finite-extremes", "mismatched-vectors", "nan-infinity"),
)
def test_numeric_pathologies_return_deterministic_unsupported_payload(
    rows: tuple[tuple[object, ...], ...],
) -> None:
    observation = replace(_observation(), sample_rows=rows)

    first = render_trusted_derived_payload(observation, TrustedWorkKind.PREVIEW)
    second = render_trusted_derived_payload(observation, TrustedWorkKind.PREVIEW)

    assert first == second
    assert first["status"] == "unsupported"
    assert first["images"] == ()
    assert isinstance(first["description"], str) and first["description"]
