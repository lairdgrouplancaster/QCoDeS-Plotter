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
from ._small_dialogs import resize_dialog


@dataclass
class _FileRow:
    filename: qtw.QTableWidgetItem
    status: qtw.QTableWidgetItem
    action: qtw.QComboBox
    target: ExportDestinationTransaction | None = None
    message: str = ""
    needs_action: bool = False


class CsvFilenameReview(qtw.QDialog):
    def __init__(self, owner, folder, proposed):
        super().__init__(owner)
        self.setWindowTitle("Review CSV filenames")
        self.folder = os.path.abspath(folder)
        self.rows: list[_FileRow] = []
        self.issue_rows: list[int] = []
        self.approved: list[tuple[int, ExportDestinationTransaction]] = []
        layout = qtw.QVBoxLayout(self)
        destination = qtw.QHBoxLayout()
        self.folder_path = qtw.QLineEdit(self.folder)
        self.folder_path.setReadOnly(True)
        self.folder_path.setAccessibleName("Destination folder")
        self.folder_path.setToolTip(self.folder)
        destination.addWidget(qtw.QLabel("Destination"))
        destination.addWidget(self.folder_path, 1)
        change_folder = qtw.QPushButton("Change folder…")
        change_folder.setAutoDefault(False)
        change_folder.clicked.connect(self._change_folder)
        destination.addWidget(change_folder)
        layout.addLayout(destination)
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
        footer = qtw.QHBoxLayout()
        self.summary = qtw.QLabel()
        footer.addWidget(self.summary)
        self.next_issue = qtw.QPushButton("Next issue")
        self.next_issue.clicked.connect(self._next_issue)
        self.unique_names = qtw.QPushButton("Use unique names")
        self.unique_names.setToolTip("Add numbered suffixes to resolve filename conflicts.")
        self.unique_names.clicked.connect(self._use_unique_names)
        for button in (self.next_issue, self.unique_names):
            button.setAutoDefault(False)
            footer.addWidget(button)
        footer.addStretch()
        footer.addWidget(buttons)
        layout.addLayout(footer)
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
        metrics = self.table.fontMetrics()
        header.resizeSection(0, min(self.table.sizeHintForColumn(0) + 12, metrics.horizontalAdvance("M" * 17)))
        header.setSectionResizeMode(1, qtw.QHeaderView.ResizeMode.Stretch)
        header.resizeSection(2, metrics.horizontalAdvance("Duplicate filename") + 20)
        header.setSectionResizeMode(3, qtw.QHeaderView.ResizeMode.ResizeToContents)
        self._update()
        resize_dialog(self, 840, min(500, 195 + 32 * len(proposed)))

    def showEvent(self, event):
        super().showEvent(event)
        screen = self.screen()
        if screen is not None:
            bounds = screen.availableGeometry().adjusted(8, 8, -8, -8)
            frame = self.frameGeometry()
            if not bounds.contains(frame):
                self.move(max(bounds.left(), min(frame.left(), bounds.right() - frame.width() + 1)),
                          max(bounds.top(), min(frame.top(), bounds.bottom() - frame.height() + 1)))

    def _change_folder(self):
        folder = qtw.QFileDialog.getExistingDirectory(self, "Choose CSV destination", self.folder)
        if not folder or os.path.abspath(folder) == self.folder:
            return
        self.folder = os.path.abspath(folder)
        self.folder_path.setText(self.folder)
        self.folder_path.setToolTip(self.folder)
        for row in self.rows:
            skipped = row.action.currentText() == "Skip"
            self._inspect(row)
            if skipped:
                with QtCore.QSignalBlocker(row.action):
                    row.action.setCurrentText("Skip")
        self._update()

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
        self.issue_rows.clear()
        can_rename = False
        skipped = 0
        count = 0
        for index, row in enumerate(self.rows):
            status = row.message
            row.needs_action = False
            action = row.action.currentText()
            if action == "Skip":
                skipped += 1
            elif row.target and counts[export_path_alias_key(row.target.filename)] > 1:
                status = f"Duplicate filename · {status}"
                self.issue_rows.append(index)
                can_rename = True
            elif action == "Review":
                row.needs_action = row.target is not None and row.target.target_existed
                self.issue_rows.append(index)
                can_rename |= row.target is not None
            else:
                count += 1
            row.status.setText(status)
            row.status.setToolTip(status)
        issues = len(self.issue_rows)
        summary = f"{count} ready · {issues} {'needs' if issues == 1 else 'need'} attention"
        if skipped:
            summary += f" · {skipped} skipped"
        self.summary.setText(summary)
        self.next_issue.setEnabled(bool(issues))
        self.unique_names.setEnabled(can_rename)
        self.export_button.setText(f"Export {count} {'file' if count == 1 else 'files'}")
        self.export_button.setEnabled(not issues and count > 0)

    def _next_issue(self):
        if self.issue_rows:
            start = self.table.currentRow()
            if self.table.currentColumn() in (1, 3):
                start += 1
            index = next((i for i in self.issue_rows if i >= start), self.issue_rows[0])
            row = self.rows[index]
            if row.needs_action:
                self.table.setCurrentCell(index, 3)
                row.action.setFocus()
            else:
                self.table.setFocus()
                self.table.setCurrentItem(row.filename)
                self.table.editItem(row.filename)

    def _use_unique_names(self):
        active = [row for row in self.rows if row.target is not None and row.action.currentText() != "Skip"]
        reserved = {export_path_alias_key(row.filename.text()) for row in active}
        kept = set()
        next_suffix: dict[str, int] = {}
        # Honour an explicit replacement even if its row follows a duplicate.
        for row in sorted(active, key=lambda row: row.action.currentText() != "Replace"):
            name = row.filename.text()
            key = export_path_alias_key(name)
            if key not in kept and row.action.currentText() != "Review":
                kept.add(key)
                continue
            number = next_suffix.get(key, 2)
            stem, ext = os.path.splitext(name)
            while True:
                candidate = f"{stem} ({number}){ext}"
                number += 1
                alias = export_path_alias_key(candidate)
                if alias not in reserved and not os.path.lexists(os.path.join(self.folder, candidate)):
                    break
            reserved.add(alias)
            next_suffix[key] = number
            with QtCore.QSignalBlocker(self.table):
                row.filename.setText(candidate)
            self._inspect(row)
        self._update()

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
