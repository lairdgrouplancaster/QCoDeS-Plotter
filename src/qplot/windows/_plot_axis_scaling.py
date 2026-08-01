from math import isclose, isfinite, log10
from typing import TYPE_CHECKING, Any, Literal

import pyqtgraph as pg
from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw
from pyqtgraph.graphicsItems.ViewBox import axisCtrlTemplate_generic

_AxisName = Literal["x", "y"]
_AXIS_MENU_ITEMS: tuple[tuple[_AxisName, str], ...] = (
    ("x", "X axis"),
    ("y", "Y axis"),
)
_AXIS_SIDES: tuple[tuple[_AxisName, str], ...] = (
    ("x", "bottom"),
    ("y", "left"),
)

if TYPE_CHECKING:
    class _PlotAxisScalingBase(qtw.QMainWindow):
        _axis_scale_controls: dict[_AxisName, Any]
        _axis_scale_dialogs: dict[_AxisName, qtw.QDialog]
        plot: Any
        vb: Any
        vbMenu: Any

        def _context_menu_action(self, text: str) -> Any | None: ...
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
    """
    Axis scaling dialogs and controls shared by plot windows.

    This mixin adapts pyqtgraph's embedded ViewBox controls into qPlot dialogs
    opened by double-clicking the plot axes.
    """

    def _init_axis_scale_dialogs(self) -> None:
        """
        Move pyqtgraph's X/Y axis scaling controls into double-click dialogs.

        """
        self._axis_scale_controls = {}
        self._axis_scale_dialogs = {}

        for _axis, menu_text in _AXIS_MENU_ITEMS:
            action = self._context_menu_action(menu_text)
            if action is None or action.menu() is None:
                continue

            self.vbMenu.removeAction(action)

        self._install_axis_scale_double_click_handlers()

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
        for axis, side in _AXIS_SIDES:
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

    def _axis_scale_dialog_title(self, axis: _AxisName) -> str:
        return f"{axis.upper()} axis scaling"

    def _axis_scale_axis_number(self, axis: _AxisName) -> int:
        return 0 if axis == "x" else 1

    def _axis_scale_axis_constant(self, axis: _AxisName) -> int:
        return pg.ViewBox.XAxis if axis == "x" else pg.ViewBox.YAxis

    def _new_axis_scale_controls(self, axis: _AxisName) -> qtw.QWidget:
        """
        Build a fresh copy of pyqtgraph's axis scaling controls for a dialog.

        """
        widget = qtw.QWidget()
        ui: Any = axisCtrlTemplate_generic.Ui_Form()
        ui.setupUi(widget)
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

        return widget

    def _sync_axis_scale_controls(self, axis: _AxisName) -> None:
        """
        Update a dialog's controls from the current view state.

        """
        ui = self._axis_scale_controls.get(axis)
        if ui is None:
            return

        axis_number = self._axis_scale_axis_number(axis)
        state = self.vb.getState(copy=False)

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
                ui.mouseCheck,
                ):
            widget.blockSignals(True)

        try:
            target_range = state["targetRange"][axis_number]
            ui.minText.setText(f"{target_range[0]:.5g}")
            ui.maxText.setText(f"{target_range[1]:.5g}")

            auto_range = state["autoRange"][axis_number]
            ui.autoRadio.setChecked(auto_range is not False)
            ui.manualRadio.setChecked(auto_range is False)
            if auto_range is not False and auto_range is not True:
                ui.autoPercentSpin.setValue(int(auto_range * 100))

            ui.mouseCheck.setChecked(state["mouseEnabled"][axis_number])
            ui.autoPanCheck.setChecked(state["autoPan"][axis_number])
            ui.visibleOnlyCheck.setChecked(state["autoVisibleOnly"][axis_number])
            ui.invertCheck.setChecked(state.get(axis + "Inverted", False))
            self._sync_axis_scale_link_combo(axis)
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
                    ui.mouseCheck,
                    ):
                widget.blockSignals(False)

    def _sync_axis_scale_link_combo(self, axis: _AxisName) -> None:
        """
        Mirror pyqtgraph's available linked views into the dialog link combo.

        """
        ui = self._axis_scale_controls[axis]
        axis_number = self._axis_scale_axis_number(axis)
        source_combo = self.vbMenu.ctrl[axis_number].linkCombo
        current = self.vb.getState(copy=False)["linkedViews"][axis_number] or ""

        ui.linkCombo.clear()
        for index in range(source_combo.count()):
            ui.linkCombo.addItem(source_combo.itemText(index))

        index = ui.linkCombo.findText(current)
        ui.linkCombo.setCurrentIndex(max(index, 0))

    def _axis_scale_mouse_toggled(self, axis: _AxisName, checked: bool) -> None:
        if axis == "x":
            self.vb.setMouseEnabled(x=checked)
        else:
            self.vb.setMouseEnabled(y=checked)

    def _axis_scale_manual_clicked(self, axis: _AxisName) -> None:
        self.vb.enableAutoRange(self._axis_scale_axis_constant(axis), False)

    def _axis_scale_range_text_changed(self, axis: _AxisName) -> None:
        ui = self._axis_scale_controls[axis]
        axis_number = self._axis_scale_axis_number(axis)
        previous_values = list(self.vb.viewRange()[axis_number])
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

        ui.manualRadio.setChecked(True)
        if axis == "x":
            self.vb.setXRange(*values, padding=0)
        else:
            self.vb.setYRange(*values, padding=0)

    def _axis_scale_auto_clicked(self, axis: _AxisName) -> None:
        ui = self._axis_scale_controls[axis]
        self.vb.enableAutoRange(
            self._axis_scale_axis_constant(axis),
            ui.autoPercentSpin.value() * 0.01,
            )

    def _axis_scale_auto_spin_changed(self, axis: _AxisName, value: float) -> None:
        ui = self._axis_scale_controls[axis]
        ui.autoRadio.setChecked(True)
        self.vb.enableAutoRange(self._axis_scale_axis_constant(axis), value * 0.01)

    def _axis_scale_link_changed(self, axis: _AxisName) -> None:
        ui = self._axis_scale_controls[axis]
        if axis == "x":
            self.vb.setXLink(str(ui.linkCombo.currentText()))
        else:
            self.vb.setYLink(str(ui.linkCombo.currentText()))

    def _axis_scale_auto_pan_toggled(self, axis: _AxisName, checked: bool) -> None:
        if axis == "x":
            self.vb.setAutoPan(x=checked)
        else:
            self.vb.setAutoPan(y=checked)

    def _axis_scale_visible_only_toggled(self, axis: _AxisName, checked: bool) -> None:
        if axis == "x":
            self.vb.setAutoVisible(x=checked)
        else:
            self.vb.setAutoVisible(y=checked)

    def _axis_scale_invert_toggled(self, axis: _AxisName, checked: bool) -> None:
        if axis == "x":
            self.vb.invertX(checked)
        else:
            self.vb.invertY(checked)

    @QtCore.pyqtSlot(str)
    def open_axis_scale_dialog(self, axis: str) -> None:
        """
        Opens the scaling dialog for the requested axis.

        """
        if hasattr(self.vb, "updateViewLists"):
            self.vb.updateViewLists()

        if hasattr(self.vbMenu, "updateState"):
            self.vbMenu.updateState()

        if axis not in ("x", "y"):
            return
        axis_name: _AxisName = "x" if axis == "x" else "y"

        dialog = self._axis_scale_dialogs.get(axis_name)
        if dialog is None:
            dialog = qtw.QDialog(self)
            dialog.setWindowTitle(self._axis_scale_dialog_title(axis_name))
            layout = qtw.QVBoxLayout(dialog)
            layout.addWidget(self._new_axis_scale_controls(axis_name))

            buttons = qtw.QDialogButtonBox(qtw.QDialogButtonBox.StandardButton.Close)
            buttons.rejected.connect(dialog.close)
            layout.addWidget(buttons)

            self._axis_scale_dialogs[axis_name] = dialog

        self._sync_axis_scale_controls(axis_name)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
