import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "qplot"
LOG_FILE_NAME = "qplot.log"
LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
DEFAULT_MAX_BYTES = 1_000_000
DEFAULT_BACKUP_COUNT = 3

_ORIGINAL_EXCEPTHOOK = None
_EXCEPTHOOK_INSTALLED = False


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

    formatter = logging.Formatter(LOG_FORMAT)
    handler: logging.Handler
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
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
            "Uncaught exception",
            exc_info=(exc_type, exc_value, traceback),
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
    Logs an exception with traceback details.

    """
    configure_logging()
    logger = get_logger(logger_name)
    if error is None:
        logger.exception(context)
    else:
        logger.error(
            "%s: %s",
            context,
            error,
            exc_info=(type(error), error, error.__traceback__),
            )


def log_user_error(title, message, details=None, logger_name=None):
    """
    Logs a user-visible error dialog.

    """
    configure_logging()
    logger = get_logger(logger_name)
    if details:
        logger.error("%s: %s\nDetails: %s", title, message, details)
    else:
        logger.error("%s: %s", title, message)


def _has_handler_for(logger, target):
    try:
        target = target.resolve()
    except OSError:
        target = target.absolute()

    for handler in logger.handlers:
        if not getattr(handler, "_qplot_owned", False):
            continue
        if not hasattr(handler, "baseFilename"):
            continue
        try:
            if Path(handler.baseFilename).resolve() == target:
                return True
        except OSError:
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
