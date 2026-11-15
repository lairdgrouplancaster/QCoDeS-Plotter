"""Real QCoDeS windows and export actions on the installed Qt/PyQtGraph stack."""

import os
import re
import xml.etree.ElementTree as ET

import numpy as np
import pyqtgraph as pg
import pytest
from PyQt6 import QtCore, QtGui, QtSvg
from PyQt6 import QtWidgets as qtw
from pyqtgraph.exporters import ImageExporter, SVGExporter

from qplot.windows._preferences import (
    COPY_PLOT_IMAGE_RESOLUTION_KEY,
    COPY_PLOT_IMAGE_RESOLUTION_SVG,
)
from tests.support.windows.svg_export_integration import (
    real_plot as real_plot,
)
from tests.support.windows.svg_export_integration import (
    select_exporter as select_exporter,
)

SVG = "{http://www.w3.org/2000/svg}"


def test_copy_plot_image_action_preserves_zoom_clipping(real_plot):
    plot, _database, errors, dimensions = real_plot
    plot.config.update(COPY_PLOT_IMAGE_RESOLUTION_KEY, COPY_PLOT_IMAGE_RESOLUTION_SVG)
    plot.vb.setRange(xRange=(-.5, .5), yRange=(-.5, .5), padding=0)
    qtw.QApplication.processEvents()
    plot.copyPlotImageAction.trigger()
    mime = qtw.QApplication.clipboard().mimeData()
    data = bytes(mime.data("image/svg+xml"))
    root = ET.fromstring(data)
    assert root.attrib["version"] == "1.1"
    assert [float(v) for v in root.attrib["viewBox"].split()] == [
        0, 0, plot.widget.width(), plot.widget.height(),
    ]
    assert mime.text() == data.decode("utf-8")
    clips = {node.attrib["id"]: node for node in root.findall(".//" + SVG + "clipPath")}
    assert len(clips) >= 2
    # Inspect the actual SVG geometry. QSvgRenderer itself ignores SVG 1.1
    # clip paths, so its raster output cannot verify this behavior.
    painted = root.find(SVG + "g")
    clipped_groups = [node for node in painted.iter()
                      if node.attrib.get("clip-path", "").startswith("url(#")]
    assert clipped_groups
    for group in clipped_groups:
        clip_id = group.attrib["clip-path"][5:-1]
        assert clip_id in clips
        assert list(clips[clip_id])
    primitive = "image" if dimensions == 2 else "polyline"
    source = plot._plot_svg_source_rect(plot.widget)
    bounds = plot.vb.sceneBoundingRect().translated(-source.left(), -source.top())
    plot_clips = []
    for group in clipped_groups:
        if not group.findall(".//" + SVG + primitive):
            continue
        clip = clips[group.attrib["clip-path"][5:-1]]
        path = clip.find(SVG + "path")
        if path is None:
            continue
        points = np.array([float(v) for v in re.findall(
            r"[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?", path.attrib["d"],
        )]).reshape(-1, 2)
        rectangle = [*points.min(axis=0), *points.max(axis=0)]
        if np.allclose(rectangle, [bounds.left(), bounds.top(), bounds.right(), bounds.bottom()], atol=1):
            plot_clips.append(clip)
    assert plot_clips, "The zoomed trace/image must be clipped to the plot's visible bounds"
    assert not errors


def destination_dialogs(monkeypatch, target, *, replace=True):
    monkeypatch.setattr(qtw.QFileDialog, "getSaveFileName",
                        lambda *_args, **_kwargs: (str(target), ""))
    monkeypatch.setattr(qtw.QMessageBox, "question", lambda *_args, **_kwargs: (
        qtw.QMessageBox.StandardButton.Yes if replace else qtw.QMessageBox.StandardButton.No
    ))


def rendered_svg(data):
    root = ET.fromstring(data)
    assert root.tag == SVG + "svg"
    assert root.attrib["version"] == "1.1"
    viewbox = [float(v) for v in root.attrib["viewBox"].split()]
    assert viewbox[:2] == [0, 0]
    assert len(root.findall(".//" + SVG + "clipPath")) >= 2
    graphics = root.findall(".//" + SVG + "path") + root.findall(".//" + SVG + "polyline")
    assert len(graphics) > 10
    assert root.findall(".//" + SVG + "text")
    renderer = QtSvg.QSvgRenderer(QtCore.QByteArray(data))
    assert renderer.isValid()
    image = QtGui.QImage(int(viewbox[2]), int(viewbox[3]), QtGui.QImage.Format.Format_RGBA8888)
    image.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(image)
    renderer.render(painter)
    painter.end()
    pixels = pg.functions.ndarray_from_qimage(image).copy()
    assert np.count_nonzero(np.ptp(pixels[:, :, :3], axis=2) > 40) > 100
    return root, pixels


@pytest.mark.parametrize("scaling", [False, True], ids=["cosmetic", "scaling"])
def test_svg_file_and_dialog_clipboard_preserve_geometry(real_plot, tmp_path, monkeypatch, scaling):
    plot, _database, errors, dimensions = real_plot
    dialog = select_exporter(plot, SVGExporter)
    exporter = dialog.currentExporter
    exporter.params["width"] = 720.5
    exporter.params["background"] = QtGui.QColor("#f0e8d8")
    exporter.params["scaling stroke"] = scaling
    state = exporter.params.saveState()
    target = tmp_path / "plot.svg"
    destination_dialogs(monkeypatch, target)
    dialog.ui.exportBtn.click()
    assert not errors
    data = target.read_bytes()
    root, pixels = rendered_svg(data)
    assert float(root.attrib["viewBox"].split()[2]) == 720.5
    primitives = [node for node in root.iter()
                  if node.tag in {SVG + tag for tag in ("path", "polyline", "ellipse", "rect")}]
    # Clip definitions retain Qt's attributes; the option applies to strokes
    # in the painted document, and must not change their width at native size.
    clip_nodes = {node for clip in root.findall(".//" + SVG + "defs") for node in clip.iter()}
    cosmetic = [node for node in primitives if node not in clip_nodes
                and node.attrib.get("vector-effect") == "non-scaling-stroke"]
    assert bool(cosmetic) is (not scaling)
    if dimensions == 2:
        assert len(root.findall(".//" + SVG + "image")) >= 2  # heatmap and colorbar
        assert "Conductance" in data.decode()
    else:
        assert b"#e01020" in data and b"#007090" in data  # main and secondary axes
        assert b"Secondary gate" in data and b"Secondary current" in data
    # Compare the plot interior to an independent real PNG render. This catches
    # missing/clipped heatmaps, misplaced secondary traces, curves and overlays.
    reference = ImageExporter(exporter.item)
    reference.params["width"] = pixels.shape[1]
    reference.params["height"] = pixels.shape[0]
    reference.params["background"] = exporter.params["background"]
    reference_image = reference.export(toBytes=True).convertToFormat(
        QtGui.QImage.Format.Format_RGBA8888,
    )
    reference_pixels = pg.functions.ndarray_from_qimage(reference_image)
    source = exporter.getSourceRect()
    bounds = plot.vb.sceneBoundingRect()
    x0, x1 = [int((v - source.left()) / source.width() * pixels.shape[1])
              for v in (bounds.left(), bounds.right())]
    y0, y1 = [int((v - source.top()) / source.height() * pixels.shape[0])
              for v in (bounds.top(), bounds.bottom())]
    actual = pixels[y0+3:y1-3, x0+3:x1-3, :3].astype(float)
    expected = reference_pixels[y0+3:y1-3, x0+3:x1-3, :3].astype(float)
    assert np.abs(actual - expected).mean() < 12
    assert np.mean(np.max(np.abs(actual - expected), axis=2) > 40) < .08
    # The dialog copies the exact renderer and options used for the file.
    assert dialog.ui.copyBtn.isVisible()
    assert dialog.ui.copyBtn.text() == "Copy SVG"
    dialog.ui.copyBtn.click()
    mime = qtw.QApplication.clipboard().mimeData()
    clipboard = bytes(mime.data("image/svg+xml"))
    assert clipboard == data
    assert exporter.params.saveState() == state
    assert not any(tmp_path.glob(".plot.svg.*"))


def test_svg_failure_cancel_and_source_protection(real_plot, tmp_path, monkeypatch):
    plot, database, errors, _dimensions = real_plot
    dialog = select_exporter(plot, SVGExporter)
    target = tmp_path / "existing.svg"
    sentinel = b"existing export must survive"
    target.write_bytes(sentinel)
    destination_dialogs(monkeypatch, target, replace=False)
    dialog.ui.exportBtn.click()
    assert target.read_bytes() == sentinel and not errors
    destination_dialogs(monkeypatch, target)
    render = plot.widget.scene().render

    def failed_render(*args, **kwargs):
        render(*args, **kwargs)  # real painter and actual plot content
        raise RuntimeError("render failed after producing SVG graphics")

    with monkeypatch.context() as patch:
        patch.setattr(plot.widget.scene(), "render", failed_render)
        dialog.ui.exportBtn.click()
    assert errors
    last_error = errors.pop()
    assert "render failed" in last_error[2]
    assert target.read_bytes() == sentinel
    assert not any(tmp_path.glob(".existing.svg.*"))
    # An input database disguised by an SVG suffix must be rejected before
    # opening a stage, including inode aliases of main/WAL/journal files.
    for suffix in ("", "-wal", "-shm", "-journal"):
        source = database.with_name(database.name + suffix)
        if not source.exists():
            continue
        alias = tmp_path / f"source{suffix}.svg"
        os.link(source, alias)
        destination_dialogs(monkeypatch, alias)
        dialog.ui.exportBtn.click()
        assert errors
        errors.clear()
        alias.unlink()
    # Recovery uses the same live dialog; export mode must have been restored.
    destination_dialogs(monkeypatch, target)
    dialog.ui.exportBtn.click()
    rendered_svg(target.read_bytes())
    assert not errors


def test_svg_destination_changed_during_real_render_is_preserved(real_plot, tmp_path, monkeypatch):
    plot, _database, errors, _dimensions = real_plot
    dialog = select_exporter(plot, SVGExporter)
    target = tmp_path / "changed.svg"
    target.write_bytes(b"original approved export")
    destination_dialogs(monkeypatch, target)
    render = plot.widget.scene().render
    concurrent = b"destination changed while painting"

    def changed_destination(*args, **kwargs):
        render(*args, **kwargs)
        target.write_bytes(concurrent)

    with monkeypatch.context() as patch:
        patch.setattr(plot.widget.scene(), "render", changed_destination)
        dialog.ui.exportBtn.click()
    assert errors
    assert target.read_bytes() == concurrent
    assert not any(tmp_path.glob(".changed.svg.*"))


def test_png_pdf_and_existing_clipboard_svg_regressions(real_plot, tmp_path, monkeypatch):
    plot, _database, errors, _dimensions = real_plot
    dialog = select_exporter(plot, ImageExporter)
    target = tmp_path / "plot.png"
    destination_dialogs(monkeypatch, target)
    dialog.ui.exportBtn.click()
    image = QtGui.QImage(str(target))
    assert not image.isNull()
    assert image.width() > 100 and image.height() > 100
    assert dialog.ui.copyBtn.isVisible()
    dialog.ui.copyBtn.click()
    assert not qtw.QApplication.clipboard().image().isNull()
    target = tmp_path / "plot.pdf"
    destination_dialogs(monkeypatch, target)
    plot.savePlotPdfAction.trigger()
    assert target.read_bytes().startswith(b"%PDF-")
    assert target.stat().st_size > 3000
    assert plot.copy_plot_image_as_svg()
    clipboard = bytes(qtw.QApplication.clipboard().mimeData().data("image/svg+xml"))
    assert QtSvg.QSvgRenderer(QtCore.QByteArray(clipboard)).isValid()
    assert b"<path" in clipboard or b"<polyline" in clipboard
    assert not errors
