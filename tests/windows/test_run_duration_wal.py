"""Acquisition duration through the real pinned WAL reader and Qt RunList."""

import os
import time
from contextlib import closing
from pathlib import Path

import pytest
from qcodes.dataset import Measurement, load_or_create_experiment
from qcodes.dataset.sqlite.connection import atomic
from qcodes.parameters import ManualParameter

from qplot.datahandling.readonly import sqlite_read_only_connection
from qplot.datahandling.readSQL import (
    _database_modified_timestamp,
    get_snapshot_selected_run_detail,
)
from qplot.datahandling.trusted_live_service import TrustedLiveReadService
from qplot.windows._trusted_derived_qt import TrustedDerivedQtBridge
from qplot.windows._widgets.treeWidgets import RunList, moreInfo, time_taken_seconds
from tests.datahandling.test_trusted_live import (
    _assert_protected_artifacts_unchanged,
    _stable_artifact_state,
)
from tests.windows._trusted_derived_wal_ui import _prepare_live_database

pytestmark = pytest.mark.timeout(120)


def test_real_wal_duration_advances_in_run_list_and_overview(tmp_path):
    path = tmp_path / "duration.db"
    writer = _prepare_live_database(path, "duration")
    experiment = load_or_create_experiment("duration", "sample", conn=writer)
    parameter = ManualParameter("duration_signal")
    measurement = Measurement(exp=experiment, name="duration_run")
    measurement.register_parameter(parameter)
    measurement.write_period = 3600
    run_list = RunList()
    overview = moreInfo()
    service = None
    try:
        with measurement.run(write_in_background=False) as datasaver:
            dataset = datasaver.dataset
            datasaver.add_result((parameter, 1.0))
            datasaver.flush_data_to_database(block=True)
            main_mtime = path.stat().st_mtime_ns
            service = TrustedLiveReadService(path, request_timeout_seconds=30)
            bootstrap = service.submit_bootstrap().wait(30)
            page = service.submit_basic_page(0, bootstrap.run_id_watermark).wait(30)
            run_list.addRuns({record.run_id: record.as_dict() for record in page.runs})
            item = run_list._item_for_guid(str(dataset.guid))
            assert item is not None
            duration_column = run_list.cols.index("Duration")
            initial_duration = time_taken_seconds(item.run_metadata)
            initial_text = item.text(duration_column)
            assert initial_duration is not None

            # Genuine commits about 1.2 s apart, without writer checkpoints.
            time.sleep(1.2)
            datasaver.add_result((parameter, 2.0))
            datasaver.flush_data_to_database(block=True)
            assert path.stat().st_mtime_ns == main_mtime
            protected = _stable_artifact_state(path)
            refresh = service.submit_refresh().wait(30)
            assert refresh.data_version_changed
            fresh = service.submit_cheap_run(dataset.run_id).wait(30)
            run_list.updateRuns({fresh.run_id: fresh.as_dict()})
            seconds = time_taken_seconds(item.run_metadata)
            assert seconds is not None and seconds >= initial_duration + 1.1
            assert max(0, path.stat().st_mtime - item.run_metadata["run_timestamp"]) == 0
            assert item.text(duration_column) != initial_text
            assert item.text(duration_column) == f"{seconds:,.1f} s"
            stale_detail = dict(item.run_metadata, database_modified_timestamp=100.0)
            merged = TrustedDerivedQtBridge._preserve_newer_live_facts(
                item.run_metadata, stale_detail
            )
            run_list.updateRuns({dataset.run_id: merged})
            assert time_taken_seconds(item.run_metadata) == seconds
            assert overview._time_taken_from_metadata(item.run_metadata).startswith(
                f"{seconds:.2f} s\t"
            )

            # Re-paging and selected detail refresh must also refresh activity.
            repaged = service.submit_basic_page(0, bootstrap.run_id_watermark).wait(30)
            assert repaged.runs[-1].as_dict()["database_modified_timestamp"] == (
                fresh.as_dict()["database_modified_timestamp"]
            )
            detail = service.submit_selected_run(dataset.run_id).wait(30)
            assert time_taken_seconds(detail.run.as_dict()) == seconds

            # The private copy is newly created; its mtime must never be used.
            with closing(sqlite_read_only_connection(path)) as snapshot:
                cursor = snapshot.cursor()
                copied_path = Path(cursor.execute("PRAGMA database_list").fetchone()[2])
                assert copied_path != path
                os.utime(copied_path, (time.time() + 1000, time.time() + 1000))
                assert _database_modified_timestamp(cursor) == (
                    fresh.as_dict()["database_modified_timestamp"]
                )
            snapshot_detail = get_snapshot_selected_run_detail(
                path, dataset.run_id, str(dataset.guid), run_metadata=item.run_metadata
            )
            assert time_taken_seconds(snapshot_detail.run.as_dict()) == seconds
            _assert_protected_artifacts_unchanged(protected, _stable_artifact_state(path))

        # QCoDeS's recorded completion wins over all source activity estimates.
        completed = service.submit_cheap_run(dataset.run_id).wait(30)
        run_list.updateRuns({completed.run_id: completed.as_dict()})
        metadata = item.run_metadata
        expected = metadata["completed_timestamp"] - metadata["run_timestamp"]
        assert time_taken_seconds(metadata) == expected
        assert item.text(duration_column) == f"{expected:,.1f} s"
        assert overview._time_taken_from_metadata(metadata).startswith(f"{expected:.2f} s")
        assert path.stat().st_mtime_ns == main_mtime
        with atomic(writer):
            writer.execute('UPDATE runs SET name=? WHERE run_id=?',
                           ("later metadata commit", dataset.run_id))
        completed = service.submit_cheap_run(dataset.run_id).wait(30)
        run_list.updateRuns({completed.run_id: completed.as_dict()})
        assert time_taken_seconds(item.run_metadata) == expected
        assert item.text(duration_column) == f"{expected:,.1f} s"
    finally:
        if service is not None:
            service.close()
        run_list.close()
        overview.close()
        writer.close()


@pytest.mark.parametrize("exception", [None, "KeyboardInterrupt"])
@pytest.mark.parametrize("complete", [False, True])
def test_recorded_completion_is_authoritative_even_if_flag_is_missing(complete, exception):
    metadata = {
        "run_timestamp": 100.0,
        "completed_timestamp": 101.22,
        "is_completed": complete,
        "measurement_exception": exception,
        "database_modified_timestamp": 1000.0,
    }
    assert time_taken_seconds(metadata) == pytest.approx(1.22)


@pytest.mark.parametrize("complete", [False, True, None])
def test_missing_duration_evidence_does_not_use_viewing_time(complete):
    assert time_taken_seconds({"run_timestamp": 100.0, "is_completed": complete}) is None


@pytest.mark.parametrize("exception", [None, "KeyboardInterrupt"])
def test_historical_unfinished_duration_does_not_grow_when_viewed(tmp_path, exception):
    path = tmp_path / "historical.db"
    writer = _prepare_live_database(path, "historical")
    # Turn the synthetic seed into an abandoned/interrupted historical run.
    started = time.time() - 86400
    with atomic(writer):
        writer.execute('ALTER TABLE runs ADD COLUMN measurement_exception TEXT')
        writer.execute(
            'UPDATE runs SET run_timestamp=?, completed_timestamp=NULL, '
            'is_completed=0, measurement_exception=? WHERE run_id=1',
            (started, exception),
        )
    stopped = started + 1.22
    os.utime(path, (started, started))
    os.utime(f"{path}-wal", (stopped, stopped))
    run_list = RunList()
    overview = moreInfo()
    service = TrustedLiveReadService(path, request_timeout_seconds=30)
    try:
        protected = _stable_artifact_state(path)
        bootstrap = service.submit_bootstrap().wait(30)
        page = service.submit_basic_page(0, bootstrap.run_id_watermark).wait(30)
        run_list.addRuns({record.run_id: record.as_dict() for record in page.runs})
        item = run_list._item_for_guid(page.runs[0].as_dict()["guid"])
        assert item is not None
        for _ in range(2):
            fresh = service.submit_cheap_run(1).wait(30)
            run_list.updateRuns({1: fresh.as_dict()})
            assert time_taken_seconds(item.run_metadata) == pytest.approx(1.22, abs=0.001)
            assert item.text(run_list.cols.index("Duration")) == "1.2 s"
            assert overview._time_taken_from_metadata(item.run_metadata).startswith("1.22 s")
            detail = get_snapshot_selected_run_detail(path, 1, item.run_metadata["guid"])
            assert time_taken_seconds(detail.run.as_dict()) == pytest.approx(1.22, abs=0.001)
        _assert_protected_artifacts_unchanged(protected, _stable_artifact_state(path))
    finally:
        service.close()
        run_list.close()
        overview.close()
        writer.close()
