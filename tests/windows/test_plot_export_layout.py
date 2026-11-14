"""Format-specific export controls drive real image and CSV publication."""

import numpy as np
from PyQt6 import QtGui
from PyQt6 import QtWidgets as qtw
from pyqtgraph.exporters import CSVExporter, ImageExporter, SVGExporter

from tests.windows.test_plot_integration import wait_for
from tests.windows.test_svg_export_integration import real_plot as real_plot


def _format(dialog, kind):
    combo = dialog.controls.format_combo
    combo.setCurrentIndex(combo.findData(kind))
    qtw.QApplication.processEvents()
    assert type(dialog.currentExporter) is kind


def test_image_fields_export_full_scene_at_chosen_size(real_plot, tmp_path, monkeypatch):
    plot, _database, errors, _dimensions = real_plot
    plot.open_export_dialog()
    dialog = plot.widget.scene().exportDialog
    controls = dialog.controls
    exporter = dialog.currentExporter
    assert exporter.item is plot.widget.scene()
    assert dialog.ui.itemTree.isHidden()
    assert controls.fields["invertValue"].isVisible()
    controls.fields["invertValue"].setChecked(True)
    assert exporter.params["invertValue"] is True
    assert not dialog.selectBox.isVisible()
    assert dialog.ui.copyBtn.isHidden()
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
    assert dialog.ui.itemTree.isHidden()
    assert not dialog.selectBox.isVisible()
    assert "width" not in controls.fields
    assert controls.heading.text() == "Data"
    _format(dialog, SVGExporter)
    assert dialog.currentExporter.item is plot.widget.scene()
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
    assert ("columnMode" in controls.fields) == (dimensions == 1)
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
