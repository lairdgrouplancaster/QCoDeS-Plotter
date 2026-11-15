import logging
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from qplot import diagnostics
from qplot.datahandling.database import _bounded_worker_error
from qplot.datahandling.readonly import (
    DatabaseInstanceChangedError,
    UnverifiableDatabaseWalError,
)


class DiagnosticsTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)

    def tearDown(self):
        diagnostics._reset_logging_for_tests()
        self.temp_dir.cleanup()

    def test_configure_logging_creates_log_file(self):
        log_file = self.temp_path / "logs" / "qplot.log"
        logger = diagnostics.configure_logging(log_file=log_file, force=True)

        logger.info("diagnostic smoke")
        for handler in logger.handlers:
            handler.flush()

        self.assertTrue(log_file.exists())
        self.assertIn("diagnostic smoke", log_file.read_text(encoding="utf-8"))

    def test_force_and_reset_preserve_caller_handlers(self):
        diagnostics._reset_logging_for_tests()
        existing_handlers = list(diagnostics.get_logger().handlers)
        first_path = self.temp_path / "first.log"
        second_path = self.temp_path / "second.log"
        caller_path = self.temp_path / "caller.log"
        logger = diagnostics.configure_logging(log_file=first_path, force=True)
        first_stream = next(
            handler.stream for handler in logger.handlers
            if isinstance(handler, logging.FileHandler) and Path(handler.baseFilename) == first_path
        )
        caller_handler = logging.FileHandler(caller_path, encoding="utf-8")
        caller_stream = caller_handler.stream
        caller_null_handler = logging.NullHandler()
        logger.addHandler(caller_handler)
        logger.addHandler(caller_null_handler)
        try:
            logger.info("before replacement")
            diagnostics.configure_logging(log_file=second_path, force=True)
            self.assertTrue(first_stream.closed)
            second_stream = next(
                handler.stream for handler in logger.handlers
                if isinstance(handler, logging.FileHandler)
                and Path(handler.baseFilename) == second_path
            )
            logger.info("after replacement")
            diagnostics._reset_logging_for_tests()
            logger.info("after reset")

            self.assertEqual(
                logger.handlers, [*existing_handlers, caller_handler, caller_null_handler],
            )
            self.assertFalse(caller_stream.closed)
            self.assertTrue(second_stream.closed)
            self.assertIn("before replacement", first_path.read_text())
            self.assertNotIn("after replacement", first_path.read_text())
            self.assertIn("after replacement", second_path.read_text())
            self.assertNotIn("after reset", second_path.read_text())
            self.assertIn("after reset", caller_path.read_text())
        finally:
            logger.removeHandler(caller_handler)
            logger.removeHandler(caller_null_handler)
            caller_handler.close()
            caller_null_handler.close()

    def test_repeated_setup_reuses_the_configured_destination(self):
        log_file = self.temp_path / "logs" / "qplot.log"
        logger = diagnostics.configure_logging(log_file=log_file, force=True)
        default_file = self.temp_path / "default.log"
        with patch.object(diagnostics, "default_log_file", return_value=default_file):
            diagnostics.configure_logging()
        diagnostics.configure_logging(log_file=log_file.parent / ".." / "logs" / "qplot.log")
        logger.info("exactly once")

        self.assertEqual(log_file.read_text().count("exactly once"), 1)
        self.assertFalse(default_file.exists())

    def test_failed_setup_is_retained_until_explicit_reconfiguration(self):
        diagnostics._reset_logging_for_tests()
        existing_handlers = tuple(diagnostics.get_logger().handlers)
        logger = diagnostics.configure_logging(log_file=Path("\0"), force=True)
        configured_handlers = tuple(logger.handlers)
        fallback_handlers = tuple(
            handler for handler in logger.handlers if handler not in existing_handlers
        )
        recovery_file = self.temp_path / "recovery.log"
        with patch.object(diagnostics, "default_log_file", return_value=recovery_file):
            diagnostics.log_event("still unavailable")
            self.assertEqual(tuple(logger.handlers), configured_handlers)
            self.assertFalse(recovery_file.exists())

            diagnostics.configure_logging(force=True)
            logger.info("recovered")
        self.assertEqual(recovery_file.read_text().count("recovered"), 1)
        self.assertTrue(all(handler not in logger.handlers for handler in fallback_handlers))

    def test_log_exception_writes_traceback(self):
        log_file = self.temp_path / "qplot.log"
        diagnostics.configure_logging(log_file=log_file, force=True)

        try:
            raise RuntimeError("broken measurement")
        except RuntimeError as error:
            diagnostics.log_exception("Refresh failed", error)

        text = log_file.read_text(encoding="utf-8")
        self.assertIn("Refresh failed: RuntimeError", text)
        self.assertIn("Traceback", text)
        self.assertIn("test_log_exception_writes_traceback", text)
        self.assertNotIn("broken measurement", text)

    def test_exception_logging_omits_sensitive_values_on_every_entry_point(self):
        log_file = self.temp_path / "qplot.log"
        logger = diagnostics.configure_logging(log_file=log_file, force=True)
        hook = diagnostics.install_excepthook(call_original=False)

        for entry in ("explicit", "implicit", "direct", "hook"):
            with self.subTest(entry=entry):
                try:
                    try:
                        raise ValueError("CAUSE_SECRET")
                    except ValueError as cause:
                        raise RuntimeError("MESSAGE_SECRET") from cause
                except RuntimeError as error:
                    error.add_note("NOTE_SECRET")
                    private_local = "LOCAL_SECRET"
                    if entry == "explicit":
                        diagnostics.log_exception("Refresh failed", error)
                    elif entry == "implicit":
                        diagnostics.log_exception("Refresh failed")
                    elif entry == "direct":
                        logger.exception("Refresh failed")
                    else:
                        hook(type(error), error, error.__traceback__)
                    self.assertEqual(private_local, "LOCAL_SECRET")

        text = log_file.read_text(encoding="utf-8")
        self.assertEqual(text.count("RuntimeError:"), 4)
        self.assertEqual(text.count("Traceback (most recent call last):"), 4)
        self.assertIn("test_exception_logging_omits_sensitive_values", text)
        for secret in ("CAUSE_SECRET", "MESSAGE_SECRET", "NOTE_SECRET", "LOCAL_SECRET"):
            self.assertNotIn(secret, text)
        # Default traceback formatting would also print the raise source line.
        self.assertNotIn("raise RuntimeError", text)

    def test_exception_logging_does_not_render_exception_or_worker_objects(self):
        class PrivateError(Exception):
            def __str__(self):
                raise AssertionError("must not render exception")

        class PrivateWorkerText(str):
            def __str__(self):
                raise AssertionError("must not render worker text")

        log_file = self.temp_path / "qplot.log"
        diagnostics.configure_logging(log_file=log_file, force=True)
        diagnostics.log_exception("Refresh failed", PrivateError())
        diagnostics.log_exception("Worker failed", PrivateWorkerText("WORKER_SECRET"))
        diagnostics.log_exception("No exception")
        text = log_file.read_text(encoding="utf-8")
        self.assertIn("Refresh failed: PrivateError", text)
        self.assertIn("Worker error text omitted", text)
        self.assertIn("No active exception", text)
        self.assertNotIn("WORKER_SECRET", text)

    def test_exception_group_does_not_render_nested_values(self):
        log_file = self.temp_path / "qplot.log"
        diagnostics.configure_logging(log_file=log_file, force=True)
        try:
            raise ExceptionGroup("GROUP_SECRET", [ValueError("NESTED_SECRET")])
        except ExceptionGroup as error:
            diagnostics.log_exception("Grouped failure", error)
        text = log_file.read_text(encoding="utf-8")
        self.assertIn("Grouped failure: ExceptionGroup", text)
        self.assertIn("Traceback", text)
        self.assertNotIn("GROUP_SECRET", text)
        self.assertNotIn("NESTED_SECRET", text)

    def test_persistent_log_ignores_traceback_cached_by_another_handler(self):
        log_file = self.temp_path / "qplot.log"
        logger = diagnostics.configure_logging(log_file=log_file, force=True)
        stream = StringIO()
        other_handler = logging.StreamHandler(stream)
        other_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.handlers.insert(0, other_handler)
        try:
            try:
                raise ValueError("CACHED_SECRET")
            except ValueError:
                logger.exception("Operation failed")
        finally:
            logger.removeHandler(other_handler)
            other_handler.close()
        self.assertIn("CACHED_SECRET", stream.getvalue())
        text = log_file.read_text(encoding="utf-8")
        self.assertIn("ValueError: exception details omitted", text)
        self.assertNotIn("CACHED_SECRET", text)

    def test_user_visible_error_omits_sensitive_details_from_log(self):
        log_file = self.temp_path / "qplot.log"
        diagnostics.configure_logging(log_file=log_file, force=True)

        diagnostics.log_user_error(
            "Database Load Failed",
            "Could not load database.",
            "locked database",
            )

        text = log_file.read_text(encoding="utf-8")
        self.assertIn("Database Load Failed: Could not load database.", text)
        self.assertIn("Details: omitted from persistent log", text)
        self.assertNotIn("locked database", text)

    def test_log_exception_accepts_bounded_worker_payloads_without_tracebacks(self):
        log_file = self.temp_path / "qplot.log"
        diagnostics.configure_logging(log_file=log_file, force=True)

        for error_type in (
            RuntimeError, DatabaseInstanceChangedError, UnverifiableDatabaseWalError,
        ):
            with self.subTest(error_type=error_type):
                try:
                    raise error_type("database is locked") from ValueError("private context")
                except error_type as error:
                    payload = _bounded_worker_error(error)
                if error_type is RuntimeError:
                    self.assertIsInstance(payload, str)
                else:
                    self.assertIsInstance(payload, error_type)
                    self.assertIsNone(payload.__traceback__)
                    self.assertIsNone(payload.__cause__)
                    self.assertIsNone(payload.__context__)
                # Text must not pick up an unrelated active exception either.
                try:
                    raise ValueError("unrelated active exception")
                except ValueError:
                    diagnostics.log_exception("Worker failed", payload)

        text = log_file.read_text(encoding="utf-8")
        self.assertEqual(text.count("Worker failed:"), 3)
        self.assertIn("Worker error text omitted", text)
        self.assertIn("DatabaseInstanceChangedError", text)
        self.assertIn("UnverifiableDatabaseWalError", text)
        self.assertNotIn("database is locked", text)
        self.assertNotIn("Traceback", text)
        self.assertNotIn("private context", text)
        self.assertNotIn("unrelated active exception", text)

    def test_bounded_shutdown_diagnostics_are_persisted_exactly(self):
        log_file = self.temp_path / "qplot.log"
        diagnostics.configure_logging(log_file=log_file, force=True)

        diagnostics.log_bounded_shutdown(
            "Bounded Application Shutdown: deadline exhausted",
            "pool threadPool: active_threads=1\nexact escalation diagnostic",
        )

        text = log_file.read_text(encoding="utf-8")
        self.assertIn("Bounded Application Shutdown: deadline exhausted", text)
        self.assertIn("pool threadPool: active_threads=1", text)
        self.assertIn("exact escalation diagnostic", text)

    def test_excepthook_logs_uncaught_exception(self):
        log_file = self.temp_path / "qplot.log"
        diagnostics.configure_logging(log_file=log_file, force=True)
        hook = diagnostics.install_excepthook(call_original=False)

        error = ValueError("uncaught value")
        hook(type(error), error, error.__traceback__)

        text = log_file.read_text(encoding="utf-8")
        self.assertIn("Uncaught exception", text)
        self.assertIn("ValueError: exception details omitted", text)
        self.assertNotIn("uncaught value", text)

    def test_reinstall_and_reset_restore_each_original_exception_hook(self):
        diagnostics._reset_logging_for_tests()
        first_original, next_original = Mock(), Mock()
        with patch.object(sys, "excepthook", first_original):
            diagnostics.configure_logging(log_file=self.temp_path / "qplot.log", force=True)
            diagnostics.install_excepthook()
            current = diagnostics.install_excepthook()
            error = ValueError("private exception value")
            current(type(error), error, error.__traceback__)
            first_original.assert_called_once_with(type(error), error, error.__traceback__)

            diagnostics._reset_logging_for_tests()
            self.assertIs(sys.excepthook, first_original)
            sys.excepthook = next_original
            diagnostics.install_excepthook(call_original=False)
            diagnostics._reset_logging_for_tests()
            self.assertIs(sys.excepthook, next_original)

    def test_reset_without_an_installed_hook_preserves_the_callers_hook(self):
        diagnostics._reset_logging_for_tests()
        original = Mock()
        with patch.object(sys, "excepthook", original):
            diagnostics._reset_logging_for_tests()
            self.assertIs(sys.excepthook, original)

    def test_failed_log_file_setup_is_non_fatal(self):
        logger = diagnostics.configure_logging(
            log_file=Path("\0") / "qplot.log",
            force=True,
            )

        self.assertTrue(
            any(isinstance(handler, logging.NullHandler) for handler in logger.handlers)
            )
