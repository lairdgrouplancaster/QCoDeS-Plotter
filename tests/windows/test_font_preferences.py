"""Saved font sizing updates widgets, plot labels, ticks, and colour scales."""

import pyqtgraph as pg
import pytest
from jsonschema import ValidationError
from PyQt6 import QtWidgets as qtw

from qplot.configuration.config import config
from qplot.configuration.fonts import FONT_SIZE_KEY, configured_stylesheet
from qplot.windows._plot_state import PlotStateOverlay
from qplot.windows._plotWin import plotWidget
from qplot.windows._preferences import PreferencesDialog
from qplot.windows.main import MainWindow
from tests._window_lifecycle import close_main_window


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "default_path", str(tmp_path))
    monkeypatch.setattr(config, "default_file", str(tmp_path / "config.json"))
    return config()


def test_font_preference_persists_and_restores_default(cfg, monkeypatch):
    dialog = PreferencesDialog(cfg)
    try:
        assert dialog.fontSizeSpin.value() == 10
        dialog.fontSizeSpin.setValue(16)
        assert dialog.apply_preferences()
        assert config().get(FONT_SIZE_KEY) == 16
        monkeypatch.setattr(
            qtw.QMessageBox, "question",
            lambda *args, **kwargs: qtw.QMessageBox.StandardButton.Yes,
        )
        assert dialog.restore_defaults()
        assert dialog.fontSizeSpin.value() == 10
        assert config().get(FONT_SIZE_KEY) == 16
        assert dialog.apply_preferences()
        assert config().get(FONT_SIZE_KEY) == 10
    finally:
        dialog.close()


@pytest.mark.parametrize("value", [5, 37, 12.5, "12", True])
def test_font_size_rejects_invalid_settings(cfg, value):
    with pytest.raises(ValidationError):
        cfg.update(FONT_SIZE_KEY, value)
    assert config().get(FONT_SIZE_KEY) == 10


class PlotFontWindow(qtw.QMainWindow):
    update_theme = plotWidget.update_theme

    def __init__(self, cfg):
        super().__init__()
        self.config = cfg
        self.widget = pg.GraphicsLayoutWidget()
        self.setCentralWidget(self.widget)
        self.plot_state_overlay = PlotStateOverlay(self.widget)
        self.plot = self.widget.addPlot()
        self.plot.setLabel("bottom", "Gate", units="V")
        self.plot.setLabel("left", "Current", units="A")
        self.bar = pg.ColorBarItem(values=(0, 1), label="Signal")
        self.bar.setLabel("right", "Signal")
        self.widget.addItem(self.bar)


@pytest.mark.parametrize("theme", ["light", "dark", "pyqt"])
def test_font_size_updates_open_widgets_and_all_plot_axes(cfg, theme, qapplication):
    cfg.update("user_preference.theme", theme)
    plot = PlotFontWindow(cfg)
    try:
        plot.show()
        plot.plot_state_overlay.show("Loading", "Reading plot data")
        for size in (10, 18, 8):
            cfg.update(FONT_SIZE_KEY, size)
            plot.update_theme(cfg)
            qapplication.processEvents()
            assert plot.widget.font().pointSize() == size
            assert plot.plot_state_overlay.title_label.font().pointSizeF() == size
            assert plot.plot_state_overlay.detail_label.font().pointSizeF() == pytest.approx(size * 0.8)
            for item in (plot.plot, plot.bar):
                for side in ("left", "bottom", "right", "top"):
                    axis = item.getAxis(side)
                    assert axis.style["tickFont"].pointSize() == size
                    assert axis.label.font().pointSize() == size
            assert plot.plot.getAxis("bottom").labelText == "Gate"
            assert plot.plot.getAxis("bottom").labelUnits == "V"
            assert plot.bar.axis.labelText == "Signal"
            # Subsequent label refreshes must keep the selected size.
            plot.plot.setLabel("bottom", "Field", units="T")
            assert plot.plot.getAxis("bottom").label.font().pointSize() == size
            plot.plot.setLabel("bottom", "Gate", units="V")
    finally:
        plot.close()
        plot.deleteLater()


def test_apply_preferences_updates_open_main_and_plot_windows(cfg, qapplication):
    main = MainWindow()
    main.startupDatabaseTimer.stop()
    plot = PlotFontWindow(main.config)
    main.windows.append(plot)
    dialog = PreferencesDialog(main.config, main)
    dialog.preferencesApplied.connect(main.apply_current_settings)
    try:
        dialog.fontSizeSpin.setValue(16)
        assert dialog.apply_preferences()
        qapplication.processEvents()
        assert main.font().pointSize() == 16
        assert main.RunList.font().pointSize() == 16
        assert dialog.fontSizeSpin.font().pointSize() == 16
        assert plot.widget.font().pointSize() == 16
        assert plot.plot.getAxis("bottom").style["tickFont"].pointSize() == 16
        assert config().get(FONT_SIZE_KEY) == 16
    finally:
        dialog.close()
        main.windows.remove(plot)
        plot.close()
        plot.deleteLater()
        close_main_window(main)


def test_default_stylesheet_unchanged_and_secondary_text_scales(cfg):
    assert configured_stylesheet(cfg) == cfg.theme.main
    cfg.update(FONT_SIZE_KEY, 20)
    stylesheet = configured_stylesheet(cfg)
    assert "font-size: 20pt;" in stylesheet
    assert "font-size: 18pt;" in stylesheet
