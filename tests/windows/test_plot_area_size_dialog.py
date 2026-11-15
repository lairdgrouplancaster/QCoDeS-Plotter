"""Custom canvas dimensions preserve ratios and apply only on acceptance."""

import pytest
from PyQt6 import QtCore, QtTest
from PyQt6 import QtWidgets as qtw

from qplot.windows._plotWin import _PlotAreaSizeDialog, plotWidget
from tests.support.windows.plot_dialog_lifecycle import dialog_plot as dialog_plot


@pytest.fixture
def size_dialog(qapplication):
    dialog = _PlotAreaSizeDialog(QtCore.QSize(800, 600))
    dialog.show()
    qapplication.processEvents()
    yield dialog
    dialog.close()
    dialog.deleteLater()
    qapplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


def test_editing_either_dimension_updates_its_partner_after_commit(size_dialog):
    dialog = size_dialog
    dialog.width_spin.setFocus()
    dialog.width_spin.selectAll()
    QtTest.QTest.keyClicks(dialog.width_spin, "1200")
    assert dialog.height_spin.value() == 600
    QtTest.QTest.keyClick(dialog.width_spin, QtCore.Qt.Key.Key_Tab)
    assert dialog.plot_area_size() == QtCore.QSize(1200, 900)
    dialog.height_spin.setFocus()
    dialog.height_spin.selectAll()
    QtTest.QTest.keyClicks(dialog.height_spin, "450")
    QtTest.QTest.keyClick(dialog.height_spin, QtCore.Qt.Key.Key_Tab)
    assert dialog.plot_area_size() == QtCore.QSize(600, 450)


def test_unlock_and_relock_adopts_the_entered_ratio(size_dialog):
    dialog = size_dialog
    dialog.lock_ratio.setChecked(False)
    dialog.width_spin.setValue(1000)
    assert dialog.height_spin.value() == 600
    dialog.height_spin.setValue(300)
    assert dialog.width_spin.value() == 1000
    dialog.lock_ratio.setChecked(True)
    assert dialog.plot_area_size() == QtCore.QSize(1000, 300)
    dialog.width_spin.setValue(500)
    assert dialog.plot_area_size() == QtCore.QSize(500, 150)
    dialog.height_spin.setValue(600)
    assert dialog.plot_area_size() == QtCore.QSize(2000, 600)


def test_whole_pixel_rounding_does_not_accumulate_ratio_drift(qapplication):
    dialog = _PlotAreaSizeDialog(QtCore.QSize(801, 603))
    try:
        for width in (1001, 800, 1200, 801):
            dialog.width_spin.setValue(width)
            assert abs(dialog.height_spin.value() - width * 603 / 801) <= 0.5
        assert dialog.plot_area_size() == QtCore.QSize(801, 603)
    finally:
        dialog.deleteLater()


@pytest.mark.parametrize("size, dimension, value, expected", [
    ((1600, 900), "height", 20_000, (20_000, 11_250)),
    ((900, 1600), "width", 20_000, (11_250, 20_000)),
    ((20_000, 1), "width", 1, (20_000, 1)),
    ((1, 20_000), "height", 1, (1, 20_000)),
    ((0, 0), "width", 10, (10, 10)),
])
def test_linked_dimensions_respect_pixel_bounds(qapplication, size, dimension, value, expected):
    dialog = _PlotAreaSizeDialog(QtCore.QSize(*size))
    try:
        getattr(dialog, dimension + "_spin").setValue(value)
        assert dialog.plot_area_size() == QtCore.QSize(*expected)
    finally:
        dialog.deleteLater()


@pytest.mark.parametrize("action", ["resize", "cancel", "escape", "close", "return"])
def test_only_acceptance_resizes_the_owner(qapplication, action):
    class Host(qtw.QMainWindow):
        open_custom_plot_area_size_dialog = plotWidget.open_custom_plot_area_size_dialog

        def _current_plot_area_size(self):
            return QtCore.QSize(800, 600)

        def resize_plot_area(self, width, height):
            applied.append((width, height))

    applied = []
    before_acceptance = []
    host = Host()
    host.show()

    def interact():
        dialog = host.findChild(_PlotAreaSizeDialog)
        dialog.width_spin.setFocus()
        dialog.width_spin.selectAll()
        QtTest.QTest.keyClicks(dialog.width_spin, "1200")
        before_acceptance.extend(applied)
        if action == "return":
            QtTest.QTest.keyClick(dialog.width_spin, QtCore.Qt.Key.Key_Return)
        elif action in ("resize", "cancel"):
            button = (qtw.QDialogButtonBox.StandardButton.Ok if action == "resize"
                      else qtw.QDialogButtonBox.StandardButton.Cancel)
            dialog.buttons.button(button).click()
        elif action == "escape":
            QtTest.QTest.keyClick(dialog, QtCore.Qt.Key.Key_Escape)
        else:
            dialog.close()

    try:
        QtCore.QTimer.singleShot(0, interact)
        host.open_custom_plot_area_size_dialog()
        assert before_acceptance == []
        assert applied == ([(1200, 900)] if action in ("resize", "return") else [])
    finally:
        host.close()
        host.deleteLater()
        qapplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


@pytest.mark.parametrize("dialog_plot", ["line"], indirect=True)
def test_closing_the_plot_rejects_pending_canvas_dimensions(dialog_plot, monkeypatch):
    _window, plot, app, _guids = dialog_plot
    applied = []
    observations = []
    monkeypatch.setattr(plot, "resize_plot_area", lambda *size: applied.append(size))

    def close_plot():
        dialog = plot.findChild(_PlotAreaSizeDialog)
        dialog.width_spin.setValue(1200)
        plot.close()
        observations.append(dialog.result())
        dialog.reject()

    QtCore.QTimer.singleShot(0, close_plot)
    plot.open_custom_plot_area_size_dialog()
    app.processEvents()
    assert applied == []
    assert observations == [qtw.QDialog.DialogCode.Rejected]
