"""Hover badges share Plot Appearance's existing incompatibility decisions."""

from types import SimpleNamespace

import pytest
from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from qplot.windows._plot_drop_axes import (
    dataset_axis_warning_details,
    dataset_axis_warnings,
)
from tests.windows.test_mixed_plot_drops import _added, _axis_warning_item, _drop
from tests.windows.test_mixed_plot_drops import (
    mixed_plot as mixed_plot,
)
from tests.windows.test_plot_overlay_integration import _assign_trace_axes


def _parameter(name, unit):
    return SimpleNamespace(name=name, label=name.replace("_", " ").title(), unit=unit)


def _metadata_host(
    coordinate="gate", coordinate_unit="V", value="other_current", value_unit="A",
    main_sides=("Bottom", "Left"), incoming_sides=("Bottom", "Left"),
):
    def source(x_name, x_unit, y_name, y_unit):
        parameter = _parameter(y_name, y_unit)
        parameter.depends_on_ = (x_name,)
        return SimpleNamespace(
            param=parameter,
            axis_options={"x": x_name, "y": y_name},
            axis_param={"x": _parameter(x_name, x_unit), "y": parameter},
        )

    host = source("gate", "V", "current", "A")
    incoming = source(coordinate, coordinate_unit, value, value_unit)
    host.line = SimpleNamespace(axis_param=host.axis_param)
    trace = SimpleNamespace(from_win=incoming, axis_param=incoming.axis_param)
    host.lines = {"main": host.line, "incoming": trace}
    host.heatmaps = {}
    host.operation_kind = "plot1d"
    styles = {
        key: SimpleNamespace(x_axis=sides[0], y_axis=sides[1])
        for key, sides in (("main", main_sides), ("incoming", incoming_sides))
    }
    host._ensure_trace_styles = lambda: styles
    host._trace_axis_parameter = lambda item, axis: item.axis_param[axis]
    return host


@pytest.mark.parametrize("metadata,expected", [
    ({"value": "different_current"}, ()),
    ({"coordinate_unit": " V ", "value_unit": " A "}, ()),
    ({"coordinate": "other_gate"}, ("bottom",)),
    ({"coordinate_unit": "K"}, ("bottom",)),
    ({"value_unit": "Hz"}, ("left",)),
    ({"coordinate": "other_gate", "coordinate_unit": "K", "value_unit": "Hz",
      "incoming_sides": ("Top", "Right")}, ()),
    ({"coordinate": "other_gate", "coordinate_unit": "K", "value_unit": "Hz",
      "main_sides": ("Top", "Right"), "incoming_sides": ("Top", "Right")}, ("top", "right")),
])
def test_warning_details_preserve_coordinate_and_measurement_matching(metadata, expected):
    host = _metadata_host(**metadata)
    warnings = dataset_axis_warning_details(host, "trace", "incoming")
    assert tuple(warning.axis for warning in warnings) == expected
    assert dataset_axis_warnings(host, "trace", "incoming") == tuple(warning.message for warning in warnings)
    if metadata.get("coordinate") == "other_gate" and expected:
        assert "Coordinate names differ (dataset: other_gate; plot: gate)." in warnings[0].message


def _assert_appearance_parity(window, dialog, kind, key, expected):
    details = dataset_axis_warning_details(window, kind, key)
    assert tuple(warning.axis for warning in details) == expected
    messages = tuple(warning.message for warning in details)
    assert dataset_axis_warnings(window, kind, key) == messages
    item = _axis_warning_item(dialog, kind, key)
    assert item.icon().isNull() == (not expected)
    assert item.toolTip() == ("Incompatible axes:\n" + "\n".join(messages) if expected else "")
    if expected:
        assert item.data(QtCore.Qt.ItemDataRole.AccessibleTextRole) == item.toolTip()


def _inspect_line(window, line, index=1):
    x, y = line.getData()
    point = line.getViewBox().mapViewToScene(QtCore.QPointF(float(x[index]), float(y[index])))
    window.plot.scene().sigMouseMoved.emit(point)
    assert window._hover_highlight.active_item is line


def _assert_hover_warnings(window, expected):
    hover = window._hover_highlight
    assert hover.warning_axes == expected
    assert set(hover.warning_badges) == set(expected)
    for badge in hover.warning_badges.values():
        assert badge.isVisible()
        assert badge.toolTip() == ""  # Explanations remain in Plot Appearance.


@pytest.mark.parametrize("mixed_plot", ["line"], indirect=True)
@pytest.mark.parametrize("axes", [("Bottom", "Left"), ("Top", "Right")])
def test_trace_hover_warnings_match_appearance_and_clear_with_target_changes(mixed_plot, axes):
    _main, window, runs = mixed_plot
    _drop(window, runs["other_line"])
    line = _added(window, runs["other_line"])
    key = line.from_win._trace_key
    _assign_trace_axes(window, window.label, *axes)
    owner = _assign_trace_axes(window, key, *axes)
    # Separate the two synthetic traces spatially so each can be inspected.
    line.setData(x=[0, 1, 2], y=[10, 11, 12])
    owner.setRange(xRange=[-0.5, 2.5], yRange=[-2, 15], padding=0)
    window.open_plot_appearance_dialog()
    dialog = window._plot_appearance_dialog
    expected = tuple(side.lower() for side in axes)
    _assert_appearance_parity(window, dialog, "trace", key, expected)
    _assert_appearance_parity(window, dialog, "trace", window.label, ())
    dialog.accept()
    qtw.QApplication.processEvents()

    _inspect_line(window, line)
    _assert_hover_warnings(window, expected)
    assert float(window.pos_labels["y"].text()) == 11
    _inspect_line(window, window.line)
    _assert_hover_warnings(window, ())
    _inspect_line(window, line)
    _assert_hover_warnings(window, expected)
    window.snap_to_trace_action.setChecked(False)
    assert window._hover_highlight.active_item is None
    _assert_hover_warnings(window, ())

    # Giving the incoming trace its own axes removes the same warning in
    # both places, without interpreting quantity labels by string equality.
    spare = ("Top", "Right") if axes == ("Bottom", "Left") else ("Bottom", "Left")
    owner = _assign_trace_axes(window, key, *spare)
    owner.setRange(xRange=[-0.5, 2.5], yRange=[8, 14], padding=0)
    dialog.refresh_axis_warnings()
    _assert_appearance_parity(window, dialog, "trace", key, ())
    window.snap_to_trace_action.setChecked(True)
    qtw.QApplication.processEvents()
    _inspect_line(window, line)
    _assert_hover_warnings(window, ())


@pytest.mark.parametrize("mixed_plot", ["heatmap"], indirect=True)
def test_heatmap_colour_scale_details_match_appearance_and_hover_lookup(mixed_plot, monkeypatch):
    _main, window, runs = mixed_plot
    monkeypatch.setattr(qtw.QMessageBox, "exec", lambda box: qtw.QMessageBox.StandardButton.Ok)
    _drop(window, runs["other_heatmap"])
    layer = _added(window, runs["other_heatmap"])
    key = layer.from_win._trace_key
    window.open_plot_appearance_dialog()
    dialog = window._plot_appearance_dialog

    _assert_appearance_parity(window, dialog, "heatmap", key, ("colour_scale",))
    assert window._cursor_axis_warnings("heatmap", layer) == ("colour_scale",)
    assert "Colour scale: dataset q (V) does not match plot z (A)." in dataset_axis_warnings(window, "heatmap", key)
    dialog.reject()


@pytest.mark.parametrize("mixed_plot", ["heatmap"], indirect=True)
def test_primary_heatmap_hover_warns_for_reordered_shared_axis_labels(mixed_plot, monkeypatch):
    _main, window, runs = mixed_plot
    monkeypatch.setattr(qtw.QMessageBox, "exec", lambda box: qtw.QMessageBox.StandardButton.Ok)
    _drop(window, runs["other_heatmap"])
    layer = _added(window, runs["other_heatmap"])
    window._set_layer_axes(layer, "Bottom", "Left")
    window.open_plot_appearance_dialog()
    dialog = window._plot_appearance_dialog
    row = next(row for row in range(dialog.table.rowCount())
               if dialog._row_entry(row) == ("heatmap", layer.from_win._trace_key))
    dialog._move_rows_to_position([row], 0)
    key = window._primary_heatmap_key
    _assert_appearance_parity(window, dialog, "heatmap", key, ("bottom", "left"))
    dialog.accept()
    # Keep the sample clear of inward axis ticks on small offscreen canvases.
    window.resize(1200, 700)
    qtw.QApplication.processEvents()

    point = window._primary_heatmap_viewbox().mapViewToScene(QtCore.QPointF(1, 10))
    window.plot.scene().sigMouseMoved.emit(point)
    assert window._hover_highlight.active_item in window._heatmap_render_items(window)
    _assert_hover_warnings(window, ("bottom", "left"))
    assert float(window.pos_labels["z"].text()) == 11
    assert "colour_scale" not in window._hover_highlight.warning_badges
