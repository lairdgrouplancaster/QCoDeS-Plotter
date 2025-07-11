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

from qcodes.dataset.sqlite.database import get_DB_location

from os.path import isfile

import numpy as np


class RunList(qtw.QListWidget):
    
    selected = QtCore.pyqtSignal([list])
    
    def __init__(self, *args, initalize=False, **kargs):
        super().__init__(*args, **kargs)
        
        if isfile(get_DB_location()):
            runs = np.array(get_runs_from_db(), dtype=str)
            self.addItems(runs)
            
        self.itemSelectionChanged.connect(self.onSelect)
    
    
    def refresh(self):
        self.clear()
        
        runs = np.array(get_runs_from_db(), dtype=str)
        self.addItems(runs)

    
    
    @QtCore.pyqtSlot()
    def onSelect(self):
        selection = [item.text() for item in self.selectedItems()]
        self.selected.emit(selection)
        
        



#taken from plottr
class moreInfo(qtw.QTreeWidget):
    
    def __init__(self, *args):
        super().__init__(*args)
        
        self.setHeaderLabels(["Key", "Value"])
        self.setColumnCount(2)
        
    # @QtCore.pyqtSlot(dict)
    def setInfo(self, info):
        self.clear()
        
        items = dictToTree(info)
        for item in items:
            self.addTopLevelItem(item)
            item.setExpanded(True)
            
        self.expandAll()
        for i in range(2):
            self.resizeColumnToContents(i)



def dictToTree(d : dict):
    items = []
    for k, v in d.items():
        if not isinstance(v, dict):
            item = qtw.QTreeWidgetItem([str(k), str(v)])
        else:
            item = qtw.QTreeWidgetItem([k, ''])
            for child in dictToTree(v):
                item.addChild(child)
        items.append(item)
    return items
        
        
        
