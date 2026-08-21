"""Secondary heatmap layers for :mod:`qplot.windows.plot2d`.

The primary heatmap remains owned by ``plot2d``.  This module supplies the
renderers and source-window lifecycle needed to place additional compatible
heatmaps in the same view, plus lightweight controls for opacity and removal.
All layers attached to one host deliberately share its colour map and levels.
"""

from __future__ import annotations

from typing import Any, cast

import numpy as np
import pyqtgraph as pg
from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

from qplot.tools.heatmap_geometry import (
    HeatmapGeometry,
    canonicalize_heatmap_data,
)

from ._colorbar import (
    _COLORBAR_COLORMAP_LABELS,
    _colorbar_colormap_for_name,
    _colorbar_colormap_preview,
)
from ._plot_appearance import ReorderAppearanceTable, configure_appearance_table
from ._plot_refresh import plot_refresh_required

DEFAULT_OVERLAY_OPACITY = 0.65
_PRIMARY_HEATMAP_KEY = "__qplot_primary_heatmap__"
_FIRST_OVERLAY_Z_VALUE = 0.1
_OVERLAY_Z_VALUE_STEP = 0.01
_MAX_OVERLAY_Z_VALUE = 0.89


def _heatmap_axis_order(
    parent_options: dict[str, str],
    source_options: dict[str, str],
) -> tuple[str, str] | None:
    """Map a source heatmap's axes onto a host with the same two axis names."""

    try:
        parent_x = str(parent_options["x"])
        parent_y = str(parent_options["y"])
        source_x = str(source_options["x"])
        source_y = str(source_options["y"])
    except (KeyError, TypeError):
        return None

    if (
        not parent_x
        or not parent_y
        or not source_x
        or not source_y
        or parent_x == parent_y
        or source_x == source_y
        or {parent_x, parent_y} != {source_x, source_y}
    ):
        return None

    if parent_x == source_x and parent_y == source_y:
        return "x", "y"
    if parent_x == source_y and parent_y == source_x:
        return "y", "x"
    return None


def _window_display_unit(window: Any) -> str | None:
    """Return a plot's currently rendered dependent-value unit, when known."""

    try:
        state = window.__dict__
    except (AttributeError, RuntimeError):
        state = {}
    parameter = state.get("display_param") or state.get("param")
    if parameter is None:
        try:
            parameter = getattr(window, "display_param", None) or getattr(
                window,
                "param",
                None,
            )
        except RuntimeError:
            parameter = None
    if parameter is None:
        return None
    unit = getattr(parameter, "unit", None)
    return None if unit is None else str(unit or "").strip()


def _window_axis_unit(window: Any, parameter_name: str) -> str | None:
    """Return one independent parameter's unit without opening its dataset."""

    try:
        parameters = window.__dict__.get("param_dict")
    except (AttributeError, RuntimeError):
        parameters = None
    if not isinstance(parameters, dict):
        try:
            parameters = getattr(window, "param_dict", None)
        except RuntimeError:
            parameters = None
    if not isinstance(parameters, dict):
        return None
    parameter = parameters.get(parameter_name)
    if parameter is None:
        return None
    unit = getattr(parameter, "unit", None)
    return None if unit is None else str(unit or "").strip()


def _heatmap_layer_compatibility(
    parent: Any,
    source: Any,
) -> tuple[tuple[str, str] | None, str | None]:
    """Return source-axis mapping and any reason the maps cannot share a view."""

    try:
        parent_options = parent.axis_options
        source_options = source.axis_options
    except (AttributeError, RuntimeError):
        return None, "the heatmap axes are unavailable"

    axis_order = _heatmap_axis_order(parent_options, source_options)
    if axis_order is None:
        return None, "the heatmap axes do not match"

    parent_value_unit = _window_display_unit(parent)
    source_value_unit = _window_display_unit(source)
    if parent_value_unit is None or source_value_unit is None:
        return None, "the displayed value units are unavailable"
    if parent_value_unit != source_value_unit:
        return None, "the displayed value units do not match"

    for parent_axis, source_axis in zip(("x", "y"), axis_order, strict=True):
        parameter_name = str(parent_options[parent_axis])
        parent_axis_unit = _window_axis_unit(parent, parameter_name)
        source_axis_name = str(source_options[source_axis])
        source_axis_unit = _window_axis_unit(source, source_axis_name)
        if parent_axis_unit is None or source_axis_unit is None:
            return None, f"the {parameter_name} axis units are unavailable"
        if parent_axis_unit != source_axis_unit:
            return None, f"the {parameter_name} axis units do not match"

    return axis_order, None


class HeatmapLayer:
    """A pair of alternate renderers backed by another ``plot2d`` window."""

    def __init__(
        self,
        parent: Any,
        from_win: Any,
        *,
        opacity: float = DEFAULT_OVERLAY_OPACITY,
        z_value: float = _FIRST_OVERLAY_Z_VALUE,
    ) -> None:
        self.parent = parent
        self.from_win = from_win
        try:
            source_state = from_win.__dict__
        except (AttributeError, RuntimeError):
            source_state = {}
        self.trace_key = source_state.get("_trace_key")
        if self.trace_key is None:
            self.trace_key = source_state.get("label")
        if self.trace_key is None:
            try:
                self.trace_key = getattr(from_win, "_trace_key", None)
            except RuntimeError:
                self.trace_key = None
        try:
            source_label = getattr(from_win, "label", self.trace_key)
        except RuntimeError:
            source_label = self.trace_key
        self.label = str(source_label)
        self.running = plot_refresh_required(from_win)
        self.geometry: HeatmapGeometry | None = None
        self.axis_data: dict[str, np.ndarray] = {
            "x": np.asarray([], dtype=float),
            "y": np.asarray([], dtype=float),
        }
        self.data_grid = np.empty((0, 0), dtype=float)
        self.compatibility_error: str | None = None

        self.image = pg.ImageItem(axisOrder="row-major")
        self.heatmap_mesh = pg.PColorMeshItem()
        safe_z_value = min(
            max(float(z_value), _FIRST_OVERLAY_Z_VALUE),
            _MAX_OVERLAY_Z_VALUE,
        )
        self.image.setZValue(safe_z_value)
        self.heatmap_mesh.setZValue(safe_z_value)
        self.heatmap_mesh.hide()

        self.opacity = DEFAULT_OVERLAY_OPACITY
        self.visible = True
        self.set_opacity(opacity)

        self._renderers_added = False
        self._source_consumer_registered = False
        self._source_interval_signal = None
        self._source_interval_slot = None
        self._source_update_signal = None
        self._source_end_wait_signal = None
        self._source_end_wait_slot = None

        try:
            self._add_renderers()
            self._connect_source_updates()
            self.refresh()
            self._register_source_consumer()
        except Exception:
            self.disconnect_source_updates()
            self.remove_renderers()
            raise

    def render_items(self) -> list[Any]:
        """Return both renderers controlled by the host colour bar."""

        return [self.image, self.heatmap_mesh]

    def set_opacity(self, opacity: float) -> None:
        """Set a finite opacity in the inclusive range from zero to one."""

        try:
            value = float(opacity)
        except (TypeError, ValueError):
            value = DEFAULT_OVERLAY_OPACITY
        if not np.isfinite(value):
            value = DEFAULT_OVERLAY_OPACITY
        value = min(max(value, 0.0), 1.0)
        self.opacity = value
        self.image.setOpacity(value)
        self.heatmap_mesh.setOpacity(value)

    def set_visible(self, visible: bool) -> None:
        """Persist visibility across source refreshes and renderer changes."""

        self.visible = bool(visible)
        if not self.visible:
            self._hide_renderers()
            return
        self._render()

    def refresh(
        self,
        *,
        source_ready: bool = False,
        sync_colorbar: bool = True,
    ) -> bool:
        """Copy ready source data, orient it to the host, and redraw the layer."""

        self._disconnect_pending_end_wait()
        self.running = plot_refresh_required(self.from_win)

        axis_order, compatibility_error = _heatmap_layer_compatibility(
            self.parent,
            self.from_win,
        )
        if axis_order is None:
            self._report_compatibility_error(compatibility_error)
            self._clear_data()
            if sync_colorbar:
                self._sync_parent_colorbar()
            return False
        self.compatibility_error = None

        worker = getattr(self.from_win, "worker", None)
        if bool(getattr(worker, "running", False)) and not source_ready:
            # A normal plot2d publishes trace_updated only after its concrete
            # render.  end_wait is a fallback for simpler legacy/test sources
            # which do not expose that signal; base end_wait is otherwise too
            # early because it fires before plot2d finishes rendering.
            if self._source_update_signal is None:
                self._connect_pending_end_wait()
            return False

        source_axis_data = getattr(self.from_win, "axis_data", None)
        source_grid = getattr(self.from_win, "dataGrid", None)
        if not isinstance(source_axis_data, dict) or source_grid is None:
            self._clear_data()
            if sync_colorbar:
                self._sync_parent_colorbar()
            return False

        source_x_axis, source_y_axis = axis_order
        try:
            x_data = source_axis_data[source_x_axis]
            y_data = source_axis_data[source_y_axis]
            data_grid = np.asarray(source_grid)
            if axis_order == ("y", "x"):
                data_grid = data_grid.transpose()
            x_data, y_data, data_grid = canonicalize_heatmap_data(
                x_data,
                y_data,
                data_grid,
            )
            geometry = HeatmapGeometry.from_centres(x_data, y_data)
        except (KeyError, TypeError, ValueError):
            self._clear_data()
            if sync_colorbar:
                self._sync_parent_colorbar()
            return False

        self.axis_data = {
            "x": np.asarray(x_data),
            "y": np.asarray(y_data),
        }
        self.data_grid = np.asarray(data_grid)
        self.geometry = geometry
        self._render()
        if sync_colorbar:
            self._sync_parent_colorbar()
        return True

    def disconnect_source_updates(self) -> None:
        """Disconnect source signals and relinquish the hidden source window."""

        if self._source_update_signal is not None:
            try:
                self._source_update_signal.disconnect(self._source_trace_updated)
            except (TypeError, RuntimeError):
                pass
            self._source_update_signal = None
        self._disconnect_pending_end_wait()
        self._release_source_consumer()

    def remove_renderers(self) -> None:
        """Remove this layer's graphics items from the host plot."""

        for item in self.render_items():
            item.hide()
            if not self._renderers_added:
                continue
            remove_item = getattr(
                self.parent,
                "_remove_heatmap_render_item",
                None,
            )
            if callable(remove_item):
                remove_item(item)
                continue
            try:
                self.parent.plot.removeItem(item)
            except (AttributeError, RuntimeError, ValueError):
                pass
        self._renderers_added = False

    def _add_renderers(self) -> None:
        plot = getattr(self.parent, "plot", None)
        add_item = getattr(plot, "addItem", None)
        if not callable(add_item):
            return
        add_item(self.image)
        try:
            add_item(self.heatmap_mesh)
        except Exception:
            try:
                plot.removeItem(self.image)
            except (AttributeError, RuntimeError, ValueError):
                pass
            raise
        self._renderers_added = True

    def _connect_source_updates(self) -> None:
        signal = getattr(self.from_win, "trace_updated", None)
        connect = getattr(signal, "connect", None)
        if not callable(connect):
            return
        connect(self._source_trace_updated)
        self._source_update_signal = signal

    def _source_trace_updated(self) -> None:
        self.refresh(source_ready=True)
        sync_view = getattr(
            self.parent,
            "_sync_secondary_heatmap_view_ranges",
            None,
        )
        if callable(sync_view):
            sync_view(layer=self)

    def _source_end_wait_finished(self) -> None:
        self.refresh(source_ready=True)

    def _connect_pending_end_wait(self) -> None:
        if self._source_end_wait_signal is not None:
            return
        signal = getattr(self.from_win, "end_wait", None)
        connect = getattr(signal, "connect", None)
        if not callable(connect):
            return
        slot = self._source_end_wait_finished
        connect(slot)
        self._source_end_wait_signal = signal
        self._source_end_wait_slot = slot

    def _disconnect_pending_end_wait(self) -> None:
        if self._source_end_wait_signal is None:
            return
        try:
            self._source_end_wait_signal.disconnect(self._source_end_wait_slot)
        except (TypeError, RuntimeError):
            pass
        self._source_end_wait_signal = None
        self._source_end_wait_slot = None

    def _register_source_consumer(self) -> None:
        if self._source_consumer_registered:
            return

        source = self.from_win
        source._merged_trace_users = (
            max(
                int(getattr(source, "_merged_trace_users", 0)),
                0,
            )
            + 1
        )
        self._source_consumer_registered = True

        if not getattr(source, "visible", True):
            parent_spinbox = getattr(self.parent, "spinBox", None)
            source_spinbox = getattr(source, "spinBox", None)
            if parent_spinbox is not None and source_spinbox is not None:
                source_spinbox.setValue(parent_spinbox.value())
                interval_signal = getattr(parent_spinbox, "valueChanged", None)
                if interval_signal is not None:
                    interval_slot = source_spinbox.setValue
                    interval_signal.connect(interval_slot)
                    self._source_interval_signal = interval_signal
                    self._source_interval_slot = interval_slot

        monitor = getattr(source, "monitor", None)
        if (
            getattr(source, "_closed", False)
            and plot_refresh_required(source)
            and monitor is not None
            and not monitor.isActive()
        ):
            source.monitorIntervalChanged(source.spinBox.value())

    def _release_source_consumer(self) -> None:
        if not self._source_consumer_registered:
            return

        source = self.from_win
        remaining = max(int(getattr(source, "_merged_trace_users", 1)) - 1, 0)
        source._merged_trace_users = remaining
        self._source_consumer_registered = False

        if self._source_interval_signal is not None:
            try:
                self._source_interval_signal.disconnect(self._source_interval_slot)
            except (TypeError, RuntimeError):
                pass
            self._source_interval_signal = None
            self._source_interval_slot = None

        if (
            remaining == 0
            and getattr(source, "_closed", False)
            and not getattr(source, "visible", True)
        ):
            monitor = getattr(source, "monitor", None)
            if monitor is not None:
                monitor.stop()
            timer = source.__dict__.get("_heatmap_view_reload_timer")
            if timer is not None:
                timer.stop()
            source._refresh_pending = False
            source._refresh_pending_force = False
            worker = source.__dict__.get("worker")
            cancel = getattr(worker, "cancel", None)
            if bool(getattr(worker, "running", False)) and callable(cancel):
                cancel()

    def _render(self) -> None:
        geometry = self.geometry
        if geometry is None or not self.visible:
            self._hide_renderers()
            return

        if geometry.is_uniform:
            self.image.setImage(self.data_grid, autoLevels=False)
            self.image.setRect(QtCore.QRectF(*geometry.rect))
            self.heatmap_mesh.hide()
            self.image.show()
            return

        mesh_data = np.asarray(self.data_grid, dtype=float).copy()
        mesh_data[~np.isfinite(mesh_data)] = np.nan
        x_vertices, y_vertices = np.meshgrid(
            geometry.x.edges,
            geometry.y.edges,
            indexing="xy",
        )
        self.heatmap_mesh.setData(
            x_vertices,
            y_vertices,
            mesh_data,
            autoLevels=False,
        )
        self.image.hide()
        self.heatmap_mesh.show()

    def _clear_data(self) -> None:
        self.geometry = None
        self.axis_data = {
            "x": np.asarray([], dtype=float),
            "y": np.asarray([], dtype=float),
        }
        self.data_grid = np.empty((0, 0), dtype=float)
        self._hide_renderers()

    def _hide_renderers(self) -> None:
        self.image.hide()
        self.heatmap_mesh.hide()

    def _sync_parent_colorbar(self) -> None:
        sync = getattr(self.parent, "_sync_heatmap_colorbar_items", None)
        if callable(sync):
            sync(rescale=self._parent_refresh_colorbar_enabled())

    def _parent_refresh_colorbar_enabled(self) -> bool:
        try:
            checkbox = self.parent.__dict__.get("relevel_refresh")
        except (AttributeError, RuntimeError):
            checkbox = None
        is_checked = getattr(checkbox, "isChecked", None)
        return bool(is_checked()) if callable(is_checked) else False

    def _report_compatibility_error(self, error: str | None) -> None:
        if not error or error == self.compatibility_error:
            return
        self.compatibility_error = error
        show_status = getattr(self.parent, "show_status", None)
        if callable(show_status):
            show_status(f"{self.label} hidden because {error}.", 5000)


class Plot2DLayerMixin:
    """Registry, controls, colour-bar sync, and cleanup for heatmap layers."""

    heatmaps: dict[Any, Any]

    def initMenu(self) -> None:
        """Add Heatmap Appearance to the standard View menu."""

        super().initMenu()
        menu_bar = self.menuBar()
        if menu_bar is None:
            return
        view_menu = None
        for action in menu_bar.actions():
            if action.text().replace("&", "") == "View":
                view_menu = action.menu()
                break
        if view_menu is None:
            return
        view_menu.addSeparator()
        action = QtGui.QAction(
            "Heatmap Appearance…",
            cast(QtCore.QObject, self),
        )
        action.triggered.connect(self.open_heatmap_appearance_dialog)
        view_menu.addAction(action)
        self.__dict__["heatmap_appearance_action"] = action

    def open_heatmap_appearance_dialog(self, heatmap_key: Any = None) -> None:
        """Show the shared-item-style editor for this plot's heatmap layers."""

        dialog = self.__dict__.get("_heatmap_appearance_dialog")
        if dialog is None:
            dialog = _HeatmapAppearanceDialog(self)
            self.__dict__["_heatmap_appearance_dialog"] = dialog
        dialog.refresh_rows()
        if heatmap_key is not None:
            dialog.select_heatmap(heatmap_key)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def available_heatmap_candidates(self) -> list[tuple[str, Any]]:
        """Return database-backed heatmaps eligible for the Add control."""

        provider = self.__dict__.get("_heatmap_candidate_provider")
        if callable(provider):
            return list(provider())
        return []

    def add_heatmap_from_dialog(self, label: str, heatmap_key: Any) -> bool:
        """Ask the main window to construct and retain one selected layer."""

        request = self.__dict__.get("_heatmap_add_request")
        dataset_key = getattr(heatmap_key, "dataset_key", None)
        parameter_name = getattr(heatmap_key, "parameter_name", None)
        if callable(request) and dataset_key is not None and parameter_name:
            return bool(request(dataset_key, parameter_name))
        return False

    def _heatmap_display_label(self, key: Any, layer: Any) -> str:
        if layer is self:
            return str(self.__dict__.get("label", key))
        return str(getattr(layer, "label", key))

    def _heatmap_measurement_name(self, key: Any, layer: Any) -> str:
        source = self if layer is self else getattr(layer, "from_win", None)
        parameter = getattr(source, "param", None)
        return str(getattr(parameter, "name", key))

    def _heatmap_is_visible(self, layer: Any) -> bool:
        if layer is self:
            return bool(self.__dict__.get("_primary_heatmap_visible", True))
        return bool(getattr(layer, "visible", True))

    def _heatmap_opacity(self, layer: Any) -> float:
        if layer is self:
            return float(self.__dict__.get("_primary_heatmap_opacity", 1.0))
        return float(getattr(layer, "opacity", DEFAULT_OVERLAY_OPACITY))

    def _set_layer_visibility(self, layer: Any, visible: bool) -> None:
        if layer is not self:
            setter = getattr(layer, "set_visible", None)
            if callable(setter):
                setter(visible)
                return

        visible = bool(visible)
        self.__dict__["_primary_heatmap_visible"] = visible
        if not visible:
            for item in (
                self.__dict__.get("image"),
                self.__dict__.get("heatmap_mesh"),
            ):
                if item is not None:
                    item.hide()
            return
        render = getattr(self, "_render_heatmap", None)
        geometry = getattr(self, "_heatmap_geometry", None)
        if callable(render) and callable(geometry) and geometry() is not None:
            render()

    def _sync_heatmap_layer_order(self) -> None:
        """Map table/registry order onto heatmap renderer Z values."""

        heatmaps = self.__dict__.get("heatmaps", {})
        count = len(heatmaps)
        if not count:
            return
        step = _MAX_OVERLAY_Z_VALUE / max(count, 1)
        for row, layer in enumerate(heatmaps.values()):
            z_value = min(
                _MAX_OVERLAY_Z_VALUE,
                max(0.0, (row + 1) * step),
            )
            items = (
                (
                    self.__dict__.get("image"),
                    self.__dict__.get("heatmap_mesh"),
                )
                if layer is self
                else tuple(layer.render_items())
            )
            for item in items:
                if item is not None:
                    item.setZValue(z_value)

    def _default_heatmap_axis_names(self) -> tuple[str, str] | None:
        names = tuple(getattr(self.param, "depends_on_", ()))
        if len(names) != 2 or names[0] == names[1]:
            return None
        return str(names[1]), str(names[0])

    def can_swap_plot_axes(self) -> bool:
        names = self._default_heatmap_axis_names()
        dropdowns = self.__dict__.get("axis_dropdown", {})
        return bool(
            names is not None
            and set(dropdowns) == {"x", "y"}
            and all(
                dropdown.findText(name) >= 0
                for dropdown in dropdowns.values()
                for name in names
            )
        )

    def plot_axes_swapped(self) -> bool:
        names = self._default_heatmap_axis_names()
        if names is None:
            return False
        default_x, default_y = names
        options = self.axis_options
        return (
            options.get("x") == default_y
            and options.get("y") == default_x
        )

    def set_plot_axes_swapped(self, swapped: bool) -> bool:
        if not self.can_swap_plot_axes():
            return False
        names = self._default_heatmap_axis_names()
        assert names is not None
        default_x, default_y = names
        target = (
            {"x": default_y, "y": default_x}
            if swapped
            else {"x": default_x, "y": default_y}
        )
        previous = dict(self.axis_options)
        if previous == target:
            return True
        for axis, name in target.items():
            dropdown = self.axis_dropdown[axis]
            blocked = dropdown.blockSignals(True)
            try:
                dropdown.setCurrentIndex(dropdown.findText(name))
            finally:
                dropdown.blockSignals(blocked)
        self._axis_selection = dict(target)
        if previous == {"x": target["y"], "y": target["x"]}:
            self._transpose_heatmap_axis_assignments()
        self.refreshWindow(force=True)
        dialog = self.__dict__.get("_heatmap_appearance_dialog")
        if dialog is not None:
            dialog.sync_swap_axes_control()
        return True

    def _transpose_heatmap_axis_assignments(self) -> None:
        """Keep heatmaps on equivalent physical sides after swapping X/Y."""

        assignments = self.__dict__.get("_heatmap_axis_assignments", {})
        for assignment in assignments.values():
            previous_x = assignment.get("x", "Bottom")
            previous_y = assignment.get("y", "Left")
            assignment["x"] = "Bottom" if previous_y == "Left" else "Top"
            assignment["y"] = "Left" if previous_x == "Bottom" else "Right"
        for layer in self.__dict__.get("heatmaps", {}).values():
            self._apply_heatmap_axis_assignment(layer, auto_range=False)

    @staticmethod
    def _window_heatmap_key(window: Any) -> Any:
        try:
            state = window.__dict__
        except (AttributeError, RuntimeError):
            state = {}
        if "_trace_key" in state:
            return state["_trace_key"]
        if "label" in state:
            return state["label"]
        try:
            return getattr(window, "_trace_key", None) or getattr(
                window,
                "label",
                None,
            )
        except RuntimeError:
            return None

    def _init_heatmap_layers(self) -> None:
        """Register the primary heatmap and its appearance state."""

        primary_key = self._window_heatmap_key(self)
        if primary_key is None:
            primary_key = _PRIMARY_HEATMAP_KEY
        self._primary_heatmap_key = primary_key

        heatmaps = self.__dict__.get("heatmaps")
        if not isinstance(heatmaps, dict):
            heatmaps = {}
            self.__dict__["heatmaps"] = heatmaps
        heatmaps.setdefault(primary_key, self)
        self.__dict__.setdefault("_primary_heatmap_opacity", 1.0)
        self.__dict__.setdefault("_primary_heatmap_visible", True)
        self.__dict__.setdefault("_heatmap_appearance_dialog", None)
        assignments = self.__dict__.get("_heatmap_axis_assignments")
        if not isinstance(assignments, dict):
            assignments = {}
            self.__dict__["_heatmap_axis_assignments"] = assignments
        assignments.setdefault(
            primary_key,
            {"x": "Bottom", "y": "Left"},
        )
        renderer_viewboxes = self.__dict__.get("_heatmap_renderer_viewboxes")
        if not isinstance(renderer_viewboxes, dict):
            renderer_viewboxes = {}
            self.__dict__["_heatmap_renderer_viewboxes"] = renderer_viewboxes
        primary_viewbox = self.__dict__.get("vb")
        if primary_viewbox is not None:
            for item in self._primary_heatmap_axis_items():
                renderer_viewboxes.setdefault(id(item), primary_viewbox)

        self._install_heatmap_double_click_handlers(
            self,
            primary_key,
            )

    def _has_heatmap_window(self, window: Any) -> bool:
        """Return whether ``window`` is already represented on this heatmap."""

        candidate_key = self._window_heatmap_key(window)
        for key, layer in self.__dict__.get("heatmaps", {}).items():
            if candidate_key is not None and key == candidate_key:
                return True
            source = layer if layer is self else getattr(layer, "from_win", None)
            if source is window:
                return True
            if (
                candidate_key is not None
                and source is not None
                and self._window_heatmap_key(source) == candidate_key
            ):
                return True
        return False

    def add_heatmap(self, from_win: Any) -> bool:
        """Add a compatible source heatmap as a translucent plot layer."""

        if not isinstance(self.__dict__.get("heatmaps"), dict):
            self._init_heatmap_layers()
        if from_win is None or from_win is self or self._has_heatmap_window(from_win):
            return False

        _axis_order, compatibility_error = _heatmap_layer_compatibility(
            self,
            from_win,
        )
        if compatibility_error is not None:
            show_status = getattr(self, "show_status", None)
            if callable(show_status):
                show_status(
                    f"Cannot add {getattr(from_win, 'label', 'heatmap')}; "
                    f"{compatibility_error}.",
                    5000,
                )
            return False

        dataset_key = getattr(from_win, "_dataset_key", None)
        try:
            make_ds = getattr(self, "make_ds", None)
        except RuntimeError:
            make_ds = None
        emit_make_ds = getattr(make_ds, "emit", None)
        retained = dataset_key is not None and callable(emit_make_ds)
        if retained:
            emit_make_ds(dataset_key)

        trace_key = self._window_heatmap_key(from_win)
        if trace_key is None:
            trace_key = id(from_win)
        overlay_count = sum(1 for layer in self.heatmaps.values() if layer is not self)
        z_value = min(
            _FIRST_OVERLAY_Z_VALUE + overlay_count * _OVERLAY_Z_VALUE_STEP,
            _MAX_OVERLAY_Z_VALUE,
        )
        layer = None
        try:
            layer = HeatmapLayer(
                self,
                from_win,
                opacity=DEFAULT_OVERLAY_OPACITY,
                z_value=z_value,
            )
            self.heatmaps[trace_key] = layer
            self._heatmap_axis_assignments[trace_key] = {
                "x": "Bottom",
                "y": "Left",
            }
            self._install_heatmap_double_click_handlers(layer, trace_key)
            self._apply_heatmap_axis_assignment(layer, auto_range=False)
            self._sync_heatmap_layer_order()
            self._sync_heatmap_colorbar_items(rescale=True)
            self._sync_secondary_heatmap_view_ranges(layer=layer)
        except Exception:
            if layer is not None:
                layer.disconnect_source_updates()
                layer.remove_renderers()
            self.heatmaps.pop(trace_key, None)
            self.__dict__.get("_heatmap_axis_assignments", {}).pop(
                trace_key,
                None,
            )
            if retained:
                self._emit_remove_dataset(dataset_key)
            raise
        dialog = self.__dict__.get("_heatmap_appearance_dialog")
        if dialog is not None:
            dialog.refresh_rows()
        return True

    def remove_heatmap(self, label: str = "", trace_key: Any = None) -> bool:
        """Remove one secondary heatmap selected by stable key or display label."""

        heatmaps = self.__dict__.get("heatmaps")
        if not isinstance(heatmaps, dict):
            return False

        selected_key = trace_key
        layer = heatmaps.get(selected_key) if selected_key is not None else None
        if layer is self:
            return False
        if layer is None:
            for key, candidate in heatmaps.items():
                if candidate is self:
                    continue
                if label and str(getattr(candidate, "label", "")) == str(label):
                    selected_key = key
                    layer = candidate
                    break
        if layer is None or selected_key is None:
            return False

        heatmaps.pop(selected_key, None)
        self.__dict__.get("_heatmap_axis_assignments", {}).pop(
            selected_key,
            None,
        )
        layer.disconnect_source_updates()
        layer.remove_renderers()
        dataset_key = getattr(getattr(layer, "from_win", None), "_dataset_key", None)
        if dataset_key is not None:
            self._emit_remove_dataset(dataset_key)
        self._sync_heatmap_layer_order()
        self._sync_heatmap_axis_visibility()
        self._sync_heatmap_colorbar_items(rescale=True)
        dialog = self.__dict__.get("_heatmap_appearance_dialog")
        if dialog is not None:
            dialog.refresh_rows()
        return True

    def refresh_secondary_heatmaps(self) -> None:
        """Refresh layers and restart monitors retained by hidden live sources."""

        for layer in list(self.__dict__.get("heatmaps", {}).values()):
            if layer is self:
                continue
            layer.refresh(sync_colorbar=False)
            source = layer.from_win
            monitor = getattr(source, "monitor", None)
            if (
                not getattr(source, "visible", True)
                and plot_refresh_required(source)
                and monitor is not None
                and not monitor.isActive()
            ):
                source.monitorIntervalChanged(source.spinBox.value())

    def _sync_secondary_heatmap_view_ranges(
        self,
        *,
        layer: Any = None,
    ) -> None:
        """Map the host viewport onto SQL-backed hidden heatmap sources."""

        if layer is None:
            layers = [
                candidate
                for candidate in self.__dict__.get("heatmaps", {}).values()
                if candidate is not self
            ]
        else:
            layers = [layer]

        for candidate in layers:
            source = getattr(candidate, "from_win", None)
            if (
                source is None
                or not source.__dict__.get("_large_heatmap_sql_mode", False)
                or getattr(source, "visible", True)
            ):
                continue
            axis_order, compatibility_error = _heatmap_layer_compatibility(
                self,
                source,
            )
            if axis_order is None or compatibility_error is not None:
                continue

            view_box = self._heatmap_axis_viewbox(candidate)
            view_range = getattr(view_box, "viewRange", None)
            if not callable(view_range):
                continue
            try:
                host_x_range, host_y_range = view_range()
            except (TypeError, ValueError, RuntimeError):
                continue
            host_ranges = {
                "x": tuple(host_x_range),
                "y": tuple(host_y_range),
            }

            source_ranges = {
                axis_order[index]: host_ranges[parent_axis]
                for index, parent_axis in enumerate(("x", "y"))
            }
            source_view_box = source.__dict__.get("vb")
            set_range = getattr(source_view_box, "setRange", None)
            schedule_reload = getattr(
                source,
                "_schedule_visible_heatmap_reload",
                None,
            )
            if not callable(set_range) or not callable(schedule_reload):
                continue

            source.__dict__["_heatmap_layer_view_sync_active"] = True
            try:
                set_range(
                    xRange=source_ranges["x"],
                    yRange=source_ranges["y"],
                    padding=0,
                )
                schedule_reload()
            finally:
                source.__dict__["_heatmap_layer_view_sync_active"] = False

    def close_secondary_heatmaps(self) -> None:
        """Disconnect and remove every secondary layer while keeping the primary."""

        heatmaps = self.__dict__.get("heatmaps", {})
        secondary_keys = [
            key for key, layer in list(heatmaps.items()) if layer is not self
        ]
        for key in secondary_keys:
            self.remove_heatmap(trace_key=key)

    def _heatmap_colorbar_items(self) -> list[Any]:
        """Return both alternate renderers for the primary and every layer."""

        layers = list(self.__dict__.get("heatmaps", {}).values())
        if not layers:
            layers = [self]
        elif self not in layers:
            layers.insert(0, self)

        items: list[Any] = []
        seen: set[int] = set()
        for layer in layers:
            if layer is self:
                candidates = [
                    self.__dict__.get("image"),
                    self.__dict__.get("heatmap_mesh"),
                ]
            else:
                render_items = getattr(layer, "render_items", None)
                if callable(render_items):
                    candidates = render_items()
                else:
                    candidates = [
                        getattr(layer, "image", None),
                        getattr(layer, "heatmap_mesh", None),
                    ]
            for item in candidates:
                if item is None or id(item) in seen:
                    continue
                seen.add(id(item))
                items.append(item)
        return items

    def _heatmap_colorbar_data_arrays(self) -> list[np.ndarray]:
        """Return primary and secondary data arrays used for colour autoscaling."""

        arrays: list[np.ndarray] = []
        primary_data = self.__dict__.get("dataGrid")
        if primary_data is not None:
            arrays.append(np.asarray(primary_data))

        for layer in self.__dict__.get("heatmaps", {}).values():
            if layer is self:
                continue
            data = getattr(layer, "data_grid", None)
            if data is not None:
                arrays.append(np.asarray(data))
        return arrays

    def _sync_heatmap_colorbar_items(self, *, rescale: bool = False) -> None:
        """Rebind renderers and apply manual or union-autoscaled colour levels."""

        bar = self.__dict__.get("bar")
        if bar is None:
            return

        previous_levels = self._valid_colorbar_levels(bar)
        items = self._heatmap_colorbar_items()
        item_ids = tuple(id(item) for item in items)
        if item_ids != self.__dict__.get("_heatmap_colorbar_item_ids"):
            set_image_item = getattr(bar, "setImageItem", None)
            if callable(set_image_item):
                self._disconnect_heatmap_colorbar_items(bar)
                set_image_item(items)
            self.__dict__["_heatmap_colorbar_item_ids"] = item_ids

        manual_levels = self.__dict__.get("_colorbar_manual_levels")
        levels = None
        if manual_levels is not None:
            levels = self._normalise_colorbar_levels(manual_levels)
        elif rescale:
            levels = self._heatmap_union_colorbar_levels()
        if levels is None:
            levels = previous_levels
        if levels is None:
            return

        set_levels = getattr(self, "_set_colorbar_levels", None)
        if callable(set_levels):
            set_levels(*levels)
        else:
            bar_set_levels = getattr(bar, "setLevels", None)
            if callable(bar_set_levels):
                bar_set_levels(levels)

    @staticmethod
    def _disconnect_heatmap_colorbar_items(bar: Any) -> None:
        """Remove connections that pyqtgraph leaves behind when rebinding."""

        handler = getattr(bar, "_levelsChangedHandler", None)
        if handler is None:
            return
        for item_reference in list(getattr(bar, "img_list", ())):
            try:
                item = item_reference()
            except TypeError:
                item = item_reference
            signal = getattr(item, "sigLevelsChanged", None)
            disconnect = getattr(signal, "disconnect", None)
            if not callable(disconnect):
                continue
            try:
                disconnect(handler)
            except (TypeError, RuntimeError):
                pass

    def _set_layer_opacity(self, layer: Any, opacity: float) -> None:
        if layer is not self:
            set_opacity = getattr(layer, "set_opacity", None)
            if callable(set_opacity):
                set_opacity(opacity)
                return

        value = min(max(float(opacity), 0.0), 1.0)
        self.__dict__["_primary_heatmap_opacity"] = value
        for item in (
            self.__dict__.get("image"),
            self.__dict__.get("heatmap_mesh"),
        ):
            if item is not None:
                item.setOpacity(value)

    def _heatmap_key_for_layer(self, layer: Any) -> Any:
        """Return the stable registry key for a heatmap object."""

        for key, candidate in self.__dict__.get("heatmaps", {}).items():
            if candidate is layer:
                return key
        return None

    def _install_heatmap_double_click_handlers(
        self,
        layer: Any,
        layer_key: Any,
    ) -> None:
        """Bind double-click handlers so a heatmap image opens its appearance row."""

        for item in self._heatmap_render_items(layer):
            item._qplot_heatmap_key = layer_key
            if getattr(item, "_qplot_heatmap_double_click_handler", False):
                continue

            previous_handler = getattr(item, "mouseDoubleClickEvent", None)

            def mouse_double_click(
                event,
                target=item,
                previous_double_click=previous_handler,
            ):
                button = getattr(event, "button", lambda: None)()
                if button == QtCore.Qt.MouseButton.LeftButton:
                    heatmap_key = getattr(
                        target,
                        "_qplot_heatmap_key",
                        layer_key,
                    )
                    opener = getattr(
                        self,
                        "open_heatmap_appearance_dialog",
                        None,
                    )
                    if callable(opener):
                        opener(heatmap_key)
                    event.accept()
                    return

                if previous_double_click is not None:
                    previous_double_click(event)

            item.mouseDoubleClickEvent = mouse_double_click
            item._qplot_heatmap_double_click_handler = True

    def _heatmap_axis_sides(self, layer: Any) -> tuple[str, str]:
        """Return the horizontal and vertical display sides for one heatmap."""

        key = self._heatmap_key_for_layer(layer)
        assignment = self.__dict__.get("_heatmap_axis_assignments", {}).get(
            key,
            {},
        )
        x_axis = "Top" if assignment.get("x") == "Top" else "Bottom"
        y_axis = "Right" if assignment.get("y") == "Right" else "Left"
        return x_axis, y_axis

    def _primary_heatmap_axis_items(self) -> list[Any]:
        """Return graphics whose coordinates belong to the primary heatmap."""

        names = (
            "image",
            "heatmap_mesh",
            "hover_pixel_outline",
            "marquee_highlight",
            "marquee_outline",
            "marquee_handles",
        )
        items = [self.__dict__.get(name) for name in names]
        items.extend(self.__dict__.get("sweep_lines", {}).values())
        return [item for item in items if item is not None]

    def _heatmap_axis_items(self, layer: Any) -> list[Any]:
        if layer is self:
            return self._primary_heatmap_axis_items()
        render_items = getattr(layer, "render_items", None)
        return list(render_items()) if callable(render_items) else []

    def _heatmap_render_items(self, layer: Any) -> list[Any]:
        """Return only data renderers, excluding interaction decorations."""

        if layer is self:
            return [
                item
                for item in (
                    self.__dict__.get("image"),
                    self.__dict__.get("heatmap_mesh"),
                )
                if item is not None
            ]
        render_items = getattr(layer, "render_items", None)
        return list(render_items()) if callable(render_items) else []

    def _ensure_heatmap_axis_viewboxes(
        self,
        *,
        top: bool = False,
        right: bool = False,
    ) -> None:
        """Create the linked ViewBoxes required by a heatmap's axis pair."""

        plot = self.__dict__.get("plot")
        primary = self.__dict__.get("vb")
        if plot is None or primary is None:
            return

        if right and self.__dict__.get("right_vb") is None:
            right_viewbox = pg.ViewBox()
            right_viewbox.setDefaultPadding(0)
            plot.scene().addItem(right_viewbox)
            plot.getAxis("right").linkToView(right_viewbox)
            right_viewbox.setXLink(primary)
            right_viewbox.sigRangeChanged.connect(
                self._heatmap_overlay_range_changed
            )
            self.__dict__["right_vb"] = right_viewbox

        if top and self.__dict__.get("top_vb") is None:
            top_viewbox = pg.ViewBox()
            top_viewbox.setDefaultPadding(0)
            plot.scene().addItem(top_viewbox)
            plot.getAxis("top").linkToView(top_viewbox)
            top_viewbox.setYLink(primary)
            top_viewbox.sigRangeChanged.connect(
                self._heatmap_overlay_range_changed
            )
            self.__dict__["top_vb"] = top_viewbox

        if top and right and self.__dict__.get("top_right_vb") is None:
            top_right_viewbox = pg.ViewBox()
            top_right_viewbox.setDefaultPadding(0)
            plot.scene().addItem(top_right_viewbox)
            top_right_viewbox.setXLink(self.top_vb)
            top_right_viewbox.setYLink(self.right_vb)
            top_right_viewbox.sigRangeChanged.connect(
                self._heatmap_overlay_range_changed
            )
            self.__dict__["top_right_vb"] = top_right_viewbox

        if not self.__dict__.get("_heatmap_axis_viewboxes_connected", False):
            moved = getattr(primary, "main_moved", None)
            if moved is not None:
                moved.connect(self._update_heatmap_axis_viewboxes)
            primary.sigResized.connect(self._update_heatmap_axis_viewboxes)
            auto_button = getattr(plot, "autoBtn", None)
            if auto_button is not None:
                auto_button.clicked.connect(
                    self._heatmap_axis_auto_button_clicked
                )
            auto_requested = getattr(primary, "autoRange_triggered", None)
            if auto_requested is not None:
                auto_requested.connect(self._heatmap_axis_auto_range_requested)
            self.__dict__["_heatmap_axis_viewboxes_connected"] = True

        install_range_handlers = getattr(
            self,
            "_install_axis_scale_viewbox_range_handlers",
            None,
        )
        if callable(install_range_handlers):
            if self.__dict__.get("right_vb") is not None:
                install_range_handlers(self.right_vb, y_axis="y2")
            if self.__dict__.get("top_vb") is not None:
                install_range_handlers(self.top_vb, x_axis="x2")
        self._update_heatmap_axis_viewboxes(None)

    def _heatmap_axis_viewbox(self, layer: Any) -> Any:
        """Return the ViewBox representing a heatmap's selected axis pair."""

        primary = self.__dict__.get("vb")
        if primary is None:
            plot = self.__dict__.get("plot")
            primary = getattr(plot, "vb", None)
        x_axis, y_axis = self._heatmap_axis_sides(layer)
        uses_top = x_axis == "Top"
        uses_right = y_axis == "Right"
        if uses_top or uses_right:
            self._ensure_heatmap_axis_viewboxes(
                top=uses_top,
                right=uses_right,
            )
        if uses_top and uses_right:
            return self.__dict__.get("top_right_vb") or primary
        if uses_top:
            return self.__dict__.get("top_vb") or primary
        if uses_right:
            return self.__dict__.get("right_vb") or primary
        return primary

    def _primary_heatmap_viewbox(self) -> Any:
        """Return the coordinate owner used by primary heatmap interactions."""

        return self._heatmap_axis_viewbox(self)

    def _primary_heatmap_semantic_axes(self) -> tuple[str, str]:
        x_axis, y_axis = self._heatmap_axis_sides(self)
        return (
            "x2" if x_axis == "Top" else "x",
            "y2" if y_axis == "Right" else "y",
        )

    def _move_heatmap_item_to_viewbox(self, item: Any, target: Any) -> None:
        """Move one heatmap-related graphics item without recreating it."""

        if target is None:
            return
        tracked = self.__dict__.setdefault("_heatmap_renderer_viewboxes", {})
        get_viewbox = getattr(item, "getViewBox", None)
        current = get_viewbox() if callable(get_viewbox) else None
        current = current or tracked.get(id(item))
        if current is target:
            tracked[id(item)] = target
            return

        if current is self.__dict__.get("vb"):
            self.plot.removeItem(item)
        elif current is not None:
            current.removeItem(item)
        else:
            try:
                self.plot.removeItem(item)
            except (AttributeError, RuntimeError, ValueError):
                pass

        if target is self.__dict__.get("vb"):
            self.plot.addItem(item)
        else:
            target.addItem(item)
        tracked[id(item)] = target

    def _remove_heatmap_render_item(self, item: Any) -> None:
        """Remove a renderer from whichever ViewBox currently owns it."""

        tracked = self.__dict__.get("_heatmap_renderer_viewboxes", {})
        get_viewbox = getattr(item, "getViewBox", None)
        current = get_viewbox() if callable(get_viewbox) else None
        tracked_current = tracked.pop(id(item), None)
        current = current or tracked_current
        if current is self.__dict__.get("vb") or current is None:
            self.plot.removeItem(item)
        else:
            current.removeItem(item)

    def _apply_heatmap_axis_assignment(
        self,
        layer: Any,
        *,
        auto_range: bool = True,
    ) -> None:
        target = self._heatmap_axis_viewbox(layer)
        for item in self._heatmap_axis_items(layer):
            self._move_heatmap_item_to_viewbox(item, target)
        self._sync_heatmap_axis_visibility()
        if auto_range and target is not None:
            target.autoRange()
        self._sync_secondary_heatmap_view_ranges(layer=None if layer is self else layer)

    def _set_layer_axes(self, layer: Any, x_axis: str, y_axis: str) -> None:
        """Assign one heatmap to any of the four displayed axis pairs."""

        key = self._heatmap_key_for_layer(layer)
        if key is None:
            return
        assignment = {
            "x": "Top" if x_axis == "Top" else "Bottom",
            "y": "Right" if y_axis == "Right" else "Left",
        }
        assignments = self.__dict__.setdefault("_heatmap_axis_assignments", {})
        if assignments.get(key) == assignment:
            return
        assignments[key] = assignment
        self._apply_heatmap_axis_assignment(layer)

    def _heatmap_axis_parameter(self, display_axis: str) -> Any:
        parameters = self.__dict__.get("axis_param", {})
        if isinstance(parameters, dict):
            parameter = parameters.get(display_axis)
            if parameter is not None:
                return parameter
        options = getattr(self, "axis_options", {})
        name = options.get(display_axis) if isinstance(options, dict) else None
        parameter_dict = self.__dict__.get("param_dict", {})
        if isinstance(parameter_dict, dict) and name is not None:
            return parameter_dict.get(name)
        return None

    def _sync_heatmap_axis_visibility(self) -> None:
        """Show and label secondary axes used by at least one heatmap."""

        plot = self.__dict__.get("plot")
        if plot is None:
            return
        layers = list(self.__dict__.get("heatmaps", {}).values())
        for side, display_axis, selected_side in (
            ("top", "x", "Top"),
            ("right", "y", "Right"),
        ):
            used = any(
                selected_side in self._heatmap_axis_sides(layer)
                for layer in layers
            )
            axis = plot.getAxis(side)
            axis.setStyle(showValues=used)
            if not used:
                axis.setLabel(text="", units="")
                continue
            parameter = self._heatmap_axis_parameter(display_axis)
            label = getattr(parameter, "label", None) or getattr(
                parameter,
                "name",
                getattr(self, "axis_options", {}).get(display_axis, ""),
            )
            unit = getattr(parameter, "unit", "") or ""
            axis.setLabel(text=str(label), units=str(unit))
        sync_tabs = getattr(self, "_sync_axis_scale_tab_states", None)
        if callable(sync_tabs):
            sync_tabs()

    def _set_param_axis_labels(self) -> None:
        """Refresh primary labels without losing active heatmap side axes."""

        super()._set_param_axis_labels()
        self._sync_heatmap_axis_visibility()

    def _update_heatmap_axis_viewboxes(self, event: Any = None) -> None:
        """Keep secondary ViewBoxes aligned and mirror primary navigation."""

        primary = self.__dict__.get("vb")
        if primary is None:
            return
        geometry = primary.sceneBoundingRect()
        for name in ("right_vb", "top_vb", "top_right_vb"):
            viewbox = self.__dict__.get(name)
            if viewbox is not None:
                viewbox.setGeometry(geometry)

        right_viewbox = self.__dict__.get("right_vb")
        top_viewbox = self.__dict__.get("top_vb")
        constrained_axis = getattr(primary, "_main_moved_axis", None)
        if event is not None:
            if event.__class__.__name__ == "QGraphicsSceneWheelEvent":
                if right_viewbox is not None:
                    right_viewbox.wheelEvent(event, axis=1)
                if top_viewbox is not None:
                    top_viewbox.wheelEvent(event, axis=0)
            elif event.__class__.__name__ == "MouseDragEvent":
                if right_viewbox is not None and constrained_axis in (None, 1):
                    right_viewbox.mouseDragEvent(event, axis=1)
                if top_viewbox is not None and constrained_axis in (None, 0):
                    top_viewbox.mouseDragEvent(event, axis=0)

    def _heatmap_axis_auto_button_clicked(self, *_args: Any) -> None:
        auto_button = getattr(self.plot, "autoBtn", None)
        enabled = auto_button is None or auto_button.mode == "auto"
        for name in ("right_vb", "top_vb", "top_right_vb"):
            viewbox = self.__dict__.get(name)
            if viewbox is not None:
                viewbox.enableAutoRange(enable=enabled)

    def _heatmap_axis_auto_range_requested(self, *_args: Any) -> None:
        for name in ("right_vb", "top_vb", "top_right_vb"):
            viewbox = self.__dict__.get(name)
            if viewbox is not None:
                viewbox.autoRange()
        self._sync_secondary_heatmap_view_ranges()

    def _heatmap_overlay_range_changed(self, *_args: Any) -> None:
        self._sync_secondary_heatmap_view_ranges()
        primary_viewbox = self._primary_heatmap_viewbox()
        if primary_viewbox is not self.__dict__.get("vb"):
            schedule = getattr(self, "_schedule_visible_heatmap_reload", None)
            if callable(schedule):
                schedule()

    def _emit_remove_dataset(self, dataset_key: Any) -> None:
        try:
            signal = getattr(self, "remove_dataset", None)
        except RuntimeError:
            signal = None
        emit = getattr(signal, "emit", None)
        if callable(emit):
            emit(dataset_key)

    def _heatmap_union_colorbar_levels(self) -> tuple[float, float] | None:
        minimum = np.inf
        maximum = -np.inf
        found_finite = False
        for data in self._heatmap_colorbar_data_arrays():
            values = np.asarray(data)
            finite_values = values[np.isfinite(values)]
            if finite_values.size == 0:
                continue
            found_finite = True
            minimum = min(minimum, float(np.min(finite_values)))
            maximum = max(maximum, float(np.max(finite_values)))

        if not found_finite:
            return None
        if minimum == maximum:
            constant_levels = getattr(self, "_constant_colorbar_levels", None)
            if callable(constant_levels):
                return constant_levels(minimum)
            padding = 1e-6 if minimum == 0 else abs(minimum) * 1e-6
            return minimum - padding, maximum + padding
        return minimum, maximum

    @classmethod
    def _normalise_colorbar_levels(
        cls,
        levels: Any,
    ) -> tuple[float, float] | None:
        try:
            low, high = levels
            low = float(low)
            high = float(high)
        except (TypeError, ValueError):
            return None
        if not np.isfinite(low) or not np.isfinite(high) or low >= high:
            return None
        return low, high

    @classmethod
    def _valid_colorbar_levels(cls, bar: Any) -> tuple[float, float] | None:
        levels_getter = getattr(bar, "levels", None)
        if not callable(levels_getter):
            return None
        try:
            levels = levels_getter()
        except (TypeError, ValueError, RuntimeError):
            return None
        return cls._normalise_colorbar_levels(levels)


class _HeatmapTableWidget(ReorderAppearanceTable):
    """Heatmap-specific instance of the shared appearance reorder table."""

    _REORDER_MIME_TYPE = "application/x-qplot-heatmap-reorder"

    def __init__(self, dialog: _HeatmapAppearanceDialog) -> None:
        super().__init__(dialog, mime_type=self._REORDER_MIME_TYPE)


class _HeatmapAppearanceDialog(qtw.QDialog):
    """Edit heatmap layers using the same interaction model as traces."""

    _COL_ID = 0
    _COL_PREVIEW = 1
    _COL_MEASUREMENT = 2

    def __init__(self, owner: Plot2DLayerMixin) -> None:
        super().__init__(cast(qtw.QWidget, owner))
        self.owner = owner
        self._building = False
        self.setWindowTitle("Heatmap Appearance")
        self.resize(780, 360)
        self.setMinimumSize(700, 300)
        self.setStyleSheet(
            self.styleSheet()
            + """
            QTableWidget#heatmapAppearanceTable {
                border: 1px solid palette(mid);
                background-color: palette(base);
                alternate-background-color: palette(alternate-base);
                gridline-color: palette(midlight);
            }
            QTableWidget#heatmapAppearanceTable::item {
                padding: 2px 6px;
                border: none;
            }
            QTableWidget#heatmapAppearanceTable QHeaderView::section {
                font-weight: normal;
            }
            QPushButton#heatmapColorScaleButton {
                color: palette(link);
                text-decoration: underline;
                padding: 2px;
            }
            """
        )

        main = qtw.QVBoxLayout(self)
        main.setContentsMargins(10, 10, 10, 10)
        main.setSpacing(8)
        body = qtw.QHBoxLayout()
        body.setSpacing(10)
        main.addLayout(body, 1)

        heatmap_group = qtw.QGroupBox(self)
        heatmap_layout = qtw.QVBoxLayout(heatmap_group)
        heatmap_layout.setContentsMargins(8, 8, 8, 8)
        heatmap_layout.setSpacing(6)

        self.table = _HeatmapTableWidget(self)
        configure_appearance_table(
            self.table,
            self,
            object_name="heatmapAppearanceTable",
            item_name="heatmap",
        )
        self.table.itemSelectionChanged.connect(
            self._sync_controls_from_selection
        )
        heatmap_layout.addWidget(self.table)

        actions = qtw.QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(6)
        self.add_heatmap_combo = qtw.QComboBox(self)
        self.add_heatmap_combo.setObjectName("heatmapAppearanceAddCombo")
        self.add_heatmap_combo.setMinimumContentsLength(18)
        self.add_heatmap_combo.setSizeAdjustPolicy(
            qtw.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.add_heatmap_combo.setToolTip(
            "Choose an available measurement to add."
        )
        self.add_heatmap_button = qtw.QPushButton("Add Heatmap", self)
        self.add_heatmap_button.setObjectName("heatmapAppearanceAddButton")
        self.add_heatmap_button.setEnabled(False)
        self.remove_heatmap_button = qtw.QPushButton("Remove Heatmap", self)
        self.remove_heatmap_button.setObjectName("heatmapAppearanceRemoveButton")
        self.remove_heatmap_button.setEnabled(False)
        actions.addWidget(self.add_heatmap_combo, 1)
        actions.addWidget(self.add_heatmap_button)
        actions.addWidget(self.remove_heatmap_button)
        heatmap_layout.addLayout(actions)
        body.addWidget(heatmap_group, 5)

        panel = qtw.QWidget(self)
        panel_layout = qtw.QVBoxLayout(panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(8)

        color_scale_group = qtw.QGroupBox("Color scale", panel)
        color_scale_layout = qtw.QGridLayout(color_scale_group)
        color_scale_layout.setContentsMargins(8, 10, 8, 8)
        color_scale_layout.setHorizontalSpacing(8)
        color_scale_layout.addWidget(qtw.QLabel("Colors"), 0, 0)
        self.color_scale_name = qtw.QLabel()
        self.color_scale_name.setObjectName("heatmapColorScaleName")
        color_scale_layout.addWidget(self.color_scale_name, 0, 1, 1, 2)
        self.color_scale_preview = qtw.QLabel()
        self.color_scale_preview.setObjectName("heatmapColorScalePreview")
        self.color_scale_preview.setMinimumWidth(170)
        color_scale_layout.addWidget(self.color_scale_preview, 1, 0, 1, 3)
        self.color_scale_button = qtw.QPushButton("Color scale…")
        self.color_scale_button.setObjectName("heatmapColorScaleButton")
        self.color_scale_button.setFlat(True)
        self.color_scale_button.setCursor(
            QtCore.Qt.CursorShape.PointingHandCursor
        )
        self.color_scale_button.setToolTip("Open the Color scale dialog.")
        color_scale_layout.addWidget(
            self.color_scale_button,
            2,
            0,
            1,
            3,
            QtCore.Qt.AlignmentFlag.AlignLeft,
        )
        panel_layout.addWidget(color_scale_group)

        self.visible = qtw.QCheckBox("Visible")
        self.visible.setChecked(True)
        self.opacity = qtw.QSpinBox()
        self.opacity.setObjectName("heatmapAppearanceOpacity")
        self.opacity.setRange(0, 100)
        self.opacity.setValue(100)
        self.opacity.setSuffix("%")
        self.opacity.setFixedWidth(68)
        self.opacity_slider = qtw.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.opacity_slider.setObjectName("heatmapAppearanceOpacitySlider")
        self.opacity_slider.setRange(0, 100)
        self.opacity_slider.setValue(100)
        visibility_layout = qtw.QHBoxLayout()
        visibility_layout.setContentsMargins(0, 0, 0, 0)
        visibility_layout.setSpacing(6)
        visibility_layout.addWidget(self.visible)
        visibility_layout.addSpacing(16)
        visibility_layout.addWidget(qtw.QLabel("Opacity"))
        visibility_layout.addWidget(self.opacity_slider, 1)
        visibility_layout.addWidget(self.opacity)
        panel_layout.addLayout(visibility_layout)

        self.x_axis = qtw.QComboBox()
        self.x_axis.addItems(["Bottom", "Top"])
        self.x_axis.setToolTip("Choose the horizontal axis for this heatmap.")
        self.y_axis = qtw.QComboBox()
        self.y_axis.addItems(["Left", "Right"])
        self.y_axis.setToolTip("Choose the vertical axis for this heatmap.")
        for combo in (self.x_axis, self.y_axis):
            combo.setFixedWidth(108)
        heatmap_axes = qtw.QGroupBox("Heatmap axes", panel)
        heatmap_axes_grid = qtw.QGridLayout(heatmap_axes)
        heatmap_axes_grid.setContentsMargins(8, 10, 8, 8)
        heatmap_axes_grid.addWidget(qtw.QLabel("Horizontal"), 0, 0)
        heatmap_axes_grid.addWidget(self.x_axis, 0, 1)
        heatmap_axes_grid.addWidget(qtw.QLabel("Vertical"), 1, 0)
        heatmap_axes_grid.addWidget(self.y_axis, 1, 1)
        heatmap_axes_grid.setColumnStretch(2, 1)
        panel_layout.addWidget(heatmap_axes)

        self.swap_axes = qtw.QCheckBox("Swap X/Y")
        self.swap_axes.setToolTip("Exchange the two plotted heatmap axes.")
        plot_axes = qtw.QGroupBox("Plot axes", panel)
        plot_axes_layout = qtw.QVBoxLayout(plot_axes)
        plot_axes_layout.setContentsMargins(8, 10, 8, 8)
        plot_axes_layout.addWidget(self.swap_axes)
        panel_layout.addWidget(plot_axes)
        panel_layout.addStretch()
        body.addWidget(panel, 4)

        buttons = qtw.QDialogButtonBox(
            qtw.QDialogButtonBox.StandardButton.Close,
            self,
        )
        buttons.rejected.connect(self.close)
        main.addWidget(buttons)

        self.opacity_slider.valueChanged.connect(self.opacity.setValue)
        self.opacity.valueChanged.connect(self.opacity_slider.setValue)
        self.opacity.valueChanged.connect(self._apply_selection)
        self.visible.toggled.connect(self._apply_selection)
        self.x_axis.currentTextChanged.connect(self._apply_axis_selection)
        self.y_axis.currentTextChanged.connect(self._apply_axis_selection)
        self.swap_axes.toggled.connect(self._swap_axes_toggled)
        self.color_scale_button.clicked.connect(self._open_color_scale)
        self.add_heatmap_combo.currentIndexChanged.connect(
            self._add_heatmap_selection_changed
        )
        self.add_heatmap_button.clicked.connect(self._add_selected_heatmap)
        self.remove_heatmap_button.clicked.connect(
            self._remove_selected_heatmaps
        )
        self._update_control_enabled_states(False)
        self.sync_swap_axes_control()
        self.refresh_color_scale(refresh_rows=False)

    def refresh_color_scale(self, *, refresh_rows: bool = True) -> None:
        """Refresh the shared color-map name and both kinds of preview."""

        name_getter = getattr(self.owner, "_current_colorbar_colormap_name", None)
        name = str(name_getter()) if callable(name_getter) else "viridis"
        self.color_scale_name.setText(_COLORBAR_COLORMAP_LABELS.get(name, name))
        self.color_scale_name.setToolTip(name)
        self.color_scale_preview.setPixmap(
            _colorbar_colormap_preview(name, width=190, height=18)
        )
        if refresh_rows:
            self.refresh_rows()

    def _open_color_scale(self, _checked: bool = False) -> None:
        opener = getattr(self.owner, "open_colorbar_scale_dialog", None)
        if callable(opener):
            opener()

    def sync_swap_axes_control(self) -> None:
        can_swap = getattr(self.owner, "can_swap_plot_axes", None)
        is_swapped = getattr(self.owner, "plot_axes_swapped", None)
        enabled = bool(callable(can_swap) and can_swap())
        checked = bool(enabled and callable(is_swapped) and is_swapped())
        blocked = self.swap_axes.blockSignals(True)
        try:
            self.swap_axes.setEnabled(enabled)
            self.swap_axes.setChecked(checked)
        finally:
            self.swap_axes.blockSignals(blocked)

    def _swap_axes_toggled(self, checked: bool) -> None:
        setter = getattr(self.owner, "set_plot_axes_swapped", None)
        if not callable(setter) or not setter(checked):
            self.sync_swap_axes_control()

    def refresh_available_heatmaps(self) -> None:
        previous_key = self.add_heatmap_combo.currentData()
        plotted_keys = set(self.owner.heatmaps)
        available = [
            (label, key)
            for label, key in self.owner.available_heatmap_candidates()
            if key not in plotted_keys
        ]
        blocked = self.add_heatmap_combo.blockSignals(True)
        try:
            self.add_heatmap_combo.clear()
            self.add_heatmap_combo.addItem(
                "Select a heatmap to add…",
                userData=None,
            )
            for label, key in available:
                self.add_heatmap_combo.addItem(label, userData=key)
            index = self.add_heatmap_combo.findData(previous_key)
            self.add_heatmap_combo.setCurrentIndex(max(index, 0))
        finally:
            self.add_heatmap_combo.blockSignals(blocked)
        self._add_heatmap_selection_changed(
            self.add_heatmap_combo.currentIndex()
        )

    def _add_heatmap_selection_changed(self, _index: int) -> None:
        self.add_heatmap_button.setEnabled(
            self.add_heatmap_combo.currentData() is not None
        )

    def _add_selected_heatmap(self, _checked: bool = False) -> None:
        key = self.add_heatmap_combo.currentData()
        if key is None:
            return
        try:
            added = self.owner.add_heatmap_from_dialog(
                self.add_heatmap_combo.currentText(),
                key,
            )
        except Exception as error:
            qtw.QMessageBox.critical(
                self,
                "Could Not Add Heatmap",
                f"The selected heatmap could not be added:\n{error}",
            )
            added = False
        self.refresh_rows()
        if added:
            self.select_heatmap(key)

    def _remove_selected_heatmaps(self, _checked: bool = False) -> None:
        selected = self._selected_keys()
        if not selected or self.owner._primary_heatmap_key in selected:
            return
        for key in selected:
            layer = self.owner.heatmaps.get(key)
            if layer is not None:
                self.owner.remove_heatmap(
                    self.owner._heatmap_display_label(key, layer),
                    key,
                )
        self.refresh_rows()

    def refresh_rows(self) -> None:
        self.sync_swap_axes_control()
        selected = set(self._selected_keys())
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        try:
            for row, (key, layer) in enumerate(self.owner.heatmaps.items()):
                self.table.insertRow(row)
                label = self.owner._heatmap_display_label(key, layer)
                heatmap_id = (
                    label.split()[0].replace("ID:", "")
                    if label.startswith("ID:")
                    else str(row + 1)
                )
                measurement = self.owner._heatmap_measurement_name(key, layer)
                for column, value in (
                    (self._COL_ID, heatmap_id),
                    (self._COL_MEASUREMENT, measurement),
                ):
                    item = qtw.QTableWidgetItem(value)
                    item.setFlags(
                        item.flags()
                        & ~QtCore.Qt.ItemFlag.ItemIsEditable
                        | QtCore.Qt.ItemFlag.ItemIsDragEnabled
                    )
                    item.setTextAlignment(
                        (
                            QtCore.Qt.AlignmentFlag.AlignRight
                            | QtCore.Qt.AlignmentFlag.AlignVCenter
                        )
                        if column == self._COL_ID
                        else QtCore.Qt.AlignmentFlag.AlignVCenter
                    )
                    item.setToolTip(value)
                    self.table.setItem(row, column, item)

                preview = qtw.QTableWidgetItem()
                preview.setIcon(QtGui.QIcon(self._heatmap_pixmap(layer)))
                preview.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                preview.setFlags(
                    preview.flags()
                    & ~QtCore.Qt.ItemFlag.ItemIsEditable
                    & ~QtCore.Qt.ItemFlag.ItemIsDragEnabled
                )
                preview.setToolTip("Heatmap preview")
                self.table.setItem(row, self._COL_PREVIEW, preview)
                id_item = self.table.item(row, self._COL_ID)
                if id_item is not None:
                    id_item.setData(QtCore.Qt.ItemDataRole.UserRole, key)
                    id_item.setToolTip(label)
                self.table.setRowHeight(row, 28)
                if key in selected:
                    self.table.selectRow(row)
        finally:
            self.table.blockSignals(False)

        self.owner._sync_heatmap_layer_order()
        if self.table.rowCount() and not self._selected_keys():
            self.table.selectRow(0)
        else:
            self._sync_controls_from_selection()
        self.refresh_available_heatmaps()

    def _heatmap_pixmap(self, layer: Any) -> QtGui.QPixmap:
        """Render a small data-shaped image rather than a line glyph."""

        size = QtCore.QSize(64, 22)
        source = (
            self.owner.__dict__.get("dataGrid")
            if layer is self.owner
            else getattr(layer, "data_grid", None)
        )
        try:
            data = np.asarray(source, dtype=float)
        except (TypeError, ValueError):
            data = np.empty((0, 0))
        if data.ndim != 2 or data.size == 0:
            x_values = np.linspace(-1.0, 1.0, size.width())
            y_values = np.linspace(-1.0, 1.0, size.height())[:, None]
            sampled = np.sin(3 * x_values) + np.cos(4 * y_values)
        else:
            y_indices = np.linspace(0, data.shape[0] - 1, size.height()).astype(int)
            x_indices = np.linspace(0, data.shape[1] - 1, size.width()).astype(int)
            sampled = data[np.ix_(y_indices, x_indices)]

        finite = sampled[np.isfinite(sampled)]
        if finite.size:
            low = float(np.min(finite))
            high = float(np.max(finite))
            if high > low:
                normalised = (sampled - low) / (high - low)
            else:
                normalised = np.full(sampled.shape, 0.5)
        else:
            normalised = np.zeros(sampled.shape)

        name_getter = getattr(self.owner, "_current_colorbar_colormap_name", None)
        name = str(name_getter()) if callable(name_getter) else "viridis"
        color_map = _colorbar_colormap_for_name(name)
        if not isinstance(color_map, pg.ColorMap):
            color_map = _colorbar_colormap_for_name("viridis")
        lookup = color_map.getLookupTable(nPts=256, alpha=True)
        image = QtGui.QImage(
            size.width(),
            size.height(),
            QtGui.QImage.Format.Format_ARGB32,
        )
        alpha = self.owner._heatmap_opacity(layer)
        if not self.owner._heatmap_is_visible(layer):
            alpha *= 0.35
        for y_position in range(size.height()):
            source_y = size.height() - y_position - 1
            for x_position in range(size.width()):
                value = normalised[source_y, x_position]
                if not np.isfinite(value):
                    image.setPixelColor(
                        x_position,
                        y_position,
                        QtGui.QColor(0, 0, 0, 0),
                    )
                    continue
                rgba = lookup[min(255, max(0, round(float(value) * 255)))]
                color = QtGui.QColor(*[int(channel) for channel in rgba[:4]])
                color.setAlphaF(alpha * color.alphaF())
                image.setPixelColor(x_position, y_position, color)
        return QtGui.QPixmap.fromImage(image)

    def _selected_rows(self) -> list[int]:
        model = self.table.selectionModel()
        if model is None:
            return []
        rows = sorted({index.row() for index in model.selectedRows()})
        current_row = self.table.currentRow()
        if not rows and 0 <= current_row < self.table.rowCount():
            return [current_row]
        return rows

    def _selected_keys(self) -> list[Any]:
        keys = []
        for row in self._selected_rows():
            item = self.table.item(row, self._COL_ID)
            if item is not None:
                key = item.data(QtCore.Qt.ItemDataRole.UserRole)
                if key in self.owner.heatmaps:
                    keys.append(key)
        return keys

    def _key_for_row(self, row: int) -> Any:
        item = self.table.item(row, self._COL_ID)
        return (
            None
            if item is None
            else item.data(QtCore.Qt.ItemDataRole.UserRole)
        )

    def select_heatmap(self, key: Any) -> bool:
        for row in range(self.table.rowCount()):
            if self._key_for_row(row) != key:
                continue
            self.table.clearSelection()
            self.table.selectRow(row)
            item = self.table.item(row, self._COL_ID)
            if item is not None:
                self.table.setCurrentItem(item)
                self.table.scrollToItem(item)
            return True
        return False

    def _move_rows_to_position(
        self,
        source_rows: list[int],
        destination_row: int,
    ) -> None:
        keys = list(self.owner.heatmaps)
        source_rows = sorted(
            {row for row in source_rows if 0 <= row < len(keys)}
        )
        if not source_rows:
            return
        destination_row = max(0, min(destination_row, len(keys)))
        selected = [keys[row] for row in source_rows]
        destination_row -= sum(row < destination_row for row in source_rows)
        remaining = [
            key for row, key in enumerate(keys) if row not in source_rows
        ]
        reordered = (
            remaining[:destination_row]
            + selected
            + remaining[destination_row:]
        )
        if reordered == keys:
            return
        heatmaps = self.owner.heatmaps
        reordered_layers = [(key, heatmaps[key]) for key in reordered]
        heatmaps.clear()
        heatmaps.update(reordered_layers)
        self.owner._sync_heatmap_layer_order()
        self.refresh_rows()

    def _sync_controls_from_selection(self) -> None:
        if self._building:
            return
        keys = self._selected_keys()
        if not keys:
            self._update_control_enabled_states(False)
            return
        layer = self.owner.heatmaps[keys[0]]
        self._building = True
        try:
            self.visible.setChecked(self.owner._heatmap_is_visible(layer))
            self.opacity.setValue(
                round(self.owner._heatmap_opacity(layer) * 100)
            )
            x_axis, y_axis = self.owner._heatmap_axis_sides(layer)
            self.x_axis.setCurrentText(x_axis)
            self.y_axis.setCurrentText(y_axis)
        finally:
            self._building = False
        self._update_control_enabled_states(True)

    def _update_control_enabled_states(self, has_selection: bool) -> None:
        self.visible.setEnabled(has_selection)
        self.opacity.setEnabled(has_selection)
        self.opacity_slider.setEnabled(has_selection)
        self.x_axis.setEnabled(has_selection)
        self.y_axis.setEnabled(has_selection)
        keys = self._selected_keys() if has_selection else []
        self.remove_heatmap_button.setEnabled(
            bool(keys) and self.owner._primary_heatmap_key not in keys
        )

    def _apply_selection(self, *_args: Any) -> None:
        if self._building:
            return
        keys = self._selected_keys()
        self._update_control_enabled_states(bool(keys))
        for key in keys:
            layer = self.owner.heatmaps[key]
            self.owner._set_layer_visibility(layer, self.visible.isChecked())
            self.owner._set_layer_opacity(layer, self.opacity.value() / 100)
        self.refresh_rows()

    def _apply_axis_selection(self, *_args: Any) -> None:
        if self._building:
            return
        keys = self._selected_keys()
        self._update_control_enabled_states(bool(keys))
        for key in keys:
            self.owner._set_layer_axes(
                self.owner.heatmaps[key],
                self.x_axis.currentText(),
                self.y_axis.currentText(),
            )
