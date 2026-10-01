"""Exercise numeric input through the heatmap's actual Color Scale dialog."""

import numpy as np
import pyqtgraph as pg
import pytest
from PyQt6 import QtCore, QtWidgets as qtw

from qplot.windows.plot2d import plot2d


@pytest.fixture(params=["de_DE", "C", "en_US"])
def heatmap(request, qapplication):
    original_locale = QtCore.QLocale()
    QtCore.QLocale.setDefault(QtCore.QLocale(request.param))
    window = plot2d.__new__(plot2d)
    qtw.QMainWindow.__init__(window)
    try:
        # Use real renderers and production dialog/range methods; no database I/O.
        window.widget = pg.GraphicsLayoutWidget()
        window.setCentralWidget(window.widget)
        window.plot = window.widget.addPlot()
        window.image = pg.ImageItem(axisOrder="row-major")
        window.dataGrid = np.array([[0.0, 1.0], [2.0, 3.0]])
        window.image.setImage(window.dataGrid)
        window.plot.addItem(window.image)
        window.bar = pg.ColorBarItem(values=(0.0, 3.0), colorMap=pg.colormap.get("viridis"))
        window.bar.setImageItem(window.image, insert_in=window.plot)
        window._colorbar_manual_levels = None
        window._colorbar_colormap_name = "viridis"
        window.relevel_refresh = qtw.QCheckBox(window)
        window.relevel_refresh.setChecked(True)
        window._init_colorbar_scale_controls()
        window.show()
        window.open_colorbar_scale_dialog()
        qapplication.processEvents()
        yield window
    finally:
        window.hide()
        window.deleteLater()
        qapplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        QtCore.QLocale.setDefault(original_locale)


@pytest.mark.parametrize("texts, expected", [
    (("1.5", "2.5"), (1.5, 2.5)),
    (("1.234", "2.345"), (1.234, 2.345)),
    (("1.5e-3", "2.5E+3"), (0.0015, 2500.0)),
    (("-2.5e2", "+1.5e2"), (-250.0, 150.0)),
])
def test_manual_input_has_validator_numeric_meaning(heatmap, texts, expected):
    fields = (heatmap.colorbar_min_text, heatmap.colorbar_max_text)
    for field, text in zip(fields, texts):
        field.setText(text)
        assert field.hasAcceptableInput()
        value, ok = field.validator().locale().toDouble(text)
        assert ok
        assert value == expected[fields.index(field)]
    heatmap.colorbar_manual_radio.click()
    assert heatmap._colorbar_manual_levels == expected
    assert tuple(heatmap.bar.levels()) == expected
    assert not heatmap.relevel_refresh.isChecked()


@pytest.mark.parametrize("text", [
    "1,5", "1,234", "1.234,5", "1,234.5", "1 234", "1_234",
    "nan", "inf", "-inf", "1e999", "1e", "",
])
@pytest.mark.parametrize("field_name", ["colorbar_min_text", "colorbar_max_text"])
def test_invalid_input_cannot_apply_through_manual(heatmap, text, field_name):
    assert heatmap.setColorbarManualRange(0.0, 3.0)
    field = getattr(heatmap, field_name)
    field.setText(text)
    assert not field.hasAcceptableInput()
    heatmap.colorbar_manual_radio.click()
    assert heatmap._colorbar_manual_levels == (0.0, 3.0)
    assert tuple(heatmap.bar.levels()) == (0.0, 3.0)


@pytest.mark.parametrize("limits", [(2.0, 1.0), (2.0, 2.0)])
def test_manual_requires_increasing_limits(heatmap, limits):
    heatmap.colorbar_min_text.setText(str(limits[0]))
    heatmap.colorbar_max_text.setText(str(limits[1]))
    heatmap.colorbar_manual_radio.click()
    assert heatmap._colorbar_manual_levels is None
    assert tuple(heatmap.bar.levels()) == (0.0, 3.0)


@pytest.mark.parametrize("limits", [
    (1.234, 2.345),
    (1.2345678901234567, 1.2345678901234569),
    (-1.2345678901234567e-12, 2.345678901234567e12),
])
def test_reopening_manual_limits_preserves_values(heatmap, limits, qapplication):
    assert heatmap.setColorbarManualRange(*limits)
    heatmap.colorbar_scale_dialog.close()
    heatmap.open_colorbar_scale_dialog()
    qapplication.processEvents()
    for field, value in zip(
        (heatmap.colorbar_min_text, heatmap.colorbar_max_text), limits
    ):
        assert field.hasAcceptableInput()
        assert float(field.text()) == value
    heatmap.colorbar_manual_radio.click()
    assert heatmap._colorbar_manual_levels == limits
    assert tuple(heatmap.bar.levels()) == limits
