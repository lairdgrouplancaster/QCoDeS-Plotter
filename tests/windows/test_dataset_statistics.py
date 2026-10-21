import numpy as np
from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from qplot.windows._dataset_statistics import dataset_statistics_text
from qplot.windows._plot_marquee import new_statistics_dialog
from qplot.windows._widgets.preview import PreviewImageLabel


def test_statistics_include_all_samples_and_ignore_nonfinite():
    values = np.arange(100_001, dtype=float)
    text = dataset_statistics_text({
        'x': np.arange(100_003),
        'signal': np.r_[values, np.nan, np.inf],
    }, 'signal')
    assert text.startswith('100001 points\n')
    assert 'Average: 5.00e+04' in text
    assert 'Max: 1.00e+05' in text
    assert dataset_statistics_text({'signal': np.array([np.nan])}, 'signal') is None


def test_2d_ranges_match_default_plot_axes():
    text = dataset_statistics_text({
        'signal': np.array([1., 2., 3., 4.]),
        'outer': np.array([10., 10., 20., 20.]),
        'inner': np.array([100., 200., 100., 200.]),
    }, 'signal')
    from qplot.windows._plotWin import plotWidget

    fmt = plotWidget.formatNum
    assert f'X range: {fmt(100.)} to {fmt(200.)}' in text
    assert f'Y range: {fmt(10.)} to {fmt(20.)}' in text
    assert f'Average: {fmt(2.5)}' in text


def test_thumbnail_action_and_shared_dialog(monkeypatch):
    app = qtw.QApplication.instance() or qtw.QApplication([])
    label = PreviewImageLabel('signal')
    requested = []
    label.statsRequested.connect(requested.append)

    def choose_stats(menu, _position):
        action = next(a for a in menu.actions() if 'stats' in a.text())
        action.trigger()

    monkeypatch.setattr(qtw.QMenu, 'exec', choose_stats)
    label._show_context_menu(QtCore.QPoint())
    assert requested == ['signal']
    text = dataset_statistics_text({'signal': np.array([1., 2., 3.])}, 'signal')
    dialog = new_statistics_dialog(label, text, 'Dataset stats')
    table = dialog.findChild(qtw.QTableWidget)
    assert table.item(0, 1).text() == '3 points'
    buttons = dialog.findChild(qtw.QDialogButtonBox)
    next(b for b in buttons.buttons() if b.text() == 'Copy').click()
    assert app.clipboard().text() == text
    dialog.close()
    label.close()


def test_stats_action_releases_reader_before_showing_dialog(monkeypatch):
    from types import SimpleNamespace

    from qplot.windows import _plot_actions, _plot_marquee

    events = []
    dataset = object()
    key = SimpleNamespace(guid='run')

    class Host(_plot_actions.PlotActionsMixin):
        def _bind_one_shot_dataset_key(self, requested):
            assert requested is key
            return key

        def _load_run_csv_dataset(self, requested):
            events.append('load')
            return dataset

        def _require_run_csv_source_current(self, requested):
            events.append('validate')

    class Dialog:
        def setAttribute(self, *args):
            pass

        def show(self):
            events.append('show')

        def raise_(self):
            pass

        def activateWindow(self):
            pass

    monkeypatch.setattr(_plot_actions, 'parameter_data_for_export',
                        lambda ds, name: {'x': np.arange(3), name: np.array([1., 2., 3.])})
    monkeypatch.setattr(_plot_actions, 'close_dataset_connection',
                        lambda ds: events.append('close'))
    monkeypatch.setattr(_plot_marquee, 'new_statistics_dialog',
                        lambda *args: Dialog())
    Host()._show_preview_stats(key, 'signal')
    assert events == ['load', 'validate', 'close', 'show']
