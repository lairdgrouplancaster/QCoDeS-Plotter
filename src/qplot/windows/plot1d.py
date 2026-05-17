from ._plotWin import plotWidget
from ._plot1d_snap import Plot1DSnapMixin
from ._plot1d_traces import Plot1DTraceMixin


from PyQt5 import (
    QtCore,
    )

import numpy as np


class plot1d(Plot1DSnapMixin, Plot1DTraceMixin, plotWidget):
    """
    Plot window for 1d Line plots.
    Inherits and wraps several functions from qplot.windows._plotWin.plotWidget.
    PlotWidget handles majority of set up, recommend to view first.
    
    Key functions to see in plot1d:
        initFrame
        refreshPlot
        
    Snap-to-trace behavior lives in qplot.windows._plot1d_snap.
    Trace-control and secondary-axis behavior lives in
    qplot.windows._plot1d_traces.
    
    """
    get_mergables = QtCore.pyqtSignal()
    remove_dataset = QtCore.pyqtSignal([str])
    
    def __init__(self, 
                 *args,
                 **kargs
                 ):
        self.mergable = None
        self.line = None
        self.right_vb = None
        self.snap_to_trace_action = None
        self.trace_label = None
        self.snap_marker = None
        self._snap_marker_view = None
        super().__init__(*args, **kargs)
        
        
    def initFrame(self):
        """
        Sets up the initial plot and starting data.

        """
        
        self.line = self.plot.plot(connect="all")
        self._register_main_line()
        
        # Wait for loader to finish to enure needed data is collected.
        self.load_data()
        self.show_status("Line plot ready; loading data...", 5000)


    def _snap_marquee_rect(self, rect):
        """
        Snap marquee X edges to the spaces between plotted data points.

        """
        boundaries = self._marquee_x_boundaries()
        if boundaries is None:
            return rect

        left_index = int(np.searchsorted(boundaries, rect.left(), side="right")) - 1
        right_index = int(np.searchsorted(boundaries, rect.right(), side="left"))
        left_index = min(max(left_index, 0), len(boundaries) - 2)
        right_index = min(max(right_index, left_index + 1), len(boundaries) - 1)

        return QtCore.QRectF(
            boundaries[left_index],
            rect.top(),
            boundaries[right_index] - boundaries[left_index],
            rect.height(),
            )


    def _marquee_x_boundaries(self):
        """
        Return X coordinates halfway between visible 1d sample points.

        """
        x_data = None
        line = self.__dict__.get("line")
        if line is not None and hasattr(line, "getData"):
            data = line.getData()
            if data is not None:
                x_data = data[0]

        if x_data is None:
            x_data = self.__dict__.get("axis_data", {}).get("x")

        if x_data is None:
            return None

        values = np.asarray(x_data, dtype=float)
        values = np.unique(values[np.isfinite(values)])
        if values.size == 0:
            return None
        if values.size == 1:
            point = float(values[0])
            return np.array([point - 0.5, point + 0.5])

        gaps = np.diff(values)
        mids = values[:-1] + gaps / 2
        first = values[0] - gaps[0] / 2
        last = values[-1] + gaps[-1] / 2
        return np.concatenate(([first], mids, [last]))


    def _marquee_stats_text(self):
        values = self._marquee_line_values()
        if values is None:
            return None

        return self._format_marquee_stats_text(f"{values.size} points", values)


    def _marquee_line_values(self):
        if self.marquee is None:
            return None

        line = self.__dict__.get("line")
        if line is not None and hasattr(line, "getData"):
            data = line.getData()
            if data is not None:
                x_data, y_data = data
            else:
                x_data, y_data = None, None
        else:
            axis_data = self.__dict__.get("axis_data", {})
            x_data = axis_data.get("x")
            y_data = axis_data.get("y")

        if x_data is None or y_data is None:
            return None

        x_data = np.asarray(x_data, dtype=float)
        y_data = np.asarray(y_data, dtype=float)
        count = min(x_data.size, y_data.size)
        if count == 0:
            return None

        rect = self.marquee.normalized()
        x_data = x_data[:count]
        y_data = y_data[:count]
        mask = (
            np.isfinite(x_data)
            & np.isfinite(y_data)
            & (x_data >= rect.left())
            & (x_data <= rect.right())
            & (y_data >= rect.top())
            & (y_data <= rect.bottom())
            )
        if not np.any(mask):
            return None

        return y_data[mask]


    def refreshPlot(self, finished : bool = True, worker=None):
        """
        Updates plot based on data produced by the thread worker. Data is 
        assigned in plotWidget.refreshPlot, then all plot items are produced
        here.

        Parameters
        ----------
        finished : bool
            In the event the worker had to abort, finished is False and refresh
            is not ran.
        """
        if not super().refreshPlot(finished, worker=worker):
            return
        
        # Main line
        self.line.setData(
            x=self.axis_data["x"], 
            y=self.axis_data["y"],
            )
        if self.marquee is not None:
            self.set_marquee_rect(self.marquee)

        self.refresh_secondary_lines()
        
        # Allow new worker to be produced
        self.worker.running = False
