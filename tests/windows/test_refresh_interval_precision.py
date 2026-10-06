"""Valid configured intervals remain live despite rounded Qt control text."""

import hashlib
from contextlib import contextmanager

import numpy as np
import pytest
from PyQt6 import QtCore, QtTest
from PyQt6 import QtWidgets as qtw
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.configuration.config import config
from qplot.windows._plotWin import plotWidget
from qplot.windows._preferences import PreferencesDialog
from qplot.windows._refresh_interval import (
    refresh_interval_value,
    set_refresh_interval,
)
from qplot.windows.main import MainWindow
from qplot.windows.plot1d import plot1d
from tests._window_lifecycle import close_main_window
from tests.windows.test_plot_integration import wait_for


@contextmanager
def live_view(tmp_path, monkeypatch, rate, *, heatmap=False):
    """View only a new current QCoDeS measurement with its writer still open."""
    monkeypatch.setattr(config, "default_path", str(tmp_path / "settings"))
    monkeypatch.setattr(config, "default_file", str(tmp_path / "settings/config.json"))
    cfg = config()
    cfg.update_many({
        "user_preference.default_refresh_rate": rate,
        "user_preference.confirm_close": False,
        "user_preference.confirm_close_all": False,
    })
    path = tmp_path / "measurement.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("refresh_precision", sample_name="test")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("x")
    if heatmap:
        measurement.register_custom_parameter("y")
    setpoints = ("y", "x") if heatmap else ("x",)
    for name in ("signal_a", "signal_b"):
        measurement.register_custom_parameter(name, setpoints=setpoints)
    window = None
    errors = []
    try:
        with measurement.run(write_in_background=False) as saver:
            for index in range(4):
                coordinates = [("x", float(index % 2))]
                if heatmap:
                    coordinates.append(("y", float(index // 2)))
                saver.add_result(*coordinates, ("signal_a", index + 1.),
                                 ("signal_b", index + 11.))
            saver.flush_data_to_database()
            before = hashlib.sha256(path.read_bytes()).digest()
            parameters = {p.name: p for p in saver.dataset.get_parameters()}
            window = MainWindow()
            window.startupDatabaseTimer.stop()
            window.monitor.stop()
            monkeypatch.setattr(window, "show_error", lambda *args: errors.append(args))
            with QtCore.QSignalBlocker(window.monitor):
                assert window.load_file(str(path))
                wait_for(lambda: not window._database_load_active)

            def open_plot(name, *, show=True):
                with QtCore.QSignalBlocker(window.monitor), monkeypatch.context() as launch_patch:
                    # Metadata now arrives asynchronously. Fence the new plot's
                    # timer before its first worker, including the event batches
                    # needed to wait for the window itself to appear.
                    blockers = []
                    def fence_initial_load(plot, _worker):
                        if plot not in window.windows:
                            blockers.append(QtCore.QSignalBlocker(plot.monitor))
                    launch_patch.setattr(plotWidget, "_refresh_worker_will_start",
                                         fence_initial_load, raising=False)
                    prior_plot_count = len(window.windows)
                    window.openPlot(guid=saver.dataset.guid, params=[parameters[name]], show=show)
                    wait_for(lambda prior_plot_count=prior_plot_count: len(window.windows) > prior_plot_count)
                    plot = window.windows[-1]
                    monkeypatch.setattr(plot, "show_error", lambda *args: errors.append(args))
                    wait_for_plot_load(window, plot)
                    for blocker in blockers:
                        blocker.unblock()
                assert plot._last_error_text is None
                return plot

            try:
                yield window, open_plot
            finally:
                close_main_window(window)
                window = None
                assert errors == []
                assert hashlib.sha256(path.read_bytes()).digest() == before
    finally:
        if window is not None:
            close_main_window(window)
        experiment.conn.close()


def wait_for_plot_load(window, plot):
    # Drain one finite load. A live timer can start a replacement worker in the
    # same event batch as publication, hiding the idle state from the poll.
    # Leave both timers active at their exact configured intervals and restore
    # emissions before testing the refresh controls.
    with QtCore.QSignalBlocker(window.monitor), QtCore.QSignalBlocker(plot.monitor):
        wait_for(lambda: not plot.worker.running)


def assert_interval(owner, rate):
    assert refresh_interval_value(owner.spinBox) == rate
    assert owner.monitor.isActive() == (rate > 0)
    if rate > 0:
        assert owner.monitor.interval() == max(1, round(rate * 1000))


@pytest.mark.parametrize("rate", [0.049, 0.149, 0.0001, np.nextafter(0., 1.)])
def test_live_main_and_new_plot_keep_configured_rate_after_unrelated_apply(
    tmp_path, monkeypatch, rate,
):
    with live_view(tmp_path, monkeypatch, rate) as (window, open_plot):
        plot = open_plot("signal_a")
        assert_interval(window, rate)
        assert_interval(plot, rate)
        plot.monitor.stop()
        plot._ensure_refresh_monitor()
        assert_interval(plot, rate)
        dialog = PreferencesDialog(window.config, window)
        dialog.preferencesApplied.connect(window.apply_current_settings)
        try:
            dialog.themeCombo.setCurrentIndex(dialog.themeCombo.findData("dark"))
            dialog.buttonBox.button(qtw.QDialogButtonBox.StandardButton.Apply).click()
            assert window.config.get("user_preference.default_refresh_rate") == rate
            assert_interval(window, rate)
            assert_interval(plot, rate)
            window.config.update("user_preference.default_refresh_rate", 1.049)
            window.apply_current_settings()
            assert_interval(window, 1.049)
            assert_interval(plot, rate)
        finally:
            dialog.close()
            dialog.deleteLater()


def test_initial_load_fence_keeps_live_timers_and_restores_timeout_delivery(
    tmp_path, monkeypatch,
):
    publications = []
    original_refresh = plot1d.refreshPlot

    def publish_then_due_timeout(plot, *args, **kwargs):
        result = original_refresh(plot, *args, **kwargs)
        publications.append((plot.monitor.isActive(), plot.monitor.interval(),
                             plot.monitor.signalsBlocked()))
        # Deliver a due timeout in the publication's event batch instead of
        # depending on the runner's speed to reproduce the polling race.
        plot.monitor.timeout.emit()
        return result

    monkeypatch.setattr(plot1d, "refreshPlot", publish_then_due_timeout)
    with live_view(tmp_path, monkeypatch, .0001) as (window, open_plot):
        plot = open_plot("signal_a")
        assert publications == [(True, 1, True)]
        for owner in (window, plot):
            assert_interval(owner, .0001)
            assert not owner.monitor.signalsBlocked()
        np.testing.assert_array_equal(plot.line.getOriginalDataset()[1], [1., 2., 3., 4.])
        timeouts = []
        plot.monitor.timeout.connect(lambda: timeouts.append(True))
        first_worker = plot.worker
        plot.monitor.timeout.emit()
        assert timeouts == [True]
        assert plot.worker is not first_worker
        wait_for_plot_load(window, plot)
        assert publications == [(True, 1, True), (True, 1, True)]
        assert not plot.monitor.signalsBlocked()
        assert_interval(plot, .0001)
        np.testing.assert_array_equal(plot.line.getOriginalDataset()[1], [1., 2., 3., 4.])


def type_zero(spin):
    editor = spin.lineEdit()
    editor.setFocus()
    QtTest.QTest.keyClick(editor, QtCore.Qt.Key.Key_A,
                        QtCore.Qt.KeyboardModifier.ControlModifier)
    QtTest.QTest.keyClicks(editor, "0.0")
    QtTest.QTest.keyClick(editor, QtCore.Qt.Key.Key_Return)


def test_unedited_return_preserves_rate_and_typing_same_display_zero_disables(
    tmp_path, monkeypatch,
):
    with live_view(tmp_path, monkeypatch, .049) as (window, open_plot):
        plot = open_plot("signal_a")
        for owner in (window, plot):
            assert owner.spinBox.value() == 0
            assert "0.049" in owner.spinBox.toolTip()
            QtTest.QTest.keyClick(owner.spinBox.lineEdit(), QtCore.Qt.Key.Key_Return)
            assert_interval(owner, .049)
            type_zero(owner.spinBox)
            assert_interval(owner, 0.)
        assert window.config.get("user_preference.default_refresh_rate") == 0
        window.spinBox.stepUp()
        assert_interval(window, .1)
        assert window.config.get("user_preference.default_refresh_rate") == .1


def test_failed_setting_write_restores_precise_interval_and_timer(
    tmp_path, monkeypatch,
):
    with live_view(tmp_path, monkeypatch, .049) as (window, _open_plot):
        errors = []
        monkeypatch.setattr(window, "show_error", lambda *args: errors.append(args))
        def fail_save(_path):
            raise OSError("settings unavailable")
        with monkeypatch.context() as failure:
            failure.setattr(window.config, "save_config", fail_save)
            window.spinBox.setValue(.1)
        assert len(errors) == 1
        assert window.config.get("user_preference.default_refresh_rate") == .049
        assert_interval(window, .049)
        window.spinBox.setValue(.2)
        assert_interval(window, .2)


@pytest.mark.parametrize("heatmap", [False, True], ids=["line", "heatmap"])
def test_hidden_live_sources_copy_and_rearm_the_exact_parent_rate(
    tmp_path, monkeypatch, heatmap,
):
    with live_view(tmp_path, monkeypatch, .149, heatmap=heatmap) as (window, open_plot):
        target = open_plot("signal_a")
        source = open_plot("signal_b", show=False)
        set_refresh_interval(target.spinBox, .049)
        target.monitorIntervalChanged(.049)
        source.monitor.stop()
        assert window.add_trace_to_plot(
            target, source._dataset_key, source.param.name, param=source.param,
        )
        assert source._closed
        assert source._merged_trace_users == 1
        assert_interval(source, .049)
        QtTest.QTest.keyClick(target.spinBox.lineEdit(), QtCore.Qt.Key.Key_Return)
        assert_interval(target, .049)
        assert_interval(source, .049)
        source.monitor.stop()
        if heatmap:
            target.refresh_secondary_heatmaps()
        else:
            target.refresh_secondary_lines()
        assert_interval(source, .049)
        type_zero(target.spinBox)
        assert_interval(source, 0.)
