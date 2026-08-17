"""Secondary heatmap layers for :mod:`qplot.windows.plot2d`.

The primary heatmap remains owned by ``plot2d``.  This module supplies the
renderers and source-window lifecycle needed to place additional compatible
heatmaps in the same view, plus lightweight controls for opacity and removal.
All layers attached to one host deliberately share its colour map and levels.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pyqtgraph as pg
from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from qplot.tools.heatmap_geometry import (
    HeatmapGeometry,
    canonicalize_heatmap_data,
)

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
        if geometry is None:
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
        """Register the primary heatmap and create the layer-control panel."""

        primary_key = self._window_heatmap_key(self)
        if primary_key is None:
            primary_key = _PRIMARY_HEATMAP_KEY
        self._primary_heatmap_key = primary_key

        heatmaps = self.__dict__.get("heatmaps")
        if not isinstance(heatmaps, dict):
            heatmaps = {}
            self.__dict__["heatmaps"] = heatmaps
        heatmaps.setdefault(primary_key, self)

        rows = self.__dict__.get("_heatmap_layer_rows")
        if not isinstance(rows, dict):
            rows = {}
            self.__dict__["_heatmap_layer_rows"] = rows

        if self.__dict__.get("_heatmap_layer_layout") is None:
            self._init_heatmap_layer_controls()
        self._add_heatmap_layer_row(primary_key, self, removable=False)

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
        """Add a compatible source heatmap as a translucent shared-axis layer."""

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
            self._add_heatmap_layer_row(trace_key, layer, removable=True)
            self._sync_heatmap_colorbar_items(rescale=True)
            self._sync_secondary_heatmap_view_ranges(layer=layer)
        except Exception:
            if layer is not None:
                layer.disconnect_source_updates()
                layer.remove_renderers()
            self.heatmaps.pop(trace_key, None)
            self._remove_heatmap_layer_row(trace_key)
            if retained:
                self._emit_remove_dataset(dataset_key)
            raise
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
        self._remove_heatmap_layer_row(selected_key)
        layer.disconnect_source_updates()
        layer.remove_renderers()
        dataset_key = getattr(getattr(layer, "from_win", None), "_dataset_key", None)
        if dataset_key is not None:
            self._emit_remove_dataset(dataset_key)
        self._sync_heatmap_colorbar_items(rescale=True)
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

        view_box = self.__dict__.get("vb")
        view_range = getattr(view_box, "viewRange", None)
        if not callable(view_range):
            return
        try:
            host_x_range, host_y_range = view_range()
        except (TypeError, ValueError, RuntimeError):
            return

        if layer is None:
            layers = [
                candidate
                for candidate in self.__dict__.get("heatmaps", {}).values()
                if candidate is not self
            ]
        else:
            layers = [layer]

        host_ranges = {
            "x": tuple(host_x_range),
            "y": tuple(host_y_range),
        }
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

    def _init_heatmap_layer_controls(self) -> None:
        if qtw.QApplication.instance() is None:
            return
        axes_dock = self.__dict__.get("axes_dock")
        if axes_dock is None:
            return

        container = qtw.QWidget()
        layout = qtw.QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        heading = qtw.QLabel("Heatmaps")
        layout.addWidget(heading)

        content_layout = getattr(axes_dock, "content_layout", None)
        if content_layout is not None and hasattr(content_layout, "insertWidget"):
            insertion_index = max(0, content_layout.count() - 1)
            content_layout.insertWidget(insertion_index, container)
        else:
            add_widget = getattr(axes_dock, "addWidget", None)
            if not callable(add_widget):
                return
            add_widget(container)

        self.__dict__["_heatmap_layer_controls"] = container
        self.__dict__["_heatmap_layer_layout"] = layout

    def _add_heatmap_layer_row(
        self,
        trace_key: Any,
        layer: Any,
        *,
        removable: bool,
    ) -> None:
        rows = self.__dict__.setdefault("_heatmap_layer_rows", {})
        if trace_key in rows:
            return
        layout = self.__dict__.get("_heatmap_layer_layout")
        if layout is None or qtw.QApplication.instance() is None:
            return

        row = qtw.QWidget()
        row.setObjectName("heatmapLayerRow")
        row_layout = qtw.QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)

        if layer is self:
            label_text = str(self.__dict__.get("label", trace_key))
        else:
            label_text = str(getattr(layer, "label", trace_key))
        label_widget = qtw.QLabel(label_text)
        label_widget.setToolTip(label_text)
        row_layout.addWidget(label_widget, 1)

        opacity_slider = qtw.QSlider(QtCore.Qt.Orientation.Horizontal)
        opacity_slider.setObjectName("heatmapOpacitySlider")
        opacity_slider.setRange(0, 100)
        opacity_slider.setToolTip("Heatmap opacity")
        initial_opacity = (
            getattr(layer, "opacity", DEFAULT_OVERLAY_OPACITY)
            if removable
            else self.__dict__.get("_primary_heatmap_opacity", 1.0)
        )
        opacity_slider.setValue(round(float(initial_opacity) * 100))
        opacity_slider.valueChanged.connect(
            lambda value, selected_layer=layer: self._set_layer_opacity(
                selected_layer,
                value / 100,
            )
        )
        row_layout.addWidget(opacity_slider)

        remove_button = qtw.QPushButton("X")
        remove_button.setObjectName("removeHeatmapButton")
        remove_button.setFixedWidth(24)
        remove_button.setToolTip("Remove this heatmap from the plot")
        remove_button.setEnabled(removable)
        if removable:
            remove_button.clicked.connect(
                lambda _checked=False, selected_key=trace_key: self.remove_heatmap(
                    trace_key=selected_key,
                )
            )
        row_layout.addWidget(remove_button)

        row.label_widget = label_widget
        row.opacity_slider = opacity_slider
        row.remove_button = remove_button
        rows[trace_key] = row
        layout.addWidget(row)

    def _remove_heatmap_layer_row(self, trace_key: Any) -> None:
        rows = self.__dict__.get("_heatmap_layer_rows", {})
        row = rows.pop(trace_key, None)
        if row is None:
            return
        row.setParent(None)
        row.deleteLater()

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
