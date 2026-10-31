"""Display actual nD QCoDeS counts in both run and selected-info tables."""

import pytest

from qplot.datahandling.trusted_live_queries import TrustedMetadataQueryAdapter
from qplot.windows._widgets.treeWidgets import RunList, moreInfo
from tests.datahandling.test_high_dimensional_setpoints import (
    _database,
    _load_basic_runs,
    _ReadOnlySqliteExecutor,
)


@pytest.mark.parametrize("mixed", [False, True])
def test_nd_counts_and_steps_in_run_table_and_info(tmp_path, mixed):
    path, axes = _database(tmp_path, 4, mixed=mixed)
    executor = _ReadOnlySqliteExecutor(path)
    run_list, info = RunList(), moreInfo()
    try:
        adapter = TrustedMetadataQueryAdapter(executor, path)
        _load_basic_runs(adapter)
        fields = adapter.expensive_run(1).as_dict()
        expected = "120 = 2 × 3 × 4 × 5"
        run_list.setRuns({1: fields})
        assert run_list.topLevelItem(0).text(run_list.cols.index("Setpoints")) == expected
        info.set_trusted_run_detail(adapter.selected_run_detail(1))
        overview = {info.overview.item(row, 0).text(): info.overview.item(row, 1).text()
                    for row in range(info.overview.rowCount())}
        assert overview["Data points"] == expected
        steps = {info.parameters.item(row, 0).text(): info.parameters.item(row, 5).text()
                 for row in range(info.parameters.rowCount())}
        assert {a.name: steps[a.name] for a in axes} == {
            a.name: str(a.points) for a in axes
        }
    finally:
        executor.close()
        run_list.deleteLater()
        info.deleteLater()
