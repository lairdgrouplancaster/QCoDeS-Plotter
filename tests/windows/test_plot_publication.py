"""Publication ownership, exact snapshots and conditional shared-cache rollback."""

from types import SimpleNamespace

import numpy as np
import pytest

from qplot.windows._plot_publication import PlotPublicationOwner, PublicationOutcome


@pytest.fixture
def publication():
    cache = SimpleNamespace(
        _dataset=SimpleNamespace(_completed=False), _read_status={"signal": 1},
        _write_status={"signal": 1}, _data={"signal": object(), "other": object()},
        _qplot_synchronized_parameters=set(),
    )
    state = {"axis_data": np.array([2**63 + 1], dtype=np.uint64), "last_ds_len": 1,
             "_qplot_display_synchronized": True, "cut_metadata": ("accepted",)}
    owner = PlotPublicationOwner()
    worker = object()
    record = owner.begin(worker, state, cache=cache, parameter_name="signal", extra_fields=("cut_metadata",))
    return owner, worker, record, state, cache


def publish(owner, worker, state, cache):
    state.update(axis_data=np.array([99.]), dataGrid=np.array([[99.]]), last_ds_len=2,
                 cut_metadata=("pending",))
    cache._data["signal"] = object()
    cache._read_status["signal"] = 2
    cache._write_status["signal"] = 2
    cache._dataset._completed = True
    cache._qplot_synchronized_parameters.add("signal")
    owner.record_cache(worker)


def test_source_rollback_restores_exact_fields_and_only_owned_cache_slots(publication):
    owner, worker, record, state, cache = publication
    prior = dict(state)
    prior_data = cache._data["signal"]
    unrelated = cache._data["other"]
    publish(owner, worker, state, cache)
    assert owner.rollback(worker, worker, state)
    assert state["axis_data"] is prior["axis_data"]
    assert state["axis_data"][0] == 2**63 + 1
    assert "dataGrid" not in state
    assert state["cut_metadata"] == ("accepted",)
    assert cache._data["signal"] is prior_data
    assert cache._data["other"] is unrelated
    assert cache._read_status == {"signal": 1}
    assert cache._write_status == {"signal": 1}
    assert not cache._dataset._completed
    assert not cache._qplot_synchronized_parameters
    assert record.outcome is PublicationOutcome.ROLLED_BACK
    assert owner.current is None
    assert not owner.rollback(worker, worker, state)


def test_rollback_preserves_a_newer_shared_cache_write(publication):
    owner, worker, record, state, cache = publication
    prior_axis = state["axis_data"]
    publish(owner, worker, state, cache)
    newer_data = cache._data["signal"] = object()
    cache._read_status["signal"] = 3
    # Final bookkeeping after a nested callback must not adopt its cache write.
    owner.record_cache(worker)
    assert owner.rollback(worker, worker, state)
    assert state["axis_data"] is prior_axis
    assert cache._data["signal"] is newer_data
    assert cache._read_status["signal"] == 3
    assert cache._dataset._completed
    assert record.outcome is PublicationOutcome.ROLLED_BACK


def test_render_failure_keeps_valid_cache_and_restores_retryable_plot(publication):
    owner, worker, record, state, cache = publication
    prior_axis = state["axis_data"]
    publish(owner, worker, state, cache)
    loaded = cache._data["signal"]
    assert owner.render_failed(worker, worker, state)
    assert state["axis_data"] is prior_axis
    assert "dataGrid" not in state
    assert not state["_qplot_display_synchronized"]
    assert cache._data["signal"] is loaded
    assert cache._dataset._completed
    assert cache._qplot_synchronized_parameters == {"signal"}
    assert record.outcome is PublicationOutcome.RENDER_FAILED
    assert not owner.commit(record, worker)


@pytest.mark.parametrize("operation", ["commit", "rollback", "render_failed", "discard"])
def test_old_finalisation_cannot_change_a_nested_newer_publication(publication, operation):
    owner, worker, record, state, cache = publication
    publish(owner, worker, state, cache)
    newer = object()
    replacement = owner.begin(newer, state, cache=cache, parameter_name="signal")
    publish(owner, newer, state, cache)
    newest_axis = state["axis_data"]
    newest_cache = cache._data["signal"]
    if operation == "commit":
        assert not owner.commit(record, newer)
    elif operation == "discard":
        owner.discard(worker)
    else:
        assert not getattr(owner, operation)(worker, newer, state)
    assert owner.current is replacement
    assert state["axis_data"] is newest_axis
    assert cache._data["signal"] is newest_cache
    assert record.outcome is PublicationOutcome.DISCARDED


def test_commit_is_terminal_and_cannot_be_repeated_or_rolled_back(publication):
    owner, worker, record, state, cache = publication
    publish(owner, worker, state, cache)
    accepted = state["axis_data"]
    assert owner.commit(record, worker)
    assert record.outcome is PublicationOutcome.COMMITTED
    assert not owner.commit(record, worker)
    assert not owner.rollback(worker, worker, state)
    assert not owner.render_failed(worker, worker, state)
    assert state["axis_data"] is accepted


def test_replacement_job_without_publication_prevents_old_rollback(publication):
    owner, worker, record, state, cache = publication
    publish(owner, worker, state, cache)
    current = state["axis_data"]
    assert not owner.rollback(worker, object(), state)
    assert state["axis_data"] is current
    assert record.outcome is PublicationOutcome.DISCARDED
