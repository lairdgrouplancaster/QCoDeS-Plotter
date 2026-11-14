from typing import TYPE_CHECKING, Any, cast

import numpy as np
from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

from ._colour_map_picker import ColourMapPicker
from ._plot_host import PlotHost
from ._range_entry import AutoLimitsButton as _ColorbarAutoLimitsButton
from ._range_entry import CompactNumberEdit, RangeEntry, layout_range_fields

if TYPE_CHECKING:
    class _ColorbarScaleDialogBase(PlotHost):
        _colorbar_colormap_name: str
        _colorbar_manual_levels: tuple[float, float] | None
        bar: Any
        colorbar_auto_radio: qtw.QRadioButton
        colorbar_button_group: qtw.QButtonGroup
        colorbar_colormap_table: qtw.QTableWidget
        colorbar_manual_radio: qtw.QRadioButton
        colorbar_max_label: qtw.QLabel
        colorbar_max_text: qtw.QLineEdit
        colorbar_min_label: qtw.QLabel
        colorbar_min_text: qtw.QLineEdit
        colorbar_range_entry: RangeEntry
        colorbar_copy_auto_limits_button: qtw.QToolButton
        colorbar_scale_action: QtGui.QAction
        colorbar_scale_controls: qtw.QWidget
        colorbar_scale_dialog: qtw.QDialog
        relevel_refresh: qtw.QCheckBox

        def _available_colorbar_colormaps(self) -> tuple[str, ...]: ...
        def _colorbar_colormap(self, name: str | None = None) -> Any: ...
        def _current_colorbar_colormap_name(self) -> str: ...
        def _current_colorbar_levels(self) -> tuple[float, float] | None: ...
        def _data_colorbar_levels(self) -> tuple[float, float] | None: ...
        def _set_colorbar_levels(self, vmin: float, vmax: float) -> bool: ...
        def scaleColorbar(self, event: Any = None) -> None: ...
else:
    class _ColorbarScaleDialogBase(PlotHost):
        pass


class ColorbarScaleDialogMixin(_ColorbarScaleDialogBase):
    """
    Dialog and table controls for heatmap color-scale configuration.

    """

    def _init_colorbar_refresh_control(self):
        """Keep the refresh choice available even for hidden overlay sources."""
        self.relevel_refresh = qtw.QCheckBox("Autoscale on refresh", cast(qtw.QWidget, self))
        self.relevel_refresh.setChecked(True)
        self.relevel_refresh.setToolTip(
            "Automatically adjust the colour range on each refresh. "
            "Manual scaling temporarily disables this option without changing your choice."
        )
        self.relevel_refresh.toggled.connect(self._colorbar_auto_refresh_changed)
        self.relevel_refresh.hide()

    def _init_colorbar_scale_controls(self):
        """
        Build manual/auto color scale controls for the dialog.

        """
        controls = qtw.QWidget()
        layout = qtw.QVBoxLayout(controls)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(8)

        self.colorbar_manual_radio = qtw.QRadioButton("Manual")
        self.colorbar_manual_radio.setToolTip("Set fixed colour limits using the range fields.")
        self.colorbar_auto_radio = qtw.QRadioButton("Auto")
        self.colorbar_auto_radio.setToolTip("Calculate colour limits automatically from the data.")
        if "relevel_refresh" not in self.__dict__:
            self._init_colorbar_refresh_control()
        self.symmetric_color = qtw.QCheckBox("Symmetric about zero")
        self.symmetric_color.setChecked(self.__dict__.get("_colorbar_symmetric", False))
        self.symmetric_color.setToolTip(
            "Keep colour limits equally spaced above and below zero. "
            "Automatic limits use the larger absolute value; editing either limit mirrors the other."
        )
        self.symmetric_color.toggled.connect(self._colorbar_symmetric_changed)
        self.colorbar_min_label = qtw.QLabel("Minimum")
        self.colorbar_max_label = qtw.QLabel("Maximum")
        self.colorbar_min_text = CompactNumberEdit()
        self.colorbar_max_text = CompactNumberEdit()
        self.colorbar_copy_auto_limits_button = _ColorbarAutoLimitsButton(
            self._update_colorbar_auto_limits_tooltip,
            controls,
        )
        self.colorbar_copy_auto_limits_button.setObjectName(
            "colorbarCopyAutoLimitsButton"
        )
        self.colorbar_copy_auto_limits_button.setAccessibleName(
            "Use auto colour scale limits as manual limits"
        )
        self.colorbar_copy_auto_limits_button.setFixedSize(26, 24)
        validator = QtGui.QDoubleValidator(cast(QtCore.QObject, self))
        # Match Python's dot-decimal parsing/formatting regardless of Qt's
        # default locale. Grouping must never change an input's meaning.
        numeric_locale = QtCore.QLocale.c()
        numeric_locale.setNumberOptions(
            numeric_locale.numberOptions()
            | QtCore.QLocale.NumberOption.RejectGroupSeparator
        )
        validator.setLocale(numeric_locale)
        validator.setNotation(QtGui.QDoubleValidator.Notation.ScientificNotation)
        self.colorbar_min_text.setValidator(validator)
        self.colorbar_max_text.setValidator(validator)
        self.colorbar_min_label.setBuddy(self.colorbar_min_text)
        self.colorbar_max_label.setBuddy(self.colorbar_max_text)
        self.colorbar_min_text.setAccessibleName("Colour scale minimum")
        self.colorbar_max_text.setAccessibleName("Colour scale maximum")
        for line_edit in (self.colorbar_min_text, self.colorbar_max_text):
            line_edit.setMinimumWidth(80)
            line_edit.setToolTip(
                "Use a decimal point (e.g. 1.5 or 1.5e-3), without grouping separators."
                " Values are shown rounded; focus the field to see and edit full precision."
            )
        self.colorbar_range_entry = RangeEntry(
            self.colorbar_min_text, self.colorbar_max_text,
            self.colorbar_min_label, self.colorbar_max_label,
            accessible_prefix="Colour scale", parent=controls,
        )
        self.colorbar_range_entry.group.idClicked.connect(self._sync_colorbar_scale_controls)

        self.colorbar_button_group = qtw.QButtonGroup(cast(QtCore.QObject, self))
        self.colorbar_button_group.addButton(self.colorbar_manual_radio)
        self.colorbar_button_group.addButton(self.colorbar_auto_radio)
        self.colorbar_button_group.setExclusive(True)

        range_controls = qtw.QWidget()
        range_layout = qtw.QGridLayout(range_controls)
        layout_range_fields(
            range_layout, self.colorbar_range_entry, self.colorbar_manual_radio,
            self.colorbar_min_text, self.colorbar_max_text,
            self.colorbar_min_label, self.colorbar_max_label,
            self.colorbar_copy_auto_limits_button,
        )
        range_layout.addWidget(self.colorbar_auto_radio, 4, 0)
        auto_options = qtw.QHBoxLayout()
        auto_options.setSpacing(16)
        auto_options.addWidget(self.relevel_refresh)
        auto_options.addWidget(self.symmetric_color)
        auto_options.addStretch()
        range_layout.addLayout(auto_options, 4, 1, 1, 3)
        self.relevel_refresh.show()

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
        self.colorbar_scaling_heading = picker.insert_scaling_controls(range_controls)
        picker.mapSelected.connect(self.setColorbarColorMap)
        layout.addWidget(picker, 1)
        options = qtw.QHBoxLayout()
        self._init_colorbar_scope_controls(options)
        if options.count():
            options.addStretch()
            range_layout.addLayout(options, 5, 1, 1, 3)

        self.colorbar_scale_controls = controls

        menu = getattr(self, "vbMenu", None)
        if menu is not None:
            self.colorbar_scale_action = QtGui.QAction("&Colour Scale...", cast(QtCore.QObject, self))
            self.colorbar_scale_action.triggered.connect(self.open_colorbar_scale_dialog)
            before = getattr(self, "autoscaleSep", None)
            menu.insertAction(before, self.colorbar_scale_action)

        self.colorbar_manual_radio.clicked.connect(self._apply_colorbar_manual_fields)
        self.colorbar_range_entry.bind_auto_mode(
            self.colorbar_auto_radio, lambda limits: self.setColorbarManualRange(*limits),
        )
        self.colorbar_min_text.editingFinished.connect(self._apply_colorbar_manual_fields)
        self.colorbar_max_text.editingFinished.connect(self._apply_colorbar_manual_fields)
        self.colorbar_auto_radio.clicked.connect(self.setColorbarAuto)
        self.colorbar_copy_auto_limits_button.clicked.connect(
            self._copy_colorbar_auto_limits
        )

        self._sync_colorbar_scale_controls()

    def _init_colorbar_scope_controls(self, layout):
        """Allow volume plots to expose their automatic colour scope here."""

    def _symmetric_colorbar_levels(self, vmin, vmax):
        if self.__dict__.get("_colorbar_symmetric", False):
            extent = max(abs(float(vmin)), abs(float(vmax)))
            return -extent, extent
        return vmin, vmax

    def _colorbar_symmetric_changed(self, enabled):
        levels = self._current_colorbar_levels()
        if enabled and levels is not None:
            extent = max(abs(float(value)) for value in levels)
            if not np.isfinite(2 * extent):
                self.show_status("The symmetric colour scale range is too wide to display safely.", 5000)
                with QtCore.QSignalBlocker(self.symmetric_color):
                    self.symmetric_color.setChecked(False)
                return
        self._colorbar_symmetric = enabled
        manual_levels = self.__dict__.get("_colorbar_manual_levels")
        if manual_levels is not None:
            self.setColorbarManualRange(*manual_levels)
        else:
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

        self._sync_colorbar_scale_mode()
        fixed_center = self.__dict__.get("_colorbar_symmetric", False) and self.colorbar_range_entry.center_span
        self.colorbar_min_text.setEnabled(not fixed_center)
        self.colorbar_min_label.setEnabled(not fixed_center)
        self._update_colorbar_auto_limits_tooltip()

    def _sync_colorbar_scale_mode(self):
        """Suspend refresh autoscaling in manual mode, retaining the choice."""
        manual = self.__dict__.get("_colorbar_manual_levels") is not None
        entry = self.__dict__.get("colorbar_range_entry")
        if entry is not None:
            entry.set_automatic(not manual)
        for name, checked in (("colorbar_manual_radio", manual), ("colorbar_auto_radio", not manual)):
            widget = self.__dict__.get(name)
            if widget is not None:
                with QtCore.QSignalBlocker(widget):
                    widget.setChecked(checked)
        checkbox = self.__dict__.get("relevel_refresh")
        if checkbox is not None:
            checkbox.setEnabled(not manual)

    def _colorbar_auto_limits(self):
        """Return validated prospective automatic color-scale limits."""

        levels = self._data_colorbar_levels()
        if levels is None:
            return None
        try:
            vmin, vmax = (float(value) for value in levels)
        except (TypeError, ValueError):
            return None
        if not np.isfinite(vmin) or not np.isfinite(vmax) or vmin >= vmax:
            return None
        return vmin, vmax

    def _update_colorbar_auto_limits_tooltip(self):
        """Describe the range that the copy-auto button would apply."""

        button = self.colorbar_copy_auto_limits_button
        levels = self._colorbar_auto_limits()
        button.setEnabled(levels is not None)
        if levels is None:
            button.setToolTip("Auto limits are unavailable for the colour scale.")
            return
        button.setToolTip(
            f"Set manual limits to {float(levels[0])!r} and {float(levels[1])!r}."
        )

    @QtCore.pyqtSlot()
    def _copy_colorbar_auto_limits(self):
        """Freeze the prospective automatic color range as a manual range."""

        levels = self._colorbar_auto_limits()
        if levels is None:
            self._update_colorbar_auto_limits_tooltip()
            return
        self.colorbar_range_entry.clear_error()
        if self.setColorbarManualRange(*levels):
            self._sync_colorbar_level_fields(*levels)
        self._update_colorbar_auto_limits_tooltip()

    def _sync_colorbar_level_fields(self, vmin, vmax):
        """
        Mirror colorbar levels into the dialog text fields.

        """
        if "colorbar_min_text" not in self.__dict__:
            return

        self.colorbar_range_entry.set_limits(vmin, vmax)

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

    @QtCore.pyqtSlot()
    def _apply_colorbar_manual_fields(self):
        """
        Apply color scale levels entered in the dialog.

        """
        entry = self.colorbar_range_entry
        # Merely leaving a field or changing format must preserve Auto. An
        # explicit click on Manual still freezes the current exact endpoints.
        if entry.unchanged() and not self.colorbar_manual_radio.isChecked():
            entry.restore()
            return
        try:
            if self.__dict__.get("_colorbar_symmetric", False) and not entry.center_span:
                fields = (self.colorbar_min_text, self.colorbar_max_text)
                if not all(field.hasAcceptableInput() for field in fields):
                    raise ValueError("Invalid numeric input")
                values = tuple(float(field.text()) for field in fields)
                if not all(np.isfinite(value) for value in values):
                    raise ValueError("Invalid numeric input")
                previous = self._current_colorbar_levels()
                changed = [i for i in (0, 1) if previous is None or values[i] != previous[i]]
                extent = abs(values[changed[0]]) if len(changed) == 1 else max(map(abs, values))
                vmin, vmax = -extent, extent
            else:
                vmin, vmax = entry.limits_from_fields()
                if self.__dict__.get("_colorbar_symmetric", False) and entry.center_span:
                    extent = (vmax - vmin) / 2
                    vmin, vmax = -extent, extent
        except ValueError:
            self.show_status(
                "Colour scale ranges must use finite numbers with a positive span."
                if entry.center_span else "Invalid colour scale range.", 5000,
            )
            entry.show_error(entry.validation_message())
            return

        if not np.isfinite(vmax - vmin) or vmin >= vmax:
            entry.show_error("The colour range must have finite, increasing limits.")
            return
        entry.clear_error()
        self.setColorbarManualRange(vmin, vmax)

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

        vmin, vmax = self._symmetric_colorbar_levels(vmin, vmax)
        if not np.isfinite(float(vmax) - float(vmin)):
            self.show_status("The colour scale range is too wide to display safely.", 5000)
            self._sync_colorbar_scale_controls()
            return False

        self._colorbar_manual_levels = (float(vmin), float(vmax))

        minimum, maximum = self._colorbar_manual_levels
        self._set_colorbar_levels(minimum, maximum)
        self._sync_colorbar_scale_mode()
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
            entry.clear_error()
        self._colorbar_manual_levels = None
        self._sync_colorbar_scale_mode()
        self.scaleColorbar()

    @QtCore.pyqtSlot(bool)
    def _colorbar_auto_refresh_changed(self, enabled):
        """
        Apply newly enabled refresh autoscaling only in automatic mode.

        """
        if enabled and self.__dict__.get("_colorbar_manual_levels") is None:
            self.scaleColorbar()
