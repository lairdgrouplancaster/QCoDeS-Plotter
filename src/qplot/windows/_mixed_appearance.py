"""One appearance list with controls for the selected renderer type."""

from typing import TYPE_CHECKING, Any

from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

from ._plot1d_traces import _TraceAppearanceDialog
from ._plot2d_layers import _HeatmapAppearanceControls
from ._plot_appearance import CenteredPreviewDelegate
from ._plot_drop_axes import dataset_axis_warnings

if TYPE_CHECKING:
    from ._mixed_plots import MixedPlotMixin

_KIND_ROLE = int(QtCore.Qt.ItemDataRole.UserRole) + 1


class _HeatmapControls(_HeatmapAppearanceControls):
    def __init__(self, dialog: "PlotAppearanceDialog"):
        self.dialog = dialog
        super().__init__(dialog.owner, table=dialog.table)
        self.setParent(dialog)
        layout = self.layout()
        if layout is not None:
            layout.setContentsMargins(0, 0, 0, 0)

    def refresh_rows(self):
        self.dialog.refresh_rows()


class PlotAppearanceDialog(_TraceAppearanceDialog):
    """Reuse trace and heatmap editors behind a single dataset table."""

    owner: "MixedPlotMixin"
    _COL_WARNING = 3

    def __init__(self, owner: "MixedPlotMixin"):
        super().__init__(owner)
        self.setWindowTitle("Plot Appearance")
        self.table.setObjectName("plotAppearanceTable")
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["ID", "Preview", "Measurement", ""])
        self.table.setItemDelegateForColumn(
            self._COL_WARNING,
            CenteredPreviewDelegate(self.table, icon_size=QtCore.QSize(18, 18)),
        )
        header = self.table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(self._COL_WARNING, qtw.QHeaderView.ResizeMode.Fixed)
            header.resizeSection(self._COL_WARNING, 28)
        self.table.setToolTip("Data at the top is drawn on top. Drag rows to change the drawing order.")
        self.setStyleSheet(self.styleSheet().replace("traceAppearanceTable", "plotAppearanceTable"))
        self.heatmap_controls = _HeatmapControls(self)
        self.controls_stack = qtw.QStackedWidget(self)
        self.controls_stack.setObjectName("plotAppearanceControls")
        self.appearance_body.removeWidget(self.trace_controls_panel)
        self.controls_stack.addWidget(self.trace_controls_panel)
        self.controls_stack.addWidget(self.heatmap_controls)
        self.controls_stack.setMinimumWidth(self.trace_controls_panel.minimumSizeHint().width())
        self.appearance_body.addWidget(self.controls_stack, 4)
        for widget in (self.add_trace_combo, self.add_trace_button, self.remove_trace_button):
            widget.hide()
        self.add_item_combo = qtw.QComboBox(self)
        self.add_item_combo.setObjectName("plotAppearanceAddCombo")
        self.add_item_combo.setMinimumContentsLength(18)
        self.add_item_combo.setSizeAdjustPolicy(
            qtw.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.add_item_combo.setToolTip("Choose a trace or heatmap to add.")
        self.add_item_button = qtw.QPushButton("Add", self)
        self.remove_item_button = qtw.QPushButton("Remove", self)
        actions = qtw.QHBoxLayout()
        actions.addWidget(self.add_item_combo, 1)
        actions.addWidget(self.add_item_button)
        actions.addWidget(self.remove_item_button)
        self.items_layout.addLayout(actions)
        self.add_item_combo.currentIndexChanged.connect(self._add_item_selection_changed)
        self.add_item_button.clicked.connect(self._add_item)
        self.remove_item_button.clicked.connect(self._remove_items)
        self.table.currentCellChanged.connect(lambda *_args: self._sync_controls_from_selection())
        self.setMinimumWidth(max(self.minimumWidth(), self.minimumSizeHint().width()))
        self.refresh_rows()

    def _row_entry(self, row: int) -> tuple[str, Any] | None:
        item = self.table.item(row, self._COL_ID)
        if item is None:
            return None
        return item.data(_KIND_ROLE), item.data(QtCore.Qt.ItemDataRole.UserRole)

    def _selected_entries(self) -> list[tuple[str, Any]]:
        return [entry for row in self._selected_rows() if (entry := self._row_entry(row)) is not None]

    def _sync_controls_from_selection(self):
        if self._building:
            return
        controls = self.__dict__.get("heatmap_controls")
        if controls is None:
            super()._sync_controls_from_selection()
            return
        selected = self._selected_entries()
        current = self._row_entry(self.table.currentRow())
        entry = current if current in selected else next(iter(selected), None)
        if entry is not None and entry[0] == "heatmap":
            self.controls_stack.setCurrentWidget(controls)
            controls._sync_controls_from_selection()
        else:
            self.controls_stack.setCurrentWidget(self.trace_controls_panel)
            super()._sync_controls_from_selection()
        self.remove_item_button.setEnabled(self._can_remove_items(selected))
        self.remove_item_button.setToolTip(self.owner.plot_item_removal_error(selected) or "")

    def _can_remove_items(self, selected):
        entries = set(self.owner._plot_item_order)
        selected = set(selected)
        return bool(selected) and selected < entries and not self.owner.plot_item_removal_error(selected)

    def _is_primary(self, kind: str, key: Any) -> bool:
        if kind == "trace":
            return self.owner.lines.get(key) is self.owner.line
        return self.owner.heatmaps.get(key) is self.owner

    def sync_swap_axes_control(self):
        super().sync_swap_axes_control()
        controls = self.__dict__.get("heatmap_controls")
        if controls is not None:
            controls.sync_swap_axes_control()

    def refresh_rows(self):
        if "heatmap_controls" not in self.__dict__:
            return
        selected = set(self._selected_entries())
        current = self._row_entry(self.table.currentRow())
        self.owner._sync_plot_item_order()
        pending = self.__dict__.get("_pending_added_entry")
        if pending is not None and pending in self.owner._plot_item_order:
            selected = {pending}
            current = pending
            self._pending_added_entry = None
        self.sync_swap_axes_control()
        self.heatmap_controls.refresh_color_scale(refresh_rows=False)
        self._building = True
        blocked = self.table.blockSignals(True)
        try:
            self.table.setRowCount(0)
            for row, (kind, key) in enumerate(self.owner._plot_item_order):
                self.table.insertRow(row)
                if kind == "trace":
                    renderer = self.owner.lines[key]
                    label = self.owner._trace_display_label(key, renderer)
                    measurement = self.owner._trace_measurement_name(key, renderer)
                    icon = self._trace_icon(self.owner._ensure_trace_styles()[key])
                else:
                    renderer = self.owner.heatmaps[key]
                    label = self.owner._heatmap_display_label(key, renderer)
                    measurement = self.owner._heatmap_measurement_name(key, renderer)
                    icon = QtGui.QIcon(self.heatmap_controls._heatmap_pixmap(renderer))
                dataset_id = label.split()[0].replace("ID:", "") if label.startswith("ID:") else str(row + 1)
                for column, value in ((self._COL_ID, dataset_id), (self._COL_MEASUREMENT, measurement)):
                    item = qtw.QTableWidgetItem(value)
                    item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
                    item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignVCenter | (
                        QtCore.Qt.AlignmentFlag.AlignRight if column == self._COL_ID
                        else QtCore.Qt.AlignmentFlag.AlignLeft
                    ))
                    item.setToolTip(label if column == self._COL_ID else f"{measurement} ({kind})")
                    self.table.setItem(row, column, item)
                id_item = self.table.item(row, self._COL_ID)
                assert id_item is not None
                id_item.setData(QtCore.Qt.ItemDataRole.UserRole, key)
                id_item.setData(_KIND_ROLE, kind)
                preview = qtw.QTableWidgetItem()
                preview.setIcon(icon)
                preview.setFlags(preview.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable
                                 & ~QtCore.Qt.ItemFlag.ItemIsDragEnabled)
                preview.setToolTip("Trace preview" if kind == "trace" else "Heatmap preview")
                self.table.setItem(row, self._COL_PREVIEW, preview)
                self.table.setRowHeight(row, 28)
                if (kind, key) in selected:
                    selection = self.table.selectionModel()
                    model = self.table.model()
                    if selection is not None and model is not None:
                        selection.select(model.index(row, 0),
                                         QtCore.QItemSelectionModel.SelectionFlag.Select
                                         | QtCore.QItemSelectionModel.SelectionFlag.Rows)
                if (kind, key) == current:
                    self.table.setCurrentItem(id_item, QtCore.QItemSelectionModel.SelectionFlag.NoUpdate)
        finally:
            self.table.blockSignals(blocked)
            self._building = False
        if self.table.rowCount() and not self._selected_entries():
            self.table.selectRow(0)
        self._sync_controls_from_selection()
        self.refresh_available_items()
        self.refresh_axis_warnings()

    def refresh_axis_warnings(self):
        if self._building or "heatmap_controls" not in self.__dict__:
            return
        style = self.style()
        if style is None:
            return
        icon = style.standardIcon(qtw.QStyle.StandardPixmap.SP_MessageBoxWarning)
        for row in range(self.table.rowCount()):
            entry = self._row_entry(row)
            if entry is None:
                continue
            kind, key = entry
            registry = self.owner.lines if kind == "trace" else self.owner.heatmaps
            if key not in registry:
                continue
            reasons = dataset_axis_warnings(self.owner, kind, key)
            item = qtw.QTableWidgetItem()
            item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable
                          & ~QtCore.Qt.ItemFlag.ItemIsDragEnabled)
            if reasons:
                item.setIcon(icon)
                item.setToolTip("Incompatible axes:\n" + "\n".join(reasons))
                item.setData(QtCore.Qt.ItemDataRole.AccessibleTextRole, item.toolTip())
            self.table.setItem(row, self._COL_WARNING, item)

    def _sync_plot_order_from_rows(self):
        self.owner._sync_plot_item_order()

    def _move_rows_to_position(self, source_rows, destination_row):
        entries = list(self.owner._plot_item_order)
        source_rows = sorted({row for row in source_rows if 0 <= row < len(entries)})
        selected = [entries[row] for row in source_rows]
        destination_row = max(0, min(destination_row, len(entries)))
        destination_row -= sum(row < destination_row for row in source_rows)
        remaining = [entry for row, entry in enumerate(entries) if row not in source_rows]
        self.owner._plot_item_order = remaining[:destination_row] + selected + remaining[destination_row:]
        for kind, registry in (("trace", self.owner.lines), ("heatmap", self.owner.heatmaps)):
            reordered = [(key, registry[key]) for entry_kind, key in self.owner._plot_item_order if entry_kind == kind]
            registry.clear()
            registry.update(reordered)
        self.owner._sync_mixed_axis_labels()
        self.owner._sync_heatmap_colorbar_items()
        self.refresh_rows()

    def select_heatmap(self, key: Any) -> bool:
        return self.heatmap_controls.select_heatmap(key)

    def refresh_color_scale(self, *, refresh_rows=True):
        self.heatmap_controls.refresh_color_scale(refresh_rows=refresh_rows)

    def refresh_available_traces(self):
        super().refresh_available_traces()
        if "add_item_combo" in self.__dict__:
            self.refresh_available_items()

    def refresh_available_items(self):
        previous = self.add_item_combo.currentData()
        blocked = self.add_item_combo.blockSignals(True)
        try:
            self.add_item_combo.clear()
            self.add_item_combo.addItem("Select a measurement to add…", None)
            for kind, provider, registry in (
                ("trace", self.owner.available_trace_candidates, self.owner.lines),
                ("heatmap", self.owner.available_heatmap_candidates, self.owner.heatmaps),
            ):
                for label, key in provider():
                    if key not in registry:
                        self.add_item_combo.addItem(f"{label} ({kind})", (kind, key, label))
            self.add_item_combo.setCurrentIndex(max(0, self.add_item_combo.findData(previous)))
        finally:
            self.add_item_combo.blockSignals(blocked)
        self._add_item_selection_changed()

    def _add_item_selection_changed(self, *_args):
        self.add_item_button.setEnabled(self.add_item_combo.currentData() is not None)

    def _add_item(self, *_args):
        entry = self.add_item_combo.currentData()
        if entry is None:
            return
        kind, key, label = entry
        self._pending_added_entry = (kind, key)
        try:
            if kind == "trace":
                self.owner.add_trace_from_dialog(label, key)
            else:
                self.owner.add_heatmap_from_dialog(label, key)
        except Exception as error:
            self._pending_added_entry = None
            qtw.QMessageBox.critical(self, "Could Not Add Dataset", f"The selected dataset could not be added:\n{error}")
        self.refresh_rows()
        if kind == "trace":
            self.select_trace(key)
        else:
            self.select_heatmap(key)

    def _remove_items(self, *_args):
        selected = self._selected_entries()
        if not self._can_remove_items(selected):
            return
        try:
            owner = self.owner.remove_plot_items(selected)
        except Exception as error:
            qtw.QMessageBox.critical(self, "Could Not Remove Dataset", str(error))
            return
        if owner is self.owner:
            self.refresh_rows()
