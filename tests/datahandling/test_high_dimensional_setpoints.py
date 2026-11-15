"""High-dimensional setpoint counts reach the run table and selected info."""

import itertools
from pathlib import Path

import numpy as np
import pytest

from qplot.datahandling import _trusted_result_layout as layout_queries
from qplot.datahandling import trusted_live_queries as queries
from qplot.datahandling.file_identity import database_instance
from tests.support.datahandling.high_dimensional_setpoints import (
    _Axis as _Axis,
)
from tests.support.datahandling.high_dimensional_setpoints import (
    _database as _database,
)
from tests.support.datahandling.high_dimensional_setpoints import (
    _load_basic_runs as _load_basic_runs,
)
from tests.support.datahandling.high_dimensional_setpoints import (
    _ReadOnlySqliteExecutor as _ReadOnlySqliteExecutor,
)


def _protected_artifact_state(path):
    return {suffix: (p.stat().st_ino, p.stat().st_mtime_ns, p.read_bytes())
            for suffix in ("", "-wal", "-journal")
            if (p := Path(str(path) + suffix)).exists()}


@pytest.mark.parametrize("dimensions,mixed,descending,planned", [
    (3, False, False, False), (4, False, False, False), (7, False, False, False),
    (3, True, False, False), (4, True, False, False), (4, True, True, False),
    (4, False, False, True), (4, True, False, True),
])
def test_nd_counts_and_axis_steps_in_run_metadata_and_detail(
    tmp_path, monkeypatch, dimensions, mixed, descending, planned,
):
    path, axes = _database(tmp_path, dimensions, mixed, descending, planned)
    before = _protected_artifact_state(path)
    executor = _ReadOnlySqliteExecutor(path)
    try:
        adapter = queries.TrustedMetadataQueryAdapter(executor, path)
        _load_basic_runs(adapter)
        # Force more than one transaction even for the small mixed fixture.
        monkeypatch.setattr(layout_queries, "_TRUSTED_PROGRESSIVE_LAYOUT_PAGE_ROWS",
                            17 if dimensions <= 4 else 4096)
        while True:
            fields = adapter.expensive_run(1).as_dict()
            state = adapter._layouts.nd_state(1)
            if state.after_row_id == state.result_watermark:
                break
            assert fields["read_setpoint_count"] is None
        sizes = [a.points for a in axes]
        count = int(np.prod(sizes))
        assert fields["setpoint_count"] == fields["read_setpoint_count"] == count
        assert fields["setpoint_shape"] == sizes
        detail = adapter.selected_run_detail(1)
        assert detail.run.as_dict()["read_setpoint_count"] == count
        assert {s.name: s.steps for s in detail.setpoint_summaries} == {
            a.name: a.points for a in axes
        }
        assert all("COUNT(DISTINCT" not in sql.upper() for sql in executor.queries)
    finally:
        executor.close()
    assert _protected_artifact_state(path) == before


def test_nd_count_progress_is_scheduled_until_complete(tmp_path, monkeypatch):
    path, axes = _database(tmp_path, 4, mixed=True)
    monkeypatch.setattr(layout_queries, "_TRUSTED_PROGRESSIVE_LAYOUT_PAGE_ROWS", 17)
    executor = _ReadOnlySqliteExecutor(path)
    try:
        adapter = queries.TrustedMetadataQueryAdapter(executor, path)
        _load_basic_runs(adapter)
        namespace = queries.TrustedSourceRevisionNamespace.create()
        cursors = []
        for _ in range(30):
            start = len(executor.queries)
            observation = adapter.derived_source_observation(
                1, database_instance=database_instance(path), namespace=namespace,
            )
            pages = [sql for sql in executor.queries[start:]
                     if sql.startswith('SELECT "id", CASE WHEN')
                     and sql.endswith('ORDER BY "id" LIMIT ?')]
            assert len(pages) == 1  # Count and preview proofs share one scan.
            if not observation.progressive_layout_pending:
                break
            cursors.append(observation.progressive_layout_cursor)
        else:
            pytest.fail("The nD setpoint scan did not finish")
        assert cursors and cursors == sorted(set(cursors))
        assert dict(observation.run_fields)["read_setpoint_count"] == np.prod(
            [a.points for a in axes]
        )
    finally:
        executor.close()


@pytest.mark.parametrize("mutation", ["duplicate", "wrong-cycle", "nonmonotonic"])
def test_nd_verifier_rejects_unproven_layouts(mutation):
    values = list(itertools.product(range(2), range(3), range(4)))
    if mutation == "duplicate":
        values.insert(15, values[14])
    elif mutation == "wrong-cycle":
        values[18] = (1, 1, 2.5)
    else:
        values[2], values[3] = values[3], values[2]
    state = layout_queries._NDDependentCount(
        ("x", "y", "z"), "signal", tuple(layout_queries._NDCountAxis() for _ in range(3)),
    )
    for row in values:
        if state.viable:
            state = layout_queries.ResultLayoutQueries._consume_nd_count(state, row)
    assert not state.viable


def test_cancelled_nd_page_keeps_last_accepted_cursor(tmp_path, monkeypatch):
    path, _axes = _database(tmp_path, 4)
    monkeypatch.setattr(layout_queries, "_TRUSTED_PROGRESSIVE_LAYOUT_PAGE_ROWS", 17)
    executor = _ReadOnlySqliteExecutor(path)
    try:
        adapter = queries.TrustedMetadataQueryAdapter(executor, path)
        _load_basic_runs(adapter)
        adapter.expensive_run(1)
        accepted = adapter._layouts.nd_state(1)
        query = executor.query

        def cancel(sql, *args, **kwargs):
            if 'AS "qplot_axis_3"' in sql:
                raise RuntimeError("cancelled nD page")
            return query(sql, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(executor, "query", cancel)
            with pytest.raises(RuntimeError, match="cancelled nD page"):
                adapter.expensive_run(1)
        assert adapter._layouts.nd_state(1) is accepted
        for _ in range(10):
            fields = adapter.expensive_run(1).as_dict()
            if fields["read_setpoint_count"] is not None:
                break
        assert fields["read_setpoint_count"] == 120
    finally:
        executor.close()


@pytest.mark.parametrize("planned", [False, True])
def test_partial_nd_grid_has_an_acquired_count_without_inventing_shape(tmp_path, planned):
    path, _axes = _database(tmp_path, 4, planned=planned, acquired=17)
    executor = _ReadOnlySqliteExecutor(path)
    try:
        adapter = queries.TrustedMetadataQueryAdapter(executor, path)
        _load_basic_runs(adapter)
        fields = adapter.expensive_run(1).as_dict()
        assert fields["read_setpoint_count"] == 17
        assert fields["setpoint_count"] == (120 if planned else 17)
        assert fields["setpoint_shape"] == ([2, 3, 4, 5] if planned else None)
    finally:
        executor.close()
