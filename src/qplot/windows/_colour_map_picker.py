"""Shared colour-map previews and chooser, independent of numerical scaling."""

from functools import lru_cache

from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

from ._colorbar import (
    _CET_COLORBAR_SUBTYPES,
    _COLORBAR_CATEGORY_DESCRIPTIONS,
    _COLORBAR_SOURCES,
    _COLORBAR_TABLE_SORT_ROLE,
    _MATPLOTLIB_COLORBAR_SUBTYPES,
    _colorbar_colormap_category,
    _colorbar_colormap_group,
    _colorbar_colormap_label,
    _colorbar_colormap_preview,
    _colorbar_colormap_type_label,
    _ColorbarColormapTableItem,
)


@lru_cache(maxsize=512)
def _gradient(name, width, height=14):
    return _colorbar_colormap_preview(name, width=width, height=height)


class _GradientDelegate(qtw.QStyledItemDelegate):
    """Fill the preview column without tinting the colours on selection."""

    def paint(self, painter, option, index):
        opt = qtw.QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        opt.icon = QtGui.QIcon()
        style = opt.widget.style() if opt.widget else qtw.QApplication.style()
        style.drawControl(qtw.QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        rect = opt.rect.adjusted(6, 0, -6, 0)
        rect.setHeight(min(14, opt.rect.height()))
        rect.moveCenter(opt.rect.center())
        name = index.data(QtCore.Qt.ItemDataRole.UserRole)
        if name and rect.width() > 0 and rect.height() > 0:
            painter.drawPixmap(rect.topLeft(), _gradient(name, rect.width(), rect.height()))


class ColourMapButton(qtw.QPushButton):
    """One native button containing the map name, gradient and chooser arrow."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._name = "viridis"
        self.setAutoDefault(False)
        self.setMinimumWidth(220)
        self.setMinimumHeight(max(34, self.fontMetrics().height() + 16))
        self.setSizePolicy(qtw.QSizePolicy.Policy.Expanding, qtw.QSizePolicy.Policy.Fixed)
        self.set_colormap(self._name)

    def set_colormap(self, name):
        self._name = name
        label = _colorbar_colormap_label(name)
        self.setText(label)
        self.setAccessibleName(f"Colour map: {label}. Choose colour map.")
        self.setToolTip(f"{label}: choose a colour map. Colours run from low to high values.")
        self.update()

    def paintEvent(self, event):
        option = qtw.QStyleOptionButton()
        self.initStyleOption(option)
        option.text = ""
        painter = QtGui.QPainter(self)
        style = self.style()
        style.drawControl(qtw.QStyle.ControlElement.CE_PushButton, option, painter, self)
        rect = style.subElementRect(qtw.QStyle.SubElement.SE_PushButtonContents, option, self)
        rect.adjust(10, 0, -10, 0)
        if self.isDown():
            rect.translate(style.pixelMetric(qtw.QStyle.PixelMetric.PM_ButtonShiftHorizontal),
                           style.pixelMetric(qtw.QStyle.PixelMetric.PM_ButtonShiftVertical))
        arrow = qtw.QStyleOption()
        arrow.initFrom(self)
        arrow.rect = QtCore.QRect(rect.right() - 10, rect.center().y() - 4, 10, 8)
        style.drawPrimitive(qtw.QStyle.PrimitiveElement.PE_IndicatorArrowDown, arrow, painter, self)
        rect.setRight(arrow.rect.left() - 10)
        name_width = min(self.fontMetrics().horizontalAdvance(self.text()), int(rect.width() * 0.4))
        name_rect = QtCore.QRect(rect.left(), rect.top(), name_width, rect.height())
        label = self.fontMetrics().elidedText(self.text(), QtCore.Qt.TextElideMode.ElideRight, name_width)
        style.drawItemText(painter, name_rect, QtCore.Qt.AlignmentFlag.AlignVCenter,
                           option.palette, self.isEnabled(), label, QtGui.QPalette.ColorRole.ButtonText)
        gradient_rect = QtCore.QRect(name_rect.right() + 10, rect.center().y() - 6,
                                    rect.right() - name_rect.right() - 9, 14)
        if gradient_rect.width() > 0:
            painter.drawPixmap(gradient_rect.topLeft(), _gradient(self._name, gradient_rect.width()))


class GradientPreview(qtw.QLabel):
    """Render the current gradient at its available width with a crisp border."""

    def __init__(self):
        super().__init__()
        self._name = None
        self._rendered = None
        self.setMinimumWidth(80)
        self.setFixedHeight(14)
        self.setSizePolicy(qtw.QSizePolicy.Policy.Ignored, qtw.QSizePolicy.Policy.Fixed)

    def set_colormap(self, name):
        self._name = name
        self._render_preview()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._render_preview()

    def _render_preview(self):
        key = (self._name, self.width())
        if self._name is not None and self.width() > 0 and key != self._rendered:
            self.setPixmap(_colorbar_colormap_preview(self._name, width=self.width(), height=14))
            self._rendered = key


class ColourMapPicker(qtw.QWidget):
    """Browse and select maps, with no ownership of plot scaling or settings."""

    mapSelected = QtCore.pyqtSignal(str)

    def __init__(self, names, current, parent=None):
        super().__init__(parent)
        self._names = tuple(names)
        self._name = current
        self.table = qtw.QTableWidget()
        self.table.setToolTip("Select a colour map to preview it on the plot.")
        self.current_name = qtw.QLabel()
        self.current_name.setObjectName("colorbarCurrentMapName")
        self.current_name.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.current_name.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
            | QtCore.Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.current_name.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.current_name.setAccessibleName("Current colour map name")
        self.current_preview = GradientPreview()
        self.current_preview.setObjectName("colorbarCurrentMapPreview")
        self.current_preview.setAccessibleName("Current colour map preview")
        self.no_matches = qtw.QLabel("No colour maps match these filters.")
        self.no_matches.setObjectName("colorbarNoMatches")
        self.no_matches.hide()
        filters = self._build_filters()
        self._build_table()
        self.show_current_button = qtw.QPushButton("Show in list")
        self.show_current_button.setObjectName("colorbarShowCurrentButton")
        self.show_current_button.setAutoDefault(False)
        self.show_current_button.setToolTip(
            "Reveal the current map in the list. Clear filters if they hide it."
        )
        self.show_current_button.clicked.connect(self.reveal_current)
        current = qtw.QHBoxLayout()
        current.setSpacing(10)
        current.addWidget(self.current_name)
        current.addWidget(self.current_preview, 1)
        current.addWidget(self.show_current_button)
        self.current_heading = self._heading("Colour map", "colorbarCurrentHeading")
        self.picker_heading = self._heading("Choose colour map", "colorbarPickerHeading")
        layout = self._content_layout = qtw.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.current_heading)
        layout.addLayout(current)
        layout.addSpacing(8)
        layout.addWidget(self.picker_heading)
        layout.addWidget(filters)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.no_matches)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.set_current_map(self._name)

    @staticmethod
    def _heading(text, object_name):
        heading = qtw.QLabel(text)
        heading.setObjectName(object_name)
        font = heading.font()
        font.setBold(True)
        heading.setFont(font)
        return heading

    def insert_scaling_controls(self, controls):
        """Let the full Colour Scale dialog place its controls above the list."""
        heading = self._heading("Set scaling", "colorbarScalingHeading")
        self._content_layout.insertWidget(3, heading)
        self._content_layout.insertWidget(4, controls)
        self._content_layout.insertSpacing(5, 8)
        return heading

    def set_current_map(self, name):
        self._name = name
        label = _colorbar_colormap_label(name)
        if self.current_name.text() != label:
            self.current_name.setText(label)
        self.current_name.setToolTip(_colorbar_colormap_type_label(name) + "\nSelect the name to copy it.")
        self.current_preview.set_colormap(name)
        self.current_preview.setToolTip(label)
        self.select_map(name)

    def set_maps(self, names, current):
        self._names = tuple(names)
        self._name = current
        self.populate()
        self.set_current_map(current)

    def _build_filters(self):
        """Build a name search and independent source/category selectors."""
        controls = qtw.QWidget()
        layout = qtw.QHBoxLayout(controls)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.search = qtw.QLineEdit()
        self.search.setObjectName("colorbarMapSearch")
        self.search.setPlaceholderText("Search names…")
        self.search.setAccessibleName("Search colour maps")
        self.search.setToolTip("Filter colour maps by name.")
        self.search.setClearButtonEnabled(True)
        layout.addWidget(self.search, 1)
        self.source_combo = qtw.QComboBox()
        self.source_combo.setObjectName("colorbarSourceFilter")
        self.source_combo.setAccessibleName("Colour map source")
        self.source_combo.addItem("All", "all")
        for source, label in _COLORBAR_SOURCES:
            self.source_combo.addItem(label, source)
        self.category_combo = qtw.QComboBox()
        self.category_combo.setObjectName("colorbarCategoryFilter")
        self.category_combo.setAccessibleName("Colour map category")
        self.category_combo.addItem("All", "all")
        self.category_combo.setItemData(0, _COLORBAR_CATEGORY_DESCRIPTIONS["all"],
                                                 QtCore.Qt.ItemDataRole.ToolTipRole)
        for title, combo in (("Source:", self.source_combo),
                             ("Category:", self.category_combo)):
            label = qtw.QLabel(title)
            label.setBuddy(combo)
            layout.addWidget(label)
            layout.addWidget(combo)
            combo.setToolTip(f"Filter colour maps by {title[:-1].lower()}.")
            combo.currentIndexChanged.connect(self._apply_filters)
        self.search.textChanged.connect(self._apply_filters)
        return controls

    def _sync_categories(self):
        """Offer the categories present in the chosen source, retaining a valid choice."""
        source = self.source_combo.currentData()
        table = self.table
        categories = set()
        for row in range(table.rowCount()):
            name = table.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole)
            if source == "all" or source == _colorbar_colormap_group(name):
                categories.add(_colorbar_colormap_category(name))
        labels = dict(_CET_COLORBAR_SUBTYPES + _MATPLOTLIB_COLORBAR_SUBTYPES)
        choices = [("all", "All")] + [
            (category, labels[category]) for category in _COLORBAR_CATEGORY_DESCRIPTIONS
            if category in categories
        ]
        combo = self.category_combo
        if [combo.itemData(index) for index in range(combo.count())] != [key for key, _ in choices]:
            previous = combo.currentData()
            with QtCore.QSignalBlocker(combo):
                combo.clear()
                for category, label in choices:
                    combo.addItem(label, category)
                    combo.setItemData(combo.count() - 1, _COLORBAR_CATEGORY_DESCRIPTIONS[category],
                                      QtCore.Qt.ItemDataRole.ToolTipRole)
                combo.setCurrentIndex(max(0, combo.findData(previous)))
        combo.setToolTip("Filter colour maps by category.\n"
                         + _COLORBAR_CATEGORY_DESCRIPTIONS[combo.currentData()])

    def _apply_filters(self):
        """Filter the list without applying a map or changing the colour range."""
        table = self.table
        self._sync_categories()
        query = self.search.text().strip().casefold()
        source = self.source_combo.currentData()
        category = self.category_combo.currentData()
        count = 0
        with QtCore.QSignalBlocker(table):
            for row in range(table.rowCount()):
                item = table.item(row, 0)
                name = item.data(QtCore.Qt.ItemDataRole.UserRole)
                visible = (
                    (not query or query in item.text().casefold() or query in name.casefold())
                    and (source == "all" or source == _colorbar_colormap_group(name))
                    and (category == "all" or category == _colorbar_colormap_category(name))
                )
                table.setRowHidden(row, not visible)
                count += visible
        self.no_matches.setVisible(count == 0)
        self.select_map(self._name)

    def _build_table(self):
        """Build the scrollable colour-map chooser with rendered previews."""
        table = self.table
        table.setStyleSheet("QTableWidget::item { padding: 1px 3px; }")
        table.setColumnCount(3)
        table.setHorizontalHeaderLabels(("Name", "Preview", "Type"))
        for column, title in ((0, "name"), (2, "type")):
            table.horizontalHeaderItem(column).setToolTip(
                f"Click to sort by {title}; click again to reverse the order."
            )
        vertical_header = table.verticalHeader()
        if vertical_header is not None:
            vertical_header.hide()
        table.setShowGrid(False)
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(qtw.QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(qtw.QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(qtw.QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSortingEnabled(True)
        table.setItemDelegateForColumn(1, _GradientDelegate(table))

        header = table.horizontalHeader()
        if header is None:
            return
        # Override the application-wide header padding only for this compact
        # picker, keeping the native colours, bold titles and sorting indicator.
        header.setStyleSheet("QHeaderView::section { padding: 1px 3px; }")
        font = header.font()
        font.setBold(True)
        header.setFont(font)
        header.setFixedHeight(max(20, header.fontMetrics().height() + 4))
        table.setMinimumSize(420, header.height() + 2 * table.frameWidth()
                             + 6 * max(20, table.fontMetrics().height() + 6))
        header.setSectionResizeMode(0, qtw.QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, qtw.QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, qtw.QHeaderView.ResizeMode.ResizeToContents)

        self.populate()

    def populate(self):
        """Fill the colour-map table using the current filter settings."""
        table = self.table
        table.blockSignals(True)
        sorting_enabled = table.isSortingEnabled()
        table.setSortingEnabled(False)
        table.setRowCount(0)

        colormaps = self._names
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

            table.setItem(row, 0, name_item)
            table.setItem(row, 1, preview_item)
            table.setItem(row, 2, type_item)
            table.setRowHeight(row, max(20, table.fontMetrics().height() + 6))

        table.resizeColumnToContents(0)
        table.resizeColumnToContents(2)
        # Reserve a useful minimum gradient width beside the longest name/type.
        table.setMinimumWidth(
            table.columnWidth(0) + table.columnWidth(2) + 120 + 12
            + table.verticalScrollBar().sizeHint().width() + 2 * table.frameWidth()
        )
        table.setSortingEnabled(sorting_enabled)
        table.blockSignals(False)
        self._apply_filters()

    def row_for_name(self, name):
        """Return the table row for a colour-map name."""
        table = getattr(self, "table", None)
        if table is None:
            return -1

        for row in range(table.rowCount()):
            item = table.item(row, 0)
            if item is not None and item.data(QtCore.Qt.ItemDataRole.UserRole) == name:
                return row

        return -1

    def select_map(self, name):
        """Select the current colour-map row without applying it again."""
        table = getattr(self, "table", None)
        if table is None:
            return

        row = self.row_for_name(name)
        with QtCore.QSignalBlocker(table):
            if row < 0 or table.isRowHidden(row):
                table.clearSelection()
                table.setCurrentItem(None)
                return
            table.setCurrentCell(row, 0)
            table.selectRow(row)

    def _selection_changed(self):
        """Preview and emit the selected colour map."""
        table = self.table
        row = table.currentRow()
        if row < 0:
            return

        item = table.item(row, 0)
        if item is None:
            return

        name = item.data(QtCore.Qt.ItemDataRole.UserRole)
        if name is None:
            return

        self.set_current_map(name)
        self.mapSelected.emit(name)

    def reveal_current(self):
        """Reveal the active map without changing it, its range or the sort order."""
        table = self.table
        name = self._name
        row = self.row_for_name(name)
        if row < 0:
            return
        if table.isRowHidden(row):
            for widget in (self.search, self.source_combo,
                           self.category_combo):
                with QtCore.QSignalBlocker(widget):
                    if isinstance(widget, qtw.QLineEdit):
                        widget.clear()
                    else:
                        widget.setCurrentIndex(0)
            self._apply_filters()
        table.doItemsLayout()
        table.resizeColumnToContents(0)
        table.resizeColumnToContents(2)
        self.select_map(name)
        table.scrollToItem(table.item(row, 0), qtw.QAbstractItemView.ScrollHint.PositionAtCenter)


class ColourMapDialog(qtw.QDialog):
    """Preview a map on the owner; cancelling restores only the original map."""

    def __init__(self, owner, parent):
        super().__init__(parent)
        self.owner = owner
        self._opening_map = owner._current_colorbar_colormap_name()
        self._preview_active = True
        self.setWindowTitle("Choose colour map")
        self.setWindowModality(QtCore.Qt.WindowModality.WindowModal)
        self.resize(600, 390)
        layout = qtw.QVBoxLayout(self)
        self.picker = ColourMapPicker(owner._available_colorbar_colormaps(), self._opening_map, self)
        layout.addWidget(self.picker, 1)
        self.buttons = qtw.QDialogButtonBox(
            qtw.QDialogButtonBox.StandardButton.Ok | qtw.QDialogButtonBox.StandardButton.Cancel,
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.picker.mapSelected.connect(self._preview)
        self.finished.connect(self._finish_preview)

    def _preview(self, name):
        if self._preview_active:
            if not self.owner.setColorbarColorMap(name):
                self.picker.set_current_map(self.owner._current_colorbar_colormap_name())

    def _finish_preview(self, result):
        if not self._preview_active:
            return
        self._preview_active = False
        if result == qtw.QDialog.DialogCode.Rejected:
            self.owner.setColorbarColorMap(self._opening_map)

    def showEvent(self, event):
        super().showEvent(event)
        self.picker.reveal_current()
