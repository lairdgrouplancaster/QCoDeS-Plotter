"""Real current-QCoDeS acceptance for the Stage 5C shape/render repair."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Mapping
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
    TrustedMetadataQueryAdapter,
    TrustedSourceRevisionNamespace,
)
from qplot.datahandling.trusted_work_scheduler import TrustedWorkKind
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
        return TrustedQueryResult(columns, tuple(tuple(row) for row in cursor.fetchall()))


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
        indexes = {
            name: index for index, name in enumerate(observation.sample_columns)
        }
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
