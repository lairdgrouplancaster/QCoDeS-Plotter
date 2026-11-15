"""Refresh scheduling must yield while another worker owns the plot."""

from types import SimpleNamespace

from PyQt6 import QtCore

from qplot.windows._plot_refresh import PlotRefreshMixin


class _RefreshHarness(PlotRefreshMixin):
    def __init__(self):
        self.worker = SimpleNamespace(running=False)
        self.refreshes = []
        self.callbacks = 0

    def _run_pending_refresh(self):
        self.callbacks += 1
        super()._run_pending_refresh()

    def refreshWindow(self, force=False):
        self.refreshes.append(force)


def test_queued_refresh_waits_for_replacement_worker_without_busy_polling(qapplication):
    window = _RefreshHarness()
    window._queue_pending_refresh(force=True)
    # Completion queues a refresh, but another action starts a load before
    # the zero-delay callback runs (for example a viewport heatmap reload).
    window._schedule_pending_refresh()
    window.worker = SimpleNamespace(running=True)
    for _ in range(25):
        QtCore.QCoreApplication.processEvents()
    try:
        assert window.callbacks == 1
        assert window._refresh_pending
        assert window.refreshes == []
    finally:
        # A worker completion invokes this scheduler, including after errors.
        window.worker.running = False
        window._schedule_pending_refresh()
        QtCore.QCoreApplication.processEvents()
    assert window.refreshes == [True]
    assert not window._refresh_pending
