"""Keep transient plot emphasis tied to the coordinate readout."""

from typing import TYPE_CHECKING, Any, Literal

from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from ._plot_drop_axes import dataset_axis_warning_details
from ._plot_host import PlotHost
from ._plot_hover import PlotHoverHighlight

_AXIS_SIDES = {"x": "bottom", "y": "left", "x2": "top", "y2": "right"}


class _CursorHoverEvents(QtCore.QObject):
    def __init__(self, window: Any):
        super().__init__(window)
        self.window = window

    def eventFilter(self, source, event):
        if (
            event.type() == QtCore.QEvent.Type.MouseMove
            and source is self.window.widget.viewport()
        ):
            pos = self.window.widget.mapToScene(event.position().toPoint())
            # PyQtGraph rate-limits its scene mouse signal and can discard the
            # final move onto an axis. Clear immediately from the raw event.
            if not self.window._cursor_inside_plot(pos):
                self.window._clear_cursor_hover()
        elif event.type() in (
            QtCore.QEvent.Type.Leave,
            QtCore.QEvent.Type.Hide,
            QtCore.QEvent.Type.WindowDeactivate,
        ):
            self.window._clear_cursor_hover()
        return False


if TYPE_CHECKING:
    class _PlotCursorHoverBase(PlotHost, qtw.QMainWindow):
        widget: Any
        plot: Any
        trace_updated: Any

        def _axis_scale_axis_for_line(
            self, line: Any, dimension: Literal["x", "y"],
        ) -> Literal["x", "y", "x2", "y2"]: ...
        def _heatmap_axis_sides(self, layer: Any) -> tuple[str, str]: ...
        def mouseMoved(self, pos: Any) -> None: ...
else:
    class _PlotCursorHoverBase(PlotHost):
        pass


class PlotCursorHoverMixin(_PlotCursorHoverBase):
    def _init_cursor_hover(self):
        self._hover_highlight = PlotHoverHighlight(self)
        self._cursor_hover_refresh_timer = QtCore.QTimer(self)
        self._cursor_hover_refresh_timer.setSingleShot(True)
        self._cursor_hover_refresh_timer.timeout.connect(self._refresh_cursor_hover)
        self._cursor_hover_events = _CursorHoverEvents(self)
        self.widget.viewport().installEventFilter(self._cursor_hover_events)
        self.installEventFilter(self._cursor_hover_events)
        self.trace_updated.connect(self._refresh_cursor_hover)

    def _cursor_inside_plot(self, pos):
        """Exclude axis strokes and inward ticks from the inspection area."""
        viewbox = self.plot.vb
        point = viewbox.mapFromScene(pos)
        # ViewBox.boundingRect includes extra painting space beyond its edge.
        # Use the actual data rectangle, allowing half a pixel for each stroke.
        rect = viewbox.rect()
        margins = {}
        for side in ("left", "top", "right", "bottom"):
            axis = self.plot.getAxis(side)
            margins[side] = (
                max(0.0, -axis.style["tickLength"]) + 0.5
                if axis.isVisible() else 0.5
            )
        return (
            rect.left() + margins["left"] < point.x() < rect.right() - margins["right"]
            and rect.top() + margins["top"] < point.y() < rect.bottom() - margins["bottom"]
        )

    def _active_cursor_highlight(self):
        highlight = self.__dict__.get("_hover_highlight")
        pos = self.__dict__.get("_last_cursor_scene_pos")
        if highlight is not None and (pos is None or not self._cursor_inside_plot(pos)):
            self._clear_cursor_highlight()
            return None
        return highlight

    def _clear_cursor_highlight(self):
        """Free coordinates do not identify a dataset or its assigned axes."""
        self._watch_cursor_trace(None)
        highlight = self.__dict__.get("_hover_highlight")
        if highlight is not None:
            highlight.clear()

    def _highlight_cursor_trace(self, line):
        highlight = self._active_cursor_highlight()
        if highlight is not None:
            if line is None:
                self._clear_cursor_highlight()
                return
            axes = (
                _AXIS_SIDES[self._axis_scale_axis_for_line(line, "x")],
                _AXIS_SIDES[self._axis_scale_axis_for_line(line, "y")],
            )
            highlight.set_trace(line, axes, warning_axes=self._cursor_axis_warnings("trace", line))
            self._watch_cursor_trace(line)

    def _highlight_cursor_heatmap(self):
        highlight = self._active_cursor_highlight()
        if highlight is None:
            return
        self._watch_cursor_trace(None)
        # The heatmap readout samples the primary layer. Its active renderer
        # may be an image or a mesh, depending on the coordinate spacing.
        renderer = next((
            item for name in ("image", "heatmap_mesh")
            if (item := self.__dict__.get(name)) is not None and item.isVisible()
        ), None)
        axes = tuple(side.lower() for side in self._heatmap_axis_sides(self))
        if renderer is None:
            highlight.clear()
        else:
            highlight.set_heatmap(
                renderer, axes, self.__dict__.get("bar"),
                warning_axes=self._cursor_axis_warnings("heatmap", self),
            )

    def _cursor_axis_warnings(self, kind, item):
        registry = self.__dict__.get("lines" if kind == "trace" else "heatmaps", {})
        for key, candidate in registry.items():
            if candidate is item:
                return tuple(
                    warning.axis for warning in dataset_axis_warning_details(self, kind, key)
                )
        return ()

    def _watch_cursor_trace(self, line):
        """Track updates from an inspected secondary trace as well as the main one."""
        previous = self.__dict__.get("_cursor_hover_trace")
        if previous is line:
            return
        if previous is not None:
            try:
                previous.sigPlotChanged.disconnect(self._schedule_cursor_hover_refresh)
            except (TypeError, RuntimeError):
                pass
        self._cursor_hover_trace = line
        if line is not None:
            line.sigPlotChanged.connect(self._schedule_cursor_hover_refresh)

    def _schedule_cursor_hover_refresh(self, *_args):
        # Data changes can occur inside a render/update callback. Re-evaluate
        # the readout once that update has finished, without re-entering it.
        self._cursor_hover_refresh_timer.start(0)

    def _refresh_cursor_hover(self):
        """Refresh the readout and its emphasis when the displayed data changes."""
        pos = self.__dict__.get("_last_cursor_scene_pos")
        if pos is not None and "_hover_highlight" in self.__dict__:
            self.mouseMoved(pos)

    def _clear_cursor_hover(self):
        self.__dict__.pop("_last_cursor_scene_pos", None)
        self._clear_cursor_highlight()
        timer = self.__dict__.get("_cursor_hover_refresh_timer")
        if timer is not None:
            timer.stop()
        hide_marker = getattr(self, "_hide_snap_marker", None)
        if callable(hide_marker) and "snap_marker" in self.__dict__:
            hide_marker()
        clear_report = getattr(self, "_clear_snap_report", None)
        # Hide/leave events can arrive after closeEvent released the dataset.
        # Clearing transient graphics must not reacquire metadata for that plot.
        if callable(clear_report) and not self.__dict__.get("_closed", False):
            clear_report()
        hide_pixel = getattr(self, "hide_hover_pixel_outline", None)
        if callable(hide_pixel):
            hide_pixel()
