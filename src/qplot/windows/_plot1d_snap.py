from PyQt6 import QtGui, QtWidgets as qtw
from PyQt6 import QtCore
from PyQt6.QtGui import QKeySequence

import numpy as np
import pyqtgraph as pg

from ._shortcuts import platform_key_sequences


SNAP_TO_TRACE_SHORTCUTS = platform_key_sequences(
    mac=["Ctrl+Alt+S", "Meta+Alt+S"],
    windows=["Ctrl+Alt+S"],
    other=["Ctrl+Alt+S"],
    )
SNAP_TO_TRACE_SHORTCUT_LABEL = SNAP_TO_TRACE_SHORTCUTS[0].toString(
    QKeySequence.SequenceFormat.NativeText
    )


class Plot1DSnapMixin:
    """Snap-to-trace cursor readout for 1D plot windows."""

    def initLabels(self):
        """
        Sets up coordinate labels and trace snapping command for 1d plots.

        """
        super().initLabels()

        self.trace_label = qtw.QLabel("")
        self.trace_label.setMinimumWidth(0)
        self.toolbarCo_ord.addWidget(self.trace_label)

        self.snap_to_trace_action = QtGui.QAction(
            f"Snap to Trace ({SNAP_TO_TRACE_SHORTCUT_LABEL})",
            self,
            checkable=True
            )
        self.snap_to_trace_action.setToolTip(
            "Lock the coordinate readout to the nearest plotted data point"
            )
        self.register_shortcut(
            self.snap_to_trace_action,
            SNAP_TO_TRACE_SHORTCUTS,
            "Toggle snap-to-trace cursor readout"
            )
        self.snap_to_trace_action.toggled.connect(self._snap_to_trace_toggled)


    def initMenu(self):
        """
        Adds 1d-specific commands to the plot window menu bar.

        """
        super().initMenu()

        view_menu = self._menu_by_title("&View")
        if view_menu is None or self.snap_to_trace_action is None:
            return

        actions = view_menu.actions()
        before = actions[0] if actions else None
        view_menu.insertAction(before, self.snap_to_trace_action)
        view_menu.insertSeparator(before)


    def _menu_by_title(self, title):
        """
        Returns the menu matching a top-level menu title.

        """
        for action in self.menuBar().actions():
            if action.text() == title:
                return action.menu()
        return None


    @QtCore.pyqtSlot(bool)
    def _snap_to_trace_toggled(self, enabled):
        """
        Handles the snap-to-trace toggle state.

        """
        if not enabled:
            self._hide_snap_marker()
            self._clear_snap_report()


    @QtCore.pyqtSlot(object)
    def mouseMoved(self, pos):
        """
        Updates the coordinate readout, optionally snapping to a 1d trace.

        """
        if not (
            self.snap_to_trace_action is not None
            and self.snap_to_trace_action.isChecked()
            ):
            super().mouseMoved(pos)
            return

        if not self.plot.sceneBoundingRect().contains(pos):
            self._hide_snap_marker()
            self._clear_snap_report()
            return

        nearest = self._nearest_trace_point(pos)
        if nearest is None:
            self._hide_snap_marker()
            self._clear_snap_report()
            return

        label, x_value, y_value, viewbox, point_number = nearest
        self.pos_labels["x"].setText(f"x = {self.formatNum(x_value)};")
        self.pos_labels["y"].setText(f"y = {self.formatNum(y_value)}")
        self._show_snap_report(label, point_number)
        self._show_snap_marker(x_value, y_value, viewbox)


    def _show_snap_report(self, label, point_number):
        """
        Shows the currently snapped run, trace, and point.

        """
        if self.trace_label is None:
            return

        run_id, trace = self._snap_report_parts(label)
        self.trace_label.setText(
            f"Snapped to run {run_id}, trace {trace}, point {point_number}."
            )
        self.trace_label.setToolTip(str(label))
        self.trace_label.adjustSize()
        self.trace_label.updateGeometry()
        self.toolbarCo_ord.updateGeometry()


    def _clear_snap_report(self):
        """
        Hides the snap status message.

        """
        if self.trace_label is None:
            return

        self.trace_label.clear()
        self.trace_label.setToolTip("")
        self.trace_label.adjustSize()
        self.trace_label.updateGeometry()
        self.toolbarCo_ord.updateGeometry()


    def _snap_report_parts(self, label):
        """
        Returns run and trace names for the snap status message.

        """
        line = self.lines.get(label)
        source = getattr(line, "from_win", self)
        run_id = getattr(source.ds, "run_id", "?")
        trace = getattr(source.param, "name", str(label).split()[-1])
        return run_id, trace


    def _nearest_trace_point(self, scene_pos):
        """
        Finds the plotted data point nearest to the mouse position.

        """
        nearest = None
        nearest_distance = None

        for label, line in self.lines.items():
            if line is None or not hasattr(line, "getData"):
                continue

            data = line.getData()
            if data is None or data[0] is None or data[1] is None:
                continue

            x_data = np.asarray(data[0], dtype=float)
            y_data = np.asarray(data[1], dtype=float)
            if x_data.size == 0 or y_data.size == 0:
                continue

            count = min(x_data.size, y_data.size)
            x_data = x_data[:count]
            y_data = y_data[:count]
            finite = np.isfinite(x_data) & np.isfinite(y_data)
            if not np.any(finite):
                continue

            finite_indices = np.flatnonzero(finite)
            x_values = x_data[finite]
            y_values = y_data[finite]
            viewbox = self._viewbox_for_line(line)
            mouse_point = viewbox.mapSceneToView(scene_pos)
            index = int(np.argmin(np.abs(x_values - mouse_point.x())))

            x_value = float(x_values[index])
            y_value = float(y_values[index])
            point_scene = viewbox.mapViewToScene(QtCore.QPointF(x_value, y_value))
            distance = (
                (point_scene.x() - scene_pos.x()) ** 2
                + (point_scene.y() - scene_pos.y()) ** 2
                )

            if nearest_distance is None or distance < nearest_distance:
                point_number = int(finite_indices[index]) + 1
                nearest = (label, x_value, y_value, viewbox, point_number)
                nearest_distance = distance

        return nearest


    def _viewbox_for_line(self, line):
        """
        Returns the viewbox that owns a plotted line.

        """
        if getattr(line, "side", "left") == "right" and self.right_vb is not None:
            return self.right_vb

        return self.plot.vb


    def _show_snap_marker(self, x_value, y_value, viewbox):
        """
        Places a small marker on the snapped data point.

        """
        if self.snap_marker is None:
            self.snap_marker = pg.ScatterPlotItem(
                symbol="s",
                size=3,
                pen=pg.mkPen("k", width=1),
                brush=pg.mkBrush("w"),
                )

        if self._snap_marker_view is not viewbox:
            self._hide_snap_marker()
            viewbox.addItem(self.snap_marker)
            self._snap_marker_view = viewbox

        self.snap_marker.setData([x_value], [y_value])


    def _hide_snap_marker(self):
        """
        Removes the snap marker from whichever viewbox currently owns it.

        """
        if self.snap_marker is None or self._snap_marker_view is None:
            return

        self._snap_marker_view.removeItem(self.snap_marker)
        self._snap_marker_view = None


