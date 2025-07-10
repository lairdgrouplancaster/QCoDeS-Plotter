# -*- coding: utf-8 -*-
"""
Created on Thu Jul 10 11:16:24 2025

@author: Benjamin Wordsworth
"""
from PyQt5 import (
    QtWidgets as qtw,
    QtCore
    )

from ..datahandling import get_runs_from_db

import numpy as np

class MainList(qtw.QListWidget):
    
    selected = QtCore.pyqtSignal([list])
    
    def __init__(self, *args, **kargs):
        super().__init__(*args, **kargs)
        
        runs = np.array(get_runs_from_db(), dtype=str)
        self.addItems(runs)
        
        self.itemSelectionChanged.connect(self.onSelect)
        
    @QtCore.pyqtSlot()
    def onSelect(self):
        selection = [item.text() for item in self.selectedItems()]
        self.selected.emit(selection)