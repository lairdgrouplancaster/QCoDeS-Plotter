from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

from .._shortcuts import standard_key_sequences

COPY_SELECTION_SHORTCUTS = standard_key_sequences(
    QtGui.QKeySequence.StandardKey.Copy,
    ["Ctrl+C"],
    )
COPY_CELL_SHORTCUTS = [QtGui.QKeySequence("Ctrl+Shift+C")]


def copy_action(label, shortcuts, slot, parent):
    action = QtGui.QAction(label, parent)
    action.setShortcuts(shortcuts)
    action.setShortcutContext(QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
    if hasattr(action, "setShortcutVisibleInContextMenu"):
        action.setShortcutVisibleInContextMenu(True)
    action.triggered.connect(slot)
    return action


class WrappedValueDelegate(qtw.QStyledItemDelegate):
    WRAP_FLAGS = (
        QtCore.Qt.AlignmentFlag.AlignLeft
        | QtCore.Qt.AlignmentFlag.AlignTop
        | QtCore.Qt.TextFlag.TextWrapAnywhere
        )

    def paint(self, painter, option, index):
        opt = qtw.QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)

        widget = opt.widget
        style = widget.style() if widget is not None else qtw.QApplication.style()
        text = opt.text
        opt.text = ""

        style.drawControl(qtw.QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)

        text_rect = style.subElementRect(qtw.QStyle.SubElement.SE_ItemViewItemText, opt, widget)
        text_rect.adjust(0, 2, 0, -2)
        painter.save()
        painter.setFont(opt.font)
        role = (
            QtGui.QPalette.ColorRole.HighlightedText
            if opt.state & qtw.QStyle.StateFlag.State_Selected
            else QtGui.QPalette.ColorRole.Text
            )
        painter.setPen(opt.palette.color(role))
        painter.drawText(text_rect, self.WRAP_FLAGS, text)
        painter.restore()


    def sizeHint(self, option, index):
        opt = qtw.QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)

        width = opt.rect.width()
        if width <= 0 and opt.widget is not None:
            width = opt.widget.columnWidth(index.column())
        width = max(24, width - 6)

        metrics = QtGui.QFontMetrics(opt.font)
        text_rect = metrics.boundingRect(
            QtCore.QRect(0, 0, width, 100000),
            self.WRAP_FLAGS,
            opt.text
            )
        base = super().sizeHint(option, index)
        return QtCore.QSize(base.width(), max(base.height(), text_rect.height() + 6))


class infoTree(qtw.QTreeWidget):
    def __init__(self, expand_all=True, truncate_values=False):
        super().__init__()
        self.expand_all = expand_all
        self.truncate_values = truncate_values
        self.setHeaderLabels(["Key", "Value"])
        self.setColumnCount(2)
        self.setWordWrap(True)
        self.setTextElideMode(QtCore.Qt.TextElideMode.ElideNone)
        self.setUniformRowHeights(False)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setItemDelegateForColumn(1, WrappedValueDelegate(self))
        self.header().setSectionResizeMode(0, qtw.QHeaderView.ResizeMode.ResizeToContents)
        self.header().setSectionResizeMode(1, qtw.QHeaderView.ResizeMode.Stretch)
        self.header().setStretchLastSection(True)
        self.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.openCopyMenu)

        self.copy_value_action = copy_action(
            "Copy Value",
            COPY_CELL_SHORTCUTS,
            self.copyValue,
            self
            )
        self.copy_selection_action = copy_action(
            "Copy Selection",
            COPY_SELECTION_SHORTCUTS,
            self.copySelection,
            self
            )
        self.addActions([self.copy_value_action, self.copy_selection_action])


    def setInfo(self, info):
        self.clear()

        if not info:
            self.addTopLevelItem(qtw.QTreeWidgetItem(["No data", ""]))
            return
        if not isinstance(info, dict):
            item = qtw.QTreeWidgetItem(
                ["Value", format_value(info, 180 if self.truncate_values else None)]
                )
            item.setToolTip(1, format_value(info))
            self.addTopLevelItem(item)
            return

        items = dictToTree(info, truncate_values=self.truncate_values)
        for item in items:
            self.addTopLevelItem(item)
            item.setExpanded(True)

        if self.expand_all:
            self.expandAll()
        self.header().setSectionResizeMode(0, qtw.QHeaderView.ResizeMode.ResizeToContents)
        self.header().setSectionResizeMode(1, qtw.QHeaderView.ResizeMode.Stretch)
        self.doItemsLayout()


    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.doItemsLayout()


    def openCopyMenu(self, pos):
        item = self.itemAt(pos)
        if item is None:
            return

        self.setCurrentItem(item)

        menu = qtw.QMenu(self)
        menu.addAction(self.copy_value_action)

        copy_row = QtGui.QAction("Copy Row", menu)
        copy_row.triggered.connect(lambda: copy_to_clipboard(row_text(item)))
        menu.addAction(copy_row)

        if self.selectedItems():
            menu.addAction(self.copy_selection_action)

        menu.exec(self.viewport().mapToGlobal(pos))


    def copyValue(self):
        item = self.currentItem()
        if item is not None:
            copy_to_clipboard(item.text(1))


    def copySelection(self):
        items = self.selectedItems()
        if not items and self.currentItem() is not None:
            items = [self.currentItem()]
        copy_to_clipboard("\n".join(row_text(item) for item in items))


class CopyableTableWidget(qtw.QTableWidget):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.openCopyMenu)

        self.copy_cell_action = copy_action(
            "Copy Cell",
            COPY_CELL_SHORTCUTS,
            self.copyCell,
            self
            )
        self.copy_selection_action = copy_action(
            "Copy Selection",
            COPY_SELECTION_SHORTCUTS,
            self.copySelection,
            self
            )
        self.addActions([self.copy_cell_action, self.copy_selection_action])


    def openCopyMenu(self, pos):
        item = self.itemAt(pos)
        if item is None:
            return

        self.setCurrentItem(item)

        menu = qtw.QMenu(self)
        menu.addAction(self.copy_cell_action)
        menu.addAction(self.copy_selection_action)

        menu.exec(self.viewport().mapToGlobal(pos))


    def copyCell(self):
        item = self.currentItem()
        if item is not None:
            copy_to_clipboard(item.text())


    def copySelection(self):
        ranges = self.selectedRanges()
        if not ranges and self.currentItem() is not None:
            self.copyCell()
            return

        sections = []
        for selected_range in ranges:
            rows = []
            for row in range(selected_range.topRow(), selected_range.bottomRow() + 1):
                values = []
                for col in range(
                        selected_range.leftColumn(),
                        selected_range.rightColumn() + 1,
                        ):
                    item = self.item(row, col)
                    values.append(item.text() if item is not None else "")
                rows.append("\t".join(values))
            sections.append("\n".join(rows))

        copy_to_clipboard("\n".join(section for section in sections if section))


def dictToTree(d: dict, truncate_values=False):
    items = []
    for k, v in d.items():
        if not isinstance(v, dict):
            item = qtw.QTreeWidgetItem(
                [str(k), format_value(v, 180 if truncate_values else None)]
                )
            item.setToolTip(1, format_value(v))
        else:
            item = qtw.QTreeWidgetItem([k, ""])
            for child in dictToTree(v, truncate_values=truncate_values):
                item.addChild(child)
        items.append(item)
    return items


def snapshot_parameters(snapshot):
    if not isinstance(snapshot, dict):
        return {}

    out = {}
    station = snapshot.get("station", snapshot)
    parameter_dicts = []

    if isinstance(station, dict):
        params = station.get("parameters")
        if isinstance(params, dict):
            parameter_dicts.append(params)

        instruments = station.get("instruments")
        if isinstance(instruments, dict):
            for instrument in instruments.values():
                if isinstance(instrument, dict) and isinstance(instrument.get("parameters"), dict):
                    parameter_dicts.append(instrument["parameters"])

    for params in parameter_dicts:
        for key, details in params.items():
            if not isinstance(details, dict):
                continue
            for name in (key, details.get("name"), details.get("full_name")):
                if name:
                    out[str(name)] = details

    return out


def format_value(value, max_len=None):
    if value is None:
        text = ""
    elif isinstance(value, float):
        text = f"{value:.6g}"
    elif isinstance(value, (list, tuple)):
        text = ", ".join(format_value(item) for item in value)
    else:
        text = str(value)

    text = text.replace("\n", " ")
    if max_len is not None and len(text) > max_len:
        return text[:max_len - 3] + "..."
    return text


def row_text(item):
    return "\t".join(item.text(col) for col in range(item.columnCount()))


def copy_to_clipboard(text):
    clipboard = qtw.QApplication.clipboard()
    if clipboard is not None:
        clipboard.setText(text)
