"""Current metadata observations must retire obsolete omission warnings."""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.datahandling.trusted_live import TRUSTED_LIVE_MAX_SCALAR_BYTES
from qplot.datahandling.trusted_live_service import TrustedLiveReadService
from qplot.datahandling.trusted_snapshot import TRUSTED_SNAPSHOT_MAX_INPUT_BYTES
from tests.datahandling.test_trusted_live import (
    _artifact_state,
    _assert_protected_artifacts_unchanged,
)
from tests.datahandling.test_trusted_live_queries import _adapter, _load_basic_pages


@contextmanager
def _live_run(database):
    initialise_or_create_database_at(database, journal_mode="WAL")
    experiment = load_or_create_experiment("omission recovery", "current metadata")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("x")
    measurement.register_custom_parameter("y", setpoints=("x",))
    dataset = None
    service = None
    try:
        with measurement.run(write_in_background=True) as saver:
            dataset = saver.dataset
            saver.add_result(("x", 1.0), ("y", 2.0))
            saver.flush_data_to_database(block=True)
            service = TrustedLiveReadService(database)
            yield dataset, experiment, service
    finally:
        if service is not None:
            service.close(timeout=15)
            state = service.liveness()
            assert state.closed and not state.helper_alive and not state.receiver_alive
            assert not state.dispatcher_alive and not state.control_alive
            assert state.open_supervisor_endpoints == state.outstanding_requests == 0
            assert not state.resource_cleanup_pending
        if dataset is not None:
            dataset.conn.close()
        experiment.conn.close()


@pytest.mark.parametrize(
    "fields",
    [
        ("operator_note",), ("snapshot",), ("name",),
        ("snapshot", "operator_note", "large_note"),
    ],
    ids=["metadata", "snapshot", "cheap-field", "partial-recovery"],
)
def test_live_metadata_recovery_removes_only_obsolete_raw_omissions(tmp_path, fields):
    database = tmp_path / "recovery.db"
    with _live_run(database) as (dataset, experiment, service):
        for name in fields:
            value = (
                json.dumps({"station": {"note": "s" * TRUSTED_SNAPSHOT_MAX_INPUT_BYTES}})
                if name == "snapshot"
                else "m" * (TRUSTED_LIVE_MAX_SCALAR_BYTES + 1)
            )
            dataset.add_metadata(name, value)
        before = _artifact_state(database)
        assert service.submit_bootstrap().wait(20).run_id_watermark == 1
        service.submit_basic_page(0, 1).wait(20)
        original = service.submit_selected_run(1).wait(20)
        helper_pid = service.liveness().helper_pid
        assert set(original.unavailable_fields) == set(fields)
        assert original.presentation.raw.status == "truncated"
        _assert_protected_artifacts_unchanged(before, _artifact_state(database))

        remaining = set(fields)
        for name in fields:
            value = (
                json.dumps({"station": {"parameters": {"voltage": {"value": 3.25}}}})
                if name == "snapshot"
                else f"recovered {name}"
            )
            dataset.add_metadata(name, value)
            before = _artifact_state(database)
            assert service.submit_refresh(1).wait(20).data_version_changed
            cheap = service.submit_cheap_run(1).wait(20)
            if name == "name":
                remaining.remove(name)
                assert cheap.as_dict()[name] == value
            # Cheap refresh did not fetch dynamic metadata or the snapshot.
            assert set(cheap.unavailable_fields) == remaining
            detail = service.submit_selected_run(1).wait(20)
            remaining.discard(name)
            if name == "snapshot":
                assert detail.snapshot.status == "available"
            elif name == "name":
                assert detail.run.as_dict()[name] == value
            else:
                assert dict(detail.metadata)[name] == value
            assert set(detail.unavailable_fields) == remaining
            assert detail.run.unavailable_fields == detail.unavailable_fields
            assert detail.presentation.raw.status == (
                "truncated" if remaining else "available"
            )
            assert ("source data was omitted" in detail.presentation.raw.message) == bool(
                remaining
            )
            assert service.submit_expensive_run(1).wait(20).as_dict()["result_count"] == 1
            assert service.liveness().helper_pid == helper_pid
            assert service.fatal_error is None
            _assert_protected_artifacts_unchanged(before, _artifact_state(database))
            assert experiment.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (
                0, 0, 0,
            )


@pytest.mark.parametrize("boundary", ["columns", "layouts"])
@pytest.mark.parametrize("newly_omitted", [True, False], ids=["omitted", "recovered"])
def test_selected_preserves_newer_nested_cheap_observation(
    tmp_path, boundary, newly_omitted,
):
    adapter, executor = _adapter(tmp_path, (1,))
    oversized = "n" * (TRUSTED_LIVE_MAX_SCALAR_BYTES + 1)
    executor.runs[1]["name"] = "original" if newly_omitted else oversized
    # Force a separate guarded group after the standard fields have loaded.
    executor.runs[1]["operator"] = "v" * 200_000
    bootstrap = adapter.bootstrap()
    _load_basic_pages(adapter, 0, bootstrap.run_id_watermark)
    original_query = executor.query
    nested = []
    marker = 'THEN "operator"' if boundary == "columns" else 'MAX("layout_id")'

    def query_with_newer_omission(sql, bindings=None, **kwargs):
        if marker in sql and not nested:
            executor.runs[1]["name"] = oversized if newly_omitted else "recovered"
            nested.append(adapter.cheap_run(1))
        return original_query(sql, bindings, **kwargs)

    executor.query = query_with_newer_omission

    selected = adapter.selected_run_detail(1)

    assert len(nested) == 1
    assert ("name" in nested[0].unavailable_fields) == newly_omitted
    assert ("name" in selected.unavailable_fields) == newly_omitted
    assert selected.run.as_dict()["name"] == (None if newly_omitted else "recovered")
    assert selected.presentation.raw.status == (
        "truncated" if newly_omitted else "available"
    )
