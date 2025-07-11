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


class RunList(qtw.QTreeWidget):
    
    
    cols = ['Run ID', 'Date','Experiment', 'Sample', 'Name', 'Started', 'Completed', 'GUID']

    selected = QtCore.pyqtSignal([int])
    
    def __init__(self, *args, initalize=False, **kargs):
        super().__init__(*args, **kargs)
        
        self.setColumnCount(len(self.cols))
        self.setHeaderLabels(self.cols)
        
        if isfile(get_DB_location()):
            # runs = np.array(list(get_runs_from_db().keys()), dtype=str)
            self.addRuns()
            
        self.itemSelectionChanged.connect(self.onSelect)
    
    
    def refresh(self):
        self.clear()
        
        # runs = np.array(list(get_runs_from_db().keys()), dtype=str)
        self.addRuns()

    def addRuns(self):
        runs = get_runs_from_db()
        self.setSortingEnabled(False)
        
        
        for run_id, exp in runs.items():
            arr = [str(run_id)]
            arr.append("") #Date
            arr.append(exp["name"])
            arr.append(exp["sample_name"])
            arr.append("")
            arr.append(str(exp["started_at"]))
            arr.append(str(exp["finished_at"]))
            arr.append("")
        
            item = SortableTreeWidgetItem(arr)
            
            self.addTopLevelItem(item)
            
            
            
        self.setSortingEnabled(True)
        for i in range(len(self.cols)):
            self.resizeColumnToContents(i)


    
    @QtCore.pyqtSlot()
    def onSelect(self):
        # print(self.selectedItems())
        selection = self.selectedItems()[0].text(0)
        # print(selection)
        self.selected.emit(int(selection))
        
        
class SortableTreeWidgetItem(qtw.QTreeWidgetItem):
    """
    QTreeWidgetItem with an overridden comparator that sorts numerical values
    as numbers instead of sorting them alphabetically.
    """
    def __init__(self, strings):
        super().__init__(strings)

    def __lt__(self, other: qtw.QTreeWidgetItem) -> bool:
        col = self.treeWidget().sortColumn()
        text1 = self.text(col)
        text2 = other.text(col)
        try:
            return float(text1) < float(text2)
        except ValueError:
            return text1 < text2    


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
        
        
        
