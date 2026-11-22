"""User-controlled desktop integration through the existing CLI."""
import sys
from pathlib import Path

from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

from qplot._icons import application_icon
from qplot.desktop import default_destination, is_owned_launcher

from ._small_dialogs import (
    close_default,
    dialog_layout,
    equal_button_widths,
    resize_dialog,
)


class DesktopIntegrationDialog(qtw.QDialog):
    def __init__(self, parent: qtw.QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Desktop integration")
        self.setObjectName("desktopIntegrationDialog")
        self.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        self._pending: list[list[str]] = []
        self._busy = False
        self._operation = ""
        layout = dialog_layout(self)
        icon = application_icon()
        self.setWindowIcon(icon)
        heading = qtw.QHBoxLayout()
        heading.setSpacing(16)
        icon_label = qtw.QLabel()
        icon_label.setAccessibleName("qPlot application icon")
        icon_label.setPixmap(icon.pixmap(QtCore.QSize(64, 64), self.devicePixelRatioF()))
        icon_label.setFixedSize(64, 64)
        heading.addWidget(icon_label)
        title = qtw.QLabel("qPlot desktop integration")
        title_font = title.font()
        title_font.setBold(True)
        title.setFont(title_font)
        heading.addWidget(title, 1)
        layout.addLayout(heading)
        description = qtw.QLabel(
            "Install a launcher to open qPlot from your desktop or your file manager."
        )
        description.setWordWrap(True)
        layout.addWidget(description)
        destination_label = qtw.QLabel("&Launcher location:")
        layout.addWidget(destination_label)
        self.destination = qtw.QLineEdit()
        self.destination.setToolTip("Location where the qPlot launcher will be installed or updated.")
        self.destination.setAccessibleName("Launcher location")
        destination_label.setBuddy(self.destination)
        destination_row = qtw.QHBoxLayout()
        destination_row.addWidget(self.destination, 1)
        self.browse = qtw.QPushButton("Browse…")
        self.browse.setToolTip("Choose the folder in which to install the launcher.")
        self.browse.clicked.connect(self._browse)
        destination_row.addWidget(self.browse)
        layout.addLayout(destination_row)
        self.installation_status = qtw.QLabel()
        self.installation_status.setWordWrap(True)
        layout.addWidget(self.installation_status)
        self.shortcut = qtw.QCheckBox("Add a desktop &shortcut")
        self.shortcut.setToolTip("Create a desktop shortcut to the installed qPlot launcher.")
        self.shortcut.setChecked(True)
        layout.addWidget(self.shortcut)
        self.associations = qtw.QCheckBox("Add qPlot to Open &With and show database icons")
        self.associations.setToolTip(
            "Keep your current default app for .db files. Database, WAL and SHM files get icons. "
            "Open the .db file to view the measurement."
        )
        self.associations.setChecked(True)
        layout.addWidget(self.associations)
        self.output = qtw.QPlainTextEdit()
        self.output.setToolTip("Progress and results of the desktop integration operation. Select text to copy it.")
        self.output.setReadOnly(True)
        self.output.setTabChangesFocus(True)
        self.output.setAccessibleName("Desktop integration progress")
        layout.addWidget(self.output, 1)
        self.output.hide()
        buttons = qtw.QDialogButtonBox(qtw.QDialogButtonBox.StandardButton.Close)
        self.install_button = qtw.QPushButton("Install launcher")
        self.install_button.setToolTip("Install or update the launcher at the chosen location with these options.")
        buttons.addButton(self.install_button, qtw.QDialogButtonBox.ButtonRole.ActionRole)
        self.remove_button = qtw.QPushButton("Remove integration")
        self.remove_button.setToolTip("Remove the registered qPlot integration and its owned shortcut.")
        buttons.addButton(self.remove_button, qtw.QDialogButtonBox.ButtonRole.ActionRole)
        self.close_button = buttons.button(qtw.QDialogButtonBox.StandardButton.Close)
        assert self.close_button is not None
        buttons.rejected.connect(self.reject)
        self.install_button.clicked.connect(lambda: self._start("setup"))
        self.remove_button.clicked.connect(lambda: self._start("remove"))
        layout.addWidget(buttons)
        equal_button_widths(self.install_button, self.remove_button)
        close_default(buttons)
        self.process = QtCore.QProcess(self)
        self.process.setProcessChannelMode(QtCore.QProcess.ProcessChannelMode.MergedChannels)
        environment = QtCore.QProcessEnvironment.systemEnvironment()
        for key in environment.keys():
            if key.startswith(("QPLOT_", "_QPLOT_")) or key == "__PYVENV_LAUNCHER__":
                environment.remove(key)
        environment.insert("PYTHONIOENCODING", "utf-8")
        self.process.setProcessEnvironment(environment)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._error)
        try:
            inspect_location = QtCore.QTimer(self)
            inspect_location.setSingleShot(True)
            inspect_location.setInterval(200)
            inspect_location.timeout.connect(self._update_status)
            self.destination.textChanged.connect(lambda: inspect_location.start())
            self.destination.editingFinished.connect(self._update_status)
            self.destination.setText(str(default_destination()))
            self.destination.setCursorPosition(0)
            self._update_status()
        except ValueError as error:
            self.installation_status.setText(str(error))
            self.browse.setEnabled(False)
            self.destination.setEnabled(False)
            self.install_button.setEnabled(False)
            self.remove_button.hide()

        resize_dialog(self, 640, 315)

    def _browse(self):
        current = self.destination.text().strip()
        default = default_destination()
        try:
            path = Path(current).expanduser() if current else default
        except (OSError, RuntimeError, ValueError):
            path = default
        folder = qtw.QFileDialog.getExistingDirectory(self, "Choose launcher installation folder", str(path.parent))
        if folder:
            directory = Path(folder)
            destination = directory if is_owned_launcher(directory) else directory / (path.name or default.name)
            self.destination.setText(str(destination))
            self._update_status()

    def _update_status(self):
        current = self.destination.text().strip()
        owned = False
        available = False
        if not current:
            status = "Choose a launcher location."
        else:
            try:
                location = Path(current).expanduser()
                owned = is_owned_launcher(location)
                available = owned or not (location.exists() or location.is_symlink())
            except (OSError, ValueError, RuntimeError):
                available = False
            if owned:
                status = "qPlot is installed at this location."
            elif available:
                status = "No launcher is installed at this location."
            else:
                status = "Choose an unused location or an existing qPlot launcher."
        self.installation_status.setText(status)
        self.install_button.setText("Update launcher" if owned else "Install launcher")
        self.install_button.setEnabled(available and not self._busy)
        self.remove_button.setVisible(owned)
        self.remove_button.setEnabled(owned and not self._busy)

    def _start(self, action: str) -> None:
        if self._busy:
            return
        self._update_status()
        if not self.install_button.isEnabled() or (action == "remove" and not self.remove_button.isEnabled()):
            return
        destination = Path(self.destination.text().strip()).expanduser().absolute()
        common = ["--destination", str(destination)]
        command = [action, *common]
        if action == "setup":
            if self.associations.isChecked():
                command.append("--file-associations")
            if not self.shortcut.isChecked():
                command.append("--no-desktop-shortcut")
        self._pending = [command]
        if action == "setup" and (destination.exists() or destination.is_symlink()):
            # CLI removal validates ownership and checks for measurement files
            # before touching the existing launcher or its registrations.
            self._pending.insert(0, ["remove", *common])
        self._operation = action
        self.output.show()
        if self.height() < 430:
            resize_dialog(self, 640, 430)
        self.output.setPlainText("Updating desktop integration…" if action == "setup" else "Removing desktop integration…")
        self._set_busy(True)
        self._next()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for control in (self.destination, self.browse, self.shortcut, self.associations,
                        self.install_button, self.remove_button, self.close_button):
            if control is not None:
                control.setEnabled(not busy)
        if not busy:
            self._update_status()

    def _next(self) -> None:
        arguments = self._pending.pop(0)
        self.process.start(sys.executable, ["-m", "qplot.desktop", *arguments])

    def _read_output(self) -> None:
        text = self.process.readAllStandardOutput().data().decode("utf-8", errors="replace")
        cursor = self.output.textCursor()
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        self.output.setTextCursor(cursor)

    def _finished(self, code: int, status: QtCore.QProcess.ExitStatus) -> None:
        if not self._busy:
            return
        self._read_output()
        if code != 0 or status != QtCore.QProcess.ExitStatus.NormalExit:
            self._pending.clear()
            self.output.appendPlainText("Desktop integration failed. Review the details above and try again.")
        elif self._pending:
            self._next()
            return
        else:
            self.output.appendPlainText(
                "Desktop integration installed. You may need to reopen Finder or File Explorer to see updated icons."
                if self._operation == "setup" else "Desktop integration removed."
            )
        self._set_busy(False)

    def _error(self, error: QtCore.QProcess.ProcessError) -> None:
        if error == QtCore.QProcess.ProcessError.FailedToStart:
            self._pending.clear()
            self.output.appendPlainText(f"Could not start desktop setup: {self.process.errorString()}")
            self._set_busy(False)

    def reject(self) -> None:
        if not self._busy:
            super().reject()

    def closeEvent(self, event: QtGui.QCloseEvent | None) -> None:
        if self._busy:
            if event is not None:
                event.ignore()
        else:
            super().closeEvent(event)
