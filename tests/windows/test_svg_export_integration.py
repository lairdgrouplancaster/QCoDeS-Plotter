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
from qcodes.dataset.sqlite.database import connect

from qplot.testdata import enable_generation_provenance_for_writer
from qplot.windows import main as main_window
from qplot.windows._preferences import (
    COPY_PLOT_IMAGE_RESOLUTION_KEY,
    COPY_PLOT_IMAGE_RESOLUTION_SVG,
)
from tests._window_lifecycle import close_main_window
from tests.windows.test_plot_integration import (
    build_synthetic_database,
    configure_temp_qplot,
    database_artifact_state,
    prepare_generated_database_for_live_writes,
    wait_for,
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


@pytest.fixture(params=[1, 2], ids=["line", "heatmap"])
def real_plot(tmp_path, monkeypatch, request):
    configure_temp_qplot(monkeypatch, tmp_path)
    database = tmp_path / "measurement.db"
    prepare_generated_database_for_live_writes(database)
    run_ids = build_synthetic_database(database)
    # Keep a synthetic writer open so the real WAL/SHM remain present during
    # viewing and exports. Capture protected files after fixture generation.
    writer = connect(database)
    enable_generation_provenance_for_writer(writer)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("UPDATE runs SET name = name || '_export'")
    writer.commit()
    before = database_artifact_state(database, hash_shm=False)
    before.pop("-shm")  # SQLite's pinned VFS may update only this sidecar
    guid = writer.execute(
        "SELECT guid FROM runs WHERE run_id = ?", (run_ids[request.param - 1],),
    ).fetchone()[0]
    main = main_window.MainWindow()
    try:
        main.startupDatabaseTimer.stop()
        main.monitor.stop()
        main.config.config["user_preference"]["confirm_close"] = False
        main.config.config["user_preference"]["confirm_close_all"] = False
        main.close_database(status=False)
        assert main.load_file(str(database))
        wait_for(lambda: not main._database_load_active)
        prior_plot_count = len(main.windows)
        main.openPlot(guid=guid, show=True)
        wait_for(lambda prior_plot_count=prior_plot_count: len(main.windows) > prior_plot_count)
        plot = main.windows[-1]
        wait_for(lambda: hasattr(plot, "axis_data") and not plot.worker.running)
        plot.monitor.stop()
        errors = []
        monkeypatch.setattr(plot, "show_error", lambda *args: errors.append(args))
        if request.param == 1:
            plot.line.setPen(pg.mkPen("#e01020", width=3))
            plot.line.setSymbol("o")  # Qt 6.11 emits standalone Z in closed paths
            plot.line.setSymbolBrush("#e01020")
            plot._ensure_trace_axis_viewboxes(top=True, right=True)
            plot.plot.showAxis("top")
            plot.plot.showAxis("right")
            plot.plot.setLabel("top", "Secondary gate", units="V")
            plot.plot.setLabel("right", "Secondary current", units="nA")
            secondary = pg.PlotDataItem([-.9, 0, .9], [100, 300, 100],
                                        pen=pg.mkPen("#007090", width=3))
            plot.top_right_vb.addItem(secondary)
            plot.top_right_vb.setRange(xRange=(-1.5, 1.5), yRange=(0, 400), padding=0)
        # A closed curved overlay exercises Qt paths under an anisotropic
        # data-to-view transform. Its out-of-range part must remain clipped.
        path = QtGui.QPainterPath()
        path.moveTo(-100, 0)
        path.cubicTo(-.5, .3, .5, .3, 100, 0)
        path.lineTo(100, -.2)
        path.closeSubpath()
        overlay = qtw.QGraphicsPathItem(path)
        overlay.setPen(pg.mkPen("#8030a0", width=2))
        overlay.setBrush(pg.mkBrush(128, 48, 160, 60))
        plot.vb.addItem(overlay, ignoreBounds=True)
        qtw.QApplication.processEvents()
        yield plot, database, errors, request.param
    finally:
        close_main_window(main)
        try:
            after = database_artifact_state(database, hash_shm=False)
            after.pop("-shm")
            assert after == before
        finally:
            writer.close()


def select_exporter(plot, exporter_type):
    plot.exportPlotAction.trigger()  # File > Export Plot
    qtw.QApplication.processEvents()
    dialog = plot.widget.scene().exportDialog
    assert dialog.isVisible()
    assert dialog.ui.itemTree.currentItem().gitem is plot.plot
    for row in range(dialog.ui.formatList.count()):
        if dialog.ui.formatList.item(row).expClass is exporter_type:
            dialog.ui.formatList.setCurrentRow(row)
            break
    assert type(dialog.currentExporter) is exporter_type
    return dialog


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
    # The dialog Copy button must use the same renderer and options as Export.
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
    assert errors and "render failed" in errors.pop()[2]
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
