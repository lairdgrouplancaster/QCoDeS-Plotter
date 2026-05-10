from PyQt5 import (
    QtWidgets as qtw,
    QtCore,
    QtGui,
    )

from qplot.datahandling import (
    get_runs_via_sql,
    has_finished
    )

from qcodes.dataset.sqlite.database import get_DB_location

from os.path import isfile

import numpy as np

from datetime import datetime

from .._shortcuts import standard_key_sequences

COPY_SELECTION_SHORTCUTS = standard_key_sequences(QtGui.QKeySequence.Copy, ["Ctrl+C"])
COPY_CELL_SHORTCUTS = [QtGui.QKeySequence("Ctrl+Shift+C")]


def copy_action(label, shortcuts, slot, parent):
    action = qtw.QAction(label, parent)
    action.setShortcuts(shortcuts)
    action.setShortcutContext(QtCore.Qt.WidgetWithChildrenShortcut)
    if hasattr(action, "setShortcutVisibleInContextMenu"):
        action.setShortcutVisibleInContextMenu(True)
    action.triggered.connect(slot)
    return action


class RunList(qtw.QTreeWidget):
    """
    A modified PyQt5.QtWidgets.QTreeWidget, formated as a list which displays
    all run_ids and other properties found in self.cols.
    
    All QTreeWidgetItem are converted to SortableTreeWidgetItem to allow the user to sort
    by any columns.
    
    """
    
    cols = ['ID', 'Experiment', 'Sample', 'Name', 'Started', 'Completed', 'GUID']

    selected = QtCore.pyqtSignal([str])
    plot = QtCore.pyqtSignal([str])
    _shortcut_keys = "1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    
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

        context_action = qtw.QAction("Show Context Menu", self)
        context_action.setShortcut("Shift+F10")
        context_action.setShortcutContext(QtCore.Qt.WidgetWithChildrenShortcut)
        context_action.triggered.connect(self.openKeyboardMenu)
        self.addAction(context_action)
        
    
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
            arr = [str(run_id)] # Run ID
            
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
        main = self.main_window()
        if main is None:
            return

        if main.ds is None:
            main.show_status("Select a run before opening the context menu.", 5000)
            return
        
        menu = qtw.QMenu(self)

        self._add_menu_section(menu, "Plot")
        open_all = qtw.QAction("&Plot all", menu)
        self._set_action_shortcut(open_all, "Ctrl+Return")
        open_all.triggered.connect(lambda _,: main.openPlot()) # Feed no param to plot all
        menu.addAction(open_all)

        params = {param: param.depends_on_ for param in main.ds.get_parameters() if param.depends_on}

        # Create an action for all dependant parameters in the loaded dataset,
        # linking the coresponding parameter to the openPlot.
        for itr, param in enumerate(params.keys()):
            
            open_win = qtw.QAction(f"  - {param.name}", menu)
            if itr < 9:
                self._set_action_shortcut(open_win, f"Ctrl+{itr + 1}")
            
            # Due to the for loop, the lambda function sets param as an optional 
            # default. Otherwise, param is set by the last iteration of the for loop.
            # This will be done a few times through the program but this note 
            # may be missing
            open_win.triggered.connect(lambda _, param=param: main.openPlot(params=[param]))
            
            menu.addAction(open_win)

        valid_wins = []

        """
        These actions add a parameter from the selected run to an existing
        compatible plot window. The menu is intentionally flat so right-click
        workflows do not require chasing submenus.

        """
        add_actions = []
        for param, depends_on in params.items():
            if len(depends_on) != 1: # Ignore non 1d plots
                continue

            # Run through each window for each parameter
            for win in main.windows:
                if win.param.depends_on_ == depends_on: # If it can be added

                    # Produce action and connect open
                    win_action = qtw.QAction(f"Add {param.name} to {win.label}", menu)
                    win_action.triggered.connect(
                        lambda _, win=win, param=param: self.add_plot(win, param)
                        )
                    add_actions.append(win_action)
                    
                    # Check if this window is on the add all list.
                    if win not in valid_wins:
                        all_action = qtw.QAction(f"Add all to {win.label}", menu)
                        if len(valid_wins) < 9:
                            self._set_action_shortcut(all_action, f"Ctrl+Alt+{len(valid_wins) + 1}")
                        all_action.triggered.connect(
                            lambda _, win=win, param_dict=params: self.add_all(win, param_dict)
                            )
                        add_actions.insert(len(valid_wins), all_action)
                        valid_wins.append(win)

        if add_actions:
            menu.addSeparator()
            self._add_menu_section(menu, "Add to open plot")
            for action in add_actions:
                action.setText(f"  - {action.text()}")
                menu.addAction(action)
            
        # Display context menu
        menu.exec_(self.mapToGlobal(pos))


    @QtCore.pyqtSlot()
    def openKeyboardMenu(self):
        """
        Opens the run context menu from the keyboard.

        """
        item = self.currentItem()
        pos = self.visualItemRect(item).center() if item else self.rect().center()
        self.prepareMenu(pos)


    def main_window(self):
        """
        Returns the owning main window regardless of intermediate layouts.

        """
        window = self.window()
        if hasattr(window, "ds") and hasattr(window, "openPlot"):
            return window

        parent = self.parentWidget()
        while parent is not None:
            if hasattr(parent, "ds") and hasattr(parent, "openPlot"):
                return parent
            parent = parent.parentWidget()

        return None


    def _set_action_shortcut(self, action, shortcut):
        """
        Sets a context-menu action shortcut.

        """
        action.setShortcut(shortcut)
        action.setShortcutContext(QtCore.Qt.WidgetWithChildrenShortcut)
        if hasattr(action, "setShortcutVisibleInContextMenu"):
            action.setShortcutVisibleInContextMenu(True)


    def _add_menu_section(self, menu, label):
        """
        Adds a disabled section label to a context menu.

        """
        section = qtw.QAction(label, menu)
        section.setEnabled(False)
        menu.addAction(section)
        return section


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
        main = self.main_window()
        if main is None:
            return

        from_win = None
        
        # Find window with param from open windows.
        for win in main.windows:
            if win.ds.guid == self.selectedItems()[0].guid and win.param == param:
                if target_win == win:
                    main.show_status(f"Skipped {target_win.label}; source and target are the same.", 5000)
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


class moreInfo(qtw.QTabWidget):
    
    def __init__(self, *args):
        super().__init__(*args)
        self.setObjectName("runDetailsTabs")

        self.overview = CopyableTableWidget()
        self.parameters = CopyableTableWidget()
        self.metadata = infoTree(expand_all=True, truncate_values=True)
        self.raw = infoTree(expand_all=False, truncate_values=False)

        self._setup_table(self.overview, ["Field", "Value"])
        self._setup_table(
            self.parameters,
            ["Name", "Label", "Unit", "Role", "Axes", "Value", "Instrument", "Validator"]
            )

        self.addTab(self.overview, "Overview")
        self.addTab(self.parameters, "Parameters")
        self.addTab(self.metadata, "Metadata")
        self.addTab(self.raw, "Raw")


    def _setup_table(self, table, headers):
        table.setObjectName("detailsTable")
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().hide()
        table.verticalHeader().setMinimumSectionSize(16)
        table.verticalHeader().setDefaultSectionSize(20)
        table.horizontalHeader().setFixedHeight(22)
        table.setAlternatingRowColors(True)
        table.setEditTriggers(qtw.QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(qtw.QAbstractItemView.SelectRows)
        table.setTextElideMode(QtCore.Qt.ElideRight)
        table.setWordWrap(False)
        table.horizontalHeader().setStretchLastSection(True)


    def setInfo(self, info, dataset=None):
        self.clear()

        self._set_overview(info, dataset)
        self._set_parameters(info, dataset)
        self.metadata.setInfo(info.get("MetaData", {}))
        self.raw.setInfo(info)


    def clear(self):
        self.overview.setRowCount(0)
        self.parameters.setRowCount(0)
        self.metadata.clear()
        self.raw.clear()


    def scrollToTop(self):
        self.overview.scrollToTop()
        self.parameters.scrollToTop()
        self.metadata.scrollToTop()
        self.raw.scrollToTop()


    def _set_overview(self, info, dataset):
        structure = info.get("Data Structure", {})
        param_info = {
            key: value for key, value in structure.items()
            if isinstance(value, dict)
            }
        measured = [
            name for name, details in param_info.items()
            if details.get("axes")
            ]
        setpoints = [
            name for name, details in param_info.items()
            if not details.get("axes")
            ]

        rows = [
            ("Run ID", self._dataset_attr(dataset, "run_id")),
            ("Name", self._dataset_attr(dataset, "name")),
            ("Experiment", self._dataset_attr(dataset, "exp_name")),
            ("Sample", self._dataset_attr(dataset, "sample_name")),
            ("Status", self._status_text(dataset)),
            ("Data points", structure.get("Data points")),
            ("Measured parameters", ", ".join(measured)),
            ("Setpoints", ", ".join(setpoints)),
            ("GUID", self._dataset_attr(dataset, "guid")),
            ("Started", self._dataset_timestamp(dataset, "run_timestamp")),
            ("Completed", self._dataset_timestamp(dataset, "completed_timestamp")),
            ]
        rows = [(key, value) for key, value in rows if self._has_value(value)]

        self._fill_key_value_table(self.overview, rows)


    def _set_parameters(self, info, dataset):
        params = list(dataset.get_parameters()) if dataset is not None else []
        snapshot_params = snapshot_parameters(info.get("Snapshot"))
        all_axes = {
            axis
            for param in params
            for axis in getattr(param, "depends_on_", ())
            }

        self.parameters.setRowCount(len(params))
        for row, param in enumerate(params):
            name = getattr(param, "name", "")
            snap = snapshot_params.get(name, {})
            axes = list(getattr(param, "depends_on_", ()) or [])
            role = "Measured" if axes else "Setpoint" if name in all_axes else "Other"
            values = [
                name,
                getattr(param, "label", "") or snap.get("label", ""),
                getattr(param, "unit", "") or snap.get("unit", ""),
                role,
                ", ".join(axes),
                snap.get("value", snap.get("raw_value", "")),
                snap.get("instrument_name", snap.get("instrument", "")),
                snap.get("vals", snap.get("validators", "")),
                ]

            for col, value in enumerate(values):
                self.parameters.setItem(row, col, self._table_item(value, max_len=80))

        self._resize_table(self.parameters)


    def _fill_key_value_table(self, table, rows):
        table.setRowCount(len(rows))
        for row, (key, value) in enumerate(rows):
            table.setItem(row, 0, self._table_item(key))
            table.setItem(row, 1, self._table_item(value, max_len=140))
        self._resize_table(table)


    def _resize_table(self, table):
        header = table.horizontalHeader()
        last_col = table.columnCount() - 1

        for col in range(table.columnCount()):
            header.setSectionResizeMode(col, qtw.QHeaderView.Interactive)
        for col in range(last_col):
            table.resizeColumnToContents(col)
        if last_col >= 0:
            header.setSectionResizeMode(last_col, qtw.QHeaderView.Stretch)
            header.setStretchLastSection(True)
        for row in range(table.rowCount()):
            table.setRowHeight(row, 20)


    def _table_item(self, value, max_len=None):
        text = format_value(value, max_len=max_len)
        item = qtw.QTableWidgetItem(text)
        item.setToolTip(format_value(value))
        return item


    def _dataset_attr(self, dataset, name):
        if dataset is None:
            return ""
        value = getattr(dataset, name, "")
        return value() if callable(value) else value


    def _dataset_timestamp(self, dataset, name):
        value = self._dataset_attr(dataset, name)
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")
        return value


    def _status_text(self, dataset):
        running = self._dataset_attr(dataset, "running")
        if running is True:
            return "Running"
        if running is False:
            return "Completed"
        return ""


    def _has_value(self, value):
        return value is not None and value != ""


class infoTree(qtw.QTreeWidget):
    def __init__(self, expand_all=True, truncate_values=False):
        super().__init__()
        self.expand_all = expand_all
        self.truncate_values = truncate_values
        self.setHeaderLabels(["Key", "Value"])
        self.setColumnCount(2)
        self.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.openCopyMenu)

        self.copy_value_action = copy_action(
            "Copy Value",
            COPY_CELL_SHORTCUTS,
            self.copyValue,
            self
            )
        self.copy_selection_action = copy_action(
            "Copy Selection",
            COPY_SELECTION_SHORTCUTS,
            self.copySelection,
            self
            )
        self.addActions([self.copy_value_action, self.copy_selection_action])


    def setInfo(self, info):
        self.clear()

        if not info:
            self.addTopLevelItem(qtw.QTreeWidgetItem(["No data", ""]))
            return
        if not isinstance(info, dict):
            item = qtw.QTreeWidgetItem(["Value", format_value(info, 180 if self.truncate_values else None)])
            item.setToolTip(1, format_value(info))
            self.addTopLevelItem(item)
            return

        items = dictToTree(info, truncate_values=self.truncate_values)
        for item in items:
            self.addTopLevelItem(item)
            item.setExpanded(True)

        if self.expand_all:
            self.expandAll()
        for i in range(2):
            self.resizeColumnToContents(i)


    def openCopyMenu(self, pos):
        item = self.itemAt(pos)
        if item is None:
            return

        self.setCurrentItem(item)

        menu = qtw.QMenu(self)
        menu.addAction(self.copy_value_action)

        copy_row = qtw.QAction("Copy Row", menu)
        copy_row.triggered.connect(lambda: copy_to_clipboard(row_text(item)))
        menu.addAction(copy_row)

        if self.selectedItems():
            menu.addAction(self.copy_selection_action)

        menu.exec_(self.viewport().mapToGlobal(pos))


    def copyValue(self):
        item = self.currentItem()
        if item is not None:
            copy_to_clipboard(item.text(1))


    def copySelection(self):
        items = self.selectedItems()
        if not items and self.currentItem() is not None:
            items = [self.currentItem()]
        copy_to_clipboard("\n".join(row_text(item) for item in items))


class CopyableTableWidget(qtw.QTableWidget):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.openCopyMenu)

        self.copy_cell_action = copy_action(
            "Copy Cell",
            COPY_CELL_SHORTCUTS,
            self.copyCell,
            self
            )
        self.copy_selection_action = copy_action(
            "Copy Selection",
            COPY_SELECTION_SHORTCUTS,
            self.copySelection,
            self
            )
        self.addActions([self.copy_cell_action, self.copy_selection_action])


    def openCopyMenu(self, pos):
        item = self.itemAt(pos)
        if item is None:
            return

        self.setCurrentItem(item)

        menu = qtw.QMenu(self)
        menu.addAction(self.copy_cell_action)
        menu.addAction(self.copy_selection_action)

        menu.exec_(self.viewport().mapToGlobal(pos))


    def copyCell(self):
        item = self.currentItem()
        if item is not None:
            copy_to_clipboard(item.text())


    def copySelection(self):
        ranges = self.selectedRanges()
        if not ranges and self.currentItem() is not None:
            self.copyCell()
            return

        sections = []
        for selected_range in ranges:
            rows = []
            for row in range(selected_range.topRow(), selected_range.bottomRow() + 1):
                values = []
                for col in range(selected_range.leftColumn(), selected_range.rightColumn() + 1):
                    item = self.item(row, col)
                    values.append(item.text() if item is not None else "")
                rows.append("\t".join(values))
            sections.append("\n".join(rows))

        copy_to_clipboard("\n".join(section for section in sections if section))


def dictToTree(d : dict, truncate_values=False):
    items = []
    for k, v in d.items():
        if not isinstance(v, dict):
            item = qtw.QTreeWidgetItem([str(k), format_value(v, 180 if truncate_values else None)])
            item.setToolTip(1, format_value(v))
        else:
            item = qtw.QTreeWidgetItem([k, ''])
            for child in dictToTree(v, truncate_values=truncate_values):
                item.addChild(child)
        items.append(item)
    return items


def snapshot_parameters(snapshot):
    if not isinstance(snapshot, dict):
        return {}

    out = {}
    station = snapshot.get("station", snapshot)
    parameter_dicts = []

    if isinstance(station, dict):
        params = station.get("parameters")
        if isinstance(params, dict):
            parameter_dicts.append(params)

        instruments = station.get("instruments")
        if isinstance(instruments, dict):
            for instrument in instruments.values():
                if isinstance(instrument, dict) and isinstance(instrument.get("parameters"), dict):
                    parameter_dicts.append(instrument["parameters"])

    for params in parameter_dicts:
        for key, details in params.items():
            if not isinstance(details, dict):
                continue
            for name in (key, details.get("name"), details.get("full_name")):
                if name:
                    out[str(name)] = details

    return out


def format_value(value, max_len=None):
    if value is None:
        text = ""
    elif isinstance(value, float):
        text = f"{value:.6g}"
    elif isinstance(value, (list, tuple)):
        text = ", ".join(format_value(item) for item in value)
    else:
        text = str(value)

    text = text.replace("\n", " ")
    if max_len is not None and len(text) > max_len:
        return text[:max_len - 3] + "..."
    return text


def row_text(item):
    return "\t".join(item.text(col) for col in range(item.columnCount()))


def copy_to_clipboard(text):
    clipboard = qtw.QApplication.clipboard()
    if clipboard is not None:
        clipboard.setText(text)
        
