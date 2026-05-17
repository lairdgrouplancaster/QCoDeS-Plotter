import numpy as np
from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

from ._colorbar import (
    _CET_COLORBAR_SUBTYPES,
    _COLORBAR_COLORMAP_LABELS,
    _COLORBAR_TABLE_SORT_ROLE,
    _MATPLOTLIB_COLORBAR_SUBTYPES,
    _colorbar_colormap_preview,
    _colorbar_colormap_type_label,
    _colorbar_subtype_config_key,
    _ColorbarColormapTableItem,
    _config_value,
)


class _CenteredIconDelegate(qtw.QStyledItemDelegate):
    """
    Paint icon-only table cells with the icon centered in the cell.

    """

    def paint(self, painter, option, index):
        icon = index.data(QtCore.Qt.ItemDataRole.DecorationRole)
        if not isinstance(icon, QtGui.QIcon) or icon.isNull():
            super().paint(painter, option, index)
            return

        opt = qtw.QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        opt.icon = QtGui.QIcon()

        widget = opt.widget
        style = widget.style() if widget else qtw.QApplication.style()
        style.drawControl(qtw.QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)

        icon_size = opt.decorationSize
        if not icon_size.isValid() or icon_size.isEmpty():
            icon_size = icon.actualSize(opt.rect.size())
        icon_size = icon_size.boundedTo(opt.rect.size())
        icon_rect = QtCore.QRect(QtCore.QPoint(0, 0), icon_size)
        icon_rect.moveCenter(opt.rect.center())

        mode = QtGui.QIcon.Normal
        if not opt.state & qtw.QStyle.StateFlag.State_Enabled:
            mode = QtGui.QIcon.Disabled
        elif opt.state & qtw.QStyle.StateFlag.State_Selected:
            mode = QtGui.QIcon.Selected

        icon.paint(painter, icon_rect, QtCore.Qt.AlignmentFlag.AlignCenter, mode)


class ColorbarScaleDialogMixin:
    """
    Dialog and table controls for heatmap color-scale configuration.

    """

    def _init_colorbar_scale_controls(self):
        """
        Build manual/auto color scale controls for the dialog.

        """
        controls = qtw.QWidget()
        layout = qtw.QVBoxLayout(controls)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(8)

        self.colorbar_manual_radio = qtw.QRadioButton("Manual")
        self.colorbar_auto_radio = qtw.QRadioButton("Auto")
        self.colorbar_min_text = qtw.QLineEdit()
        self.colorbar_max_text = qtw.QLineEdit()
        self.colorbar_colormap_table = qtw.QTableWidget()
        self.colorbar_include_cet_check = qtw.QCheckBox("CET")
        self.colorbar_include_matplotlib_check = qtw.QCheckBox("Matplotlib")
        self.colorbar_include_local_check = qtw.QCheckBox("Local")
        self.colorbar_include_custom_check = qtw.QCheckBox("Custom")
        self.colorbar_cet_subtype_checks = {}
        self.colorbar_matplotlib_subtype_checks = {}
        for subtype, label in _CET_COLORBAR_SUBTYPES:
            self.colorbar_cet_subtype_checks[subtype] = qtw.QCheckBox(label)
        for subtype, label in _MATPLOTLIB_COLORBAR_SUBTYPES:
            self.colorbar_matplotlib_subtype_checks[subtype] = qtw.QCheckBox(label)
        self._init_colorbar_colormap_table()

        validator = QtGui.QDoubleValidator(self)
        self.colorbar_min_text.setValidator(validator)
        self.colorbar_max_text.setValidator(validator)
        for line_edit in (self.colorbar_min_text, self.colorbar_max_text):
            line_edit.setMinimumWidth(80)

        self.colorbar_button_group = qtw.QButtonGroup(self)
        self.colorbar_button_group.addButton(self.colorbar_manual_radio)
        self.colorbar_button_group.addButton(self.colorbar_auto_radio)
        self.colorbar_button_group.setExclusive(True)

        range_controls = qtw.QWidget()
        range_layout = qtw.QGridLayout(range_controls)
        range_layout.setContentsMargins(0, 0, 0, 0)
        range_layout.setHorizontalSpacing(4)
        range_layout.setVerticalSpacing(4)
        range_layout.addWidget(self.colorbar_manual_radio, 0, 0)
        range_layout.addWidget(self.colorbar_min_text, 0, 1)
        range_layout.addWidget(self.colorbar_max_text, 0, 2)
        range_layout.addWidget(self.colorbar_auto_radio, 1, 0)

        filter_controls = self._init_colorbar_filter_controls()

        layout.addWidget(qtw.QLabel("Colors"))
        layout.addWidget(filter_controls)
        layout.addWidget(self.colorbar_colormap_table, 1)
        layout.addWidget(range_controls)

        self.colorbar_scale_controls = controls

        self.colorbar_colormap_table.itemSelectionChanged.connect(
            self._colorbar_colormap_selection_changed
            )
        self.colorbar_include_cet_check.toggled.connect(
            self._colorbar_include_cet_changed
            )
        self.colorbar_include_matplotlib_check.toggled.connect(
            self._colorbar_include_matplotlib_changed
            )
        self.colorbar_include_local_check.toggled.connect(
            self._colorbar_include_local_changed
            )
        self.colorbar_include_custom_check.toggled.connect(
            self._colorbar_include_custom_changed
            )
        for subtype, widget in self.colorbar_cet_subtype_checks.items():
            widget.toggled.connect(
                lambda enabled, subtype=subtype: self._colorbar_include_subtype_changed(
                    "cet",
                    subtype,
                    enabled,
                )
            )
        for subtype, widget in self.colorbar_matplotlib_subtype_checks.items():
            widget.toggled.connect(
                lambda enabled, subtype=subtype: self._colorbar_include_subtype_changed(
                    "matplotlib",
                    subtype,
                    enabled,
                )
            )
        self.colorbar_manual_radio.clicked.connect(self._apply_colorbar_manual_fields)
        self.colorbar_min_text.editingFinished.connect(self._apply_colorbar_manual_fields)
        self.colorbar_max_text.editingFinished.connect(self._apply_colorbar_manual_fields)
        self.colorbar_auto_radio.clicked.connect(self.setColorbarAuto)

        self._sync_colorbar_scale_controls()

    def _init_colorbar_filter_controls(self):
        """
        Build broad and subtype color-map filter controls.

        """
        controls = qtw.QWidget()
        layout = qtw.QGridLayout(controls)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(2)

        layout.addWidget(qtw.QLabel("Show"), 0, 0)
        layout.addWidget(self.colorbar_include_cet_check, 0, 1)
        layout.addWidget(self.colorbar_include_matplotlib_check, 0, 2)
        layout.addWidget(self.colorbar_include_local_check, 0, 3)
        layout.addWidget(self.colorbar_include_custom_check, 0, 4)

        layout.addWidget(qtw.QLabel("CET"), 1, 0)
        for column, (_subtype, widget) in enumerate(
                self.colorbar_cet_subtype_checks.items(),
                1,
                ):
            layout.addWidget(widget, 1, column)

        layout.addWidget(qtw.QLabel("Matplotlib"), 2, 0)
        for column, (_subtype, widget) in enumerate(
                self.colorbar_matplotlib_subtype_checks.items(),
                1,
                ):
            layout.addWidget(widget, 2, column)

        layout.setColumnStretch(7, 1)
        return controls

    def _init_colorbar_colormap_table(self):
        """
        Build the scrollable color-map chooser with rendered previews.

        """
        table = self.colorbar_colormap_table
        table.setColumnCount(3)
        table.setHorizontalHeaderLabels(("Color map", "Preview", "Type"))
        table.verticalHeader().hide()
        table.setShowGrid(False)
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(qtw.QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(qtw.QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(qtw.QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSortingEnabled(True)
        table.setMinimumSize(560, 360)
        table.setIconSize(QtCore.QSize(220, 14))
        table.setItemDelegateForColumn(1, _CenteredIconDelegate(table))

        header = table.horizontalHeader()
        header.setSectionResizeMode(0, qtw.QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, qtw.QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, qtw.QHeaderView.ResizeMode.ResizeToContents)

        self._populate_colorbar_colormap_table()

    def _populate_colorbar_colormap_table(self):
        """
        Fill the color-map table using the current filter settings.

        """
        table = self.colorbar_colormap_table
        table.blockSignals(True)
        sorting_enabled = table.isSortingEnabled()
        table.setSortingEnabled(False)
        table.setRowCount(0)

        colormaps = self._available_colorbar_colormaps()
        table.setRowCount(len(colormaps))
        for row, name in enumerate(colormaps):
            label = _COLORBAR_COLORMAP_LABELS.get(name, name)
            type_label = _colorbar_colormap_type_label(name)
            name_item = _ColorbarColormapTableItem(label)
            name_item.setData(QtCore.Qt.ItemDataRole.UserRole, name)
            name_item.setData(_COLORBAR_TABLE_SORT_ROLE, label)
            type_item = _ColorbarColormapTableItem(type_label)
            type_item.setData(QtCore.Qt.ItemDataRole.UserRole, name)
            type_item.setData(_COLORBAR_TABLE_SORT_ROLE, type_label)
            preview_item = _ColorbarColormapTableItem()
            preview_item.setData(QtCore.Qt.ItemDataRole.UserRole, name)
            preview_item.setData(_COLORBAR_TABLE_SORT_ROLE, label)
            preview_item.setIcon(QtGui.QIcon(_colorbar_colormap_preview(name)))

            table.setItem(row, 0, name_item)
            table.setItem(row, 1, preview_item)
            table.setItem(row, 2, type_item)
            table.setRowHeight(row, 20)

        table.resizeColumnToContents(0)
        table.resizeColumnToContents(2)
        table.setSortingEnabled(sorting_enabled)
        table.blockSignals(False)

    def _sync_colorbar_filter_controls(self):
        """
        Mirror persisted broad color-map filters into the dialog controls.

        """
        config_obj = self.__dict__.get("config")
        for widget, key in (
                (
                    self.colorbar_include_cet_check,
                    "user_preference.bar_colour_include_cet",
                    ),
                (
                    self.colorbar_include_matplotlib_check,
                    "user_preference.bar_colour_include_matplotlib",
                    ),
                (
                    self.colorbar_include_local_check,
                    "user_preference.bar_colour_include_local",
                    ),
                (
                    self.colorbar_include_custom_check,
                    "user_preference.bar_colour_include_custom",
                    ),
                ):
            widget.blockSignals(True)
            widget.setChecked(bool(_config_value(config_obj, key, True)))
            widget.blockSignals(False)

        include_cet = self.colorbar_include_cet_check.isChecked()
        include_matplotlib = self.colorbar_include_matplotlib_check.isChecked()
        for subtype, widget in self.colorbar_cet_subtype_checks.items():
            key = _colorbar_subtype_config_key("cet", subtype)
            widget.blockSignals(True)
            widget.setChecked(bool(_config_value(config_obj, key, True)))
            widget.setEnabled(include_cet)
            widget.blockSignals(False)

        for subtype, widget in self.colorbar_matplotlib_subtype_checks.items():
            key = _colorbar_subtype_config_key("matplotlib", subtype)
            widget.blockSignals(True)
            widget.setChecked(bool(_config_value(config_obj, key, True)))
            widget.setEnabled(include_matplotlib)
            widget.blockSignals(False)

    def _set_colorbar_filter_setting(self, key, enabled):
        """
        Persist a broad color-map filter and rebuild the chooser.

        """
        config_obj = self.__dict__.get("config")
        if config_obj is not None:
            config_obj.update(key, bool(enabled))

        self._populate_colorbar_colormap_table()
        self._sync_colorbar_scale_controls()

    def _set_colorbar_subtype_filter_setting(self, group, subtype, enabled):
        """
        Persist a color-map subtype filter and rebuild the chooser.

        """
        self._set_colorbar_filter_setting(
            _colorbar_subtype_config_key(group, subtype),
            enabled,
        )

    @QtCore.pyqtSlot(bool)
    def _colorbar_include_cet_changed(self, enabled):
        """
        Include or exclude CET color maps in the chooser.

        """
        self._set_colorbar_filter_setting(
            "user_preference.bar_colour_include_cet",
            enabled,
        )

    @QtCore.pyqtSlot(bool)
    def _colorbar_include_matplotlib_changed(self, enabled):
        """
        Include or exclude matplotlib color maps in the chooser.

        """
        self._set_colorbar_filter_setting(
            "user_preference.bar_colour_include_matplotlib",
            enabled,
        )

    @QtCore.pyqtSlot(bool)
    def _colorbar_include_local_changed(self, enabled):
        """
        Include or exclude pyqtgraph local color maps in the chooser.

        """
        self._set_colorbar_filter_setting(
            "user_preference.bar_colour_include_local",
            enabled,
        )

    @QtCore.pyqtSlot(bool)
    def _colorbar_include_custom_changed(self, enabled):
        """
        Include or exclude app-provided custom color maps in the chooser.

        """
        self._set_colorbar_filter_setting(
            "user_preference.bar_colour_include_custom",
            enabled,
        )

    def _colorbar_include_subtype_changed(self, group, subtype, enabled):
        """
        Include or exclude a color-map subtype in the chooser.

        """
        self._set_colorbar_subtype_filter_setting(group, subtype, enabled)

    def _sync_colorbar_scale_controls(self):
        """
        Update color scale controls from the current colorbar state.

        """
        levels = self._current_colorbar_levels()
        if levels is not None:
            self._sync_colorbar_level_fields(*levels)

        table = getattr(self, "colorbar_colormap_table", None)
        if table is not None:
            self._sync_colorbar_filter_controls()
            self._select_colorbar_colormap(self._current_colorbar_colormap_name())

        manual = getattr(self, "_colorbar_manual_levels", None) is not None
        for widget in (self.colorbar_manual_radio, self.colorbar_auto_radio):
            widget.blockSignals(True)

        self.colorbar_manual_radio.setChecked(manual)
        self.colorbar_auto_radio.setChecked(not manual)

        for widget in (self.colorbar_manual_radio, self.colorbar_auto_radio):
            widget.blockSignals(False)

    def _sync_colorbar_level_fields(self, vmin, vmax):
        """
        Mirror colorbar levels into the dialog text fields.

        """
        if "colorbar_min_text" not in self.__dict__:
            return

        for widget, value in (
                (self.colorbar_min_text, vmin),
                (self.colorbar_max_text, vmax),
                ):
            widget.blockSignals(True)
            widget.setText(f"{value:.6g}")
            widget.blockSignals(False)

    def _colorbar_colormap_row(self, name):
        """
        Return the table row for a color map name.

        """
        table = getattr(self, "colorbar_colormap_table", None)
        if table is None:
            return -1

        for row in range(table.rowCount()):
            item = table.item(row, 0)
            if item is not None and item.data(QtCore.Qt.ItemDataRole.UserRole) == name:
                return row

        return -1

    def _select_colorbar_colormap(self, name):
        """
        Select the current color map row without applying it again.

        """
        table = getattr(self, "colorbar_colormap_table", None)
        if table is None:
            return

        row = self._colorbar_colormap_row(name)
        if row < 0:
            row = self._colorbar_colormap_row("viridis")
        if row < 0:
            return

        table.blockSignals(True)
        table.setCurrentCell(row, 0)
        table.selectRow(row)
        table.blockSignals(False)

    @QtCore.pyqtSlot()
    def _colorbar_colormap_selection_changed(self):
        """
        Apply the color map selected in the dialog.

        """
        table = self.colorbar_colormap_table
        row = table.currentRow()
        if row < 0:
            return

        item = table.item(row, 0)
        if item is None:
            return

        name = item.data(QtCore.Qt.ItemDataRole.UserRole)
        if name is None:
            return

        self.setColorbarColorMap(name)

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
        """
        Opens the color scale controls in a dialog.

        """
        controls = getattr(self, "colorbar_scale_controls", None)
        if controls is None:
            return

        self._populate_colorbar_colormap_table()
        self._sync_colorbar_scale_controls()

        dialog = getattr(self, "colorbar_scale_dialog", None)
        if dialog is None:
            dialog = qtw.QDialog(self)
            dialog.setWindowTitle("Color scale")
            dialog.resize(520, 560)
            layout = qtw.QVBoxLayout(dialog)
            layout.addWidget(controls)

            buttons = qtw.QDialogButtonBox(qtw.QDialogButtonBox.StandardButton.Close)
            buttons.rejected.connect(dialog.close)
            layout.addWidget(buttons)

            self.colorbar_scale_dialog = dialog

        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    @QtCore.pyqtSlot()
    def _apply_colorbar_manual_fields(self):
        """
        Apply color scale levels entered in the dialog.

        """
        try:
            vmin = float(self.colorbar_min_text.text())
            vmax = float(self.colorbar_max_text.text())
        except ValueError:
            self.show_status("Invalid color scale range.", 5000)
            self._sync_colorbar_scale_controls()
            return

        self.setColorbarManualRange(vmin, vmax)

    def setColorbarColorMap(self, name):
        """
        Set and persist the colorbar color map.

        """
        if name not in self._available_colorbar_colormaps():
            show_status = getattr(self, "show_status", None)
            if show_status is not None:
                show_status("Unknown color map.", 5000)
            return False

        self._colorbar_colormap_name = name
        bar = self.__dict__.get("bar")
        if bar is not None:
            bar.setColorMap(self._colorbar_colormap(name))

        config_obj = self.__dict__.get("config")
        if config_obj is not None:
            config_obj.update("user_preference.bar_colour", name)

        return True

    def setColorbarManualRange(self, vmin, vmax):
        """
        Set a persistent manual color scale range.

        """
        if not np.isfinite(vmin) or not np.isfinite(vmax) or vmin >= vmax:
            self.show_status("Color scale minimum must be below maximum.", 5000)
            self._sync_colorbar_scale_controls()
            return False

        self._colorbar_manual_levels = (float(vmin), float(vmax))

        if "relevel_refresh" in self.__dict__:
            self.relevel_refresh.setChecked(False)

        self._set_colorbar_levels(*self._colorbar_manual_levels)
        if "colorbar_manual_radio" in self.__dict__:
            self.colorbar_manual_radio.setChecked(True)
        return True

    @QtCore.pyqtSlot()
    def setColorbarAuto(self):
        """
        Return the color scale to automatic data-range scaling.

        """
        self._colorbar_manual_levels = None
        if "relevel_refresh" in self.__dict__:
            self.relevel_refresh.setChecked(True)
        self.scaleColorbar()

    @QtCore.pyqtSlot(bool)
    def _colorbar_auto_refresh_changed(self, enabled):
        """
        Keep colorbar manual state in sync with the refresh toolbar checkbox.

        """
        if enabled:
            self._colorbar_manual_levels = None
            self.scaleColorbar()
