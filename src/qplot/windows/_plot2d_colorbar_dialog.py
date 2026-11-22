from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

import numpy as np
from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

from ._colour_map_picker import ColourMapPicker
from ._plot_host import PlotHost
from ._range_controls import RangeControls, RangeState

if TYPE_CHECKING:
    class _ColorbarScaleDialogBase(PlotHost):
        _colorbar_colormap_name: str
        _colorbar_manual_levels: tuple[float, float] | None
        bar: Any
        colorbar_auto_radio: qtw.QRadioButton
        colorbar_colormap_table: qtw.QTableWidget
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
        entry = self.colorbar_range_entry = RangeControls("Colour scale", colour=True, parent=panel)
        layout.addWidget(entry)
        self._colour_range_state = RangeState()
        entry.bind(self._finite_data_colorbar_range, self._apply_colour_range_state, fit_range=self._colour_fit_target)
        entry.visible_only.toggled.connect(self._colour_fit_options_changed)
        entry.percent.valueChanged.connect(self._colour_fit_options_changed)
        self.colorbar_auto_radio = entry.auto_radio
        self.colorbar_manual_radio = entry.manual_radio
        self.colorbar_min_text = entry.fields["min"]
        self.colorbar_max_text = entry.fields["max"]
        self.colorbar_copy_auto_limits_button = entry.fit_button
        options = qtw.QHBoxLayout()
        self._init_colorbar_scope_controls(options)
        if options.count():
            options.addStretch()
            layout.addLayout(options)

        self.colorbar_map_picker = ColourMapPicker(
            self._available_colorbar_colormaps(), self._current_colorbar_colormap_name(), controls,
        )
        picker = self.colorbar_map_picker
        self.colorbar_colormap_table = picker.table
        self.colorbar_search = picker.search
        self.colorbar_source_combo = picker.source_combo
        self.colorbar_category_combo = picker.category_combo
        self.colorbar_current_name = picker.current_name
        self.colorbar_current_preview = picker.current_preview
        self.colorbar_no_matches = picker.no_matches
        self.colorbar_show_current_button = picker.show_current_button
        self.colorbar_current_heading = picker.current_heading
        self.colorbar_picker_heading = picker.picker_heading
        picker.mapSelected.connect(self.setColorbarColorMap)
        layout.addWidget(picker, 1)

        self.colorbar_scale_controls = controls

        menu = getattr(self, "vbMenu", None)
        if menu is not None:
            self.colorbar_scale_action = QtGui.QAction("&Colour Scale...", cast(QtCore.QObject, self))
            self.colorbar_scale_action.triggered.connect(self.open_colorbar_scale_dialog)
            before = getattr(self, "autoscaleSep", None)
            menu.insertAction(before, self.colorbar_scale_action)

        self._sync_colorbar_scale_controls()

    def _init_colorbar_scope_controls(self, layout):
        """Allow volume plots to expose their automatic colour scope here."""

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
        self.colorbar_range_entry.show_error("")
        if self.__dict__.get("_colorbar_manual_levels") is None:
            self.scaleColorbar()

    def _capture_colour_range_settings(self):
        entry = self.colorbar_range_entry
        return self._colour_range_state, entry.visible_only.isChecked(), entry.percent.value()

    def _restore_colour_range_settings(self, saved):
        state, visible, percent = saved
        entry = self.colorbar_range_entry
        entry.restore()
        with QtCore.QSignalBlocker(entry.visible_only), QtCore.QSignalBlocker(entry.percent):
            entry.visible_only.setChecked(visible)
            entry.percent.setValue(percent)
        self._apply_colour_range_state(state)
        if state.automatic:
            self.scaleColorbar()
        self._sync_colorbar_scale_controls()

    def _populate_colorbar_colormap_table(self):
        self.colorbar_map_picker.set_maps(
            self._available_colorbar_colormaps(), self._current_colorbar_colormap_name(),
        )

    def _sync_current_colorbar_map(self):
        picker = self.__dict__.get("colorbar_map_picker")
        if picker is not None:
            picker.set_current_map(self._current_colorbar_colormap_name())

    def _sync_colorbar_scale_controls(self):
        """
        Update color scale controls from the current colorbar state.

        """
        levels = self._current_colorbar_levels()
        if levels is not None:
            self._sync_colorbar_level_fields(*levels)

        table = getattr(self, "colorbar_colormap_table", None)
        if table is not None:
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

    def _colorbar_colormap_row(self, name):
        picker = self.__dict__.get("colorbar_map_picker")
        return picker.row_for_name(name) if picker is not None else -1

    def _select_colorbar_colormap(self, name):
        picker = self.__dict__.get("colorbar_map_picker")
        if picker is not None:
            picker.select_map(name)

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
    def _show_current_colorbar_map(self):
        self.colorbar_map_picker.reveal_current()

    @QtCore.pyqtSlot()
    def open_colorbar_scale_dialog(self):
        """Open the colour-scale controls and reveal the active map."""
        controls = getattr(self, "colorbar_scale_controls", None)
        if controls is None:
            return
        open_settings = self.__dict__.get("_open_plot_settings_page")
        if open_settings is not None:
            open_settings(page="Colour scale")
            return

        self._populate_colorbar_colormap_table()
        self._sync_colorbar_scale_controls()
        self._show_current_colorbar_map()
        table = self.colorbar_colormap_table
        row = self._colorbar_colormap_row(self._current_colorbar_colormap_name())

        dialog = getattr(self, "colorbar_scale_dialog", None)
        first_open = dialog is None
        if dialog is None:
            dialog = qtw.QDialog(cast(qtw.QWidget, self))
            dialog.setWindowTitle("Colour scale")
            dialog.resize(480, 560)
            layout = qtw.QVBoxLayout(dialog)
            layout.addWidget(controls)

            buttons = qtw.QDialogButtonBox(qtw.QDialogButtonBox.StandardButton.Close)
            buttons.rejected.connect(dialog.close)
            layout.addWidget(buttons)

            self.colorbar_scale_dialog = dialog

        dialog.show()
        if first_open:
            # Measure the styled viewport after layout, including the actual
            # frame/header sizes, so the default ends on six complete rows.
            dialog_layout = dialog.layout()
            header = table.verticalHeader()
            viewport = table.viewport()
            assert dialog_layout is not None and header is not None and viewport is not None
            dialog_layout.activate()
            row_height = table.rowHeight(0) if table.rowCount() else header.defaultSectionSize()
            viewport_height = 6 * row_height
            table.setMinimumHeight(table.height() - viewport.height() + viewport_height)
            dialog_layout.activate()
            dialog.resize(dialog.width(), dialog.height() + viewport_height - viewport.height())
            dialog_layout.activate()
        if row >= 0:
            table.scrollToItem(table.item(row, 0), qtw.QAbstractItemView.ScrollHint.PositionAtCenter)
        dialog.raise_()
        dialog.activateWindow()

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
