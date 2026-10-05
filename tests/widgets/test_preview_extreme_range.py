"""Finite opposite-sign ranges must retain the Qt sparkline geometry."""

import warnings

import numpy as np
import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.datahandling.readSQL import get_runs_via_sql
from qplot.windows._widgets.preview import (
    generate_run_previews,
    render_heatmap_grid_preview,
    render_sparkline_preview,
)
from tests.datahandling.test_array_record_metadata import (
    _artifact_state,
    _protect_source,
)


@pytest.mark.parametrize('wide_axis', ['x', 'signal'])
def test_real_qcodes_extreme_range_sparkline_matches_scaled_geometry(tmp_path, monkeypatch, wide_axis):
    path = tmp_path / 'extreme-range.db'
    initialise_or_create_database_at(str(path), journal_mode='DELETE')
    experiment = load_or_create_experiment('extreme_range', sample_name='sparkline')
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter('x', paramtype='numeric')
    measurement.register_custom_parameter('signal', paramtype='numeric', setpoints=('x',))
    ordinary = np.arange(4, dtype=float)
    wide = np.array([0., 1e308, 1., -1e308])
    x, signal = (wide, ordinary) if wide_axis == 'x' else (ordinary, wide)
    with measurement.run(write_in_background=False) as saver:
        for x_value, y_value in zip(x, signal, strict=True):
            saver.add_result(('x', x_value), ('signal', y_value))
        run_id = saver.run_id
    saver.dataset.conn.close()
    experiment.conn.close()
    protected = _artifact_state(path)
    _protect_source(monkeypatch, path)
    metadata = get_runs_via_sql(path)[run_id]
    with warnings.catch_warnings(record=True) as observed:
        warnings.simplefilter('always', RuntimeWarning)
        actual = generate_run_previews(path, metadata, size=64)[0]['image']
    expected = render_sparkline_preview(x / 1e308 if wide_axis == 'x' else x,
                                       signal / 1e308 if wide_axis == 'signal' else signal,
                                       size=64)
    assert actual == expected, 'Finite acquired vertices must have the same Qt geometry after scaling.'
    assert not observed
    assert _artifact_state(path) == protected


@pytest.mark.parametrize('values', [np.array([0., 1e-308, 2e-308]),
                                    np.array([1e308, np.nextafter(1e308, np.inf)])])
def test_small_and_nearby_large_ranges_keep_sparkline_geometry(values):
    low, high = float(values.min()), float(values.max())
    normalized = (values - low) / (high - low)
    x = np.arange(values.size)
    assert render_sparkline_preview(x, values, 64) == render_sparkline_preview(x, normalized, 64)
    assert (render_heatmap_grid_preview(values.reshape(1, -1), 64)
            == render_heatmap_grid_preview(normalized.reshape(1, -1), 64))


def test_real_qcodes_extreme_range_heatmap_matches_scaled_colors(tmp_path, monkeypatch):
    path = tmp_path / 'extreme-heatmap.db'
    initialise_or_create_database_at(str(path), journal_mode='DELETE')
    experiment = load_or_create_experiment('extreme_range', sample_name='heatmap')
    measurement = Measurement(exp=experiment)
    for name in ('x', 'y'):
        measurement.register_custom_parameter(name, paramtype='numeric')
    measurement.register_custom_parameter('signal', paramtype='numeric', setpoints=('y', 'x'))
    measurement.set_shapes({'signal': (2, 2)})
    grid = np.array([[-1e308, 1e308], [0., 1.]])
    with measurement.run(write_in_background=False) as saver:
        for y in range(2):
            for x in range(2):
                saver.add_result(('x', x), ('y', y), ('signal', grid[y, x]))
        run_id = saver.run_id
    saver.dataset.conn.close()
    experiment.conn.close()
    protected = _artifact_state(path)
    _protect_source(monkeypatch, path)
    metadata = get_runs_via_sql(path)[run_id]
    with warnings.catch_warnings(record=True) as observed:
        warnings.simplefilter('always', RuntimeWarning)
        actual = generate_run_previews(path, metadata, size=64)[0]['image']
    expected = render_heatmap_grid_preview(grid / 1e308, size=64)
    actual.save(str(tmp_path / 'actual.png'))
    expected.save(str(tmp_path / 'expected.png'))
    assert actual == expected, 'Finite acquired cells must retain scale-invariant Qt colors.'
    assert not observed
    assert _artifact_state(path) == protected
