"""Real current-QCoDeS acceptance for the Stage 5C shape/render repair."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Mapping
from itertools import chain, zip_longest
from pathlib import Path

import numpy as np
import pytest
import qcodes
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.parameters import ManualParameter

from qplot.datahandling.file_identity import database_instance
from qplot.datahandling.trusted_derived_rendering import render_trusted_derived_payload
from qplot.datahandling.trusted_live import TrustedQuery, TrustedQueryResult
from qplot.datahandling.trusted_live_queries import (
    TRUSTED_DERIVED_MAX_SAMPLE_CELLS,
    TRUSTED_DERIVED_MAX_SAMPLE_ROWS,
    TrustedMetadataQueryAdapter,
    TrustedSourceRevisionNamespace,
    trusted_derived_source_revision,
)
from qplot.datahandling.trusted_live_service import TrustedLiveReadService
from qplot.datahandling.trusted_work_scheduler import RenderingOptions, TrustedWorkKind
from qplot.testdata import RunSpecification, generate_database
from qplot.windows._trusted_derived_qt import TrustedDerivedQtBridge
from qplot.windows._widgets.preview import PreviewImageLabel
from qplot.windows._widgets.run_list_items import RunPreviewCell

pytestmark = pytest.mark.timeout(120)


class _ReadOnlySqliteExecutor:
    """Small DB-API executor for an explicitly generated diagnostic source."""

    def __init__(self, path: Path) -> None:
        self.incarnation = 1
        self.queries: list[TrustedQuery] = []
        self._connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        self._connection.execute("PRAGMA query_only=ON")

    def close(self) -> None:
        self._connection.close()

    def query(
        self,
        sql: str,
        bindings=None,
        *,
        timeout: float | None = None,
        wait_timeout: float | None = None,
    ) -> TrustedQueryResult:
        del timeout, wait_timeout
        return self._execute(TrustedQuery(sql, bindings))

    def query_batch(
        self,
        queries: tuple[TrustedQuery, ...],
        *,
        timeout: float | None = None,
        wait_timeout: float | None = None,
    ) -> tuple[TrustedQueryResult, ...]:
        del timeout, wait_timeout
        self._connection.execute("BEGIN")
        try:
            return tuple(self._execute(query) for query in queries)
        finally:
            self._connection.rollback()

    def data_version(
        self,
        *,
        timeout: float | None = None,
        wait_timeout: float | None = None,
    ) -> int:
        del timeout, wait_timeout
        row = self._connection.execute("PRAGMA data_version").fetchone()
        assert row is not None and type(row[0]) is int
        return row[0]

    def _execute(self, query: TrustedQuery) -> TrustedQueryResult:
        self.queries.append(query)
        bindings = query.bindings
        if isinstance(bindings, Mapping):
            bindings = dict(bindings)
        cursor = self._connection.execute(query.sql, bindings or ())
        columns = tuple(item[0] for item in (cursor.description or ()))
        return TrustedQueryResult(
            columns, tuple(tuple(row) for row in cursor.fetchall())
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _protected_artifact_state(database_path: Path) -> tuple[tuple[object, ...], ...]:
    state = []
    for suffix in ("", "-wal", "-journal"):
        path = Path(f"{database_path}{suffix}")
        if not path.exists():
            state.append((suffix, None))
            continue
        status = os.stat(path, follow_symlinks=False)
        state.append(
            (
                suffix,
                status.st_dev,
                status.st_ino,
                status.st_size,
                status.st_mtime_ns,
                _sha256(path),
            )
        )
    return tuple(state)


def _load_basic_runs(
    adapter: TrustedMetadataQueryAdapter,
) -> tuple[object, ...]:
    bootstrap = adapter.bootstrap()
    cursor = 0
    runs: list[object] = []
    while True:
        page = adapter.basic_run_page(cursor, bootstrap.run_id_watermark)
        runs.extend(page.runs)
        if page.complete:
            return tuple(runs)
        cursor = page.next_run_id


def _drain_expensive_run(
    adapter: TrustedMetadataQueryAdapter,
    run_id: int,
    *,
    maximum_pages: int,
) -> dict[str, object]:
    fields: dict[str, object] = {}
    for _page in range(maximum_pages):
        fields = adapter.expensive_run(run_id).as_dict()
        if run_id not in adapter._regular_layout_progress:
            return fields
    raise AssertionError("The real progressive layout verifier did not drain.")


@pytest.mark.parametrize("planned", [False, True], ids=["unplanned", "planned"])
@pytest.mark.parametrize("mixed", [False, True], ids=["z2-only", "mixed"])
def test_real_qcodes_mixed_dependency_preview(tmp_path: Path, planned, mixed) -> None:
    database_path = tmp_path / "mixed-dependencies.db"
    initialise_or_create_database_at(str(database_path), journal_mode="DELETE")
    experiment = load_or_create_experiment("mixed_dependencies", sample_name="2x3")
    dataset = None
    try:
        x, y, z1, z2 = (ManualParameter(name) for name in ("x", "y", "z1", "z2"))
        measurement = Measurement(exp=experiment, name="mixed_dependencies")
        measurement.register_parameter(x)
        measurement.register_parameter(y)
        if mixed:
            measurement.register_parameter(z1, setpoints=(x,))
        measurement.register_parameter(z2, setpoints=(x, y))
        if planned:
            measurement.set_shapes({**({"z1": (2,)} if mixed else {}), "z2": (2, 3)})
        with measurement.run(write_in_background=False) as datasaver:
            dataset = datasaver.dataset
            for x_index in range(2):
                if mixed:
                    datasaver.add_result((x, x_index), (z1, 10 + x_index))
                for y_index in range(3):
                    datasaver.add_result(
                        (x, x_index), (y, y_index), (z2, x_index * 10 + y_index)
                    )
            run_id = datasaver.run_id
    finally:
        if dataset is not None:
            dataset.conn.close()
        experiment.conn.close()

    protected_before = _protected_artifact_state(database_path)
    executor = _ReadOnlySqliteExecutor(database_path)
    try:
        adapter = TrustedMetadataQueryAdapter(executor, database_path)
        _load_basic_runs(adapter)
        for _request in range(3):
            observation = adapter.derived_source_observation(
                run_id,
                database_instance=database_instance(database_path),
                namespace=TrustedSourceRevisionNamespace(b"mixed-dependencies"),
            )
            assert not observation.progressive_layout_pending
            assert [
                (layout.dependent, layout.shape)
                for layout in observation.validated_2d_layouts
            ] == [("z2", (2, 3))]
            payload = render_trusted_derived_payload(
                observation, TrustedWorkKind.PREVIEW
            )
            assert payload["status"] == "ok"
            assert {dict(image)["dependent"] for image in payload["images"]} == (
                {"z1", "z2"} if mixed else {"z2"}
            )
    finally:
        executor.close()
    assert _protected_artifact_state(database_path) == protected_before


def _create_multiple_dependency_groups(
    database_path: Path,
    *,
    planned: bool,
    large: bool = False,
    invalid: bool = False,
    fast_first: bool = False,
    journal_mode: str = "DELETE",
    missing: str | None = None,
) -> tuple[int, dict[str, tuple[int, int]]]:
    initialise_or_create_database_at(str(database_path), journal_mode=journal_mode)
    experiment = load_or_create_experiment("multiple_groups", sample_name="independent")
    dataset = None
    first_shape = (70, 70) if large else (2, 3)
    second_shape = (50, 60) if large else (3, 2)
    expected_shapes = {
        "z2": first_shape[::-1] if fast_first else first_shape,
        "z_reordered": first_shape if fast_first else first_shape[::-1],
        "z_other": second_shape,
    }
    try:
        x, y, t, z1, z2, z_reordered, z_other, z3d = (
            ManualParameter(name)
            for name in ("x", "y", "t", "z1", "z2", "z_reordered", "z_other", "z3d")
        )
        measurement = Measurement(exp=experiment, name="multiple_groups")
        for parameter in (x, y, t):
            measurement.register_parameter(parameter)
        measurement.register_parameter(z1, setpoints=(x,))
        measurement.register_parameter(z2, setpoints=(y, x) if fast_first else (x, y))
        measurement.register_parameter(
            z_reordered, setpoints=(x, y) if fast_first else (y, x)
        )
        measurement.register_parameter(z_other, setpoints=(x, t))
        measurement.register_parameter(z3d, setpoints=(x, y, t))
        if planned:
            measurement.set_shapes({"z1": (2,), **expected_shapes, "z3d": (1, 1, 1)})
        with measurement.run(write_in_background=False) as datasaver:
            dataset = datasaver.dataset
            datasaver.add_result((x, 0), (z1, 10))
            for x_index in range(first_shape[0]):
                for y_index in range(first_shape[1]):
                    datasaver.add_result(
                        (x, x_index),
                        (y, y_index),
                        (
                            z2,
                            float("nan")
                            if (
                                (missing == "first" and x_index == y_index == 0)
                                or (
                                    missing == "interior"
                                    and x_index == 1
                                    and y_index == 1
                                )
                            )
                            else x_index * 100 + y_index,
                        ),
                        (z_reordered, 10_000 + x_index * 100 + y_index),
                    )
            # Another group has a different shape and physically fast axis.
            for t_index in range(second_shape[1]):
                for x_index in range(second_shape[0]):
                    coordinate = x_index
                    if invalid and t_index == second_shape[1] - 1 and x_index == 1:
                        coordinate = 0  # duplicate, not a rectangular grid
                    datasaver.add_result(
                        (x, coordinate),
                        (t, t_index),
                        (z_other, t_index * 100 + x_index),
                    )
            datasaver.add_result((x, 0), (y, 0), (t, 0), (z3d, 1))
            datasaver.add_result((x, 1), (z1, 11))
            run_id = datasaver.run_id
    finally:
        if dataset is not None:
            dataset.conn.close()
        experiment.conn.close()
    return run_id, expected_shapes


@pytest.mark.parametrize("planned", [False, True], ids=["unplanned", "planned"])
@pytest.mark.parametrize("fast_first", [False, True], ids=["slow-first", "fast-first"])
@pytest.mark.parametrize(
    "invalid", [False, True], ids=["rectangular", "invalid-other-group"]
)
def test_real_qcodes_independent_layouts_preserve_dependency_order(
    tmp_path: Path,
    planned: bool,
    fast_first: bool,
    invalid: bool,
) -> None:
    database_path = tmp_path / "multiple-groups.db"
    run_id, shapes = _create_multiple_dependency_groups(
        database_path,
        planned=planned,
        fast_first=fast_first,
        invalid=invalid,
    )
    protected_before = _protected_artifact_state(database_path)
    executor = _ReadOnlySqliteExecutor(database_path)
    try:
        adapter = TrustedMetadataQueryAdapter(executor, database_path)
        _load_basic_runs(adapter)
        for _request in range(3):
            observation = adapter.derived_source_observation(
                run_id,
                database_instance=database_instance(database_path),
                namespace=TrustedSourceRevisionNamespace(b"multiple-groups"),
            )
            assert not observation.progressive_layout_pending
            expected = {
                name: shape
                for name, shape in shapes.items()
                if not (invalid and name == "z_other")
            }
            assert {
                layout.dependent: layout.shape
                for layout in observation.validated_2d_layouts
            } == expected
            views = {parameter.name: parameter for parameter in observation.parameters}
            for layout in observation.validated_2d_layouts:
                assert layout.dependencies == views[layout.dependent].depends_on
                assert layout.source == ("planned" if planned else "observed")
                assert layout.complete
            for kind in (TrustedWorkKind.THUMBNAIL, TrustedWorkKind.PREVIEW):
                payload = render_trusted_derived_payload(observation, kind)
                assert {dict(image)["dependent"] for image in payload["images"]} == {
                    "z1",
                    *expected,
                }
    finally:
        executor.close()
    assert _protected_artifact_state(database_path) == protected_before


@pytest.mark.parametrize("planned", [False, True], ids=["unplanned", "planned"])
@pytest.mark.parametrize("missing", ["first", "interior"])
def test_real_qcodes_mixed_null_cell_does_not_suppress_compatible_grid(
    tmp_path: Path,
    planned: bool,
    missing: str,
) -> None:
    database_path = tmp_path / "mixed-null-cell.db"
    run_id, shapes = _create_multiple_dependency_groups(
        database_path, planned=planned, missing=missing
    )
    protected_before = _protected_artifact_state(database_path)
    executor = _ReadOnlySqliteExecutor(database_path)
    try:
        adapter = TrustedMetadataQueryAdapter(executor, database_path)
        _load_basic_runs(adapter)
        for _request in range(3):
            observation = adapter.derived_source_observation(
                run_id,
                database_instance=database_instance(database_path),
                namespace=TrustedSourceRevisionNamespace(b"mixed-null-cell"),
            )
            assert not observation.progressive_layout_pending
            assert {
                layout.dependent: layout.shape
                for layout in observation.validated_2d_layouts
            } == shapes
            payload = render_trusted_derived_payload(
                observation, TrustedWorkKind.PREVIEW
            )
            images = {
                dict(image)["dependent"]: dict(image) for image in payload["images"]
            }
            assert set(images) == {"z1", *shapes}
            assert images["z2"]["sampled_points"] == 5
            assert images["z_reordered"]["sampled_points"] == 6
    finally:
        executor.close()
    assert _protected_artifact_state(database_path) == protected_before


@pytest.mark.parametrize("planned", [False, True], ids=["unplanned", "planned"])
@pytest.mark.parametrize("bad_first", [False, True], ids=["good-first", "bad-first"])
@pytest.mark.parametrize(
    ("first_bad", "invalid_coordinates"),
    [
        (0.0, None),
        (float("nan"), None),
        (float("inf"), None),
        (float("nan"), "duplicate"),
        (float("nan"), "invalid-axis"),
    ],
    ids=["finite", "nan", "inf", "duplicate", "invalid-axis"],
)
def test_real_qcodes_cross_dependent_row_ownership(
    tmp_path: Path,
    planned: bool,
    bad_first: bool,
    first_bad: float,
    invalid_coordinates: str | None,
) -> None:
    database_path = tmp_path / "cross-dependent-ownership.db"
    initialise_or_create_database_at(str(database_path), journal_mode="DELETE")
    experiment = load_or_create_experiment("cross_dependent", sample_name="2x3")
    dataset = None
    shapes = {"good": (2, 3), "bad": (3, 2)}
    try:
        x, y, good, bad = (ManualParameter(name) for name in ("x", "y", "good", "bad"))
        measurement = Measurement(exp=experiment, name="cross_dependent")
        measurement.register_parameter(x)
        measurement.register_parameter(y)
        measurement.register_parameter(good, setpoints=(x, y))
        measurement.register_parameter(bad, setpoints=(y, x))
        if planned:
            measurement.set_shapes(shapes)
        with measurement.run(write_in_background=False) as datasaver:
            dataset = datasaver.dataset
            for x_index in range(2):
                for y_index in range(3):
                    values = (
                        (good, 100 + x_index * 10 + y_index),
                        (
                            bad,
                            first_bad
                            if x_index == y_index == 0
                            else x_index * 10 + y_index,
                        ),
                    )
                    for parameter, value in values[::-1] if bad_first else values:
                        y_coordinate = y_index
                        if parameter is bad and x_index == 1 and y_index == 2:
                            if invalid_coordinates == "duplicate":
                                y_coordinate = 1
                            elif invalid_coordinates == "invalid-axis":
                                y_coordinate = float("nan")
                        datasaver.add_result(
                            (x, x_index), (y, y_coordinate), (parameter, value)
                        )
            run_id = datasaver.run_id
    finally:
        if dataset is not None:
            dataset.conn.close()
        experiment.conn.close()

    protected_before = _protected_artifact_state(database_path)
    executor = _ReadOnlySqliteExecutor(database_path)
    try:
        adapter = TrustedMetadataQueryAdapter(executor, database_path)
        _load_basic_runs(adapter)
        for _request in range(3):
            start = len(executor.queries)
            observation = adapter.derived_source_observation(
                run_id,
                database_instance=database_instance(database_path),
                namespace=TrustedSourceRevisionNamespace(b"cross-dependent"),
            )
            assert not observation.progressive_layout_pending
            expected_shapes = (
                {"good": shapes["good"]} if invalid_coordinates else shapes
            )
            assert {
                layout.dependent: layout.shape
                for layout in observation.validated_2d_layouts
            } == expected_shapes
            for layout in observation.validated_2d_layouts:
                expected_first = 1 if (layout.dependent == "bad") == bad_first else 2
                assert layout.first_row_id == expected_first
                assert layout.fast_id_stride == 2
            payload = render_trusted_derived_payload(
                observation, TrustedWorkKind.PREVIEW
            )
            assert payload["status"] == "ok"
            images = {
                dict(image)["dependent"]: dict(image) for image in payload["images"]
            }
            assert set(images) == set(expected_shapes)
            assert images["good"]["sampled_points"] == 6
            if not invalid_coordinates:
                assert images["bad"]["sampled_points"] == (
                    6 if np.isfinite(first_bad) else 5
                )
            if _request:
                assert not any(
                    query.sql.endswith('ORDER BY "id" LIMIT ?')
                    and 'AS "qplot_present_0"' in query.sql
                    for query in executor.queries[start:]
                )
    finally:
        executor.close()
    assert _protected_artifact_state(database_path) == protected_before


def _create_same_dependency_grids(
    database_path: Path,
    *,
    planned: bool,
    second_first: bool,
    scenario: str,
    mixed: bool,
    large: bool = False,
    interleaved: bool = False,
    include_solo: bool = True,
) -> tuple[dict[tuple[str, ...], int], dict[str, tuple[int, int]]]:
    initialise_or_create_database_at(str(database_path), journal_mode="DELETE")
    experiment = load_or_create_experiment("same_dependencies", sample_name=scenario)
    shapes = {"z1": (2, 3), "z2": (3, 2)}
    if scenario == "same-shape-extents":
        shapes["z2"] = (2, 3)
    elif scenario == "different-count":
        shapes["z2"] = (3, 4)
    if large:
        shapes = {"z1": (33, 65), "z2": (65, 33)}
    if scenario in {"compatible", "planned-mismatch"}:
        shapes["z2"] = shapes["z1"]
    runs = {}
    dataset = None
    try:
        for names in (
            (("z1",), ("z2",), ("z1", "z2")) if include_solo else (("z1", "z2"),)
        ):
            x, y, z1, z2, other = (
                ManualParameter(name) for name in ("x", "y", "z1", "z2", "other")
            )
            dependents = {"z1": z1, "z2": z2}
            measurement = Measurement(exp=experiment, name="same_dependencies")
            measurement.register_parameter(x)
            measurement.register_parameter(y)
            for name in names:
                measurement.register_parameter(dependents[name], setpoints=(x, y))
            if mixed:
                measurement.register_parameter(other, setpoints=(x,))
            if planned:
                declared = {name: shapes[name] for name in names}
                if scenario == "planned-mismatch" and "z2" in names:
                    declared["z2"] = (3, 2)
                measurement.set_shapes(
                    {**declared, **({"other": (2,)} if mixed else {})}
                )

            def points(name, x=x, y=y, dependents=dependents):
                shape = shapes[name]
                coordinates = (
                    ((i, j) for j in range(shape[1]) for i in range(shape[0]))
                    if interleaved and name == "z2"
                    else ((i, j) for i in range(shape[0]) for j in range(shape[1]))
                )
                for x_index, y_index in coordinates:
                    # Same names, independently chosen finite coordinate vectors.
                    shared = name == "z1" or scenario in {
                        "compatible",
                        "planned-mismatch",
                    }
                    x_value = x_index if shared else 20 - 2 * x_index
                    y_value = y_index if shared else 100 + 3 * y_index
                    if (
                        name == "z2"
                        and x_index == shape[0] - 1
                        and y_index == shape[1] - 1
                    ):
                        if scenario == "duplicate":
                            y_value -= 3
                        elif scenario == "warped":
                            y_value += 1
                    yield (
                        (x, x_value),
                        (y, y_value),
                        (dependents[name], x_index * 10 + y_index),
                    )

            with measurement.run(write_in_background=False) as datasaver:
                dataset = datasaver.dataset
                if mixed:
                    datasaver.add_result((x, -10), (other, 1))
                streams = [
                    points(name) for name in (names[::-1] if second_first else names)
                ]
                acquisition = (
                    chain.from_iterable(zip_longest(*streams))
                    if interleaved
                    else chain.from_iterable(streams)
                )
                for results in acquisition:
                    if results is not None:
                        datasaver.add_result(*results)
                if mixed:
                    datasaver.add_result((x, -9), (other, 2))
                runs[names] = datasaver.run_id
    finally:
        if dataset is not None:
            dataset.conn.close()
        experiment.conn.close()
    return runs, shapes


@pytest.mark.parametrize("planned", [False, True], ids=["unplanned", "planned"])
@pytest.mark.parametrize("second_first", [False, True], ids=["z1-first", "z2-first"])
@pytest.mark.parametrize("mixed", [False, True], ids=["same-tuple", "another-group"])
@pytest.mark.parametrize(
    ("scenario", "interleaved"),
    [
        (scenario, interleaved)
        for scenario in (
            "different-shape",
            "same-shape-extents",
            "different-count",
            "duplicate",
            "warped",
            "planned-mismatch",
        )
        for interleaved in (
            (False,) if scenario == "different-count" else (False, True)
        )
    ],
    ids=lambda value: (
        ("interleaved-fast-first" if value else "blocked")
        if isinstance(value, bool)
        else value
    ),
)
def test_real_qcodes_same_dependencies_keep_independent_previews(
    tmp_path: Path,
    planned: bool,
    second_first: bool,
    mixed: bool,
    interleaved: bool,
    scenario: str,
) -> None:
    database_path = tmp_path / "same-dependencies.db"
    runs, shapes = _create_same_dependency_grids(
        database_path,
        planned=planned,
        second_first=second_first,
        scenario=scenario,
        mixed=mixed,
        interleaved=interleaved,
    )
    protected_before = _protected_artifact_state(database_path)
    executor = _ReadOnlySqliteExecutor(database_path)
    try:
        adapter = TrustedMetadataQueryAdapter(executor, database_path)
        _load_basic_runs(adapter)
        solo_images = {}
        revisions = {}
        options = RenderingOptions.from_mapping({"width": 60, "height": 60})
        for names, run_id in runs.items():
            expected = {
                name: shapes[name]
                for name in names
                if not (
                    name == "z2"
                    and (
                        scenario in {"duplicate", "warped"}
                        or (planned and scenario == "planned-mismatch")
                    )
                )
            }
            for request in range(3):
                start = len(executor.queries)
                observation = adapter.derived_source_observation(
                    run_id,
                    database_instance=database_instance(database_path),
                    namespace=TrustedSourceRevisionNamespace(b"same-dependencies"),
                )
                assert not observation.progressive_layout_pending
                assert {
                    layout.dependent: layout.shape
                    for layout in observation.validated_2d_layouts
                } == expected
                revision = trusted_derived_source_revision(observation)
                if request:
                    assert revision == revisions[run_id]
                    if expected or len(names) > 1 or mixed:
                        assert not any(
                            query.sql.endswith('ORDER BY "id" LIMIT ?')
                            and 'AS "qplot_present_0"' in query.sql
                            for query in executor.queries[start:]
                        )
                revisions[run_id] = revision
                for kind in (TrustedWorkKind.THUMBNAIL, TrustedWorkKind.PREVIEW):
                    payload = render_trusted_derived_payload(observation, kind, options)
                    images = {
                        dict(image)["dependent"]: dict(image)
                        for image in payload["images"]
                    }
                    assert set(images) == {*expected, *({"other"} if mixed else set())}
                    for name in expected:
                        assert images[name]["sampled_points"] == np.prod(shapes[name])
                        assert images[name]["bytes"].startswith(b"\x89PNG\r\n\x1a\n")
                        if len(names) == 1:
                            solo_images[name, kind] = images[name]["bytes"]
                        else:
                            assert images[name]["bytes"] == solo_images[name, kind]
    finally:
        executor.close()
    assert _protected_artifact_state(database_path) == protected_before


@pytest.mark.parametrize("planned", [False, True], ids=["unplanned", "planned"])
@pytest.mark.parametrize("scenario", ["different-shape", "compatible", "duplicate"])
def test_real_qcodes_same_tuple_shares_scan_budget_and_preserves_cancellation(
    tmp_path: Path, planned: bool, scenario: str
) -> None:
    database_path = tmp_path / "same-tuple-progressive.db"
    runs, shapes = _create_same_dependency_grids(
        database_path,
        planned=planned,
        second_first=False,
        scenario=scenario,
        mixed=False,
        large=True,
        include_solo=False,
    )
    run_id = runs["z1", "z2"]
    protected_before = _protected_artifact_state(database_path)

    def is_layout_page(query):
        return (
            'AS "qplot_present_0"' in query.sql
            and query.sql.endswith('ORDER BY "id" LIMIT ?')
            and query.bindings[-1] == 4096
        )

    class RecordingExecutor(_ReadOnlySqliteExecutor):
        def __init__(self, path):
            super().__init__(path)
            self.results = []
            self.cancel_next_page = False

        def _execute(self, query):
            if is_layout_page(query) and self.cancel_next_page:
                self.cancel_next_page = False
                raise InterruptedError("cancelled same-tuple layout page")
            result = super()._execute(query)
            self.results.append((query, result))
            return result

    executor = RecordingExecutor(database_path)
    try:
        adapter = TrustedMetadataQueryAdapter(executor, database_path)
        _load_basic_runs(adapter)
        observations = []
        namespace = TrustedSourceRevisionNamespace(b"same-tuple-progressive")
        for request in range(2):
            if request:
                previous = adapter._independent_layouts[run_id]
                executor.cancel_next_page = True
                with pytest.raises(InterruptedError, match="same-tuple layout page"):
                    adapter.derived_source_observation(
                        run_id,
                        database_instance=database_instance(database_path),
                        namespace=namespace,
                    )
                assert adapter._independent_layouts[run_id] == previous
                assert not executor._connection.in_transaction
            start = len(executor.results)
            observation = adapter.derived_source_observation(
                run_id,
                database_instance=database_instance(database_path),
                namespace=namespace,
            )
            observations.append(observation)
            results = executor.results[start:]
            pages = [result for query, result in results if is_layout_page(query)]
            assert len(pages) == 1
            assert len(pages[0].rows) <= 4096
            assert (
                sum(len(row) for row in pages[0].rows)
                <= TRUSTED_DERIVED_MAX_SAMPLE_CELLS
            )
            samples = [
                result
                for query, result in results
                if query.sql.startswith('SELECT "id", CASE WHEN')
                and 'AS "qplot_axis_0"' not in query.sql
            ]
            assert (
                sum(len(result.rows) for result in samples)
                <= TRUSTED_DERIVED_MAX_SAMPLE_ROWS
            )
            assert (
                sum(len(row) for result in samples for row in result.rows)
                <= TRUSTED_DERIVED_MAX_SAMPLE_CELLS
            )
            state = adapter._independent_layouts[run_id]
            assert len(state.states) == 2
            assert len(repr(state)) < 8192
            assert not executor._connection.in_transaction
        assert observations[0].progressive_layout_cursor == 4096
        assert observations[0].progressive_layout_pending
        assert not observations[1].progressive_layout_pending
        assert trusted_derived_source_revision(
            observations[0]
        ) != trusted_derived_source_revision(observations[1])
        expected = {
            name: shape
            for name, shape in shapes.items()
            if name != "z2" or scenario != "duplicate"
        }
        assert {
            layout.dependent: layout.shape
            for layout in observations[1].validated_2d_layouts
        } == expected
        for kind in (TrustedWorkKind.THUMBNAIL, TrustedWorkKind.PREVIEW):
            first = render_trusted_derived_payload(observations[1], kind)
            start = len(executor.results)
            cached = adapter.derived_source_observation(
                run_id,
                database_instance=database_instance(database_path),
                namespace=namespace,
            )
            assert not any(
                is_layout_page(query) for query, _result in executor.results[start:]
            )
            assert trusted_derived_source_revision(
                cached
            ) == trusted_derived_source_revision(observations[1])
            assert render_trusted_derived_payload(cached, kind) == first
            assert {dict(image)["dependent"] for image in first["images"]} == set(
                expected
            )
    finally:
        executor.close()
    assert _protected_artifact_state(database_path) == protected_before


@pytest.mark.parametrize("planned", [False, True], ids=["unplanned", "planned"])
def test_real_qcodes_mixed_progressive_scan_shares_read_budget(
    tmp_path: Path, planned: bool
) -> None:
    database_path = tmp_path / "mixed-progressive.db"
    run_id, shapes = _create_multiple_dependency_groups(
        database_path, planned=planned, large=True
    )
    protected_before = _protected_artifact_state(database_path)

    def is_layout_page(query):
        return query.sql.startswith('SELECT "id", CASE WHEN') and query.sql.endswith(
            'ORDER BY "id" LIMIT ?'
        )

    class RecordingExecutor(_ReadOnlySqliteExecutor):
        def __init__(self, path):
            super().__init__(path)
            self.results = []
            self.cancel_next_page = False

        def _execute(self, query):
            if is_layout_page(query) and self.cancel_next_page:
                self.cancel_next_page = False
                raise InterruptedError("cancelled shared layout page")
            result = super()._execute(query)
            self.results.append((query, result))
            return result

    executor = RecordingExecutor(database_path)
    try:
        adapter = TrustedMetadataQueryAdapter(executor, database_path)
        _load_basic_runs(adapter)
        revisions = []
        cursors = []
        for request_index in range(5):
            if request_index == 1:
                previous = adapter._independent_layouts[run_id]
                executor.cancel_next_page = True
                with pytest.raises(
                    InterruptedError, match="cancelled shared layout page"
                ):
                    adapter.derived_source_observation(
                        run_id,
                        database_instance=database_instance(database_path),
                        namespace=TrustedSourceRevisionNamespace(b"mixed-progressive"),
                    )
                assert adapter._independent_layouts[run_id] == previous
                assert not executor._connection.in_transaction
            start = len(executor.results)
            observation = adapter.derived_source_observation(
                run_id,
                database_instance=database_instance(database_path),
                namespace=TrustedSourceRevisionNamespace(b"mixed-progressive"),
            )
            request_results = executor.results[start:]
            pages = [
                result for query, result in request_results if is_layout_page(query)
            ]
            assert len(pages) == 1
            assert len(pages[0].rows) <= 4096
            assert (
                sum(len(row) for row in pages[0].rows)
                <= TRUSTED_DERIVED_MAX_SAMPLE_CELLS
            )
            samples = [
                result
                for query, result in request_results
                if query.sql.startswith('SELECT "id", CASE WHEN')
                and not is_layout_page(query)
            ]
            assert (
                sum(len(result.rows) for result in samples)
                <= TRUSTED_DERIVED_MAX_SAMPLE_ROWS
            )
            assert (
                sum(len(row) for result in samples for row in result.rows)
                <= TRUSTED_DERIVED_MAX_SAMPLE_CELLS
            )
            assert len(observation.sample_rows) <= TRUSTED_DERIVED_MAX_SAMPLE_ROWS
            assert not executor._connection.in_transaction
            revisions.append(trusted_derived_source_revision(observation))
            cursors.append(observation.progressive_layout_cursor)
            if not observation.progressive_layout_pending:
                break
            assert not observation.validated_2d_layouts
        else:
            pytest.fail("Mixed progressive verification did not complete")
        assert cursors == [4096, 8192, 12288, 0]
        assert len(set(revisions)) == len(revisions)
        assert {
            layout.dependent: layout.shape
            for layout in observation.validated_2d_layouts
        } == shapes
        payload = render_trusted_derived_payload(observation, TrustedWorkKind.PREVIEW)
        assert {dict(image)["dependent"] for image in payload["images"]} == {
            "z1",
            *shapes,
        }
        # Repeated requests reuse all proofs without another verification scan.
        start = len(executor.results)
        cached = adapter.derived_source_observation(
            run_id,
            database_instance=database_instance(database_path),
            namespace=TrustedSourceRevisionNamespace(b"mixed-progressive"),
        )
        assert cached.validated_2d_layouts == observation.validated_2d_layouts
        assert not cached.progressive_layout_pending
        assert not any(
            is_layout_page(query) for query, _result in executor.results[start:]
        )
    finally:
        executor.close()
    assert _protected_artifact_state(database_path) == protected_before


@pytest.mark.parametrize("planned", [False, True], ids=["unplanned", "planned"])
def test_real_qcodes_mixed_wal_preview_uses_trusted_reader(
    tmp_path: Path, planned: bool
) -> None:
    database_path = tmp_path / "mixed-wal.db"
    run_id, shapes = _create_multiple_dependency_groups(
        database_path, planned=planned, journal_mode="WAL"
    )
    # Fixture generation leaves a real committed WAL frame visible throughout
    # the viewing phase. Keep its writer open until artifact checks finish.
    writer = sqlite3.connect(database_path)
    writer.execute(
        "UPDATE runs SET name = ? WHERE run_id = ?", ("committed mixed WAL", run_id)
    )
    writer.commit()
    assert Path(f"{database_path}-wal").stat().st_size > 0
    protected_before = _protected_artifact_state(database_path)
    instance = database_instance(database_path)
    service = TrustedLiveReadService(
        database_path, expected_database_instance=instance, request_timeout_seconds=30
    )
    try:
        bootstrap = service.submit_bootstrap().wait(30)
        service.submit_basic_page(0, bootstrap.run_id_watermark).wait(30)
        observation = service.submit_derived_source(run_id).wait(30)
        assert not observation.progressive_layout_pending
        assert {
            layout.dependent: layout.shape
            for layout in observation.validated_2d_layouts
        } == shapes
        payload = render_trusted_derived_payload(observation, TrustedWorkKind.PREVIEW)
        assert {dict(image)["dependent"] for image in payload["images"]} == {
            "z1",
            *shapes,
        }
    finally:
        service.close(timeout=30)
        try:
            assert _protected_artifact_state(database_path) == protected_before
        finally:
            writer.close()


@pytest.mark.parametrize("planned", [False, True], ids=["unplanned", "planned"])
def test_real_qcodes_mixed_fast_row_spans_verification_pages(
    tmp_path: Path,
    planned: bool,
) -> None:
    database_path = tmp_path / "mixed-wide-grid.db"
    initialise_or_create_database_at(str(database_path), journal_mode="DELETE")
    experiment = load_or_create_experiment("mixed_wide", sample_name="2x4100")
    dataset = None
    try:
        x, y, z1, z2 = (ManualParameter(name) for name in ("x", "y", "z1", "z2"))
        measurement = Measurement(exp=experiment, name="mixed_wide")
        measurement.register_parameter(x)
        measurement.register_parameter(y)
        measurement.register_parameter(z1, setpoints=(x,))
        measurement.register_parameter(z2, setpoints=(x, y))
        if planned:
            measurement.set_shapes({"z1": (2,), "z2": (2, 4100)})
        with measurement.run(write_in_background=False) as datasaver:
            dataset = datasaver.dataset
            for x_index in range(2):
                datasaver.add_result((x, x_index), (z1, x_index + 10))
                for y_index in range(4100):
                    datasaver.add_result(
                        (x, x_index), (y, y_index), (z2, x_index * 10000 + y_index)
                    )
            run_id = datasaver.run_id
    finally:
        if dataset is not None:
            dataset.conn.close()
        experiment.conn.close()
    protected_before = _protected_artifact_state(database_path)
    executor = _ReadOnlySqliteExecutor(database_path)
    try:
        adapter = TrustedMetadataQueryAdapter(executor, database_path)
        _load_basic_runs(adapter)
        for expected_cursor in (4096, 8192, 0):
            observation = adapter.derived_source_observation(
                run_id,
                database_instance=database_instance(database_path),
                namespace=TrustedSourceRevisionNamespace(b"mixed-wide"),
            )
            assert observation.progressive_layout_cursor == expected_cursor
            assert observation.progressive_layout_pending == bool(expected_cursor)
        assert [
            (layout.dependent, layout.shape)
            for layout in observation.validated_2d_layouts
        ] == [("z2", (2, 4100))]
        payload = render_trusted_derived_payload(observation, TrustedWorkKind.PREVIEW)
        assert {dict(image)["dependent"] for image in payload["images"]} == {"z1", "z2"}
    finally:
        executor.close()
    assert _protected_artifact_state(database_path) == protected_before


@pytest.mark.parametrize("planned", [False, True], ids=["unplanned", "planned"])
def test_real_qcodes_mixed_layout_cache_resumes_live_append(
    tmp_path: Path, planned: bool
) -> None:
    database_path = tmp_path / "mixed-live-append.db"
    initialise_or_create_database_at(str(database_path), journal_mode="WAL")
    experiment = load_or_create_experiment("mixed_append", sample_name="2x3")
    dataset = None
    service = None
    try:
        x, y, z1, z2 = (ManualParameter(name) for name in ("x", "y", "z1", "z2"))
        measurement = Measurement(exp=experiment, name="mixed_append")
        measurement.register_parameter(x)
        measurement.register_parameter(y)
        measurement.register_parameter(z1, setpoints=(x,))
        measurement.register_parameter(z2, setpoints=(x, y))
        if planned:
            measurement.set_shapes({"z1": (2,), "z2": (2, 3)})
        with measurement.run(write_in_background=False) as datasaver:
            dataset = datasaver.dataset
            for x_index in range(2):
                datasaver.add_result((x, x_index), (z1, x_index + 10))
                for y_index in range(3):
                    datasaver.add_result(
                        (x, x_index), (y, y_index), (z2, x_index * 10 + y_index)
                    )
                datasaver.flush_data_to_database(block=True)
                if service is None:
                    service = TrustedLiveReadService(
                        database_path,
                        expected_database_instance=database_instance(database_path),
                        request_timeout_seconds=30,
                    )
                    bootstrap = service.submit_bootstrap().wait(30)
                    service.submit_basic_page(0, bootstrap.run_id_watermark).wait(30)
                protected_before = _protected_artifact_state(database_path)
                observation = service.submit_derived_source(datasaver.run_id).wait(30)
                assert not observation.progressive_layout_pending
                if x_index == 1:
                    assert [
                        (layout.dependent, layout.shape)
                        for layout in observation.validated_2d_layouts
                    ] == [("z2", (2, 3))]
                    payload = render_trusted_derived_payload(
                        observation, TrustedWorkKind.PREVIEW
                    )
                    assert {
                        dict(image)["dependent"] for image in payload["images"]
                    } == {"z1", "z2"}
                assert _protected_artifact_state(database_path) == protected_before
        protected_before = _protected_artifact_state(database_path)
        completed = service.submit_derived_source(datasaver.run_id).wait(30)
        assert dict(completed.run_fields)["is_completed"] == 1
        assert completed.validated_2d_layouts[0].complete
        assert _protected_artifact_state(database_path) == protected_before
    finally:
        if service is not None:
            service.close(timeout=30)
        if dataset is not None:
            dataset.conn.close()
        experiment.conn.close()


def test_real_qcodes_progressive_shape_enriches_cached_setpoint_steps(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "progressive-setpoint-steps.db"
    initialise_or_create_database_at(str(database_path), journal_mode="DELETE")
    experiment = load_or_create_experiment("progressive_steps", sample_name="70x70")
    dataset = None
    try:
        x = ManualParameter("x")
        y = ManualParameter("y")
        z = ManualParameter("z")
        measurement = Measurement(exp=experiment, name="unplanned_grid")
        measurement.write_period = 3_600
        measurement.register_parameter(x)
        measurement.register_parameter(y)
        measurement.register_parameter(z, setpoints=(x, y))
        with measurement.run(write_in_background=False) as datasaver:
            dataset = datasaver.dataset
            for x_index in range(70):
                for y_index in range(70):
                    datasaver.add_result(
                        (x, float(x_index)),
                        (y, float(y_index)),
                        (z, float(x_index * 70 + y_index)),
                    )
            run_id = datasaver.run_id
    finally:
        if dataset is not None:
            dataset.conn.close()
        experiment.conn.close()

    protected_before = _protected_artifact_state(database_path)
    executor = _ReadOnlySqliteExecutor(database_path)
    try:
        adapter = TrustedMetadataQueryAdapter(executor, database_path)
        runs = _load_basic_runs(adapter)
        assert len(runs) == 1
        assert runs[0].as_dict()["setpoint_shape"] is None

        initial = adapter.expensive_run(run_id).as_dict()
        assert initial["is_completed"] == 1
        assert initial["result_count"] == 4_900
        assert initial["setpoint_shape"] is None
        assert adapter._regular_layout_progress[run_id].after_row_id == 4_096
        initial_summaries = adapter.selected_run_detail(run_id).setpoint_summaries
        assert {summary.name: summary.steps for summary in initial_summaries} == {
            "x": None,
            "y": None,
        }

        def edge_queries() -> tuple[TrustedQuery, ...]:
            return tuple(
                query
                for query in executor.queries
                if query.sql.startswith("SELECT (SELECT ")
                and 'ORDER BY "id" ' in query.sql
            )

        cached_edge_queries = edge_queries()
        assert cached_edge_queries
        verified = _drain_expensive_run(adapter, run_id, maximum_pages=3)
        assert verified["result_count"] == 4_900
        assert verified["setpoint_shape"] == [70, 70]
        assert verified["setpoint_shape_source"] == "observed"
        pages = [
            query
            for query in executor.queries
            if 'AS "qplot_axis_0"' in query.sql
            and query.bindings
            and query.bindings[-1] == 4_096
        ]
        assert [query.bindings[0] for query in pages] == [0, 4_096]

        # The count changes without another QCoDeS commit or result-row change.
        # Both the first verified detail and later cached reads must expose it.
        for _read in range(3):
            summaries = adapter.selected_run_detail(run_id).setpoint_summaries
            assert {summary.name: summary.steps for summary in summaries} == {
                "x": 70,
                "y": 70,
            }
            assert (
                [(summary.first, summary.last) for summary in summaries]
                == [(summary.first, summary.last) for summary in initial_summaries]
                == [(0.0, 69.0), (0.0, 69.0)]
            )
            assert adapter.expensive_run(run_id).as_dict()["setpoint_shape"] == [70, 70]
        assert edge_queries() == cached_edge_queries
        assert _protected_artifact_state(database_path) == protected_before
    finally:
        executor.close()
    assert _protected_artifact_state(database_path) == protected_before


def test_real_qcodes_partial_thumbnail_keeps_missing_parameter_identity(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "partial-thumbnail-identity.db"
    original_database_path = qcodes.config.core.db_location
    executor = None
    cell = None
    protected_before = None
    try:
        initialise_or_create_database_at(str(database_path))
        experiment = load_or_create_experiment(
            "partial_thumbnail_identity",
            sample_name="missing_first_dependent",
        )
        x = ManualParameter("partial_identity_x")
        first = ManualParameter("partial_identity_first")
        signal = ManualParameter("partial_identity_signal")
        measurement = Measurement(exp=experiment, name="partial_thumbnail_identity")
        measurement.register_parameter(x)
        measurement.register_parameter(first, setpoints=(x,))
        measurement.register_parameter(signal, setpoints=(x,))
        with measurement.run() as datasaver:
            datasaver.add_result((x, 0.0), (signal, 1.0))
            datasaver.add_result((x, 1.0), (signal, 2.0))
            run_id = datasaver.run_id
            guid = datasaver.dataset.guid

        protected_before = _protected_artifact_state(database_path)
        executor = _ReadOnlySqliteExecutor(database_path)
        adapter = TrustedMetadataQueryAdapter(executor, database_path)
        runs = _load_basic_runs(adapter)
        run = next(item for item in runs if item.run_id == run_id)
        metadata = run.as_dict()
        assert metadata["measure_parameters"] == [
            "partial_identity_first",
            "partial_identity_signal",
        ]

        observation = adapter.derived_source_observation(
            run_id,
            database_instance=database_instance(database_path),
            namespace=TrustedSourceRevisionNamespace(b"partial-thumbnail-identity"),
        )
        payload = render_trusted_derived_payload(
            observation,
            TrustedWorkKind.THUMBNAIL,
        )
        assert [dict(image)["dependent"] for image in payload["images"]] == [
            "partial_identity_signal",
        ]

        class _Decoder:
            _pairs = staticmethod(TrustedDerivedQtBridge._pairs)

        decoder = _Decoder()
        decoder._parameters_by_guid = {guid: observation.parameters}
        previews, decode_error = TrustedDerivedQtBridge._decode_images(
            decoder,
            guid,
            payload,
        )
        assert decode_error is None
        assert [preview["parameter"] for preview in previews] == [
            "partial_identity_signal",
        ]
        assert not previews[0]["image"].isNull()

        cell = RunPreviewCell(guid, 2)
        cell.update_placeholder_metadata(metadata)
        cell.show_previews(previews)
        widgets = [
            cell.content_layout.itemAt(index).widget()
            for index in range(cell.content_layout.count())
            if cell.content_layout.itemAt(index).widget() is not None
        ]
        assert [widget.objectName() for widget in widgets] == [
            "measurementPreviewPlaceholder",
            "measurementPreviewImage",
        ]
        assert [widget.parameter for widget in widgets] == [
            "partial_identity_first",
            "partial_identity_signal",
        ]
        assert all(isinstance(widget, PreviewImageLabel) for widget in widgets)
    finally:
        if cell is not None:
            cell.deleteLater()
        if executor is not None:
            executor.close()
        qcodes.config.core.db_location = original_database_path

    assert protected_before is not None
    assert _protected_artifact_state(database_path) == protected_before


def test_real_generated_unplanned_grids_get_exact_shapes_and_full_domain_sample(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "stage5c-current-qcodes.db"
    generate_database(
        (
            RunSpecification(
                2,
                "current",
                "Current",
                "nA",
                -0.01,
                0.01,
                201,
                -1.0,
                1.0,
                301,
            ),
            RunSpecification(
                2,
                "signal",
                "Signal",
                "a.u.",
                -0.02,
                0.02,
                301,
                -2.0,
                2.0,
                451,
            ),
        ),
        database_path,
        rng=np.random.default_rng(5),
    )
    before = (
        os.stat(database_path, follow_symlinks=False),
        _sha256(database_path),
    )
    executor = _ReadOnlySqliteExecutor(database_path)
    try:
        adapter = TrustedMetadataQueryAdapter(executor, database_path)
        runs = _load_basic_runs(adapter)
        assert len(runs) == 2

        first = _drain_expensive_run(adapter, 1, maximum_pages=35)
        second = _drain_expensive_run(adapter, 2, maximum_pages=35)
        assert first["setpoint_shape"] == [201, 301]
        assert first["setpoint_count"] == 60_501
        assert second["setpoint_shape"] == [301, 451]
        assert second["setpoint_count"] == 135_751
        assert first["setpoint_shape_source"] == "observed"
        assert second["setpoint_shape_source"] == "observed"

        observation = adapter.derived_source_observation(
            2,
            database_instance=database_instance(database_path),
            namespace=TrustedSourceRevisionNamespace(b"stage5c-real-qcodes"),
        )
        assert len(observation.sample_rows) <= 4_095
        assert len(observation.validated_2d_layouts) == 1
        layout = observation.validated_2d_layouts[0]
        assert layout.shape == (301, 451)
        assert layout.fast_axis_index == 1
        indexes = {name: index for index, name in enumerate(observation.sample_columns)}
        slow_values = {
            row[indexes["V_SD"]]
            for row in observation.sample_rows
            if row[indexes["V_SD"]] is not None
        }
        fast_values = {
            row[indexes["V_G"]]
            for row in observation.sample_rows
            if row[indexes["V_G"]] is not None
        }
        assert len(slow_values) > 40
        assert len(fast_values) > 60
        assert (min(slow_values), max(slow_values)) == (-0.02, 0.02)
        assert (min(fast_values), max(fast_values)) == (-2.0, 2.0)

        payload = render_trusted_derived_payload(
            observation,
            TrustedWorkKind.PREVIEW,
        )
        assert payload["status"] == "ok"
        image = dict(payload["images"][0])
        assert image["dimensions"] == 2
        assert image["sampled_points"] == len(observation.sample_rows)

        result_sql = "\n".join(
            query.sql
            for query in executor.queries
            if "results-" in query.sql or '"results_' in query.sql
        ).upper()
        assert "OFFSET" not in result_sql
        assert "DISTINCT" not in result_sql
        assert "GROUP BY" not in result_sql
        assert "COUNT(" not in result_sql
    finally:
        executor.close()

    after_status = os.stat(database_path, follow_symlinks=False)
    assert (after_status.st_dev, after_status.st_ino) == (
        before[0].st_dev,
        before[0].st_ino,
    )
    assert (after_status.st_size, after_status.st_mtime_ns, _sha256(database_path)) == (
        before[0].st_size,
        before[0].st_mtime_ns,
        before[1],
    )
    assert not Path(f"{database_path}-wal").exists()
    assert not Path(f"{database_path}-journal").exists()


def test_real_current_qcodes_unfinalized_interleaved_grid_updates_after_append(
    tmp_path: Path,
) -> None:
    """Match the reported 108 x 861, two-output live QCoDeS layout."""

    database_path = tmp_path / "stage5c-live-interleaved.db"
    original_database_path = qcodes.config.core.db_location
    experiment = None
    dataset = None
    run_context = None
    executor = None
    try:
        initialise_or_create_database_at(str(database_path), journal_mode="WAL")
        experiment = load_or_create_experiment(
            "stage5c_live_interleaved",
            sample_name="unfinalized_108x861",
        )
        slow = ManualParameter("stage5c_live_slow")
        fast = ManualParameter("stage5c_live_fast")
        signal = ManualParameter("stage5c_live_signal")
        signal_b = ManualParameter("stage5c_live_signal_b")
        measurement = Measurement(
            exp=experiment,
            name="stage5c_live_interleaved",
        )
        measurement.write_period = 3_600
        measurement.register_parameter(slow)
        measurement.register_parameter(fast)
        measurement.register_parameter(signal, setpoints=(slow, fast))
        measurement.register_parameter(signal_b, setpoints=(slow, fast))
        run_context = measurement.run(write_in_background=False)
        datasaver = run_context.__enter__()
        dataset = datasaver.dataset

        # One scalar add_result with same-dependency outputs is the production
        # current-QCoDeS path: it creates two physical rows per logical cell.
        for slow_index in range(107):
            for fast_index in range(861):
                datasaver.add_result(
                    (slow, float(slow_index)),
                    (fast, float(fast_index)),
                    (signal, float(slow_index * 1_000 + fast_index)),
                    (signal_b, float(1_000_000 + slow_index * 1_000 + fast_index)),
                )
        datasaver.flush_data_to_database(block=True)

        executor = _ReadOnlySqliteExecutor(database_path)
        adapter = TrustedMetadataQueryAdapter(executor, database_path)
        runs = _load_basic_runs(adapter)
        assert len(runs) == 1
        run_id = int(dataset.run_id)

        def enrich_until_shape(
            expected_shape: tuple[int, int],
            *,
            maximum_pages: int,
        ) -> dict[str, object]:
            fields: dict[str, object] = {}
            for _page in range(maximum_pages):
                fields = adapter.expensive_run(run_id).as_dict()
                if tuple(fields.get("setpoint_shape") or ()) == expected_shape:
                    return fields
                # An invalid/unsupported prefix has no resumable work.  Stop
                # immediately so the pre-repair failure remains quick.
                if run_id not in adapter._regular_layout_progress:
                    return fields
            return fields

        initial = enrich_until_shape((107, 861), maximum_pages=48)
        assert initial["is_completed"] == 0
        assert initial["setpoint_shape"] == [107, 861]
        assert initial["setpoint_count"] == 107 * 861
        assert initial["read_setpoint_count"] == 107 * 861
        assert initial["point_shape"] == [107, 861, 2]
        initial_observation = adapter.derived_source_observation(
            run_id,
            database_instance=database_instance(database_path),
            namespace=TrustedSourceRevisionNamespace(b"stage5c-live-initial"),
        )
        assert initial_observation.result_watermark == 107 * 861 * 2
        assert len(initial_observation.validated_2d_layouts) == 2
        assert {
            layout.dependent for layout in initial_observation.validated_2d_layouts
        } == {signal.name, signal_b.name}
        assert all(
            layout.shape == (107, 861)
            and layout.fast_axis_index == 1
            and not layout.complete
            for layout in initial_observation.validated_2d_layouts
        )

        initial_watermark = initial_observation.result_watermark
        query_start = len(executor.queries)
        for fast_index in range(861):
            datasaver.add_result(
                (slow, 107.0),
                (fast, float(fast_index)),
                (signal, float(107_000 + fast_index)),
                (signal_b, float(1_107_000 + fast_index)),
            )
        datasaver.flush_data_to_database(block=True)
        protected_before_reader = _protected_artifact_state(database_path)

        appended = enrich_until_shape((108, 861), maximum_pages=3)
        assert appended["is_completed"] == 0
        assert appended["setpoint_shape"] == [108, 861]
        assert appended["setpoint_count"] == 92_988
        assert appended["read_setpoint_count"] == 92_988
        assert appended["point_shape"] == [108, 861, 2]
        assert appended["setpoint_count"] != 185_976
        appended_observation = adapter.derived_source_observation(
            run_id,
            database_instance=database_instance(database_path),
            namespace=TrustedSourceRevisionNamespace(b"stage5c-live-appended"),
        )
        assert appended_observation.result_watermark == 185_976
        assert len(appended_observation.validated_2d_layouts) == 2
        assert {
            layout.dependent for layout in appended_observation.validated_2d_layouts
        } == {signal.name, signal_b.name}
        assert all(
            layout.shape == (108, 861)
            and layout.fast_axis_index == 1
            and not layout.complete
            for layout in appended_observation.validated_2d_layouts
        )

        appended_queries = executor.queries[query_start:]
        progressive_pages = [
            query
            for query in appended_queries
            if 'AS "qplot_axis_0"' in query.sql
            and query.bindings
            and query.bindings[-1] == 4_096
        ]
        assert progressive_pages
        assert progressive_pages[0].bindings[0] == initial_watermark
        assert _protected_artifact_state(database_path) == protected_before_reader
    finally:
        if executor is not None:
            executor.close()
        if run_context is not None:
            run_context.__exit__(None, None, None)
        connections = tuple(
            {
                id(connection): connection
                for connection in (
                    getattr(dataset, "conn", None),
                    getattr(experiment, "conn", None),
                )
                if connection is not None
            }.values()
        )
        try:
            for connection in connections:
                connection.close()
        finally:
            qcodes.config.core.db_location = original_database_path
