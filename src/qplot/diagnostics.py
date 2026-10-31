import logging
import os
import sys
from copy import copy
from io import BufferedWriter
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import cast

from qplot._auxiliary_paths import ensure_safe_auxiliary_path

LOGGER_NAME = "qplot"
LOG_FILE_NAME = "qplot.log"
LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
DEFAULT_MAX_BYTES = 1_000_000
DEFAULT_BACKUP_COUNT = 3

_ORIGINAL_EXCEPTHOOK = None
_EXCEPTHOOK_INSTALLED = False


def _exception_diagnostic(exc_type, traceback):
    """Describe an exception without its values, source text, notes, or chain."""
    if exc_type is None:
        return "No active exception"
    lines = [f"{exc_type.__name__}: exception details omitted from persistent log"]
    if traceback is not None:
        lines.append("Traceback (most recent call last):")
        while traceback is not None:
            code = traceback.tb_frame.f_code
            lines.append(
                f'  File "{code.co_filename}", line {traceback.tb_lineno}, '
                f"in {code.co_name}",
            )
            traceback = traceback.tb_next
    return "\n".join(lines)


class _PrivateExceptionFormatter(logging.Formatter):
    def format(self, record):
        # Another handler may already have cached an unfiltered traceback.
        # Keep our policy independent without changing that handler's record.
        private_record = copy(record)
        private_record.exc_text = None
        return super().format(private_record)

    def formatException(self, exc_info):
        # Direct logger.exception() calls must follow the same privacy policy.
        return _exception_diagnostic(exc_info[0], exc_info[2])


class _DatabaseSafeRotatingFileHandler(RotatingFileHandler):
    """Prevent logging and log rotation from touching measurement files."""

    def _open(self):
        ensure_safe_auxiliary_path(self.baseFilename)
        return super()._open()

    def _discard_stream(self):
        stream, self.stream = self.stream, None
        if stream is not None:
            # FileHandler opens a buffered text stream. Closing its raw file
            # first discards any pending text without flushing it into a file
            # which may now belong to a QCoDeS database.
            cast(BufferedWriter, stream.buffer).raw.close()

    def _prepare_stream(self):
        try:
            ensure_safe_auxiliary_path(self.baseFilename)
            if self.stream is not None:
                try:
                    current = os.path.samestat(
                        os.stat(self.baseFilename), os.fstat(self.stream.fileno()),
                    )
                except FileNotFoundError:
                    current = False
                if not current:
                    self._discard_stream()
        except (OSError, ValueError):
            self._discard_stream()
            return False
        return True

    def emit(self, record):
        if not self._prepare_stream():
            return
        super().emit(record)

    def flush(self):
        self.acquire()
        try:
            # FileHandler.close() and logging.shutdown() call flush too.
            if self.stream is not None and self._prepare_stream():
                super().flush()
        finally:
            self.release()

    def doRollover(self):
        if not self._prepare_stream():
            return
        try:
            for index in range(1, self.backupCount + 1):
                ensure_safe_auxiliary_path(self.rotation_filename(
                    f"{self.baseFilename}.{index}",
                ))
        except OSError:
            return
        super().doRollover()


def default_log_file():
    """
    Returns qPlot's default log file path.

    """
    return Path.home() / ".qplot" / LOG_FILE_NAME


def get_logger(name=None):
    """
    Returns qPlot's base logger or a child logger.

    """
    if not name:
        return logging.getLogger(LOGGER_NAME)
    if name == LOGGER_NAME or str(name).startswith(f"{LOGGER_NAME}."):
        return logging.getLogger(str(name))
    return logging.getLogger(f"{LOGGER_NAME}.{name}")


def configure_logging(
        log_file=None,
        level=logging.INFO,
        force=False,
        max_bytes=DEFAULT_MAX_BYTES,
        backup_count=DEFAULT_BACKUP_COUNT,
        ):
    """
    Configures qPlot's file logger.

    Logging failures are intentionally non-fatal: diagnostics should never stop
    the GUI from starting.

    """
    logger = get_logger()
    logger.setLevel(level)
    logger.propagate = False

    target = Path(log_file) if log_file is not None else default_log_file()

    if force:
        _remove_owned_handlers(logger)
    elif log_file is None and _has_owned_handler(logger):
        return logger
    elif _has_handler_for(logger, target):
        return logger

    formatter = _PrivateExceptionFormatter(LOG_FORMAT)
    handler: logging.Handler
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handler = _DatabaseSafeRotatingFileHandler(
            target,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            )
    except (OSError, ValueError):
        handler = logging.NullHandler()

    handler.setLevel(level)
    handler.setFormatter(formatter)
    handler.__dict__["_qplot_owned"] = True
    logger.addHandler(handler)
    return logger


def install_excepthook(call_original=True):
    """
    Installs a process-wide exception hook that writes uncaught exceptions.

    """
    global _ORIGINAL_EXCEPTHOOK, _EXCEPTHOOK_INSTALLED

    configure_logging()
    if _ORIGINAL_EXCEPTHOOK is None:
        _ORIGINAL_EXCEPTHOOK = sys.excepthook

    def hook(exc_type, exc_value, traceback):
        get_logger().critical(
            "Uncaught exception: %s",
            _exception_diagnostic(exc_type, traceback),
            )
        if call_original and _ORIGINAL_EXCEPTHOOK is not None:
            _ORIGINAL_EXCEPTHOOK(exc_type, exc_value, traceback)

    sys.excepthook = hook
    _EXCEPTHOOK_INSTALLED = True
    return hook


def log_event(message, *args, level=logging.INFO, logger_name=None):
    """
    Logs a diagnostic event after ensuring logging is configured.

    """
    configure_logging()
    get_logger(logger_name).log(level, message, *args)


def log_exception(context, error=None, logger_name=None):
    """
    Logs operation context, exception type, and stack locations only.

    Exception messages, chains, notes, source lines, and worker text can contain
    sensitive data. Bounded worker text is safe for Qt transport, but is not
    necessarily safe to persist.

    """
    configure_logging()
    logger = get_logger(logger_name)
    if error is None:
        exc_type, _, traceback = sys.exc_info()
        diagnostic = _exception_diagnostic(exc_type, traceback)
    elif isinstance(error, BaseException):
        diagnostic = _exception_diagnostic(type(error), error.__traceback__)
    else:
        diagnostic = "Worker error text omitted from persistent log"
    logger.error("%s: %s", context, diagnostic)


def log_user_error(title, message, details=None, logger_name=None):
    """
    Logs the summary of a user-visible error dialog.

    Dialog details can contain database values, connection strings, or other
    measurement metadata.  They remain available in the on-screen dialog but
    are deliberately excluded from the persistent clear-text log.

    """
    configure_logging()
    logger = get_logger(logger_name)
    if details:
        logger.error("%s: %s\nDetails: omitted from persistent log", title, message)
    else:
        logger.error("%s: %s", title, message)


def log_bounded_shutdown(message, diagnostics, logger_name=None):
    """
    Persists qPlot's internal bounded-shutdown resource snapshot exactly.

    Unlike user-dialog details, this payload is generated solely from qPlot's
    process and resource-liveness accounting and is required for post-mortem
    shutdown diagnosis.

    """
    configure_logging()
    get_logger(logger_name).error("%s\n%s", message, diagnostics)


def _has_handler_for(logger, target):
    try:
        target = target.resolve()
    except (OSError, RuntimeError):
        # Python 3.11/3.12 report symlink loops as RuntimeError. Let the
        # guarded file open refuse the path without making logging fatal.
        target = target.absolute()

    for handler in logger.handlers:
        if not getattr(handler, "_qplot_owned", False):
            continue
        if not hasattr(handler, "baseFilename"):
            continue
        try:
            if Path(handler.baseFilename).resolve() == target:
                return True
        except (OSError, RuntimeError):
            continue
    return False


def _has_owned_handler(logger):
    return any(getattr(handler, "_qplot_owned", False) for handler in logger.handlers)


def _remove_owned_handlers(logger):
    for handler in list(logger.handlers):
        if not getattr(handler, "_qplot_owned", False):
            continue
        logger.removeHandler(handler)
        handler.close()


def _reset_logging_for_tests():
    """
    Resets qPlot diagnostics globals for isolated unit tests.

    """
    global _ORIGINAL_EXCEPTHOOK, _EXCEPTHOOK_INSTALLED

    _remove_owned_handlers(get_logger())
    if _EXCEPTHOOK_INSTALLED and _ORIGINAL_EXCEPTHOOK is not None:
        sys.excepthook = _ORIGINAL_EXCEPTHOOK
    _ORIGINAL_EXCEPTHOOK = None
    _EXCEPTHOOK_INSTALLED = False
