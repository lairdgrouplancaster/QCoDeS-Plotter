from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

import numpy as np
from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

from ._colour_map_picker import ColourMapDialog, CurrentColourMap
from ._plot_host import PlotHost
from ._range_controls import RangeControls, RangeState

if TYPE_CHECKING:
    class _ColorbarScaleDialogBase(PlotHost):
        _colorbar_colormap_name: str
        _colorbar_manual_levels: tuple[float, float] | None
        bar: Any
        colorbar_auto_radio: qtw.QRadioButton
        colorbar_manual_radio: qtw.QRadioButton
        colorbar_max_text: qtw.QLineEdit
        colorbar_min_text: qtw.QLineEdit
        colorbar_range_entry: RangeControls
        colorbar_copy_auto_limits_button: qtw.QPushButton
        colorbar_scale_action: QtGui.QAction
        colorbar_scale_controls: qtw.QWidget
        colorbar_scale_dialog: qtw.QDialog
        _colour_range_state: RangeState
        colorbar_quantity: qtw.QLabel

        def _available_colorbar_colormaps(self) -> tuple[str, ...]: ...
        def _colorbar_colormap(self, name: str | None = None) -> Any: ...
        def _current_colorbar_colormap_name(self) -> str: ...
        def _current_colorbar_levels(self) -> tuple[float, float] | None: ...
        def _data_colorbar_levels(self) -> tuple[float, float] | None: ...
        def _finite_data_colorbar_range(self) -> tuple[float, float] | None: ...
        def _colour_fit_target(self) -> tuple[float, float] | None: ...
        def _set_colorbar_levels(self, vmin: float, vmax: float) -> bool: ...
        def scaleColorbar(self, event: Any = None) -> None: ...
else:
    class _ColorbarScaleDialogBase(PlotHost):
        pass


class ColorbarScaleDialogMixin(_ColorbarScaleDialogBase):
    """
    Dialog and table controls for heatmap color-scale configuration.

    """

    def _init_colorbar_scale_controls(self):
        """Build the shared range editor and the colour-map chooser."""
        controls = qtw.QWidget()
        outer = qtw.QVBoxLayout(controls)
        outer.setContentsMargins(10, 8, 10, 8)
        panel = qtw.QFrame()
        panel.setObjectName("axisScalePanel")
        panel.setFrameShape(qtw.QFrame.Shape.StyledPanel)
        outer.addWidget(panel)
        layout = qtw.QVBoxLayout(panel)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(8)
        self.colorbar_quantity = qtw.QLabel()
        self.colorbar_quantity.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.colorbar_quantity.setSizePolicy(qtw.QSizePolicy.Policy.Ignored, qtw.QSizePolicy.Policy.Preferred)
        font = self.colorbar_quantity.font()
        font.setBold(True)
        self.colorbar_quantity.setFont(font)
        layout.addWidget(self.colorbar_quantity)
        entry = self.colorbar_range_entry = RangeControls(
            "Colour scale", colour=True, source_controls=self._create_colour_source_controls(), parent=panel)
        layout.addWidget(entry)
        self._colour_range_state = RangeState()
        self._colour_filter_settings = (False, 100)
        entry.bind(self._finite_data_colorbar_range, self._apply_colour_range_state, fit_range=self._colour_fit_target)
        entry.visible_only.toggled.connect(self._colour_fit_options_changed)
        entry.percent.valueChanged.connect(self._colour_fit_options_changed)
        self.colorbar_auto_radio = entry.auto_radio
        self.colorbar_manual_radio = entry.manual_radio
        self.colorbar_min_text = entry.fields["min"]
        self.colorbar_max_text = entry.fields["max"]
        self.colorbar_copy_auto_limits_button = entry.fit_button
        self._sync_colour_filter_controls()

        self.colorbar_choose_map = qtw.QPushButton("Choose colour map…")
        self.colorbar_choose_map.setAutoDefault(False)
        self.colorbar_choose_map.clicked.connect(lambda: self.open_colour_map_chooser(controls.window()))
        self.colorbar_current_map = CurrentColourMap(self.colorbar_choose_map)
        layout.addLayout(self.colorbar_current_map)
        outer.addStretch()
        self.colorbar_map_dialog: ColourMapDialog | None = None

        self.colorbar_scale_controls = controls

        menu = getattr(self, "vbMenu", None)
        if menu is not None:
            self.colorbar_scale_action = QtGui.QAction("&Colour Scale...", cast(QtCore.QObject, self))
            self.colorbar_scale_action.triggered.connect(self.open_colorbar_scale_dialog)
            before = getattr(self, "autoscaleSep", None)
            menu.insertAction(before, self.colorbar_scale_action)

        self._sync_colorbar_scale_controls()

    def _create_colour_source_controls(self) -> qtw.QWidget | None:
        return None

    def _colour_filters_enabled(self) -> bool:
        return True

    def _sync_colour_filter_controls(self) -> None:
        entry = self.colorbar_range_entry
        enabled = self._colour_filters_enabled()
        visible, percent = self._colour_filter_settings if enabled else (False, 100)
        with QtCore.QSignalBlocker(entry.visible_only), QtCore.QSignalBlocker(entry.percent):
            entry.visible_only.setChecked(visible)
            entry.percent.setValue(percent)
        entry.filter_row.setEnabled(enabled)
        entry.filter_row.setToolTip("" if enabled else "Select Current slice to use these filters.")

    def _apply_colour_range_state(self, state: RangeState) -> None:
        self._colour_fit_error = ""
        self._colour_range_state = state
        if state.automatic:
            self._colorbar_manual_levels = None
            self._set_colorbar_levels(*state.limits)
            if self.__dict__.get("_heatmap_color_range_rejected", False):
                self.refreshWindow(force=True)
        else:
            self.setColorbarManualRange(*state.limits)

    def _colour_fit_options_changed(self) -> None:
        entry = self.colorbar_range_entry
        self._colour_filter_settings = entry.visible_only.isChecked(), entry.percent.value()
        entry.show_error("")
        if self.__dict__.get("_colorbar_manual_levels") is None:
            self.scaleColorbar()

    def _capture_colour_range_settings(self):
        return self._colour_range_state, *self._colour_filter_settings

    def _restore_colour_range_settings(self, saved):
        state, visible, percent = saved
        self.colorbar_range_entry.restore()
        self._colour_filter_settings = visible, percent
        self._sync_colour_filter_controls()
        self._apply_colour_range_state(state)
        if state.automatic:
            self.scaleColorbar()
        self._sync_colorbar_scale_controls()

    def open_colour_map_chooser(self, parent):
        dialog = self.colorbar_map_dialog
        if dialog is not None:
            dialog.raise_()
            dialog.activateWindow()
            return
        dialog = self.colorbar_map_dialog = ColourMapDialog(self, parent)
        dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.finished.connect(self._colour_map_chooser_finished)
        dialog.open()

    def _colour_map_chooser_finished(self):
        self.colorbar_map_dialog = None

    def _sync_current_colorbar_map(self):
        current = self.__dict__.get("colorbar_current_map")
        if current is not None:
            name = self._current_colorbar_colormap_name()
            current.set_colormap(name)
            dialog = self.colorbar_map_dialog
            if dialog is not None:
                dialog.picker.set_current_map(name)

    def _sync_colorbar_scale_controls(self):
        """
        Update color scale controls from the current colorbar state.

        """
        levels = self._current_colorbar_levels()
        if levels is not None:
            self._sync_colorbar_level_fields(*levels)

        self._sync_current_colorbar_map()

    def _sync_colorbar_scale_mode(self):
        entry = self.__dict__.get("colorbar_range_entry")
        if entry is not None:
            self._sync_colorbar_level_fields(*self._colour_range_state.limits)

    def _sync_colorbar_level_fields(self, vmin, vmax):
        entry = self.__dict__.get("colorbar_range_entry")
        if entry is None or not (vmin < vmax and np.isfinite(float(vmax) - float(vmin))):
            return
        state = replace(self._colour_range_state, limits=(float(vmin), float(vmax)),
                        automatic=self.__dict__.get("_colorbar_manual_levels") is None)
        self._colour_range_state = state
        provider = getattr(self, "_colorbar_display_parameter", None)
        param = provider() if callable(provider) else self.__dict__.get("display_param", self.__dict__.get("param"))
        label = getattr(param, "label", "") or getattr(param, "name", "") or "Colour scale"
        unit = getattr(param, "unit", "") or ""
        self.colorbar_quantity.setText(f"{label} ({unit})" if unit else label)
        self.colorbar_quantity.setToolTip(self.colorbar_quantity.text())
        entry.set_state(state, units=unit)
        if state.automatic and self.__dict__.get("_colour_fit_error"):
            entry.show_error(self._colour_fit_error)

    @QtCore.pyqtSlot(int)
    def _colorbar_colormap_changed(self, index):
        """
        Compatibility slot for older combo-box based tests or callers.

        """
        combo = getattr(self, "colorbar_colormap_combo", None)
        if combo is None:
            return

        name = combo.itemData(index)
        if name is not None:
            self.setColorbarColorMap(name)

    @QtCore.pyqtSlot()
    def open_colorbar_scale_dialog(self):
        """Open the colour-scale range controls and current map."""
        controls = getattr(self, "colorbar_scale_controls", None)
        if controls is None:
            return
        open_settings = self.__dict__.get("_open_plot_settings_page")
        if open_settings is not None:
            open_settings(page="Colour scale")
            return

        self._sync_colorbar_scale_controls()
        dialog = getattr(self, "colorbar_scale_dialog", None)
        if dialog is None:
            dialog = qtw.QDialog(cast(qtw.QWidget, self))
            dialog.setWindowTitle("Colour scale")
            dialog.finished.connect(self.close_colour_map_chooser)
            layout = qtw.QVBoxLayout(dialog)
            layout.addWidget(controls)

            buttons = qtw.QDialogButtonBox(qtw.QDialogButtonBox.StandardButton.Close)
            buttons.rejected.connect(dialog.close)
            layout.addWidget(buttons)

            self.colorbar_scale_dialog = dialog

        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def close_colour_map_chooser(self):
        if self.colorbar_map_dialog is not None:
            self.colorbar_map_dialog.reject()

    def setColorbarColorMap(self, name):
        """
        Apply the colour map to this plot without saving a default.

        """
        if name not in self._available_colorbar_colormaps():
            show_status = getattr(self, "show_status", None)
            if show_status is not None:
                show_status("Unknown colour map.", 5000)
            return False

        self._colorbar_colormap_name = name
        bar = self.__dict__.get("bar")
        if bar is not None:
            bar.setColorMap(self._colorbar_colormap(name))

        self._sync_current_colorbar_map()

        appearance_dialog = self.__dict__.get("_heatmap_appearance_dialog")
        if appearance_dialog is not None:
            appearance_dialog.refresh_color_scale()

        return True

    def setColorbarManualRange(self, vmin, vmax):
        """
        Set a persistent manual color scale range.

        """
        if not np.isfinite(vmin) or not np.isfinite(vmax) or vmin >= vmax:
            self.show_status("Colour scale minimum must be below maximum.", 5000)
            self._sync_colorbar_scale_controls()
            return False

        if not np.isfinite(float(vmax) - float(vmin)):
            self.show_status("The colour scale range is too wide to display safely.", 5000)
            self._sync_colorbar_scale_controls()
            return False

        levels = float(vmin), float(vmax)
        state = self.__dict__.get("_colour_range_state")
        if state is not None and state.lock and levels != state.limits:
            try:
                levels = state.fit(levels).limits
            except ValueError as exc:
                self.colorbar_range_entry.show_error(str(exc))
                return False
        self._colorbar_manual_levels = levels

        minimum, maximum = self._colorbar_manual_levels
        self._set_colorbar_levels(minimum, maximum)
        if self.__dict__.get("_heatmap_color_range_rejected", False):
            self.refreshWindow(force=True)
        return True

    @QtCore.pyqtSlot()
    def setColorbarAuto(self):
        """
        Return the color scale to automatic data-range scaling.

        """
        entry = self.__dict__.get("colorbar_range_entry")
        if entry is not None:
            entry.restore()
        self._colorbar_manual_levels = None
        self._sync_colorbar_scale_mode()
        self.scaleColorbar()
