from __future__ import annotations

import threading
from dataclasses import replace

import pytest

from qplot.datahandling.file_identity import DatabaseInstance
from qplot.datahandling.trusted_derived_rendering import (
    TRUSTED_DERIVED_MAX_IMAGE_WIDTH,
    render_trusted_derived_payload,
    trusted_derived_payload_size,
)
from qplot.datahandling.trusted_live_queries import (
    TrustedDerivedSourceObservation,
    TrustedParameterView,
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


def test_2d_rendering_uses_two_dependencies() -> None:
    payload = render_trusted_derived_payload(
        _observation(dimensions=2), TrustedWorkKind.PREVIEW
    )

    image = dict(payload["images"][0])
    assert image["dimensions"] == 2
    assert image["sampled_points"] == 16


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
