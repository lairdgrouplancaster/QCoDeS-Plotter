from types import SimpleNamespace

import numpy as np
import pyqtgraph as pg
import pytest
from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from qplot.windows._dataset_handle import DatasetKey, TraceKey
from qplot.windows._plot2d_layers import (
    HeatmapLayer,
    Plot2DLayerMixin,
    _heatmap_axis_order,
    _heatmap_layer_compatibility,
)


class _Monitor:
    def __init__(self):
        self.stop_count = 0

    def stop(self):
        self.stop_count += 1

    def isActive(self):
        return False


class _SourceWindow(QtCore.QObject):
    trace_updated = QtCore.pyqtSignal()
    end_wait = QtCore.pyqtSignal()

    def __init__(
            self,
            trace_key,
            *,
            axis_options,
            x_values,
            y_values,
            data_grid,
            visible=True,
            value_unit="A",
            axis_units=None,
            ):
        super().__init__()
        self._dataset_key = trace_key.dataset_key
        self._trace_key = trace_key
        self.label = f"ID:2 {trace_key.parameter_name}"
        self.axis_options = dict(axis_options)
        self.axis_data = {
            "x": np.asarray(x_values, dtype=float),
            "y": np.asarray(y_values, dtype=float),
        }
        self.dataGrid = np.asarray(data_grid, dtype=float)
        self.param = SimpleNamespace(unit=value_unit)
        self.display_param = SimpleNamespace(unit=value_unit)
        axis_units = axis_units or {}
        self.param_dict = {
            name: SimpleNamespace(unit=axis_units.get(name, "V"))
            for name in self.axis_options.values()
        }
        self.worker = SimpleNamespace(running=False)
        self.ds = SimpleNamespace(running=False)
        self.visible = visible
        self._closed = not visible
        self._merged_trace_users = 0
        self.monitor = _Monitor()
        self.spinBox = qtw.QDoubleSpinBox()
        self.spinBox.setValue(0.5)


class _LayerHost(Plot2DLayerMixin):
    def __init__(
            self,
            trace_key,
            *,
            axis_options,
            data_grid=None,
            value_unit="A",
            axis_units=None,
            ):
        self._trace_key = trace_key
        self._dataset_key = trace_key.dataset_key
        self.label = f"ID:1 {trace_key.parameter_name}"
        self._axis_options = dict(axis_options)
        self.dataGrid = np.asarray(
            np.zeros((2, 2)) if data_grid is None else data_grid,
            dtype=float,
        )
        self.param = SimpleNamespace(unit=value_unit)
        self.display_param = SimpleNamespace(unit=value_unit)
        axis_units = axis_units or {}
        self.param_dict = {
            name: SimpleNamespace(unit=axis_units.get(name, "V"))
            for name in self._axis_options.values()
        }
        self.widget = pg.GraphicsLayoutWidget()
        self.plot = self.widget.addPlot()
        self.vb = self.plot.vb
        self.image = pg.ImageItem(axisOrder="row-major")
        self.heatmap_mesh = pg.PColorMeshItem()
        self.plot.addItem(self.image)
        self.plot.addItem(self.heatmap_mesh)
        self.spinBox = qtw.QDoubleSpinBox()
        self.spinBox.setValue(0.25)

    @property
    def axis_options(self):
        return dict(self._axis_options)

    def close(self):
        for layer in list(getattr(self, "heatmaps", {}).values()):
            if isinstance(layer, HeatmapLayer):
                layer.disconnect_source_updates()
                layer.remove_renderers()
        self.spinBox.deleteLater()
        self.widget.deleteLater()


def _trace_key(tmp_path, guid, parameter):
    return TraceKey(
        DatasetKey(str(tmp_path / f"{guid}.db"), guid),
        parameter,
    )


def _new_layer(
        tmp_path,
        *,
        parent_axes=None,
        source_axes=None,
        x_values=(0.0, 1.0),
        y_values=(10.0, 12.0),
        data_grid=((1.0, 2.0), (3.0, 4.0)),
        visible=True,
        parent_value_unit="A",
        source_value_unit="A",
        parent_axis_units=None,
        source_axis_units=None,
        ):
    parent_axes = parent_axes or {"x": "field", "y": "gate"}
    source_axes = source_axes or dict(parent_axes)
    host = _LayerHost(
        _trace_key(tmp_path, "host-guid", "host_signal"),
        axis_options=parent_axes,
        value_unit=parent_value_unit,
        axis_units=parent_axis_units,
    )
    source = _SourceWindow(
        _trace_key(tmp_path, "source-guid", "source_signal"),
        axis_options=source_axes,
        x_values=x_values,
        y_values=y_values,
        data_grid=data_grid,
        visible=visible,
        value_unit=source_value_unit,
        axis_units=source_axis_units,
    )
    return host, source, HeatmapLayer(host, source)


@pytest.mark.parametrize(
    ("parent_axes", "source_axes", "expected"),
    [
        (
            {"x": "field", "y": "gate"},
            {"x": "field", "y": "gate"},
            ("x", "y"),
        ),
        (
            {"x": "field", "y": "gate"},
            {"x": "gate", "y": "field"},
            ("y", "x"),
        ),
        (
            {"x": "field", "y": "gate"},
            {"x": "field", "y": "bias"},
            None,
        ),
    ],
    ids=("same", "swapped", "mismatch"),
)
def test_heatmap_axis_order_maps_same_and_swapped_axes(
        parent_axes,
        source_axes,
        expected,
        ):
    assert _heatmap_axis_order(parent_axes, source_axes) == expected


def test_uniform_layer_uses_image_renderer_and_preserves_grid(tmp_path):
    source_grid = np.array([[1.0, 2.0], [3.0, 4.0]])
    host, source, layer = _new_layer(tmp_path, data_grid=source_grid)

    try:
        assert layer.geometry is not None
        assert layer.geometry.is_uniform
        assert layer.image.isVisible()
        assert not layer.heatmap_mesh.isVisible()
        assert layer.render_items() == [layer.image, layer.heatmap_mesh]
        np.testing.assert_array_equal(layer.data_grid, source_grid)
        np.testing.assert_array_equal(layer.image.image, source_grid)
        np.testing.assert_array_equal(source.dataGrid, source_grid)
    finally:
        host.close()
        source.spinBox.deleteLater()


def test_swapped_layer_transposes_grid_and_geometry(tmp_path):
    source_grid = np.array([
        [1.0, 2.0, 3.0],
        [4.0, 5.0, 6.0],
    ])
    host, source, layer = _new_layer(
        tmp_path,
        parent_axes={"x": "gate", "y": "field"},
        source_axes={"x": "field", "y": "gate"},
        x_values=(10.0, 20.0, 30.0),
        y_values=(1.0, 2.0),
        data_grid=source_grid,
    )

    try:
        np.testing.assert_array_equal(layer.data_grid, source_grid.T)
        np.testing.assert_array_equal(layer.image.image, source_grid.T)
        assert layer.geometry.x.centres == (1.0, 2.0)
        assert layer.geometry.y.centres == (10.0, 20.0, 30.0)
        np.testing.assert_array_equal(source.dataGrid, source_grid)
    finally:
        host.close()
        source.spinBox.deleteLater()


def test_nonuniform_layer_uses_mesh_with_exact_edges(tmp_path):
    source_grid = np.array([
        [1.0, 2.0, 3.0],
        [4.0, 5.0, 6.0],
    ])
    host, source, layer = _new_layer(
        tmp_path,
        x_values=(0.0, 1.0, 4.0),
        y_values=(10.0, 13.0),
        data_grid=source_grid,
    )

    try:
        assert not layer.geometry.is_uniform
        assert not layer.image.isVisible()
        assert layer.heatmap_mesh.isVisible()
        np.testing.assert_array_equal(
            layer.heatmap_mesh.x,
            [[-0.5, 0.5, 2.5, 5.5]] * 3,
        )
        np.testing.assert_array_equal(
            layer.heatmap_mesh.y,
            [[8.5] * 4, [11.5] * 4, [14.5] * 4],
        )
        np.testing.assert_array_equal(layer.heatmap_mesh.z, source_grid)
    finally:
        host.close()
        source.spinBox.deleteLater()


def test_source_trace_update_refreshes_layer_data(tmp_path):
    host, source, layer = _new_layer(tmp_path)
    replacement = np.array([[11.0, 12.0], [13.0, 14.0]])

    try:
        source.dataGrid = replacement
        source.trace_updated.emit()

        np.testing.assert_array_equal(layer.data_grid, replacement)
        np.testing.assert_array_equal(layer.image.image, replacement)
    finally:
        host.close()
        source.spinBox.deleteLater()


def test_source_consumer_is_released_exactly_once(tmp_path):
    host, source, layer = _new_layer(tmp_path, visible=False)
    cancellations = []
    source.worker = SimpleNamespace(
        running=True,
        cancel=lambda: cancellations.append(True),
    )

    try:
        assert source._merged_trace_users == 1

        layer.disconnect_source_updates()
        layer.disconnect_source_updates()

        assert source._merged_trace_users == 0
        assert source.monitor.stop_count == 1
        assert cancellations == [True]
    finally:
        host.close()
        source.spinBox.deleteLater()


@pytest.mark.parametrize(
    ("source_value_unit", "source_axis_units", "expected_error"),
    [
        ("A/V", None, "displayed value units"),
        ("A", {"field": "mV", "gate": "V"}, "field axis units"),
    ],
)
def test_add_rejects_incompatible_units_before_retaining_source(
        tmp_path,
        source_value_unit,
        source_axis_units,
        expected_error,
        ):
    host = _LayerHost(
        _trace_key(tmp_path, "host-guid", "host_signal"),
        axis_options={"x": "field", "y": "gate"},
    )
    source = _SourceWindow(
        _trace_key(tmp_path, "source-guid", "source_signal"),
        axis_options={"x": "field", "y": "gate"},
        x_values=(0.0, 1.0),
        y_values=(10.0, 12.0),
        data_grid=((1.0, 2.0), (3.0, 4.0)),
        value_unit=source_value_unit,
        axis_units=source_axis_units,
    )

    try:
        host._init_heatmap_layers()

        assert not host.add_heatmap(source)
        assert set(host.heatmaps) == {host._trace_key}
        assert source._merged_trace_users == 0
        _axis_order, error = _heatmap_layer_compatibility(host, source)
        assert error is not None
        assert expected_error in error
    finally:
        host.close()
        source.spinBox.deleteLater()


def test_display_unit_change_suspends_and_restores_existing_layer(tmp_path):
    host, source, layer = _new_layer(tmp_path)
    original_grid = np.asarray(layer.data_grid).copy()

    try:
        assert source._merged_trace_users == 1

        source.display_param.unit = "A/V"
        source.trace_updated.emit()

        assert layer.compatibility_error == "the displayed value units do not match"
        assert layer.geometry is None
        assert layer.data_grid.size == 0
        assert not layer.image.isVisible()
        assert not layer.heatmap_mesh.isVisible()
        assert source._merged_trace_users == 1

        source.display_param.unit = "A"
        source.trace_updated.emit()

        assert layer.compatibility_error is None
        assert layer.geometry is not None
        np.testing.assert_array_equal(layer.data_grid, original_grid)
        assert layer.image.isVisible()
    finally:
        host.close()
        source.spinBox.deleteLater()


def test_target_display_unit_change_suspends_layer_on_host_refresh(tmp_path):
    host, source, layer = _new_layer(tmp_path)
    host.heatmaps = {host._trace_key: host, layer.trace_key: layer}

    try:
        host.display_param.unit = "A/V"
        host.refresh_secondary_heatmaps()
        assert layer.geometry is None
        assert layer.data_grid.size == 0

        host.display_param.unit = "A"
        host.refresh_secondary_heatmaps()
        assert layer.geometry is not None
        assert layer.image.isVisible()
    finally:
        host.close()
        source.spinBox.deleteLater()


def _install_test_colorbar(host):
    bar = pg.ColorBarItem(
        values=(0.0, 10.0),
        colorMap=pg.colormap.get("viridis"),
        interactive=True,
    )
    items = host._heatmap_colorbar_items()
    bar.setImageItem(items)
    host.bar = bar
    host._heatmap_colorbar_item_ids = tuple(id(item) for item in items)
    host.relevel_refresh = qtw.QCheckBox()
    return bar


def test_source_refresh_respects_remap_colors_setting(tmp_path):
    host = _LayerHost(
        _trace_key(tmp_path, "host-guid", "host_signal"),
        axis_options={"x": "field", "y": "gate"},
        data_grid=((1.0, 2.0), (3.0, 4.0)),
    )
    source = _SourceWindow(
        _trace_key(tmp_path, "source-guid", "source_signal"),
        axis_options=host.axis_options,
        x_values=(0.0, 1.0),
        y_values=(10.0, 12.0),
        data_grid=((101.0, 102.0), (103.0, 104.0)),
    )

    try:
        host._init_heatmap_layers()
        bar = _install_test_colorbar(host)
        assert host.add_heatmap(source)
        bar.setLevels((0.0, 10.0))

        source.dataGrid = np.array([[201.0, 202.0], [203.0, 204.0]])
        source.trace_updated.emit()
        assert bar.levels() == (0.0, 10.0)

        host.relevel_refresh.setChecked(True)
        source.trace_updated.emit()
        assert bar.levels() == (1.0, 204.0)
    finally:
        host.close()
        source.spinBox.deleteLater()


def test_colorbar_rebind_disconnects_retained_and_removed_meshes(tmp_path):
    host = _LayerHost(
        _trace_key(tmp_path, "host-guid", "host_signal"),
        axis_options={"x": "field", "y": "gate"},
    )
    source = _SourceWindow(
        _trace_key(tmp_path, "source-guid", "source_signal"),
        axis_options=host.axis_options,
        x_values=(0.0, 1.0),
        y_values=(10.0, 12.0),
        data_grid=((1.0, 2.0), (3.0, 4.0)),
    )

    try:
        host._init_heatmap_layers()
        _install_test_colorbar(host)
        assert host.heatmap_mesh.receivers(host.heatmap_mesh.sigLevelsChanged) == 1

        assert host.add_heatmap(source)
        layer = host.heatmaps[source._trace_key]
        assert host.heatmap_mesh.receivers(host.heatmap_mesh.sigLevelsChanged) == 1
        assert layer.heatmap_mesh.receivers(layer.heatmap_mesh.sigLevelsChanged) == 1

        assert host.remove_heatmap(trace_key=source._trace_key)
        assert host.heatmap_mesh.receivers(host.heatmap_mesh.sigLevelsChanged) == 1
        assert layer.heatmap_mesh.receivers(layer.heatmap_mesh.sigLevelsChanged) == 0
    finally:
        host.close()
        source.spinBox.deleteLater()


@pytest.mark.parametrize(
    ("source_axes", "expected_x", "expected_y"),
    [
        ({"x": "field", "y": "gate"}, (1.0, 2.0), (10.0, 20.0)),
        ({"x": "gate", "y": "field"}, (10.0, 20.0), (1.0, 2.0)),
    ],
)
def test_hidden_large_heatmap_source_tracks_host_viewport(
        tmp_path,
        source_axes,
        expected_x,
        expected_y,
        ):
    host, source, layer = _new_layer(
        tmp_path,
        source_axes=source_axes,
        x_values=(0.0, 1.0),
        y_values=(10.0, 12.0),
        visible=False,
    )
    applied_ranges = []
    scheduled = []
    host.vb = SimpleNamespace(viewRange=lambda: ((1.0, 2.0), (10.0, 20.0)))
    source._large_heatmap_sql_mode = True
    source.vb = SimpleNamespace(
        setRange=lambda **ranges: applied_ranges.append(ranges),
    )
    source._schedule_visible_heatmap_reload = lambda: scheduled.append(True)

    try:
        host.heatmaps = {host._trace_key: host, layer.trace_key: layer}
        host._sync_secondary_heatmap_view_ranges()

        assert applied_ranges == [
            {"xRange": expected_x, "yRange": expected_y, "padding": 0},
        ]
        assert scheduled == [True]
    finally:
        host.close()
        source.spinBox.deleteLater()


def test_visible_large_heatmap_source_viewport_is_not_hijacked(tmp_path):
    host, source, layer = _new_layer(tmp_path, visible=True)
    applied_ranges = []
    scheduled = []
    host.vb = SimpleNamespace(viewRange=lambda: ((1.0, 2.0), (10.0, 20.0)))
    source._large_heatmap_sql_mode = True
    source.vb = SimpleNamespace(
        setRange=lambda **ranges: applied_ranges.append(ranges),
    )
    source._schedule_visible_heatmap_reload = lambda: scheduled.append(True)

    try:
        host.heatmaps = {host._trace_key: host, layer.trace_key: layer}
        host._sync_secondary_heatmap_view_ranges()

        assert applied_ranges == []
        assert scheduled == []
    finally:
        host.close()
        source.spinBox.deleteLater()


def test_shared_colorbar_data_includes_primary_and_secondary_grids(tmp_path):
    primary_grid = np.array([[1.0, 2.0], [3.0, 4.0]])
    secondary_grid = np.array([[101.0, 102.0], [103.0, 104.0]])
    host = _LayerHost(
        _trace_key(tmp_path, "host-guid", "host_signal"),
        axis_options={"x": "field", "y": "gate"},
        data_grid=primary_grid,
    )
    source = _SourceWindow(
        _trace_key(tmp_path, "source-guid", "source_signal"),
        axis_options=host.axis_options,
        x_values=(0.0, 1.0),
        y_values=(10.0, 12.0),
        data_grid=secondary_grid,
    )
    layer = None

    try:
        host._init_heatmap_layers()
        layer = HeatmapLayer(host, source)
        host.heatmaps[layer.trace_key] = layer

        arrays = host._heatmap_colorbar_data_arrays()

        assert len(arrays) == 2
        np.testing.assert_array_equal(arrays[0], primary_grid)
        np.testing.assert_array_equal(arrays[1], secondary_grid)
    finally:
        host.close()
        source.spinBox.deleteLater()


def test_registry_supports_multiple_secondary_heatmaps(tmp_path):
    host = _LayerHost(
        _trace_key(tmp_path, "host-guid", "host_signal"),
        axis_options={"x": "field", "y": "gate"},
        data_grid=((1.0, 2.0), (3.0, 4.0)),
    )
    sources = [
        _SourceWindow(
            _trace_key(tmp_path, f"source-{index}", f"signal_{index}"),
            axis_options=host.axis_options,
            x_values=(0.0, 1.0),
            y_values=(10.0, 12.0),
            data_grid=np.full((2, 2), 10.0 * index),
        )
        for index in (1, 2)
    ]

    try:
        host._init_heatmap_layers()
        assert all(host.add_heatmap(source) for source in sources)

        assert set(host.heatmaps) == {
            host._trace_key,
            *(source._trace_key for source in sources),
        }
        layers = [host.heatmaps[source._trace_key] for source in sources]
        assert len({id(layer.image) for layer in layers}) == 2
        assert layers[0].image.zValue() < layers[1].image.zValue() < 1
        assert len(host._heatmap_colorbar_data_arrays()) == 3
    finally:
        host.close()
        for source in sources:
            source.spinBox.deleteLater()


def test_heatmap_renderers_move_together_between_all_axis_viewboxes(tmp_path):
    host, source, layer = _new_layer(tmp_path)

    try:
        host._init_heatmap_layers()
        host.heatmaps[layer.trace_key] = layer
        host._heatmap_axis_assignments[layer.trace_key] = {
            "x": "Bottom",
            "y": "Left",
        }

        host._set_layer_axes(layer, "Top", "Right")

        assert host._heatmap_axis_sides(layer) == ("Top", "Right")
        assert layer.image.getViewBox() is host.top_right_vb
        assert layer.heatmap_mesh.getViewBox() is host.top_right_vb
        assert host.plot.getAxis("top").style["showValues"]
        assert host.plot.getAxis("right").style["showValues"]

        host._set_layer_axes(layer, "Bottom", "Right")
        assert layer.image.getViewBox() is host.right_vb
        assert layer.heatmap_mesh.getViewBox() is host.right_vb

        host._set_layer_axes(layer, "Top", "Left")
        assert layer.image.getViewBox() is host.top_vb
        assert layer.heatmap_mesh.getViewBox() is host.top_vb

        host._set_layer_axes(layer, "Bottom", "Left")
        assert layer.image.getViewBox() is host.vb
        assert layer.heatmap_mesh.getViewBox() is host.vb
        assert not host.plot.getAxis("top").style["showValues"]
        assert not host.plot.getAxis("right").style["showValues"]
    finally:
        host.close()
        source.spinBox.deleteLater()


def test_primary_heatmap_interactions_follow_its_selected_viewbox(tmp_path):
    host = _LayerHost(
        _trace_key(tmp_path, "host-guid", "host_signal"),
        axis_options={"x": "field", "y": "gate"},
    )
    host.hover_pixel_outline = qtw.QGraphicsRectItem()
    host.plot.addItem(host.hover_pixel_outline)

    try:
        host._init_heatmap_layers()
        host._set_layer_axes(host, "Top", "Right")

        assert host.image.getViewBox() is host.top_right_vb
        assert host.heatmap_mesh.getViewBox() is host.top_right_vb
        assert host._heatmap_renderer_viewboxes[
            id(host.hover_pixel_outline)
        ] is host.top_right_vb
        assert host._primary_heatmap_viewbox() is host.top_right_vb
        assert host._primary_heatmap_semantic_axes() == ("x2", "y2")
    finally:
        host.close()
