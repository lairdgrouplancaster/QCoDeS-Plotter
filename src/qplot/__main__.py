from qplot.windows import MainWindow

import sys

from PyQt5 import QtWidgets as qtw

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
    print("Initialising GUI, this may take a few seconds.\n")
    
    app = qtw.QApplication(sys.argv)
    w = MainWindow()
    app.exec()

    if return_objects:
        return app, w
    return None

if __name__=="__main__":
    run()
