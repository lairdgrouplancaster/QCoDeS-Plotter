"""Disposable modal dialogs release their Qt ownership after dismissal."""

import pytest
from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from qplot.windows._plot_feedback import PlotWindowFeedbackMixin
from qplot.windows._plotWin import plotWidget
from qplot.windows._preferences import PreferencesDialog
from qplot.windows.main import MainWindow
from qplot.windows.plot2d import plot2d
from tests._window_lifecycle import close_main_window
from tests.windows.test_plot_integration import configure_temp_qplot


class DialogOwner(PlotWindowFeedbackMixin, qtw.QMainWindow):
    """Qt owner for production modal factories that need no measurement data."""

    show_preferences_dialog = plotWidget.show_preferences_dialog
    open_custom_plot_area_size_dialog = plotWidget.open_custom_plot_area_size_dialog
    show_heatmap_downsample_dialog = plot2d.show_heatmap_downsample_dialog
    _new_heatmap_downsample_dialog = plot2d._new_heatmap_downsample_dialog
    _heatmap_downsample_dialog_message = plot2d._heatmap_downsample_dialog_message

    def __init__(self, config):
        super().__init__()
        self.config = config
        self.visible = True
        self._heatmap_downsample_info = {"source_sampled": True}
        self.resize_requests = []

    def _current_plot_area_size(self):
        return QtCore.QSize(640, 480)

    def resize_plot_area(self, width, height):
        self.resize_requests.append((width, height))

    def apply_current_settings(self):
        pass


@pytest.fixture
def dialog_owners(tmp_path, monkeypatch, qapplication):
    configure_temp_qplot(monkeypatch, tmp_path)
    main = MainWindow()
    main.startupDatabaseTimer.stop()
    main.monitor.stop()
    plot = DialogOwner(main.config)
    try:
        yield main, plot
    finally:
        plot.hide()
        plot.deleteLater()
        close_main_window(main)


@pytest.mark.parametrize("factory", [
    "main_preferences", "plot_preferences", "main_error", "plot_error",
    "plot_size", "heatmap_downsample",
])
@pytest.mark.parametrize("accepted", [False, True])
def test_disposable_dialog_is_destroyed_after_return(dialog_owners, qapplication, factory, accepted):
    main, plot = dialog_owners
    owner = main if factory.startswith("main_") else plot
    destroyed = []
    for iteration in range(2):
        def dismiss():
            dialog = qapplication.activeModalWidget()
            assert isinstance(dialog, qtw.QDialog)
            dialog.destroyed.connect(lambda: destroyed.append(True))
            if accepted and isinstance(dialog, PreferencesDialog):
                dialog.themeCombo.setCurrentIndex(dialog.themeCombo.findData("dark"))
                assert dialog.apply_preferences()
            dialog.accept() if accepted else dialog.reject()

        QtCore.QTimer.singleShot(0, dismiss)
        if factory.endswith("preferences"):
            owner.show_preferences_dialog()
        elif factory.endswith("error"):
            owner.show_error("Lifecycle test", "Dismiss this temporary diagnostic.", "Details")
        elif factory == "plot_size":
            owner.open_custom_plot_area_size_dialog()
        elif factory == "heatmap_downsample":
            owner.show_heatmap_downsample_dialog()
        qapplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        qapplication.processEvents()
        assert len(destroyed) == iteration + 1, f"{factory} retained its dismissed dialog"
        assert owner.findChildren(qtw.QDialog) == []
    if factory == "plot_size" and accepted:
        assert plot.resize_requests == [(640, 480), (640, 480)]
    if factory.endswith("preferences") and accepted:
        assert main.config.get("user_preference.theme") == "dark"


@pytest.mark.parametrize("factory", ["main_preferences", "plot_preferences", "heatmap_downsample"])
def test_disposable_dialog_is_destroyed_if_exec_raises(dialog_owners, qapplication, monkeypatch, factory):
    main, plot = dialog_owners
    owner = main if factory.startswith("main_") else plot
    destroyed = []

    def fail_exec(dialog):
        dialog.destroyed.connect(lambda: destroyed.append(True))
        raise RuntimeError("Modal event loop failed")

    monkeypatch.setattr(qtw.QDialog, "exec", fail_exec)
    with pytest.raises(RuntimeError, match="Modal event loop failed"):
        if factory.endswith("preferences"):
            owner.show_preferences_dialog()
        else:
            owner.show_heatmap_downsample_dialog()
    qapplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    qapplication.processEvents()
    assert destroyed == [True]
    assert owner.findChildren(qtw.QDialog) == []
