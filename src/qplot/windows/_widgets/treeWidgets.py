from datetime import datetime
from os.path import isfile
from typing import cast

import numpy as np
from PyQt6 import (
    QtCore,
    QtGui,
)
from PyQt6 import (
    QtWidgets as qtw,
)
from qcodes.dataset.sqlite.database import get_DB_location

from qplot.datahandling import (
    get_run_status,
    get_runs_via_sql,
)
from qplot.datahandling.readonly import sqlite_read_only_connection

from .._commands import (
    configure_action,
    create_action,
    plot_measurement_command_spec,
)
from ._run_formatting import (  # noqa: F401
    complete_cell_sort_value,
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
    run_failed,
    run_is_complete,
    run_tooltip_plain_text,
    run_tooltip_text,
    time_taken_seconds,
)
from .details_tables import (
    CopyableTableWidget,
    WrappedValueDelegate,  # noqa: F401 - re-exported for compatibility
    format_value,
    infoTree,
    snapshot_parameters,
)
from .preview import (
    COLLAPSE_MINIMUM_RATIO,
    PreviewTab,
)
from .run_list_items import (
    MEASUREMENT_PREVIEW_SIZE,
    EqualsAlignedDelegate,
    RunPreviewCell,
    SortableTreeWidgetItem,
)

MAX_RUN_PREVIEW_WIDGETS = 500
MAX_SYNCHRONOUS_SETPOINT_SUMMARY_ROWS = 100_000


class RunList(qtw.QTreeWidget):
    """
    A modified PyQt6.QtWidgets.QTreeWidget, formated as a list which displays
    all run_ids and other properties found in self.cols.
    
    All QTreeWidgetItem are converted to SortableTreeWidgetItem to allow the user to sort
    by any columns.
    
    """
    
    cols = ['ID', 'Measurements', 'Setpoints', 'Started', 'Status', 'Duration', 'Size']
    column_widths = {
        "ID": 40,
        "Measurements": 104,
        "Status": 86,
        "Duration": 96,
        "Size": 72,
        }
    elastic_column_widths = {
        "Setpoints": 180,
        "Started": 150,
        }
    readable_column_widths = {
        "ID": 37,
        "Measurements": 96,
        "Setpoints": 100,
        "Started": 128,
        "Status": 78,
        "Duration": 84,
        "Size": 58,
        }
    minimum_column_widths = {
        "ID": 34,
        "Measurements": 84,
        "Setpoints": 80,
        "Started": 84,
        "Status": 72,
        "Duration": 68,
        "Size": 50,
        }
    compact_growth_order = (
        "Measurements",
        "Started",
        "Duration",
        "Size",
        "Status",
        "Setpoints",
        "ID",
        )
    preferred_growth_order = (
        "Setpoints",
        "Started",
        "Duration",
        "Size",
        "Measurements",
        "Status",
        "ID",
        )
    compact_shrink_order = (
        "Setpoints",
        "Started",
        "Measurements",
        "Duration",
        "Status",
        "Size",
        "ID",
        )

    selected = QtCore.pyqtSignal([str])
    plot = QtCore.pyqtSignal([str])
    previewPlotRequested = QtCore.pyqtSignal(str, str)
    previewExportRequested = QtCore.pyqtSignal(str, str)
    _shortcut_keys = "1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    
    def __init__(self, *args, initalize=False, initialize=None, **kargs):
        super().__init__(*args, **kargs)
        if initialize is not None:
            initalize = initialize
        
        self.watching: list[SortableTreeWidgetItem] = []
        self.preview_cells: dict[str, RunPreviewCell] = {}
        self._items_by_guid: dict[str, SortableTreeWidgetItem] = {}
        self._resizing_columns = False
        self._manual_column_widths = False
        self._preview_widgets_enabled = True
        self.maxRunId = 0
        
        self.setColumnCount(len(self.cols))
        self.setHeaderLabels(self.cols)
        self.setRootIsDecorated(False)
        self.setIndentation(0)
        self.setUniformRowHeights(False)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._setpoints_delegate = EqualsAlignedDelegate(self)
        self.setItemDelegateForColumn(
            self.cols.index("Setpoints"),
            self._setpoints_delegate,
            )
        self._resize_columns()

        header = self.header()
        if header is not None:
            header.sectionResized.connect(self._column_resized)
            header.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
            header.customContextMenuRequested.connect(self._open_header_menu)
        
        # Optional IDE convenience; MainWindow loads databases asynchronously.
        if initalize and isfile(get_DB_location()):
            self.setRuns()
            
        # Slot connections
        self.itemSelectionChanged.connect(self.onSelect)
        self.itemDoubleClicked.connect(self._double_clicked)
        
        # Setup Context Menu
        self.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.prepareMenu)

        context_action = create_action(
            "context.show",
            self,
            status_tip="Show run-list context menu",
            )
        context_action.setShortcutContext(
            QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut
            )
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

        if (
                self._preview_widgets_enabled
                and self.topLevelItemCount() + len(runs) > MAX_RUN_PREVIEW_WIDGETS
                ):
            self._disable_measurement_preview_widgets()

        self.setSortingEnabled(False) # Prevent constant restort on adding items

        self.maxRunId = max(self.maxRunId, max(runs, default=0))
        
        for run_id, metadata in runs.items():
            append_to_watching = False
            arr = [str(run_id)] # Run ID
            
            measurement_count = measured_parameter_count(metadata)
            arr.append("") #measured previews; count remains the hidden sort key
            arr.append(format_point_count(metadata)) #points
            arr.append(format_timestamp(metadata.get("run_timestamp"))) #started
            arr.append(format_complete_cell(metadata)) #status
            arr.append(format_time_taken_seconds(metadata)) #duration
            arr.append(format_storage_size(metadata.get("storage_bytes"))) #size

            if not run_is_complete(metadata):
                append_to_watching = True

            # Convert arr to easy to sort QTreeWidgetItem
            item = SortableTreeWidgetItem(arr)
            item.set_guid(metadata["guid"])
            item.run_metadata = dict(metadata)
            self._items_by_guid[item.guid] = item
            for col_name in ("ID", "Setpoints", "Size"):
                item.setTextAlignment(
                    self.cols.index(col_name),
                    QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter
                    )
            item.setTextAlignment(
                self.cols.index("Status"),
                QtCore.Qt.AlignmentFlag.AlignCenter
                )
            item.setTextAlignment(
                self.cols.index("Duration"),
                QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter
                )
            item.setData(
                self.cols.index("Measurements"),
                QtCore.Qt.ItemDataRole.UserRole,
                measurement_count
                )
            item.setData(
                self.cols.index("Measurements"),
                QtCore.Qt.ItemDataRole.AccessibleTextRole,
                self._measurement_accessible_text(metadata, measurement_count),
                )
            item.setSizeHint(
                self.cols.index("Measurements"),
                QtCore.QSize(0, MEASUREMENT_PREVIEW_SIZE + 6)
                )
            item.setData(
                self.cols.index("Setpoints"),
                QtCore.Qt.ItemDataRole.UserRole,
                metadata.get("setpoint_count")
                or metadata.get("expected_results")
                or metadata.get("result_count")
                )
            item.setData(
                self.cols.index("Started"),
                QtCore.Qt.ItemDataRole.UserRole,
                metadata.get("run_timestamp")
                )
            item.setData(
                self.cols.index("Status"),
                QtCore.Qt.ItemDataRole.UserRole,
                complete_cell_sort_value(metadata)
                )
            item.setData(
                self.cols.index("Duration"),
                QtCore.Qt.ItemDataRole.UserRole,
                time_taken_seconds(metadata)
                )
            item.setData(
                self.cols.index("Size"),
                QtCore.Qt.ItemDataRole.UserRole,
                metadata.get("storage_bytes")
                )
            item.update_tooltip()
            
            # Add to top
            self.addTopLevelItem(item)
            if self._preview_widgets_enabled:
                self._set_measurement_preview_cell(item, measurement_count)
            else:
                self._set_compact_measurement_cell(item, measurement_count)
            
            # If unfinished run
            if append_to_watching:
                self.watching.append(item)
            
        self._setpoints_delegate.invalidate_width_cache()
        self.setSortingEnabled(True)


    def updateRuns(self, runs):
        """
        Merge updated metadata into existing rows.

        Background detail loading uses this to fill expensive columns without
        rebuilding the table or disturbing the user's selection.

        """
        if not runs:
            return {}

        updated = {}
        sorting_enabled = self.isSortingEnabled()
        sort_column = self.sortColumn()
        mutable_columns = {
            self.cols.index(name)
            for name in ("Measurements", "Setpoints", "Status", "Duration", "Size")
            }
        suspend_sorting = sorting_enabled and sort_column in mutable_columns
        if suspend_sorting:
            self.setSortingEnabled(False)
        for run_id, metadata in runs.items():
            guid = metadata.get("guid")
            item = self._item_for_guid(guid)
            if item is None:
                continue

            merged_metadata = dict(metadata)
            if (
                    item.run_metadata.get("storage_bytes") is not None
                    and item.run_metadata.get("storage_bytes_estimated") is False
                    and merged_metadata.get("storage_bytes_estimated") is True
                    ):
                merged_metadata.pop("storage_bytes", None)
                merged_metadata.pop("storage_bytes_estimated", None)

            item.run_metadata.update(merged_metadata)
            self._refresh_run_item(item)
            self._sync_watching_item(item)
            updated[run_id] = dict(item.run_metadata)

        self._setpoints_delegate.invalidate_width_cache()
        if suspend_sorting:
            self.setSortingEnabled(True)
        return updated


    def _refresh_run_item(self, item):
        metadata = item.run_metadata
        measurement_count = measured_parameter_count(metadata)

        measurements_col = self.cols.index("Measurements")
        item.setData(
            measurements_col,
            QtCore.Qt.ItemDataRole.UserRole,
            measurement_count,
            )
        item.setData(
            measurements_col,
            QtCore.Qt.ItemDataRole.AccessibleTextRole,
            self._measurement_accessible_text(metadata, measurement_count),
            )
        item.setSizeHint(
            measurements_col,
            QtCore.QSize(0, MEASUREMENT_PREVIEW_SIZE + 6),
            )
        cell = self.preview_cells.get(item.guid)
        if (
                self._preview_widgets_enabled
                and (cell is None or cell.placeholder_count != measurement_count)
                ):
            self._set_measurement_preview_cell(item, measurement_count)
        elif not self._preview_widgets_enabled:
            self._set_compact_measurement_cell(item, measurement_count)

        setpoints_col = self.cols.index("Setpoints")
        item.setText(setpoints_col, format_point_count(metadata))
        item.setData(
            setpoints_col,
            QtCore.Qt.ItemDataRole.UserRole,
            metadata.get("setpoint_count")
            or metadata.get("expected_results")
            or metadata.get("result_count"),
            )

        complete_col = self.cols.index("Status")
        item.setText(complete_col, format_complete_cell(metadata))
        item.setData(
            complete_col,
            QtCore.Qt.ItemDataRole.UserRole,
            complete_cell_sort_value(metadata),
            )

        duration_col = self.cols.index("Duration")
        item.setText(duration_col, format_time_taken_seconds(metadata))
        item.setData(
            duration_col,
            QtCore.Qt.ItemDataRole.UserRole,
            time_taken_seconds(metadata),
            )

        size_col = self.cols.index("Size")
        item.setText(size_col, format_storage_size(metadata.get("storage_bytes")))
        item.setData(
            size_col,
            QtCore.Qt.ItemDataRole.UserRole,
            metadata.get("storage_bytes"),
            )

        item.update_tooltip()


    def _sync_watching_item(self, item):
        watching = item in self.watching
        complete = run_is_complete(item.run_metadata)
        if complete and watching:
            self.watching.remove(item)
        elif not complete and not watching:
            self.watching.append(item)


    def clear(self):
        self.preview_cells = {}
        self._items_by_guid = {}
        self._preview_widgets_enabled = True
        self.setUniformRowHeights(False)
        self._setpoints_delegate.invalidate_width_cache()
        super().clear()


    def _set_measurement_preview_cell(self, item, measurement_count):
        column = self.cols.index("Measurements")
        cell = RunPreviewCell(item.guid, measurement_count, self)
        cell.plotRequested.connect(self._preview_plot_requested)
        cell.exportRequested.connect(self._preview_export_requested)
        accessible_text = self._measurement_accessible_text(
            item.run_metadata,
            measurement_count,
            )
        cell.setAccessibleName(accessible_text)
        cell.setAccessibleDescription(
            "Measurement previews. Focus a preview for plot and export actions."
            )
        self.preview_cells[item.guid] = cell
        self.setItemWidget(item, column, cell)


    def _set_compact_measurement_cell(self, item, measurement_count):
        column = self.cols.index("Measurements")
        item.setText(column, str(measurement_count))
        item.setSizeHint(column, QtCore.QSize(0, 22))
        item.setToolTip(
            column,
            "Inline previews are disabled for this large run list. "
            "Select the run to use the Preview tab.",
            )


    def _disable_measurement_preview_widgets(self):
        column = self.cols.index("Measurements")
        for guid, cell in tuple(self.preview_cells.items()):
            item = self._items_by_guid.get(guid)
            if item is not None:
                self.removeItemWidget(item, column)
                self._set_compact_measurement_cell(
                    item,
                    measured_parameter_count(item.run_metadata),
                    )
            cell.deleteLater()
        self.preview_cells.clear()
        self._preview_widgets_enabled = False
        self.setUniformRowHeights(True)


    @QtCore.pyqtSlot(str, object)
    def set_run_previews(self, guid, previews):
        cell = self.preview_cells.get(guid)
        if cell is not None:
            cell.show_previews(previews)


    @QtCore.pyqtSlot(str, bool)
    def set_run_preview_generating(self, guid, generating):
        cell = self.preview_cells.get(guid)
        if cell is not None:
            cell.set_generating(generating)


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
        return self._items_by_guid.get(guid)


    @staticmethod
    def _measurement_accessible_text(metadata, measurement_count):
        parameters = [
            str(parameter)
            for parameter in metadata.get("measure_parameters", [])
            if parameter
            ]
        noun = "measurement" if measurement_count == 1 else "measurements"
        summary = f"{measurement_count} {noun}"
        if parameters:
            summary += f": {', '.join(parameters)}"
        return summary


    def _column_resized(self, _column, old_size, new_size):
        if not self._resizing_columns and old_size != new_size:
            self._manual_column_widths = True


    def reset_column_widths(self):
        self._manual_column_widths = False
        self._resize_columns(force=True)


    def _open_header_menu(self, pos):
        header = self.header()
        if header is None:
            return
        menu = qtw.QMenu(self)
        reset_action = menu.addAction("Reset column widths")
        if reset_action is not None:
            reset_action.triggered.connect(self.reset_column_widths)
        menu.exec(header.mapToGlobal(pos))


    def _resize_columns(self, force=False):
        if self._manual_column_widths and not force:
            return

        header = self.header()
        if header is not None:
            header.setStretchLastSection(False)
            header.setMinimumSectionSize(32)

            for col in range(len(self.cols)):
                header.setSectionResizeMode(col, qtw.QHeaderView.ResizeMode.Interactive)

        preferred_fixed_width = sum(self.column_widths.values())
        preferred_elastic_width = sum(self.elastic_column_widths.values())
        viewport = self.viewport()
        available_width = viewport.width() if viewport is not None else 0
        preferred_widths = {
            **self.elastic_column_widths,
            **self.column_widths,
            }
        preferred_width = preferred_fixed_width + preferred_elastic_width
        if available_width <= 0:
            available_width = preferred_width

        if available_width < preferred_width:
            readable_width = sum(self.readable_column_widths.values())
            if available_width < readable_width:
                widths = self._grow_column_widths(
                    self.minimum_column_widths,
                    self.readable_column_widths,
                    available_width,
                    self.compact_growth_order,
                    )
            else:
                widths = self._grow_column_widths(
                    self.readable_column_widths,
                    preferred_widths,
                    available_width,
                    self.preferred_growth_order,
                    )
        else:
            extra_width = max(0, available_width - preferred_width)
            widths = dict(preferred_widths)
            setpoints_extra = (extra_width * 2) // 3
            widths["Setpoints"] += setpoints_extra
            widths["Started"] += extra_width - setpoints_extra

        self._resizing_columns = True
        try:
            for col, name in enumerate(self.cols):
                width = widths.get(name)
                if width is not None:
                    self.setColumnWidth(col, width)
        finally:
            self._resizing_columns = False


    def _grow_column_widths(self, base_widths, target_widths, available_width, order):
        widths = dict(base_widths)
        deficit = sum(widths.values()) - available_width
        if deficit > 0:
            return widths

        extra_width = max(0, available_width - sum(widths.values()))
        for name in order:
            target = target_widths.get(name, widths.get(name, 0))
            grow_by = min(max(0, target - widths.get(name, 0)), extra_width)
            widths[name] = widths.get(name, 0) + grow_by
            extra_width -= grow_by
            if extra_width <= 0:
                break
        return widths


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

            run_id: int | str
            try:
                run_id = int(item.text(0))
            except ValueError:
                run_id = item.text(0)

            runs[run_id] = dict(getattr(item, "run_metadata", {}))
        return runs


    def visible_run_ids(self, limit=50):
        run_ids: list[int | str] = []
        viewport = self.viewport()
        if viewport is None:
            return run_ids

        viewport_rect = viewport.rect()
        for index in range(self.topLevelItemCount()):
            item = self.topLevelItem(index)
            if item is None:
                continue

            rect = self.visualItemRect(item)
            if not rect.isValid():
                continue
            if rect.bottom() < viewport_rect.top():
                continue
            if rect.top() > viewport_rect.bottom():
                if run_ids:
                    break
                continue

            run_id = self._item_run_id(item)
            if run_id is None:
                continue
            run_ids.append(run_id)
            if len(run_ids) >= limit:
                break
        return run_ids


    def selected_run_ids(self):
        run_ids = []
        for item in self.selectedItems():
            if isinstance(item, SortableTreeWidgetItem):
                run_id = self._item_run_id(item)
                if run_id is not None:
                    run_ids.append(run_id)
        return run_ids


    def run_id_for_guid(self, guid):
        item = self._item_for_guid(guid)
        if item is None:
            return None
        return self._item_run_id(item)


    def _item_run_id(self, item):
        try:
            return int(item.text(0))
        except (TypeError, ValueError):
            text = item.text(0)
            return text if text else None


    def checkWatching(self, statuses=None):
        """
        Check unfinished runs within table and sets finish time if completed.

        """
        to_remove = []
        updated_runs = {}
        for run in self.watching:

            status = (
                get_run_status(run.guid)
                if statuses is None
                else statuses.get(run.guid, {})
                )
            if not status:
                continue

            if status.get("database_modified_timestamp") is not None:
                run.run_metadata["database_modified_timestamp"] = status[
                    "database_modified_timestamp"
                    ]

            if status.get("is_completed") is not None:
                run.run_metadata["is_completed"] = bool(status["is_completed"])

            shape_metadata_changed = False
            for field in (
                    "point_shape",
                    "setpoint_shape",
                    "setpoint_shape_source",
                    "setpoint_count",
                    "setpoint_count_source",
                    "expected_results",
                    "expected_results_source",
                    ):
                if field not in status:
                    continue
                if run.run_metadata.get(field) != status[field]:
                    shape_metadata_changed = True
                # None is meaningful here: it clears a stale early inference.
                run.run_metadata[field] = status[field]

            if status.get("result_count") is not None:
                run.run_metadata["result_count"] = status["result_count"]

            if status.get("result_count") is not None or shape_metadata_changed:
                points_col = self.cols.index("Setpoints")
                run.setText(points_col, format_point_count(run.run_metadata))
                run.setData(
                    points_col,
                    QtCore.Qt.ItemDataRole.UserRole,
                    run.run_metadata.get("setpoint_count")
                    or run.run_metadata.get("expected_results")
                    or run.run_metadata.get("result_count"),
                    )
                complete_col = self.cols.index("Status")
                run.setText(complete_col, format_complete_cell(run.run_metadata))
                run.setData(
                    complete_col,
                    QtCore.Qt.ItemDataRole.UserRole,
                    progress_percent_value(run.run_metadata)
                    )
                time_taken_col = self.cols.index("Duration")
                run.setText(time_taken_col, format_time_taken_seconds(run.run_metadata))
                run.setData(
                    time_taken_col,
                    QtCore.Qt.ItemDataRole.UserRole,
                    time_taken_seconds(run.run_metadata)
                    )

            completion_metadata_changed = False
            if status.get("read_setpoint_count") is not None:
                run.run_metadata["read_setpoint_count"] = status["read_setpoint_count"]
                completion_metadata_changed = True

            if status.get("measurement_exception") is not None:
                run.run_metadata["measurement_exception"] = status["measurement_exception"]
                completion_metadata_changed = True

            if completion_metadata_changed:
                complete_col = self.cols.index("Status")
                run.setText(complete_col, format_complete_cell(run.run_metadata))
                run.setData(
                    complete_col,
                    QtCore.Qt.ItemDataRole.UserRole,
                    complete_cell_sort_value(run.run_metadata)
                    )

            if status.get("storage_bytes") is not None:
                storage_col = self.cols.index("Size")
                run.run_metadata["storage_bytes"] = status["storage_bytes"]
                run.setText(storage_col, format_storage_size(status["storage_bytes"]))
                run.setData(storage_col, QtCore.Qt.ItemDataRole.UserRole, status["storage_bytes"])

            completed_timestamp = status.get("completed_timestamp")
            if completed_timestamp is not None:
                run.run_metadata["completed_timestamp"] = completed_timestamp

            if run_is_complete(run.run_metadata):
                complete_col = self.cols.index("Status")
                run.setText(complete_col, format_complete_cell(run.run_metadata))
                run.setData(
                    complete_col,
                    QtCore.Qt.ItemDataRole.UserRole,
                    complete_cell_sort_value(run.run_metadata)
                    )
                time_taken_col = self.cols.index("Duration")
                run.setText(time_taken_col, format_time_taken_seconds(run.run_metadata))
                run.setData(
                    time_taken_col,
                    QtCore.Qt.ItemDataRole.UserRole,
                    time_taken_seconds(run.run_metadata)
                    )
                to_remove.append(run)

            run.update_tooltip()
            run_id: int | str
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
        
        Selects the row under the pointer before building actions so every menu
        command targets the row that was actually clicked.

        Parameters
        ----------
        pos : PyQt6.QtCore.QPoint
            The cursor position to open the menu at.

        """
        main = self.main_window()
        if main is None:
            return

        item = self.itemAt(pos)
        if item is None:
            main.show_status("Right-click a run to open its plot menu.", 3000)
            return
        item = cast(qtw.QTreeWidgetItem, item)
        while True:
            parent = item.parent()
            if parent is None:
                break
            item = parent
        if not isinstance(item, SortableTreeWidgetItem):
            return

        if self.currentItem() is not item or item not in self.selectedItems():
            self.clearSelection()
            self.setCurrentItem(item)
            item.setSelected(True)

        if main.ds is None:
            main.show_status("Select a run before opening the context menu.", 5000)
            return
        
        menu = qtw.QMenu(self)

        open_all = create_action(
            "run.plot_selected_all",
            menu,
            text="&Plot all",
            )
        self._set_action_shortcut(open_all, "run.plot_selected_all")
        open_all.triggered.connect(lambda _,: main.open_selected_run_all())
        menu.addAction(open_all)

        params = {param: param.depends_on_ for param in main.ds.get_parameters() if param.depends_on}

        # Create an action for all dependant parameters in the loaded dataset,
        # linking the coresponding parameter to the openPlot.
        for itr, param in enumerate(params.keys()):
            
            open_win = QtGui.QAction(f"  - {param.name}", menu)
            if itr < 9:
                self._set_action_shortcut(
                    open_win,
                    plot_measurement_command_spec(itr),
                    )
            
            # Due to the for loop, the lambda function sets param as an optional 
            # default. Otherwise, param is set by the last iteration of the for loop.
            # This will be done a few times through the program but this note 
            # may be missing
            open_win.triggered.connect(lambda _, param=param: main.openPlot(params=[param]))
            
            menu.addAction(open_win)

        # Display context menu
        menu.exec(self.mapToGlobal(pos))


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


    def _set_action_shortcut(self, action, command):
        """
        Sets a context-menu action shortcut.

        """
        configure_action(action, command)
        action.setShortcutContext(QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
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
            item = self.selectedItems()[0]
            if isinstance(item, SortableTreeWidgetItem):
                self.selected.emit(item.guid)


    @QtCore.pyqtSlot(qtw.QTreeWidgetItem, int)
    def _double_clicked(self, item, column):
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
        selected_item = selected[0]
        if not isinstance(selected_item, SortableTreeWidgetItem):
            return

        main.add_trace_to_plot(
            target_win,
            selected_item.guid,
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
   
    
class moreInfo(qtw.QTabWidget):
    
    def __init__(self, *args, preview_size=None):
        super().__init__(*args)
        self.setObjectName("runDetailsTabs")
        self._setpoint_summary_cache = {}

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
        table.setEditTriggers(qtw.QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(qtw.QAbstractItemView.SelectionBehavior.SelectRows)
        table.setTextElideMode(QtCore.Qt.TextElideMode.ElideRight)
        table.setWordWrap(False)
        table.horizontalHeader().setStretchLastSection(True)


    def setInfo(self, info, dataset=None, run_metadata=None, database_path=None):
        self.clear()

        self._set_overview(info, dataset, run_metadata=run_metadata)
        self._set_parameters(info, dataset, run_metadata, database_path)
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


    def _set_overview(self, info, dataset, run_metadata=None):
        structure = info.get("Data Structure", {})
        param_info = {
            key: value for key, value in structure.items()
            if isinstance(value, dict)
            }
        measured = list((run_metadata or {}).get("measure_parameters") or [])
        setpoints = list((run_metadata or {}).get("sweep_parameters") or [])
        if not measured and not setpoints and dataset is not None:
            params = list(dataset.get_parameters())
            setpoint_names = {
                axis
                for param in params
                for axis in getattr(param, "depends_on_", ())
                }
            measured = [
                getattr(param, "name", "")
                for param in params
                if getattr(param, "name", "") not in setpoint_names
                ]
            setpoints = [
                getattr(param, "name", "")
                for param in params
                if getattr(param, "name", "") in setpoint_names
                ]
        elif not measured and not setpoints:
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


    def _set_parameters(self, info, dataset, run_metadata=None, database_path=None):
        params = list(dataset.get_parameters()) if dataset is not None else []
        snapshot_params = snapshot_parameters(info.get("Snapshot"))
        all_axes = []
        seen_axes = set()
        for param in params:
            for axis in getattr(param, "depends_on_", ()):
                if axis in seen_axes:
                    continue
                all_axes.append(axis)
                seen_axes.add(axis)

        setpoint_summaries = self._setpoint_summaries(
            dataset,
            all_axes,
            run_metadata=run_metadata,
            database_path=database_path,
            )
        setpoint_rows = []
        measured_rows = []

        for param in params:
            name = getattr(param, "name", "")
            snap = snapshot_params.get(name, {})
            is_setpoint = name in seen_axes and not getattr(param, "depends_on_", ())
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
        try:
            points = float(points)
        except (TypeError, ValueError):
            return ""

        if points <= 0:
            return ""

        return f"{seconds / points:.3g}"


    def _setpoint_summaries(
            self,
            dataset,
            setpoint_names,
            run_metadata=None,
            database_path=None,
            ):
        run_metadata = run_metadata or {}
        table_name = (
            run_metadata.get("result_table_name")
            or self._dataset_attr(dataset, "table_name")
            )
        try:
            result_count = int(run_metadata.get("result_count") or 0)
        except (TypeError, ValueError, OverflowError):
            result_count = 0

        cache_key = (
            database_path,
            table_name,
            tuple(setpoint_names),
            result_count,
            )
        cached = self._setpoint_summary_cache.get(cache_key)
        if cached is not None:
            summaries = {name: dict(summary) for name, summary in cached.items()}
        elif result_count > MAX_SYNCHRONOUS_SETPOINT_SUMMARY_ROWS:
            summaries = {}
        else:
            summaries = self._setpoint_summaries_from_sql(
                database_path,
                table_name,
                setpoint_names,
                )
            if len(self._setpoint_summary_cache) >= 32:
                self._setpoint_summary_cache.clear()
            self._setpoint_summary_cache[cache_key] = {
                name: dict(summary) for name, summary in summaries.items()
                }
        self._add_setpoint_shape_steps(summaries, setpoint_names, run_metadata)
        return summaries


    def _setpoint_summaries_from_sql(self, database_path, table_name, setpoint_names):
        if not database_path or not table_name or not setpoint_names:
            return {}

        conn = None
        cursor = None
        try:
            conn = sqlite_read_only_connection(database_path, timeout=2)
            cursor = conn.cursor()
            columns = self._result_table_columns(cursor, table_name)
            summaries = {}
            for name in setpoint_names:
                if name not in columns:
                    continue
                summary = self._setpoint_summary_from_sql(cursor, table_name, name)
                if summary:
                    summaries[name] = summary
            return summaries
        except Exception:
            return {}
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()


    def _result_table_columns(self, cursor, table_name):
        cursor.execute(f"PRAGMA table_info({_sqlite_identifier(table_name)})")
        return {row[1] for row in cursor.fetchall()}


    def _setpoint_summary_from_sql(self, cursor, table_name, parameter):
        table = _sqlite_identifier(table_name)
        column = _sqlite_identifier(parameter)
        try:
            cursor.execute(f"""
              WITH distinct_values(value, first_rowid) AS (
                  SELECT {column}, MIN(rowid)
                  FROM {table}
                  WHERE {column} IS NOT NULL
                  GROUP BY {column}
              )
              SELECT
                  (
                      SELECT value
                      FROM distinct_values
                      ORDER BY first_rowid ASC
                      LIMIT 1
                  ),
                  (
                      SELECT value
                      FROM distinct_values
                      ORDER BY first_rowid DESC
                      LIMIT 1
                  ),
                  (SELECT COUNT(*) FROM distinct_values)
            """)
            first_value, last_value, count = cursor.fetchone()
            count = int(count or 0)
            if count <= 0:
                return {}
        except Exception:
            return {}

        if first_value is None or last_value is None:
            return {}

        return {
            "from": first_value,
            "to": last_value,
            "steps": count,
            }


    def _add_setpoint_shape_steps(self, summaries, setpoint_names, run_metadata):
        shape = run_metadata.get("setpoint_shape") or run_metadata.get("point_shape")
        if not shape:
            return

        for name, steps in zip(setpoint_names, shape, strict=False):
            if not self._has_value(steps):
                continue
            summaries.setdefault(name, {}).setdefault("steps", steps)


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
                header.setSectionResizeMode(col, qtw.QHeaderView.ResizeMode.Stretch)
            else:
                header.setSectionResizeMode(col, qtw.QHeaderView.ResizeMode.ResizeToContents)
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


def _sqlite_identifier(name):
    return f'"{str(name).replace(chr(34), chr(34) * 2)}"'
