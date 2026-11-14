"""Trace defaults are staged, validated, and used only for new traces."""

import sys
from copy import deepcopy

import pytest
from jsonschema import ValidationError
from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

from qplot.configuration.config import config
from qplot.configuration.trace import TRACE_COLOUR_KEY, TRACE_STYLE_KEY, TRACE_WIDTH_KEY
from qplot.windows._preferences import PreferencesDialog
from tests.windows.test_mixed_plot_drops import _added, _drop, _wait_for
from tests.windows.test_mixed_plot_drops import mixed_plot as _mixed_plot
from tests.windows.test_preferences_value_preservation import (
    precise_config as _precise_config,
)

mixed_plot = _mixed_plot
precise_config = _precise_config


@pytest.fixture
def preferences(precise_config, qapplication, monkeypatch):
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *error: errors.append(error))
    precise_config.update_many({
        TRACE_COLOUR_KEY: "#123456", TRACE_WIDTH_KEY: 3.5, TRACE_STYLE_KEY: "Dot",
    })
    dialog = PreferencesDialog(precise_config)
    yield dialog, precise_config
    dialog.close()
    dialog.deleteLater()
    qapplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    assert not errors


def test_trace_defaults_load_cancel_apply_and_restore(preferences, monkeypatch):
    dialog, cfg = preferences
    assert dialog.defaultTraceColourCombo.currentData() == "#123456"
    assert dialog.defaultTraceWidthSpin.value() == 3.5
    assert dialog.defaultTraceStyleCombo.currentData() == "Dot"
    assert "#123456" in dialog.defaultTraceColourCombo.accessibleDescription()
    assert "Dot" in dialog.defaultTraceStyleCombo.accessibleDescription()
    dialog.defaultTraceWidthSpin.setValue(5.5)
    dialog.reject()
    assert config().get(TRACE_WIDTH_KEY) == 3.5
    dialog.load_from_config()
    dialog.defaultTraceColourCombo.setCurrentIndex(dialog.defaultTraceColourCombo.findData("#ff7f0e"))
    dialog.defaultTraceWidthSpin.setValue(5.5)
    dialog.defaultTraceStyleCombo.setCurrentIndex(dialog.defaultTraceStyleCombo.findData("Dash Dot"))
    assert dialog.apply_preferences()
    saved = config()
    assert saved.get(TRACE_COLOUR_KEY) == "#ff7f0e"
    assert saved.get(TRACE_WIDTH_KEY) == 5.5
    assert saved.get(TRACE_STYLE_KEY) == "Dash Dot"
    monkeypatch.setattr(qtw.QMessageBox, "question", lambda *args: qtw.QMessageBox.StandardButton.Yes)
    assert dialog.restore_defaults()
    assert dialog.defaultTraceColourCombo.currentData() == "#1f77b4"
    assert dialog.defaultTraceWidthSpin.value() == 2.0
    assert dialog.defaultTraceStyleCombo.currentData() == "Solid"
    assert cfg.get(TRACE_WIDTH_KEY) == 5.5
    assert dialog.apply_preferences()
    assert config().get(TRACE_WIDTH_KEY) == 2.0


@pytest.mark.parametrize("accept", [False, True])
def test_custom_default_trace_colour_is_staged_and_cancellable(preferences, monkeypatch, accept):
    dialog, cfg = preferences
    opening_colours = []

    def choose(opening, *_args):
        opening_colours.append(opening.name())
        return QtGui.QColor("#abcdef") if accept else QtGui.QColor()

    monkeypatch.setattr(qtw.QColorDialog, "getColor", choose)
    combo = dialog.defaultTraceColourCombo
    combo.setCurrentIndex(combo.findData("custom"))
    assert opening_colours == ["#123456"]
    expected = "#abcdef" if accept else "#123456"
    assert combo.currentData() == expected
    assert dialog.preference_values()[TRACE_COLOUR_KEY] == expected
    assert cfg.get(TRACE_COLOUR_KEY) == "#123456"
    assert dialog.apply_preferences()
    assert config().get(TRACE_COLOUR_KEY) == expected


def test_untouched_trace_width_preserves_saved_precision(preferences):
    dialog, cfg = preferences
    cfg.update(TRACE_WIDTH_KEY, 2.25)
    dialog.load_from_config()
    assert dialog.defaultTraceWidthSpin.value() != 2.25
    assert dialog.apply_preferences()
    assert config().get(TRACE_WIDTH_KEY) == 2.25


@pytest.mark.parametrize("width", [360, 760])
@pytest.mark.parametrize("font_size", [9, 14])
def test_trace_default_row_keeps_controls_visible(preferences, qapplication, width, font_size):
    dialog, _cfg = preferences
    font = QtGui.QFont(dialog.font())
    font.setPointSize(font_size)
    dialog.setFont(font)
    dialog.show()
    dialog.resize(width, 450)
    dialog.tabs.setCurrentIndex(1)
    qapplication.processEvents()
    page = dialog.tabs.currentWidget()
    page.ensureWidgetVisible(dialog.defaultTraceRow)
    qapplication.processEvents()
    controls = (dialog.defaultTraceColourCombo, dialog.defaultTraceWidthSpin, dialog.defaultTraceStyleCombo)
    for control in controls:
        rect = QtCore.QRect(control.mapTo(dialog.defaultTraceRow, QtCore.QPoint()), control.size())
        assert dialog.defaultTraceRow.rect().contains(rect)
    if width == 760:
        assert max(control.geometry().center().y() for control in controls) - min(
            control.geometry().center().y() for control in controls
        ) <= 1
    assert controls[0].geometry().left() == 0
    assert controls[-1].geometry().right() == dialog.defaultTraceRow.width() - 1
    assert abs(controls[0].width() - controls[-1].width()) <= 1
    assert not page.horizontalScrollBar().isVisible()


@pytest.mark.parametrize("key, value", [
    (TRACE_COLOUR_KEY, "blue"), (TRACE_COLOUR_KEY, "#12345g"),
    (TRACE_COLOUR_KEY, "#123456\n"),
    (TRACE_WIDTH_KEY, 0.4), (TRACE_WIDTH_KEY, 15.1),
    (TRACE_WIDTH_KEY, float("inf")), (TRACE_WIDTH_KEY, True),
    (TRACE_STYLE_KEY, "Unknown"),
])
def test_invalid_trace_defaults_do_not_change_saved_settings(preferences, key, value):
    _dialog, cfg = preferences
    original = cfg.get(key)
    with pytest.raises(ValidationError):
        cfg.update(key, value)
    assert config().get(key) == original


def test_trace_defaults_affect_new_plots_and_additions_without_restyling_existing(mixed_plot):
    main, target, runs = mixed_plot
    # Make the source before changing defaults, so copying its old pen would fail.
    main.openPlot(runs["other_line"].guid)
    _wait_for(lambda: main.windows[-1] is not target and not main.windows[-1].worker.running)
    source = main.windows[-1]
    before = deepcopy(source._trace_styles)
    dialog = PreferencesDialog(main.config)
    try:
        dialog.preferencesApplied.connect(main.apply_current_settings)
        dialog.defaultTraceColourCombo.set_colour("#123456")
        dialog.defaultTraceWidthSpin.setValue(4.5)
        dialog.defaultTraceStyleCombo.setCurrentIndex(dialog.defaultTraceStyleCombo.findData("Dash"))
        assert dialog.apply_preferences()
        assert source._trace_styles == before
        assert source.line.opts["pen"].color().name() == "#1f77b4"
        _drop(target, runs["other_line"])
        added = _added(target, runs["other_line"])
        main.openPlot(runs["third_line"].guid)
        _wait_for(lambda: main.windows[-1] not in (source, target) and not main.windows[-1].worker.running)
        for line in (added, main.windows[-1].line):
            pen = line.opts["pen"]
            assert pen.color().name() == "#123456"
            assert pen.widthF() == 4.5
            assert pen.style() == QtCore.Qt.PenStyle.DashLine
        assert source._trace_styles == before
    finally:
        dialog.close()
        dialog.deleteLater()
