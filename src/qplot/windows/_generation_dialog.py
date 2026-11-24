"""Review synthetic data inputs and present the publishing worker's progress."""

import csv
import os
from importlib import resources
from pathlib import Path

from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from qplot.testdata import INSTRUCTION_FILE_NAMES, RunSpecification, read_specifications

from ._file_manager import reveal_file_in_file_manager
from ._small_dialogs import confirm_action, dialog_layout


class GenerationReview(qtw.QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Generate test database")
        self.directory = parent.test_database_csv_directory()
        self.remember_directory = parent._remember_test_database_csv_directory
        self.custom_path = ""
        self.database_path = ""
        self.overwrite = False
        self.specifications: list[RunSpecification] = []
        layout = dialog_layout(self)
        form = qtw.QFormLayout()
        layout.addLayout(form)
        self.source = qtw.QComboBox(self)
        self.source.addItems(["Bundled template", "My specification CSV"])
        form.addRow("&Source", self.source)
        self.templates = qtw.QComboBox(self)
        for filename in INSTRUCTION_FILE_NAMES:
            self.templates.addItem(filename, filename)
        form.addRow("&Template", self.templates)
        self.csv_path = qtw.QLineEdit(self)
        self.csv_path.setReadOnly(True)
        self.csv_path.setPlaceholderText("Choose a specification CSV")
        self.csv_path.setAccessibleName("Specification CSV")
        self.custom_row = qtw.QWidget(self)
        custom = qtw.QHBoxLayout(self.custom_row)
        custom.setContentsMargins(0, 0, 0, 0)
        custom.addWidget(self.csv_path)
        browse = qtw.QPushButton("Browse…")
        browse.setAccessibleName("Browse for specification CSV")
        browse.clicked.connect(self.choose_csv)
        custom.addWidget(browse)
        form.addRow("Specification CSV", self.custom_row)
        self.summary = qtw.QLabel(self)
        self.summary.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.summary.setWordWrap(True)
        form.addRow("Workload", self.summary)
        dest = qtw.QHBoxLayout()
        self.destination = qtw.QLineEdit(self)
        self.destination.setReadOnly(True)
        self.destination.setAccessibleName("Output database")
        self.destination.setPlaceholderText("Choose an output database")
        dest.addWidget(self.destination)
        self.browse_output = qtw.QPushButton("Browse…", self)
        self.browse_output.setAccessibleName("Choose output database")
        self.browse_output.clicked.connect(self.choose_destination)
        dest.addWidget(self.browse_output)
        form.addRow("Output database", dest)
        buttons = qtw.QDialogButtonBox(qtw.QDialogButtonBox.StandardButton.Cancel)
        self.generate = qtw.QPushButton("Generate database", self)
        buttons.addButton(self.generate, qtw.QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.source.currentIndexChanged.connect(self.update_specification)
        self.templates.currentIndexChanged.connect(self.update_specification)
        self.form = form
        self.update_specification()
        self.resize(540, self.sizeHint().height())

    def choose_csv(self):
        path, _ = qtw.QFileDialog.getOpenFileName(
            self, "Choose specification CSV", self.directory, "CSV files (*.csv)"
        )
        if path:
            self.custom_path = os.path.abspath(path)
            self.directory = os.path.dirname(self.custom_path)
            self.remember_directory(self.directory)
            self.csv_path.setText(self.custom_path)
            self.csv_path.setToolTip(self.custom_path)
            self.update_specification()

    def update_specification(self):
        bundled = self.source.currentIndex() == 0
        self.form.setRowVisible(self.templates, bundled)
        self.form.setRowVisible(self.custom_row, not bundled)
        filename = self.templates.currentData()
        path = self.custom_path
        if bundled:
            path = str(resources.files("qplot").joinpath("resources", "testdata", filename))
        self.specifications = []
        message = "Choose a specification CSV to see its workload."
        if path:
            try:
                self.specifications = read_specifications(path)
            except (ValueError, OSError, csv.Error) as error:
                message = str(error)
            else:
                runs = len(self.specifications)
                points = sum(spec.point_count for spec in self.specifications)
                run_word = "run" if runs == 1 else "runs"
                point_word = "point" if points == 1 else "points"
                message = f"{runs:,} {run_word} · {points:,} {point_word}"
                if bundled:
                    size = Path(filename).stem.rsplit("_", 1)[-1]
                    message += f" · approximately {size[:-2]} {size[-2:].upper()}"
        self.summary.setText(message)
        self.browse_output.setEnabled(bool(self.specifications))
        self.generate.setEnabled(bool(self.specifications and self.database_path))

    def choose_destination(self):
        source = self.templates.currentData() if self.source.currentIndex() == 0 else self.custom_path
        suggestion = self.database_path or str(Path(self.directory) / Path(source).with_suffix(".db").name)
        path, _ = qtw.QFileDialog.getSaveFileName(
            self, "Choose output database", suggestion, "QCoDeS databases (*.db)",
            options=qtw.QFileDialog.Option.DontConfirmOverwrite,
        )
        if path:
            self.database_path = os.path.abspath(path if path.lower().endswith(".db") else path + ".db")
            self.destination.setText(self.database_path)
            self.destination.setToolTip(self.database_path)
            self.generate.setEnabled(bool(self.specifications))

    def accept(self):
        if not self.specifications or not self.database_path:
            return
        self.overwrite = os.path.lexists(self.database_path)
        if self.overwrite and not confirm_action(
            self, "Replace database?",
            f"Replace this file with the generated database?\n\n{self.database_path}",
            affirmative="Replace file",
        ):
            return
        super().accept()


class GenerationProgress(qtw.QDialog):
    def __init__(self, parent, path, worker):
        super().__init__(parent)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle("Synthetic database generation")
        layout = dialog_layout(self)
        self.status = qtw.QLabel("Preparing generation…", self)
        self.status.setWordWrap(True)
        self.status.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        destination = qtw.QLabel(path, self)
        destination.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        destination.setWordWrap(True)
        destination.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(destination)
        self.bar = qtw.QProgressBar(self)
        self.bar.setRange(0, 0)
        layout.addWidget(self.bar)
        buttons = qtw.QDialogButtonBox(self)
        self.dismiss = qtw.QPushButton("Cancel generation", self)
        self.open_file = qtw.QPushButton("Open database", self)
        self.open_folder = qtw.QPushButton("Show in folder", self)
        buttons.addButton(self.dismiss, qtw.QDialogButtonBox.ButtonRole.RejectRole)
        buttons.addButton(self.open_file, qtw.QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(self.open_folder, qtw.QDialogButtonBox.ButtonRole.ActionRole)
        buttons.rejected.connect(self.reject)
        self.open_file.clicked.connect(lambda: parent.load_database_path(path))

        def show_in_folder():
            if not reveal_file_in_file_manager(path):
                self.status.setText("Could not open the folder. The database is available at the path below.")

        self.open_folder.clicked.connect(show_in_folder)
        self.open_file.hide()
        self.open_folder.hide()
        layout.addWidget(buttons)
        self.worker = worker
        self.resize(450, self.sizeHint().height())

    def reject(self):
        if self.worker is None:
            super().reject()
        else:
            self.worker.cancel()
            self.dismiss.setEnabled(False)
            self.status.setText("Cancelling generation…")

    def finish(self, message, success):
        self.worker = None
        self.status.setText(message)
        self.bar.hide()
        self.dismiss.setText("Close")
        self.dismiss.setEnabled(True)
        self.open_file.setVisible(success)
        self.open_folder.setVisible(success)
        self.resize(self.width(), self.sizeHint().height())
