from PyQt5 import (
    QtWidgets as qtw,
    QtCore
    )

from qplot.datahandling import (
    get_runs_via_sql,
    has_finished
    )

from qcodes.dataset.sqlite.database import get_DB_location

from os.path import isfile

from datetime import datetime


class RunList(qtw.QTreeWidget):
    
    cols = ['Run ID', 'Experiment', 'Sample', 'Name', 'Started', 'Completed', 'GUID']

    selected = QtCore.pyqtSignal([str])
    plot = QtCore.pyqtSignal([str])
    
    def __init__(self, *args, initalize=False, **kargs):
        super().__init__(*args, **kargs)
        
        self.watching = []
        
        self.setColumnCount(len(self.cols))
        self.setHeaderLabels(self.cols)
        
        if isfile(get_DB_location()):
            self.setRuns()
            
        self.itemSelectionChanged.connect(self.onSelect)
        self.itemDoubleClicked.connect(self.doubleClicked)
        
    
    def addRuns(self, runs, track = False): #tbd
        self.setSortingEnabled(False)
        
        append = False
        self.maxTime = max([subDict["run_timestamp"] for subDict in runs.values()], default=0)
        
        for run_id, metadata in runs.items():
            arr = [str(run_id)] #run id
            
            run_time = datetime.fromtimestamp(metadata["run_timestamp"])
            
            arr.append(metadata["exp_name"]) #experiment
            arr.append(metadata["sample_name"]) #sample
            arr.append(metadata["name"]) #name
            arr.append(run_time.strftime("%Y-%m-%d %H:%M:%S")) #started
            try:
                assert metadata["completed_timestamp"] is not None
                arr.append(datetime.fromtimestamp(
                    metadata["completed_timestamp"], 
                    ).strftime("%Y-%m-%d %H:%M:%S")) #finished
            except AssertionError:
                arr.append("Ongoing")
                append = True
            arr.append(metadata["guid"]) #guid
        
            item = SortableTreeWidgetItem(arr)
            
            self.addTopLevelItem(item)
            if append:
                self.watching.append(item)
            
        self.setSortingEnabled(True)
        for i in range(len(self.cols)):
            self.resizeColumnToContents(i)
        
        
    def setRuns(self):
        self.clear()
        runs = get_runs_via_sql()
        
        self.addRuns(runs)      


    def checkWatching(self):

        to_remove = []
        for run in self.watching:

            finished = has_finished(run.guid)[0]

            if finished:
                run.setText(5, datetime.fromtimestamp(
                        finished,
                        ).strftime("%Y-%m-%d %H:%M:%S"))
                to_remove.append(run)
        
        for run in to_remove:
            self.watching.remove(run)        


    @QtCore.pyqtSlot()
    def onSelect(self):
        if len(self.selectedItems()) == 1:
            selection = self.selectedItems()[0].guid #emit guid
            self.selected.emit(selection)

    @QtCore.pyqtSlot(qtw.QTreeWidgetItem, int)
    def doubleClicked(self, item, column):
        self.plot.emit(None)
    
   
#3 classes/methods below are adapted from plottr
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
    
    @property
    def guid(self):
        return self.text(6)


class moreInfo(qtw.QTreeWidget):
    
    def __init__(self, *args):
        super().__init__(*args)
        
        self.setHeaderLabels(["Key", "Value"])
        self.setColumnCount(2)
        
    @QtCore.pyqtSlot(dict)
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
        
        

