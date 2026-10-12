import sqlite3
import threading
from types import SimpleNamespace

import pytest
from PyQt6 import QtCore, QtGui, QtTest, QtWidgets

from qplot.configuration.config import config
from qplot.datahandling import trusted_plot
from qplot.datahandling.plot_progress import PlotProgress
from qplot.windows._plot_refresh import PlotRefreshMixin
from qplot.windows._plot_feedback import PlotWindowFeedbackMixin
from qplot.windows._plotWin import plotWidget
from qplot.windows._plot_state import PlotStateOverlay
from qplot.windows._plot_status import PlotStatusWidget
from qplot.windows.main import MainWindow
from tests._window_lifecycle import close_main_window
from tests.datahandling.test_trusted_plot import make_run, prohibit_snapshots
from tests.windows.test_trusted_plot_launch import wait_for


def worker(progress):
    return SimpleNamespace(progress=progress, is_cancelled=lambda: False)


def test_stage_progress_unknown_work_and_stale_completion(qapplication, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr('qplot.windows._plot_state.perf_counter', lambda: clock[0])
    widget = QtWidgets.QWidget()
    widget.resize(600, 400)
    overlay = PlotStateOverlay(widget, PlotStatusWidget(widget))
    first = worker(PlotProgress('Scanning coordinates', 25, 100))
    first.started_at = 98.0  # Include time spent queued before tracking began.
    second = worker(PlotProgress('Processing plot data', stage=2))
    try:
        overlay.show('Loading measurement', kind='loading')
        overlay.track(first)
        assert overlay.progress_bar.value() == 250
        assert overlay.progress_bar.maximum() == 1000
        assert overlay.progress_bar.format() == '%p%'
        assert overlay.detail_label.text() == 'Stage 1/2: Scanning coordinates'
        assert overlay.feedback.stage_label.text() == 'Stage 1/2'
        assert 'Scanning coordinates' in overlay.feedback.progress_panel.toolTip()
        assert '25 / 100 (scan)' in overlay.feedback.progress_panel.toolTip()
        assert overlay.frame.isHidden()
        assert overlay.progress_timer.isActive()
        assert overlay.elapsed_label.text() == '2s'
        clock[0] = 163.0
        overlay._poll_progress()  # Elapsed time advances even without new progress.
        assert overlay.elapsed_label.text() == '1m 05s'
        first.progress = PlotProgress('Building heatmap', 50, 100, stage=2)
        overlay._poll_progress()
        assert overlay.elapsed_label.text() == '1m 05s'
        overlay.track(second)
        assert overlay.elapsed_label.text() == '0s'
        overlay.finish(first)
        assert overlay._worker is second
        assert overlay.feedback.stack.currentWidget() is overlay.feedback.progress_panel
        assert overlay.detail_label.text() == 'Stage 2/2: Processing plot data'
        assert overlay.progress_bar.maximum() == 0
        overlay.rendering(second)
        assert overlay.detail_label.text() == 'Stage 2/2: Rendering plot'
        clock[0] += 3661
        overlay._poll_progress()
        assert overlay.elapsed_label.text() == '1h 01m 01s'
        assert overlay.detail_label.text() == 'Stage 2/2: Rendering plot'
        overlay.finish(second)
        assert overlay.feedback.message == 'Plot load complete.'
        assert overlay.frame.isHidden()
        assert overlay.elapsed_label.isHidden()
        assert not overlay.progress_timer.isActive()
    finally:
        widget.close()


def test_cancel_error_and_close_stop_progress(qapplication):
    widget = QtWidgets.QWidget()
    overlay = PlotStateOverlay(widget, PlotStatusWidget(widget))
    current = worker(PlotProgress('Reading plot data', 10, 100))
    try:
        overlay.show('Loading measurement', kind='loading')
        overlay.track(current)
        current.is_cancelled = lambda: True
        overlay._poll_progress()
        assert overlay.title_label.text() == 'Plot load cancelled'
        assert overlay.feedback.message == 'Plot load cancelled'
        assert overlay.progress_bar.isHidden()
        assert overlay.elapsed_label.isHidden()
        assert not overlay.progress_timer.isActive()
        overlay.track(current)  # An already-cancelled worker must not restart polling.
        assert not overlay.progress_timer.isActive()
        current.is_cancelled = lambda: False
        overlay.show('Loading measurement', kind='loading')
        overlay.track(current)
        overlay.show('Source changed', kind='error')
        overlay.finish(current)
        assert overlay.title_label.text() == 'Source changed'
        assert overlay.feedback.message == 'Source changed'
        assert not overlay.progress_timer.isActive()
        overlay.show('Loading measurement', kind='loading')
        overlay.track(current)
        overlay.hide()
        assert overlay._worker is None and not overlay.progress_timer.isActive()
    finally:
        widget.close()


@pytest.mark.parametrize('dimensions,arrays', [(1, False), (2, False), (2, True)])
def test_real_plot_keeps_progress_until_display_commit(tmp_path, monkeypatch, dimensions, arrays):
    path = tmp_path / 'display-progress.db'
    _, guid, _ = make_run(path, dimensions=dimensions, arrays=arrays)
    if arrays:
        for name in ('ARRAY', 'NUMERIC', 'COMPLEX'):
            monkeypatch.delitem(sqlite3.converters, name, raising=False)
    prohibit_snapshots(monkeypatch)
    monkeypatch.setattr(config, 'default_path', str(tmp_path / 'settings'))
    monkeypatch.setattr(config, 'default_file', str(tmp_path / 'settings' / config.config_file_name))
    monkeypatch.setattr(trusted_plot, 'PAGE_ROWS', 1)
    reached, release = threading.Event(), threading.Event()
    original = trusted_plot.plot_prefix
    committed = []
    original_commit = PlotRefreshMixin._commit_refresh_publication

    def suspend(executor, dataset, *args):
        steps = original(executor, dataset, *args)
        try:
            next(steps)
            reached.set()
            while not release.is_set():
                executor.check_cancelled()
                yield
            return (yield from steps)
        finally:
            steps.close()

    def commit(plot, current, **kwargs):
        overlay = plot.plot_state_overlay
        assert overlay._worker is current
        assert overlay.detail_label.text() == 'Stage 2/2: Rendering plot'
        assert overlay.progress_bar.maximum() == 0
        original_commit(plot, current, **kwargs)
        committed.append(current)

    monkeypatch.setattr(trusted_plot, 'plot_prefix', suspend)
    monkeypatch.setattr(PlotRefreshMixin, '_commit_refresh_publication', commit)
    window = MainWindow()
    try:
        window.startupDatabaseTimer.stop()
        window.config.config['user_preference']['confirm_close'] = False
        window.config.config['user_preference']['confirm_close_all'] = False
        window.close_database(status=False)
        window.load_file(str(path))
        wait_for(lambda: not window._database_load_active)
        window.monitor.stop()
        window.openPlot(guid, show=True)
        wait_for(lambda: reached.is_set() and window.windows)
        plot = window.windows[-1]
        plot.resize(1000, 600)
        qapplication = QtWidgets.QApplication.instance()
        qapplication.processEvents()
        overlay = plot.plot_state_overlay
        assert plot.toolbarRef.isVisible()  # This fixture is a completed run.
        assert plot.toolbarRef.actions()[-1] is plot.plotStatusAction
        assert plot.plot_status.parentWidget() is plot.toolbarRef
        assert plot.findChildren(QtWidgets.QStatusBar) == []
        wait_for(lambda: overlay.progress_bar.maximum() == 1000 and overlay.progress_bar.value() > 0)
        assert 0 < overlay.progress_bar.value() < 1000
        assert overlay.detail_label.text().startswith('Stage 1/2: Reading ')
        assert plot.worker.running
        assert overlay.frame.isHidden()
        assert plot.plot_status.progress_panel.isVisible()
        plot.toolbarRef.toggleViewAction().trigger()
        assert plot.toolbarRef.isHidden()
        overlay._poll_progress()
        assert plot.toolbarRef.isHidden()
        release.set()
        wait_for(lambda: not plot.worker.running)
        assert committed == [plot.worker]
        assert overlay.frame.isHidden() and not overlay.progress_timer.isActive()
        assert plot.toolbarRef.isHidden()
        plot.toolbarRef.toggleViewAction().trigger()
        assert plot.plot_status.status_label.isVisible()
        assert plot.plot_status.message.startswith('Loaded ')
        QtWidgets.QApplication.sendEvent(plot, QtGui.QStatusTipEvent('Menu hint'))
        assert plot.findChildren(QtWidgets.QStatusBar) == []
        assert plot.plot_status.message.startswith('Loaded ')

        # A renderer exception must also remove the busy indicator; exercise
        # the concrete callback directly so the exception stays in this test.
        monkeypatch.setattr(PlotRefreshMixin, '_commit_refresh_publication', original_commit)
        def fail_render(*args, **kwargs):
            raise RuntimeError('render failure')
        if dimensions == 2:
            monkeypatch.setattr(plot, '_render_heatmap', fail_render)
        else:
            monkeypatch.setattr(plot.line, 'setData', fail_render)
        overlay.show('Loading measurement', kind='loading')
        overlay.track(plot.worker)
        plot.worker.running = True
        with pytest.raises(RuntimeError, match='render failure'):
            plot.refreshPlot(True, worker=plot.worker)
        assert overlay.progress_bar.isHidden() and not overlay.progress_timer.isActive()
    finally:
        release.set()
        close_main_window(window)


def test_toolbar_messages_persist_and_fit_narrow_windows(qapplication):
    class Window(PlotWindowFeedbackMixin, QtWidgets.QMainWindow):
        visible = True
        monitorIntervalChanged = lambda *args: None
        refreshWindow = lambda *args: None

    window = Window()
    window.monitor = QtCore.QTimer(window)
    plotWidget.initRefresh(window, 1.0)
    feedback = window.plot_status
    overlay = PlotStateOverlay(window, feedback)
    message = 'Loaded 135,751 points for a measurement with a very long name in 0.08 seconds.'
    try:
        window.resize(550, 120)
        window.show()
        window.show_status(message, timeout=1)
        QtTest.QTest.qWait(20)
        assert feedback.message == message
        assert feedback.status_label.toolTip().startswith(message + '\nCreated: ')
        assert feedback.status_label.width() < feedback.status_label.fontMetrics().horizontalAdvance(message)
        assert feedback.isVisible() and window.spinBox.isVisible()
        assert feedback.geometry().left() > window.spinBox.geometry().right()
        assert window.width() == 550

        current = worker(PlotProgress('Reading coordinates', 1, 10))
        overlay.show('Loading measurement', kind='loading')
        overlay.track(current)
        assert feedback.message == ''  # Do not resurrect the previous load's message.
        assert feedback.stack.currentWidget() is feedback.progress_panel
        window.show_status('A new message', timeout=1)
        assert feedback.stack.currentWidget() is feedback.status_label
        overlay.rendering(current)
        assert feedback.stack.currentWidget() is feedback.progress_panel
        window.show_status('Loaded new data', timeout=1)
        overlay.finish(current)
        QtTest.QTest.qWait(20)
        assert feedback.message == 'Loaded new data'
        assert feedback.stack.currentWidget() is feedback.status_label
        assert window.findChildren(QtWidgets.QStatusBar) == []
    finally:
        overlay.hide()
        window.close()
