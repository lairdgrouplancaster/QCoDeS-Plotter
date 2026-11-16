"""Refresh scheduling must yield while another worker owns the plot."""

from types import SimpleNamespace

from PyQt6 import QtCore, QtWidgets, sip

from qplot.windows._plot_refresh import PlotRefreshMixin
from qplot.windows._shutdown_resources import ShutdownResources
from tests.support.plot_refresh import set_plot_worker


class _RefreshHarness(PlotRefreshMixin, QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        set_plot_worker(self, SimpleNamespace(running=False))
        self.refreshes = []
        self.callbacks = 0
        run = self._refresh_controller().run_pending

        def counted():
            self.callbacks += 1
            run()

        self._refresh_controller().run_pending = counted

    def refreshWindow(self, force=False):
        self.refreshes.append(force)


def test_queued_refresh_waits_for_replacement_worker_without_busy_polling(qapplication):
    window = _RefreshHarness()
    window._queue_pending_refresh(force=True)
    # Completion queues a refresh, but another action starts a load before
    # the zero-delay callback runs (for example a viewport heatmap reload).
    window._schedule_pending_refresh()
    set_plot_worker(window, SimpleNamespace(running=True))
    for _ in range(25):
        QtCore.QCoreApplication.processEvents()
    try:
        assert window.callbacks == 0
        assert window._refresh_pending
        assert window.refreshes == []
    finally:
        # A worker completion invokes this scheduler, including after errors.
        window.worker.running = False
        window._schedule_pending_refresh()
        QtCore.QCoreApplication.processEvents()
    assert window.refreshes == [True]
    assert window.callbacks == 1
    assert not window._refresh_pending
    window.deleteLater()


def test_destroyed_window_discards_queued_refresh_timer(qapplication):
    window = _RefreshHarness()
    refreshes = window.refreshes
    window._queue_pending_refresh(force=True)
    window._schedule_pending_refresh()
    sip.delete(window)
    QtCore.QCoreApplication.processEvents()
    assert refreshes == []


def test_shutdown_invalidates_queued_refreshes_for_retained_sources(qapplication):
    visible, hidden, volume = (_RefreshHarness() for _ in range(3))
    visible.lines = {"trace": SimpleNamespace(from_win=hidden)}
    visible.source = volume
    windows = (visible, hidden, volume)
    try:
        for window in windows:
            set_plot_worker(window, None)
            window._queue_pending_refresh(force=True)
            window._schedule_pending_refresh()
        ShutdownResources(SimpleNamespace(windows=[visible])).cancel_plot_work()
        QtCore.QCoreApplication.processEvents()
        for window in windows:
            assert not window._refresh_pending
            assert not window.refreshes
    finally:
        for window in windows:
            sip.delete(window)
