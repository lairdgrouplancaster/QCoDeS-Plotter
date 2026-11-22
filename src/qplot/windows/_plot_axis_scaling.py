from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from math import isclose, isfinite, log10
from typing import TYPE_CHECKING, Any, Literal
from weakref import ReferenceType, ref

import numpy as np
import numpy.typing as npt
import pyqtgraph as pg
from PyQt6 import QtCore, QtGui, sip
from PyQt6 import QtWidgets as qtw

from qplot.datahandling.numeric_samples import numeric_isfinite

from ._plot_data_viewbox import coordinate_viewbox
from ._plot_host import PlotHost
from ._range_controls import RangeControls, RangeFitError, RangeState

_AxisName = Literal["x", "y", "x2", "y2"]
_TRANSPOSED_AXIS: dict[_AxisName, _AxisName] = {"x": "y", "y": "x", "x2": "y2", "y2": "x2"}
_LINK_TOOLTIP = "Link ranges with an axis in another plot. Choose None for an independent range."


@dataclass(frozen=True)
class _AxisSpec:
    name: _AxisName
    label: str
    side: str
    dimension: Literal["x", "y"]
    tooltip: str

    @property
    def assignment_side(self) -> str:
        return self.side.capitalize()

    @property
    def trace_attribute(self) -> str:
        return f"{self.dimension}_axis"


@dataclass(frozen=True)
class _RangeLink:
    """Identify the target quantity even while its plot is transposed."""

    owner: ReferenceType
    axis: _AxisName

    def endpoint(self) -> tuple[Any, _AxisName] | None:
        owner = self.owner()
        if owner is None or sip.isdeleted(owner):
            return None
        if isinstance(owner, PlotAxisScalingMixin):
            if owner.__dict__.get("_closed", False):
                return None
            side = owner._axis_in_original_orientation(self.axis)
            return owner._axis_scale_viewbox(side), side
        if owner.getViewWidget() is None:
            return None
        return owner, self.axis


@dataclass(frozen=True)
class _AxisSettings:
    limits: tuple[float, float]
    auto: bool | float
    filtered_auto: bool
    editor: RangeState | None
    visible_only: bool
    inverted: bool
    mouse: bool
    log: bool
    linked: _RangeLink | None


_AXIS_SPECS = (
    _AxisSpec("x", "X", "bottom", "x", "Bottom horizontal axis"),
    _AxisSpec("y", "Y", "left", "y", "Left vertical axis"),
    _AxisSpec("x2", "X2", "top", "x", "Top horizontal axis"),
    _AxisSpec("y2", "Y2", "right", "y", "Right vertical axis"),
)
_AXIS_BY_NAME = {spec.name: spec for spec in _AXIS_SPECS}
_AXIS_ALIASES = {
    alias: spec.name for spec in _AXIS_SPECS for alias in (spec.name, spec.side)
}
_AXIS_MENU_ITEMS = tuple(
    (spec.name, f"{spec.label} axis")
    for spec in _AXIS_SPECS if spec.name in ("x", "y")
)

_NumericValue = float | npt.ArrayLike


def _axis_quantity_text(plot: Any, axis: _AxisName) -> str:
    """Describe the plotted quantity in the data units used by the range fields."""
    item = plot.getAxis(_AXIS_BY_NAME[axis].side)
    document = QtGui.QTextDocument()
    document.setHtml(item.labelText)
    label = document.toPlainText().strip() or _AXIS_BY_NAME[axis].tooltip
    units = item.labelUnits
    return f"{label} ({units})" if units else label


class _AxisQuantityLabel(qtw.QLabel):
    """Keep long parameter names from pushing the axis tabs or editor off screen."""

    def sizeHint(self) -> QtCore.QSize:
        size = super().sizeHint()
        size.setWidth(min(size.width(), 32 * self.fontMetrics().averageCharWidth()))
        return size

    def minimumSizeHint(self) -> QtCore.QSize:
        size = super().minimumSizeHint()
        size.setWidth(min(size.width(), 10 * self.fontMetrics().averageCharWidth()))
        return size

    def paintEvent(self, event: QtGui.QPaintEvent | None) -> None:
        qtw.QFrame.paintEvent(self, event)
        painter = QtGui.QPainter(self)
        rect = self.contentsRect()
        text = self.fontMetrics().elidedText(self.text(), QtCore.Qt.TextElideMode.ElideMiddle, rect.width())
        style = self.style()
        assert style is not None
        style.drawItemText(painter, rect, self.alignment(), self.palette(), self.isEnabled(),
                           text, QtGui.QPalette.ColorRole.WindowText)


class _AxisScaleControls(qtw.QWidget):
    """Range and behaviour controls for one semantic axis."""

    def __init__(self, axis: _AxisSpec):
        super().__init__()
        layout = qtw.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.rangeEntry = RangeControls(f"{axis.assignment_side} axis", parent=self)
        self.minText = self.rangeEntry.fields["min"]
        self.maxText = self.rangeEntry.fields["max"]
        self.autoRadio = self.rangeEntry.auto_radio
        self.manualRadio = self.rangeEntry.manual_radio
        self.visibleOnlyCheck = self.rangeEntry.visible_only
        self.copyAutoLimitsButton = self.rangeEntry.fit_button
        layout.addWidget(self.rangeEntry)
        form = qtw.QFormLayout()
        form.setHorizontalSpacing(12)
        form.setFieldGrowthPolicy(qtw.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(qtw.QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop)
        layout.addLayout(form)
        self.behaviourHeading = qtw.QLabel("Axis behaviour", self)
        self.behaviourHeading.setFont(self.rangeEntry.range_heading.font())
        self.behaviourHeading.setMinimumWidth(self.rangeEntry.range_heading.minimumWidth())
        self.logCheck = qtw.QCheckBox("Log scale", self)
        self.logCheck.setObjectName("axisScaleLogCheck")
        self.invertCheck = qtw.QCheckBox("Reverse axis", self)
        self.invertCheck.setToolTip("Reverse this axis so values increase in the opposite direction.")
        self.mouseCheck = qtw.QCheckBox("Allow zoom/pan", self)
        self.mouseCheck.setToolTip("Allow mouse-wheel zooming and drag-panning for this axis.")
        behaviour = qtw.QHBoxLayout()
        behaviour.setSpacing(16)
        for check in (self.logCheck, self.invertCheck, self.mouseCheck):
            behaviour.addWidget(check)
        behaviour.addStretch()
        form.addRow(self.behaviourHeading, behaviour)

        self.linkCombo = qtw.QComboBox(self)
        self.linkCombo.setSizeAdjustPolicy(qtw.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.linkCombo.setMinimumContentsLength(12)
        self.linkCombo.setAccessibleName("Link range to")
        self.linkCombo.setToolTip(_LINK_TOOLTIP)
        self.label = qtw.QLabel("Link range to", self)
        self.label.setBuddy(self.linkCombo)
        form.addRow(self.label, self.linkCombo)
        layout.addStretch()


if TYPE_CHECKING:
    from qplot.tools.heatmap_geometry import HeatmapGeometry

    class _PlotAxisScalingBase(PlotHost, qtw.QMainWindow):
        _axis_selection: dict[str, str]
        _axis_scale_controls: dict[_AxisName, Any]
        _axis_scale_dialog: qtw.QDialog | None
        _axis_scale_editor: qtw.QWidget | None
        _axis_scale_tabs: qtw.QTabBar
        _axis_scale_stack: qtw.QStackedWidget
        _axis_scale_quantity: qtw.QLabel
        plot: Any
        right_vb: Any
        vb: Any
        vbMenu: Any
        dataGrid: Any

        def _heatmap_axis_sides(self, layer: Any) -> tuple[str, str]: ...
        def _heatmap_is_visible(self, layer: Any) -> bool: ...
        def _heatmap_geometry(self) -> HeatmapGeometry | None: ...
        def _heatmap_axis_viewbox(self, layer: Any) -> Any: ...
        def _context_menu_action(self, text: str) -> Any | None: ...
        def _view_range_changed_programmatically(self) -> None: ...
        def _ensure_trace_axis_viewboxes(self, *, top: bool = False, right: bool = False) -> None: ...
else:
    class _PlotAxisScalingBase(PlotHost):
        pass


def _manual_axis_view_limits_are_usable(
    viewbox: Any,
    axis_number: int,
    values: tuple[float, float] | list[float],
) -> bool:
    """Preflight PyQtGraph's range arithmetic before it changes view state."""
    lower, upper = values
    span = upper - lower
    endpoint_sum = lower + upper
    if (
        not all(isfinite(value) for value in (lower, upper, span, endpoint_sum))
        or span <= 0
    ):
        return False
    # ViewBox expands narrow positive-offset ranges by this quantization
    # allowance before applying its limits. Check the same arithmetic without
    # disabling Auto or publishing an intermediate invalid target range.
    allowance = endpoint_sum * 1.5e-15
    if span < 2 * allowance:
        lower -= allowance
        upper += allowance
    if not all(isfinite(value) for value in (lower, upper, upper - lower)):
        return False
    effective_limits = getattr(viewbox, "_effectiveLimits", None)
    if callable(effective_limits):
        minimum, maximum = effective_limits()[axis_number]
        if minimum is not None and lower < minimum:
            return False
        if maximum is not None and upper > maximum:
            return False
    return True


def _disable_native_axis_auto(viewbox: Any, axis_constant: int) -> None:
    """Let qPlot replace pending native auto bounds without applying them."""
    viewbox = coordinate_viewbox(viewbox)
    pending = viewbox._autoRangeNeedsUpdate
    viewbox._autoRangeNeedsUpdate = False
    try:
        viewbox.enableAutoRange(axis_constant, False)
    finally:
        viewbox._autoRangeNeedsUpdate = pending and any(
            mode is not False for mode in viewbox.autoRangeEnabled()
        )


@contextmanager
def _axis_bounds_without_dynamic_clipping(
    viewbox: Any, items: list[Any] | None,
) -> Iterator[None]:
    """Measure processed curves without clipping them to the previous Y view."""
    changed = []
    try:
        for item in items if items is not None else viewbox.addedItems:
            if not isinstance(item, pg.PlotDataItem):
                continue
            limit = item.opts.get("dynamicRangeLimit")
            if limit is None:
                continue
            changed.append((item, limit))
            item.opts["dynamicRangeLimit"] = None
            item._datasetDisplay = None
            item.updateItems()
        yield
    finally:
        for item, limit in reversed(changed):
            item.opts["dynamicRangeLimit"] = limit
            item._datasetDisplay = None
            item.updateItems()


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


class PlotAxisScalingMixin(_PlotAxisScalingBase):
    """Edit the range and behaviour of individual ViewBox axes in plot windows."""

    def _init_axis_scale_dialogs(self) -> None:
        """
        Install per-axis scaling actions and initialise the shared editor.

        """
        self._axis_scale_controls = {}
        self._axis_scale_dialog = None
        self._axis_scale_editor = None
        self._axis_range_states: dict[_AxisName, RangeState] = {}
        self._axis_scale_custom_auto_axes: set[_AxisName] = set()
        self._paused_axis_links: dict[_AxisName, _RangeLink] = {}
        self._axis_scale_transposed = False
        self._swapping_axis_scales = False
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
        if getattr(self, "operation_kind", None) in ("plot1d", "sweeper"):
            # Full-view requests must reach qPlot before native bounds derived
            # from drawing-clipped coordinates mutate the current view.
            self.plot.autoBtn.clicked.disconnect(self.plot.autoBtnClicked)
            self.plot.autoBtn.clicked.connect(self._axis_scale_full_view_button_clicked)
            self.vb.set_auto_range_handler(lambda: self.force_all_axes_autoscale())

    def _axis_scale_full_view_button_clicked(self) -> None:
        """Apply full-view Auto with the same range guard as axis controls."""
        if self.plot.autoBtn.mode == "auto":
            self.force_all_axes_autoscale()
            self.plot.autoBtn.hide()
            self.plot.sigRangeChangedManually.emit(self.vb.mouseEnabled()[:])
            return
        self.__dict__.get("_axis_scale_custom_auto_axes", set()).clear()
        self.plot.disableAutoRange()
        for name in ("right_vb", "top_vb", "top_right_vb"):
            viewbox = self.__dict__.get(name)
            if viewbox is not None:
                viewbox.disableAutoRange()
        for axis in self.__dict__.get("_axis_scale_controls", {}):
            self._sync_axis_scale_controls(axis)

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
        """Synchronize controls and refresh Auto axes using this visible range."""

        if self.__dict__.get("_swapping_axis_scales", False):
            return
        if self.__dict__.get("_axis_scale_programmatic_change_depth", 0) == 0:
            self.__dict__.get("_axis_scale_custom_auto_axes", set()).discard(axis)
            previous = self.__dict__.get("_axis_range_states", {}).get(axis)
            if previous is not None and previous.lock is not None:
                current = self._axis_range_state(axis)
                if current.limits != previous.limits:
                    # Navigation moves the whole viewport. Carry the padlock
                    # forward at the displayed value for subsequent edits.
                    updated = replace(current, held=None)
                    self._axis_range_states[axis] = replace(updated, held=updated.value(previous.lock))
        if axis in self.__dict__.get("_axis_scale_controls", {}):
            self._sync_axis_scale_controls(axis)
        if (
                self.__dict__.get("_axis_scale_custom_auto_axes")
                and not self.__dict__.get("_axis_scale_visible_refresh_active", False)
                ):
            self.__dict__.setdefault("_axis_scale_changed_axes", set()).add(axis)
            timer = self.__dict__.get("_axis_scale_visible_refresh_timer")
            if timer is None:
                timer = QtCore.QTimer(self)
                timer.setSingleShot(True)
                timer.timeout.connect(self._refresh_axis_scale_visible_auto)
                self._axis_scale_visible_refresh_timer = timer
            # Linked axis-pair ViewBoxes may receive the same signal after us.
            # Read their final ranges once the current signal delivery finishes.
            timer.start(0)

    def _refresh_axis_scale_visible_auto(self) -> None:
        """Apply one bounded refresh of dependent semantic axes after a change."""

        changed_axes = self.__dict__.pop("_axis_scale_changed_axes", set())
        limits_by_axis: dict[_AxisName, tuple[float, float] | None] = {}
        for spec in _AXIS_SPECS:
            axis = spec.name
            if axis not in self.__dict__.get("_axis_scale_custom_auto_axes", set()):
                continue
            number = self._axis_scale_axis_number(axis)
            state = self._axis_scale_viewbox(axis).getState(copy=False)
            if not state["autoVisibleOnly"][number]:
                continue
            for bounds_viewbox, _items in self._axis_scale_bound_item_groups(axis):
                bounds_viewbox = coordinate_viewbox(bounds_viewbox)
                # The containing ViewBox identifies the perpendicular axis of
                # these assigned items, independently of the currently selected axis.
                if number == 0:
                    perpendicular: _AxisName = (
                        "y2" if bounds_viewbox in (
                            self.__dict__.get("right_vb"),
                            self.__dict__.get("top_right_vb"),
                        ) else "y"
                    )
                else:
                    perpendicular = (
                        "x2" if bounds_viewbox in (
                            self.__dict__.get("top_vb"),
                            self.__dict__.get("top_right_vb"),
                        ) else "x"
                    )
                if perpendicular in changed_axes:
                    limits_by_axis[axis] = self._axis_scale_auto_limits(axis)
                    break

        # Calculate every range before applying any of them. Mutually dependent
        # axes then use the same visible region, without chasing each other's
        # padding or auto-pan updates across successive event-loop turns.
        self._axis_scale_visible_refresh_active = True
        try:
            for axis, limits in limits_by_axis.items():
                self._apply_axis_scale_filtered_auto(axis, limits=limits)
        finally:
            self._axis_scale_visible_refresh_active = False

    def force_all_axes_autoscale(self) -> None:
        """Return every active plot axis to automatic scaling mode."""

        for spec in _AXIS_SPECS:
            axis = spec.name
            if not self._axis_scale_axis_is_used(axis):
                continue
            saved = self.__dict__.get("_axis_range_states", {}).get(axis)
            if self._axis_scale_uses_filtered_auto(axis) or (saved is not None and saved.lock is not None):
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
        for spec in _AXIS_SPECS:
            axis = spec.name
            axis_item = self.plot.getAxis(spec.side)
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
        return _AXIS_BY_NAME[axis].dimension

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
        Connect the axis editor to its semantic ViewBox.

        """
        ui = _AxisScaleControls(_AXIS_BY_NAME[axis])
        self._axis_scale_controls[axis] = ui
        ui.rangeEntry.bind(
            lambda: self._axis_scale_data_limits(axis, samples=True),
            lambda proposed: self._accept_axis_range(axis, proposed),
            fit_range=lambda: self._axis_scale_data_limits(axis),
        )
        for signal in (
            ui.mouseCheck.toggled, ui.linkCombo.currentIndexChanged,
            ui.visibleOnlyCheck.toggled, ui.invertCheck.toggled, ui.logCheck.toggled,
        ):
            signal.connect(lambda *_args: self._remember_axis_edit(axis))
        ui.mouseCheck.toggled.connect(lambda checked: self._axis_scale_mouse_toggled(axis, checked))
        ui.linkCombo.currentIndexChanged.connect(lambda _index: self._axis_scale_link_changed(axis))
        ui.visibleOnlyCheck.toggled.connect(lambda checked: self._axis_scale_visible_only_toggled(axis, checked))
        ui.invertCheck.toggled.connect(lambda checked: self._axis_scale_invert_toggled(axis, checked))
        ui.logCheck.toggled.connect(lambda checked: self._axis_scale_log_toggled(axis, checked))
        return ui

    def _remember_axis_edit(self, axis: _AxisName) -> None:
        dialog = self.__dict__.get("_plot_appearance_dialog")
        if dialog is not None and dialog._session_active and not dialog._session_finishing:
            dialog.remember_axis_edit(axis)

    def _physical_axis_limits(self, axis: _AxisName, limits: tuple[float, float] | list[float]) -> tuple[float, float]:
        lower, upper = np.asarray(self.view_to_data(axis, limits))
        return float(lower), float(upper)

    def _axis_range_state(self, axis: _AxisName) -> RangeState:
        number = self._axis_scale_axis_number(axis)
        view = self._axis_scale_viewbox(axis).getState(copy=False)
        automatic = (
            axis in self.__dict__.get("_axis_scale_custom_auto_axes", set())
            or view["autoRange"][number] is not False
        )
        limits = self._physical_axis_limits(axis, view["targetRange"][number])
        previous = self.__dict__.get("_axis_range_states", {}).get(axis)
        if previous is None:
            return RangeState(limits=limits, automatic=automatic)
        return replace(previous, limits=limits, automatic=automatic)

    def _accept_axis_range(self, axis: _AxisName, proposed: RangeState) -> None:
        """Validate a physical edit before changing the semantic axis or its edit history."""
        viewbox = self._axis_scale_viewbox(axis)
        number = self._axis_scale_axis_number(axis)
        if self._axis_scale_log_mode(axis) and any(value <= 0 for value in proposed.limits):
            raise ValueError("Log-scale limits must be greater than zero.")
        current = viewbox.getState(copy=False)["targetRange"][number]
        unchanged = self._physical_axis_limits(axis, current) == proposed.limits
        if unchanged:
            limits = (current[0], current[1])
        else:
            lower, upper = np.asarray(self.data_to_view(axis, proposed.limits))
            limits = (float(lower), float(upper))
        if not _manual_axis_view_limits_are_usable(viewbox, number, limits):
            raise ValueError("These axis limits exceed the supported plot range.")
        self._remember_axis_edit(axis)
        self.__dict__.setdefault("_axis_range_states", {})[axis] = proposed
        if proposed.automatic:
            if not self._axis_scale_uses_filtered_auto(axis):
                for box, assigned in self._axis_scale_bound_item_groups(axis):
                    for item in box.addedItems if assigned is None else assigned:
                        if isinstance(item, pg.PlotDataItem):
                            self._install_axis_scale_trace_handler(item)
            self._apply_axis_scale_filtered_auto(axis, limits=limits, already_fitted=True)
        elif unchanged:
            # A first keystroke freezes automatic tracking at its current
            # limits. Only committing a changed value needs a viewport update.
            self.__dict__.get("_axis_scale_custom_auto_axes", set()).discard(axis)
            _disable_native_axis_auto(viewbox, self._axis_scale_axis_constant(axis))
        else:
            self._apply_axis_scale_manual_limits(axis, limits)
        self._sync_axis_scale_controls(axis)

    def _sync_axis_scale_controls(self, axis: _AxisName) -> None:
        """Synchronise controls from the current semantic axis and view."""
        ui = self.__dict__.get("_axis_scale_controls", {}).get(axis)
        if ui is None or self.__dict__.get("_swapping_axis_scales"):
            return
        spec = _AXIS_BY_NAME[axis]
        quantity = _axis_quantity_text(self.plot, axis)
        tabs = self._axis_scale_tabs
        index = _AXIS_SPECS.index(spec)
        tooltip = spec.tooltip if quantity == spec.tooltip else f"{spec.tooltip} · {quantity}"
        if tabs.tabToolTip(index) != tooltip:
            tabs.setTabToolTip(index, tooltip)
            tabs.setAccessibleTabName(index, tooltip)
        if tabs.currentIndex() == index and self._axis_scale_quantity.text() != quantity:
            self._axis_scale_quantity.setText(quantity)
            self._axis_scale_quantity.setToolTip(quantity)
        axis_number = self._axis_scale_axis_number(axis)
        state = self._axis_scale_viewbox(axis).getState(copy=False)
        ui.rangeEntry.set_state(self._axis_range_state(axis), self.plot.getAxis(spec.side).labelUnits)
        for widget, value in (
            (ui.mouseCheck, state["mouseEnabled"][axis_number]),
            (ui.visibleOnlyCheck, state["autoVisibleOnly"][axis_number]),
            (ui.invertCheck, state.get(spec.dimension + "Inverted", False)),
            (ui.logCheck, self._axis_scale_log_mode(axis)),
        ):
            with QtCore.QSignalBlocker(widget):
                widget.setChecked(bool(value))
        log_supported = self._axis_scale_log_is_supported(axis)
        ui.logCheck.setEnabled(log_supported)
        ui.logCheck.setToolTip(
            "Display this axis on a base-10 logarithmic scale. Values at or below zero are not shown."
            if log_supported else "Log scaling is available for line-plot axes."
        )
        with QtCore.QSignalBlocker(ui.linkCombo):
            self._sync_axis_scale_link_combo(axis)

    def _axis_in_original_orientation(self, axis: _AxisName) -> _AxisName:
        """Convert a side in either direction between original and displayed axes."""
        return _TRANSPOSED_AXIS[axis] if self.__dict__.get("_axis_scale_transposed") else axis

    def _range_link(self, axis: _AxisName) -> _RangeLink | None:
        number = self._axis_scale_axis_number(axis)
        target = self._axis_scale_viewbox(axis).linkedView(number)
        if target is None:
            return self.__dict__.get("_paused_axis_links", {}).get(axis)
        widget = target.getViewWidget()
        owner = widget.window() if widget is not None else None
        if isinstance(owner, PlotAxisScalingMixin):
            for spec in _AXIS_SPECS:
                if spec.dimension == _AXIS_BY_NAME[axis].dimension and owner._axis_scale_viewbox(spec.name) is target:
                    return _RangeLink(ref(owner), owner._axis_in_original_orientation(spec.name))
        return _RangeLink(ref(target), axis)

    def _apply_range_link(self, axis: _AxisName, link: _RangeLink | None) -> None:
        view = self._axis_scale_viewbox(axis)
        number = self._axis_scale_axis_number(axis)
        paused = self.__dict__.setdefault("_paused_axis_links", {})
        paused.pop(axis, None)
        target = None
        endpoint = link.endpoint() if link is not None else None
        if link is not None and endpoint is not None:
            target, target_axis = endpoint
            if self._axis_scale_axis_number(target_axis) != number:
                paused[axis] = link
                target = None
        if view.linkedView(number) is not target:
            view.linkView(number, target)

    @contextmanager
    def _pause_following_range_links(self) -> Iterator[None]:
        """Hold other windows at their current range until their target has moved."""
        followers = []
        for window in qtw.QApplication.topLevelWidgets():
            if (window is self or not isinstance(window, PlotAxisScalingMixin)
                    or "vb" not in window.__dict__ or window.__dict__.get("_closed")):
                continue
            for axis in _TRANSPOSED_AXIS:
                if not window._axis_scale_axis_is_used(axis):
                    continue
                link = window._range_link(axis)
                if link is not None and link.owner() is self:
                    followers.append((window, axis, link))
        try:
            for window, axis, _link in followers:
                window._apply_range_link(axis, None)
            yield
        finally:
            for window, axis, link in followers:
                window._apply_range_link(axis, link)
                controls = window.__dict__.get("_axis_scale_controls", {}).get(axis)
                if controls is not None:
                    with QtCore.QSignalBlocker(controls.linkCombo):
                        window._sync_axis_scale_link_combo(axis)

    @contextmanager
    def _transposing_axis_scales(self) -> Iterator[None]:
        """Move the view with the data, detaching links before either axis changes."""
        if "_axis_scale_controls" not in self.__dict__:
            yield
            return
        used = {axis for axis in _TRANSPOSED_AXIS if self._axis_scale_axis_is_used(axis)}
        # A Top-only overlay needs the new Right view (and vice versa). Capture
        # that view's defaults as well, so the vacated side can be reused.
        need_top = "y2" in used and self.__dict__.get("top_vb") is None
        need_right = "x2" in used and self.__dict__.get("right_vb") is None
        if need_top or need_right:
            self._ensure_trace_axis_viewboxes(top=need_top, right=need_right)
        saved = {axis: self._capture_axis_settings(axis) for axis, other in _TRANSPOSED_AXIS.items()
                 if axis in used or other in used}
        dialog = self.__dict__.get("_plot_appearance_dialog")
        if dialog is not None and dialog._session_active and not dialog._session_finishing:
            for axis in used:
                dialog.remember_axis_edit(axis)
        original = self._axis_selection
        reversed_selection = {"x": original["y"], "y": original["x"]}
        with self._pause_following_range_links():
            self._swapping_axis_scales = True
            try:
                for axis, settings in saved.items():
                    self._apply_range_link(axis, None)
                    self._axis_scale_custom_auto_axes.discard(axis)
                    _disable_native_axis_auto(self._axis_scale_viewbox(axis), self._axis_scale_axis_number(axis))
                    if settings.log:
                        self._set_axis_log_mode(axis, False)
                yield
            finally:
                try:
                    # A refresh can fail after the new orientation has taken effect.
                    transposed = self._axis_selection == reversed_selection
                    if transposed:
                        self._axis_scale_transposed = not self._axis_scale_transposed
                        if dialog is not None and (dialog._session_active or dialog._session_finishing):
                            dialog.transpose_axis_edits(_TRANSPOSED_AXIS)
                    for axis, settings in saved.items():
                        self._restore_axis_settings(_TRANSPOSED_AXIS[axis] if transposed else axis, settings)
                finally:
                    self._swapping_axis_scales = False
                    for axis in self._axis_scale_controls:
                        self._sync_axis_scale_controls(axis)

    def _capture_axis_settings(self, axis: _AxisName) -> _AxisSettings:
        number = self._axis_scale_axis_number(axis)
        view = self._axis_scale_viewbox(axis)
        state = view.getState(copy=False)
        auto = state["autoRange"][number]
        return _AxisSettings(
            tuple(state["targetRange"][number]), auto,
            axis in self._axis_scale_custom_auto_axes,
            self._axis_range_states.get(axis), state["autoVisibleOnly"][number],
            state[self._axis_scale_dimension(axis) + "Inverted"], state["mouseEnabled"][number],
            self._axis_scale_log_mode(axis), self._range_link(axis),
        )

    def _restore_axis_settings(self, axis: _AxisName, saved: _AxisSettings) -> None:
        """Restore the edited side independently of live updates on the other axes."""
        if not self.__dict__.get("_swapping_axis_scales") and not self._axis_scale_axis_is_used(axis):
            return
        view = self._axis_scale_viewbox(axis)
        number = self._axis_scale_axis_number(axis)
        self._apply_range_link(axis, None)
        log = saved.log and self._axis_scale_log_is_supported(axis)
        limits = saved.limits
        if saved.log and not log:
            # The datasets may have changed during a modeless edit. Heatmaps
            # require linear axes, so restore their limits in physical units.
            with np.errstate(over="ignore", under="ignore"):
                lower, upper = np.power(10.0, limits)
            limits = (float(lower), float(upper))
        if self._axis_scale_log_mode(axis) != log:
            self._set_axis_log_mode(axis, log)
        self._axis_scale_custom_auto_axes.discard(axis)
        if saved.editor is None:
            self._axis_range_states.pop(axis, None)
        else:
            self._axis_range_states[axis] = saved.editor
        view.state["autoVisibleOnly"][number] = saved.visible_only
        self._axis_scale_mouse_toggled(axis, saved.mouse)
        self._axis_scale_invert_toggled(axis, saved.inverted)
        ui = self._axis_scale_controls.get(axis)
        if ui is not None:
            ui.rangeEntry.restore()
        # Restore the saved viewport before resuming live fits.
        self._apply_axis_scale_manual_limits(axis, limits)
        if saved.filtered_auto:
            self._axis_scale_custom_auto_axes.add(axis)
            if not self.__dict__.get("_swapping_axis_scales"):
                self._apply_axis_scale_filtered_auto(axis)
        elif saved.auto is not False:
            view.enableAutoRange(number, saved.auto)
        self._apply_range_link(axis, saved.linked)
        self._sync_axis_scale_controls(axis)

    def _sync_axis_scale_link_combo(self, axis: _AxisName) -> None:
        """
        List plot quantities while retaining the underlying ViewBox link target.

        """
        ui = self._axis_scale_controls[axis]
        axis_number = self._axis_scale_axis_number(axis)
        viewbox = self._axis_scale_viewbox(axis)
        menu = self.vbMenu if viewbox is self.vb else getattr(viewbox, "menu", None)
        source_combo = getattr(menu, "ctrl", [None, None])[axis_number]
        source_combo = getattr(source_combo, "linkCombo", None)
        current = viewbox.getState(copy=False)["linkedViews"][axis_number] or ""

        ui.linkCombo.clear()
        ui.linkCombo.addItem("None", "")
        targets = set()
        application = qtw.QApplication.instance()
        if isinstance(application, qtw.QApplication):
            windows = sorted(application.topLevelWidgets(), key=lambda w: w.windowTitle())
            for window in windows:
                if (
                    window is self
                    or not isinstance(window, PlotAxisScalingMixin)
                    or "vb" not in window.__dict__
                    or "plot" not in window.__dict__
                    or window.__dict__.get("_closed", False)
                    or not window.isVisible()
                ):
                    continue
                for spec in _AXIS_SPECS:
                    if (
                        window._axis_scale_axis_number(spec.name) != axis_number
                        or not window._axis_scale_axis_is_used(spec.name)
                    ):
                        continue
                    target = window._axis_scale_viewbox(spec.name)
                    quantity = _axis_quantity_text(window.plot, spec.name)
                    text = f"{window.windowTitle()} — {spec.assignment_side}: {quantity}"
                    ui.linkCombo.addItem(text, target.name or ref(target))
                    targets.add(target)

        for index in range(source_combo.count() if source_combo is not None else 0):
            name = source_combo.itemText(index)
            if name and pg.ViewBox.NamedViews.get(name) not in targets:
                ui.linkCombo.addItem(name, name)

        linked_view = viewbox.linkedView(axis_number)
        linked_target = ref(linked_view) if linked_view is not None else ""
        index = ui.linkCombo.findData(current or linked_target)
        if index < 0:
            index = ui.linkCombo.findData(linked_target)
        paused = self.__dict__.get("_paused_axis_links", {}).get(axis)
        tooltip = _LINK_TOOLTIP
        if paused is not None and (endpoint := paused.endpoint()) is not None:
            target_view, target_axis = endpoint
            window = target_view.getViewWidget().window()
            label = f"Paused — {window.windowTitle()} · {_AXIS_BY_NAME[target_axis].assignment_side}"
            if isinstance(window, PlotAxisScalingMixin):
                label += f" · {_axis_quantity_text(window.plot, target_axis)}"
            ui.linkCombo.addItem(label, paused)
            index = ui.linkCombo.count() - 1
            tooltip = f"{label}\nThe link resumes when both axes have the same orientation. Choose None to remove it."
            ui.linkCombo.setItemData(index, tooltip, QtCore.Qt.ItemDataRole.ToolTipRole)
        ui.linkCombo.setToolTip(tooltip)
        ui.linkCombo.setCurrentIndex(max(index, 0))

    def _release_axis_scale_links(self) -> None:
        """Detach other plots from this window before its axes are hidden or deleted."""
        targets = {self._axis_scale_viewbox(spec.name) for spec in _AXIS_SPECS}
        target_choices = tuple(view.name or ref(view) for view in targets)
        for window in qtw.QApplication.topLevelWidgets():
            if window is self or not isinstance(window, PlotAxisScalingMixin) or "vb" not in window.__dict__:
                continue
            for axis in _TRANSPOSED_AXIS:
                view = window._axis_scale_viewbox(axis)
                dimension = window._axis_scale_axis_number(axis)
                if view.linkedView(dimension) in targets:
                    view.linkView(dimension, None)
                # Touch only the chooser. A full range sync discards pending
                # edits, and removing the selected row could link its neighbour.
                paused = window.__dict__.get("_paused_axis_links", {}).get(axis)
                paused_target_closed = paused is not None and paused.owner() is self
                if paused_target_closed:
                    window._paused_axis_links.pop(axis)
                controls = window.__dict__.get("_axis_scale_controls", {}).get(axis)
                if controls is None:
                    continue
                combo = controls.linkCombo
                with QtCore.QSignalBlocker(combo):
                    if combo.currentData() in target_choices or paused_target_closed:
                        combo.setCurrentIndex(0)
                        combo.setToolTip(_LINK_TOOLTIP)
                    for row in range(combo.count() - 1, 0, -1):
                        choice = combo.itemData(row)
                        if choice in target_choices or (isinstance(choice, _RangeLink) and choice.owner() is self):
                            combo.removeItem(row)

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

    def _axis_scale_full_heatmap_bounds(
        self, axis: _AxisName, *, cell_edges: bool,
    ) -> Iterator[tuple[float, float]]:
        """Use the full measurement extents cached by viewport-limited readers."""
        number = self._axis_scale_axis_number(axis)
        for layer in self.__dict__.get("heatmaps", {}).values():
            if self._heatmap_axis_sides(layer)[number] != _AXIS_BY_NAME[axis].assignment_side:
                continue
            if not self._heatmap_is_visible(layer):
                continue
            source = self if layer is self else layer.from_win
            if not getattr(source, "_large_heatmap_sql_mode", False):
                continue
            order = ("x", "y") if layer is self else layer.source_axis_order()
            if order is None:
                continue
            bounds = getattr(source, "_heatmap_full_view_ranges" if cell_edges else "_heatmap_full_axis_ranges", None)
            if bounds is not None and order[number] in bounds:
                yield bounds[order[number]]

    def _axis_scale_sample_limits(
        self, axis: _AxisName, *, cell_edges: bool = False,
    ) -> tuple[float, float] | None:
        """Measure assigned, finite samples in view coordinates before any plot padding."""
        number = self._axis_scale_axis_number(axis)
        visible = self._axis_scale_viewbox(axis).getState(copy=False)["autoVisibleOnly"][number]
        bounds = [] if visible else list(self._axis_scale_full_heatmap_bounds(axis, cell_edges=cell_edges))
        for box, items in self._axis_scale_bound_item_groups(axis):
            for item in box.addedItems if items is None else items:
                if not isinstance(item, pg.PlotDataItem) or not item.isVisible():
                    continue
                # Use the native mapping (FFT, derivatives, log scaling) before
                # cosmetic clipping and pixel downsampling change the extrema.
                item.getData()
                mapped = item._datasetMapped
                if mapped is None or mapped.x.shape != mapped.y.shape:
                    continue
                values, other = (mapped.x, mapped.y) if number == 0 else (mapped.y, mapped.x)
                eligible = numeric_isfinite(values) & numeric_isfinite(other)
                if visible:
                    low, high = box.viewRange()[1 - number]
                    coordinates = other[eligible]
                    eligible[eligible] = (coordinates >= low) & (coordinates <= high)
                if eligible.any():
                    samples = values[eligible]
                    bounds.append((float(samples.min()), float(samples.max())))
        side = _AXIS_BY_NAME[axis].assignment_side
        for layer in self.__dict__.get("heatmaps", {}).values():
            if self._heatmap_axis_sides(layer)[number] != side or not self._heatmap_is_visible(layer):
                continue
            geometry = self._heatmap_geometry() if layer is self else layer.geometry
            if geometry is None:
                continue
            grid = self.dataGrid if layer is self else layer.data_grid
            along, across = (geometry.x, geometry.y) if number == 0 else (geometry.y, geometry.x)
            rows = np.asarray(grid) if number == 0 else np.asarray(grid).T
            if visible:
                low, high = self._heatmap_axis_viewbox(layer).viewRange()[1 - number]
                centres = np.asarray(across.centres)
                rows = rows[np.searchsorted(centres, low):np.searchsorted(centres, high, side="right")]
            occupied = np.flatnonzero(numeric_isfinite(rows).any(axis=0))
            if occupied.size:
                first, last = occupied[0], occupied[-1]
                bounds.append((along.edges[first], along.edges[last + 1]) if cell_edges
                              else (along.centres[first], along.centres[last]))
        if not bounds:
            return None
        return min(low for low, _ in bounds), max(high for _, high in bounds)

    def _axis_scale_auto_limits(self, axis: _AxisName) -> tuple[float, float] | None:
        """Calculate this tab's auto range without changing the viewbox."""

        viewbox = self._axis_scale_viewbox(axis)
        axis_number = self._axis_scale_axis_number(axis)
        state = viewbox.getState(copy=False)
        current_ranges = viewbox.viewRange()
        fractions = [1.0, 1.0]
        visible_only = state["autoVisibleOnly"][axis_number]
        ranges = [] if visible_only else [
            list(bounds) for bounds in self._axis_scale_full_heatmap_bounds(axis, cell_edges=True)
        ]
        if visible_only:
            eligible = self._axis_scale_sample_limits(axis, cell_edges=True)
            if eligible is None:
                return None
            ranges.append(list(eligible))
        for bounds_viewbox, items in (() if visible_only else self._axis_scale_bound_item_groups(axis)):
            orthogonal_ranges: list[list[float] | None] = [None, None]
            with _axis_bounds_without_dynamic_clipping(bounds_viewbox, items):
                for item in items if items is not None else bounds_viewbox.addedItems:
                    if not isinstance(item, pg.PlotDataItem) or not item.isVisible():
                        continue
                    axis_bounds = getattr(item, "axisDataBounds", item.dataBounds)
                    sample_bounds = axis_bounds(
                        axis_number, fractions[axis_number], orthogonal_ranges[axis_number],
                    )
                    if sample_bounds[0] is not None and sample_bounds[1] is not None:
                        sample_lower, sample_upper = (float(value) for value in sample_bounds)
                        if (
                            isfinite(sample_lower) and isfinite(sample_upper)
                            and not all(isfinite(value) for value in (
                                sample_upper - sample_lower, sample_upper + sample_lower,
                            ))
                        ):
                            # Qt rectangles cannot represent this finite pair;
                            # preserve it for explicit rejection by the guard.
                            return sample_lower, sample_upper
                        if (isfinite(sample_lower) and isfinite(sample_upper)
                                and all(value is None for value in item.dataBounds(
                                    axis_number, fractions[axis_number], orthogonal_ranges[axis_number],
                                ))):
                            # A rejected viewport hides native painting items,
                            # but supported mapped data can still restore Auto.
                            # childrenBounds cannot see these unpainted bounds.
                            ranges.append([sample_lower, sample_upper])
                bounds = bounds_viewbox.childrenBounds(
                    frac=fractions,
                    orthoRange=orthogonal_ranges,
                    items=items,
                )[axis_number]
            if bounds is not None:
                if not all(isfinite(value) for value in bounds):
                    # Native pixel padding can overflow finite sample bounds.
                    # Keep unsupported geometry distinct from absent data so
                    # the common preflight disables retries and reports it.
                    return float(bounds[0]), float(bounds[1])
                ranges.append(bounds)
        if not ranges:
            return None

        lower = min(float(bounds[0]) for bounds in ranges)
        upper = max(float(bounds[1]) for bounds in ranges)
        current_span = current_ranges[axis_number][1] - current_ranges[axis_number][0]
        if lower == upper:
            if not isfinite(current_span) or current_span <= 0:
                current_span = 1.0
            pixels = viewbox.width() if axis_number == 0 else viewbox.height()
            if not isfinite(max(1.0, pixels) / current_span):
                current_span = 1.0
            center = lower * 0.5 + upper * 0.5
            # A narrow span at a large offset may collapse to one double.
            # Retain at least the backend's quantization allowance so that
            # unsupported constant/auto-pan geometry reaches our preflight.
            half_span = max(current_span * 0.5, abs(center) * 1.5e-15)
            lower = center - half_span
            upper = center + half_span
        else:
            padding = viewbox.suggestPadding(axis_number)
            extra = (upper - lower) * padding
            lower -= extra
            upper += extra

        # Existing bounds can produce unsupported geometry after padding or
        # centering. Preserve that result for the common range preflight;
        # None means no bounds, and would bypass rejection and mode recovery.
        return lower, upper

    def _axis_scale_data_limits(self, axis: _AxisName, *, samples: bool = False) -> tuple[float, float] | None:
        limits = self._axis_scale_sample_limits(axis) if samples else self._axis_scale_auto_limits(axis)
        if limits is not None and not samples and self._axis_range_state(axis).lock is None:
            if not _manual_axis_view_limits_are_usable(
                self._axis_scale_viewbox(axis), self._axis_scale_axis_number(axis), limits,
            ):
                raise RangeFitError("Automatic axis limits exceed the supported plot range.")
        return None if limits is None else self._physical_axis_limits(axis, limits)

    def _axis_scale_mouse_toggled(self, axis: _AxisName, checked: bool) -> None:
        viewbox = self._axis_scale_viewbox(axis)
        if self._axis_scale_dimension(axis) == "x":
            viewbox.setMouseEnabled(x=checked)
        else:
            viewbox.setMouseEnabled(y=checked)

    def _apply_axis_scale_manual_limits(
            self,
            axis: _AxisName,
            view_values: tuple[float, float] | list[float],
            ) -> None:
        """Apply validated numeric ViewBox limits without parsing UI text."""

        viewbox = self._axis_scale_viewbox(axis)
        if not _manual_axis_view_limits_are_usable(
            viewbox, self._axis_scale_axis_number(axis), view_values,
        ):
            self._sync_axis_scale_controls(axis)
            show_status = getattr(self, "show_status", None)
            if callable(show_status):
                show_status("These axis limits exceed the supported plot range.", 5000)
            return
        self.__dict__.get("_axis_scale_custom_auto_axes", set()).discard(axis)
        self._axis_scale_programmatic_change_depth = self.__dict__.get("_axis_scale_programmatic_change_depth", 0) + 1
        try:
            if self._axis_scale_dimension(axis) == "x":
                viewbox.setXRange(*view_values, padding=0)
            else:
                viewbox.setYRange(*view_values, padding=0)
        finally:
            self._axis_scale_programmatic_change_depth -= 1
        self._view_range_changed_programmatically()

    def _axis_scale_uses_filtered_auto(self, axis: _AxisName) -> bool:
        """Return whether auto-ranging must respect per-item axis assignment."""

        return (
            getattr(self, "operation_kind", None) == "sweeper"
            or (
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

    def _apply_axis_scale_filtered_auto(
            self,
            axis: _AxisName,
            *,
            limits: tuple[float, float] | None = None,
            already_fitted: bool = False,
            ) -> None:
        """Apply an item-filtered auto range while retaining Auto mode in the UI."""

        if self.__dict__.get("_swapping_axis_scales"):
            return
        if limits is None:
            limits = self._axis_scale_auto_limits(axis)
        if limits is None:
            ui = self.__dict__.get("_axis_scale_controls", {}).get(axis)
            if ui is not None:
                ui.rangeEntry.show_error("There are no eligible samples to fit.")
            return
        viewbox = self._axis_scale_viewbox(axis)
        axis_constant = self._axis_scale_axis_constant(axis)
        axis_number = self._axis_scale_axis_number(axis)
        bound_groups = self._axis_scale_bound_item_groups(axis)
        saved = self.__dict__.get("_axis_range_states", {}).get(axis)
        if not already_fitted and saved is not None and saved.lock is not None:
            try:
                fitted = saved.fit(self._physical_axis_limits(axis, limits))
                if self._axis_scale_log_mode(axis) and fitted.limits[0] <= 0:
                    raise ValueError("The held value prevents a fit with positive log-scale limits.")
                lower, upper = np.asarray(self.data_to_view(axis, fitted.limits))
                limits = (float(lower), float(upper))
            except ValueError as error:
                ui = self.__dict__.get("_axis_scale_controls", {}).get(axis)
                if ui is not None:
                    ui.rangeEntry.show_error(str(error))
                return
        if not _manual_axis_view_limits_are_usable(viewbox, axis_number, limits):
            if viewbox.autoRangeEnabled()[axis_number] is not False or any(
                coordinate_viewbox(bounds_viewbox).autoRangeEnabled()[axis_number] is not False
                for bounds_viewbox, _items in bound_groups
            ):
                # Initial/native Auto must not retry the rejected geometry on
                # paint. Retain qPlot's Auto mode so a later supported source
                # update can recover automatically with the previous view intact.
                for bounds_viewbox, _items in bound_groups:
                    _disable_native_axis_auto(bounds_viewbox, axis_constant)
                if all(bounds_viewbox is not viewbox for bounds_viewbox, _items in bound_groups):
                    _disable_native_axis_auto(viewbox, axis_constant)
                self.__dict__.setdefault("_axis_scale_custom_auto_axes", set()).add(axis)
            if axis in self.__dict__.get("_axis_scale_controls", {}):
                self._sync_axis_scale_controls(axis)
            show_status = getattr(self, "show_status", None)
            if callable(show_status):
                fft_error = self.__dict__.get("_qplot_native_fft_error_text")
                show_status(
                    fft_error or "Automatic axis limits exceed the supported plot range.",
                    0 if fft_error else 5000,
                )
            return
        self.__dict__.setdefault("_axis_scale_custom_auto_axes", set()).add(axis)
        self._axis_scale_programmatic_change_depth = (
            self.__dict__.get("_axis_scale_programmatic_change_depth", 0) + 1
        )
        try:
            for bounds_viewbox, _items in bound_groups:
                _disable_native_axis_auto(bounds_viewbox, axis_constant)
            if all(bounds_viewbox is not viewbox for bounds_viewbox, _items in bound_groups):
                _disable_native_axis_auto(viewbox, axis_constant)
            if self._axis_scale_dimension(axis) == "x":
                viewbox.setXRange(*limits, padding=0)
            else:
                viewbox.setYRange(*limits, padding=0)
        finally:
            self._axis_scale_programmatic_change_depth -= 1
        self._sync_axis_scale_controls(axis)
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

        axes = set(self.__dict__.get("_axis_scale_custom_auto_axes", set()))
        for spec in _AXIS_SPECS:
            axis = spec.name
            if (self._axis_scale_axis_is_used(axis)
                    and self._axis_scale_uses_filtered_auto(axis)
                    and self._axis_scale_viewbox(axis).autoRangeEnabled()[
                        self._axis_scale_axis_number(axis)] is not False):
                axes.add(axis)
        for axis in tuple(axes):
            if self._axis_scale_axis_is_used(axis):
                self._apply_axis_scale_filtered_auto(axis)

    def _axis_scale_link_changed(self, axis: _AxisName) -> None:
        ui = self._axis_scale_controls[axis]
        viewbox = self._axis_scale_viewbox(axis)
        target = ui.linkCombo.currentData() or None
        if isinstance(target, _RangeLink):
            return
        self.__dict__.get("_paused_axis_links", {}).pop(axis, None)
        if isinstance(target, ReferenceType):
            target = target()
            if target is None or sip.isdeleted(target):
                self._sync_axis_scale_controls(axis)
                return
        if self._axis_scale_dimension(axis) == "x":
            viewbox.setXLink(target)
        else:
            viewbox.setYLink(target)

    def _axis_scale_visible_only_toggled(self, axis: _AxisName, checked: bool) -> None:
        viewbox = self._axis_scale_viewbox(axis)
        # Native setAutoVisible clears the perpendicular option. qPlot fits
        # both axes together from the same visible region in one bounded pass.
        viewbox.state["autoVisibleOnly"][self._axis_scale_axis_number(axis)] = checked
        if self._axis_range_state(axis).automatic:
            self._apply_axis_scale_filtered_auto(axis)

    def _axis_scale_invert_toggled(self, axis: _AxisName, checked: bool) -> None:
        viewbox = self._axis_scale_viewbox(axis)
        if self._axis_scale_dimension(axis) == "x":
            viewbox.invertX(checked)
        else:
            viewbox.invertY(checked)

    def _axis_scale_log_is_supported(self, axis: _AxisName) -> bool:
        """Return whether this axis contains data items that support log mode."""

        supports_log = False
        for _viewbox, items in self._axis_scale_bound_item_groups(axis):
            candidates = items
            if candidates is None:
                candidates = list(getattr(self.plot, "dataItems", []))
            for item in candidates:
                if not callable(getattr(item, "setLogMode", None)):
                    return False
                supports_log = True
        return supports_log

    def _axis_scale_log_mode(self, axis: _AxisName) -> bool:
        """Return the current log state from the displayed axis item."""

        plot = self.__dict__.get("plot")
        get_axis = getattr(plot, "getAxis", None)
        if not callable(get_axis):
            return False
        axis_item = get_axis(_AXIS_BY_NAME[axis].side)
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

        if checked and not self._axis_scale_log_is_supported(axis):
            self._sync_axis_scale_controls(axis)
            return

        self._set_axis_log_mode(axis, checked)
        if self._axis_scale_uses_filtered_auto(axis):
            self._apply_axis_scale_filtered_auto(axis)
        else:
            self._axis_scale_viewbox(axis).enableAutoRange(
                self._axis_scale_axis_constant(axis), True,
            )
        self._sync_axis_scale_controls(axis)
        self._view_range_changed_programmatically()

    def _set_axis_log_mode(self, axis: _AxisName, checked: bool) -> None:
        self.plot.getAxis(_AXIS_BY_NAME[axis].side).setLogMode(checked)
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

    def _axis_scale_axis_is_used(self, axis: _AxisName) -> bool:
        """Return whether the plot currently has an item on this axis."""

        styles = self.__dict__.get("_trace_styles")
        lines = self.__dict__.get("lines")
        if not isinstance(styles, dict) or not isinstance(lines, dict):
            assignments = self.__dict__.get("_heatmap_axis_assignments")
            if isinstance(assignments, dict):
                spec = _AXIS_BY_NAME[axis]
                return any(
                    assignment.get(spec.dimension) == spec.assignment_side
                    for assignment in assignments.values()
                )
            return axis in ("x", "y")

        spec = _AXIS_BY_NAME[axis]
        return any(
            line is not None
            and getattr(styles.get(trace_key), spec.trace_attribute, None) == spec.assignment_side
            for trace_key, line in lines.items()
        )

    def _create_axis_scale_editor(self) -> qtw.QWidget:
        editor = qtw.QWidget()
        editor.setObjectName("axisScaleEditor")
        layout = qtw.QVBoxLayout(editor)
        layout.setContentsMargins(10, 8, 10, 8)
        panel = qtw.QFrame(editor)
        panel.setObjectName("axisScalePanel")
        panel.setAccessibleName("Axis settings")
        panel.setFrameShape(qtw.QFrame.Shape.StyledPanel)
        layout.addWidget(panel)
        panel_layout = qtw.QVBoxLayout(panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(0)

        tabs = qtw.QTabBar(panel)
        tabs.setObjectName("axisScaleTabs")
        tabs.setAccessibleName("Edit axis")
        tabs.setDrawBase(False)
        tabs.setExpanding(True)
        tabs.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        tabs.setSizePolicy(qtw.QSizePolicy.Policy.Expanding, qtw.QSizePolicy.Policy.Fixed)
        panel_layout.addWidget(tabs)

        details = qtw.QVBoxLayout()
        details.setContentsMargins(10, 8, 10, 8)
        details.setSpacing(8)
        panel_layout.addLayout(details, 1)
        quantity = _AxisQuantityLabel(panel)
        quantity.setObjectName("axisScaleQuantity")
        quantity.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        quantity.setSizePolicy(qtw.QSizePolicy.Policy.Expanding, qtw.QSizePolicy.Policy.Preferred)
        font = quantity.font()
        font.setBold(True)
        quantity.setFont(font)
        details.addWidget(quantity)

        stack = qtw.QStackedWidget(panel)
        for spec in _AXIS_SPECS:
            index = tabs.addTab(spec.assignment_side)
            tabs.setTabData(index, spec.name)
            stack.addWidget(self._new_axis_scale_controls(spec.name))
        details.addWidget(stack, 1)
        self._axis_scale_editor = editor
        self._axis_scale_tabs = tabs
        self._axis_scale_stack = stack
        self._axis_scale_quantity = quantity
        tabs.currentChanged.connect(stack.setCurrentIndex)
        tabs.currentChanged.connect(self._axis_scale_choice_changed)
        return editor

    def _create_axis_scale_dialog(self) -> qtw.QDialog:
        """Standalone axis editor for windows without dataset controls."""
        dialog = qtw.QDialog(self)
        dialog.setWindowTitle(self._axis_scale_dialog_title())
        layout = qtw.QVBoxLayout(dialog)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        layout.addWidget(self._create_axis_scale_editor())
        # The standalone editor has no dataset pane to establish its opening width.
        for ui in self._axis_scale_controls.values():
            for field in ui.rangeEntry.fields.values():
                field.setMinimumWidth(field.fontMetrics().horizontalAdvance("0.888888888888888888") + 12)

        buttons = qtw.QDialogButtonBox(qtw.QDialogButtonBox.StandardButton.Close)
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.rejected.connect(dialog.close)
        layout.addWidget(buttons)
        layout.setSizeConstraint(qtw.QLayout.SizeConstraint.SetMinimumSize)

        self._axis_scale_dialog = dialog
        return dialog

    def _axis_scale_choice_changed(self, index: int) -> None:
        """Refresh an axis when it is selected."""

        if not 0 <= index < len(_AXIS_SPECS):
            return
        axis = _AXIS_SPECS[index].name
        if self._axis_scale_axis_is_used(axis):
            self._sync_axis_scale_controls(axis)

    def _sync_axis_scale_choices(self) -> None:
        """Keep the tabs on an assigned axis when plot contents change."""
        if self.__dict__.get("_axis_scale_editor") is None:
            return
        tabs = self._axis_scale_tabs
        current = tabs.currentIndex()
        first_used = -1
        for index, spec in enumerate(_AXIS_SPECS):
            axis = spec.name
            enabled = self._axis_scale_axis_is_used(axis)
            tabs.setTabEnabled(index, enabled)
            if enabled:
                if first_used < 0:
                    first_used = index
                self._sync_axis_scale_controls(axis)
            else:
                tabs.setTabToolTip(index, "No datasets are assigned to this axis.")
                tabs.setAccessibleTabName(index, spec.tooltip)
        if first_used >= 0 and not tabs.isTabEnabled(current):
            tabs.setCurrentIndex(first_used)
        self._axis_scale_stack.setVisible(first_used >= 0)
        self._axis_scale_quantity.setVisible(first_used >= 0)

    @QtCore.pyqtSlot(str)
    def open_axis_scale_dialog(self, axis: str) -> None:
        """
        Open the shared scaling dialog on the requested axis.

        """
        if hasattr(self.vb, "updateViewLists"):
            self.vb.updateViewLists()

        if hasattr(self.vbMenu, "updateState"):
            self.vbMenu.updateState()

        axis_name = _AXIS_ALIASES.get(axis.lower())
        if axis_name is None:
            return

        open_settings = self.__dict__.get("_open_plot_settings_page")
        if open_settings is not None:
            dialog = open_settings(page="Axes")
        else:
            dialog = self.__dict__.get("_axis_scale_dialog")
            if dialog is None:
                dialog = self._create_axis_scale_dialog()

        self._sync_axis_scale_choices()
        if self._axis_scale_axis_is_used(axis_name):
            self._axis_scale_tabs.setCurrentIndex(_AXIS_SPECS.index(_AXIS_BY_NAME[axis_name]))
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
