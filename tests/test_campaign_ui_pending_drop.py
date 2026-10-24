"""Deferred preview drops belong to the plot window that received them."""

import pytest
from PyQt6 import QtCore, sip
from PyQt6 import QtWidgets as qtw

from tests.windows.test_mixed_plot_drops import (
    _added,
    _drop,
    _wait_for,
    mixed_plot,  # noqa: F401
)


@pytest.mark.parametrize("source_name", ["other_line", "other_heatmap"])
@pytest.mark.parametrize("close_action", ["window", "all"])
def test_closed_target_discards_pending_drop(mixed_plot, monkeypatch, source_name, close_action):  # noqa: F811
    main, target, runs = mixed_plot
    monkeypatch.setattr(qtw.QMessageBox, "exec", lambda box: qtw.QMessageBox.StandardButton.Ok)
    assert main._database_access_mode == "trusted_live"
    original_items = list(target._plot_item_order)
    source_key = main._current_dataset_key(runs[source_name].guid)
    direct_children = QtCore.Qt.FindChildOption.FindDirectChildrenOnly
    timers_before = set(main.findChildren(QtCore.QTimer, options=direct_children))
    _drop(target, runs[source_name])
    timer, = set(main.findChildren(QtCore.QTimer, options=direct_children)) - timers_before
    request, = main._trusted_plot_pending
    cancellations = []
    cancel = type(request).cancel

    def record_cancel(cancelled_request):
        if cancelled_request is request:
            cancellations.append(request)
        return cancel(cancelled_request)

    monkeypatch.setattr(type(request), "cancel", record_cancel)

    if close_action == "all":
        main.closeAllPlotsAction.trigger()
    else:
        target.close()
    assert target not in main.windows
    # Close All cancels immediately; the deferred poll may cancel again before
    # retiring. Wait for that callback before checking that nothing published.
    _wait_for(lambda: not main._trusted_plot_pending
              and (sip.isdeleted(timer) or not timer.isActive()))
    qtw.QApplication.processEvents()

    assert cancellations
    assert request.done
    assert target._plot_item_order == original_items
    assert source_key not in main.dataset_holder
    assert all(handle.users == 0 for handle in main.dataset_holder.values())
    assert not main.windows
    assert not main.__dict__.get("_trusted_plot_ready")


@pytest.mark.parametrize("source_name", ["other_line", "other_heatmap"])
def test_completed_unpublished_drop_is_discarded_after_target_closes(mixed_plot, monkeypatch, source_name):  # noqa: F811
    main, target, runs = mixed_plot
    monkeypatch.setattr(qtw.QMessageBox, "exec", lambda box: qtw.QMessageBox.StandardButton.Ok)
    original_items = list(target._plot_item_order)
    source_key = main._current_dataset_key(runs[source_name].guid)
    _drop(target, runs[source_name])
    request, = main._trusted_plot_pending
    # Let the real broker finish before the GUI handles its next timer tick.
    # This is also reachable when Qt is busy while the backend finishes.
    request.wait(10)
    assert request.done
    target.close()
    _wait_for(lambda: not main._trusted_plot_pending)
    assert target._plot_item_order == original_items
    assert source_key not in main.dataset_holder
    assert not main.__dict__.get("_trusted_plot_ready")


@pytest.mark.parametrize("source_name", ["other_line", "other_heatmap"])
def test_other_target_keeps_its_pending_drop(mixed_plot, monkeypatch, source_name):  # noqa: F811
    main, target, runs = mixed_plot
    monkeypatch.setattr(qtw.QMessageBox, "exec", lambda box: qtw.QMessageBox.StandardButton.Ok)
    main.openPlot(guid=runs["third_line"].guid)
    _wait_for(lambda: len(main.windows) == 2)
    other = main.windows[-1]
    _wait_for(lambda: not other.worker.running)
    original_items = list(target._plot_item_order)
    source_key = main._current_dataset_key(runs[source_name].guid)
    _drop(target, runs[source_name])
    _drop(other, runs[source_name])
    assert len(main._trusted_plot_pending) == 2
    target.close()
    layer = _added(other, runs[source_name])
    _wait_for(lambda: not main._trusted_plot_pending)

    assert target._plot_item_order == original_items
    assert main.windows == [other]
    assert layer.from_win._merged_trace_users == 1
    assert main.dataset_holder[source_key].users == 1
    assert not main.__dict__.get("_trusted_plot_ready")


def test_promotion_discards_drop_to_replaced_window(mixed_plot, monkeypatch):  # noqa: F811
    main, target, runs = mixed_plot
    monkeypatch.setattr(qtw.QMessageBox, "exec", lambda box: qtw.QMessageBox.StandardButton.Ok)
    primary = target._plot_item_order[0]
    _drop(target, runs["other_line"])
    _added(target, runs["other_line"])
    _drop(target, runs["other_heatmap"])
    assert main._trusted_plot_pending
    replacement = target.remove_plot_items([primary])
    assert replacement is not target
    qtw.QApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    _wait_for(lambda: not main._trusted_plot_pending
              and not replacement.worker.running and not replacement._refresh_pending)
    assert main.windows == [replacement]
    assert not replacement.__dict__.get("heatmaps")
    assert main._current_dataset_key(runs["other_heatmap"].guid) not in main.dataset_holder
    assert not main.__dict__.get("_trusted_plot_ready")
