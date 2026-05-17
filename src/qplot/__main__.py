import sys
from pathlib import Path

from PyQt6 import QtWidgets as qtw

from qplot._version import package_version
from qplot.diagnostics import (
    configure_logging,
    install_excepthook,
    log_event,
    log_exception,
)
from qplot.windows import MainWindow


def _database_path_from_arguments(args):
    """
    Return the first database path passed on the command line.

    File managers pass the double-clicked file as a plain positional argument.
    Qt options are ignored here so they can still be handled by QApplication.

    """
    for arg in args:
        if arg.startswith("-"):
            continue

        if Path(arg).suffix.lower() == ".db":
            return arg

    return None


def _configure_application_identity(app):
    """
    Sets the application name used by native desktop menus.

    """
    app.setApplicationName("qPlot")
    if hasattr(app, "setApplicationDisplayName"):
        app.setApplicationDisplayName("qPlot")


def run(return_objects=False, database_path=None):
    """
    Entry point for opening the qplot app.

    Parameters
    ----------
    return_objects : bool, optional
        If true, returns the QApplication and MainWindow after the event loop
        exits. The default is false so the command-line entry point exits
        quietly and successfully.
    database_path : str, optional
        QCoDeS database path to load after the main window opens. When omitted,
        qPlot uses the first `.db` path passed on the command line, if any.

    Returns
    -------
    tuple[PyQt6.QtWidgets.QApplication, qplot.windows.main.MainWindow] | None
        Returned only when return_objects is true.
        
    """
    configure_logging()
    install_excepthook()
    log_event("Starting qPlot %s", package_version())
    print("Initialising GUI, this may take a few seconds.\n")

    try:
        app = qtw.QApplication(sys.argv)
        _configure_application_identity(app)
        if database_path is None:
            database_path = _database_path_from_arguments(sys.argv[1:])
        w = MainWindow(startup_database_path=database_path)
        exit_code = app.exec()
    except Exception as err:
        log_exception("qPlot startup failed", err)
        raise

    log_event("qPlot event loop exited with code %s", exit_code)

    if return_objects:
        return app, w
    return None

if __name__=="__main__":
    run()
