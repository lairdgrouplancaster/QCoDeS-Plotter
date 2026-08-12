import csv
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PyQt6 import QtGui
from PyQt6 import QtWidgets as qtw

from qplot import testdata as testdata_module
from qplot.datahandling.file_identity import (
    database_instance,
    database_publication_guard_path,
)
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


def test_create_example_csv_confirms_normalized_existing_path(tmp_path):
    harness = GuiHarness(tmp_path)
    selected_path = tmp_path / "example"
    csv_path = tmp_path / "example.csv"
    sentinel = b"existing example sentinel"
    csv_path.write_bytes(sentinel)

    try:
        with (
            patch.object(
                qtw.QFileDialog,
                "getSaveFileName",
                return_value=(str(selected_path), "CSV Files (*.csv)"),
            ),
            patch.object(
                qtw.QMessageBox,
                "question",
                return_value=qtw.QMessageBox.StandardButton.No,
            ) as question,
            patch(
                "qplot.windows._database_actions.reveal_file_in_file_manager",
            ) as reveal_file,
        ):
            assert not harness.create_test_database_csv()

        assert csv_path.read_bytes() == sentinel
        assert not selected_path.exists()
        assert set(tmp_path.iterdir()) == {csv_path}
        assert str(csv_path) in question.call_args.args[2]
        reveal_file.assert_not_called()
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
    output_directory = tmp_path / "GUI # %3f space 測定"
    output_directory.mkdir()
    database_path = output_directory / "out#%23 space 測定.db"
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
    harness._reload_replaced_database = Mock(return_value=True)
    specification = Mock(point_count=5)

    try:
        with patch(
            "qplot.windows._database_actions.quarantine_wal_for_replaced_database"
        ) as quarantine:
            harness.test_database_generation_finished(
                str(database_path),
                [specification],
                None,
                True,
            )

        quarantine.assert_called_once_with(str(database_path))
        harness._reload_replaced_database.assert_called_once_with(str(database_path))
    finally:
        harness.deleteLater()


def test_generation_releases_current_database_before_replacing_it(tmp_path):
    harness = GuiHarness(tmp_path)
    csv_path = tmp_path / "runs.csv"
    output_directory = tmp_path / "loaded # %23 測定"
    output_directory.mkdir()
    database_path = output_directory / "runs#%3f 測定.db"
    testdata_module.generate_database(
        [
            testdata_module.RunSpecification(
                1,
                "old_current",
                "Old current",
                "nA",
                -1.0,
                1.0,
                3,
            )
        ],
        database_path,
    )
    write_small_specification(csv_path)
    harness.fileTextbox = type(
        "Field",
        (),
        {"text": lambda _self: str(database_path)},
    )()
    harness._prepare_test_database_replacement = Mock()
    harness._reload_replaced_database = Mock(return_value=True)

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
            patch.object(
                qtw.QMessageBox,
                "question",
                return_value=qtw.QMessageBox.StandardButton.Yes,
            ),
            patch(
                "qplot.windows._database_actions."
                "quarantine_wal_for_replaced_database"
            ) as quarantine,
        ):
            assert harness.generate_test_database_from_csv()

        harness._prepare_test_database_replacement.assert_called_once_with(
            str(database_path)
        )
        quarantine.assert_called_once_with(str(database_path))
        harness._reload_replaced_database.assert_called_once_with(str(database_path))
        connection = sqlite3.connect(database_path)
        try:
            assert connection.execute("SELECT name FROM runs").fetchall() == [
                ("run_1",)
            ]
        finally:
            connection.close()
    finally:
        harness.deleteLater()


def test_failed_current_database_replacement_reloads_without_quarantine(tmp_path):
    harness = GuiHarness(tmp_path)
    csv_path = tmp_path / "runs.csv"
    database_path = tmp_path / "runs.db"
    database_path.write_bytes(b"unchanged database")
    write_small_specification(csv_path)
    harness.fileTextbox = type(
        "Field",
        (),
        {"text": lambda _self: str(database_path)},
    )()
    def capture_original_instance(path):
        harness._test_database_replacement_state = SimpleNamespace(
            database_path=str(path),
            original_instance=database_instance(path),
            outcome=None,
        )
        harness._database_view_released_for_generation = True

    harness._prepare_test_database_replacement = Mock(
        side_effect=capture_original_instance
    )

    def reload_after_error(*args, **kwargs):
        assert harness.errors
        return True

    harness.load_file = Mock(side_effect=reload_after_error)
    publication_error = RuntimeError(
        "database active or SQLite sidecars present; close database users and retry"
    )

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
            patch.object(
                qtw.QMessageBox,
                "question",
                return_value=qtw.QMessageBox.StandardButton.Yes,
            ),
            patch(
                "qplot.windows._database_actions.generate_database",
                side_effect=publication_error,
            ),
            patch(
                "qplot.windows._database_actions."
                "quarantine_wal_for_replaced_database"
            ) as quarantine,
        ):
            assert harness.generate_test_database_from_csv()

        harness._prepare_test_database_replacement.assert_called_once_with(
            str(database_path)
        )
        quarantine.assert_not_called()
        harness.load_file.assert_called_once_with(
            str(database_path),
            force=True,
            generation_recovery=True,
        )
        assert harness.errors == [
            (
                "Test Database Generation Failed",
                "Could not generate the test database.",
                str(publication_error),
            )
        ]
        assert database_path.read_bytes() == b"unchanged database"
    finally:
        harness.deleteLater()


def test_ambiguous_sidecar_failure_keeps_guarded_database_unloaded(tmp_path):
    harness = GuiHarness(tmp_path)
    database_path = tmp_path / "guarded.db"
    database_path.write_bytes(b"restored database")
    database_publication_guard_path(database_path).write_text(
        "ambiguous sidecar race",
        encoding="utf-8",
    )
    harness.fileTextbox = type(
        "Field",
        (),
        {"text": lambda _self: str(database_path)},
    )()
    harness._test_database_replacement_state = SimpleNamespace(
        database_path=str(database_path),
        original_instance=database_instance(database_path),
        outcome=None,
    )
    harness._database_view_released_for_generation = True
    harness.load_file = Mock(return_value=True)
    publication_error = RuntimeError(
        "database active or SQLite sidecars present; safety guard retained"
    )

    try:
        with patch(
            "qplot.windows._database_actions.quarantine_wal_for_replaced_database"
        ) as quarantine:
            harness.test_database_generation_finished(
                str(database_path),
                [Mock(point_count=5)],
                publication_error,
            )

        quarantine.assert_not_called()
        harness.load_file.assert_not_called()
        assert harness.errors[-1][2] == str(publication_error)
        assert harness._test_database_replacement_state.outcome == "ambiguous"
        assert harness._database_view_released_for_generation
        assert not harness.generateTestDatabaseAction.isEnabled()
        assert not harness._database_generation_read_allowed(
            str(database_path),
            notify=False,
        )
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
        assert not any(
            path.name.startswith(testdata_module._TEMPORARY_DATABASE_PREFIX)
            for path in tmp_path.iterdir()
        )
        assert not harness._test_database_generation_active
        assert harness._test_database_generation_worker is None
        assert harness.generateTestDatabaseAction.isEnabled()
        assert "cancelled" in harness.status_messages[-1][0].lower()
    finally:
        harness.deleteLater()


def test_exception_after_publication_is_reloaded_as_replacement(tmp_path):
    harness = GuiHarness(tmp_path)
    csv_path = tmp_path / "runs.csv"
    database_path = tmp_path / "runs.db"
    replacement_path = tmp_path / "replacement.db"
    database_path.write_bytes(b"original database instance")
    replacement_path.write_bytes(b"published replacement instance")
    write_small_specification(csv_path)
    harness.fileTextbox = type(
        "Field",
        (),
        {"text": lambda _self: str(database_path)},
    )()
    def capture_original_instance(path):
        harness._test_database_replacement_state = SimpleNamespace(
            database_path=str(path),
            original_instance=database_instance(path),
            outcome=None,
        )
        harness._database_view_released_for_generation = True

    harness._prepare_test_database_replacement = Mock(
        side_effect=capture_original_instance
    )
    harness._reload_replaced_database = Mock(return_value=True)

    publication_error = RuntimeError("injected failure after publication")

    def publish_then_fail(*_args, **_kwargs):
        replacement_path.replace(database_path)
        raise publication_error

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
            patch.object(
                qtw.QMessageBox,
                "question",
                return_value=qtw.QMessageBox.StandardButton.Yes,
            ),
            patch(
                "qplot.windows._database_actions.generate_database",
                side_effect=publish_then_fail,
            ),
            patch(
                "qplot.windows._database_actions."
                "quarantine_wal_for_replaced_database"
            ) as quarantine,
        ):
            assert harness.generate_test_database_from_csv()

        assert database_path.read_bytes() == b"published replacement instance"
        assert not harness._test_database_generation_active
        assert harness._test_database_generation_worker is None
        assert not harness.generateTestDatabaseAction.isEnabled()
        assert harness._database_view_released_for_generation
        quarantine.assert_called_once_with(str(database_path))
        harness._reload_replaced_database.assert_called_once_with(
            str(database_path),
            generation_recovery=True,
        )
        assert harness.errors == [
            (
                "Test Database Generation Failed",
                "Could not generate the test database.",
                str(publication_error),
            )
        ]
    finally:
        harness.deleteLater()


def test_generation_declined_overwrite_leaves_existing_output_unchanged(tmp_path):
    harness = GuiHarness(tmp_path)
    csv_path = tmp_path / "runs.csv"
    database_path = tmp_path / "runs.db"
    database_path.write_bytes(b"existing owner")
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
            patch.object(
                qtw.QMessageBox,
                "question",
                return_value=qtw.QMessageBox.StandardButton.No,
            ) as question,
        ):
            assert not harness.generate_test_database_from_csv()

        question.assert_called_once()
        assert database_path.read_bytes() == b"existing owner"
        assert harness._test_database_generation_worker is None
        assert "cancelled" in harness.status_messages[-1][0].lower()
    finally:
        harness.deleteLater()


def test_new_gui_output_does_not_clobber_publication_race(tmp_path):
    harness = GuiHarness(tmp_path)
    csv_path = tmp_path / "runs.csv"
    database_path = tmp_path / "runs.db"
    write_small_specification(csv_path)

    class RacingThreadPool:
        def start(self, worker):
            assert not worker.overwrite
            database_path.write_bytes(b"concurrent owner")
            worker.run()

    harness.testDatabaseGenerationThreadPool = RacingThreadPool()
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

        assert database_path.read_bytes() == b"concurrent owner"
        assert harness.errors[0][0] == "Test Database Generation Failed"
        assert "already exists" in harness.errors[0][2]
        assert not any(
            path.name.startswith(testdata_module._TEMPORARY_DATABASE_PREFIX)
            for path in tmp_path.iterdir()
        )
    finally:
        harness.deleteLater()
