import pytest
from PyQt6 import QtWidgets as qtw

from qplot.configuration.themes import dark, light
from qplot.windows._widgets.run_list_items import MEASUREMENT_PREVIEW_SPACING
from qplot.windows._widgets.treeWidgets import RunList


@pytest.mark.parametrize("theme", [light, dark])
@pytest.mark.parametrize("width", [583, 780])
def test_default_column_fits_four_thumbnails_at_normal_spacing(theme, width):
    run_list = RunList()
    try:
        run_list.setStyleSheet(theme.main)
        run_list.resize(width, 300)
        run_list.addRuns({
            1: {
                "guid": "four-measurements",
                "measure_parameters": ["a", "b", "c", "d"],
                "sweep_parameters": ["x"],
                "is_completed": True,
            },
        })
        run_list.show()
        qtw.QApplication.processEvents()

        cell = run_list.preview_cells["four-measurements"]
        layout = cell.content_layout
        thumbnails = [layout.itemAt(index).widget() for index in range(4)]
        margins = layout.contentsMargins()
        assert thumbnails[0].x() >= margins.left()
        assert thumbnails[-1].geometry().right() < cell.width() - margins.right()
        for left, right in zip(thumbnails, thumbnails[1:], strict=False):
            assert right.x() - left.geometry().right() - 1 == MEASUREMENT_PREVIEW_SPACING
    finally:
        run_list.close()
        run_list.deleteLater()
