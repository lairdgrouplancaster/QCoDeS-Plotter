# -*- coding: utf-8 -*-
"""
Created on Sat Jul  5 17:03:14 2025

@author: Benjamin Wordsworth
"""

# from qplot.datahandling.dataset import init_and_load_by_id
# import os
# from qplot.datahandling import dataset
# from qcodes.dataset import initialise_or_create_database_at

import qplot
import sys
from PyQt5 import QtWidgets as qtw

# initialise_or_create_database_at(os.path.join(os.getcwd(), "WN7C_first cooldown.db"), journal_mode = None)

# ds = dataset.init_and_load_by_spec(
#     "C:/Users/Benjamin Wordsworth/.qcodes/code/WN7C_first cooldown.db",
#     captured_run_id=2
#     )

app = qtw.QApplication(sys.argv)
w = qplot.run()
# print(type(w))
w.show()
app.exec()