import html

from PyQt5 import (
    QtWidgets as qtw,
    QtCore,
    QtGui,
    )

from qplot.datahandling import (
    get_runs_via_sql,
    get_run_status,
    )
from .preview import COLLAPSE_MINIMUM_RATIO, PreviewTab

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


def run_tooltip_text(metadata):
    """
    Builds the summary shown when hovering over a run table row.

    """
    sweep = html.escape(format_parameter_list(metadata.get("sweep_parameters")))
    measure = html.escape(format_parameter_list(metadata.get("measure_parameters")))

    return (
        "<table cellspacing='0' cellpadding='0'>"
        f"<tr><td>Sweep</td><td>&nbsp;</td><td>({sweep})</td></tr>"
        f"<tr><td>Measure</td><td>&nbsp;</td><td>({measure})</td></tr>"
        "</table>"
        )


def run_tooltip_plain_text(metadata):
    sweep = format_parameter_list(metadata.get("sweep_parameters"))
    measure = format_parameter_list(metadata.get("measure_parameters"))

    return "\n".join([
        f"{'Sweep':<7}({sweep})",
        f"Measure ({measure})",
        ])


def format_parameter_list(parameters):
    if not parameters:
        return "unknown"
    return ", ".join(str(parameter) for parameter in parameters)


def run_is_complete(metadata):
    return bool(metadata.get("completed_timestamp") or metadata.get("is_completed"))


def format_run_status(metadata):
    if run_is_complete(metadata):
        return "Complete"
    return f"Incomplete ({format_progress_percent(metadata)})"


def format_progress(metadata):
    progress = format_progress_percent(metadata)
    if progress == "unknown":
        return "unknown% complete"
    return f"{progress} complete"


def format_progress_percent(metadata):
    if run_is_complete(metadata):
        return "100%"

    percent = progress_percent_value(metadata)
    if percent is None:
        return "unknown"

    return f"{percent:.1f}%"


def progress_percent_value(metadata):
    expected = metadata.get("expected_results")
    count = metadata.get("result_count")
    if not expected or count is None:
        return None

    try:
        return max(0, min(100, (float(count) / float(expected)) * 100))
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def format_complete_cell(metadata):
    if run_is_complete(metadata):
        return "✓"

    progress = format_progress_percent(metadata)
    if progress == "unknown":
        return "unknown"
    return progress


def format_timestamp(timestamp):
    if not timestamp:
        return "unknown"
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def time_taken_seconds(metadata):
    started = metadata.get("run_timestamp")
    if not started:
        return None

    completed = metadata.get("completed_timestamp")
    end = completed if completed else datetime.now().timestamp()
    try:
        return max(0, float(end) - float(started))
    except (TypeError, ValueError):
        return None


def format_time_taken_seconds(metadata):
    seconds = time_taken_seconds(metadata)
    if seconds is None:
        return "unknown"
    return f"{seconds:.1f} s"


def format_run_duration(metadata):
    seconds = time_taken_seconds(metadata)
    if seconds is None:
        return "unknown"

    if seconds < 10:
        return f"{seconds:.2f} s"
    if seconds < 100:
        return f"{seconds:.1f} s"
    return f"{seconds:.0f} s"


def format_duration_dhms(seconds):
    total_seconds = int(round(seconds))
    days, remainder = divmod(total_seconds, 24 * 60 * 60)
    hours, remainder = divmod(remainder, 60 * 60)
    minutes, seconds = divmod(remainder, 60)
    return f"{days}d {hours}h {minutes}m {seconds}s"


def format_storage_size(bytes_value):
    if bytes_value is None:
        return "unknown"

    try:
        size = float(bytes_value)
    except (TypeError, ValueError):
        return "unknown"

    if size < 0:
        return "unknown"

    units = ["B", "KB", "MB", "GB", "TB"]
    unit_index = 0
    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1

    if unit_index == 0:
        return f"{int(size)} B"
    if size < 10:
        return f"{size:.1f} {units[unit_index]}"
    return f"{size:.0f} {units[unit_index]}"


def format_point_count(metadata):
    expected = metadata.get("expected_results")
    shape = metadata.get("point_shape")
    if shape:
        try:
            shape_parts = " × ".join(f"{int(size):,}" for size in shape)
        except (TypeError, ValueError):
            shape_parts = ""

        if expected:
            return f"{int(expected):,} = {shape_parts}"
        if shape_parts:
            return shape_parts

    if expected:
        return f"{int(expected):,}"

    count = metadata.get("result_count")
    if count is not None:
        try:
            return f"{int(count):,}"
        except (TypeError, ValueError):
            pass

    return "unknown"


def measured_parameter_count(metadata):
    return len(metadata.get("measure_parameters") or [])


class EqualsAlignedDelegate(qtw.QStyledItemDelegate):
    """
    Paints values containing " = " with the equals signs vertically aligned.
    """

    def paint(self, painter, option, index):
        text = index.data(QtCore.Qt.DisplayRole)
        if not text or " = " not in text:
            super().paint(painter, option, index)
            return

        opt = qtw.QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        left, right = str(text).split(" = ", 1)
        opt.text = ""

        widget = opt.widget
        style = widget.style() if widget else qtw.QApplication.style()
        style.drawControl(qtw.QStyle.CE_ItemViewItem, opt, painter, widget)

        text_rect = style.subElementRect(
            qtw.QStyle.SE_ItemViewItemText,
            opt,
            widget
            ).adjusted(2, 0, -2, 0)
        metrics = opt.fontMetrics
        equals_text = " = "
        equals_width = metrics.horizontalAdvance(equals_text)
        max_right_width = self._max_right_width(index, metrics)
        equals_left = max(
            text_rect.left(),
            text_rect.right() - max_right_width - equals_width
            )

        left_rect = QtCore.QRect(
            text_rect.left(),
            text_rect.top(),
            max(0, equals_left - text_rect.left()),
            text_rect.height()
            )
        equals_rect = QtCore.QRect(
            equals_left,
            text_rect.top(),
            equals_width,
            text_rect.height()
            )
        right_rect = QtCore.QRect(
            equals_left + equals_width,
            text_rect.top(),
            max(0, text_rect.right() - equals_left - equals_width + 1),
            text_rect.height()
            )

        painter.save()
        painter.setFont(opt.font)
        painter.setPen(self._text_color(opt))
        painter.drawText(
            left_rect,
            QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter,
            metrics.elidedText(left, QtCore.Qt.ElideLeft, left_rect.width())
            )
        painter.drawText(equals_rect, QtCore.Qt.AlignCenter, equals_text)
        painter.drawText(
            right_rect,
            QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
            metrics.elidedText(right, QtCore.Qt.ElideRight, right_rect.width())
            )
        painter.restore()


    def _max_right_width(self, index, metrics):
        view = self.parent()
        if not isinstance(view, qtw.QTreeWidget):
            return 0

        max_width = 0
        column = index.column()
        for row in range(view.topLevelItemCount()):
            item = view.topLevelItem(row)
            if item is None:
                continue

            text = item.text(column)
            if " = " not in text:
                continue

            right = text.split(" = ", 1)[1]
            max_width = max(max_width, metrics.horizontalAdvance(right))
        return max_width


    def _text_color(self, option):
        if option.state & qtw.QStyle.State_Selected:
            return option.palette.color(QtGui.QPalette.HighlightedText)
        return option.palette.color(QtGui.QPalette.Text)


class RunList(qtw.QTreeWidget):
    """
    A modified PyQt5.QtWidgets.QTreeWidget, formated as a list which displays
    all run_ids and other properties found in self.cols.
    
    All QTreeWidgetItem are converted to SortableTreeWidgetItem to allow the user to sort
    by any columns.
    
    """
    
    cols = ['ID', 'Measurements', 'Setpoints', 'Started', 'Complete', 'Time taken', 'Storage']

    selected = QtCore.pyqtSignal([str])
    plot = QtCore.pyqtSignal([str])
    _shortcut_keys = "1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    
    def __init__(self, *args, initalize=False, **kargs):
        super().__init__(*args, **kargs)
        
        self.watching = []
        
        self.setColumnCount(len(self.cols))
        self.setHeaderLabels(self.cols)
        self.setItemDelegateForColumn(
            self.cols.index("Setpoints"),
            EqualsAlignedDelegate(self)
            )
        
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
        if not runs:
            return

        self.setSortingEnabled(False) # Prevent constant restort on adding items

        self.maxTime = max(np.array([subDict["run_timestamp"] for subDict in runs.values()], dtype=float), default=0)
        
        for run_id, metadata in runs.items():
            append_to_watching = False
            arr = [str(run_id)] # Run ID
            
            # Skip values missing 'run_timestamp', this only happens on a run 
            # which failed to initialise and has no data. Also breaks app...
            if not metadata["run_timestamp"]:
                continue
            # Add data display to array
            
            arr.append(str(measured_parameter_count(metadata))) #measured
            arr.append(format_point_count(metadata)) #points
            arr.append(format_timestamp(metadata["run_timestamp"])) #started
            arr.append(format_complete_cell(metadata)) #complete
            arr.append(format_time_taken_seconds(metadata)) #time taken
            arr.append(format_storage_size(metadata.get("storage_bytes"))) #storage

            if not run_is_complete(metadata):
                append_to_watching = True

            # Convert arr to easy to sort QTreeWidgetItem
            item = SortableTreeWidgetItem(arr)
            item.set_guid(metadata["guid"])
            item.run_metadata = dict(metadata)
            for col_name in ("ID", "Setpoints", "Storage"):
                item.setTextAlignment(
                    self.cols.index(col_name),
                    QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter
                    )
            item.setTextAlignment(
                self.cols.index("Complete"),
                QtCore.Qt.AlignCenter
                )
            item.setTextAlignment(
                self.cols.index("Time taken"),
                QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter
                )
            item.setData(
                self.cols.index("Measurements"),
                QtCore.Qt.UserRole,
                measured_parameter_count(metadata)
                )
            item.setData(
                self.cols.index("Setpoints"),
                QtCore.Qt.UserRole,
                metadata.get("expected_results") or metadata.get("result_count")
                )
            item.setData(
                self.cols.index("Started"),
                QtCore.Qt.UserRole,
                metadata.get("run_timestamp")
                )
            item.setData(
                self.cols.index("Complete"),
                QtCore.Qt.UserRole,
                100 if run_is_complete(metadata) else progress_percent_value(metadata)
                )
            item.setData(
                self.cols.index("Time taken"),
                QtCore.Qt.UserRole,
                time_taken_seconds(metadata)
                )
            item.setData(
                self.cols.index("Storage"),
                QtCore.Qt.UserRole,
                metadata.get("storage_bytes")
                )
            item.update_tooltip()
            
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
        self.watching = []
        runs = get_runs_via_sql()
        
        self.addRuns(runs)
        return runs


    def all_run_metadata(self):
        runs = {}
        for index in range(self.topLevelItemCount()):
            item = self.topLevelItem(index)
            if item is None:
                continue

            try:
                run_id = int(item.text(0))
            except ValueError:
                run_id = item.text(0)

            runs[run_id] = dict(getattr(item, "run_metadata", {}))
        return runs


    def checkWatching(self):
        """
        Check unfinished runs within table and sets finish time if completed.

        """
        to_remove = []
        for run in self.watching:

            status = get_run_status(run.guid)
            if not status:
                continue

            if status.get("result_count") is not None:
                run.run_metadata["result_count"] = status["result_count"]
                if not run.run_metadata.get("expected_results"):
                    points_col = self.cols.index("Setpoints")
                    run.setText(points_col, format_point_count(run.run_metadata))
                    run.setData(points_col, QtCore.Qt.UserRole, status["result_count"])
                complete_col = self.cols.index("Complete")
                run.setText(complete_col, format_complete_cell(run.run_metadata))
                run.setData(
                    complete_col,
                    QtCore.Qt.UserRole,
                    progress_percent_value(run.run_metadata)
                    )
                time_taken_col = self.cols.index("Time taken")
                run.setText(time_taken_col, format_time_taken_seconds(run.run_metadata))
                run.setData(
                    time_taken_col,
                    QtCore.Qt.UserRole,
                    time_taken_seconds(run.run_metadata)
                    )

            if status.get("storage_bytes") is not None:
                storage_col = self.cols.index("Storage")
                run.run_metadata["storage_bytes"] = status["storage_bytes"]
                run.setText(storage_col, format_storage_size(status["storage_bytes"]))
                run.setData(storage_col, QtCore.Qt.UserRole, status["storage_bytes"])

            finished = status.get("completed_timestamp")

            if finished:
                run.run_metadata["completed_timestamp"] = finished
                run.run_metadata["is_completed"] = status.get("is_completed", True)
                complete_col = self.cols.index("Complete")
                run.setText(complete_col, format_complete_cell(run.run_metadata))
                run.setData(complete_col, QtCore.Qt.UserRole, 100)
                time_taken_col = self.cols.index("Time taken")
                run.setText(time_taken_col, format_time_taken_seconds(run.run_metadata))
                run.setData(
                    time_taken_col,
                    QtCore.Qt.UserRole,
                    time_taken_seconds(run.run_metadata)
                    )
                to_remove.append(run)

            run.update_tooltip()
        
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
        self._set_action_shortcut(open_all, "Ctrl+Shift+Return")
        open_all.triggered.connect(lambda _,: main.open_selected_run_all())
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
            for itr, action in enumerate(add_actions):
                prefix = "" if itr == 0 else "  - "
                action.setText(f"{prefix}{action.text()}")
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
        self.run_metadata = {}
        self._guid = ""

    def __lt__(self, other: qtw.QTreeWidgetItem) -> bool:
        col = self.treeWidget().sortColumn()
        value1 = self.data(col, QtCore.Qt.UserRole)
        value2 = other.data(col, QtCore.Qt.UserRole)
        if value1 is not None and value2 is not None:
            try:
                return float(value1) < float(value2)
            except (TypeError, ValueError):
                pass

        text1 = self.text(col)
        text2 = other.text(col)
        try:
            return float(text1) < float(text2)
        except ValueError:
            return text1 < text2    
    
    @property
    def guid(self): # Easier fetching for data
        return self._guid


    def set_guid(self, guid):
        self._guid = guid


    def update_tooltip(self):
        tooltip = run_tooltip_text(self.run_metadata)
        for col in range(self.columnCount()):
            self.setToolTip(col, tooltip)


class moreInfo(qtw.QTabWidget):
    
    def __init__(self, *args, preview_size=None):
        super().__init__(*args)
        self.setObjectName("runDetailsTabs")

        self.overview = CopyableTableWidget()
        self.parameters = CopyableTableWidget()
        self.preview = PreviewTab(preview_size=preview_size)
        self._update_preview_minimum_height()
        self.metadata = infoTree(expand_all=True, truncate_values=True)
        self.raw = infoTree(expand_all=False, truncate_values=False)

        self._setup_table(self.overview, ["Field", "Value"])
        self._setup_table(
            self.parameters,
            ["Name", "Label", "Unit", "From", "To", "Steps", "Delay", "Instrument"]
            )

        self.addTab(self.overview, "Overview")
        self.addTab(self.parameters, "Sweep parameters")
        self.addTab(self.preview, "Preview")
        self.addTab(self.metadata, "Metadata")
        self.addTab(self.raw, "Raw key-value")


    def set_preview_size(self, preview_size):
        self.preview.set_preview_size(preview_size)
        self._update_preview_minimum_height()


    def _update_preview_minimum_height(self):
        preferred_height = self.preview.preferred_tab_height() + 36
        self.setMinimumHeight(max(1, round(preferred_height * COLLAPSE_MINIMUM_RATIO)))


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
        self.preview.set_current_run(dataset)
        self.metadata.setInfo(info.get("MetaData", {}))
        self.raw.setInfo(info)


    def clear(self):
        self.overview.setRowCount(0)
        self.parameters.setRowCount(0)
        self.preview.clear_current_run()
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
            ("Status", self._status_text(dataset)),
            ("Data points", structure.get("Data points")),
            ("Time taken", self._time_taken_value(dataset, info)),
            ("Measured parameters", ", ".join(measured)),
            ("Setpoints", ", ".join(setpoints)),
            ("Started", self._dataset_timestamp(dataset, "run_timestamp")),
            ("Completed", self._dataset_timestamp(dataset, "completed_timestamp")),
            ("Experiment", self._dataset_attr(dataset, "exp_name")),
            ("Sample", self._dataset_attr(dataset, "sample_name")),
            ("Name", self._dataset_attr(dataset, "name")),
            ("GUID", self._dataset_attr(dataset, "guid")),
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
        setpoint_summaries = self._setpoint_summaries(dataset, all_axes, params)

        self.parameters.setRowCount(len(params))
        for row, param in enumerate(params):
            name = getattr(param, "name", "")
            snap = snapshot_params.get(name, {})
            is_setpoint = name in all_axes and not getattr(param, "depends_on_", ())
            is_measured = bool(getattr(param, "depends_on_", ()))
            values = self._parameter_row_values(param, snap, is_setpoint, setpoint_summaries)

            for col, value in enumerate(values):
                self.parameters.setItem(row, col, self._table_item(value, max_len=80))
            self._style_parameter_row(row, is_setpoint, is_measured)

        self._resize_table(self.parameters)


    def _style_parameter_row(self, row, is_setpoint, is_measured):
        if not is_setpoint and not is_measured:
            return

        role = "Setpoint" if is_setpoint else "Measured"
        for col in range(self.parameters.columnCount()):
            item = self.parameters.item(row, col)
            if item is None:
                continue
            font = item.font()
            font.setBold(is_setpoint)
            font.setItalic(is_measured)
            item.setFont(font)
            item.setToolTip(f"{role} parameter\n{item.toolTip()}")


    def _parameter_row_values(self, param, snap, is_setpoint, setpoint_summaries):
        name = getattr(param, "name", "")
        common = [
            name,
            getattr(param, "label", "") or snap.get("label", ""),
            getattr(param, "unit", "") or snap.get("unit", ""),
            ]
        instrument = snap.get("instrument_name", snap.get("instrument", ""))

        if not is_setpoint:
            return common + ["", "", "", "", instrument]

        summary = setpoint_summaries.get(name, {})
        return common + [
            summary.get("from", snap.get("value", snap.get("raw_value", ""))),
            summary.get("to", snap.get("value", snap.get("raw_value", ""))),
            summary.get("steps", ""),
            self._parameter_delay(snap),
            instrument,
            ]


    def _parameter_delay(self, snap):
        for key in ("delay", "post_delay", "inter_delay"):
            value = snap.get(key)
            if self._has_value(value):
                return value
        return ""


    def _time_taken_value(self, dataset, info):
        started = self._dataset_attr(dataset, "run_timestamp_raw")
        completed = self._dataset_attr(dataset, "completed_timestamp_raw")
        if not self._has_value(started):
            started = self._dataset_attr(dataset, "run_timestamp")
        if not self._has_value(completed):
            completed = self._dataset_attr(dataset, "completed_timestamp")
        if not self._has_value(started):
            return ""

        end = completed if self._has_value(completed) else datetime.now().timestamp()
        try:
            seconds = max(0, self._timestamp_seconds(end) - self._timestamp_seconds(started))
        except (TypeError, ValueError):
            return ""

        per_point = self._time_per_point(seconds, info, dataset)
        if self._has_value(per_point):
            return f"{seconds:.2f} s\t({format_duration_dhms(seconds)}; {per_point} s/point)"
        return f"{seconds:.2f} s\t({format_duration_dhms(seconds)})"


    def _timestamp_seconds(self, value):
        if isinstance(value, (int, float)):
            return float(value)

        if isinstance(value, datetime):
            return value.timestamp()

        text = str(value)
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                return datetime.strptime(text, fmt).timestamp()
            except ValueError:
                pass

        return float(value)


    def _time_per_point(self, seconds, info, dataset):
        points = info.get("Data Structure", {}).get("Data points")
        if not self._has_value(points):
            points = self._dataset_attr(dataset, "number_of_results")

        try:
            points = float(points)
        except (TypeError, ValueError):
            return ""

        if points <= 0:
            return ""

        return f"{seconds / points:.3g}"


    def _setpoint_summaries(self, dataset, setpoint_names, params):
        if dataset is None or not setpoint_names:
            return {}

        summaries = {}
        measured_params = [
            param for param in params
            if getattr(param, "depends_on_", ())
            ]

        for param in measured_params:
            try:
                parameter_data = dataset.get_parameter_data(param.name).get(param.name, {})
            except Exception:
                continue

            for name in setpoint_names:
                if name not in parameter_data or name in summaries:
                    continue
                summary = self._setpoint_summary(parameter_data[name])
                if summary:
                    summaries[name] = summary

        return summaries


    def _setpoint_summary(self, values):
        try:
            array = np.asarray(values).ravel()
        except Exception:
            return {}

        unique_values = []
        seen = set()
        for value in array:
            try:
                if np.isnan(value):
                    continue
            except TypeError:
                pass

            key = value.item() if hasattr(value, "item") else value
            if key in seen:
                continue
            seen.add(key)
            unique_values.append(key)

        if not unique_values:
            return {}

        return {
            "from": unique_values[0],
            "to": unique_values[-1],
            "steps": len(unique_values),
            }


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
        
