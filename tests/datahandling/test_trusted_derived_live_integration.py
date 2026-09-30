"""Real public-QCoDeS WAL acceptance for the Stage 5B backend."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import apsw
import pytest
import qcodes
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.parameters import ManualParameter

from qplot.datahandling.file_identity import database_instance
from qplot.datahandling.trusted_derived_cache import TrustedDerivedDiskCache
from qplot.datahandling.trusted_derived_rendering import (
    render_trusted_derived_payload,
)
from qplot.datahandling.trusted_live_queries import trusted_source_revision
from qplot.datahandling.trusted_live_service import TrustedLiveReadService
from qplot.datahandling.trusted_work_coordinator import (
    TrustedDerivedRun,
    TrustedWorkCoordinator,
)
from qplot.datahandling.trusted_work_scheduler import TrustedWorkKind
from tests.datahandling.test_trusted_derived_rendering import (
    _decode_png_rgba,
    _rgba_at,
)
from tests.datahandling.test_trusted_live import (
    _assert_protected_artifacts_unchanged,
    _QcodesWalWriter,
    _stable_artifact_state,
)

pytestmark = pytest.mark.timeout(120)


@pytest.fixture
def stage5b_wal_writer(tmp_path: Path) -> _QcodesWalWriter:
    database_directory = tmp_path / "database"
    database_directory.mkdir()
    writer = _QcodesWalWriter.start(database_directory / "stage5b-live.db")
    try:
        assert writer.startup["run_count"] == 1
        assert writer.request("commit_many", count=999) == 999
        yield writer
    finally:
        writer.close()


def _drain(coordinator: TrustedWorkCoordinator, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        coordinator.poll()
        snapshot = coordinator.snapshot()
        if snapshot.pending_count == 0 and not coordinator.active:
            return
        time.sleep(0.005)
    raise AssertionError("The real Stage 5B coordinator did not drain")


def _source_fields(publication: object) -> dict[str, object]:
    result = publication.result  # type: ignore[attr-defined]
    return dict(result["source"])


def _create_completed_rectangular_qcodes_run(
    database_path: Path,
) -> tuple[int, str, int]:
    """Create an unplanned 17 x 23 shared-row grid with one NULL output."""

    experiment: Any = None
    dataset: Any = None
    original_database_path = qcodes.config.core.db_location
    try:
        initialise_or_create_database_at(str(database_path), journal_mode="WAL")
        experiment = load_or_create_experiment(
            "stage5c_observed_grid",
            sample_name="missing_cell",
        )
        slow = ManualParameter("stage5c_slow")
        fast = ManualParameter("stage5c_fast")
        signal = ManualParameter("stage5c_signal")
        signal_b = ManualParameter("stage5c_signal_b")
        measurement = Measurement(exp=experiment, name="stage5c_observed_grid")
        measurement.register_parameter(slow)
        measurement.register_parameter(fast)
        measurement.register_parameter(signal, setpoints=(slow, fast))
        measurement.register_parameter(signal_b, setpoints=(slow, fast))
        with measurement.run(write_in_background=False) as datasaver:
            dataset = datasaver.dataset
            for slow_index in range(17):
                for fast_index in range(23):
                    datasaver.add_result(
                        (slow, float(slow_index)),
                        (fast, float(fast_index)),
                        (signal, float(slow_index * 10 + fast_index)),
                        (signal_b, float(1_000 + slow_index * 10 + fast_index)),
                    )
            datasaver.flush_data_to_database(block=True)
        run_id = int(dataset.run_id)
        table_name = str(dataset.table_name)
    finally:
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

    # Synthetic fixture generation is the sole write phase.  Retaining the
    # coordinate row while clearing z models a genuine unmeasured grid cell.
    quoted_table = '"' + table_name.replace('"', '""') + '"'
    connection = apsw.Connection(str(database_path))
    try:
        with connection:
            missing_row = next(
                connection.execute(
                    f'SELECT "id" FROM {quoted_table} '
                    'WHERE "stage5c_slow" = ? AND "stage5c_fast" = ? '
                    'AND "stage5c_signal" IS NOT NULL ORDER BY "id" LIMIT 1',
                    (7.0, 11.0),
                ),
                None,
            )
            assert missing_row is not None and type(missing_row[0]) is int
            missing_row_id = int(missing_row[0])
            connection.execute(
                f'UPDATE {quoted_table} SET "stage5c_signal" = NULL WHERE "id" = ?',
                (missing_row_id,),
            )
    finally:
        connection.close()
    return run_id, table_name, missing_row_id


def _create_independent_qcodes_run(
    database_path: Path, *, include_1d: bool, include_3d: bool
) -> int:
    """Create independent public-QCoDeS dependents with distinct setpoints."""

    experiment: Any = None
    dataset: Any = None
    original_database_path = qcodes.config.core.db_location
    try:
        initialise_or_create_database_at(str(database_path), journal_mode="WAL")
        experiment = load_or_create_experiment(
            "trusted_independent_dimensions", sample_name="independent"
        )
        measurement = Measurement(exp=experiment, name="independent_dimensions")
        axes = tuple(ManualParameter(f"x{index}") for index in range(3))
        signals = tuple(ManualParameter(f"z{index}") for index in range(3))
        if include_1d:
            for axis, signal in zip(axes, signals, strict=True):
                measurement.register_parameter(axis)
                measurement.register_parameter(signal, setpoints=(axis,))
        if include_3d:
            for axis in axes:
                if not include_1d:
                    measurement.register_parameter(axis)
            signal_3d = ManualParameter("z3d")
            measurement.register_parameter(signal_3d, setpoints=axes)
        with measurement.run(write_in_background=False) as datasaver:
            dataset = datasaver.dataset
            for index in range(8):
                if include_1d:
                    for axis, signal in zip(axes, signals, strict=True):
                        datasaver.add_result(
                            (axis, float(index)), (signal, float(index * 10))
                        )
                if include_3d:
                    datasaver.add_result(
                        *((axis, float(index)) for axis in axes),
                        (signal_3d, float(index * 100)),
                    )
            datasaver.flush_data_to_database(block=True)
        return int(dataset.run_id)
    finally:
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


@pytest.mark.parametrize(
    ("include_1d", "include_3d", "expected_dependents"),
    (
        (True, False, {"z0", "z1", "z2"}),
        (True, True, {"z0", "z1", "z2"}),
        (False, True, set()),
    ),
)
def test_real_qcodes_derived_dimensions_are_per_dependent(
    tmp_path: Path,
    include_1d: bool,
    include_3d: bool,
    expected_dependents: set[str],
) -> None:
    database_path = tmp_path / "independent.db"
    run_id = _create_independent_qcodes_run(
        database_path, include_1d=include_1d, include_3d=include_3d
    )
    accepted = database_instance(database_path)
    before = _stable_artifact_state(
        database_path, consecutive_observations=2, observation_interval=0.02
    )
    service = TrustedLiveReadService(
        database_path,
        expected_database_instance=accepted,
        request_timeout_seconds=30.0,
    )
    try:
        bootstrap = service.submit_bootstrap().wait(30.0)
        page = service.submit_basic_page(0, bootstrap.run_id_watermark).wait(30.0)
        assert tuple(run.run_id for run in page.runs) == (run_id,)
        observation = service.submit_derived_source(run_id).wait(30.0)
        assert observation.database_instance == accepted
        assert observation.unsupported_reason is None
        assert set(observation.dependent_parameters) == (
            expected_dependents | ({"z3d"} if include_3d else set())
        )
        for kind in (TrustedWorkKind.THUMBNAIL, TrustedWorkKind.PREVIEW):
            payload = render_trusted_derived_payload(observation, kind)
            assert dict(payload["source"])["run_guid"] == observation.run_guid
            assert dict(payload["source"])["result_watermark"] == (
                observation.result_watermark
            )
            images = {
                dict(image)["dependent"]: dict(image) for image in payload["images"]
            }
            assert set(images) == expected_dependents
            assert all(image["dimensions"] == 1 for image in images.values())
            assert payload["status"] == ("ok" if include_1d else "unsupported")
            if include_3d:
                assert "more than two sweep dimensions" in str(payload["description"])
                assert "z3d" not in images
    finally:
        service.close(timeout=30.0)
    after = _stable_artifact_state(
        database_path, consecutive_observations=2, observation_interval=0.02
    )
    _assert_protected_artifacts_unchanged(before, after)


def test_real_qcodes_observed_grid_infers_and_renders_missing_cell_without_writes(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "stage5c-observed-grid.db"
    run_id, table_name, missing_row_id = _create_completed_rectangular_qcodes_run(
        database_path
    )
    accepted = database_instance(database_path)
    before = _stable_artifact_state(
        database_path,
        consecutive_observations=2,
        observation_interval=0.02,
    )
    service = TrustedLiveReadService(
        database_path,
        expected_database_instance=accepted,
        request_timeout_seconds=30.0,
    )
    try:
        bootstrap = service.submit_bootstrap().wait(30.0)
        page = service.submit_basic_page(0, bootstrap.run_id_watermark).wait(30.0)
        assert page.complete

        fields = service.submit_expensive_run(run_id).wait(30.0).as_dict()
        assert fields["result_count"] == 17 * 23 * 2
        assert fields["setpoint_shape"] == [17, 23]
        assert fields["setpoint_count"] == 17 * 23
        assert fields["point_shape"] == [17, 23, 2]
        assert fields["setpoint_shape_source"] == "observed"

        observation = service.submit_derived_source(run_id).wait(30.0)
        assert observation.result_table_name == table_name
        assert observation.planned_shape == (17, 23)
        assert len(observation.validated_2d_layouts) == 2
        assert tuple(
            layout.dependent for layout in observation.validated_2d_layouts
        ) == ("stage5c_signal", "stage5c_signal_b")
        for layout in observation.validated_2d_layouts:
            assert layout.dependencies == ("stage5c_slow", "stage5c_fast")
            assert layout.shape == (17, 23)
            assert layout.fast_axis_index == 1
            assert layout.first_row_id in (1, 2)
            assert layout.fast_id_stride == 2
            assert layout.slow_id_stride == 46
            assert layout.complete
        missing = next(
            row for row in observation.sample_rows if row[0] == missing_row_id
        )
        signal_index = observation.sample_columns.index("stage5c_signal")
        signal_b_index = observation.sample_columns.index("stage5c_signal_b")
        assert missing[signal_index] is None
        assert missing[signal_b_index] is None

        payload = render_trusted_derived_payload(
            observation,
            TrustedWorkKind.PREVIEW,
        )
        assert payload["status"] == "ok"
        images = {dict(image)["dependent"]: dict(image) for image in payload["images"]}
        assert set(images) == {"stage5c_signal", "stage5c_signal_b"}
        assert images["stage5c_signal"]["sampled_points"] == (17 * 23) - 1
        assert images["stage5c_signal_b"]["sampled_points"] == 17 * 23

        signal_layout = next(
            layout
            for layout in observation.validated_2d_layouts
            if layout.dependent == "stage5c_signal"
        )
        slow_index, row_remainder = divmod(
            missing_row_id - signal_layout.first_row_id,
            signal_layout.slow_id_stride,
        )
        fast_index = row_remainder // signal_layout.fast_id_stride
        assert (slow_index, fast_index) == (7, 11)
        first_width, first_height, first_rgba = _decode_png_rgba(
            images["stage5c_signal"]["bytes"]
        )
        second_width, second_height, second_rgba = _decode_png_rgba(
            images["stage5c_signal_b"]["bytes"]
        )
        assert (first_width, first_height) == (second_width, second_height)
        missing_x = (2 * fast_index + 1) * first_width // (2 * 23)
        display_slow_index = 17 - 1 - slow_index
        missing_y = (2 * display_slow_index + 1) * first_height // (2 * 17)
        assert _rgba_at(first_rgba, first_width, missing_x, missing_y) == (
            230,
            230,
            230,
            255,
        )
        assert _rgba_at(second_rgba, second_width, missing_x, missing_y) != (
            230,
            230,
            230,
            255,
        )
    finally:
        service.close(timeout=30.0)

    after = _stable_artifact_state(
        database_path,
        consecutive_observations=2,
        observation_interval=0.02,
    )
    _assert_protected_artifacts_unchanged(before, after)


def test_real_stage5b_backend_publishes_live_prefixes_without_source_writes(
    stage5b_wal_writer: _QcodesWalWriter,
    tmp_path: Path,
) -> None:
    writer = stage5b_wal_writer
    accepted = database_instance(writer.database_path)
    service = TrustedLiveReadService(
        writer.database_path,
        expected_database_instance=accepted,
        request_timeout_seconds=30.0,
    )
    coordinator: TrustedWorkCoordinator | None = None
    publications = []
    try:
        discovery_started = time.monotonic()
        bootstrap = service.submit_bootstrap().wait(30.0)
        page = service.submit_basic_page(0, bootstrap.run_id_watermark).wait(30.0)
        discovery_elapsed = time.monotonic() - discovery_started
        assert page.complete and len(page.runs) == 1
        assert discovery_elapsed < 15.0

        run = page.runs[0]
        initial_revision = trusted_source_revision(
            run,
            bootstrap.data_version,
            namespace=service.source_revision_namespace,
            helper_incarnation=bootstrap.helper_incarnation,
        )
        before = _stable_artifact_state(
            writer.database_path,
            consecutive_observations=2,
            observation_interval=0.02,
        )
        cache = TrustedDerivedDiskCache(tmp_path / "cache")
        coordinator = TrustedWorkCoordinator(
            service.database_instance,
            (
                TrustedDerivedRun(
                    run.run_id, str(run.as_dict()["guid"]), initial_revision
                ),
            ),
            service,
            cache=cache,
            on_publish=publications.append,
        )
        assert cache.enabled
        coordinator.select_run(0)
        coordinator.set_visible_range(0, 1)
        coordinator.start()
        _drain(coordinator)

        after = _stable_artifact_state(
            writer.database_path,
            consecutive_observations=2,
            observation_interval=0.02,
        )
        _assert_protected_artifacts_unchanged(before, after)
        assert [item.key.kind for item in publications[:3]] == list(TrustedWorkKind)
        assert _source_fields(publications[0])["result_watermark"] == 1_000
        metadata = dict(publications[0].result["metadata"])
        run_fields = dict(metadata["run_fields"])
        assert run_fields["run_id"] == run.run_id
        assert run_fields["guid"] == run.as_dict()["guid"]
        assert run_fields["name"] == "trusted_live_run"
        assert run_fields["result_count"] == 1_000
        assert not bool(run_fields["is_completed"])

        captured = service.submit_derived_source(run.run_id).wait(30.0)
        sample_ids = tuple(row[0] for row in captured.sample_rows)
        assert sample_ids[0] == 1
        assert sample_ids[-1] == 1_000
        assert any(250 < row_id < 750 for row_id in sample_ids)
        expensive = service.submit_expensive_run(run.run_id).wait(30.0).as_dict()
        derived_fields = dict(captured.run_fields)
        for field in (
            "point_shape",
            "setpoint_shape",
            "setpoint_count",
            "read_setpoint_count",
        ):
            assert field in derived_fields
            derived_value = derived_fields[field]
            expensive_value = expensive[field]
            if isinstance(expensive_value, list):
                expensive_value = tuple(expensive_value)
            assert derived_value == expensive_value
        selected = service.submit_selected_run(run.run_id).wait(30.0)
        assert captured.setpoint_summaries == selected.setpoint_summaries
        assert captured.setpoint_summaries

        writer_errors: list[BaseException] = []

        def commit_continuously() -> None:
            try:
                for index in range(12):
                    writer.request("commit", value=f"stage5b-live-{index}")
            except BaseException as error:
                writer_errors.append(error)

        commit_thread = threading.Thread(target=commit_continuously)
        commit_thread.start()
        coordinator.source_changed(0)
        _drain(coordinator)
        commit_thread.join(30.0)
        assert not commit_thread.is_alive()
        assert writer_errors == []
        coordinator.source_changed(0)
        _drain(coordinator)

        current = [
            item
            for item in publications
            if _source_fields(item).get("result_watermark") == 1_012
        ]
        assert {item.key.kind for item in current} == set(TrustedWorkKind)
        assert all(item.result["status"] in {"ok", "unsupported"} for item in current)
        checkpoint = writer.request("checkpoint", mode="PASSIVE")
        assert checkpoint[0] == 0
        assert writer.request("commit", value="after-passive") == 1_012
        assert writer.request("checkpoint", mode="TRUNCATE") == (0, 0, 0)
        assert writer.request("commit", value="after-truncate") == 1_013

        coordinator.source_changed(0)
        queued_deadline = time.monotonic() + 30.0
        while coordinator._completions.empty():  # type: ignore[attr-defined]
            assert time.monotonic() < queued_deadline
            time.sleep(0.005)
        supervisor = service._required_supervisor()  # type: ignore[attr-defined]
        prior_incarnation = supervisor.incarnation
        supervisor.restart()
        assert supervisor.incarnation > prior_incarnation
        publication_count = len(publications)
        coordinator.helper_restarted()
        coordinator.poll()
        _drain(coordinator)
        replacement = publications[publication_count:]
        assert replacement
        assert all(
            dict(item.result["source"])["helper_incarnation"] == supervisor.incarnation
            for item in replacement
        )

        assert not tuple(writer.database_path.parent.rglob("*.qdc"))
        assert tuple(cache.root.glob("*.qdc"))
    finally:
        if coordinator is not None:
            coordinator.close(timeout=30.0)
        service.close(timeout=30.0)

    assert not service.liveness().helper_alive
    assert writer.request("commit", value="after-stage5b-close") == 1_014
    assert writer.request("checkpoint", mode="TRUNCATE") == (0, 0, 0)


def test_real_stage5b_metadata_refreshes_run_completion_without_pre_enrichment(
    stage5b_wal_writer: _QcodesWalWriter,
) -> None:
    writer = stage5b_wal_writer
    service = TrustedLiveReadService(
        writer.database_path,
        expected_database_instance=database_instance(writer.database_path),
        request_timeout_seconds=30.0,
    )
    try:
        bootstrap = service.submit_bootstrap().wait(30.0)
        page = service.submit_basic_page(0, bootstrap.run_id_watermark).wait(30.0)
        run = page.runs[0]
        before = service.submit_derived_source(run.run_id).wait(30.0)
        before_fields = dict(before.run_fields)
        assert not bool(before_fields["is_completed"])
        assert before_fields.get("completed_timestamp") is None

        assert writer.request("complete_run") is True
        after = service.submit_derived_source(run.run_id).wait(30.0)
        after_fields = dict(after.run_fields)

        assert bool(after_fields["is_completed"])
        assert isinstance(after_fields["completed_timestamp"], float)
    finally:
        service.close(timeout=30.0)


def test_real_queued_completion_is_inert_after_database_switch_to_no_runs(
    stage5b_wal_writer: _QcodesWalWriter,
    tmp_path: Path,
) -> None:
    first_writer = stage5b_wal_writer
    second_directory = tmp_path / "second-database"
    second_directory.mkdir()
    second_writer = _QcodesWalWriter.start(second_directory / "second.db")
    first_service = TrustedLiveReadService(
        first_writer.database_path,
        expected_database_instance=database_instance(first_writer.database_path),
        request_timeout_seconds=30.0,
    )
    second_service = TrustedLiveReadService(
        second_writer.database_path,
        expected_database_instance=database_instance(second_writer.database_path),
        request_timeout_seconds=30.0,
    )
    coordinator: TrustedWorkCoordinator | None = None
    publications = []
    try:
        bootstrap = first_service.submit_bootstrap().wait(30.0)
        page = first_service.submit_basic_page(0, bootstrap.run_id_watermark).wait(30.0)
        run = page.runs[0]
        initial_revision = trusted_source_revision(
            run,
            bootstrap.data_version,
            namespace=first_service.source_revision_namespace,
            helper_incarnation=bootstrap.helper_incarnation,
        )
        queued = threading.Event()
        coordinator = TrustedWorkCoordinator(
            first_service.database_instance,
            (
                TrustedDerivedRun(
                    run.run_id,
                    str(run.as_dict()["guid"]),
                    initial_revision,
                ),
            ),
            first_service,
            cache=TrustedDerivedDiskCache(tmp_path / "switch-cache"),
            wakeup=queued.set,
            on_publish=publications.append,
        )
        coordinator.start()
        assert queued.wait(30.0)
        coordinator.switch_database(
            second_service.database_instance,
            (),
            second_service,
        )

        assert coordinator.poll() == 1
        assert coordinator.snapshot().run_count == 0
        assert publications == []
    finally:
        if coordinator is not None:
            coordinator.close(timeout=30.0)
        first_service.close(timeout=30.0)
        second_service.close(timeout=30.0)
        second_writer.close()
