from collections.abc import Callable
from math import isclose, isfinite, log10
from typing import TYPE_CHECKING, Any, Literal

import numpy as np
import numpy.typing as npt
import pyqtgraph as pg
from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw
from pyqtgraph.graphicsItems.ViewBox import axisCtrlTemplate_generic

_AxisName = Literal["x", "y", "x2", "y2"]
_AXIS_MENU_ITEMS: tuple[tuple[_AxisName, str], ...] = (
    ("x", "X axis"),
    ("y", "Y axis"),
)
_AXIS_SPECS: tuple[tuple[_AxisName, str, str], ...] = (
    ("x", "X", "bottom"),
    ("y", "Y", "left"),
    ("x2", "X2", "top"),
    ("y2", "Y2", "right"),
)
_AXIS_DIMENSIONS: dict[_AxisName, Literal["x", "y"]] = {
    "x": "x",
    "y": "y",
    "x2": "x",
    "y2": "y",
}
_AXIS_SIDES: dict[_AxisName, str] = {
    "x": "bottom",
    "y": "left",
    "x2": "top",
    "y2": "right",
}
_AXIS_ALIASES: dict[str, _AxisName] = {
    "x": "x",
    "bottom": "x",
    "y": "y",
    "left": "y",
    "x2": "x2",
    "top": "x2",
    "y2": "y2",
    "right": "y2",
}

_NumericValue = float | npt.ArrayLike

if TYPE_CHECKING:
    class _PlotAxisScalingBase(qtw.QMainWindow):
        _axis_scale_controls: dict[_AxisName, Any]
        _axis_scale_dialog: qtw.QDialog | None
        _axis_scale_tabs: qtw.QTabWidget | None
        plot: Any
        right_vb: Any
        vb: Any
        vbMenu: Any

        def _context_menu_action(self, text: str) -> Any | None: ...
        def _view_range_changed_programmatically(self) -> None: ...
else:
    class _PlotAxisScalingBase:
        pass


def _axis_scale_power_text(scale: float) -> str:
    """
    Return a compact HTML power-of-ten label for an axis display scale.

    """
    if not isfinite(scale) or scale <= 0 or isclose(scale, 1.0):
        return ""

    exponent = round(log10(scale))
    if isclose(scale, 10**exponent, rel_tol=1e-9, abs_tol=0.0):
        return f"10<sup>{exponent}</sup>"

    return f"{scale:g}"


def _flush_axis_draw_specs(axis: pg.AxisItem, specs: Any) -> Any:
    """Move pyqtgraph's axis line onto the edge of its linked view."""

    if specs is None:
        return None

    endpoint_offsets = {
        "left": (QtCore.QPointF(1.0, 1.0), QtCore.QPointF(1.0, -1.0)),
        "right": (QtCore.QPointF(-1.0, 1.0), QtCore.QPointF(-1.0, -1.0)),
        "top": (QtCore.QPointF(1.0, 1.0), QtCore.QPointF(-1.0, 1.0)),
        "bottom": (QtCore.QPointF(1.0, -1.0), QtCore.QPointF(-1.0, -1.0)),
        }
    offsets = endpoint_offsets.get(axis.orientation)
    if offsets is None:
        return specs

    axis_spec, tick_specs, text_specs = specs
    pen, start, end = axis_spec
    start_offset, end_offset = offsets
    flush_axis_spec = (
        pen,
        QtCore.QPointF(start) + start_offset,
        QtCore.QPointF(end) + end_offset,
        )
    return flush_axis_spec, tick_specs, text_specs


def _install_flush_axis_draw_specs(axis: pg.AxisItem) -> None:
    """Remove the one-pixel frame standoff from an existing axis item."""

    if getattr(axis, "_qplot_flush_axis_draw_specs", False):
        return

    original_generate_draw_specs = axis.generateDrawSpecs

    def generate_draw_specs(painter: Any) -> Any:
        return _flush_axis_draw_specs(
            axis,
            original_generate_draw_specs(painter),
            )

    axis.generateDrawSpecs = generate_draw_specs
    axis._qplot_flush_axis_draw_specs = True
    axis.picture = None
    axis.update()


class _PowerScaledAxisItem(pg.AxisItem):
    """
    Display pyqtgraph's auto SI scaling as powers of ten in the axis unit.

    """

    def labelString(self) -> str:
        if self.autoSIPrefix and not isclose(self.autoSIPrefixScale, 1.0):
            unit_scale = 1.0 / self.autoSIPrefixScale
        else:
            unit_scale = 1.0

        scale_text = _axis_scale_power_text(unit_scale)
        if self.labelUnits == "":
            units = f"({scale_text})" if scale_text else ""
        elif scale_text:
            units = f"({scale_text} {self.labelUnits})"
        else:
            units = f"({self.labelUnitPrefix}{self.labelUnits})"

        text = f"{self.labelText} {units}"
        style = ";".join([f"{k}: {self.labelStyle[k]}" for k in self.labelStyle])
        return f"<span style='{style}'>{text}</span>"


class _AutoLimitsButton(qtw.QToolButton):
    """Refresh its prospective-limit tooltip immediately before display."""

    def __init__(self, refresh_tooltip: Callable[[], None], parent: qtw.QWidget):
        super().__init__(parent)
        self._refresh_tooltip = refresh_tooltip

    def event(self, event: QtCore.QEvent) -> bool:
        if event.type() == QtCore.QEvent.Type.ToolTip:
            self._refresh_tooltip()
        return super().event(event)


class PlotAxisScalingMixin(_PlotAxisScalingBase):
    """
    Axis scaling dialogs and controls shared by plot windows.

    This mixin adapts pyqtgraph's embedded ViewBox controls into qPlot dialogs
    opened by double-clicking the plot axes.
    """

    def _init_axis_scale_dialogs(self) -> None:
        """
        Move pyqtgraph's axis scaling controls into one tabbed dialog.

        """
        self._axis_scale_controls = {}
        self._axis_scale_dialog = None
        self._axis_scale_tabs = None
        self._axis_scale_custom_auto_axes: set[_AxisName] = set()
        self._axis_scale_programmatic_change_depth = 0
        self._axis_scale_range_connections: list[tuple[Any, Any]] = []

        for _axis, menu_text in _AXIS_MENU_ITEMS:
            action = self._context_menu_action(menu_text)
            if action is None or action.menu() is None:
                continue

            self.vbMenu.removeAction(action)

        # Log controls now live with the other per-axis controls. The original
        # PlotItem checkboxes are global to both sides and cannot represent X2
        # and Y2 independently.
        plot_controls = getattr(self.plot, "ctrl", None)
        for name in ("logXCheck", "logYCheck"):
            control = getattr(plot_controls, name, None)
            if control is not None:
                control.hide()

        self._install_axis_scale_viewbox_range_handlers(
            self.vb,
            x_axis="x",
            y_axis="y",
        )
        self._install_axis_scale_double_click_handlers()

    def _install_axis_scale_viewbox_range_handlers(
            self,
            viewbox: Any,
            *,
            x_axis: _AxisName | None = None,
            y_axis: _AxisName | None = None,
            ) -> None:
        """Keep dialog fields synchronized with semantic ViewBox dimensions."""

        installed = self.__dict__.setdefault(
            "_axis_scale_range_handler_keys",
            set(),
        )
        connections = self.__dict__.setdefault(
            "_axis_scale_range_connections",
            [],
        )
        for signal_name, axis in (
                ("sigXRangeChanged", x_axis),
                ("sigYRangeChanged", y_axis),
                ):
            if axis is None:
                continue
            key = (id(viewbox), axis)
            if key in installed:
                continue
            signal = getattr(viewbox, signal_name, None)
            if signal is None or not callable(getattr(signal, "connect", None)):
                continue
            slot = lambda *_args, axis=axis: self._axis_scale_range_changed(axis)
            signal.connect(slot)
            connections.append((signal, slot))
            installed.add(key)

    def _axis_scale_range_changed(self, axis: _AxisName) -> None:
        """Reflect wheel, drag, and linked-view changes in an open dialog."""

        if self.__dict__.get("_axis_scale_programmatic_change_depth", 0) == 0:
            self.__dict__.get("_axis_scale_custom_auto_axes", set()).discard(axis)
        if axis in self.__dict__.get("_axis_scale_controls", {}):
            self._sync_axis_scale_controls(axis)

    def force_all_axes_autoscale(self) -> None:
        """Return every active plot axis to automatic scaling mode."""

        for axis, _label, _side in _AXIS_SPECS:
            if not self._axis_scale_axis_is_used(axis):
                continue
            if self._axis_scale_uses_filtered_auto(axis):
                self._apply_axis_scale_filtered_auto(axis)
            else:
                self._axis_scale_viewbox(axis).enableAutoRange(
                    self._axis_scale_axis_constant(axis),
                    True,
                )
            if axis in self.__dict__.get("_axis_scale_controls", {}):
                self._sync_axis_scale_controls(axis)

    def _menu_control_widget(self, menu: qtw.QMenu) -> qtw.QWidget | None:
        """
        Returns the embedded control widget from a QWidgetAction menu.

        """
        for action in menu.actions():
            if isinstance(action, qtw.QWidgetAction):
                return action.defaultWidget()
        return None

    def _install_axis_scale_double_click_handlers(self) -> None:
        """
        Open the relevant axis scale dialog when an axis is double-clicked.

        """
        for axis, _label, side in _AXIS_SPECS:
            axis_item = self.plot.getAxis(side)
            if axis_item is None:
                continue

            previous_handler = getattr(axis_item, "mouseDoubleClickEvent", None)

            def mouse_double_click(
                    event: Any,
                    axis: _AxisName = axis,
                    previous_handler: Any = previous_handler,
                    ) -> None:
                if event.button() == QtCore.Qt.MouseButton.LeftButton:
                    self.open_axis_scale_dialog(axis)
                    event.accept()
                    return

                if previous_handler is not None:
                    previous_handler(event)

            axis_item.mouseDoubleClickEvent = mouse_double_click

    def _axis_scale_dialog_title(self) -> str:
        return "Axis scaling"

    def _axis_scale_dimension(self, axis: _AxisName) -> Literal["x", "y"]:
        return _AXIS_DIMENSIONS[axis]

    @staticmethod
    def _axis_scale_normalise_axis(axis: str) -> _AxisName:
        """Return the semantic axis represented by a name or physical side."""

        semantic_axis = _AXIS_ALIASES.get(axis.lower())
        if semantic_axis is None:
            raise ValueError(f"Unknown plot axis: {axis!r}")
        return semantic_axis

    @staticmethod
    def _axis_scale_transform_result(
            source: _NumericValue,
            result: npt.NDArray[np.float64],
            ) -> float | npt.NDArray[np.float64]:
        """Keep scalar transforms scalar while supporting array-like inputs."""

        if np.ndim(source) == 0:
            return float(result)
        return result

    def data_to_view(
            self,
            axis: str,
            values: _NumericValue,
            ) -> float | npt.NDArray[np.float64]:
        """Convert physical data values to coordinates used by a ViewBox.

        Linear axes are unchanged. Log axes match ``PlotDataItem``: only
        positive finite samples have display coordinates; nonpositive and
        non-finite raw samples map to NaN and therefore do not participate in
        geometry or hit testing.
        """

        semantic_axis = self._axis_scale_normalise_axis(axis)
        numeric = np.asarray(values, dtype=float)
        if not self._axis_scale_log_mode(semantic_axis):
            result = numeric.copy()
        else:
            result = np.full(numeric.shape, np.nan, dtype=float)
            valid = np.isfinite(numeric) & (numeric > 0)
            with np.errstate(divide="ignore", invalid="ignore"):
                np.log10(numeric, out=result, where=valid)
        return self._axis_scale_transform_result(values, result)

    def view_to_data(
            self,
            axis: str,
            values: _NumericValue,
            ) -> float | npt.NDArray[np.float64]:
        """Convert ViewBox coordinates to physical data values.

        For log axes this is the extended-real inverse of ``log10``: NaN
        remains NaN, positive infinity remains infinity, and negative
        infinity maps to zero. Finite overflow deliberately becomes infinity.
        """

        semantic_axis = self._axis_scale_normalise_axis(axis)
        numeric = np.asarray(values, dtype=float)
        if not self._axis_scale_log_mode(semantic_axis):
            result = numeric.copy()
        else:
            with np.errstate(over="ignore", invalid="ignore"):
                result = np.asarray(np.power(10.0, numeric), dtype=float)
        return self._axis_scale_transform_result(values, result)

    def _axis_scale_axis_for_line(
            self,
            line: Any,
            dimension: Literal["x", "y"],
            ) -> _AxisName:
        """Return the semantic axis assigned to one plotted line dimension."""

        styles = self.__dict__.get("_trace_styles")
        lines = self.__dict__.get("lines")
        style = None
        if isinstance(styles, dict) and isinstance(lines, dict):
            trace_key = next(
                (key for key, candidate in lines.items() if candidate is line),
                None,
            )
            if trace_key is not None:
                style = styles.get(trace_key)

        if dimension == "x":
            return "x2" if getattr(style, "x_axis", "Bottom") == "Top" else "x"
        return "y2" if getattr(style, "y_axis", "Left") == "Right" else "y"

    def _axis_scale_viewbox(self, axis: _AxisName) -> Any:
        if axis == "x2":
            top_vb = self.__dict__.get("top_vb")
            if top_vb is not None:
                return top_vb
        if axis == "y2":
            right_vb = self.__dict__.get("right_vb")
            if right_vb is not None:
                return right_vb
        return self.vb

    def _axis_scale_axis_number(self, axis: _AxisName) -> int:
        return 0 if self._axis_scale_dimension(axis) == "x" else 1

    def _axis_scale_axis_constant(self, axis: _AxisName) -> int:
        if self._axis_scale_dimension(axis) == "x":
            return pg.ViewBox.XAxis
        return pg.ViewBox.YAxis

    def _new_axis_scale_controls(self, axis: _AxisName) -> qtw.QWidget:
        """
        Build a fresh copy of pyqtgraph's axis scaling controls for a dialog.

        """
        widget = qtw.QWidget()
        ui: Any = axisCtrlTemplate_generic.Ui_Form()
        ui.setupUi(widget)
        widget.setFixedWidth(280)

        ui.mouseCheck.setText("Allow Zoom/Pan")
        ui.mouseCheck.setToolTip(
            "Allow mouse-wheel zooming and drag-panning for this axis."
        )
        ui.invertCheck.setText("Reverse Axis")
        ui.invertCheck.setToolTip(
            "Reverse this axis so values increase in the opposite direction."
        )
        ui.invertCheck.setMinimumWidth(ui.invertCheck.sizeHint().width())
        ui.mouseCheck.setMinimumWidth(ui.mouseCheck.sizeHint().width())
        ui.visibleOnlyCheck.setText("Autoscale from Visible Data")
        ui.visibleOnlyCheck.setToolTip(
            "When autoscaling, use only data visible within the other axis's "
            "current range."
        )
        ui.autoPanCheck.setText("Follow New Data, Keep Span")
        ui.autoPanCheck.setToolTip(
            "Follow new data by moving this axis without changing its displayed span."
        )
        ui.logCheck = qtw.QCheckBox("Log Scale", widget)
        ui.logCheck.setObjectName("axisScaleLogCheck")
        ui.logCheck.setToolTip(
            "Display this axis on a base-10 logarithmic scale. Values at or "
            "below zero are not shown."
        )
        ui.autoPercentSpin.setToolTip(
            "Percentage of the data range included during autoscaling. Lower "
            "values can reduce the influence of extreme spikes."
        )
        ui.linkCombo.setToolTip(
            "Match this axis's displayed range to the corresponding axis in "
            "another linked plot."
        )

        # The upstream form is intentionally very dense and relies on the
        # order of two unlabelled edits to communicate minimum/maximum.  Keep
        # its controls and behaviour, but arrange them more clearly for the
        # standalone qPlot dialog.
        while ui.gridLayout.takeAt(0) is not None:
            pass
        ui.gridLayout.setContentsMargins(8, 8, 8, 8)
        ui.gridLayout.setHorizontalSpacing(6)
        ui.gridLayout.setVerticalSpacing(5)

        ui.minimumLabel = qtw.QLabel("Minimum", widget)
        ui.minimumLabel.setObjectName("axisScaleMinimumLabel")
        ui.maximumLabel = qtw.QLabel("Maximum", widget)
        ui.maximumLabel.setObjectName("axisScaleMaximumLabel")
        ui.minimumLabel.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft)
        ui.maximumLabel.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft)

        ui.copyAutoLimitsButton = _AutoLimitsButton(
            lambda axis=axis: self._update_axis_scale_auto_limits_tooltip(axis),
            widget,
        )
        ui.copyAutoLimitsButton.setObjectName("copyAutoLimitsButton")
        ui.copyAutoLimitsButton.setAccessibleName("Use auto limits as manual limits")
        ui.copyAutoLimitsButton.setFixedSize(26, 24)
        style = qtw.QApplication.style()
        if style is not None:
            ui.copyAutoLimitsButton.setIcon(
                style.standardIcon(qtw.QStyle.StandardPixmap.SP_BrowserReload)
            )

        ui.rangeSeparator = qtw.QFrame(widget)
        ui.rangeSeparator.setObjectName("axisScaleRangeSeparator")
        ui.rangeSeparator.setFrameShape(qtw.QFrame.Shape.HLine)
        ui.rangeSeparator.setFrameShadow(qtw.QFrame.Shadow.Sunken)
        ui.linkSeparator = qtw.QFrame(widget)
        ui.linkSeparator.setObjectName("axisScaleLinkSeparator")
        ui.linkSeparator.setFrameShape(qtw.QFrame.Shape.HLine)
        ui.linkSeparator.setFrameShadow(qtw.QFrame.Shadow.Sunken)

        ui.gridLayout.addWidget(ui.minimumLabel, 0, 1)
        ui.gridLayout.addWidget(ui.maximumLabel, 0, 2)
        ui.gridLayout.addWidget(ui.manualRadio, 1, 0)
        ui.gridLayout.addWidget(ui.minText, 1, 1)
        ui.gridLayout.addWidget(ui.maxText, 1, 2)
        ui.gridLayout.addWidget(ui.copyAutoLimitsButton, 1, 3)
        ui.gridLayout.addWidget(ui.autoRadio, 2, 0)
        ui.gridLayout.addWidget(ui.autoPercentSpin, 2, 1, 1, 2)
        ui.gridLayout.addWidget(ui.visibleOnlyCheck, 3, 1, 1, 3)
        ui.gridLayout.addWidget(ui.autoPanCheck, 4, 1, 1, 3)
        ui.gridLayout.addWidget(ui.rangeSeparator, 5, 0, 1, 4)
        ui.gridLayout.addWidget(ui.logCheck, 6, 0, 1, 4)
        ui.gridLayout.addWidget(ui.invertCheck, 7, 0, 1, 2)
        ui.gridLayout.addWidget(ui.mouseCheck, 7, 2, 1, 2)
        ui.gridLayout.addWidget(ui.linkSeparator, 8, 0, 1, 4)
        ui.gridLayout.addWidget(ui.label, 9, 0)
        ui.gridLayout.addWidget(ui.linkCombo, 9, 1, 1, 3)
        ui.gridLayout.setColumnStretch(1, 1)
        ui.gridLayout.setColumnStretch(2, 1)
        self._axis_scale_controls[axis] = ui

        ui.mouseCheck.toggled.connect(
            lambda checked, axis=axis: self._axis_scale_mouse_toggled(axis, checked)
            )
        ui.manualRadio.clicked.connect(
            lambda _checked=False, axis=axis: self._axis_scale_manual_clicked(axis)
            )
        ui.minText.editingFinished.connect(
            lambda axis=axis: self._axis_scale_range_text_changed(axis)
            )
        ui.maxText.editingFinished.connect(
            lambda axis=axis: self._axis_scale_range_text_changed(axis)
            )
        ui.autoRadio.clicked.connect(
            lambda _checked=False, axis=axis: self._axis_scale_auto_clicked(axis)
            )
        ui.autoPercentSpin.valueChanged.connect(
            lambda value, axis=axis: self._axis_scale_auto_spin_changed(axis, value)
            )
        ui.linkCombo.currentIndexChanged.connect(
            lambda _index, axis=axis: self._axis_scale_link_changed(axis)
            )
        ui.autoPanCheck.toggled.connect(
            lambda checked, axis=axis: self._axis_scale_auto_pan_toggled(axis, checked)
            )
        ui.visibleOnlyCheck.toggled.connect(
            lambda checked, axis=axis: self._axis_scale_visible_only_toggled(axis, checked)
            )
        ui.invertCheck.toggled.connect(
            lambda checked, axis=axis: self._axis_scale_invert_toggled(axis, checked)
            )
        ui.logCheck.toggled.connect(
            lambda checked, axis=axis: self._axis_scale_log_toggled(axis, checked)
            )
        ui.copyAutoLimitsButton.clicked.connect(
            lambda _checked=False, axis=axis: self._axis_scale_copy_auto_limits(axis)
        )

        return widget

    def _sync_axis_scale_controls(self, axis: _AxisName) -> None:
        """
        Update a dialog's controls from the current view state.

        """
        ui = self._axis_scale_controls.get(axis)
        if ui is None:
            return

        axis_number = self._axis_scale_axis_number(axis)
        viewbox = self._axis_scale_viewbox(axis)
        state = viewbox.getState(copy=False)

        for widget in (
                ui.minText,
                ui.maxText,
                ui.manualRadio,
                ui.autoRadio,
                ui.autoPercentSpin,
                ui.linkCombo,
                ui.autoPanCheck,
                ui.visibleOnlyCheck,
                ui.invertCheck,
                ui.logCheck,
                ui.mouseCheck,
                ):
            widget.blockSignals(True)

        try:
            target_range = state["targetRange"][axis_number]
            data_range = self.view_to_data(axis, target_range)
            ui.minText.setText(f"{data_range[0]:.5g}")
            ui.maxText.setText(f"{data_range[1]:.5g}")

            auto_range = (
                True
                if axis in self.__dict__.get("_axis_scale_custom_auto_axes", set())
                else state["autoRange"][axis_number]
            )
            ui.autoRadio.setChecked(auto_range is not False)
            ui.manualRadio.setChecked(auto_range is False)
            if auto_range is not False and auto_range is not True:
                ui.autoPercentSpin.setValue(int(auto_range * 100))

            ui.mouseCheck.setChecked(state["mouseEnabled"][axis_number])
            ui.autoPanCheck.setChecked(state["autoPan"][axis_number])
            ui.visibleOnlyCheck.setChecked(state["autoVisibleOnly"][axis_number])
            dimension = self._axis_scale_dimension(axis)
            ui.invertCheck.setChecked(state.get(dimension + "Inverted", False))
            axis_item = self.plot.getAxis(_AXIS_SIDES[axis])
            ui.logCheck.setChecked(bool(getattr(axis_item, "logMode", False)))
            log_supported = self._axis_scale_log_is_supported(axis)
            ui.logCheck.setEnabled(log_supported)
            if log_supported:
                ui.logCheck.setToolTip(
                    "Display this axis on a base-10 logarithmic scale. Values "
                    "at or below zero are not shown."
                )
            else:
                ui.logCheck.setToolTip(
                    "Log scaling is available for line-plot axes."
                )
            self._sync_axis_scale_link_combo(axis)
            self._update_axis_scale_auto_limits_tooltip(axis)
        finally:
            for widget in (
                    ui.minText,
                    ui.maxText,
                    ui.manualRadio,
                    ui.autoRadio,
                    ui.autoPercentSpin,
                    ui.linkCombo,
                    ui.autoPanCheck,
                    ui.visibleOnlyCheck,
                    ui.invertCheck,
                    ui.logCheck,
                    ui.mouseCheck,
                    ):
                widget.blockSignals(False)

    def _sync_axis_scale_link_combo(self, axis: _AxisName) -> None:
        """
        Mirror pyqtgraph's available linked views into the dialog link combo.

        """
        ui = self._axis_scale_controls[axis]
        axis_number = self._axis_scale_axis_number(axis)
        viewbox = self._axis_scale_viewbox(axis)
        menu = self.vbMenu if viewbox is self.vb else getattr(viewbox, "menu", None)
        source_combo = getattr(menu, "ctrl", [None, None])[axis_number]
        source_combo = getattr(source_combo, "linkCombo", None)
        current = viewbox.getState(copy=False)["linkedViews"][axis_number] or ""

        ui.linkCombo.clear()
        if source_combo is None:
            return
        for index in range(source_combo.count()):
            ui.linkCombo.addItem(source_combo.itemText(index))

        index = ui.linkCombo.findText(current)
        ui.linkCombo.setCurrentIndex(max(index, 0))

    def _axis_scale_bound_item_groups(
        self,
        axis: _AxisName,
    ) -> list[tuple[Any, list[Any] | None]]:
        """Group plot items assigned to an axis by their containing ViewBox."""

        styles = self.__dict__.get("_trace_styles")
        lines = self.__dict__.get("lines")
        if not isinstance(styles, dict) or not isinstance(lines, dict):
            assignments = self.__dict__.get("_heatmap_axis_assignments")
            heatmaps = self.__dict__.get("heatmaps")
            axis_sides = getattr(self, "_heatmap_axis_sides", None)
            render_items = getattr(self, "_heatmap_render_items", None)
            axis_viewbox = getattr(self, "_heatmap_axis_viewbox", None)
            if (
                isinstance(assignments, dict)
                and isinstance(heatmaps, dict)
                and callable(axis_sides)
                and callable(render_items)
                and callable(axis_viewbox)
            ):
                dimension = self._axis_scale_dimension(axis)
                expected = {
                    "x": "Bottom",
                    "x2": "Top",
                    "y": "Left",
                    "y2": "Right",
                }[axis]
                heatmap_groups: dict[Any, list[Any]] = {}
                for layer in heatmaps.values():
                    x_side, y_side = axis_sides(layer)
                    selected = x_side if dimension == "x" else y_side
                    if selected != expected:
                        continue
                    viewbox = axis_viewbox(layer)
                    heatmap_groups.setdefault(viewbox, []).extend(
                        render_items(layer)
                    )
                return list(heatmap_groups.items())
            return [(self._axis_scale_viewbox(axis), None)]

        attribute, value = {
            "x": ("x_axis", "Bottom"),
            "y": ("y_axis", "Left"),
            "x2": ("x_axis", "Top"),
            "y2": ("y_axis", "Right"),
        }[axis]
        grouped: dict[Any, list[Any]] = {}
        for trace_key, line in lines.items():
            style = styles.get(trace_key)
            if (
                line is None
                or getattr(style, attribute, None) != value
                ):
                continue
            get_viewbox = getattr(line, "getViewBox", None)
            viewbox = get_viewbox() if callable(get_viewbox) else None
            if viewbox is None:
                uses_top = getattr(style, "x_axis", "Bottom") == "Top"
                uses_right = getattr(style, "y_axis", "Left") == "Right"
                if uses_top and uses_right:
                    viewbox = self.__dict__.get("top_right_vb")
                elif uses_top:
                    viewbox = self.__dict__.get("top_vb")
                elif uses_right:
                    viewbox = self.__dict__.get("right_vb")
                viewbox = viewbox or self.vb
            grouped.setdefault(viewbox, []).append(line)
        return list(grouped.items())

    def _axis_scale_auto_limits(self, axis: _AxisName) -> tuple[float, float] | None:
        """Calculate this tab's auto range without changing the viewbox."""

        ui = self.__dict__.get("_axis_scale_controls", {}).get(axis)
        viewbox = self._axis_scale_viewbox(axis)
        axis_number = self._axis_scale_axis_number(axis)
        state = viewbox.getState(copy=False)
        current_ranges = viewbox.viewRange()
        fractions = [1.0, 1.0]
        auto_range = state["autoRange"][axis_number]
        default_fraction = (
            float(auto_range)
            if isinstance(auto_range, (int, float))
            and not isinstance(auto_range, bool)
            else 1.0
        )
        fractions[axis_number] = (
            ui.autoPercentSpin.value() * 0.01
            if ui is not None
            else default_fraction
        )
        visible_only = (
            ui.visibleOnlyCheck.isChecked()
            if ui is not None
            else state["autoVisibleOnly"][axis_number]
        )
        auto_pan = (
            ui.autoPanCheck.isChecked()
            if ui is not None
            else state["autoPan"][axis_number]
        )
        ranges: list[list[float]] = []
        for bounds_viewbox, items in self._axis_scale_bound_item_groups(axis):
            orthogonal_ranges: list[list[float] | None] = [None, None]
            if visible_only:
                orthogonal_ranges[axis_number] = bounds_viewbox.viewRange()[
                    1 - axis_number
                ]
            bounds = bounds_viewbox.childrenBounds(
                frac=fractions,
                orthoRange=orthogonal_ranges,
                items=items,
            )[axis_number]
            if bounds is not None and all(isfinite(value) for value in bounds):
                ranges.append(bounds)
        if not ranges:
            return None

        lower = min(float(bounds[0]) for bounds in ranges)
        upper = max(float(bounds[1]) for bounds in ranges)
        current_span = current_ranges[axis_number][1] - current_ranges[axis_number][0]
        if auto_pan or lower == upper:
            if not isfinite(current_span) or current_span <= 0:
                current_span = 1.0
            center = (lower + upper) * 0.5
            lower = center - current_span * 0.5
            upper = center + current_span * 0.5
        else:
            padding = viewbox.suggestPadding(axis_number)
            extra = (upper - lower) * padding
            lower -= extra
            upper += extra

        if not all(isfinite(value) for value in (lower, upper)) or lower >= upper:
            return None
        return lower, upper

    def _update_axis_scale_auto_limits_tooltip(self, axis: _AxisName) -> None:
        """Describe the range that the copy-auto button would apply."""

        button = self._axis_scale_controls[axis].copyAutoLimitsButton
        limits = self._axis_scale_auto_limits(axis)
        data_limits = (
            None
            if limits is None
            else tuple(float(value) for value in self.view_to_data(axis, limits))
        )
        usable = (
            data_limits is not None
            and all(isfinite(value) for value in data_limits)
            and data_limits[0] < data_limits[1]
        )
        button.setEnabled(usable)
        if not usable:
            button.setToolTip("Auto limits are unavailable for this axis.")
            return
        assert data_limits is not None
        button.setToolTip(
            f"Set manual limits to {data_limits[0]:.5g} and {data_limits[1]:.5g}."
        )

    def _axis_scale_copy_auto_limits(self, axis: _AxisName) -> None:
        """Freeze the prospective auto range as this axis's manual range."""

        limits = self._axis_scale_auto_limits(axis)
        if limits is None:
            return
        data_limits = tuple(
            float(value) for value in self.view_to_data(axis, limits)
        )
        if (
                not all(isfinite(value) for value in data_limits)
                or data_limits[0] >= data_limits[1]
                ):
            self._update_axis_scale_auto_limits_tooltip(axis)
            return
        ui = self._axis_scale_controls[axis]
        ui.minText.setText(f"{data_limits[0]:.5g}")
        ui.maxText.setText(f"{data_limits[1]:.5g}")
        self._axis_scale_range_text_changed(axis)
        self._update_axis_scale_auto_limits_tooltip(axis)

    def _axis_scale_mouse_toggled(self, axis: _AxisName, checked: bool) -> None:
        viewbox = self._axis_scale_viewbox(axis)
        if self._axis_scale_dimension(axis) == "x":
            viewbox.setMouseEnabled(x=checked)
        else:
            viewbox.setMouseEnabled(y=checked)

    def _axis_scale_manual_clicked(self, axis: _AxisName) -> None:
        self.__dict__.get("_axis_scale_custom_auto_axes", set()).discard(axis)
        self._axis_scale_viewbox(axis).enableAutoRange(
            self._axis_scale_axis_constant(axis),
            False,
        )

    def _axis_scale_range_text_changed(self, axis: _AxisName) -> None:
        ui = self._axis_scale_controls[axis]
        axis_number = self._axis_scale_axis_number(axis)
        viewbox = self._axis_scale_viewbox(axis)
        previous_view_values = list(viewbox.viewRange()[axis_number])
        previous_values = self.view_to_data(axis, previous_view_values)
        try:
            values = [float(ui.minText.text()), float(ui.maxText.text())]
        except ValueError:
            values = []

        if (
                len(values) != 2
                or not all(isfinite(value) for value in values)
                or values[0] >= values[1]
                ):
            ui.minText.setText(f"{previous_values[0]:.5g}")
            ui.maxText.setText(f"{previous_values[1]:.5g}")
            show_status = getattr(self, "show_status", None)
            if callable(show_status):
                show_status(
                    "Axis limits must be finite numbers with minimum below maximum.",
                    5000,
                    )
            return

        if self._axis_scale_log_mode(axis) and any(value <= 0 for value in values):
            ui.minText.setText(f"{previous_values[0]:.5g}")
            ui.maxText.setText(f"{previous_values[1]:.5g}")
            show_status = getattr(self, "show_status", None)
            if callable(show_status):
                show_status(
                    "Log-scale axis limits must be greater than zero.",
                    5000,
                    )
            return

        view_values = [
            float(value) for value in self.data_to_view(axis, values)
        ]

        ui.manualRadio.setChecked(True)
        self.__dict__.get("_axis_scale_custom_auto_axes", set()).discard(axis)
        if self._axis_scale_dimension(axis) == "x":
            viewbox.setXRange(*view_values, padding=0)
        else:
            viewbox.setYRange(*view_values, padding=0)
        self._view_range_changed_programmatically()

    def _axis_scale_uses_filtered_auto(self, axis: _AxisName) -> bool:
        """Return whether auto-ranging must respect per-item axis assignment."""

        return (
            (
                isinstance(self.__dict__.get("_trace_styles"), dict)
                and isinstance(self.__dict__.get("lines"), dict)
            )
            or (
                isinstance(
                    self.__dict__.get("_heatmap_axis_assignments"),
                    dict,
                )
                and isinstance(self.__dict__.get("heatmaps"), dict)
            )
        )

    def _apply_axis_scale_filtered_auto(self, axis: _AxisName) -> None:
        """Apply an item-filtered auto range while retaining Auto mode in the UI."""

        limits = self._axis_scale_auto_limits(axis)
        if limits is None:
            return
        viewbox = self._axis_scale_viewbox(axis)
        axis_constant = self._axis_scale_axis_constant(axis)
        self._axis_scale_programmatic_change_depth = (
            self.__dict__.get("_axis_scale_programmatic_change_depth", 0) + 1
        )
        try:
            for bounds_viewbox, _items in self._axis_scale_bound_item_groups(axis):
                bounds_viewbox.enableAutoRange(axis_constant, False)
            if self._axis_scale_dimension(axis) == "x":
                viewbox.setXRange(*limits, padding=0)
            else:
                viewbox.setYRange(*limits, padding=0)
        finally:
            self._axis_scale_programmatic_change_depth -= 1
        self.__dict__.setdefault("_axis_scale_custom_auto_axes", set()).add(axis)
        ui = self.__dict__.get("_axis_scale_controls", {}).get(axis)
        if ui is not None:
            ui.autoRadio.setChecked(True)
        self._view_range_changed_programmatically()

    def _install_axis_scale_trace_handler(self, line: Any) -> None:
        """Keep filtered auto ranges current when a trace publishes new data."""

        if line is None or getattr(line, "_qplot_axis_scale_handler", False):
            return
        signal = getattr(line, "sigPlotChanged", None)
        if signal is None or not callable(getattr(signal, "connect", None)):
            return
        signal.connect(self._axis_scale_trace_data_changed)
        line._qplot_axis_scale_handler = True

    def _axis_scale_trace_data_changed(self, _line: Any = None) -> None:
        """Reapply active trace-filtered auto ranges after a data update."""

        for axis in tuple(
            self.__dict__.get("_axis_scale_custom_auto_axes", set())
        ):
            if self._axis_scale_axis_is_used(axis):
                self._apply_axis_scale_filtered_auto(axis)

    def _axis_scale_auto_clicked(self, axis: _AxisName) -> None:
        ui = self._axis_scale_controls[axis]
        if self._axis_scale_uses_filtered_auto(axis):
            self._apply_axis_scale_filtered_auto(axis)
            return
        self._axis_scale_viewbox(axis).enableAutoRange(
            self._axis_scale_axis_constant(axis),
            ui.autoPercentSpin.value() * 0.01,
            )

    def _axis_scale_auto_spin_changed(self, axis: _AxisName, value: float) -> None:
        ui = self._axis_scale_controls[axis]
        ui.autoRadio.setChecked(True)
        if self._axis_scale_uses_filtered_auto(axis):
            self._apply_axis_scale_filtered_auto(axis)
            return
        self._axis_scale_viewbox(axis).enableAutoRange(
            self._axis_scale_axis_constant(axis),
            value * 0.01,
        )

    def _axis_scale_link_changed(self, axis: _AxisName) -> None:
        ui = self._axis_scale_controls[axis]
        viewbox = self._axis_scale_viewbox(axis)
        if self._axis_scale_dimension(axis) == "x":
            viewbox.setXLink(str(ui.linkCombo.currentText()))
        else:
            viewbox.setYLink(str(ui.linkCombo.currentText()))

    def _axis_scale_auto_pan_toggled(self, axis: _AxisName, checked: bool) -> None:
        if axis in self.__dict__.get("_axis_scale_custom_auto_axes", set()):
            self._apply_axis_scale_filtered_auto(axis)
            return
        viewbox = self._axis_scale_viewbox(axis)
        if self._axis_scale_dimension(axis) == "x":
            viewbox.setAutoPan(x=checked)
        else:
            viewbox.setAutoPan(y=checked)

    def _axis_scale_visible_only_toggled(self, axis: _AxisName, checked: bool) -> None:
        if axis in self.__dict__.get("_axis_scale_custom_auto_axes", set()):
            self._apply_axis_scale_filtered_auto(axis)
            return
        viewbox = self._axis_scale_viewbox(axis)
        if self._axis_scale_dimension(axis) == "x":
            viewbox.setAutoVisible(x=checked)
        else:
            viewbox.setAutoVisible(y=checked)

    def _axis_scale_invert_toggled(self, axis: _AxisName, checked: bool) -> None:
        viewbox = self._axis_scale_viewbox(axis)
        if self._axis_scale_dimension(axis) == "x":
            viewbox.invertX(checked)
        else:
            viewbox.invertY(checked)

    def _axis_scale_log_is_supported(self, axis: _AxisName) -> bool:
        """Return whether this axis contains data items that support log mode."""

        if getattr(self, "operation_kind", None) == "plot2d":
            return False
        for _viewbox, items in self._axis_scale_bound_item_groups(axis):
            candidates = items
            if candidates is None:
                candidates = list(getattr(self.plot, "dataItems", []))
            if any(callable(getattr(item, "setLogMode", None)) for item in candidates):
                return True
        return False

    def _axis_scale_log_mode(self, axis: _AxisName) -> bool:
        """Return the current log state from the displayed axis item."""

        plot = self.__dict__.get("plot")
        get_axis = getattr(plot, "getAxis", None)
        if not callable(get_axis):
            return False
        axis_item = get_axis(_AXIS_SIDES[axis])
        return bool(getattr(axis_item, "logMode", False))

    def _sync_axis_scale_line_log_mode(self, trace_key: Any, line: Any) -> None:
        """Apply the log modes of a trace's assigned axes to that trace."""

        set_log_mode = getattr(line, "setLogMode", None)
        if not callable(set_log_mode):
            return
        style = self.__dict__.get("_trace_styles", {}).get(trace_key)
        x_axis: _AxisName = (
            "x2" if getattr(style, "x_axis", "Bottom") == "Top" else "x"
        )
        y_axis: _AxisName = (
            "y2" if getattr(style, "y_axis", "Left") == "Right" else "y"
        )
        set_log_mode(
            self._axis_scale_log_mode(x_axis),
            self._axis_scale_log_mode(y_axis),
        )

    def _axis_scale_log_toggled(self, axis: _AxisName, checked: bool) -> None:
        """Apply a tab's log state to its ticks and assigned line traces."""

        if not self._axis_scale_log_is_supported(axis):
            self._sync_axis_scale_controls(axis)
            return

        self.plot.getAxis(_AXIS_SIDES[axis]).setLogMode(checked)
        if axis in ("x", "y"):
            control_name = "logXCheck" if axis == "x" else "logYCheck"
            plot_control = getattr(
                getattr(self.plot, "ctrl", None),
                control_name,
                None,
            )
            if plot_control is not None:
                plot_control.blockSignals(True)
                try:
                    plot_control.setChecked(checked)
                finally:
                    plot_control.blockSignals(False)
        styles = self.__dict__.get("_trace_styles")
        lines = self.__dict__.get("lines")
        if isinstance(styles, dict) and isinstance(lines, dict):
            for trace_key, line in lines.items():
                style = styles.get(trace_key)
                if self._axis_scale_dimension(axis) == "x":
                    assigned_axis = (
                        "x2"
                        if getattr(style, "x_axis", "Bottom") == "Top"
                        else "x"
                    )
                else:
                    assigned_axis = (
                        "y2"
                        if getattr(style, "y_axis", "Left") == "Right"
                        else "y"
                    )
                if assigned_axis == axis:
                    self._sync_axis_scale_line_log_mode(trace_key, line)
        else:
            axis_number = self._axis_scale_axis_number(axis)
            for item in getattr(self.plot, "dataItems", []):
                set_log_mode = getattr(item, "setLogMode", None)
                if not callable(set_log_mode):
                    continue
                current = list(getattr(item, "opts", {}).get("logMode", (False, False)))
                current[axis_number] = checked
                set_log_mode(bool(current[0]), bool(current[1]))

        if self._axis_scale_uses_filtered_auto(axis):
            self._apply_axis_scale_filtered_auto(axis)
        else:
            self._axis_scale_viewbox(axis).enableAutoRange(
                self._axis_scale_axis_constant(axis),
                True,
            )
        self._sync_axis_scale_controls(axis)
        self._view_range_changed_programmatically()

    def _axis_scale_axis_is_used(self, axis: _AxisName) -> bool:
        """Return whether the plot currently has an item on this axis."""

        styles = self.__dict__.get("_trace_styles")
        lines = self.__dict__.get("lines")
        if not isinstance(styles, dict) or not isinstance(lines, dict):
            assignments = self.__dict__.get("_heatmap_axis_assignments")
            if isinstance(assignments, dict):
                attribute, value = {
                    "x": ("x", "Bottom"),
                    "y": ("y", "Left"),
                    "x2": ("x", "Top"),
                    "y2": ("y", "Right"),
                }[axis]
                return any(
                    assignment.get(attribute) == value
                    for assignment in assignments.values()
                )
            return axis in ("x", "y")

        attribute, value = {
            "x": ("x_axis", "Bottom"),
            "y": ("y_axis", "Left"),
            "x2": ("x_axis", "Top"),
            "y2": ("y_axis", "Right"),
        }[axis]
        return any(
            line is not None
            and getattr(styles.get(trace_key), attribute, None) == value
            for trace_key, line in lines.items()
        )

    def _create_axis_scale_dialog(self) -> qtw.QDialog:
        """Create the shared four-tab scaling dialog."""

        dialog = qtw.QDialog(self)
        dialog.setWindowTitle(self._axis_scale_dialog_title())
        layout = qtw.QVBoxLayout(dialog)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        tabs = qtw.QTabWidget(dialog)
        tabs.setObjectName("axisScaleTabs")
        tabs.setFixedWidth(300)
        tabs.tabBar().setExpanding(True)
        tabs.tabBar().setUsesScrollButtons(False)
        tabs.setStyleSheet(
            "QTabBar::tab { min-width: 57px; padding-left: 8px; "
            "padding-right: 8px; } "
            "QTabBar::tab:selected { background: palette(highlight); "
            "color: palette(highlighted-text); font-weight: 600; }"
        )
        tab_tooltips = {
            "x": "Bottom horizontal axis",
            "y": "Left vertical axis",
            "x2": "Top horizontal axis",
            "y2": "Right vertical axis",
        }
        for axis, label, _side in _AXIS_SPECS:
            index = tabs.addTab(self._new_axis_scale_controls(axis), label)
            tabs.setTabToolTip(index, tab_tooltips[axis])
        tabs.currentChanged.connect(self._axis_scale_current_tab_changed)
        layout.addWidget(tabs)

        buttons = qtw.QDialogButtonBox(qtw.QDialogButtonBox.StandardButton.Close)
        buttons.setContentsMargins(0, 0, 28, 0)
        buttons.rejected.connect(dialog.close)
        layout.addWidget(buttons)
        layout.setSizeConstraint(qtw.QLayout.SizeConstraint.SetFixedSize)

        self._axis_scale_tabs = tabs
        self._axis_scale_dialog = dialog
        return dialog

    def _axis_scale_current_tab_changed(self, index: int) -> None:
        """Refresh a tab whenever the user switches to it."""

        if not 0 <= index < len(_AXIS_SPECS):
            return
        axis = _AXIS_SPECS[index][0]
        if self._axis_scale_axis_is_used(axis):
            self._sync_axis_scale_controls(axis)

    def _sync_axis_scale_tab_states(self) -> None:
        """Grey out tabs for axes without any assigned trace."""

        tabs = self.__dict__.get("_axis_scale_tabs")
        if tabs is None:
            return
        for index, (axis, _label, _side) in enumerate(_AXIS_SPECS):
            enabled = self._axis_scale_axis_is_used(axis)
            tabs.setTabEnabled(index, enabled)
            if enabled and axis in self._axis_scale_controls:
                self._sync_axis_scale_controls(axis)

    @QtCore.pyqtSlot(str)
    def open_axis_scale_dialog(self, axis: str) -> None:
        """
        Open the shared scaling dialog on the requested axis tab.

        """
        if hasattr(self.vb, "updateViewLists"):
            self.vb.updateViewLists()

        if hasattr(self.vbMenu, "updateState"):
            self.vbMenu.updateState()

        axis_name = _AXIS_ALIASES.get(axis.lower())
        if axis_name is None:
            return

        dialog = self.__dict__.get("_axis_scale_dialog")
        if dialog is None:
            dialog = self._create_axis_scale_dialog()

        self._sync_axis_scale_tab_states()

        tabs = self._axis_scale_tabs
        requested_index = next(
            index
            for index, (current_axis, _label, _side) in enumerate(_AXIS_SPECS)
            if current_axis == axis_name
        )
        if tabs.isTabEnabled(requested_index):
            tabs.setCurrentIndex(requested_index)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
