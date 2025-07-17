from qplot.windows import MainWindow

import sys
from PyQt5 import QtWidgets as qtw

def run():
    print("Initialising GUI, this may take a few seconds.\n")
    app = qtw.QApplication(sys.argv)
    w = MainWindow()
    app.exec()
    
    return app, w

if __name__=="__main__":
    run()