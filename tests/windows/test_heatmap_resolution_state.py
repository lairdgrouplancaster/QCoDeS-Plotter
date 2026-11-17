"""Resolution reports follow committed, failed and superseded publications."""

import pytest

from qplot.tools.heatmap_resolution import HeatmapResolutionReport
from qplot.windows._heatmap_resolution_state import HeatmapResolutionState
from qplot.windows._plot_publication import PlotPublicationOwner


def stage(state, owner, report):
    worker = object()
    record = owner.begin(worker, {}, cache=None, parameter_name="signal")
    state.stage(record, report)
    return record


@pytest.fixture
def accepted():
    state = HeatmapResolutionState()
    owner = PlotPublicationOwner()
    report = HeatmapResolutionReport(sampled=True)
    record = stage(state, owner, report)
    assert owner.commit(record, record.worker)
    assert state.accepted is report
    return state, owner, report


@pytest.mark.parametrize("report", [None, HeatmapResolutionReport(aggregated=True)], ids=["full-resolution", "reduced"])
def test_pending_report_is_published_only_after_commit(accepted, report):
    state, owner, previous = accepted
    record = stage(state, owner, report)
    assert state.accepted is previous
    assert owner.commit(record, record.worker)
    assert state.accepted is report


@pytest.mark.parametrize("outcome", ["render_failed", "rollback"])
def test_cleared_display_has_no_resolution_warning(accepted, outcome):
    state, owner, previous = accepted
    record = stage(state, owner, HeatmapResolutionReport(aggregated=True))
    assert state.accepted is previous
    assert getattr(owner, outcome)(record.worker, record.worker, {})
    assert state.accepted is None


def test_discarded_result_does_not_replace_accepted_report(accepted):
    state, owner, previous = accepted
    record = stage(state, owner, HeatmapResolutionReport(aggregated=True))
    owner.discard(record.worker)
    assert state.accepted is previous


def test_nested_newer_commit_cannot_be_cleared_by_stale_outer_failure(accepted):
    state, owner, previous = accepted
    older = stage(state, owner, HeatmapResolutionReport(aggregated=True))
    newest = HeatmapResolutionReport(interpolated=True)
    newer = stage(state, owner, newest)
    assert state.accepted is previous
    assert owner.commit(newer, newer.worker)
    assert not owner.render_failed(older.worker, newer.worker, {})
    assert state.accepted is newest


def test_reset_retires_staged_and_accepted_reports(accepted):
    state, owner, _ = accepted
    record = stage(state, owner, HeatmapResolutionReport(aggregated=True))
    state.reset()
    assert owner.commit(record, record.worker)
    assert state.accepted is None
