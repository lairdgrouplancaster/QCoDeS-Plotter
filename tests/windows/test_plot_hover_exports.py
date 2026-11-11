"""Transient dataset emphasis never becomes part of exported plot graphics."""

from types import SimpleNamespace

import numpy as np
import pyqtgraph as pg
import pytest
from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw
from pyqtgraph.exporters import ImageExporter, SVGExporter

from qplot.windows import _plot_export as export_module
from qplot.windows._plot_export import PlotExportMixin
from qplot.windows._plot_hover import PlotHoverHighlight


class _ExportHost(PlotExportMixin):
    def show_status(self, *_args):
        pass

    def _default_plot_pdf_filename(self):
        return "plot.pdf"


@pytest.fixture(params=[
    pytest.param(("trace", ()), id="trace"),
    pytest.param(("heatmap", ()), id="heatmap"),
    pytest.param(("trace", ("left",)), id="trace-axis-warning"),
    pytest.param(("heatmap", ("left",)), id="heatmap-axis-warning"),
    pytest.param(("heatmap", ("colour_scale",)), id="heatmap-colour-scale-warning"),
])
def hover_export_plot(request):
    kind, warning_axes = request.param
    host = _ExportHost()
    host.widget = pg.PlotWidget(background="w")
    host.widget.resize(480, 360)
    host.plot = host.widget.getPlotItem()
    host.plot.setLabel("bottom", "Gate voltage", units="V")
    host.plot.setLabel("left", "Current", units="A")
    if kind == "trace":
        item = host.plot.plot([0, 1, 2], [0, 1, .5], pen="r", symbol="o")
    else:
        item = pg.ImageItem(np.arange(12).reshape(3, 4))
        host.plot.addItem(item)
        host.bar = host.plot.addColorBar(
            item, colorMap=pg.colormap.get("viridis"), values=(0, 11), label="Signal",
        )
    host.plot.setRange(xRange=(-.5, 4.5), yRange=(-.5, 3.5), padding=0)
    host.widget.show()
    qtw.QApplication.processEvents()
    host._hover_highlight = PlotHoverHighlight(host)
    activate = (
        host._hover_highlight.set_trace if kind == "trace"
        else host._hover_highlight.set_heatmap
    )
    options = {"warning_axes": warning_axes}
    if kind == "heatmap":
        options["colourbar"] = host.bar
    try:
        yield host, lambda: activate(item, ("bottom", "left"), **options)
    finally:
        host._hover_highlight.clear()
        host.widget.close()
        host.widget.deleteLater()


def _capture(host, route, monkeypatch, tmp_path):
    if route == "screen":
        return host._plot_image_pixmap().toImage()
    if route == "sized":
        return host._plot_image_at_size(QtCore.QSize(600, 450))
    if route == "svg":
        return host._plot_svg_bytes()
    if route == "print":
        image = QtGui.QImage(600, 450, QtGui.QImage.Format.Format_ARGB32)
        image.fill(QtCore.Qt.GlobalColor.white)
        assert host._paint_plot_to_device(image)
        return image

    exporter = SVGExporter(host.plot) if route.endswith("svg") else ImageExporter(host.plot)
    if route.startswith("clipboard"):
        assert host._copy_pyqtgraph_exporter(exporter)
        clipboard = qtw.QApplication.clipboard()
        if route.endswith("svg"):
            return bytes(clipboard.mimeData().data("image/svg+xml"))
        return clipboard.image()

    target = tmp_path / ("plot.svg" if route.endswith("svg") else "plot.png")
    monkeypatch.setattr(
        export_module, "choose_export_path_with_suffixes",
        lambda *_args, **_kwargs: SimpleNamespace(filename=str(target)),
    )

    def write_stage(_destination, writer):
        writer(str(target))
        return True

    monkeypatch.setattr(export_module, "write_export_atomically", write_stage)
    assert host._export_pyqtgraph_exporter(exporter)
    return target.read_bytes() if route.endswith("svg") else QtGui.QImage(str(target))


@pytest.mark.parametrize("route", [
    "screen", "sized", "svg", "print", "file_png", "file_svg",
    "clipboard_png", "clipboard_svg",
])
def test_exports_match_unhighlighted_plot(hover_export_plot, route, monkeypatch, tmp_path):
    host, activate = hover_export_plot
    expected = _capture(host, route, monkeypatch, tmp_path)
    plain_screen = host.widget.grab().toImage()
    activate()
    badges = dict(host._hover_highlight.warning_badges)
    assert set(badges) == set(host._hover_highlight.warning_axes)
    assert all(badge.isVisible() for badge in badges.values())
    assert host.widget.grab().toImage() != plain_screen
    assert _capture(host, route, monkeypatch, tmp_path) == expected
    assert host._hover_highlight.active_item is not None
    assert all(effect.isEnabled() for effect in host._hover_highlight.effects)
    assert host._hover_highlight.warning_badges == badges
    assert all(badge.isVisible() for badge in badges.values())
    assert host.widget.grab().toImage() != plain_screen


def test_failed_capture_restores_hover(hover_export_plot, monkeypatch):
    host, activate = hover_export_plot
    activate()
    badges = dict(host._hover_highlight.warning_badges)

    def fail_capture():
        assert all(not effect.isEnabled() for effect in host._hover_highlight.effects)
        assert all(not badge.isVisible() for badge in badges.values())
        outline = host._hover_highlight.outline
        assert outline is None or not outline.isVisible()
        raise RuntimeError("Capture failed")

    monkeypatch.setattr(host.widget, "grab", fail_capture)
    with pytest.raises(RuntimeError, match="Capture failed"):
        host._plot_image_pixmap()
    assert all(effect.isEnabled() for effect in host._hover_highlight.effects)
    assert host._hover_highlight.warning_badges == badges
    assert all(badge.isVisible() for badge in badges.values())
    outline = host._hover_highlight.outline
    assert outline is None or outline.isVisible()
