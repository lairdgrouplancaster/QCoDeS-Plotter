from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np
import numpy.typing as npt
import pyqtgraph as pg
from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

from ._commands import command_spec, command_with_status, create_action

if TYPE_CHECKING:
    class _Plot1DSnapBase(qtw.QMainWindow):
        toolbarCo_ord: qtw.QToolBar
        snap_to_trace_action: QtGui.QAction | None
        trace_label: qtw.QLabel | None
        lines: dict[Any, Any]
        pos_labels: dict[str, qtw.QLabel]
        plot: Any
        right_vb: Any
        snap_marker: Any
        _snap_marker_view: Any
        ds: Any
        param: Any

        def initLabels(self) -> None: ...

        def initMenu(self) -> None: ...

        def mouseMoved(self, pos: object) -> None: ...

        def register_shortcut(
                self,
                action: QtGui.QAction,
                shortcut: object,
                status_tip: str | None = None,
                ) -> None: ...

        @staticmethod
        def formatNum(num: float, sf: int = 3) -> str: ...

        def _set_cursor_index_label(self, text: str) -> None: ...
else:
    class _Plot1DSnapBase:
        pass


SNAP_TO_TRACE_COMMAND = command_spec("plot.snap_to_trace")
SNAP_TO_TRACE_SHORTCUT_LABEL = SNAP_TO_TRACE_COMMAND.shortcut_display_text()


@dataclass(frozen=True)
class _SnapTraceSample:
    x_value: float
    y_value: float
    point_number: int


_LineData = tuple[npt.ArrayLike, npt.ArrayLike]


def _nearest_trace_sample(
        x_data: npt.ArrayLike,
        y_data: npt.ArrayLike,
        cursor_x: float,
        cursor_y: float | None = None,
        ) -> _SnapTraceSample | None:
    """
    Return the finite plotted sample nearest to a cursor X coordinate.

    """
    x_data = np.asarray(x_data, dtype=float)
    y_data = np.asarray(y_data, dtype=float)
    count = min(x_data.size, y_data.size)
    if count == 0:
        return None

    x_data = x_data[:count]
    y_data = y_data[:count]
    finite = np.isfinite(x_data) & np.isfinite(y_data)
    if not np.any(finite):
        return None

    finite_indices = np.flatnonzero(finite)
    x_values = x_data[finite]
    y_values = y_data[finite]
    if cursor_y is None:
        distances = np.abs(x_values - cursor_x)
    else:
        distances = np.square(x_values - cursor_x) + np.square(y_values - cursor_y)
    index = int(np.argmin(distances))

    return _SnapTraceSample(
        x_value=float(x_values[index]),
        y_value=float(y_values[index]),
        point_number=int(finite_indices[index]) + 1,
        )


def _line_is_snap_visible(line: object) -> bool:
    """
    Return whether a plotted line should participate in snap selection.

    """
    is_visible = getattr(line, "isVisible", None)
    if callable(is_visible):
        return bool(is_visible())
    return True


def _line_snap_data(line: object | None) -> _LineData | None:
    """
    Return line data when the item is usable for snap selection.

    """
    if line is None or not _line_is_snap_visible(line):
        return None

    get_data = getattr(line, "getData", None)
    if not callable(get_data):
        return None

    data = get_data()
    if data is None or data[0] is None or data[1] is None:
        return None
    return data[0], data[1]


def _scene_distance_squared(
        first: QtCore.QPointF,
        second: QtCore.QPointF,
        ) -> float:
    """
    Return squared distance between two scene points.

    """
    return (first.x() - second.x()) ** 2 + (first.y() - second.y()) ** 2


class Plot1DSnapMixin(_Plot1DSnapBase):
    """Snap-to-trace cursor readout for 1D plot windows."""

    def initLabels(self):
        """
        Sets up coordinate labels and trace snapping command for 1d plots.

        """
        super().initLabels()

        self.trace_label = qtw.QLabel("")
        self.trace_label.setMinimumWidth(0)
        self.toolbarCo_ord.addWidget(self.trace_label)
        self._update_coordinate_context()

        self.snap_to_trace_action = create_action(
            SNAP_TO_TRACE_COMMAND,
            self,
            text=f"Snap to Trace ({SNAP_TO_TRACE_SHORTCUT_LABEL})",
            status_tip="Toggle snap-to-trace cursor readout",
            checkable=True,
            )
        self.snap_to_trace_action.setToolTip(
            "Lock the coordinate readout to the nearest plotted data point"
            )
        self.register_shortcut(
            self.snap_to_trace_action,
            command_with_status(
                "plot.snap_to_trace",
                "Toggle snap-to-trace cursor readout",
                )
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
        menu_bar = self.menuBar()
        if menu_bar is None:
            return None

        for action in menu_bar.actions():
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
            self._set_cursor_index_label("")
            return

        nearest = self._nearest_trace_point(pos)
        if nearest is None:
            self._hide_snap_marker()
            self._clear_snap_report()
            self._set_cursor_index_label("")
            return

        label, x_value, y_value, viewbox, point_number = nearest
        self._set_cursor_index_label(f"[{point_number - 1}]")
        self.pos_labels["x"].setText(f"x = {self.formatNum(x_value)};")
        self.pos_labels["y"].setText(f"y = {self.formatNum(y_value)}")
        self._show_snap_report(label, point_number)
        self._show_snap_marker(x_value, y_value, viewbox)


    def _show_snap_report(self, label, point_number):
        """
        Shows the active run, plotted axes, and snapped point.

        """
        self._update_coordinate_context(label, point_number)


    def _clear_snap_report(self):
        """
        Restores the persistent run and axis relationship message.

        """
        self._update_coordinate_context()


    def _update_coordinate_context(self, label=None, point_number=None):
        """Show ``Run N, measurement vs setpoint`` and optional snap detail."""

        if self.trace_label is None:
            return

        run_id, vertical, horizontal, tooltip = self._coordinate_context_parts(label)
        message = f"Run {run_id}, {vertical} vs {horizontal}"
        if point_number is not None:
            message += f", snapped to point {point_number}"
        self.trace_label.setText(message)
        self.trace_label.setToolTip(tooltip)
        self.trace_label.adjustSize()
        self.trace_label.updateGeometry()
        self.toolbarCo_ord.updateGeometry()


    def _coordinate_context_parts(self, label=None):
        """Return run and human-readable plotted-axis names for the message."""

        line = self.lines.get(label) if label is not None else self.__dict__.get("line")
        source = getattr(line, "from_win", self)
        try:
            dataset = source.ds
        except (AttributeError, RuntimeError):
            dataset = None
        run_id = getattr(dataset, "run_id", "?")

        host_options = self.__dict__.get("_axis_selection", {})
        if not host_options:
            try:
                host_options = self.axis_options
            except RuntimeError:
                host_options = {}
        horizontal_source = self
        horizontal_axis = "x"
        source_axis = "y"
        if source is not self:
            axis_order = getattr(line, "choose_from", None)
            if axis_order is not None and len(axis_order) == 2:
                horizontal_axis = axis_order[0]
                source_axis = axis_order[1]
            horizontal_source = source
        source_options = (
            host_options
            if source is self
            else getattr(source, "axis_options", {})
            )
        horizontal_name = source_options.get(horizontal_axis)
        if horizontal_name is None:
            horizontal_name = host_options.get("x", "x")
            horizontal_source = self
            horizontal_axis = "x"
        horizontal = self._parameter_display_name(
            horizontal_source,
            horizontal_name,
            horizontal_axis,
            )
        vertical_name = source_options.get(source_axis)
        if vertical_name is None:
            try:
                source_param = source.param
            except (AttributeError, RuntimeError):
                source_param = None
            vertical_name = getattr(source_param, "name", "y")
        vertical = self._parameter_display_name(
            source,
            vertical_name,
            source_axis,
            )

        display_label = getattr(self, "_trace_display_label", None)
        if label is not None and callable(display_label):
            tooltip = str(display_label(label, line))
        else:
            tooltip = str(getattr(source, "label", ""))
        return run_id, vertical, horizontal, tooltip


    @staticmethod
    def _parameter_display_name(source, name, axis):
        """Resolve a parameter label without discarding a useful name."""

        param = getattr(source, "param_dict", {}).get(name)
        if param is None:
            axis_param = getattr(source, "axis_param", {})
            candidate = axis_param.get(axis) if isinstance(axis_param, dict) else None
            if getattr(candidate, "name", name) == name:
                param = candidate
        if param is None:
            candidate = getattr(source, "param", None)
            if getattr(candidate, "name", None) == name:
                param = candidate
        return str(getattr(param, "label", None) or getattr(param, "name", None) or name)

    def _nearest_trace_point(self, scene_pos):
        """
        Finds the plotted data point nearest to the mouse position.

        """
        nearest = None
        nearest_distance = None

        for label, line in self.lines.items():
            data = _line_snap_data(line)
            if data is None:
                continue

            viewbox = self._viewbox_for_line(line)
            sample, distance = self._nearest_scene_trace_sample(
                data[0],
                data[1],
                scene_pos,
                viewbox,
                )
            if sample is None:
                continue

            if nearest_distance is None or distance < nearest_distance:
                nearest = (
                    label,
                    sample.x_value,
                    sample.y_value,
                    viewbox,
                    sample.point_number,
                    )
                nearest_distance = distance

        return nearest


    @staticmethod
    def _nearest_scene_trace_sample(x_data, y_data, scene_pos, viewbox):
        """Return the trace sample nearest to a scene position in screen space."""

        x_data = np.asarray(x_data, dtype=float)
        y_data = np.asarray(y_data, dtype=float)
        count = min(x_data.size, y_data.size)
        if count == 0:
            return None, None

        x_data = x_data[:count]
        y_data = y_data[:count]
        finite = np.isfinite(x_data) & np.isfinite(y_data)
        if not np.any(finite):
            return None, None

        finite_indices = np.flatnonzero(finite)
        x_values = x_data[finite]
        y_values = y_data[finite]
        origin = viewbox.mapViewToScene(QtCore.QPointF(0.0, 0.0))
        x_basis = viewbox.mapViewToScene(QtCore.QPointF(1.0, 0.0))
        y_basis = viewbox.mapViewToScene(QtCore.QPointF(0.0, 1.0))
        scene_x = (
            origin.x()
            + x_values * (x_basis.x() - origin.x())
            + y_values * (y_basis.x() - origin.x())
            )
        scene_y = (
            origin.y()
            + x_values * (x_basis.y() - origin.y())
            + y_values * (y_basis.y() - origin.y())
            )
        distances = np.square(scene_x - scene_pos.x()) + np.square(
            scene_y - scene_pos.y()
            )
        index = int(np.argmin(distances))
        sample = _SnapTraceSample(
            x_value=float(x_values[index]),
            y_value=float(y_values[index]),
            point_number=int(finite_indices[index]) + 1,
            )
        return sample, float(distances[index])


    def _viewbox_for_line(self, line):
        """
        Returns the viewbox that owns a plotted line.

        """
        get_viewbox = getattr(line, "getViewBox", None)
        if callable(get_viewbox):
            viewbox = get_viewbox()
            if viewbox is not None:
                return viewbox
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
