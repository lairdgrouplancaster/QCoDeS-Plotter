"""Shared fixtures and assertions for axis limit precision."""

import pyqtgraph as pg
from PyQt6 import QtWidgets as qtw

from qplot.windows._plot_axis_scaling import PlotAxisScalingMixin

__all__ = [
    'AxisLimitWindow',
]


class AxisLimitWindow(PlotAxisScalingMixin, qtw.QMainWindow):
    def __init__(self):
        super().__init__()
        self.widget = pg.GraphicsLayoutWidget()
        self.setCentralWidget(self.widget)
        self.plot = self.widget.addPlot()
        self.vb = self.plot.vb
        self.vbMenu = self.vb.menu
        self.statuses = []
        self._init_axis_scale_dialogs()

    def _context_menu_action(self, _text):
        return None

    def _view_range_changed_programmatically(self):
        pass

    def show_status(self, message, _timeout):
        self.statuses.append(message)
