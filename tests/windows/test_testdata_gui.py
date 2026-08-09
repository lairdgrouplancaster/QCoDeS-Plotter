import csv
import sqlite3
from unittest.mock import Mock, patch

from PyQt6 import QtGui
from PyQt6 import QtWidgets as qtw

from qplot.testdata import CSV_COLUMNS, INSTRUCTION_FILE_NAMES
from qplot.windows._database_actions import (
    DatabaseActionsMixin,
    reveal_file_in_file_manager,
)


class ImmediateThreadPool:
    def start(self, worker):
        worker.run()


class GuiHarness(DatabaseActionsMixin, qtw.QWidget):
    def __init__(self, directory):
        super().__init__()
        self.directory = str(directory)
        self.status_messages = []
        self.errors = []
        self._test_database_generation_active = False
        self._test_database_generation_worker = None
        self.testDatabaseGenerationThreadPool = ImmediateThreadPool()
        self.generateTestDatabaseAction = QtGui.QAction(self)

    def database_open_directory(self):
        return self.directory

    def show_status(self, message, timeout=5000):
        self.status_messages.append((message, timeout))

    def show_error(self, title, message, details=None):
        self.errors.append((title, message, details))


def write_small_specification(path):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        writer.writerow(("1", "current", "Current", "nA", "-1", "1", "5", "", "", ""))


def test_create_example_csv_opens_its_folder(tmp_path):
    harness = GuiHarness(tmp_path)
    csv_path = tmp_path / "example.csv"

    try:
        with (
            patch.object(
                qtw.QFileDialog,
                "getSaveFileName",
                return_value=(str(csv_path), "CSV Files (*.csv)"),
            ),
            patch(
                "qplot.windows._database_actions.reveal_file_in_file_manager",
                return_value=True,
            ) as reveal_file,
        ):
            assert harness.create_test_database_csv()

        assert csv_path.is_file()
        reveal_file.assert_called_once_with(str(csv_path))
        assert "Created example CSV" in harness.status_messages[-1][0]
        assert harness.errors == []
    finally:
        harness.deleteLater()


def test_export_csv_collection_opens_its_folder(tmp_path):
    harness = GuiHarness(tmp_path)

    try:
        with (
            patch.object(
                qtw.QFileDialog,
                "getExistingDirectory",
                return_value=str(tmp_path),
            ),
            patch(
                "qplot.windows._database_actions.reveal_file_in_file_manager",
                return_value=True,
            ) as reveal_file,
        ):
            assert harness.export_test_database_csv_collection()

        output_paths = tuple(tmp_path / name for name in INSTRUCTION_FILE_NAMES)
        assert all(path.is_file() for path in output_paths)
        reveal_file.assert_called_once_with(output_paths[0])
        assert "Exported 10 instruction CSV files" in harness.status_messages[-1][0]
        assert harness.errors == []
    finally:
        harness.deleteLater()


def test_reveal_file_uses_finder_selection_on_macos(tmp_path):
    csv_path = tmp_path / "example.csv"
    csv_path.touch()
    completed = Mock(returncode=0)

    with (
        patch("qplot.windows._database_actions.sys.platform", "darwin"),
        patch(
            "qplot.windows._database_actions.subprocess.run",
            return_value=completed,
        ) as run,
    ):
        assert reveal_file_in_file_manager(csv_path)

    run.assert_called_once_with(
        ["open", "-R", str(csv_path)],
        capture_output=True,
        check=False,
        timeout=5,
    )


def test_generate_database_from_csv_runs_in_worker_pool(tmp_path):
    harness = GuiHarness(tmp_path)
    csv_path = tmp_path / "runs.csv"
    database_path = tmp_path / "runs.db"
    write_small_specification(csv_path)

    try:
        with (
            patch.object(
                qtw.QFileDialog,
                "getOpenFileName",
                return_value=(str(csv_path), "CSV Files (*.csv)"),
            ),
            patch.object(
                qtw.QFileDialog,
                "getSaveFileName",
                return_value=(str(database_path), "QCoDeS Database (*.db)"),
            ),
        ):
            assert harness.generate_test_database_from_csv()

        assert database_path.is_file()
        connection = sqlite3.connect(database_path)
        try:
            assert connection.execute("SELECT name FROM runs").fetchall() == [("run_1",)]
        finally:
            connection.close()
        assert not harness._test_database_generation_active
        assert harness._test_database_generation_worker is None
        assert harness.generateTestDatabaseAction.isEnabled()
        assert "with 1 run and 5 points" in harness.status_messages[-1][0]
        assert harness.errors == []
    finally:
        harness.deleteLater()


def test_generation_completion_force_reloads_replaced_current_database(tmp_path):
    harness = GuiHarness(tmp_path)
    database_path = tmp_path / "runs.db"
    harness.fileTextbox = type(
        "Field",
        (),
        {"text": lambda _self: str(database_path)},
    )()
    harness.load_file = Mock(return_value=True)
    specification = Mock(point_count=5)

    try:
        harness.test_database_generation_finished(
            str(database_path),
            [specification],
            None,
        )

        harness.load_file.assert_called_once_with(str(database_path), force=True)
    finally:
        harness.deleteLater()


def test_generation_releases_current_database_before_replacing_it(tmp_path):
    harness = GuiHarness(tmp_path)
    csv_path = tmp_path / "runs.csv"
    database_path = tmp_path / "runs.db"
    write_small_specification(csv_path)
    harness.fileTextbox = type(
        "Field",
        (),
        {"text": lambda _self: str(database_path)},
    )()
    harness._prepare_replaced_database_reload = Mock()
    harness.load_file = Mock(return_value=True)

    try:
        with (
            patch.object(
                qtw.QFileDialog,
                "getOpenFileName",
                return_value=(str(csv_path), "CSV Files (*.csv)"),
            ),
            patch.object(
                qtw.QFileDialog,
                "getSaveFileName",
                return_value=(str(database_path), "QCoDeS Database (*.db)"),
            ),
        ):
            assert harness.generate_test_database_from_csv()

        harness._prepare_replaced_database_reload.assert_called_once_with(
            str(database_path)
        )
        harness.load_file.assert_called_once_with(str(database_path), force=True)
    finally:
        harness.deleteLater()


def test_invalid_csv_is_reported_before_database_destination_prompt(tmp_path):
    harness = GuiHarness(tmp_path)
    csv_path = tmp_path / "invalid.csv"
    csv_path.write_text("not,a,valid,header\n", encoding="utf-8")

    try:
        with (
            patch.object(
                qtw.QFileDialog,
                "getOpenFileName",
                return_value=(str(csv_path), "CSV Files (*.csv)"),
            ),
            patch.object(qtw.QFileDialog, "getSaveFileName") as save_dialog,
        ):
            assert not harness.generate_test_database_from_csv()

        save_dialog.assert_not_called()
        assert harness.errors[0][0] == "Invalid Test Database CSV"
        assert "Invalid CSV header" in harness.errors[0][2]
    finally:
        harness.deleteLater()


def test_generation_cancelled_before_first_run_removes_temporary_database(tmp_path):
    harness = GuiHarness(tmp_path)
    csv_path = tmp_path / "runs.csv"
    database_path = tmp_path / "runs.db"
    write_small_specification(csv_path)

    class CancellingThreadPool:
        def start(self, worker):
            worker.cancel()
            worker.run()

    harness.testDatabaseGenerationThreadPool = CancellingThreadPool()
    try:
        with (
            patch.object(
                qtw.QFileDialog,
                "getOpenFileName",
                return_value=(str(csv_path), "CSV Files (*.csv)"),
            ),
            patch.object(
                qtw.QFileDialog,
                "getSaveFileName",
                return_value=(str(database_path), "QCoDeS Database (*.db)"),
            ),
        ):
            assert harness.generate_test_database_from_csv()

        assert not database_path.exists()
        assert list(tmp_path.glob(".runs-*.db")) == []
    finally:
        harness.deleteLater()
