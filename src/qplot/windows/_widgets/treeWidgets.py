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

import numpy as np

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
        
        self.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.prepareMenu)
        
    
    def addRuns(self, runs, track = False): #tbd
        self.setSortingEnabled(False)
        
        append = False
        self.maxTime = max(np.array([subDict["run_timestamp"] for subDict in runs.values()], dtype=float), default=0)
        
        for run_id, metadata in runs.items():
            arr = [str(run_id)] #run id
            
            if not metadata["run_timestamp"]:
                continue
            run_time = datetime.fromtimestamp(metadata["run_timestamp"])
            
            arr.append(metadata["exp_name"]) #experiment
            arr.append(metadata["sample_name"]) #sample
            arr.append(metadata["name"]) #name
            arr.append(run_time.strftime("%Y-%m-%d %H:%M:%S")) #started
            if metadata["completed_timestamp"]:
                arr.append(datetime.fromtimestamp(
                    metadata["completed_timestamp"], 
                    ).strftime("%Y-%m-%d %H:%M:%S")) #finished
            else:
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
            
    
    @QtCore.pyqtSlot(QtCore.QPoint)
    def prepareMenu(self, pos):
        main = self.parentWidget().parent()
        
        menu = qtw.QMenu(self)
        
        open_menu = menu.addMenu("&Open")
        
        open_all = qtw.QAction("All", self)
        open_all.triggered.connect(lambda _,: main.openPlot())
        open_menu.addAction(open_all)
        
        open_menu.addSeparator()
        
        params = {param: param.depends_on_ for param in main.ds.get_parameters() if param.depends_on}
        
        for param in params.keys():
            
            open_win = qtw.QAction(f"{param.name}", self)
            open_win.triggered.connect(lambda _, param=param: main.openPlot(params=[param]))
            
            open_menu.addAction(open_win)
        
        add_menu = menu.addMenu("&Add _ to _")
        
        add_all = add_menu.addMenu("All")
        valid_wins = []
        
        add_menu.addSeparator()
        
        
        for param, depends_on in params.items():
            if len(depends_on) != 1:
                continue
            
            valid_actions = []
            for win in main.windows:
                if win.param.depends_on_ == depends_on:
                    win_action = qtw.QAction(f"{win.label}", self)
                    win_action.triggered.connect(
                        lambda _, win=win, param=param: self.add_plot(win, param)
                        )
                    
                    valid_actions.append(win_action)
                    
                    if win not in valid_wins:
                        all_action = qtw.QAction(f"{win.label}", self)
                        all_action.triggered.connect(
                            lambda _, win=win, param_dict=params: self.add_all(win, param_dict)
                            )
                        valid_wins.append(win)
                        
                        add_all.addAction(all_action)
                    
            if valid_actions:
                param_menu = add_menu.addMenu(f"{param.name}")
                param_menu.addActions(valid_actions)
            
        menu.exec_(self.mapToGlobal(pos))
    

    @QtCore.pyqtSlot()
    def onSelect(self):
        if len(self.selectedItems()) == 1:
            selection = self.selectedItems()[0].guid #emit guid
            self.selected.emit(selection)


    @QtCore.pyqtSlot(qtw.QTreeWidgetItem, int)
    def doubleClicked(self, item, column):
        self.plot.emit(None)
    
    
    def add_plot(self, target_win, param):
        main = self.parentWidget().parent()
        from_win = None
        close_later = False
        
        for win in main.windows:
            if win.ds.guid == self.selectedItems()[0].guid and win.param == param:
                if target_win == win:
                    print(f"Skip, {target_win.label}. Target and Source are the same.\n")
                    return
                from_win = win
                break
            
        if not from_win:
            x, y = main.x, main.y
            
            main.openPlot(params=[param], show=False)
            from_win = main.windows[-1]
            
            if from_win.ds.running:
                if not target_win.monitor.isActive():
                    target_win.monitorIntervalChanged(target_win.spinBox.value())
                    target_win.toolbarRef.show()
            else: 
                close_later = True
            
            main.x, main.y = x, y
            
        
        if target_win.option_boxes[-1].isEnabled():
            box = target_win.option_boxes[-1]
        else:
            target_win.add_option_box()
            box = target_win.option_boxes[-1]
        
        index = box.option_box.findText(from_win.label)
        box.option_box.setCurrentIndex(index)
        
        if close_later:
            from_win.close()
        
     
    def add_all(self, target_win, param_dict):
        for param, depends_on in param_dict.items():
            if depends_on == target_win.param.depends_on_:
                self.add_plot(target_win, param)
   
    
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
        


