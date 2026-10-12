"""Shared trace and heatmap ownership for plots containing both data types."""

from typing import TYPE_CHECKING, Any

from PyQt6 import QtGui

from ._mixed_appearance import PlotAppearanceDialog
from ._plot1d_traces import Plot1DTraceMixin
from ._plot2d_colorbar import Plot2DColorbarMixin
from ._plot2d_layers import Plot2DLayerMixin
from ._plot_data_viewbox import PlotDataViewBox, coordinate_viewbox
from ._plot_drop_axes import plot_axis_entries

if TYPE_CHECKING:
    from ._plot_axis_scaling import PlotAxisScalingMixin

    class _MixedPlotBase(PlotAxisScalingMixin):
        operation_kind: str

        def apply_axis_major_tick_count_preference(self) -> None: ...
        def _update_plot_annotation(self) -> None: ...
        def _add_plot_overlay(self, item: Any, viewbox: Any = None) -> None: ...
        def _marquee_viewbox(self) -> Any: ...
else:
    class _MixedPlotBase:
        pass


class MixedPlotMixin(Plot1DTraceMixin, Plot2DLayerMixin, Plot2DColorbarMixin, _MixedPlotBase):
    def initMenu(self):
        super().initMenu()
        menu_bar = self.menuBar()
        for menu_action in menu_bar.actions() if menu_bar is not None else ():
            if menu_action.text().replace("&", "") == "View":
                menu = menu_action.menu()
                if menu is not None:
                    menu.addSeparator()
                    action = QtGui.QAction("Appearance…", self)
                    action.triggered.connect(lambda _checked: self.open_plot_appearance_dialog())
                    menu.addAction(action)
                    self.appearance_action = action
                break

    def open_plot_appearance_dialog(self, key=None, *, kind=None):
        dialog = self.__dict__.get("_plot_appearance_dialog")
        if dialog is None:
            if "heatmaps" not in self.__dict__:
                self._init_heatmap_layers()
            dialog = PlotAppearanceDialog(self)
            self._plot_appearance_dialog = dialog
            self._trace_appearance_dialog = dialog
            self._heatmap_appearance_dialog = dialog
        self.get_mergables.emit()
        dialog.refresh_rows()
        if key is not None:
            if kind == "heatmap":
                dialog.select_heatmap(key)
            else:
                dialog.select_trace(key)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def open_trace_appearance_dialog(self, trace_key=None):
        self.open_plot_appearance_dialog(trace_key, kind="trace")

    def open_heatmap_appearance_dialog(self, heatmap_key=None):
        self.open_plot_appearance_dialog(heatmap_key, kind="heatmap")

    def add_line(self, label, trace_key=None, *, axis_plan=None):
        super().add_line(label, trace_key, axis_plan=axis_plan)
        self._sync_plot_item_order()
        dialog = self.__dict__.get("_plot_appearance_dialog")
        if dialog is not None:
            dialog.refresh_rows()

    def remove_line(self, label, trace_key=None):
        key = trace_key if trace_key is not None else label
        line = self.__dict__.get("lines", {}).get(key)
        if line is self.__dict__.get("line") and line is not None:
            self.remove_plot_items([("trace", key)])
            return
        if line is not None:
            self._restore_data_coordinate_parent(line)
        super().remove_line(label, trace_key)
        self._sync_plot_item_order()
        self._sync_mixed_axis_labels()

    def remove_heatmap(self, label="", trace_key=None):
        key = trace_key
        if key is None and label == self.__dict__.get("label"):
            key = self.__dict__.get("_primary_heatmap_key")
        if self.__dict__.get("heatmaps", {}).get(key) is self:
            return self.remove_plot_items([("heatmap", key)]) is not self
        return super().remove_heatmap(label, trace_key)

    def remove_plot_items(self, selected):
        self._sync_plot_item_order()
        entries = set(self._plot_item_order)
        if not set(selected) or not set(selected) < entries:
            return self
        removal_error = self.plot_item_removal_error(selected)
        if removal_error:
            self.show_status(removal_error, 5000)
            return self
        primary_selected = any(
            (self.lines.get(key) is self.__dict__.get("line") if kind == "trace"
             else self.heatmaps.get(key) is self)
            for kind, key in selected
        )
        if primary_selected:
            return self.__dict__["_plot_source_replacement_request"](selected)
        for kind, key in selected:
            if kind == "trace":
                self.remove_line(self._trace_display_label(key, self.lines[key]), key)
            else:
                self.remove_heatmap(trace_key=key)
        return self

    def plot_item_removal_error(self, selected):
        """Cuts may remain overlays, but cannot own a mixed plot's controls."""
        selected = set(selected)
        primary_selected = any(
            (self.lines.get(key) is self.__dict__.get("line") if kind == "trace"
             else self.heatmaps.get(key) is self)
            for kind, key in selected
        )
        if primary_selected:
            for kind, key in self._plot_item_order:
                if (kind, key) in selected:
                    continue
                item = self.lines[key] if kind == "trace" else self.heatmaps[key]
                source = item.from_win
                if source.operation_kind in {"plot1d", "plot2d"}:
                    return None
            return "Keep a full line or heatmap dataset in this plot; cuts cannot be its primary source."
        return None

    def _add_plot_overlay(self, item, viewbox=None):
        if viewbox is None:
            viewbox = self._marquee_viewbox()
        super()._add_plot_overlay(item, coordinate_viewbox(viewbox))

    def _sync_plot_item_order(self):
        lines = self.__dict__.get("lines", {})
        heatmaps = self.__dict__.get("heatmaps", {})
        existing = [("trace", key) for key in lines] + [("heatmap", key) for key in heatmaps]
        order = [entry for entry in self.__dict__.get("_plot_item_order", []) if entry in existing]
        for entry in existing:
            if entry in order:
                continue
            order.insert(0, entry)
        self._plot_item_order = order
        styles = self._ensure_trace_styles()
        renderers = {}
        for index, (kind, key) in enumerate(order):
            z = len(order) - index - 1
            if kind == "trace":
                styles.setdefault(key, self._initial_trace_style()).order = z
                line = lines[key]
                set_z = getattr(line, "setZValue", None)
                if callable(set_z):
                    set_z(z)
                if "vb" in self.__dict__ and line is not None:
                    renderers[(kind, key)] = (self._trace_axis_viewbox(styles[key]), [line])
            else:
                layer = heatmaps[key]
                items = self._heatmap_render_items(layer)
                for item in items:
                    item.setZValue(z)
                renderers[(kind, key)] = (self._heatmap_axis_viewbox(layer), items)
        self._sync_data_drawing_layers(renderers)
        self._update_plot_annotation()

    def _sync_data_drawing_layers(self, renderers):
        layers = self.__dict__.get("_plot_data_viewboxes", {})
        # A single axis pair already has a common graphics parent. Allocate
        # drawing layers only when independent axis pairs need to interleave.
        if not layers and len({owner for owner, _items in renderers.values()}) < 2:
            return
        self._plot_data_viewboxes = layers
        for entry in list(layers):
            if entry not in renderers or layers[entry].coordinate_owner is not renderers[entry][0]:
                layer = layers.pop(entry)
                # Axis reassignment may already have moved these items. Keep
                # any remaining renderers alive while replacing their layer.
                for item in list(layer.childGroup.childItems()):
                    item.setParentItem(layer.coordinate_owner.childGroup)
                layer.detach()
        count = len(renderers)
        for index, (entry, (owner, items)) in enumerate(renderers.items()):
            layer = layers.get(entry)
            if layer is None:
                layer = layers[entry] = PlotDataViewBox(self.vb, owner)
            layer.sync_view()
            # Negative Z keeps cursor/marquee decorations in the primary
            # childGroup above every data layer, regardless of item count.
            layer.setZValue(-1 + (count - index) / (count + 1))
            for item in items:
                if item.parentItem() is not layer.childGroup:
                    item.setParentItem(layer.childGroup)

    @staticmethod
    def _restore_data_coordinate_parent(item):
        get_viewbox = getattr(item, "getViewBox", None)
        current = get_viewbox() if callable(get_viewbox) else None
        if isinstance(current, PlotDataViewBox):
            item.setParentItem(current.coordinate_owner.childGroup)

    def _move_trace_to_axis_viewbox(self, line, target):
        current = line.getViewBox()
        if coordinate_viewbox(current) is target:
            return
        self._restore_data_coordinate_parent(line)
        super()._move_trace_to_axis_viewbox(line, target)

    def _move_heatmap_item_to_viewbox(self, item, target):
        get_viewbox = getattr(item, "getViewBox", None)
        current = get_viewbox() if callable(get_viewbox) else None
        if coordinate_viewbox(current) is target:
            return
        self._restore_data_coordinate_parent(item)
        super()._move_heatmap_item_to_viewbox(item, target)

    def _remove_heatmap_render_item(self, item):
        self._restore_data_coordinate_parent(item)
        super()._remove_heatmap_render_item(item)

    def _init_heatmap_layers(self):
        super()._init_heatmap_layers()
        self._sync_plot_item_order()

    def _sync_heatmap_layer_order(self):
        super()._sync_heatmap_layer_order()
        self._sync_plot_item_order()

    def _set_layer_visibility(self, layer, visible):
        super()._set_layer_visibility(layer, visible)
        self._update_plot_annotation()

    def _set_layer_opacity(self, layer, opacity):
        super()._set_layer_opacity(layer, opacity)
        self._update_plot_annotation()

    def can_swap_plot_axes(self):
        mixin = Plot1DTraceMixin if self.operation_kind == "plot1d" else Plot2DLayerMixin
        return mixin.can_swap_plot_axes(self)

    def plot_axes_swapped(self):
        mixin = Plot1DTraceMixin if self.operation_kind == "plot1d" else Plot2DLayerMixin
        return mixin.plot_axes_swapped(self)

    def set_plot_axes_swapped(self, swapped):
        before = self.axis_options
        mixin = Plot1DTraceMixin if self.operation_kind == "plot1d" else Plot2DLayerMixin
        result = mixin.set_plot_axes_swapped(self, swapped)
        if result and before != self.axis_options:
            if self.operation_kind == "plot1d":
                Plot2DLayerMixin._transpose_heatmap_axis_assignments(self)
            else:
                Plot1DTraceMixin._transpose_trace_axis_assignments(self)
            self.refresh_secondary_lines()
            self.refresh_secondary_heatmaps()
        return result

    def _sync_mixed_axis_labels(self):
        self._update_plot_annotation()
        plot = self.__dict__.get("plot")
        if plot is None:
            return
        if self.operation_kind == "plot1d" and not self.__dict__.get("heatmaps"):
            Plot1DTraceMixin._sync_trace_axis_labels(self)
            self._sync_unused_plot_axes()
            self._refresh_appearance_axis_warnings()
            return
        parameters: dict[str, Any] = {}
        for side, _identity, parameter in plot_axis_entries(self):
            parameters.setdefault(side, parameter)
        for side in ("Bottom", "Left", "Top", "Right"):
            parameter = parameters.get(side)
            axis = plot.getAxis(side.lower())
            axis.setStyle(showValues=side in parameters)
            label = getattr(parameter, "label", None) or getattr(parameter, "name", "")
            axis.setLabel(text=str(label), units=str(getattr(parameter, "unit", "") or ""))
        self._sync_unused_plot_axes()
        sync_tabs = getattr(self, "_sync_axis_scale_tab_states", None)
        if callable(sync_tabs):
            sync_tabs()
        self._refresh_appearance_axis_warnings()

    def _sync_unused_plot_axes(self):
        """Keep unassigned axes linked to the opposite assigned coordinate range."""
        plot = self.__dict__.get("plot")
        primary = self.__dict__.get("vb")
        if plot is None or primary is None:
            return
        occupied = {side for side, _identity, _parameter in plot_axis_entries(self)}
        owners = {
            "Bottom": primary,
            "Left": primary,
            "Top": self.__dict__.get("top_vb") or primary,
            "Right": self.__dict__.get("right_vb") or primary,
        }
        for side, opposite in (("Bottom", "Top"), ("Top", "Bottom"),
                               ("Left", "Right"), ("Right", "Left")):
            axis = plot.getAxis(side.lower())
            owner = owners[side] if side in occupied else owners[opposite]
            if axis.linkedView() is not owner:
                axis.linkToView(owner)
                axis.linkedViewChanged(owner)
            axis.setStyle(showValues=side in occupied)
            if side not in occupied:
                axis.setLabel(text="", units="")

    def _refresh_appearance_axis_warnings(self):
        dialog = self.__dict__.get("_plot_appearance_dialog")
        if dialog is not None:
            dialog.refresh_axis_warnings()

    def _sync_trace_axis_labels(self):
        self._sync_mixed_axis_labels()

    def _sync_heatmap_axis_visibility(self):
        self._sync_mixed_axis_labels()

    def _sync_vertical_axis_visibility(self, side):
        if self.operation_kind == "plot1d" and not self.__dict__.get("heatmaps"):
            Plot1DTraceMixin._sync_vertical_axis_visibility(self, side)
            self._sync_unused_plot_axes()
        else:
            self._sync_mixed_axis_labels()

    def _sync_top_axis_visibility(self):
        if self.operation_kind == "plot1d" and not self.__dict__.get("heatmaps"):
            Plot1DTraceMixin._sync_top_axis_visibility(self)
            self._sync_unused_plot_axes()
        else:
            self._sync_mixed_axis_labels()

    def _apply_trace_style(self, key, line):
        super()._apply_trace_style(key, line)
        self._sync_plot_item_order()

    def _ensure_trace_axis_viewboxes(self, *, top=False, right=False):
        if self.operation_kind == "plot2d":
            Plot2DLayerMixin._ensure_heatmap_axis_viewboxes(self, top=top, right=right)
        else:
            Plot1DTraceMixin._ensure_trace_axis_viewboxes(self, top=top, right=right)

    def _ensure_heatmap_axis_viewboxes(self, *, top=False, right=False):
        self._ensure_trace_axis_viewboxes(top=top, right=right)
        if self.operation_kind == "plot1d":
            for name in ("right_vb", "top_vb", "top_right_vb"):
                viewbox = self.__dict__.get(name)
                if viewbox is not None and not getattr(viewbox, "_qplot_heatmap_range_connected", False):
                    viewbox.sigRangeChanged.connect(self._heatmap_overlay_range_changed)
                    viewbox._qplot_heatmap_range_connected = True

    def _axis_scale_bound_item_groups(self, axis):
        groups: dict[Any, list[Any]] = {}
        for viewbox, items in super()._axis_scale_bound_item_groups(axis):
            if isinstance(viewbox, PlotDataViewBox):
                viewbox.sync_view()
            if items is not None:
                groups.setdefault(viewbox, []).extend(items)
        dimension = self._axis_scale_dimension(axis)
        side = {"x": "Bottom", "x2": "Top", "y": "Left", "y2": "Right"}[axis]
        # The base implementation chooses traces when that registry exists.
        # Include heatmaps as well so axis autoscaling sees all assigned data.
        if isinstance(self.__dict__.get("lines"), dict):
            for layer in self.__dict__.get("heatmaps", {}).values():
                x_side, y_side = self._heatmap_axis_sides(layer)
                if (x_side if dimension == "x" else y_side) == side:
                    for item in self._heatmap_render_items(layer):
                        viewbox = item.getViewBox() or self._heatmap_axis_viewbox(layer)
                        if isinstance(viewbox, PlotDataViewBox):
                            viewbox.sync_view()
                        groups.setdefault(viewbox, []).append(item)
        return list(groups.items())

    def _axis_scale_axis_is_used(self, axis):
        if super()._axis_scale_axis_is_used(axis):
            return True
        dimension = "x" if axis in ("x", "x2") else "y"
        side = {"x": "Bottom", "x2": "Top", "y": "Left", "y2": "Right"}[axis]
        return any(assignment.get(dimension) == side for assignment in
                   self.__dict__.get("_heatmap_axis_assignments", {}).values())

    def _apply_heatmap_axis_assignment(self, layer, *, auto_range=True):
        super()._apply_heatmap_axis_assignment(layer, auto_range=auto_range)
        x_side, y_side = self._heatmap_axis_sides(layer)
        for axis in ("x2" if x_side == "Top" else "x", "y2" if y_side == "Right" else "y"):
            # Heatmap geometry uses physical coordinates. A shared axis must
            # stay linear, including when a heatmap is added to a log plot.
            if self._axis_scale_log_mode(axis):
                self._axis_scale_log_toggled(axis, False)
        self._sync_plot_item_order()

    def _colorbar_display_parameter(self):
        if self.operation_kind == "plot2d":
            return self.__dict__.get("display_param", self.__dict__.get("param"))
        layer = next(iter(self.__dict__.get("heatmaps", {}).values()), None)
        if layer is None:
            return None
        source = layer.from_win
        return getattr(source, "display_param", None) or source.param

    def _sync_heatmap_colorbar_items(self, *, rescale=False):
        self._update_plot_annotation()
        if self.operation_kind == "plot1d":
            layers = self.__dict__.get("heatmaps", {})
            if not layers:
                bar = self.__dict__.get("bar")
                if bar is not None:
                    bar.hide()
                return
            if "bar" not in self.__dict__ and self._data_colorbar_levels() is not None:
                self._colorbar_manual_levels = None
                self._init_colorbar_scale_controls()
                items = self._heatmap_colorbar_items()
                self.bar = self.plot.addColorBar(
                    items, colorMap=self._colorbar_colormap(),
                    rounding=self._data_colorbar_rounding(), width=self._configured_colorbar_width(),
                    colorMapMenu=False,
                )
                self._heatmap_colorbar_item_ids = tuple(id(item) for item in items)
                self._set_colorbar_tick_formatter()
                for key, line in self.__dict__.get("lines", {}).items():
                    self._apply_trace_style(key, line)
                self.config.theme.style_plotItem(self)
                self.apply_axis_major_tick_count_preference()
                action = QtGui.QAction("Color scale…", self)
                action.triggered.connect(self.open_colorbar_scale_dialog)
                menu_bar = self.menuBar()
                for menu_action in menu_bar.actions() if menu_bar is not None else ():
                    if menu_action.text().replace("&", "") == "View":
                        menu = menu_action.menu()
                        if menu is not None:
                            menu.addAction(action)
                        break
            bar = self.__dict__.get("bar")
            if bar is not None:
                bar.show()
        super()._sync_heatmap_colorbar_items(rescale=rescale)
        self._refresh_appearance_axis_warnings()

    def _reset_heatmap_hover(self):
        # A line-primary plot has no heatmap hover/cut decorations.
        pass

    def _set_sweep_lines_visible(self, visible):
        pass

    def closeEvent(self, event: Any):
        self.close_secondary_heatmaps()
        super().closeEvent(event)
        for layer in self.__dict__.get("_plot_data_viewboxes", {}).values():
            for item in list(layer.childGroup.childItems()):
                item.setParentItem(layer.coordinate_owner.childGroup)
            layer.detach()
        self._plot_data_viewboxes = {}
