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
    assert controls.content_combo.currentText() == "Complete plot"
    assert not controls.item_tree.isVisible()
    assert not dialog.selectBox.isVisible()
    assert dialog.ui.copyBtn.isHidden()
    np.testing.assert_allclose(
        [controls.fields[name].value() for name in ("width", "height")],
        [int(exporter.getTargetRect().width()), int(exporter.getTargetRect().height())],
    )
    controls.fields["width"].setValue(640)
    ratio = exporter.getSourceRect().height() / exporter.getSourceRect().width()
    assert controls.fields["height"].value() == int(640 * ratio)
    controls.fields["background"].setColor(QtGui.QColor(12, 24, 36, 0))
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


def test_partial_area_is_explicit_and_csv_uses_plot_data(real_plot):
    plot, _database, errors, _dimensions = real_plot
    plot.open_export_dialog()
    dialog = plot.widget.scene().exportDialog
    controls = dialog.controls
    controls.choose_part.click()
    iterator = qtw.QTreeWidgetItemIterator(controls.item_tree)
    while iterator.value().gitem is not plot.plot:
        iterator += 1
    controls.item_tree.setCurrentItem(iterator.value())
    assert dialog.currentExporter.item is plot.plot
    assert controls.content_combo.currentText() == "Selected part of plot"
    assert dialog.selectBox.isVisible()
    _format(dialog, CSVExporter)
    assert dialog.currentExporter.item is plot.plot
    assert not controls.content.isVisible()
    assert not controls.item_tree.isVisible()
    assert not dialog.selectBox.isVisible()
    assert "width" not in controls.fields
    assert controls.heading.text() == "Data"
    _format(dialog, SVGExporter)
    assert dialog.currentExporter.item is plot.plot
    assert controls.fields["scaling stroke"].isVisible()
    controls.fields["scaling stroke"].setChecked(True)
    assert dialog.currentExporter.params["scaling stroke"] is True
    _format(dialog, ImageExporter)
    controls.content_combo.setCurrentIndex(0)
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
