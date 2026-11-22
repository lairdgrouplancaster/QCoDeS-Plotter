"""Explicit synthetic-data actions, separate from ordinary database viewing.

The worker is the only GUI entry point to the synthetic database writer.
The mixin owns CSV selection, overwrite consent, worker startup and its outcome.
MainWindow also supplies DatabaseActionsMixin's reader gates, retirement and
recovery methods; those stay with the readers they protect. It supplies the
widgets, configuration, worker pool and error/status presentation as well.
"""

import os
import stat
import threading
from importlib import resources
from pathlib import Path
from time import monotonic

from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw
from PyQt6.QtGui import QDesktopServices

from qplot.datahandling.cloud_files import (
    DATABASE_CLOUD_SYNC_TIMEOUT_SECONDS,
    database_is_likely_cloud_placeholder,
    prefetch_database_file_with_timeout,
)
from qplot.datahandling.file_identity import (
    canonical_database_path,
    database_instance,
    database_instances_differ,
    database_publication_guard_path,
    logical_database_path,
)
from qplot.datahandling.readonly import quarantine_wal_for_replaced_database
from qplot.diagnostics import log_exception
from qplot.testdata import (
    GenerationCancelled,
    example_csv_contents,
    generate_database,
    instruction_collection_contents,
)

from ._config_persistence import persist_config_value
from ._export_paths import (
    choose_export_path,
    prepare_export_destination,
    write_export_atomically,
)
from ._file_manager import reveal_file_in_file_manager
from ._generation_dialog import GenerationProgress, GenerationReview

_TEST_DATABASE_READER_RELEASE_TIMEOUT_SECONDS = 10.0


def _database_publication_is_guarded(database_path):
    """Fail closed if publication is in flight or its guard cannot be inspected."""
    try:
        database_publication_guard_path(database_path).lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _write_export_bytes(filename, content):
    """Write one private staging file for the shared export transaction."""
    with open(filename, "wb") as output_file:
        output_file.write(content)
    return True


class TestDatabaseGenerationSignals(QtCore.QObject):
    """Signals emitted by background test-database generation."""

    status = QtCore.pyqtSignal(str)
    run_started = QtCore.pyqtSignal(str, int, int)
    finished = QtCore.pyqtSignal(str, object, object, bool)


class TestDatabaseGenerationWorker(QtCore.QRunnable):
    """Generate a test database without blocking the GUI thread."""

    def __init__(
        self,
        specifications,
        database_path,
        overwrite=False,
        *,
        read_services=(),
        cloud_sync_timeout=DATABASE_CLOUD_SYNC_TIMEOUT_SECONDS,
    ):
        super().__init__()
        self.signals = TestDatabaseGenerationSignals()
        self.specifications = list(specifications)
        self.database_path = str(database_path)
        self.overwrite = overwrite
        self._read_services = tuple(read_services)
        self.cloud_sync_timeout = cloud_sync_timeout
        self._cancelled = threading.Event()
        self._publication_completed = threading.Event()

    def cancel(self):
        """Request cooperative cancellation and temporary-file cleanup."""
        self._cancelled.set()

    def run(self):
        error: Exception | None = None
        try:
            self._wait_for_read_services()
            self._prefetch_existing_output()
            generate_database(
                self.specifications,
                self.database_path,
                overwrite=self.overwrite,
                cancelled_callback=self._cancelled.is_set,
                publication_callback=self._publication_completed.set,
                run_started_callback=self.signals.run_started.emit,
            )
        except GenerationCancelled as err:
            error = err
        except Exception as err:
            error = err
            log_exception("Test database generation failed", err, __name__)

        try:
            self.signals.finished.emit(
                self.database_path,
                self.specifications,
                error,
                self._publication_completed.is_set(),
            )
        except RuntimeError as err:
            if "wrapped C/C++ object" not in str(err):
                raise

    def _prefetch_existing_output(self):
        """Materialise an online-only replacement target using read-only I/O."""
        if self._cancelled.is_set():
            raise GenerationCancelled("Test-database generation was cancelled")
        if not self.overwrite or not database_is_likely_cloud_placeholder(
            self.database_path
        ):
            return
        path = Path(self.database_path)
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            return  # The generator rejects special files without opening them.
        try:
            prefetch_database_file_with_timeout(
                self.database_path,
                timeout=self.cloud_sync_timeout,
                status_callback=self.signals.status.emit,
                cancelled_callback=self._cancelled.is_set,
            )
        except InterruptedError as error:
            raise GenerationCancelled(
                "Test-database generation was cancelled"
            ) from error
        after = path.lstat()
        # Hydration may change ctime and cloud flags, but must not silently
        # redirect overwrite consent to another file or another data version.
        fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in fields):
            raise RuntimeError(
                "The existing output database changed while downloading from cloud "
                "storage. No replacement was generated. Retry or choose another output path."
            )
        self.signals.status.emit(f"Generating test database {path.name}...")

    def _wait_for_read_services(self):
        """Release qPlot's old file handles before generating their replacement."""
        deadline = monotonic() + _TEST_DATABASE_READER_RELEASE_TIMEOUT_SECONDS
        for service in self._read_services:
            while True:
                if self._cancelled.is_set():
                    raise GenerationCancelled("Test-database generation was cancelled")
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        "qPlot's database reader did not finish closing before "
                        "test-database generation. The output database was not changed."
                    )
                if service.wait_closed(min(0.1, remaining)):
                    break


class SyntheticDatabaseActionsMixin:
    """User-requested synthetic generation composed into the main window."""

    _test_database_generation_worker: TestDatabaseGenerationWorker | None

    @QtCore.pyqtSlot()
    def open_bundled_test_database_csv_folder(self):
        """Open the installed CSV templates for inspection in the file manager."""
        try:
            directory = os.fspath(
                resources.files("qplot").joinpath("resources", "testdata")
            )
            if not os.path.isdir(directory):
                raise FileNotFoundError(directory)
            if not QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(directory)):
                raise OSError("The file manager could not open the bundled CSV folder.")
        except Exception as error:
            log_exception(
                "Opening bundled test-data CSV folder failed", error, __name__
            )
            self.show_error(
                "Open Bundled CSV Folder Failed",
                "Could not open the bundled CSV folder. Use Export CSV Collection "
                "to copy the examples to a folder of your choice.",
                str(error),
            )
            return False
        self.show_status(f"Opened bundled CSV folder: {directory}", 5000)
        return True

    def test_database_csv_directory(self):
        """Use the last CSV folder, falling back when it is no longer available."""
        config = getattr(self, "config", None)
        if config is not None:
            directory = config.get("file.last_testdata_csv_directory")
            if directory and os.path.isdir(directory):
                return directory
        return self.database_open_directory()

    def _remember_test_database_csv_directory(self, directory):
        config = getattr(self, "config", None)
        if config is None:
            return
        directory = os.path.abspath(directory)
        if config.get("file.last_testdata_csv_directory") != directory:
            persist_config_value(
                self,
                config,
                "file.last_testdata_csv_directory",
                directory,
                "the last test-data CSV folder",
            )

    @QtCore.pyqtSlot()
    def create_test_database_csv(self):
        """Create an example test-data CSV and open its folder."""
        suggested_path = os.path.join(
            self.test_database_csv_directory(),
            "qplot-test-runs.csv",
        )
        try:
            destination = choose_export_path(
                self,
                caption="Create Test Database CSV",
                suggested_path=suggested_path,
                name_filter="CSV Files (*.csv)",
                required_suffix=".csv",
                replace_title="Replace Example CSV?",
                file_description="example CSV file",
            )
            if destination is None:
                self.show_status("Example CSV creation cancelled.", 3000)
                return False
            write_export_atomically(
                destination,
                lambda temporary: _write_export_bytes(
                    temporary, example_csv_contents()
                ),
            )
        except Exception as err:
            log_exception("Example test-data CSV creation failed", err, __name__)
            self.show_error(
                "Example CSV Creation Failed",
                "Could not create the example test-database CSV.",
                str(err),
            )
            return False

        csv_path = destination.filename
        self._remember_test_database_csv_directory(os.path.dirname(csv_path))
        opened = reveal_file_in_file_manager(csv_path)
        if not opened:
            self.show_error(
                "Open CSV Folder Failed",
                "The example CSV was created, but its folder could not be opened.",
                csv_path,
            )
            return True

        self.show_status(f"Created example CSV and opened its folder: {csv_path}", 5000)
        return True

    @QtCore.pyqtSlot()
    def export_test_database_csv_collection(self):
        """Export the installed cumulative CSV collection and open its folder."""
        directory = qtw.QFileDialog.getExistingDirectory(
            self,
            "Export Test Database CSV Collection",
            self.test_database_csv_directory(),
        )
        if not directory:
            self.show_status("CSV collection export cancelled.", 3000)
            return False

        directory = os.path.abspath(directory)
        try:
            collection = instruction_collection_contents()
            destinations = tuple(
                prepare_export_destination(
                    self,
                    os.path.join(directory, filename),
                )
                for filename, _content in collection
            )
            for destination, (_filename, content) in zip(
                destinations,
                collection,
                strict=True,
            ):
                write_export_atomically(
                    destination,
                    lambda temporary, data=content: _write_export_bytes(
                        temporary, data
                    ),
                )
            output_paths = tuple(
                Path(destination.filename) for destination in destinations
            )
        except Exception as err:
            log_exception("Test-data CSV collection export failed", err, __name__)
            self.show_error(
                "CSV Collection Export Failed",
                "Could not export the test-database CSV collection.",
                str(err),
            )
            return False

        self._remember_test_database_csv_directory(directory)
        opened = reveal_file_in_file_manager(output_paths[0])
        if not opened:
            self.show_error(
                "Open CSV Folder Failed",
                "The CSV collection was exported, but its folder could not be opened.",
                directory,
            )
            return True

        self.show_status(
            f"Exported {len(output_paths)} instruction CSV files and opened their "
            f"folder: {directory}",
            5000,
        )
        return True

    @QtCore.pyqtSlot()
    def generate_test_database_from_csv(self):
        """Choose a CSV and generate its QCoDeS database in the background."""
        if getattr(self, "_database_load_publication_active", False):
            self.show_status(
                "Wait for the current database view to finish loading before "
                "generating a test database.",
                5000,
            )
            return False
        if getattr(self, "_test_database_generation_active", False):
            self.show_status("A test database is already being generated.", 5000)
            return False
        if getattr(self, "_database_load_active", False) or getattr(
            self, "_database_view_released_for_generation", False
        ):
            self.show_status(
                "Wait for the current database recovery to finish.",
                5000,
            )
            return False

        review = GenerationReview(self)
        try:
            if review.exec() != qtw.QDialog.DialogCode.Accepted:
                self.show_status("Test database generation cancelled.", 3000)
                return False
            specifications = review.specifications
            database_path = review.database_path
            overwrite = review.overwrite
        finally:
            review.deleteLater()
        if (
            getattr(self, "_shutdown_started", False)
            or getattr(self, "_database_load_active", False)
            or getattr(self, "_test_database_replacement_state", None) is not None
            or getattr(self, "_test_database_generation_active", False)
        ):
            self.show_status("The database view changed. Review generation again after loading finishes.", 5000)
            return False

        try:
            self._start_test_database_generation(
                specifications, database_path, overwrite
            )
        except Exception as error:
            # Construction, signal wiring and pool submission all precede a
            # worker-owned outcome. Recover through the same terminal path as
            # a failed generation so a released source cannot stay stranded.
            worker = getattr(self, "_test_database_generation_worker", None)
            if worker is not None:
                worker.cancel()
            log_exception("Test database generation could not start", error, __name__)
            self.test_database_generation_finished(
                database_path,
                specifications,
                error,
                False,
            )
            return False
        return True

    def _start_test_database_generation(self, specifications, database_path, overwrite):
        """Release matching readers and hand generation to the worker pool."""
        file_textbox = getattr(self, "fileTextbox", None)
        current_database = file_textbox.text() if file_textbox is not None else ""
        read_services = ()
        if current_database and canonical_database_path(
            current_database
        ) == canonical_database_path(database_path):
            # Capture the accepted source before releasing its handles. The
            # worker stages elsewhere, so this is only a reversible release:
            # the terminal callback alone may confirm replacement/quarantine.
            prepare_replacement = getattr(
                self,
                "_prepare_test_database_replacement",
                None,
            )
            if callable(prepare_replacement):
                prepare_replacement(database_path)
                resolved_path = canonical_database_path(database_path)
                read_services = tuple(
                    service
                    for service in getattr(self, "_retired_trusted_read_services", ())
                    if getattr(service, "database_instance", None) is not None
                    and service.database_instance.resolved_path == resolved_path
                )

        self._test_database_generation_active = True
        self._sync_database_generation_controls()
        worker = TestDatabaseGenerationWorker(
            specifications,
            database_path,
            overwrite=overwrite,
            read_services=read_services,
            cloud_sync_timeout=(
                self.config.get("runtime_settings.cloud_sync_timeout")
                if getattr(self, "config", None) is not None
                else DATABASE_CLOUD_SYNC_TIMEOUT_SECONDS
            ),
        )
        self._test_database_generation_worker = worker
        worker.signals.status.connect(self.test_database_generation_status)
        worker.signals.run_started.connect(self.test_database_generation_run_started)
        worker.signals.finished.connect(self.test_database_generation_finished)
        self.show_status(
            f"Generating test database {os.path.basename(database_path)}...",
            0,
        )
        previous = getattr(self, "_generation_progress", None)
        if previous is not None:
            previous.close()
        progress = GenerationProgress(self, database_path, worker)
        self._generation_progress = progress
        progress.finished.connect(lambda: setattr(self, "_generation_progress", None))
        progress.show()
        self.testDatabaseGenerationThreadPool.start(worker)

    @QtCore.pyqtSlot(str)
    def test_database_generation_status(self, message):
        if getattr(self, "_test_database_generation_active", False):
            self.show_status(message, 0)
            progress = getattr(self, "_generation_progress", None)
            if progress is not None and progress.dismiss.isEnabled():
                progress.status.setText(message)

    @QtCore.pyqtSlot(str, int, int)
    def test_database_generation_run_started(self, message, run_number, total_runs):
        self.test_database_generation_status(message)
        progress = getattr(self, "_generation_progress", None)
        if progress is not None and progress.worker is not None:
            progress.bar.setRange(0, total_runs)
            unit = "run" if total_runs == 1 else "runs"
            progress.bar.setFormat(f"%v of %m {unit} complete")
            progress.bar.setValue(run_number - 1)

    @QtCore.pyqtSlot(str, object, object, bool)
    def test_database_generation_finished(
        self,
        database_path,
        specifications,
        error,
        publication_completed=False,
    ):
        """Apply the result of background test-database generation."""
        self._test_database_generation_active = False
        self._test_database_generation_worker = None

        shutting_down = bool(
            getattr(self, "_shutdown_started", False)
            or getattr(self, "_shutdown_ready", False)
        )
        cancelled = isinstance(error, GenerationCancelled)
        progress = getattr(self, "_generation_progress", None)
        if progress is not None:
            outcome = "Database ready."
            if cancelled:
                outcome = "Generation cancelled."
            elif error is not None:
                outcome = "Database generation failed."
            progress.finish(outcome, error is None)
            if shutting_down:
                progress.close()
        if shutting_down:
            self._clear_test_database_generation_transaction()
            return

        if cancelled:
            self.show_status("Test database generation cancelled.", 3000)
        elif error is not None:
            self.show_error(
                "Test Database Generation Failed",
                "Could not generate the test database.",
                str(error),
            )

        if error is None:
            run_count = len(specifications)
            point_count = sum(
                specification.point_count for specification in specifications
            )
            run_word = "run" if run_count == 1 else "runs"
            self.show_status(
                f"Generated {os.path.basename(database_path)} with {run_count} "
                f"{run_word} and {point_count} points.",
                7000,
            )

        replacement_state = getattr(
            self,
            "_test_database_replacement_state",
            None,
        )
        state_matches = bool(
            replacement_state is not None
            and logical_database_path(replacement_state.database_path)
            == logical_database_path(database_path)
        )
        file_textbox = getattr(self, "fileTextbox", None)
        current_database = file_textbox.text() if file_textbox is not None else ""
        current_matches = bool(
            current_database
            and (
                self._database_generation_transaction_blocks_path(
                    current_database,
                )
                if state_matches
                else canonical_database_path(current_database)
                == canonical_database_path(database_path)
            )
        )
        pending_unrelated_load = self._pending_database_load_targets_another_path(
            database_path,
        )
        if not state_matches and not current_matches:
            self._sync_database_generation_controls()
            return

        instance_changed = False
        if state_matches:
            current_instance = database_instance(database_path)
            instance_changed = database_instances_differ(
                replacement_state.original_instance,
                current_instance,
            )

        replacement_confirmed = bool(
            error is None or publication_completed or instance_changed
        )
        guarded = _database_publication_is_guarded(database_path)
        if replacement_confirmed:
            # This transition is intentionally the first quarantine point.
            # It covers success as well as an exception raised after commit.
            quarantine_wal_for_replaced_database(database_path)
            if state_matches:
                replacement_state.outcome = "replacement"
            if guarded:
                if state_matches:
                    replacement_state.outcome = "ambiguous"
                self._sync_database_generation_controls()
                return
            if state_matches and (not current_matches or pending_unrelated_load):
                if not current_matches and not pending_unrelated_load:
                    self._clear_test_database_generation_transaction()
                else:
                    self._sync_database_generation_controls()
                return
            # The guards above returned for every unrelated current database.
            if state_matches:
                self._reload_replaced_database(
                    database_path,
                    generation_recovery=True,
                )
            else:
                self._reload_replaced_database(database_path)
            return

        if guarded:
            # The publisher retained a guard because it could not prove a safe
            # rollback. Keep the released view invalid and do not guess which
            # main any sidecar belongs to.
            if state_matches:
                replacement_state.outcome = "ambiguous"
            self._sync_database_generation_controls()
            return

        if state_matches:
            replacement_state.outcome = "recovering-original"
            if not current_matches and not pending_unrelated_load:
                self._clear_test_database_generation_transaction()
                return
            if pending_unrelated_load:
                self._sync_database_generation_controls()
                return
        # A rejected/cancelled publication left the accepted instance in
        # place. Force bypasses the ordinary same-file shortcut while the
        # released controller session is rebuilt from its read-only view.
        if state_matches:
            self.load_file(
                database_path,
                force=True,
                generation_recovery=True,
            )
        else:
            self.load_file(database_path, force=True)
