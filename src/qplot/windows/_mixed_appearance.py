"""One appearance list with controls for the selected renderer type."""

from typing import TYPE_CHECKING, Any

from PyQt6 import QtCore, QtGui, sip
from PyQt6 import QtWidgets as qtw

from ._appearance_layout import (
    AppearanceControlsStack,
    build_appearance_panes,
    fit_appearance_dialog,
    resize_appearance_panes,
)
from ._plot1d_traces import _TraceAppearanceDialog
from ._plot2d_layers import _HeatmapAppearanceControls
from ._plot_appearance import CenteredPreviewDelegate, reordered_rows
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
        self._session_active = False
        self._session_finishing = False
        self._preview_removed = {}
        self._preview_owners = []
        self.appearance_buttons.removeButton(self.revert_button)
        self.revert_button.hide()
        self.appearance_buttons.setStandardButtons(
            qtw.QDialogButtonBox.StandardButton.Ok | qtw.QDialogButtonBox.StandardButton.Cancel,
        )
        self.appearance_buttons.rejected.disconnect()
        self.appearance_buttons.rejected.connect(self.reject)
        self.appearance_buttons.accepted.connect(self.accept)
        self.appearance_buttons.button(qtw.QDialogButtonBox.StandardButton.Ok).setToolTip(
            "Keep all changes made in this dialog."
        )
        self.appearance_buttons.button(qtw.QDialogButtonBox.StandardButton.Cancel).setToolTip(
            "Discard all changes made in this dialog."
        )
        for button in self.appearance_buttons.buttons():
            button.setAutoDefault(False)
        self.setWindowTitle("Plot appearance")
        self.table.setObjectName("plotAppearanceTable")
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Run", "Preview", "Measurement", ""])
        self.table.setItemDelegateForColumn(
            self._COL_WARNING,
            CenteredPreviewDelegate(self.table, icon_size=QtCore.QSize(18, 18)),
        )
        header = self.table.horizontalHeader()
        if header is not None:
            header.setStretchLastSection(False)
            for column in (self._COL_ID, self._COL_PREVIEW):
                header.setSectionResizeMode(column, qtw.QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(self._COL_MEASUREMENT, qtw.QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(self._COL_WARNING, qtw.QHeaderView.ResizeMode.Fixed)
            header.resizeSection(self._COL_WARNING, 28)
        self.table.setToolTip("Data at the top is drawn on top. Drag rows to change the drawing order.")
        self.setStyleSheet(self.styleSheet().replace("traceAppearanceTable", "plotAppearanceTable"))
        self.heatmap_controls = _HeatmapControls(self)
        self.controls_stack = AppearanceControlsStack(self)
        self.controls_stack.setObjectName("plotAppearanceControls")
        self.appearance_body.removeWidget(self.trace_controls_panel)
        self.controls_stack.addWidget(self.trace_controls_panel)
        self.controls_stack.addWidget(self.heatmap_controls)
        self.appearance_body.addWidget(self.controls_stack, 4)
        for widget in (self.add_trace_combo, self.add_trace_button, self.remove_trace_button):
            widget.hide()
        self.add_item_combo = qtw.QComboBox(self)
        self.add_item_combo.setObjectName("plotAppearanceAddCombo")
        self.add_item_combo.setMinimumContentsLength(12)
        self.add_item_combo.setMinimumWidth(60)
        self.add_item_combo.setSizePolicy(qtw.QSizePolicy.Policy.Ignored, qtw.QSizePolicy.Policy.Fixed)
        self.add_item_combo.setSizeAdjustPolicy(
            qtw.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.add_item_combo.setToolTip("Choose a 1D, 2D or 3D dataset to add.")
        self.add_item_button = qtw.QPushButton("Add", self)
        self.add_item_button.setToolTip("Add the selected dataset to this plot.")
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
        self._build_whole_plot_controls()
        build_appearance_panes(self)
        self.table.setMinimumHeight(56)
        self.setMinimumSize(350, 300)
        self._geometry_initialised = False
        self.refresh_rows()

    def _build_whole_plot_controls(self):
        self.whole_plot_group = qtw.QGroupBox("Whole plot")
        self.whole_plot_group.setObjectName("plotAppearanceWholePlot")
        layout = qtw.QVBoxLayout(self.whole_plot_group)
        layout.setContentsMargins(8, 12, 8, 8)
        controls = self.heatmap_controls
        controls.color_scale_group.hide()
        self.global_colour_row = qtw.QWidget()
        colours = qtw.QHBoxLayout(self.global_colour_row)
        colours.setContentsMargins(0, 0, 0, 0)
        colours.setSpacing(8)
        colours.addWidget(qtw.QLabel("Colour map"))
        colours.addWidget(controls.color_scale_button, 1)
        layout.addWidget(self.global_colour_row)
        self.plot_axes_group.hide()
        controls.plot_axes_group.hide()
        controls.swap_axes.toggled.disconnect(controls._swap_axes_toggled)
        controls.swap_axes.hide()
        controls.swap_axes = self.swap_axes
        layout.addWidget(self.swap_axes)

    def _sync_whole_plot_controls(self):
        group = self.__dict__.get("whole_plot_group")
        if group is None:
            return
        self.global_colour_row.setVisible(any(
            kind == "heatmap" and (kind, key) not in self._preview_removed
            for kind, key in self.owner._plot_item_order
        ))
        self.sync_swap_axes_control()

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
        self._sync_whole_plot_controls()
        selected = self._selected_entries()
        if not selected:
            super()._sync_controls_from_selection()
            controls._update_control_enabled_states(False)
            self.remove_item_button.setEnabled(False)
            self.remove_item_button.setToolTip("Select a dataset to remove it.")
            return
        current = self._row_entry(self.table.currentRow())
        entry = current if current in selected else next(iter(selected), None)
        if entry is not None and entry[0] == "heatmap":
            self.controls_stack.setCurrentWidget(controls)
            controls._sync_controls_from_selection()
        else:
            self.controls_stack.setCurrentWidget(self.trace_controls_panel)
            super()._sync_controls_from_selection()
        kind = entry[0] if entry else "trace"
        count = sum(selected_kind == kind for selected_kind, _key in selected)
        feedback = getattr(controls, "selection_feedback", "") if kind == "heatmap" else self.selection_feedback
        if count == 1 and entry is not None:
            _kind, key = entry
            registry = self.owner.heatmaps if kind == "heatmap" else self.owner.lines
            measurement_name = (self.owner._heatmap_measurement_name if kind == "heatmap"
                                else self.owner._trace_measurement_name)(key, registry[key])
            summary = f"{measurement_name} — {kind}"
        else:
            summary = f"{count} {kind}s selected"
        self.selection_summary.setText(
            summary
            + (f" (of {len(selected)} datasets)" if count != len(selected) else "")
            + (" · Mixed values" if feedback else "")
        )
        self.selection_summary.setToolTip(
            summary + "\n"
            + (feedback + "\n" if feedback else "")
            + ("Other selected dataset types are unaffected." if count != len(selected) else "")
        )
        self.remove_item_button.setEnabled(self._can_remove_items(selected))
        self.remove_item_button.setToolTip(
            self.owner.plot_item_removal_error(selected)
            or "Remove the selected secondary datasets from this plot."
        )

    def _can_remove_items(self, selected):
        entries = set(self.owner._plot_item_order) - self._preview_removed.keys()
        selected = set(selected)
        return bool(selected) and selected < entries and not self.owner.plot_item_removal_error(
            selected | self._preview_removed.keys(),
        )

    def showEvent(self, event):
        super().showEvent(event)
        buttons = self.appearance_buttons.buttons()
        button_width = max(button.sizeHint().width() for button in buttons)
        for button in buttons:
            button.setFixedWidth(button_width)
        if not self._session_active and not self._session_finishing:
            self._session_root = self.owner
            self._action_token = {"cancelled": False}
            self.owner._appearance_action_token = self._action_token
            self._preview_owners = [self.owner]
            self._preview_removed = {}
            self._pending_added_entry = None
            self._session_active = True
        if not self._geometry_initialised:
            self._geometry_initialised = True
            fit_appearance_dialog(self)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        resize_appearance_panes(self)

    @staticmethod
    def _entry_identity(owner, entry):
        kind, key = entry
        item = (owner.lines if kind == "trace" else owner.heatmaps)[key]
        source = owner if item is owner or item is owner.__dict__.get("line") else item.from_win
        return kind, source._dataset_key, source.param.name

    def _bind_owner(self, owner):
        for window in self._preview_owners:
            for name in ("_plot_appearance_dialog", "_trace_appearance_dialog", "_heatmap_appearance_dialog"):
                if window.__dict__.get(name) is self:
                    window.__dict__[name] = None
        self.owner = owner
        owner._appearance_action_token = self._action_token
        self.heatmap_controls.owner = owner
        self.setParent(owner, self.windowFlags())
        for name in ("_plot_appearance_dialog", "_trace_appearance_dialog", "_heatmap_appearance_dialog"):
            owner.__dict__[name] = self

    def adopt_preview(self, replacement):
        """Retain the original source while a volume previews new controls."""
        removed = {self._entry_identity(self.owner, entry): visible
                   for entry, visible in self._preview_removed.items()}
        self.owner._appearance_preview_hidden = True
        self.owner.hide()
        self._preview_owners.append(replacement)
        self._bind_owner(replacement)
        self._preview_removed = {entry: removed[self._entry_identity(replacement, entry)]
                                 for entry in replacement._plot_item_order
                                 if self._entry_identity(replacement, entry) in removed}
        self.refresh_rows()
        self.show()
        self.raise_()

    def _close_colour_editor(self):
        picker = self.heatmap_controls.colour_map_dialog
        if picker is not None and not sip.isdeleted(picker) and picker.isVisible():
            picker.reject()
        for owner in self._preview_owners:
            dialog = owner.__dict__.get("colorbar_scale_dialog")
            if dialog is not None and not sip.isdeleted(dialog):
                dialog.close()

    def accept(self):
        if not self._session_active:
            super().accept()
            return
        self._close_colour_editor()
        self._session_finishing = True
        self.hide()  # Source promotion must not reopen an already accepted editor.
        removed = dict(self._preview_removed)
        self._preview_removed.clear()
        try:
            if removed:
                self.owner.remove_plot_items(list(removed))
        except Exception as error:
            self._preview_removed = removed
            self._session_finishing = False
            self.show()
            qtw.QMessageBox.critical(self, "Could Not Apply Plot Appearance", str(error))
            return
        self._session_active = False
        for window in self._preview_owners:
            window._appearance_action_token = None
        self._preview_removed.clear()
        for window in self._preview_owners[:-1]:
            window._appearance_preview_hidden = False
            window.close()
        super().accept()
        self._session_finishing = False

    def reject(self):
        if self._session_active and not self._session_finishing:
            self._session_finishing = True
            self._action_token["cancelled"] = True
            for window in self._preview_owners:
                window._appearance_action_token = None
            self._close_colour_editor()
            self._session_active = False
            self._pending_added_entry = None
            self._preview_removed.clear()
            root = self._session_root
            if not root.__dict__.get("_closed", False) and not root.__dict__.get("_appearance_closing", False):
                self._bind_owner(root)
                root._appearance_action_token = None
                extras = [entry for entry in root._plot_item_order if entry not in self._opening_order]
                if extras:
                    root.remove_plot_items(extras)
                self.revert_appearance()
                root._appearance_preview_hidden = False
                if len(self._preview_owners) > 1:
                    root.__dict__.pop("_preview_drop_replacement", None)
                    root.show()
            for window in self._preview_owners[1:]:
                if not window.__dict__.get("_appearance_closing", False):
                    window.close()
            self._session_finishing = False
        super().reject()

    def closeEvent(self, event):
        self.reject()
        event.accept()

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
        select_first = self.table.rowCount() == 0
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
                if (kind, key) in self._preview_removed:
                    continue
                row = self.table.rowCount()
                self.table.insertRow(row)
                if kind == "trace":
                    renderer = self.owner.lines[key]
                    label = self.owner._trace_display_label(key, renderer)
                    measurement = self.owner._trace_measurement_name(key, renderer)
                    trace_style = self.owner._ensure_trace_styles()[key]
                    visible = trace_style.visible
                    icon = self._trace_icon(trace_style)
                else:
                    renderer = self.owner.heatmaps[key]
                    label = self.owner._heatmap_display_label(key, renderer)
                    measurement = self.owner._heatmap_measurement_name(key, renderer)
                    visible = self.owner._heatmap_is_visible(renderer)
                    icon = QtGui.QIcon(self.heatmap_controls._heatmap_pixmap(renderer))
                visibility_tip = "\nHidden" if not visible else ""
                dataset_id = label.split()[0].replace("ID:", "") if label.startswith("ID:") else str(row + 1)
                for column, value in ((self._COL_ID, dataset_id), (self._COL_MEASUREMENT, measurement)):
                    item = qtw.QTableWidgetItem(value)
                    item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
                    item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignVCenter | (
                        QtCore.Qt.AlignmentFlag.AlignRight if column == self._COL_ID
                        else QtCore.Qt.AlignmentFlag.AlignLeft
                    ))
                    item.setToolTip((label if column == self._COL_ID else f"{measurement} ({kind})")
                                    + visibility_tip)
                    if column == self._COL_MEASUREMENT and not visible:
                        font = item.font()
                        font.setStrikeOut(True)
                        item.setFont(font)
                        item.setData(QtCore.Qt.ItemDataRole.AccessibleDescriptionRole, "Hidden")
                    self.table.setItem(row, column, item)
                id_item = self.table.item(row, self._COL_ID)
                assert id_item is not None
                id_item.setData(QtCore.Qt.ItemDataRole.UserRole, key)
                id_item.setData(_KIND_ROLE, kind)
                preview = qtw.QTableWidgetItem()
                preview.setIcon(icon)
                preview.setFlags(preview.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable
                                 & ~QtCore.Qt.ItemFlag.ItemIsDragEnabled)
                preview.setToolTip(("Trace preview" if kind == "trace" else "Heatmap preview")
                                   + visibility_tip)
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
        if select_first and self.table.rowCount() and not self._selected_entries():
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
        entries = [entry for entry in self.owner._plot_item_order if entry not in self._preview_removed]
        self.owner._plot_item_order = reordered_rows(entries, source_rows, destination_row)
        self.owner._plot_item_order.extend(self._preview_removed)
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
            self.add_item_combo.addItem("Choose measurement to add…", None)
            for kind, key in self._preview_removed:
                registry = self.owner.lines if kind == "trace" else self.owner.heatmaps
                label = (self.owner._trace_display_label if kind == "trace" else self.owner._heatmap_display_label)(key, registry[key])
                self.add_item_combo.addItem(f"{label} ({kind})", (kind, key, label))
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
        if (kind, key) in self._preview_removed:
            visible = self._preview_removed.pop((kind, key))
            self._set_preview_visibility(kind, key, visible)
            self.refresh_rows()
            return
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
        for kind, key in selected:
            self._preview_removed[(kind, key)] = (
                self.owner._trace_styles[key].visible if kind == "trace"
                else self.owner._heatmap_is_visible(self.owner.heatmaps[key])
            )
            self._set_preview_visibility(kind, key, False)
        self.refresh_rows()

    def _set_preview_visibility(self, kind, key, visible):
        if kind == "trace":
            self.owner._trace_styles[key].visible = visible
            self.owner._apply_trace_style(key, self.owner.lines[key])
        else:
            self.owner._set_layer_visibility(self.owner.heatmaps[key], visible)
