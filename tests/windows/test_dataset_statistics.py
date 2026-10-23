import numpy as np
import pytest
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
    }, 'signal', setpoint_names=('x',))
    assert text.startswith('100001 points\n')
    assert 'Average: 5.00e+04' in text
    assert 'Max: 1.00e+05' in text
    assert dataset_statistics_text(
        {'signal': np.array([np.nan])}, 'signal', setpoint_names=(),
    ) is None


def test_2d_ranges_match_default_plot_axes():
    text = dataset_statistics_text({
        'signal': np.array([1., 2., 3., 4.]),
        'outer': np.array([10., 10., 20., 20.]),
        'inner': np.array([100., 200., 100., 200.]),
    }, 'signal', setpoint_names=('outer', 'inner'))
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
    text = dataset_statistics_text(
        {'signal': np.array([1., 2., 3.])}, 'signal', setpoint_names=(),
    )
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
    dataset = SimpleNamespace(paramspecs={'signal': SimpleNamespace(depends_on_=['x'])})
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


@pytest.mark.parametrize('heatmap', [False, True], ids=['line', 'heatmap'])
@pytest.mark.parametrize('arrays', [False, True], ids=['scalar', 'ragged-array'])
@pytest.mark.parametrize('basis_type', ['numeric', 'text', 'complex'])
def test_inferred_statistics_use_declared_setpoints(
    tmp_path, monkeypatch, heatmap, arrays, basis_type,
):
    from pathlib import Path

    from qcodes.dataset import (
        Measurement,
        initialise_or_create_database_at,
        load_or_create_experiment,
    )

    from qplot.datahandling.readonly import load_by_id_read_only
    from qplot.windows import _plot_actions, _plot_marquee
    from tests.windows.test_complex_line_data import database_state

    path = (tmp_path / 'inferred-statistics.db').resolve()
    initialise_or_create_database_at(str(path), journal_mode='DELETE')
    experiment = load_or_create_experiment('Inferred statistics', 'regression')
    assert Path(experiment.path_to_db).resolve() == path
    measurement = Measurement(exp=experiment)
    paramtype = 'array' if arrays else 'numeric'
    measurement.register_custom_parameter('x', paramtype=paramtype)
    if heatmap:
        measurement.register_custom_parameter('slow', paramtype=paramtype)
    setpoints = ('slow', 'x') if heatmap else ('x',)
    measurement.register_custom_parameter('basis', paramtype=basis_type, setpoints=setpoints)
    measurement.register_custom_parameter(
        'converted', paramtype=paramtype, setpoints=setpoints, basis=('basis',),
    )
    try:
        with measurement.run(write_in_background=False, in_memory_cache=False) as saver:
            assert Path(saver.dataset.path_to_db).resolve() == path
            for indexes in ([np.array([0]), np.array([1, 2])] if arrays else range(3)):
                values = {'x': 100 + indexes, 'converted': 10 + indexes}
                if heatmap:
                    values['slow'] = indexes
                values['basis'] = {'numeric': 1000., 'text': 'basis', 'complex': 2 + 3j}[basis_type]
                if arrays:
                    # Keep each ragged acquisition and its scalar basis in one
                    # record through QCoDeS' public mixed-storage write API.
                    saver.dataset.add_results([values])
                else:
                    saver.add_result(*values.items())
            run_id = saver.run_id
    finally:
        saver.dataset.conn.close()
        experiment.conn.close()
    before = database_state(path)
    dialogs, errors, readers = [], [], []

    class Host(_plot_actions.PlotActionsMixin, qtw.QWidget):
        def _bind_one_shot_dataset_key(self, key):
            return key

        def _load_run_csv_dataset(self, key):
            dataset = load_by_id_read_only(run_id, str(path))
            readers.append(dataset)
            return dataset

        def _require_run_csv_source_current(self, key):
            pass

        def show_error(self, *args):
            errors.append(args)

    def capture_dialog(parent, text, title):
        dialog = new_statistics_dialog(parent, text, title)
        dialogs.append((dialog, text))
        return dialog

    monkeypatch.setattr(_plot_marquee, 'new_statistics_dialog', capture_dialog)
    host = Host()
    try:
        host._show_preview_stats(object(), 'converted')
        assert not errors
        assert len(dialogs) == 1
        assert dialogs[0][1] == '\n'.join([
            '3 points',
            'X range: 100 to 102',
            'Y range: 0.000 to 2.00' if heatmap else 'Y range: 10.0 to 12.0',
            'Average: 11.0',
            'Standard deviation: 8.16e-01',
            'Max: 12.0',
            'Min: 10.0',
        ])
    finally:
        for dialog, _ in dialogs:
            dialog.close()
        host.close()
        for dataset in readers:
            dataset.conn.close()
    assert database_state(path) == before
