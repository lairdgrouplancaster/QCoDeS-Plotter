"""Cursor precision is validated, persisted, and used for every readout."""

from types import SimpleNamespace

import pyqtgraph as pg
import pytest
from jsonschema import ValidationError
from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from qplot.configuration.config import config
from qplot.windows._plotWin import plotWidget
from qplot.windows._preferences import CURSOR_SIGNIFICANT_FIGURES_KEY, PreferencesDialog
from qplot.windows.plot2d import plot2d
from tests.windows.test_plot_overlay_integration import loaded_plot as loaded_plot


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "default_path", str(tmp_path))
    monkeypatch.setattr(config, "default_file", str(tmp_path / "config.json"))
    return config()


def test_preference_persists_and_restores_default(cfg, monkeypatch):
    dialog = PreferencesDialog(cfg)
    try:
        assert dialog.cursorSignificantFiguresSpin.value() == 6
        dialog.cursorSignificantFiguresSpin.setValue(9)
        assert dialog.apply_preferences()
        assert config().get(CURSOR_SIGNIFICANT_FIGURES_KEY) == 9
        monkeypatch.setattr(qtw.QMessageBox, "question",
                            lambda *a, **k: qtw.QMessageBox.StandardButton.Yes)
        assert dialog.restore_defaults()
        assert dialog.cursorSignificantFiguresSpin.value() == 6
        assert dialog.apply_preferences()
        assert config().get(CURSOR_SIGNIFICANT_FIGURES_KEY) == 6
    finally:
        dialog.close()


@pytest.mark.parametrize("value", [0, 18, 6.5, "6", True])
def test_invalid_precision_is_rejected(cfg, value):
    with pytest.raises(ValidationError):
        cfg.update(CURSOR_SIGNIFICANT_FIGURES_KEY, value)
    assert config().get(CURSOR_SIGNIFICANT_FIGURES_KEY) == 6


@pytest.mark.parametrize("heatmap", [False, True])
@pytest.mark.parametrize("figures, expected", [(6, "1.23457"), (3, "1.23"), (1, "1")])
def test_free_and_heatmap_readouts(cfg, heatmap, figures, expected):
    cfg.update(CURSOR_SIGNIFICANT_FIGURES_KEY, figures)
    point = QtCore.QPointF(1.23456789, 1.23456789)
    host = SimpleNamespace(
        config=cfg,
        plot=SimpleNamespace(
            sceneBoundingRect=lambda: QtCore.QRectF(0, 0, 10, 10),
            vb=SimpleNamespace(mapSceneToView=lambda pos: pos),
        ),
        pos_labels={axis: qtw.QLabel() for axis in ("x", "y", "z") if heatmap or axis != "z"},
        view_to_data=lambda axis, value: value,
        _set_cursor_index_label=lambda text: None,
        _nearest_1d_array_index=lambda value: None,
        heatmap_sample_at=lambda x, y: (0, 0, x, y, x),
        formatNum=plotWidget.formatNum,
    )
    host.format_cursor_number = lambda value: plotWidget.format_cursor_number(host, value)
    plotWidget.mouseMoved(host, point)
    assert host.pos_labels["x"].text() == f"x = {expected}"
    assert host.pos_labels["y"].text() == f"y = {expected}"
    if heatmap:
        assert host.pos_labels["z"].text() == f"z = {expected}"


@pytest.mark.parametrize("snapped", [False, True])
def test_existing_plot_updates_when_precision_is_applied(loaded_plot, snapped):
    window = loaded_plot
    window.show()
    window.snap_to_trace_action.setChecked(snapped)
    window.line.setData(x=[1.23456789, 2.0], y=[1.23456789, 2.0])
    owner = window.line.getViewBox()
    owner.setRange(xRange=(1, 3), yRange=(1, 3), padding=0)
    qtw.QApplication.processEvents()
    point = owner.mapViewToScene(QtCore.QPointF(1.23456789, 1.23456789))
    window.mouseMoved(point)
    assert window.pos_labels["x"].text() == "x = 1.23457"
    window.config.update(CURSOR_SIGNIFICANT_FIGURES_KEY, 3)
    window.apply_current_settings()
    assert window.pos_labels["x"].text() == "x = 1.23"
    assert window.pos_labels["y"].text() == "y = 1.23"


@pytest.mark.parametrize("window_type", [plotWidget, plot2d])
def test_coordinate_fields_have_equal_stable_fixed_widths(cfg, window_type):
    window = window_type.__new__(window_type)
    qtw.QMainWindow.__init__(window)
    widget = pg.PlotWidget()
    window.setCentralWidget(widget)
    window.plot = widget.getPlotItem()
    window.config = cfg
    window._install_plot_control_view_range_preserver = lambda: None
    window._init_heatmap_downsample_warning_button = lambda: None
    try:
        window.initLabels()
        window.resize(1200, 400)
        window.show()
        for figures in (1, 6, 17):
            cfg.update(CURSOR_SIGNIFICANT_FIGURES_KEY, figures)
            window.apply_cursor_label_widths()
            labels = [window.pos_labels[axis] for axis in ("x", "y", "z")
                      if axis in window.pos_labels]
            widths = {label.minimumWidth() for label in labels}
            assert len(widths) == 1
            assert all(label.maximumWidth() == label.minimumWidth() for label in labels)
            index_label = window.pos_labels["index"]
            assert index_label.minimumWidth() == index_label.maximumWidth()
            positions = None
            for index, value in zip(
                ("[0,0]", "[9999999,9999999]", ""),
                (0.0, -1.23456789e-308, 1.7976931348623157e308),
                strict=True,
            ):
                window._set_cursor_index_label(index)
                assert index_label.fontMetrics().horizontalAdvance(index) < index_label.width()
                for axis, label in window.pos_labels.items():
                    if axis in ("x", "y", "z"):
                        label.setText(f"{axis} = {window.format_cursor_number(value)}")
                        assert label.fontMetrics().horizontalAdvance(label.text()) < label.width()
                qtw.QApplication.processEvents()
                current = [label.geometry().x() for label in labels]
                if positions is not None:
                    assert current == positions
                positions = current
    finally:
        window.close()
        window.deleteLater()
