import sqlite3
from dataclasses import FrozenInstanceError

import pytest

from qplot.datahandling import trusted_heatmap, trusted_plot
from qplot.datahandling.trusted_live_service import (
    TrustedLiveReadService,
    _BrokerQueryExecutor,
)
from tests.datahandling.test_trusted_heatmap import plan
from tests.datahandling.test_trusted_plot import (
    make_run,
    prohibit_snapshots,
    protected_state,
)


@pytest.mark.parametrize('kind', ['line', 'array', 'heatmap', 'precision'])
def test_capture_reports_bounded_completed_work_and_protects_source(tmp_path, monkeypatch, kind):
    path = tmp_path / 'progress.db'
    _, guid, table = make_run(path, arrays=kind == 'array', dimensions=1 if kind == 'line' else 2)
    if kind == 'precision':
        with sqlite3.connect(path) as writer:
            writer.execute(f'UPDATE "{table}" SET z=-z WHERE id%2=0')
        writer.close()
    before = protected_state(path)
    prohibit_snapshots(monkeypatch)
    monkeypatch.setattr(trusted_plot, 'PAGE_ROWS', 2)
    monkeypatch.setattr(trusted_plot, 'BLOB_CHUNK_BYTES', 16)
    monkeypatch.setattr(trusted_heatmap, 'SUMMARY_ROWS', 3)
    monkeypatch.setattr(trusted_heatmap, 'AGGREGATE_ROWS', 5)
    monkeypatch.setattr(trusted_heatmap, 'MAX_GROUPS', 2)  # Dense replies must split, not double-count.
    original = _BrokerQueryExecutor.report_progress
    observations = []

    def record(executor, *args, **kwargs):
        original(executor, *args, **kwargs)
        observations.append(executor._operation.progress)

    monkeypatch.setattr(_BrokerQueryExecutor, 'report_progress', record)
    service = TrustedLiveReadService(path)
    try:
        dataset = service.submit_plot_dataset(guid).wait()
        settings = plan(max_cells=1, max_side=1) if kind == 'precision' else plan()
        result = service.submit_plot_prefix(
            dataset, heatmap_plan=settings if kind in ('heatmap', 'precision') else None,
        ).wait()
        phases = {}
        assert [p.stage for p in observations] == sorted(p.stage for p in observations)
        assert observations[0].stage == 1
        assert observations[-1].stage == (2 if kind in ('heatmap', 'precision') else 1)
        for progress in observations:
            if progress.total is not None:
                assert 0 <= progress.completed <= progress.total
                phases.setdefault(progress.phase, []).append(progress)
        expected_phase = 'Building heatmap' if kind in ('heatmap', 'precision') else 'Reading plot data'
        assert phases[expected_phase][-1].completed == result.watermark
        for values in phases.values():
            assert [v.completed for v in values] == sorted(v.completed for v in values)
        if kind in ('heatmap', 'precision'):
            assert phases['Scanning coordinates'][-1].completed == result.watermark
        if kind == 'precision':
            assert phases['Refining numerical precision'][-1].completed == result.watermark
        if kind == 'array':
            arrays = [values for phase, values in phases.items() if phase.startswith('Reading array')]
            assert arrays and all(values[-1].completed == values[-1].total for values in arrays)
        with pytest.raises(FrozenInstanceError):
            observations[-1].phase = 'changed'
        assert protected_state(path) == before
    finally:
        service.close()
