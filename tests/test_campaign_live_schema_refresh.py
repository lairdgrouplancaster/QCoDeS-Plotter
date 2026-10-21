"""Persistent readers must observe QCoDeS metadata columns added while live."""

from contextlib import contextmanager

import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.datahandling.trusted_live import TrustedLiveReader
from qplot.datahandling.trusted_live_service import TrustedLiveReadService
from tests.datahandling.test_trusted_live import (
    _artifact_state,
    _assert_protected_artifacts_unchanged,
    _assert_safe_audit,
)


@contextmanager
def _measurement(experiment):
    measurement = Measurement(exp=experiment, name="live schema refresh")
    measurement.register_custom_parameter("x")
    measurement.register_custom_parameter("y", setpoints=("x",))
    dataset = None
    try:
        with measurement.run(write_in_background=True) as saver:
            dataset = saver.dataset
            yield saver
    finally:
        if dataset is not None:
            dataset.conn.close()


@pytest.mark.parametrize("empty", [True, False])
def test_first_native_select_after_qcodes_metadata_change_has_current_columns(
    tmp_path, empty,
):
    database = tmp_path / "schema.db"
    initialise_or_create_database_at(database, journal_mode="WAL")
    experiment = load_or_create_experiment("schema", sample_name="native")
    try:
        with _measurement(experiment) as saver:
            dataset = saver.dataset
            sql = "SELECT * FROM runs" + (" WHERE 0" if empty else "")
            with TrustedLiveReader.open(database) as reader:
                original = reader.query(sql)
                assert "operator_note" not in original.columns
                dataset.add_metadata("operator_note", "added during acquisition")
                before = _artifact_state(database)

                result = reader.query(sql)

                assert result.columns == (*original.columns, "operator_note")
                if empty:
                    assert result.rows == ()
                else:
                    assert len(result.rows) == 1
                    assert result.rows[0][-1] == "added during acquisition"
                _assert_protected_artifacts_unchanged(
                    before, _artifact_state(database),
                )
                assert experiment.conn.execute(
                    "PRAGMA wal_checkpoint(TRUNCATE)"
                ).fetchone() == (0, 0, 0)
                _assert_safe_audit(reader.audit().counters)
            _assert_safe_audit(reader.audit().counters)
            assert reader.audit().counters["proof_active"] == 0
    finally:
        experiment.conn.close()


def test_broker_refresh_discovers_metadata_created_with_new_background_run(tmp_path):
    database = tmp_path / "background.db"
    initialise_or_create_database_at(database, journal_mode="WAL")
    experiment = load_or_create_experiment("schema", sample_name="broker")
    service = TrustedLiveReadService(database)
    try:
        assert service.submit_bootstrap().wait(20).run_id_watermark == 0
        assert service.submit_basic_page(0, 0).wait(10).complete
        helper_pid = service.liveness().helper_pid
        with _measurement(experiment) as saver:
            dataset = saver.dataset
            dataset.add_metadata("operator_note", "before first point")
            before = _artifact_state(database)

            assert service.submit_refresh(0).wait(10).run_id_watermark == 1
            page = service.submit_basic_page(0, 1).wait(10)
            assert [run.run_id for run in page.runs] == [1]
            detail = service.submit_selected_run(1).wait(10)

            assert dict(detail.metadata)["operator_note"] == "before first point"
            _assert_protected_artifacts_unchanged(before, _artifact_state(database))
            saver.add_result(("x", 1.0), ("y", 2.0))
            saver.flush_data_to_database(block=True)
            dataset.add_metadata("acquisition_stage", "after first point")
            before = _artifact_state(database)

            assert service.submit_refresh(1).wait(10).data_version_changed
            detail = service.submit_selected_run(1).wait(10)

            assert dict(detail.metadata) == {
                "operator_note": "before first point",
                "acquisition_stage": "after first point",
            }
            assert service.submit_expensive_run(1).wait(10).as_dict()["result_count"] == 1
            assert service.liveness().helper_pid == helper_pid
            assert service.fatal_error is None
            _assert_protected_artifacts_unchanged(before, _artifact_state(database))
            assert experiment.conn.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone() == (0, 0, 0)
    finally:
        service.close(timeout=15)
        experiment.conn.close()
    state = service.liveness()
    assert state.closed and not state.helper_alive and not state.dispatcher_alive
    assert not state.receiver_alive and not state.control_alive
    assert state.open_supervisor_endpoints == state.outstanding_requests == 0
    assert not state.resource_cleanup_pending
