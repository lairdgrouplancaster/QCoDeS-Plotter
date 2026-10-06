import threading
from types import SimpleNamespace

import pytest
from PyQt6 import QtWidgets

from qplot.configuration.config import config
from qplot.datahandling import trusted_plot
from qplot.datahandling.plot_progress import PlotProgress
from qplot.windows._plot_refresh import PlotRefreshMixin
from qplot.windows._plot_state import PlotStateOverlay
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
    overlay = PlotStateOverlay(widget)
    first = worker(PlotProgress('Scanning coordinates', 25, 100))
    first.started_at = 98.0  # Include time spent queued before tracking began.
    second = worker(PlotProgress('Processing plot data', stage=2))
    try:
        overlay.show('Loading measurement', kind='loading')
        overlay.track(first)
        assert overlay.progress_bar.value() == 250
        assert overlay.progress_bar.maximum() == 1000
        assert overlay.progress_bar.format() == '%p% of scan'
        assert overlay.detail_label.text() == 'Stage 1/2: Scanning coordinates'
        assert overlay.progress_timer.isActive()
        assert overlay.elapsed_label.text() == 'Elapsed: 2s'
        clock[0] = 163.0
        overlay._poll_progress()  # Elapsed time advances even without new progress.
        assert overlay.elapsed_label.text() == 'Elapsed: 1m 05s'
        first.progress = PlotProgress('Building heatmap', 50, 100, stage=2)
        overlay._poll_progress()
        assert overlay.elapsed_label.text() == 'Elapsed: 1m 05s'
        overlay.track(second)
        assert overlay.elapsed_label.text() == 'Elapsed: 0s'
        overlay.finish(first)
        assert overlay._worker is second
        assert overlay.detail_label.text() == 'Stage 2/2: Processing plot data'
        assert overlay.progress_bar.maximum() == 0
        overlay.rendering(second)
        assert overlay.detail_label.text() == 'Stage 2/2: Rendering plot'
        clock[0] += 3661
        overlay._poll_progress()
        assert overlay.elapsed_label.text() == 'Elapsed: 1h 01m 01s'
        assert overlay.detail_label.text() == 'Stage 2/2: Rendering plot'
        overlay.finish(second)
        assert overlay.frame.isHidden()
        assert overlay.elapsed_label.isHidden()
        assert not overlay.progress_timer.isActive()
    finally:
        widget.close()


def test_cancel_error_and_close_stop_progress(qapplication):
    widget = QtWidgets.QWidget()
    overlay = PlotStateOverlay(widget)
    current = worker(PlotProgress('Reading plot data', 10, 100))
    try:
        overlay.show('Loading measurement', kind='loading')
        overlay.track(current)
        current.is_cancelled = lambda: True
        overlay._poll_progress()
        assert overlay.title_label.text() == 'Plot load cancelled'
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
        assert not overlay.progress_timer.isActive()
        overlay.show('Loading measurement', kind='loading')
        overlay.track(current)
        overlay.hide()
        assert overlay._worker is None and not overlay.progress_timer.isActive()
    finally:
        widget.close()


@pytest.mark.parametrize('dimensions', [1, 2])
def test_real_plot_keeps_progress_until_display_commit(tmp_path, monkeypatch, dimensions):
    path = tmp_path / 'display-progress.db'
    _, guid, _ = make_run(path, dimensions=dimensions)
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
        overlay = plot.plot_state_overlay
        wait_for(lambda: overlay.progress_bar.maximum() == 1000 and overlay.progress_bar.value() > 0)
        assert 0 < overlay.progress_bar.value() < 1000
        assert overlay.detail_label.text() == 'Stage 1/2: Reading plot data'
        assert plot.worker.running
        release.set()
        wait_for(lambda: not plot.worker.running)
        assert committed == [plot.worker]
        assert overlay.frame.isHidden() and not overlay.progress_timer.isActive()

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
