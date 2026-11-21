"""Review a batch's exact CSV paths before any work can start."""

import os
from collections import Counter
from dataclasses import dataclass, replace

from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from ._export_paths import (
    ExportDestinationTransaction,
    UnsafeExportDestinationError,
    export_path_alias_key,
    inspect_export_destination,
)


@dataclass
class _FileRow:
    filename: qtw.QTableWidgetItem
    status: qtw.QTableWidgetItem
    action: qtw.QComboBox
    target: ExportDestinationTransaction | None = None
    message: str = ""


class CsvFilenameReview(qtw.QDialog):
    def __init__(self, owner, folder, proposed):
        super().__init__(owner)
        self.setWindowTitle("Review CSV filenames")
        self.folder = os.path.abspath(folder)
        self.rows: list[_FileRow] = []
        self.approved: list[tuple[int, ExportDestinationTransaction]] = []
        layout = qtw.QVBoxLayout(self)
        folder_label = qtw.QLabel(self.folder)
        folder_label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        folder_label.setWordWrap(True)
        layout.addWidget(folder_label)
        hint = qtw.QLabel("Double-click a filename to edit it. Choose Replace to overwrite an existing file.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.table = qtw.QTableWidget(len(proposed), 4)
        self.table.setHorizontalHeaderLabels(["Source", "Filename", "Status", "Action"])
        vertical = self.table.verticalHeader()
        assert vertical is not None
        vertical.hide()
        layout.addWidget(self.table)
        buttons = qtw.QDialogButtonBox(
            qtw.QDialogButtonBox.StandardButton.Ok | qtw.QDialogButtonBox.StandardButton.Cancel,
        )
        export_button = buttons.button(qtw.QDialogButtonBox.StandardButton.Ok)
        assert export_button is not None
        self.export_button = export_button
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        for row, (source, name) in enumerate(proposed):
            source_item = qtw.QTableWidgetItem(source)
            source_item.setToolTip(source)
            filename = qtw.QTableWidgetItem(name)
            status = qtw.QTableWidgetItem()
            for column, item in enumerate((source_item, filename, status)):
                if item is not filename:
                    item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(row, column, item)
            action = qtw.QComboBox()
            action.setMinimumContentsLength(8)
            action.setSizeAdjustPolicy(qtw.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            action.setAccessibleName(f"Export action for {source}")
            self.table.setCellWidget(row, 3, action)
            entry = _FileRow(filename, status, action)
            self.rows.append(entry)
            self._inspect(entry)
            action.currentIndexChanged.connect(self._update)
        self.table.itemChanged.connect(self._edited)
        header = self.table.horizontalHeader()
        assert header is not None
        for column in (0, 3):
            header.setSectionResizeMode(column, qtw.QHeaderView.ResizeMode.ResizeToContents)
        for column in (1, 2):
            header.setSectionResizeMode(column, qtw.QHeaderView.ResizeMode.Stretch)
        self._update()
        self.resize(840, min(500, 185 + 32 * len(proposed)))

    def _edited(self, item):
        if item.column() == 1:
            self._inspect(self.rows[item.row()])
            self._update()

    def _inspect(self, row):
        name = row.filename.text()
        with QtCore.QSignalBlocker(self.table):
            row.filename.setToolTip(name)
        target = None
        try:
            if (not name.casefold().endswith(".csv") or not name[:-4].strip()
                    or os.path.basename(name) != name or os.path.splitdrive(name)[0]
                    or "/" in name or "\\" in name or "\x00" in name):
                raise ValueError("Enter a filename ending in .csv in the chosen folder.")
            if os.name == "nt" and any(c in '<>:"|?*' or ord(c) < 32 for c in name):
                raise ValueError("This filename contains a character Windows cannot use.")
            target = inspect_export_destination(self.parentWidget(), os.path.join(self.folder, name))
            status = "File exists" if target.target_existed else "New file"
        except (ValueError, OSError, UnsafeExportDestinationError) as error:
            status = str(error)
        row.target = target
        row.message = status
        action = row.action
        with QtCore.QSignalBlocker(action):
            action.clear()
            if target is None:
                action.addItem("Review")
            elif target.target_existed:
                action.addItems(["Review", "Replace"])
            else:
                action.addItem("Export")
            action.addItem("Skip")

    def _update(self):
        counts = Counter(
            export_path_alias_key(row.target.filename) for row in self.rows
            if row.target is not None and row.action.currentText() != "Skip"
        )
        ready = True
        count = 0
        for row in self.rows:
            status = row.message
            action = row.action.currentText()
            if action != "Skip":
                if row.target and counts[export_path_alias_key(row.target.filename)] > 1:
                    status = f"Duplicate filename · {status}"
                    ready = False
                elif action == "Review":
                    ready = False
                else:
                    count += 1
            row.status.setText(status)
            row.status.setToolTip(status)
        self.export_button.setText(f"Export {count} {'file' if count == 1 else 'files'}")
        self.export_button.setEnabled(ready and count > 0)

    def accept(self):
        if not self.export_button.isEnabled():
            return
        approved = []
        changed = False
        for index, row in enumerate(self.rows):
            action = row.action.currentText()
            if action == "Skip":
                continue
            assert row.target is not None
            destination = replace(row.target, replacement_confirmed=action == "Replace")
            try:
                destination.validate()
            except (OSError, UnsafeExportDestinationError):
                self._inspect(row)
                row.message = f"Changed during review · {row.message}"
                if not changed:
                    self.table.scrollToItem(row.filename)
                changed = True
            else:
                approved.append((index, destination))
        if changed:
            self._update()
            return
        self.approved = approved
        super().accept()
