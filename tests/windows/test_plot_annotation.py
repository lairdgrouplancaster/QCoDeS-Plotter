"""Plot provenance formatting, live appearance updates, and scene exports."""

import xml.etree.ElementTree as ET

from PyQt6 import QtCore, QtGui, QtWidgets

from qplot.configuration.config import config
from qplot.windows._plot_annotation import PlotSourceLabel, format_plot_sources
from qplot.windows._preferences import PreferencesDialog
from tests.windows.test_mixed_plot_drops import _added, _drop
from tests.windows.test_mixed_plot_drops import mixed_plot as mixed_plot
from tests.windows.test_svg_export_integration import real_plot as real_plot


def test_screenshot_format():
    sources = [("/data/Databasename.db", 9, "Signal")]
    sources += [("/data/Databasename.db", run, "Current") for run in (12, 10, 11)]
    sources += [("/data/Databasename.db", run, "Voltage") for run in (12, 8, 6, 11, 7, 8)]
    assert format_plot_sources(sources) == (
        "Databasename: 9 (Signal); 10-12 (Current); 6-8, 11, 12 (Voltage)"
    )


def test_separate_databases_with_same_name_and_run_ids():
    assert format_plot_sources([
        ("/a/test.db", 1, "Signal"), ("/b/test.db", 1, "Signal"),
    ]) == "/a/test.db: 1 (Signal)\n/b/test.db: 1 (Signal)"


def test_annotation_preserves_literal_text_and_wraps(qapplication):
    label = PlotSourceLabel()
    text = "A&B <sample>: 9 (Signal); 10-12 (Current); 6-8, 11, 12 (Voltage)"
    label.resize(600, 30)
    label.set_source_text(
        text, QtGui.QFont("Arial", 12), QtGui.QColor("black"),
        rich_text=format_plot_sources([
            ("A&B <sample>.db", 9, "Signal"),
            *[("A&B <sample>.db", run, "Current") for run in (10, 11, 12)],
            *[("A&B <sample>.db", run, "Voltage") for run in (6, 7, 8, 11, 12)],
        ], rich_text=True),
    )
    wide_height = label.minimumHeight()
    label.resize(170, wide_height)
    assert label.minimumHeight() > wide_height
    assert label.item.toPlainText() == text
    assert label.item.boundingRect().width() <= 170
    label.resize(600, label.minimumHeight())
    assert label.minimumHeight() == wide_height


def test_annotation_in_real_plot_and_svg(real_plot):
    plot, _database, errors, _dimensions = real_plot
    expected = f"measurement: {plot.ds.run_id} ({plot.param.label})"
    assert plot.config.get("user_preference.show_source_annotation") is True
    dialog = PreferencesDialog(plot.config, plot)
    dialog.preferencesApplied.connect(plot.apply_current_settings)
    plot_top = {}
    for visible in (True, False, True):
        dialog.showSourceAnnotationCheck.setChecked(visible)
        assert dialog.apply_preferences()
        annotation = plot.source_annotation
        assert annotation.text == expected
        assert annotation.item.toPlainText() == expected
        assert annotation.isVisible() is visible
        assert config().get("user_preference.show_source_annotation") is visible
        cursor = QtGui.QTextCursor(annotation.item.document())
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.NextCharacter,
                            QtGui.QTextCursor.MoveMode.KeepAnchor)
        assert cursor.charFormat().fontWeight() >= QtGui.QFont.Weight.Bold
        QtWidgets.QApplication.processEvents()
        plot_top[visible] = plot.vb.sceneBoundingRect().top()
        if visible:
            annotation_rect = annotation.sceneBoundingRect()
            assert abs(annotation_rect.left() - plot.vb.sceneBoundingRect().left()) < 2
            assert annotation_rect.bottom() <= plot.vb.sceneBoundingRect().top()
        else:
            assert plot.plot.layout.itemAt(0, 1) is None
        root = ET.fromstring(plot._plot_svg_bytes())
        # Bold and normal text are separate SVG elements with formatting whitespace.
        exported_text = " ".join("".join(root.itertext()).split())
        assert (expected in exported_text) is visible
        assert not plot._plot_image_at_size(QtCore.QSize(640, 480)).isNull()
    assert plot_top[False] < plot_top[True]
    dialog.close()
    assert not errors


def test_mixed_annotation_tracks_add_hide_opacity_remove(mixed_plot, monkeypatch):
    monkeypatch.setattr(
        QtWidgets.QMessageBox, "exec",
        lambda box: QtWidgets.QMessageBox.StandardButton.Ok,
    )
    _window, target, runs = mixed_plot
    primary = "1 (y)" if target.operation_kind == "plot1d" else "2 (z)"
    assert target.source_annotation.text == f"mixed: {primary}"
    _drop(target, runs["other_line"])
    line = _added(target, runs["other_line"])
    _drop(target, runs["other_heatmap"])
    heatmap = _added(target, runs["other_heatmap"])
    assert target.source_annotation.text == f"mixed: {primary}; 3 (w); 4 (q)"
    style = target._trace_styles[line.from_win._trace_key]
    style.visible = False
    target._apply_trace_style(line.from_win._trace_key, line)
    assert target.source_annotation.text == f"mixed: {primary}; 4 (q)"
    target._set_layer_visibility(heatmap, False)
    assert target.source_annotation.text == f"mixed: {primary}"
    target._set_layer_visibility(heatmap, True)
    target._set_layer_opacity(heatmap, 0)
    assert target.source_annotation.text == f"mixed: {primary}"
    target._set_layer_opacity(heatmap, .5)
    assert target.source_annotation.text == f"mixed: {primary}; 4 (q)"
    target.remove_heatmap(trace_key=heatmap.from_win._trace_key)
    assert target.source_annotation.text == f"mixed: {primary}"
