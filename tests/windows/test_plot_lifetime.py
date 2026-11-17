"""Closed plots release native widgets while retained sources remain usable."""

import pytest
from PyQt6 import QtCore, QtWidgets, sip

from qplot.windows._source_consumer import SourceConsumerRegistration
from tests.support.windows.plot_integration import wait_for
from tests.support.windows.refresh_interval_precision import live_view


def deleted(plot):
    # These integration tests pump events without QApplication.exec().
    QtWidgets.QApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    return sip.isdeleted(plot)


@pytest.mark.parametrize("heatmap", [False, True])
def test_closed_plot_releases_native_widgets(tmp_path, monkeypatch, heatmap):
    with live_view(tmp_path, monkeypatch, 0., heatmap=heatmap) as (window, open_plot):
        plot = open_plot("signal_a")
        monitor = plot.monitor
        plot.close()
        assert plot not in window.windows
        wait_for(lambda: deleted(plot))
        assert sip.isdeleted(monitor)
        assert not window._plot_workers


@pytest.mark.parametrize("heatmap", [False, True])
def test_closed_source_survives_until_final_consumer_release(tmp_path, monkeypatch, heatmap):
    with live_view(tmp_path, monkeypatch, 0., heatmap=heatmap) as (window, open_plot):
        plot = open_plot("signal_a")
        consumer = SourceConsumerRegistration(plot)
        assert consumer.register(window)
        plot.close()
        QtWidgets.QApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        QtWidgets.QApplication.processEvents()
        assert not sip.isdeleted(plot)
        assert plot._merged_trace_users == 1
        assert consumer.release() == 0
        wait_for(lambda: deleted(plot))


def test_close_inside_publication_waits_for_callback_to_unwind(tmp_path, monkeypatch):
    with live_view(tmp_path, monkeypatch, 0.) as (window, open_plot):
        plot = open_plot("signal_a")
        original = plot._complete_plot_publication
        checked = []

        def close_during_publication(worker):
            plot.close()
            # Enter a nested event loop long enough to run the retirement timer.
            loop = QtCore.QEventLoop()
            QtCore.QTimer.singleShot(75, loop.quit)
            loop.exec()
            assert not sip.isdeleted(plot)
            checked.append(True)
            return original(worker)

        monkeypatch.setattr(plot, "_complete_plot_publication", close_during_publication)
        assert plot.load_data()
        wait_for(lambda: deleted(plot))
        assert checked == [True]
        assert not window._plot_workers
