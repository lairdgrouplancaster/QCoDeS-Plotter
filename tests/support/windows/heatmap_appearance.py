"""Shared fixtures and assertions for heatmap appearance."""

from types import SimpleNamespace

import numpy as np
import pyqtgraph as pg
from PyQt6 import QtCore, sip
from PyQt6 import QtWidgets as qtw

from qplot.windows._dataset_handle import DatasetKey, TraceKey
from qplot.windows._plot2d_layers import (
    DEFAULT_OVERLAY_OPACITY,
    Plot2DLayerMixin,
)

__all__ = [
    '_Host',
    '_Layer',
    '_key',
    '_retire_appearance_widgets',
]


class _Layer:
    def __init__(self, key, label="ID:2 overlay"):
        self.trace_key = key
        self.label = label
        self.from_win = SimpleNamespace(
            param=SimpleNamespace(name=key.parameter_name),
        )
        self.data_grid = np.array([[0.0, 1.0], [2.0, 3.0]])
        self.geometry = None
        self.image = pg.ImageItem(axisOrder="row-major")
        self.heatmap_mesh = pg.PColorMeshItem()
        self.opacity = DEFAULT_OVERLAY_OPACITY
        self.visible = True

    def render_items(self):
        return [self.image, self.heatmap_mesh]

    def set_opacity(self, value):
        self.opacity = value
        self.image.setOpacity(value)
        self.heatmap_mesh.setOpacity(value)

    def set_visible(self, visible):
        self.visible = visible
        self.image.setVisible(visible)
        self.heatmap_mesh.setVisible(visible)


class _Host(Plot2DLayerMixin, qtw.QMainWindow):
    def __init__(self):
        super().__init__()
        database_key = DatasetKey("database.db", "host-guid")
        self._dataset_key = database_key
        self._trace_key = TraceKey(database_key, "signal")
        self.label = "ID:1 signal"
        self.param = SimpleNamespace(
            name="signal",
            depends_on_=("gate", "field"),
        )
        self.image = pg.ImageItem(axisOrder="row-major")
        self.heatmap_mesh = pg.PColorMeshItem()
        self.dataGrid = np.array([[1.0, 2.0], [4.0, 8.0]])
        self.axis_dropdown = {"x": qtw.QComboBox(), "y": qtw.QComboBox()}
        for dropdown in self.axis_dropdown.values():
            dropdown.addItems(["gate", "field"])
        self.axis_dropdown["x"].setCurrentText("field")
        self.axis_dropdown["y"].setCurrentText("gate")
        self._axis_selection = self.axis_options
        self.refreshes = []
        self._colour_map = "viridis"
        self._init_heatmap_layers()

    @property
    def axis_options(self):
        return {
            axis: dropdown.currentText()
            for axis, dropdown in self.axis_dropdown.items()
        }

    def _heatmap_geometry(self):
        return None

    def refreshWindow(self, *, force=False):
        self.refreshes.append(force)

    def _current_colorbar_colormap_name(self):
        return self._colour_map

    def _available_colorbar_colormaps(self):
        return ("viridis", "plasma", "magma")

    def setColorbarColorMap(self, name):
        self._colour_map = name
        return True


def _retire_appearance_widgets(host, dialog):
    app = qtw.QApplication.instance()
    # Validate queued axis changes while the test still owns its doubles.
    app.processEvents()
    dialog.deleteLater()
    host.deleteLater()
    app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()
    assert sip.isdeleted(dialog)
    assert sip.isdeleted(host)


def _key(guid, parameter):
    return TraceKey(DatasetKey("database.db", guid), parameter)
