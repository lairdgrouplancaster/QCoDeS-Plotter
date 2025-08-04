from qplot.windows import MainWindow

from qcodes.dataset.sqlite.database import (
    get_DB_location,
    conn_from_dbpath_or_conn
    )

import sys

from os.path import isfile

from PyQt5 import QtWidgets as qtw

def run():
    print("Initialising GUI, this may take a few seconds.\n")
    
    # if isfile(get_DB_location()): #close conn is already open by mistake
    #     conn_from_dbpath_or_conn(None, get_DB_location()).close()
    
    app = qtw.QApplication(sys.argv)
    w = MainWindow()
    app.exec()
    
    return app, w

if __name__=="__main__":
    run()