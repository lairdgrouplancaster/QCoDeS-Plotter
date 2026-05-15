import sys

from PyQt5 import QtWidgets as qtw

from qplot.diagnostics import (
    configure_logging,
    install_excepthook,
    log_event,
    log_exception,
    )
from qplot._version import package_version
from qplot.windows import MainWindow


def run(return_objects=False):
    """
    Entry point for opening the qplot app.

    Parameters
    ----------
    return_objects : bool, optional
        If true, returns the QApplication and MainWindow after the event loop
        exits. The default is false so the command-line entry point exits
        quietly and successfully.

    Returns
    -------
    tuple[PyQt5.QtWidgets.QApplication, qplot.windows.main.MainWindow] | None
        Returned only when return_objects is true.
        
    """
    configure_logging()
    install_excepthook()
    log_event("Starting qPlot %s", package_version())
    print("Initialising GUI, this may take a few seconds.\n")

    try:
        app = qtw.QApplication(sys.argv)
        w = MainWindow()
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
