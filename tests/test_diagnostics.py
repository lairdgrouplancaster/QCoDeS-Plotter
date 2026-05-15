import logging
import tempfile
import unittest
from pathlib import Path

from qplot import diagnostics


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

    def test_log_exception_writes_traceback(self):
        log_file = self.temp_path / "qplot.log"
        diagnostics.configure_logging(log_file=log_file, force=True)

        try:
            raise RuntimeError("broken measurement")
        except RuntimeError as error:
            diagnostics.log_exception("Refresh failed", error)

        text = log_file.read_text(encoding="utf-8")
        self.assertIn("Refresh failed: broken measurement", text)
        self.assertIn("Traceback", text)
        self.assertIn("RuntimeError: broken measurement", text)

    def test_user_visible_error_is_logged_with_details(self):
        log_file = self.temp_path / "qplot.log"
        diagnostics.configure_logging(log_file=log_file, force=True)

        diagnostics.log_user_error(
            "Database Load Failed",
            "Could not load database.",
            "locked database",
            )

        text = log_file.read_text(encoding="utf-8")
        self.assertIn("Database Load Failed: Could not load database.", text)
        self.assertIn("Details: locked database", text)

    def test_excepthook_logs_uncaught_exception(self):
        log_file = self.temp_path / "qplot.log"
        diagnostics.configure_logging(log_file=log_file, force=True)
        hook = diagnostics.install_excepthook(call_original=False)

        error = ValueError("uncaught value")
        hook(type(error), error, error.__traceback__)

        text = log_file.read_text(encoding="utf-8")
        self.assertIn("Uncaught exception", text)
        self.assertIn("ValueError: uncaught value", text)

    def test_failed_log_file_setup_is_non_fatal(self):
        logger = diagnostics.configure_logging(
            log_file=Path("\0") / "qplot.log",
            force=True,
            )

        self.assertTrue(
            any(isinstance(handler, logging.NullHandler) for handler in logger.handlers)
            )
