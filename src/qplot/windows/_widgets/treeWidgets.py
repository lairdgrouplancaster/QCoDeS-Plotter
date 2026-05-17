from PyQt5 import (
    QtWidgets as qtw,
    QtCore,
    QtGui,
    )

from qplot.datahandling import (
    get_runs_via_sql,
    get_run_status,
    )
from .preview import (
    COLLAPSE_MINIMUM_RATIO,
    DraggablePreviewImageLabel,
    PreviewTab,
    )
from ._run_formatting import (  # noqa: F401
    format_complete_cell,
    format_duration_dhms,
    format_parameter_list,
    format_parameter_list_html,
    format_point_count,
    format_progress,
    format_progress_percent,
    format_run_duration,
    format_run_status,
    format_storage_size,
    format_time_taken_seconds,
    format_timestamp,
    measured_parameter_count,
    progress_percent_value,
    run_is_complete,
    run_tooltip_plain_text,
    run_tooltip_text,
    time_taken_seconds,
    )

from qcodes.dataset.sqlite.database import get_DB_location

from os.path import isfile

import numpy as np

from datetime import datetime

from .._shortcuts import standard_key_sequences

COPY_SELECTION_SHORTCUTS = standard_key_sequences(QtGui.QKeySequence.Copy, ["Ctrl+C"])
COPY_CELL_SHORTCUTS = [QtGui.QKeySequence("Ctrl+Shift+C")]
MEASUREMENT_PREVIEW_SIZE = 22
MEASUREMENT_PREVIEW_SPACING = 3


def copy_action(label, shortcuts, slot, parent):
    action = qtw.QAction(label, parent)
    action.setShortcuts(shortcuts)
    action.setShortcutContext(QtCore.Qt.WidgetWithChildrenShortcut)
    if hasattr(action, "setShortcutVisibleInContextMenu"):
        action.setShortcutVisibleInContextMenu(True)
    action.triggered.connect(slot)
    return action


class RunPreviewCell(qtw.QWidget):
    plotRequested = QtCore.pyqtSignal(str, str)
    exportRequested = QtCore.pyqtSignal(str, str)

    def __init__(self, guid, count, parent=None, icon_size=MEASUREMENT_PREVIEW_SIZE):
        super().__init__(parent)
        self.guid = guid
        self.placeholder_count = max(0, int(count or 0))
        self.icon_size = int(icon_size)

        self.content_layout = qtw.QHBoxLayout()
        self.content_layout.setContentsMargins(2, 0, 2, 0)
        self.content_layout.setSpacing(MEASUREMENT_PREVIEW_SPACING)
        self.content_layout.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        self.setLayout(self.content_layout)
        self.setFixedHeight(self.icon_size + 6)
        self.show_placeholders()


    def show_placeholders(self, count=None):
        self._clear_layout()
        placeholder_count = self.placeholder_count if count is None else max(0, int(count))
        for _ in range(placeholder_count):
            self.content_layout.addWidget(self._placeholder_label())
        self.content_layout.addStretch()


    def show_previews(self, previews):
        self._clear_layout()

        preview_count = 0
        for preview in previews or []:
            image = preview.get("image")
            if image is None:
                continue

            label = DraggablePreviewImageLabel(
                self.guid,
                preview.get("parameter", ""),
                preview.get("axes") or [],
                )
            label.setObjectName("measurementPreviewImage")
            label.setFixedSize(self.icon_size, self.icon_size)
            label.setAlignment(QtCore.Qt.AlignCenter)
            label.setToolTip(preview.get("title", ""))
            label.setPixmap(
                QtGui.QPixmap.fromImage(image).scaled(
                    self.icon_size,
                    self.icon_size,
                    QtCore.Qt.KeepAspectRatio,
                    QtCore.Qt.SmoothTransformation,
                    )
                )
            label.plotRequested.connect(self._emit_plot_requested)
            label.exportRequested.connect(self._emit_export_requested)
            self.content_layout.addWidget(label)
            preview_count += 1

        for _ in range(max(0, self.placeholder_count - preview_count)):
            self.content_layout.addWidget(self._placeholder_label())
        self.content_layout.addStretch()


    def _placeholder_label(self):
        label = qtw.QLabel()
        label.setObjectName("measurementPreviewPlaceholder")
        label.setFixedSize(self.icon_size, self.icon_size)
        label.setFrameShape(qtw.QFrame.Box)
        label.setFrameShadow(qtw.QFrame.Plain)
        label.setLineWidth(1)
        return label


    def _emit_plot_requested(self, parameter):
        self.plotRequested.emit(self.guid, parameter)


    def _emit_export_requested(self, parameter):
        self.exportRequested.emit(self.guid, parameter)


    def _clear_layout(self):
        while self.content_layout.count():
            item = self.content_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()


class EqualsAlignedDelegate(qtw.QStyledItemDelegate):
    """
    Paints setpoint counts with equals signs aligned.
    """

    right_text_alignment = QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter

    def paint(self, painter, option, index):
        text = index.data(QtCore.Qt.DisplayRole)
        if text is None or text == "":
            super().paint(painter, option, index)
            return

        opt = qtw.QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text = str(text)
        sections = self._display_sections(text)
        if sections is None:
            super().paint(painter, option, index)
            return
        left, right = sections

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
        if right is None and max_right_width <= 0:
            painter.save()
            painter.setFont(opt.font)
            painter.setPen(self._text_color(opt))
            painter.drawText(
                text_rect,
                QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter,
                metrics.elidedText(left, QtCore.Qt.ElideLeft, text_rect.width())
                )
            painter.restore()
            return

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
        if right is not None:
            painter.drawText(equals_rect, QtCore.Qt.AlignCenter, equals_text)
            self._draw_right_text(painter, right_rect, right, metrics)
        painter.restore()


    def _display_sections(self, text):
        if " = " in text:
            return str(text).split(" = ", 1)
        if self._is_count_text(text):
            return str(text), None
        return None


    def _is_count_text(self, text):
        try:
            int(str(text).replace(",", ""))
        except (TypeError, ValueError):
            return False
        return True


    def _draw_right_text(self, painter, right_rect, right, metrics):
        painter.drawText(
            right_rect,
            self.right_text_alignment,
            metrics.elidedText(right, QtCore.Qt.ElideRight, right_rect.width())
            )


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

            sections = self._display_sections(item.text(column))
            if sections is None:
                continue

            _, right = sections
            if right is None:
                continue

            max_width = max(max_width, metrics.horizontalAdvance(right))
        return max_width


    def _text_color(self, option):
        return option.palette.color(QtGui.QPalette.Text)


class RunList(qtw.QTreeWidget):
    """
    A modified PyQt5.QtWidgets.QTreeWidget, formated as a list which displays
    all run_ids and other properties found in self.cols.
    
    All QTreeWidgetItem are converted to SortableTreeWidgetItem to allow the user to sort
    by any columns.
    
    """
    
    cols = ['ID', 'Measurements', 'Setpoints', 'Started', 'Complete', 'Duration', 'Size']
    column_widths = {
        "ID": 34,
        "Measurements": 84,
        "Complete": 66,
        "Duration": 68,
        "Size": 50,
        }
    elastic_column_widths = {
        "Setpoints": 80,
        "Started": 84,
        }

    selected = QtCore.pyqtSignal([str])
    plot = QtCore.pyqtSignal([str])
    previewPlotRequested = QtCore.pyqtSignal(str, str)
    previewExportRequested = QtCore.pyqtSignal(str, str)
    _shortcut_keys = "1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    
    def __init__(self, *args, initalize=False, initialize=None, **kargs):
        super().__init__(*args, **kargs)
        if initialize is not None:
            initalize = initialize
        
        self.watching = []
        self.preview_cells = {}
        
        self.setColumnCount(len(self.cols))
        self.setHeaderLabels(self.cols)
        self.setRootIsDecorated(False)
        self.setIndentation(0)
        self.setUniformRowHeights(False)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setItemDelegateForColumn(
            self.cols.index("Setpoints"),
            EqualsAlignedDelegate(self)
            )
        self._resize_columns()
        
        # Optional IDE convenience; MainWindow loads databases asynchronously.
        if initalize and isfile(get_DB_location()):
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

            measurement_count = measured_parameter_count(metadata)
            arr.append("") #measured previews; count remains the hidden sort key
            arr.append(format_point_count(metadata)) #points
            arr.append(format_timestamp(metadata["run_timestamp"])) #started
            arr.append(format_complete_cell(metadata)) #complete
            arr.append(format_time_taken_seconds(metadata)) #duration
            arr.append(format_storage_size(metadata.get("storage_bytes"))) #size

            if not run_is_complete(metadata):
                append_to_watching = True

            # Convert arr to easy to sort QTreeWidgetItem
            item = SortableTreeWidgetItem(arr)
            item.set_guid(metadata["guid"])
            item.run_metadata = dict(metadata)
            for col_name in ("ID", "Setpoints", "Size"):
                item.setTextAlignment(
                    self.cols.index(col_name),
                    QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter
                    )
            item.setTextAlignment(
                self.cols.index("Complete"),
                QtCore.Qt.AlignCenter
                )
            item.setTextAlignment(
                self.cols.index("Duration"),
                QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter
                )
            item.setData(
                self.cols.index("Measurements"),
                QtCore.Qt.UserRole,
                measurement_count
                )
            item.setSizeHint(
                self.cols.index("Measurements"),
                QtCore.QSize(0, MEASUREMENT_PREVIEW_SIZE + 6)
                )
            item.setData(
                self.cols.index("Setpoints"),
                QtCore.Qt.UserRole,
                metadata.get("setpoint_count")
                or metadata.get("expected_results")
                or metadata.get("result_count")
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
                self.cols.index("Duration"),
                QtCore.Qt.UserRole,
                time_taken_seconds(metadata)
                )
            item.setData(
                self.cols.index("Size"),
                QtCore.Qt.UserRole,
                metadata.get("storage_bytes")
                )
            item.update_tooltip()
            
            # Add to top
            self.addTopLevelItem(item)
            self._set_measurement_preview_cell(item, measurement_count)
            
            # If unfinished run
            if append_to_watching:
                self.watching.append(item)
            
        self.setSortingEnabled(True)
        self._resize_columns()


    def clear(self):
        self.preview_cells = {}
        super().clear()


    def _set_measurement_preview_cell(self, item, measurement_count):
        column = self.cols.index("Measurements")
        cell = RunPreviewCell(item.guid, measurement_count, self)
        cell.plotRequested.connect(self._preview_plot_requested)
        cell.exportRequested.connect(self._preview_export_requested)
        self.preview_cells[item.guid] = cell
        self.setItemWidget(item, column, cell)


    @QtCore.pyqtSlot(str, object)
    def set_run_previews(self, guid, previews):
        cell = self.preview_cells.get(guid)
        if cell is not None:
            cell.show_previews(previews)


    @QtCore.pyqtSlot(str, str)
    def _preview_plot_requested(self, guid, parameter):
        item = self._item_for_guid(guid)
        if item is not None:
            self.setCurrentItem(item)
        self.previewPlotRequested.emit(guid, parameter)


    @QtCore.pyqtSlot(str, str)
    def _preview_export_requested(self, guid, parameter):
        item = self._item_for_guid(guid)
        if item is not None:
            self.setCurrentItem(item)
        self.previewExportRequested.emit(guid, parameter)


    def _item_for_guid(self, guid):
        for row in range(self.topLevelItemCount()):
            item = self.topLevelItem(row)
            if item is not None and item.guid == guid:
                return item
        return None


    def _resize_columns(self):
        header = self.header()
        header.setStretchLastSection(False)
        header.setMinimumSectionSize(32)

        for col in range(len(self.cols)):
            header.setSectionResizeMode(col, qtw.QHeaderView.Interactive)

        fixed_width = sum(self.column_widths.values())
        elastic_min_width = sum(self.elastic_column_widths.values())
        available_width = self.viewport().width()
        if available_width <= 0:
            available_width = fixed_width + elastic_min_width

        extra_width = max(0, available_width - fixed_width - elastic_min_width)
        elastic_widths = dict(self.elastic_column_widths)
        setpoints_extra = (extra_width * 2) // 3
        elastic_widths["Setpoints"] += setpoints_extra
        elastic_widths["Started"] += extra_width - setpoints_extra

        for col, name in enumerate(self.cols):
            width = self.column_widths.get(name, elastic_widths.get(name))
            if width is not None:
                self.setColumnWidth(col, width)


    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._resize_columns()
        
        
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
        updated_runs = {}
        for run in self.watching:

            status = get_run_status(run.guid)
            if not status:
                continue

            if status.get("database_modified_timestamp") is not None:
                run.run_metadata["database_modified_timestamp"] = status[
                    "database_modified_timestamp"
                    ]

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
                time_taken_col = self.cols.index("Duration")
                run.setText(time_taken_col, format_time_taken_seconds(run.run_metadata))
                run.setData(
                    time_taken_col,
                    QtCore.Qt.UserRole,
                    time_taken_seconds(run.run_metadata)
                    )

            if status.get("storage_bytes") is not None:
                storage_col = self.cols.index("Size")
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
                time_taken_col = self.cols.index("Duration")
                run.setText(time_taken_col, format_time_taken_seconds(run.run_metadata))
                run.setData(
                    time_taken_col,
                    QtCore.Qt.UserRole,
                    time_taken_seconds(run.run_metadata)
                    )
                to_remove.append(run)

            run.update_tooltip()
            try:
                run_id = int(run.text(0))
            except ValueError:
                run_id = run.text(0)
            updated_runs[run_id] = dict(run.run_metadata)
        
        # Remove runs outside for loops to prevent interfering with loop indexing
        for run in to_remove:
            self.watching.remove(run)

        return updated_runs
            
    
    @QtCore.pyqtSlot(QtCore.QPoint)
    def prepareMenu(self, pos):
        """
        Produces the context menu at mouse position on right click.
        Allows user to open specific plots from the selected run.
        
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

        selected = self.selectedItems()
        if not selected:
            return

        main.add_trace_to_plot(
            target_win,
            selected[0].guid,
            param.name,
            param=param
            )
        
     
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
            ("Duration", self._time_taken_value(dataset, info)),
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
        setpoint_rows = []
        measured_rows = []

        for param in params:
            name = getattr(param, "name", "")
            snap = snapshot_params.get(name, {})
            is_setpoint = name in all_axes and not getattr(param, "depends_on_", ())
            values = self._parameter_row_values(param, snap, is_setpoint, setpoint_summaries)

            if is_setpoint:
                setpoint_rows.append(values)
            else:
                measured_rows.append(values)

        groups = [
            ("Set parameters", setpoint_rows),
            ("Measure parameters", measured_rows),
            ]
        self.parameters.setRowCount(sum(1 + len(rows) for _, rows in groups))

        row = 0
        for heading, rows in groups:
            self._set_parameter_heading_row(row, heading)
            row += 1
            for values in rows:
                for col, value in enumerate(values):
                    self.parameters.setItem(row, col, self._table_item(value, max_len=80))
                row += 1

        self._resize_table(self.parameters)


    def _set_parameter_heading_row(self, row, heading):
        for col in range(self.parameters.columnCount()):
            item = self._table_item(heading if col == 0 else "")
            font = item.font()
            font.setBold(True)
            item.setFont(font)
            item.setToolTip(heading)
            self.parameters.setItem(row, col, item)


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
        stretch_cols = {last_col}
        if table.columnCount() > 2:
            stretch_cols.update({0, 1})

        for col in range(table.columnCount()):
            if col in stretch_cols:
                header.setSectionResizeMode(col, qtw.QHeaderView.Stretch)
            else:
                header.setSectionResizeMode(col, qtw.QHeaderView.ResizeToContents)
        header.setStretchLastSection(False)
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


class WrappedValueDelegate(qtw.QStyledItemDelegate):
    WRAP_FLAGS = (
        QtCore.Qt.AlignLeft
        | QtCore.Qt.AlignTop
        | QtCore.Qt.TextWrapAnywhere
        )

    def paint(self, painter, option, index):
        opt = qtw.QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)

        widget = opt.widget
        style = widget.style() if widget is not None else qtw.QApplication.style()
        text = opt.text
        opt.text = ""

        style.drawControl(qtw.QStyle.CE_ItemViewItem, opt, painter, widget)

        text_rect = style.subElementRect(qtw.QStyle.SE_ItemViewItemText, opt, widget)
        text_rect.adjust(0, 2, 0, -2)
        painter.save()
        painter.setFont(opt.font)
        role = (
            QtGui.QPalette.HighlightedText
            if opt.state & qtw.QStyle.State_Selected
            else QtGui.QPalette.Text
            )
        painter.setPen(opt.palette.color(role))
        painter.drawText(text_rect, self.WRAP_FLAGS, text)
        painter.restore()


    def sizeHint(self, option, index):
        opt = qtw.QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)

        width = opt.rect.width()
        if width <= 0 and opt.widget is not None:
            width = opt.widget.columnWidth(index.column())
        width = max(24, width - 6)

        metrics = QtGui.QFontMetrics(opt.font)
        text_rect = metrics.boundingRect(
            QtCore.QRect(0, 0, width, 100000),
            self.WRAP_FLAGS,
            opt.text
            )
        base = super().sizeHint(option, index)
        return QtCore.QSize(base.width(), max(base.height(), text_rect.height() + 6))


class infoTree(qtw.QTreeWidget):
    def __init__(self, expand_all=True, truncate_values=False):
        super().__init__()
        self.expand_all = expand_all
        self.truncate_values = truncate_values
        self.setHeaderLabels(["Key", "Value"])
        self.setColumnCount(2)
        self.setWordWrap(True)
        self.setTextElideMode(QtCore.Qt.ElideNone)
        self.setUniformRowHeights(False)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setItemDelegateForColumn(1, WrappedValueDelegate(self))
        self.header().setSectionResizeMode(0, qtw.QHeaderView.ResizeToContents)
        self.header().setSectionResizeMode(1, qtw.QHeaderView.Stretch)
        self.header().setStretchLastSection(True)
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
        self.header().setSectionResizeMode(0, qtw.QHeaderView.ResizeToContents)
        self.header().setSectionResizeMode(1, qtw.QHeaderView.Stretch)
        self.doItemsLayout()


    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.doItemsLayout()


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
        
