"""Format-specific export controls drive real image and CSV publication."""

from types import SimpleNamespace

import numpy as np
import pytest
from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw
from pyqtgraph.exporters import CSVExporter, ImageExporter, SVGExporter

from tests.support.windows.plot_integration import wait_for
from tests.support.windows.svg_export_integration import real_plot as real_plot


def _format(dialog, kind):
    dialog.controls.output_tabs.setCurrentIndex(int(kind is CSVExporter))
    if kind is not CSVExporter:
        combo = dialog.controls.format_combo
        combo.setCurrentIndex(combo.findData(kind))
    qtw.QApplication.processEvents()
    assert type(dialog.currentExporter) is kind


@pytest.mark.parametrize("font_size", [9, 14])
def test_default_height_shows_all_settings_when_switching_formats(real_plot, font_size):
    plot, _database, errors, _dimensions = real_plot
    plot.open_export_dialog()
    dialog = plot.widget.scene().exportDialog
    dialog.setStyleSheet(f"QWidget {{ font-size: {font_size}pt; }}")
    for kind in (CSVExporter, SVGExporter, ImageExporter, CSVExporter):
        _format(dialog, kind)
        assert dialog.controls_scroll.verticalScrollBar().maximum() == 0, (
            kind.__name__, dialog.size(), dialog.controls.sizeHint(),
            dialog.controls_scroll.viewport().size(),
            dialog.controls.heightForWidth(dialog.controls_scroll.viewport().width()),
        )
        if kind is not CSVExporter:
            dialog.ui.copyBtn.click()
            qtw.QApplication.processEvents()
            assert dialog.controls_scroll.verticalScrollBar().maximum() == 0
    assert not errors


def test_figure_tab_saves_plot_sized_pdf(real_plot, tmp_path, monkeypatch):
    plot, _database, errors, _dimensions = real_plot
    plot.open_export_dialog()
    dialog = plot.widget.scene().exportDialog
    combo = dialog.controls.format_combo
    combo.setCurrentIndex(combo.findData(QtGui.QPdfWriter))
    assert dialog.controls.settings.findChild(qtw.QLabel, "exportAspectRatio") is None
    assert dialog.ui.exportBtn.isEnabled() and dialog.ui.copyBtn.isHidden()
    target = tmp_path / "figure.pdf"
    monkeypatch.setattr(qtw.QFileDialog, "getSaveFileName", lambda *_a, **_kw: (str(target), ""))
    dialog.ui.exportBtn.click()
    assert target.read_bytes().startswith(b"%PDF-")
    assert not errors


def test_figure_dimensions_show_fixed_source_ratio(real_plot):
    plot, _database, errors, _dimensions = real_plot
    plot.widget.setFixedSize(400, 300)
    qtw.QApplication.processEvents()
    plot.open_export_dialog()
    dialog = plot.widget.scene().exportDialog
    for kind in (ImageExporter, SVGExporter):
        _format(dialog, kind)
        label = dialog.controls.settings.findChild(qtw.QLabel, "exportAspectRatio")
        assert label.isVisible() and label.text() == "Fixed aspect ratio · 4:3"
        width, height = (dialog.controls.fields[name] for name in ("width", "height"))
        width.setValue(401)
        assert height.value() == (300 if kind is ImageExporter else 300.75)
        height.setValue(303)
        assert width.value() == 404
        assert label.text() == "Fixed aspect ratio · 4:3"
    plot.widget.setFixedSize(300, 400)
    qtw.QApplication.processEvents()
    assert label.text() == "Fixed aspect ratio · 3:4"
    assert width.value() == 404
    assert height.value() == pytest.approx(404 * 4 / 3, abs=0.005)
    assert not errors


def test_default_height_keeps_actions_accessible_on_small_screens(real_plot, monkeypatch):
    plot, _database, errors, _dimensions = real_plot
    plot.open_export_dialog()
    dialog = plot.widget.scene().exportDialog
    dialog.setStyleSheet("QWidget { font-size: 14pt; }")
    _format(dialog, CSVExporter)
    bounds = QtCore.QRect(40, 60, 520, 360)
    monkeypatch.setattr(dialog, "screen", lambda: SimpleNamespace(availableGeometry=lambda: bounds))
    dialog.fit_export_controls()
    qtw.QApplication.processEvents()
    assert bounds.contains(dialog.frameGeometry())
    assert dialog.controls_scroll.verticalScrollBar().maximum() > 0
    for button in (dialog.ui.exportBtn, dialog.ui.closeBtn):
        assert dialog.rect().contains(QtCore.QRect(button.mapTo(dialog, QtCore.QPoint()), button.size()))
    assert not errors


def test_size_limits_and_choices_survive_format_and_tab_changes(real_plot):
    plot, _database, errors, _dimensions = real_plot
    plot.widget.setFixedSize(400, 300)
    qtw.QApplication.processEvents()
    plot.open_export_dialog()
    dialog = plot.widget.scene().exportDialog
    controls = dialog.controls
    for kind in (ImageExporter, SVGExporter):
        _format(dialog, kind)
        controls.fields["width"].setValue(1)
        for key in ("width", "height"):
            assert controls.fields[key].value() == dialog.currentExporter.params[key] >= 1
        controls.fields["height"].setValue(20_000)
        for key, value in (("width", 20_000), ("height", 15_000)):
            assert controls.fields[key].value() == dialog.currentExporter.params[key] == value
        controls.fields["width"].setValue(804 if kind is ImageExporter else 408)
    _format(dialog, ImageExporter)
    controls.fields["antialias"].setChecked(False)
    figure_save_position = dialog.ui.exportBtn.mapToGlobal(QtCore.QPoint())
    _format(dialog, CSVExporter)
    assert dialog.ui.exportBtn.mapToGlobal(QtCore.QPoint()) == figure_save_position
    controls.fields["separator"].setCurrentIndex(controls.fields["separator"].findData("tab"))
    _format(dialog, SVGExporter)
    assert controls.fields["width"].value() == 408
    _format(dialog, ImageExporter)
    assert controls.fields["width"].value() == 804 and not controls.fields["antialias"].isChecked()
    _format(dialog, CSVExporter)
    assert dialog.currentExporter.params["separator"] == "tab"
    dialog.close()
    plot.open_export_dialog()
    assert dialog.currentExporter.params["separator"] == "comma"
    assert not errors


def test_image_fields_export_full_scene_at_chosen_size(real_plot, tmp_path, monkeypatch):
    plot, _database, errors, _dimensions = real_plot
    plot.open_export_dialog()
    dialog = plot.widget.scene().exportDialog
    controls = dialog.controls
    exporter = dialog.currentExporter
    assert controls.format_combo.currentText() == "PNG image"
    assert exporter.item is plot.widget.scene()
    assert dialog.ui.itemTree.isHidden()
    assert controls.fields["invertValue"].isVisible()
    controls.fields["invertValue"].setChecked(True)
    assert exporter.params["invertValue"] is True
    assert not dialog.selectBox.isVisible()
    assert dialog.ui.copyBtn.isVisible()
    assert dialog.ui.copyBtn.text() == "Copy image"
    np.testing.assert_allclose(
        [controls.fields[name].value() for name in ("width", "height")],
        [int(exporter.getTargetRect().width()), int(exporter.getTargetRect().height())],
    )
    controls.fields["width"].setValue(640)
    ratio = exporter.getSourceRect().height() / exporter.getSourceRect().width()
    assert controls.fields["height"].value() == int(640 * ratio)
    background = controls.fields["background"]
    background.setCurrentIndex(background.findData("transparent"))
    background.activated.emit(background.currentIndex())
    controls.fields["antialias"].setChecked(False)
    assert exporter.params["antialias"] is False
    target = tmp_path / "complete.png"
    monkeypatch.setattr(qtw.QFileDialog, "getSaveFileName", lambda *_args, **_kwargs: (str(target), ""))
    dialog.ui.exportBtn.click()
    image = QtGui.QImage(str(target))
    assert not image.isNull()
    assert image.width() == 640
    assert image.height() == controls.fields["height"].value()
    assert image.hasAlphaChannel()
    assert dialog.isVisible()
    assert dialog.feedback.text() == "Saved."
    dialog.ui.copyBtn.click()
    clipboard = qtw.QApplication.clipboard().image()
    assert clipboard.size() == image.size()
    assert clipboard.pixelColor(0, 0).alpha() == image.pixelColor(0, 0).alpha()
    assert dialog.feedback.text() == "Copied to clipboard."
    assert not errors


@pytest.mark.parametrize("suffix", [".tif", ".jpg"])
def test_raster_export_only_writes_png(real_plot, tmp_path, monkeypatch, suffix):
    plot, _database, errors, _dimensions = real_plot
    plot.open_export_dialog()
    dialog = plot.widget.scene().exportDialog
    requested = tmp_path / f"shared{suffix}"
    requested.write_bytes(b"existing file must remain unchanged")
    choices = []
    def destination(*args, **kwargs):
        choices.append(args)
        return str(requested), "PNG image (*.png)"
    monkeypatch.setattr(qtw.QFileDialog, "getSaveFileName", destination)
    dialog.ui.exportBtn.click()
    assert choices[0][3] == "PNG image (*.png)"
    assert choices[0][2].endswith(".png")
    assert requested.read_bytes() == b"existing file must remain unchanged"
    output = requested.with_name(requested.name + ".png")
    assert output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert not QtGui.QImage(str(output)).isNull()
    assert not errors


def test_background_presets_and_custom_picker_cancel(real_plot):
    plot, _database, errors, _dimensions = real_plot
    plot.open_export_dialog()
    dialog = plot.widget.scene().exportDialog
    for kind in (ImageExporter, SVGExporter):
        _format(dialog, kind)
        background = dialog.controls.fields["background"]
        for name, expected in (("white", QtGui.QColor("white")),
                               ("transparent", QtGui.QColor("transparent"))):
            index = background.findData(name)
            background.setCurrentIndex(index)
            background.activated.emit(index)
            assert dialog.currentExporter.params["background"] == expected
        background.activated.emit(background.findData("choose"))
        picker = background.colour_dialog
        assert picker.isVisible()
        picker.setCurrentColor(QtGui.QColor(12, 34, 56, 128))
        picker.reject()
        assert dialog.currentExporter.params["background"].alpha() == 0
        assert background.accessibleDescription() == "Transparent"
        background.activated.emit(background.findData("choose"))
        chosen = QtGui.QColor(12, 34, 56, 128)
        picker.setCurrentColor(chosen)
        picker.accept()
        assert dialog.currentExporter.params["background"] == chosen
        assert background.accessibleDescription() == chosen.name(QtGui.QColor.NameFormat.HexArgb)
        assert not background.itemIcon(background.currentIndex()).isNull()
        background.activated.emit(background.findData("choose"))
        picker.setCurrentColor(QtGui.QColor("red"))
        picker.reject()
        assert dialog.currentExporter.params["background"] == chosen
    assert not errors


def test_visual_exports_always_use_complete_scene_and_csv_uses_plot_data(real_plot):
    plot, _database, errors, _dimensions = real_plot
    plot.open_export_dialog()
    dialog = plot.widget.scene().exportDialog
    controls = dialog.controls
    assert dialog.currentExporter.item is plot.widget.scene()
    assert dialog.ui.itemTree.topLevelItem(0).childCount() == 0
    _format(dialog, CSVExporter)
    assert dialog.currentExporter.item is plot.plot
    assert controls.output_tabs.tabText(1).replace("&", "") == "Data"
    assert controls.settings.findChild(qtw.QLabel, "exportAspectRatio") is None
    assert dialog.ui.copyBtn.isHidden()
    assert dialog.ui.itemTree.isHidden()
    assert not dialog.selectBox.isVisible()
    assert "width" not in controls.fields
    assert controls.heading.text() == "Data"
    _format(dialog, SVGExporter)
    assert dialog.currentExporter.item is plot.widget.scene()
    assert dialog.ui.copyBtn.isVisible()
    assert dialog.ui.copyBtn.text() == "Copy SVG"
    assert controls.fields["scaling stroke"].isVisible()
    controls.fields["scaling stroke"].setChecked(True)
    assert dialog.currentExporter.params["scaling stroke"] is True
    _format(dialog, ImageExporter)
    assert dialog.currentExporter.item is plot.widget.scene()
    dialog.close()
    plot.open_export_dialog()
    assert dialog.currentExporter.item is plot.widget.scene()
    assert not errors


def test_csv_separator_suggests_matching_file_type(real_plot, tmp_path, monkeypatch):
    plot, _database, errors, dimensions = real_plot
    plot.open_export_dialog()
    dialog = plot.widget.scene().exportDialog
    _format(dialog, CSVExporter)
    controls = dialog.controls
    assert "columnMode" not in controls.fields
    separator = controls.fields["separator"]
    separator.setCurrentIndex(separator.findData("tab"))
    assert dialog.currentExporter.params["separator"] == "tab"
    target = tmp_path / "samples.tsv"
    choices = []
    def destination(*args, **kwargs):
        choices.append((args, kwargs))
        return str(target), "TSV files (*.tsv)"
    monkeypatch.setattr(qtw.QFileDialog, "getSaveFileName", destination)
    dialog.ui.exportBtn.click()
    wait_for(lambda: all(task.done for task in plot._export_tasks))
    assert "\t" in target.read_text(encoding="utf-8").splitlines()[0]
    args, _kwargs = choices[0]
    assert args[2].endswith(".tsv")
    assert args[3].startswith("TSV files")
    assert dialog.isVisible()
    assert dialog.ui.copyBtn.isHidden()
    assert not errors
