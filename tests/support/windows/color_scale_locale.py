"""Shared fixtures and assertions for color scale locale."""

import numpy as np
import pyqtgraph as pg
import pytest
from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from qplot.windows.plot2d import plot2d

__all__ = [
    'heatmap',
]


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
