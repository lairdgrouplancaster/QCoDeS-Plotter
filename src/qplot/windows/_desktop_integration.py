"""User-controlled desktop integration through the existing CLI."""
import sys
from pathlib import Path

from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

from qplot._icons import application_icon
from qplot.desktop import default_destination


class DesktopIntegrationDialog(qtw.QDialog):
    def __init__(self, parent: qtw.QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Desktop integration")
        self.setObjectName("desktopIntegrationDialog")
        self.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        self.resize(580, 380)
        self._pending: list[list[str]] = []
        self._busy = False
        self._operation = ""
        layout = qtw.QVBoxLayout(self)
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
            "Install a qPlot launcher and optional desktop shortcut. File associations "
            "offer qPlot in Open With without changing your default opener. Database, "
            "WAL and SHM files receive icons; WAL and SHM files are not opened."
        )
        description.setWordWrap(True)
        layout.addWidget(description)
        layout.addWidget(qtw.QLabel("Launcher location:"))
        self.destination = qtw.QLineEdit()
        self.destination.setToolTip("Location where the qPlot launcher will be installed or updated.")
        self.destination.setAccessibleName("Launcher location")
        layout.addWidget(self.destination)
        self.shortcut = qtw.QCheckBox("Add a desktop shortcut")
        self.shortcut.setToolTip("Create a desktop shortcut to the installed qPlot launcher.")
        self.shortcut.setChecked(True)
        layout.addWidget(self.shortcut)
        self.associations = qtw.QCheckBox("Register file associations and database icons")
        self.associations.setToolTip("Offer qPlot in Open With and register database icons without changing your default opener.")
        self.associations.setChecked(True)
        layout.addWidget(self.associations)
        note = qtw.QLabel(
            "Install/update replaces an existing qPlot launcher at this location. "
            "Remove deletes its registered integration and owned shortcut."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.output = qtw.QPlainTextEdit()
        self.output.setToolTip("Progress and results of the desktop integration operation. Select text to copy it.")
        self.output.setReadOnly(True)
        self.output.setAccessibleName("Desktop integration progress")
        layout.addWidget(self.output)
        buttons = qtw.QDialogButtonBox(qtw.QDialogButtonBox.StandardButton.Close)
        self.install_button = qtw.QPushButton("Install/update integration")
        self.install_button.setToolTip("Install or replace the qPlot launcher at the chosen location with the selected integration options.")
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
            self.destination.setText(str(default_destination()))
        except ValueError as error:
            self.output.setPlainText(str(error))
            self.install_button.setEnabled(False)
            self.remove_button.setEnabled(False)

    def _start(self, action: str) -> None:
        if self._busy:
            return
        if not self.destination.text().strip():
            self.output.setPlainText("Choose a launcher location first.")
            return
        destination = Path(self.destination.text()).expanduser().absolute()
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
        self.output.setPlainText("Updating desktop integration…" if action == "setup" else "Removing desktop integration…")
        self._set_busy(True)
        self._next()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for control in (self.destination, self.shortcut, self.associations,
                        self.install_button, self.remove_button, self.close_button):
            if control is not None:
                control.setEnabled(not busy)

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
            self.output.appendPlainText("Desktop integration failed. See the details above; you can retry.")
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
