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


@pytest.fixture
def recorded_statistics_columns(tmp_path):
    from contextlib import contextmanager
    from pathlib import Path

    from qcodes.dataset import (
        Measurement,
        initialise_or_create_database_at,
        load_or_create_experiment,
    )

    from qplot.datahandling.parameter_data import parameter_data_for_export
    from qplot.datahandling.readonly import load_by_id_read_only
    from qplot.windows._plot_actions import _csv_scalar_columns
    from tests.windows.test_complex_line_data import database_state

    @contextmanager
    def recorded(records, parameter_types):
        path = (tmp_path / 'statistics-domain.db').resolve()
        initialise_or_create_database_at(str(path), journal_mode='DELETE')
        experiment = load_or_create_experiment('Statistics numeric domain', 'regression')
        assert Path(experiment.path_to_db).resolve() == path
        measurement = Measurement(exp=experiment)
        measurement.register_custom_parameter('x', paramtype=parameter_types['x'])
        measurement.register_custom_parameter(
            'signal', paramtype=parameter_types['signal'], setpoints=('x',),
        )
        try:
            with measurement.run(write_in_background=False, in_memory_cache=False) as saver:
                assert Path(saver.dataset.path_to_db).resolve() == path
                for x, signal in records:
                    saver.add_result(('x', x), ('signal', signal))
                run_id = saver.run_id
        finally:
            saver.dataset.conn.close()
            experiment.conn.close()
        before = database_state(path)
        dataset = load_by_id_read_only(run_id, str(path))
        try:
            yield _csv_scalar_columns(parameter_data_for_export(dataset, 'signal'), 'signal')
        finally:
            dataset.conn.close()
            assert database_state(path) == before

    return recorded


@pytest.mark.parametrize('complex_parameter', ['signal', 'x'])
@pytest.mark.parametrize('storage', ['scalar', 'array', 'mixed-array', 'real-complex-array'])
def test_statistics_reject_complex_measurements_and_setpoints(
    recorded_statistics_columns, complex_parameter, storage,
):
    import warnings

    columns = {'x': np.array([1000., 2000., 3000.]), 'signal': np.array([1000., 2000., 3000.])}
    columns[complex_parameter] = (columns[complex_parameter].astype(complex)
                                  if storage == 'real-complex-array'
                                  else np.array([10000j, 20000j, 30000j]))
    if storage == 'scalar':
        types = {name: 'complex' if name == complex_parameter else 'numeric' for name in columns}
        records = list(zip(columns['x'], columns['signal'], strict=True))
    else:
        types = {'x': 'array', 'signal': 'array'}
        records = [(columns['x'], columns['signal'])]
        if storage == 'mixed-array':
            records = [(np.array([1000], dtype=np.int64), np.array([1000], dtype=np.int64)),
                       (columns['x'][1:], columns['signal'][1:])]
    with recorded_statistics_columns(records, types) as recorded, warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        with pytest.raises(ValueError, match='real-valued'):
            dataset_statistics_text(recorded, 'signal', setpoint_names=('x',))
        assert not caught


@pytest.mark.skipif(
    np.finfo(np.longdouble).maxexp <= np.finfo(float).maxexp,
    reason='This platform has no extended floating exponent range.',
)
@pytest.mark.parametrize('extended_parameter', ['signal', 'x'])
@pytest.mark.parametrize('samples', [
    ['1e400', '2e400'], ['1e-400', '2e-400'], ['-1', '1e-400', '1'],
])
def test_statistics_refuse_finite_samples_outside_display_range(
    recorded_statistics_columns, extended_parameter, samples,
):
    values = np.array(samples, dtype=np.longdouble)
    columns = {'x': np.arange(values.size, dtype=float), 'signal': np.arange(values.size, dtype=float)}
    columns[extended_parameter] = values
    records = [(columns['x'], columns['signal'])]
    with recorded_statistics_columns(records, {'x': 'array', 'signal': 'array'}) as recorded:
        assert np.all(np.isfinite(recorded[extended_parameter]))
        with pytest.raises(ValueError, match='display range'):
            dataset_statistics_text(recorded, 'signal', setpoint_names=('x',))


@pytest.mark.skipif(
    np.finfo(np.longdouble).maxexp <= np.finfo(float).maxexp,
    reason='This platform has no extended floating exponent range.',
)
def test_statistics_refuse_unrepresentable_nonzero_dispersion(recorded_statistics_columns):
    base = np.longdouble(np.nextafter(0., 1.))
    values = np.array([base, np.nextafter(base, np.longdouble('inf'))])
    assert values[0] != values[1]
    assert float(values[0]) == float(values[1]) != 0

    with recorded_statistics_columns(
        [(np.arange(2.), values)], {'x': 'array', 'signal': 'array'},
    ) as recorded:
        with pytest.raises(ValueError, match='display range'):
            dataset_statistics_text(recorded, 'signal', setpoint_names=('x',))


@pytest.mark.parametrize('samples,expected_mean,expected_deviation', [
    (np.array([-np.finfo(float).max, np.finfo(float).max]), '0.000', '1.80e+308'),
    (np.array([-np.nextafter(0., 1.), np.nextafter(0., 1.)]), '0.000', '4.94e-324'),
    (np.array([2**64 - 3, 2**64 - 2, 2**64 - 1], dtype=np.uint64), '1.84e+19', '8.16e-01'),
    (np.longdouble(1) + np.arange(3, dtype=np.longdouble) * np.finfo(np.longdouble).eps,
     '1.00', f'{float(np.finfo(np.longdouble).eps) * (2 / 3) ** .5:.2e}'),
])
def test_supported_statistics_keep_original_precision_and_finite_extremes(
    recorded_statistics_columns, samples, expected_mean, expected_deviation,
):
    with recorded_statistics_columns(
        [(np.arange(samples.size, dtype=float), samples)], {'x': 'array', 'signal': 'array'},
    ) as recorded:
        np.testing.assert_array_equal(recorded['signal'], samples)
        text = dataset_statistics_text(recorded, 'signal', setpoint_names=('x',))
        assert f'Average: {expected_mean}\n' in text
        assert f'Standard deviation: {expected_deviation}\n' in text
        np.testing.assert_array_equal(recorded['signal'], samples)


@pytest.mark.parametrize('storage', ['scalar', 'array', 'mixed-array'])
@pytest.mark.parametrize('sign', [1, -1])
def test_statistics_refuse_nonzero_mean_that_underflows_to_zero(
    recorded_statistics_columns, storage, sign,
):
    from fractions import Fraction

    smallest = np.nextafter(0., 1.)
    samples = sign * np.array([2 * smallest, -smallest])
    if storage == 'scalar':
        records = list(zip(np.arange(samples.size, dtype=float), samples, strict=True))
        types = {'x': 'numeric', 'signal': 'numeric'}
    else:
        records = [(np.arange(samples.size, dtype=float), samples)]
        types = {'x': 'array', 'signal': 'array'}
        if storage == 'mixed-array':
            records.insert(0, (np.array([-1]), np.array([0], dtype=np.int64)))
    with recorded_statistics_columns(records, types) as recorded:
        exact_mean = sum(Fraction(value) for value in recorded['signal']) / len(recorded['signal'])
        assert exact_mean != 0 and float(exact_mean) == 0
        with pytest.raises(ValueError, match='display range'):
            dataset_statistics_text(recorded, 'signal', setpoint_names=('x',))


@pytest.mark.skipif(
    np.finfo(np.longdouble).nmant <= np.finfo(float).nmant,
    reason='This platform has no extended floating precision.',
)
def test_statistics_refuse_extended_cancellation_with_underflowed_mean(recorded_statistics_columns):
    from fractions import Fraction

    base = np.longdouble(np.finfo(float).tiny)
    samples = np.array([np.nextafter(base, np.longdouble('inf')), -base])
    exact = [Fraction(*value.as_integer_ratio()) for value in samples]
    assert sum(exact) != 0 and float(sum(exact) / len(exact)) == 0
    assert np.all(samples.astype(float) != 0)
    with recorded_statistics_columns(
        [(np.arange(samples.size, dtype=float), samples)], {'x': 'array', 'signal': 'array'},
    ) as recorded:
        with pytest.raises(ValueError, match='display range'):
            dataset_statistics_text(recorded, 'signal', setpoint_names=('x',))


@pytest.mark.parametrize('samples', [
    np.array([0.]),
    np.array([0], dtype=np.int64),
    np.array([-np.nextafter(0., 1.), np.nextafter(0., 1.)]),
    np.array([-2**62, 0, 2**62], dtype=np.int64),
    np.array([np.finfo(float).max, np.finfo(float).max,
              -np.finfo(float).max, -np.finfo(float).max]),
])
def test_statistics_keep_exactly_zero_means(recorded_statistics_columns, samples):
    with recorded_statistics_columns(
        [(np.arange(samples.size, dtype=float), samples)], {'x': 'array', 'signal': 'array'},
    ) as recorded:
        text = dataset_statistics_text(recorded, 'signal', setpoint_names=('x',))
        assert 'Average: 0.000\n' in text
        np.testing.assert_array_equal(recorded['signal'], samples)
