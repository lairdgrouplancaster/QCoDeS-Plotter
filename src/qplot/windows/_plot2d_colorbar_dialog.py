from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

import numpy as np
from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

from ._colorbar import (
    _CET_COLORBAR_SUBTYPES,
    _COLORBAR_FILTERS,
    _COLORBAR_TABLE_SORT_ROLE,
    _MATPLOTLIB_COLORBAR_SUBTYPES,
    _colorbar_colormap_label,
    _colorbar_colormap_preview,
    _colorbar_colormap_type_label,
    _colorbar_subtype_config_key,
    _ColorbarColormapTableItem,
    _config_value,
)
from ._config_persistence import persist_config_value
from ._plot_host import PlotHost
from ._range_entry import RangeEntry

if TYPE_CHECKING:
    class _ColorbarScaleDialogBase(PlotHost):
        _colorbar_colormap_name: str
        _colorbar_manual_levels: tuple[float, float] | None
        bar: Any
        colorbar_auto_radio: qtw.QRadioButton
        colorbar_button_group: qtw.QButtonGroup
        colorbar_cet_subtype_checks: dict[str, qtw.QCheckBox]
        colorbar_colormap_table: qtw.QTableWidget
        colorbar_include_cet_check: qtw.QCheckBox
        colorbar_include_custom_check: qtw.QCheckBox
        colorbar_include_local_check: qtw.QCheckBox
        colorbar_include_matplotlib_check: qtw.QCheckBox
        colorbar_manual_radio: qtw.QRadioButton
        colorbar_matplotlib_subtype_checks: dict[str, qtw.QCheckBox]
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


class _ColorbarAutoLimitsButton(qtw.QToolButton):
    """Refresh its prospective-limit tooltip immediately before display."""

    def __init__(self, refresh_tooltip: Callable[[], None], parent: qtw.QWidget):
        super().__init__(parent)
        self._refresh_tooltip = refresh_tooltip

    def event(self, event: QtCore.QEvent | None) -> bool:
        if event is not None and event.type() == QtCore.QEvent.Type.ToolTip:
            self._refresh_tooltip()
        return super().event(event)


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
        if style is None:
            super().paint(painter, option, index)
            return
        style.drawControl(qtw.QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)

        icon_size = opt.decorationSize
        if not icon_size.isValid() or icon_size.isEmpty():
            icon_size = icon.actualSize(opt.rect.size())
        icon_size = icon_size.boundedTo(opt.rect.size())
        icon_rect = QtCore.QRect(QtCore.QPoint(0, 0), icon_size)
        icon_rect.moveCenter(opt.rect.center())

        mode = QtGui.QIcon.Mode.Normal
        if not (opt.state & qtw.QStyle.StateFlag.State_Enabled):
            mode = QtGui.QIcon.Mode.Disabled
        elif opt.state & qtw.QStyle.StateFlag.State_Selected:
            mode = QtGui.QIcon.Mode.Selected

        icon.paint(painter, icon_rect, QtCore.Qt.AlignmentFlag.AlignCenter, mode)


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
        self.symmetric_color = qtw.QCheckBox("Symmetric")
        self.symmetric_color.setChecked(self.__dict__.get("_colorbar_symmetric", False))
        self.symmetric_color.setToolTip(
            "Keep colour limits equally spaced above and below zero. "
            "Automatic limits use the larger absolute value; editing either limit mirrors the other."
        )
        self.symmetric_color.toggled.connect(self._colorbar_symmetric_changed)
        self.colorbar_min_label = qtw.QLabel("Minimum")
        self.colorbar_max_label = qtw.QLabel("Maximum")
        self.colorbar_min_text = qtw.QLineEdit()
        self.colorbar_max_text = qtw.QLineEdit()
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
        style = qtw.QApplication.style()
        if style is not None:
            self.colorbar_copy_auto_limits_button.setIcon(
                style.standardIcon(qtw.QStyle.StandardPixmap.SP_BrowserReload)
            )
        self.colorbar_colormap_table = qtw.QTableWidget()
        self.colorbar_colormap_table.setToolTip("Select a colour map to apply it to this heatmap.")
        for spec in _COLORBAR_FILTERS.values():
            setattr(self, spec.widget_name, qtw.QCheckBox(spec.label))
            getattr(self, spec.widget_name).setToolTip(f"Include {spec.label} colour maps in the list.")
        self.colorbar_cet_subtype_checks = {}
        self.colorbar_matplotlib_subtype_checks = {}
        for subtype, label in _CET_COLORBAR_SUBTYPES:
            self.colorbar_cet_subtype_checks[subtype] = qtw.QCheckBox(label)
            self.colorbar_cet_subtype_checks[subtype].setToolTip(f"Include {label.lower()} CET colour maps in the list.")
        for subtype, label in _MATPLOTLIB_COLORBAR_SUBTYPES:
            self.colorbar_matplotlib_subtype_checks[subtype] = qtw.QCheckBox(label)
            self.colorbar_matplotlib_subtype_checks[subtype].setToolTip(f"Include {label.lower()} Matplotlib colour maps in the list.")
        self._init_colorbar_colormap_table()

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
            )
        self.colorbar_range_entry = RangeEntry(
            self.colorbar_min_text, self.colorbar_max_text,
            self.colorbar_min_label, self.colorbar_max_label,
            accessible_prefix="Colour scale", parent=controls,
        )
        self.colorbar_range_entry.setFixedWidth(200)
        self.colorbar_range_entry.group.idClicked.connect(self._sync_colorbar_scale_controls)

        self.colorbar_button_group = qtw.QButtonGroup(cast(QtCore.QObject, self))
        self.colorbar_button_group.addButton(self.colorbar_manual_radio)
        self.colorbar_button_group.addButton(self.colorbar_auto_radio)
        self.colorbar_button_group.setExclusive(True)

        range_controls = qtw.QWidget()
        range_layout = qtw.QGridLayout(range_controls)
        range_layout.setContentsMargins(0, 0, 0, 0)
        range_layout.setHorizontalSpacing(6)
        range_layout.setVerticalSpacing(5)
        align_vcenter = QtCore.Qt.AlignmentFlag.AlignVCenter
        range_layout.addWidget(self.colorbar_range_entry, 1, 0, align_vcenter)
        range_layout.setColumnMinimumWidth(1, 6)
        range_layout.addWidget(self.colorbar_manual_radio, 1, 2, align_vcenter)
        range_layout.addWidget(self.colorbar_min_label, 0, 3, align_vcenter)
        range_layout.addWidget(self.colorbar_min_text, 1, 3, align_vcenter)
        range_layout.addWidget(self.colorbar_max_label, 0, 4, align_vcenter)
        range_layout.addWidget(self.colorbar_max_text, 1, 4, align_vcenter)
        range_layout.addWidget(
            self.colorbar_copy_auto_limits_button,
            1,
            5,
            align_vcenter,
        )
        range_layout.addWidget(self.colorbar_range_entry.summary, 2, 3, 1, 2)
        range_layout.addWidget(self.colorbar_auto_radio, 3, 2)
        range_layout.addWidget(self.relevel_refresh, 3, 3, 1, 3)
        self.relevel_refresh.show()
        range_layout.setColumnStretch(3, 1)
        range_layout.setColumnStretch(4, 1)

        filter_controls = self._init_colorbar_filter_controls()

        layout.addWidget(qtw.QLabel("Colours"))
        layout.addWidget(filter_controls)
        layout.addWidget(self.colorbar_colormap_table, 1)
        layout.addWidget(range_controls)
        options = qtw.QHBoxLayout()
        self._init_colorbar_scope_controls(options)
        options.addWidget(self.symmetric_color)
        options.addStretch()
        layout.addLayout(options)

        self.colorbar_scale_controls = controls

        menu = getattr(self, "vbMenu", None)
        if menu is not None:
            self.colorbar_scale_action = QtGui.QAction("&Colour Scale...", cast(QtCore.QObject, self))
            self.colorbar_scale_action.triggered.connect(self.open_colorbar_scale_dialog)
            before = getattr(self, "autoscaleSep", None)
            menu.insertAction(before, self.colorbar_scale_action)

        self.colorbar_colormap_table.itemSelectionChanged.connect(
            self._colorbar_colormap_selection_changed
            )
        for spec in _COLORBAR_FILTERS.values():
            getattr(self, spec.widget_name).toggled.connect(getattr(self, spec.changed_slot))
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
        for column, spec in enumerate(_COLORBAR_FILTERS.values(), 1):
            layout.addWidget(getattr(self, spec.widget_name), 0, column)

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
        table.setHorizontalHeaderLabels(("Colour map", "Preview", "Type"))
        vertical_header = table.verticalHeader()
        if vertical_header is not None:
            vertical_header.hide()
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
        if header is None:
            return
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
            label = _colorbar_colormap_label(name)
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
        for spec in _COLORBAR_FILTERS.values():
            widget = getattr(self, spec.widget_name)
            widget.blockSignals(True)
            widget.setChecked(_config_value(config_obj, spec.config_key, True))
            widget.blockSignals(False)

        include_cet = self.colorbar_include_cet_check.isChecked()
        include_matplotlib = self.colorbar_include_matplotlib_check.isChecked()
        for subtype, widget in self.colorbar_cet_subtype_checks.items():
            key = _colorbar_subtype_config_key("cet", subtype)
            widget.blockSignals(True)
            widget.setChecked(_config_value(config_obj, key, True))
            widget.setEnabled(include_cet)
            widget.blockSignals(False)

        for subtype, widget in self.colorbar_matplotlib_subtype_checks.items():
            key = _colorbar_subtype_config_key("matplotlib", subtype)
            widget.blockSignals(True)
            widget.setChecked(_config_value(config_obj, key, True))
            widget.setEnabled(include_matplotlib)
            widget.blockSignals(False)

    def _set_colorbar_filter_setting(self, key, enabled):
        """
        Persist a broad color-map filter and rebuild the chooser.

        """
        config_obj = self.__dict__.get("config")
        if config_obj is not None:
            if not persist_config_value(
                    self,
                    config_obj,
                    key,
                    bool(enabled),
                    "the colour-map filter",
                    self._sync_colorbar_filter_controls,
                    ):
                return False

        self._populate_colorbar_colormap_table()
        self._sync_colorbar_scale_controls()
        return True

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
            _COLORBAR_FILTERS["cet"].config_key,
            enabled,
        )

    @QtCore.pyqtSlot(bool)
    def _colorbar_include_matplotlib_changed(self, enabled):
        """
        Include or exclude matplotlib color maps in the chooser.

        """
        self._set_colorbar_filter_setting(
            _COLORBAR_FILTERS["matplotlib"].config_key,
            enabled,
        )

    @QtCore.pyqtSlot(bool)
    def _colorbar_include_local_changed(self, enabled):
        """
        Include or exclude pyqtgraph local color maps in the chooser.

        """
        self._set_colorbar_filter_setting(
            _COLORBAR_FILTERS["pyqtgraph"].config_key,
            enabled,
        )

    @QtCore.pyqtSlot(bool)
    def _colorbar_include_custom_changed(self, enabled):
        """
        Include or exclude app-provided custom color maps in the chooser.

        """
        self._set_colorbar_filter_setting(
            _COLORBAR_FILTERS["custom"].config_key,
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
            dialog = qtw.QDialog(cast(qtw.QWidget, self))
            dialog.setWindowTitle("Colour scale")
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
        entry = self.colorbar_range_entry
        # Merely leaving a field or changing format must preserve Auto. An
        # explicit click on Manual still freezes the current exact endpoints.
        if entry.unchanged() and not self.colorbar_manual_radio.isChecked():
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
                show_status("Unknown colour map.", 5000)
            return False

        config_obj = self.__dict__.get("config")
        if config_obj is not None:
            previous_name = config_obj.get("user_preference.bar_colour")

            def rollback():
                self._select_colorbar_colormap(previous_name)

            if not persist_config_value(
                    self,
                    config_obj,
                    "user_preference.bar_colour",
                    name,
                    "the colour map",
                    rollback,
                    ):
                return False

        self._colorbar_colormap_name = name
        bar = self.__dict__.get("bar")
        if bar is not None:
            bar.setColorMap(self._colorbar_colormap(name))

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
