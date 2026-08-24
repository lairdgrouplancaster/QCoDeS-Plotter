from types import SimpleNamespace

import numpy as np
import pyqtgraph as pg
from PyQt6 import QtWidgets as qtw

from qplot.windows._dataset_handle import DatasetKey, TraceKey
from qplot.windows._plot2d_layers import (
    DEFAULT_OVERLAY_OPACITY,
    Plot2DLayerMixin,
    _HeatmapAppearanceDialog,
)


class _Layer:
    def __init__(self, key, label="ID:2 overlay"):
        self.trace_key = key
        self.label = label
        self.from_win = SimpleNamespace(
            param=SimpleNamespace(name=key.parameter_name),
        )
        self.data_grid = np.array([[0.0, 1.0], [2.0, 3.0]])
        self.image = pg.ImageItem(axisOrder="row-major")
        self.heatmap_mesh = pg.PColorMeshItem()
        self.opacity = DEFAULT_OVERLAY_OPACITY
        self.visible = True

    def render_items(self):
        return [self.image, self.heatmap_mesh]

    def set_opacity(self, value):
        self.opacity = value
        self.image.setOpacity(value)
        self.heatmap_mesh.setOpacity(value)

    def set_visible(self, visible):
        self.visible = visible
        self.image.setVisible(visible)
        self.heatmap_mesh.setVisible(visible)


class _Host(Plot2DLayerMixin, qtw.QMainWindow):
    def __init__(self):
        super().__init__()
        database_key = DatasetKey("database.db", "host-guid")
        self._dataset_key = database_key
        self._trace_key = TraceKey(database_key, "signal")
        self.label = "ID:1 signal"
        self.param = SimpleNamespace(
            name="signal",
            depends_on_=("gate", "field"),
        )
        self.image = pg.ImageItem(axisOrder="row-major")
        self.heatmap_mesh = pg.PColorMeshItem()
        self.dataGrid = np.array([[1.0, 2.0], [4.0, 8.0]])
        self.axis_dropdown = {"x": qtw.QComboBox(), "y": qtw.QComboBox()}
        for dropdown in self.axis_dropdown.values():
            dropdown.addItems(["gate", "field"])
        self.axis_dropdown["x"].setCurrentText("field")
        self.axis_dropdown["y"].setCurrentText("gate")
        self._axis_selection = self.axis_options
        self.refreshes = []
        self.color_scale_opens = 0
        self._init_heatmap_layers()

    @property
    def axis_options(self):
        return {
            axis: dropdown.currentText()
            for axis, dropdown in self.axis_dropdown.items()
        }

    def refreshWindow(self, *, force=False):
        self.refreshes.append(force)

    def _current_colorbar_colormap_name(self):
        return "viridis"

    def open_colorbar_scale_dialog(self):
        self.color_scale_opens += 1


def _key(guid, parameter):
    return TraceKey(DatasetKey("database.db", guid), parameter)


def test_heatmap_appearance_matches_trace_dialog_structure_with_image_preview():
    host = _Host()
    overlay_key = _key("overlay-guid", "overlay")
    overlay = _Layer(overlay_key)
    host.heatmaps[overlay_key] = overlay
    dialog = _HeatmapAppearanceDialog(host)
    try:
        dialog.refresh_rows()

        assert dialog.windowTitle() == "Heatmap Appearance"
        assert dialog.table.columnCount() == 3
        assert [
            dialog.table.horizontalHeaderItem(index).text()
            for index in range(3)
        ] == ["ID", "Preview", "Measurement"]
        assert dialog.table.rowCount() == 2
        assert not dialog.table.item(0, dialog._COL_PREVIEW).icon().isNull()
        assert not dialog.table.item(1, dialog._COL_PREVIEW).icon().isNull()
        assert dialog.color_scale_name.text() == "Viridis"
        assert dialog.color_scale_preview.pixmap() is not None
        assert dialog.color_scale_preview.pixmap().width() == 190
        assert dialog.x_axis.currentText() == "Bottom"
        assert dialog.y_axis.currentText() == "Left"
        assert dialog.x_axis.isEnabled()
        assert dialog.y_axis.isEnabled()

        dialog.color_scale_button.click()
        assert host.color_scale_opens == 1
    finally:
        dialog.deleteLater()
        host.deleteLater()


def test_heatmap_appearance_applies_visibility_opacity_order_and_axis_swap():
    host = _Host()
    overlay_key = _key("overlay-guid", "overlay")
    overlay = _Layer(overlay_key)
    host.heatmaps[overlay_key] = overlay
    dialog = _HeatmapAppearanceDialog(host)
    try:
        dialog.refresh_rows()
        assert dialog.select_heatmap(overlay_key)

        dialog.opacity_slider.setValue(37)
        dialog.visible.setChecked(False)

        assert overlay.opacity == 0.37
        assert not overlay.visible

        dialog.x_axis.setCurrentText("Top")
        dialog.y_axis.setCurrentText("Right")
        assert host._heatmap_axis_sides(overlay) == ("Top", "Right")

        dialog._move_rows_to_position([0], 2)
        assert list(host.heatmaps) == [overlay_key, host._trace_key]
        assert overlay.image.zValue() < host.image.zValue()

        dialog.swap_axes.setChecked(True)
        assert host.axis_options == {"x": "gate", "y": "field"}
        assert host._heatmap_axis_sides(overlay) == ("Top", "Right")
        assert host.refreshes == [True]
    finally:
        dialog.deleteLater()
        host.deleteLater()


def test_heatmap_appearance_add_and_remove_use_owner_lifecycle_hooks():
    host = _Host()
    candidate_key = _key("candidate-guid", "candidate")
    host._heatmap_candidate_provider = lambda: [
        ("ID:3 candidate", candidate_key)
    ]
    added = []
    removed = []

    def add_heatmap(label, key):
        added.append((label, key))
        host.heatmaps[key] = _Layer(key, label)
        return True

    def remove_heatmap(label="", trace_key=None):
        removed.append((label, trace_key))
        host.heatmaps.pop(trace_key)
        return True

    host.add_heatmap_from_dialog = add_heatmap
    host.remove_heatmap = remove_heatmap
    dialog = _HeatmapAppearanceDialog(host)
    try:
        dialog.refresh_rows()
        dialog.add_heatmap_combo.setCurrentIndex(
            dialog.add_heatmap_combo.findData(candidate_key)
        )
        dialog.add_heatmap_button.click()

        assert added == [("ID:3 candidate", candidate_key)]
        assert dialog._selected_keys() == [candidate_key]
        assert dialog.remove_heatmap_button.isEnabled()

        dialog.remove_heatmap_button.click()

        assert removed == [("ID:3 candidate", candidate_key)]
        assert candidate_key not in host.heatmaps
        assert dialog.table.rowCount() == 1
    finally:
        dialog.deleteLater()
        host.deleteLater()
