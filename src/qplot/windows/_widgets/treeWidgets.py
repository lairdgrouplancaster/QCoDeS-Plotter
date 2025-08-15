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
    """
    A modified PyQt5.QtWidgets.QTreeWidget, formated as a list which displays
    all run_ids and other properties found in self.cols.
    
    All QTreeWidgetItem are converted to SortableTreeWidgetItem to allow the user to sort
    by any columns.
    
    """
    
    cols = ['Run ID', 'Experiment', 'Sample', 'Name', 'Started', 'Completed', 'GUID']

    selected = QtCore.pyqtSignal([str])
    plot = QtCore.pyqtSignal([str])
    
    def __init__(self, *args, initalize=False, **kargs):
        super().__init__(*args, **kargs)
        
        self.watching = []
        
        self.setColumnCount(len(self.cols))
        self.setHeaderLabels(self.cols)
        
        # Only used in IDE
        if isfile(get_DB_location()):
            self.setRuns()
            
        # Slot connections
        self.itemSelectionChanged.connect(self.onSelect)
        self.itemDoubleClicked.connect(self.doubleClicked)
        
        # Setup Context Menu
        self.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.prepareMenu)
        
    
    def addRuns(self, runs):
        """
        Adds Row to table.

        Parameters
        ----------
        runs : dict{int: dict}
            Row data to be added.
            See qplot.datahandling.readDS.get_runs_via_sql() for how runs is
            produced.

        """
        self.setSortingEnabled(False) # Prevent constant restort on adding items
        
        append_to_watching = False
        self.maxTime = max(np.array([subDict["run_timestamp"] for subDict in runs.values()], dtype=float), default=0)
        
        for run_id, metadata in runs.items():
            arr = [str(run_id)] #run id
            
            # Skip values missing 'run_timestamp', this only happens on a run 
            # which failed to initialise and has no data. Also breaks app...
            if not metadata["run_timestamp"]:
                continue
            run_time = datetime.fromtimestamp(metadata["run_timestamp"])
            
            # Add data display to array
            
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
                append_to_watching = True
            arr.append(metadata["guid"]) #guid
        
            # Convert arr to easy to sort QTreeWidgetItem
            item = SortableTreeWidgetItem(arr)
            
            # Add to top
            self.addTopLevelItem(item)
            
            # If unfinished run
            if append_to_watching:
                self.watching.append(item)
            
        self.setSortingEnabled(True)
        for i in range(len(self.cols)):
            self.resizeColumnToContents(i)
        
        
    def setRuns(self):
        """
        Resets table and creates all rows.

        """
        self.clear()
        runs = get_runs_via_sql()
        
        self.addRuns(runs)      


    def checkWatching(self):
        """
        Check unfinished runs within table and sets finish time if completed.

        """
        to_remove = []
        for run in self.watching:

            finished = has_finished(run.guid)[0]

            if finished:
                run.setText(5, datetime.fromtimestamp(
                        finished,
                        ).strftime("%Y-%m-%d %H:%M:%S"))
                to_remove.append(run)
        
        # Remove runs outside for loops to prevent interfering with loop indexing
        for run in to_remove:
            self.watching.remove(run)      
            
    
    @QtCore.pyqtSlot(QtCore.QPoint)
    def prepareMenu(self, pos):
        """
        Produces the context menu at mouse position on right click.
        Allows user to open specific Plot or add 1d plots to other 1d plots.
        
        Relies on the fact the the right click is consered the same as a left 
        click for slots. So right click also runs the selection code of left
        click before the context menu, auto loading data needed in Main Window.

        Parameters
        ----------
        pos : PyQt5.QtCore.QPoint
            The cursor position to open the menu at.

        """
        # Get Main Window
        main = self.parentWidget().parent()
        
        menu = qtw.QMenu(self)
        
        ### OPEN SUBMENU
        open_menu = menu.addMenu("&Open")
        
        open_all = qtw.QAction("All", self)
        open_all.triggered.connect(lambda _,: main.openPlot()) # Feed no param to open all
        open_menu.addAction(open_all)
        
        open_menu.addSeparator()
        
        params = {param: param.depends_on_ for param in main.ds.get_parameters() if param.depends_on}
        
        # Create an action for all dependant parameters in the loaded dataset,
        # linking the coresponding parameter to the openPlot.
        for param in params.keys():
            
            open_win = qtw.QAction(f"{param.name}", self)
            
            # Due to the for loop, the lambda function sets param as an optional 
            # default. Otherwise, param is set by the last iteration of the for loop.
            # This will be done a few times through the program but this note 
            # may be missing
            open_win.triggered.connect(lambda _, param=param: main.openPlot(params=[param]))
            
            open_menu.addAction(open_win)
        
        
        ### ADD SUBMEMU
        add_menu = menu.addMenu("&Add _ to _")
        
        add_all = add_menu.addMenu("All")
        valid_wins = []
        
        add_menu.addSeparator()
        
        """
        The Add _ to _ menu allows the user to pick a parameter and add it to 
        another open windows's plot.
        The following loops though each dependant parameter in the loaded dataset
        Inside the context menu Add _ to _, it produces a list of submenus of 
        each parameter.
        It then checks which windows that parameter can be added to and adds an
        action for each window to that parameter's submenu.
        
        It keeps track of which which windows can be added to and only shows 
        those as options in the All option. The All option doesn't care about
        which parameter it is searching through, it tries all.
        
        In practice the order of processes is different. But each button is 
        connected to self.add_plot for single parameter or self.add_all, which
        uses self.add_plot to.
        
        """
        # Why didn't I comment this when I wrote it? Scary.
        for param, depends_on in params.items():
            if len(depends_on) != 1: # Ignore non 1d plots
                continue
            
            
            valid_actions = []
            
            # Run through each window for each parameter
            for win in main.windows:
                if win.param.depends_on_ == depends_on: # If it can be added
                    
                    # Produce action and connect open
                    win_action = qtw.QAction(f"{win.label}", self)
                    win_action.triggered.connect(
                        lambda _, win=win, param=param: self.add_plot(win, param)
                        )
                    # Store for adding later
                    valid_actions.append(win_action)
                    
                    # Check if this window is on the add all list.
                    if win not in valid_wins:
                        all_action = qtw.QAction(f"{win.label}", self)
                        all_action.triggered.connect(
                            lambda _, win=win, param_dict=params: self.add_all(win, param_dict)
                            )
                        # Add action to add all list and the menu
                        valid_wins.append(win)
                        add_all.addAction(all_action)
             
            # If any actions where found, create menu for them.
            if valid_actions:
                param_menu = add_menu.addMenu(f"{param.name}")
                param_menu.addActions(valid_actions)
            
        # Display context menu
        menu.exec_(self.mapToGlobal(pos))
    

    @QtCore.pyqtSlot()
    def onSelect(self):
        """
        Event handler for right/click on table.
        This emits a signal connected to: 
            qplot.windows.main.MainWindow.updateSelected()
        for further loading.

        Returns
        -------
        None.

        """
        if len(self.selectedItems()) == 1: # Check multiple items are not selected
            selection = self.selectedItems()[0].guid #emit guid
            self.selected.emit(selection)


    @QtCore.pyqtSlot(qtw.QTreeWidgetItem, int)
    def doubleClicked(self, item, column):
        """
        Emits a signal to tell qplot.windows.main.MainWindow to open all params
        of selected row.

        Parameters
        ----------
        Unused but required by signal

        """
        self.plot.emit(None)
    
    
    def add_plot(self, target_win, param):
        """
        Event handler for add _ to _ context menu option

        Parameters
        ----------
        target_win : qplot.windows.plotWin.plotWidget
            The subplot will be added to this window.
        param : qcodes.dataset.descriptions.param_spec.ParamSpec
            The depandant parameter that will be added to the target_win.

        """
        # Get Main Window
        main = self.parentWidget().parent()
        from_win = None
        
        # Find window with param from open windows.
        for win in main.windows:
            if win.ds.guid == self.selectedItems()[0].guid and win.param == param:
                if target_win == win:
                    print(f"Skip, {target_win.label}. Target and Source are the same.\n")
                    return
                from_win = win
                break
           
        # If param window not found, produce new window with param to load and 
        # fectch data from
        if not from_win:

            main.openPlot(params=[param], show=False)
            from_win = main.windows[-1] # Due to single thread, should be right
            
            # Start monitor for live plotting
            if from_win.ds.running:
                if not target_win.monitor.isActive():
                    target_win.monitorIntervalChanged(target_win.spinBox.value())
                    target_win.toolbarRef.show()
            
        # Update the display on target_win to show new plot
        if target_win.option_boxes[-1].isEnabled():
            box = target_win.option_boxes[-1]
        else:
            target_win.add_option_box()
            box = target_win.option_boxes[-1]
        
        # Set box text, this also calls functions to add the plot
        index = box.option_box.findText(from_win.label)
        box.option_box.setCurrentIndex(index)
        
        from_win.close()
        
     
    def add_all(self, target_win, param_dict):
        """
        Event handler for add _ to _ context menu all action.
        Add all plots which are able to be added to the target window

        Parameters
        ----------
        target_win : qplot.windows.plotWin.plotWidget
            The subplot will be added to this window.
        param_dict : dict{qcodes.dataset.descriptions.param_spec.ParamSpec}
            A dictionary of all parameters to try to add.

        """
        for param, depends_on in param_dict.items():
            if depends_on == target_win.param.depends_on_:
                self.add_plot(target_win, param)
   
    
#3 classes/methods below are adapted from plottr and as such are not commented by me
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
    def guid(self): # Easier fetching for data
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
        


