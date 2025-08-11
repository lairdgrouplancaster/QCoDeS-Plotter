from qplot.windows import MainWindow

import sys

from PyQt5 import QtWidgets as qtw

def run():
    """
    Entry point for opening the qplot app.

    Returns
    -------
    app : PyQt5.QtWidgets.QApplication
        
    w : qplot.windows.main.MainWindow
        

    """
    print("Initialising GUI, this may take a few seconds.\n")
    
    app = qtw.QApplication(sys.argv)
    w = MainWindow()
    app.exec()
    
    return app, w

if __name__=="__main__":
    run()