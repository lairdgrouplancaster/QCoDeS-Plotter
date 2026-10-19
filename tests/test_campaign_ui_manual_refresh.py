"""Manual refresh reloads the sources represented by a paused mixed plot."""

import sys
from contextlib import contextmanager
from types import SimpleNamespace

import numpy as np
import pytest
from PyQt6 import QtGui
from PyQt6 import QtWidgets as qtw
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.configuration.config import config
from qplot.windows.main import MainWindow
from tests._window_lifecycle import close_main_window
from tests.windows.test_heatmap_layer_integration import (
    _assert_reader_artifact_invariant,
    _database_artifact_state,
)
from tests.windows.test_mixed_plot_drops import _added, _drop, _wait_for


def _idle(plot):
    return not plot.worker.running and not getattr(plot, "_refresh_pending", False)


def _values(plot):
    if plot.operation_kind == "plot2d":
        return np.asarray(plot.dataGrid)
    return plot.line.getOriginalDataset()[1]


def _layer_values(layer, heatmap):
    return np.asarray(layer.data_grid) if heatmap else layer.getOriginalDataset()[1]


def _manual_refresh(plot, control):
    if control == "button":
        plot.refreshPlotButton.click()
    else:
        action = next(action for action in plot.findChildren(QtGui.QAction)
                      if action.text() == "&Refresh")
        assert any(shortcut.toString() == "R" for shortcut in action.shortcuts())
        action.trigger()


@contextmanager
def _live_mixed_plot(tmp_path, monkeypatch, host_heatmap, source_heatmap, *, visible=False):
    monkeypatch.setattr(config, "default_path", str(tmp_path / "settings"))
    monkeypatch.setattr(config, "default_file", str(tmp_path / "settings/config.json"))
    cfg = config()
    cfg.update_many({
        "user_preference.default_refresh_rate": 0.1 if visible else 0,
        "user_preference.confirm_close": False,
        "user_preference.confirm_close_all": False,
    })
    path = tmp_path / "measurement.db"
    initialise_or_create_database_at(str(path), journal_mode="WAL")
    experiment = load_or_create_experiment("manual_refresh", sample_name="test")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("x")
    measurement.register_custom_parameter("y")
    axes = {
        "a": ("y", "x") if host_heatmap else ("x",),
        "b": ("y", "x") if source_heatmap else ("x",),
    }
    for name, setpoints in axes.items():
        measurement.register_custom_parameter(name, setpoints=setpoints)
    window = None
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *args: errors.append(args))
    monkeypatch.setattr(qtw.QMessageBox, "exec", lambda box: qtw.QMessageBox.StandardButton.Ok)
    try:
        with measurement.run(write_in_background=False) as saver:
            artifacts = None

            def append(start, end):
                nonlocal artifacts
                if artifacts is not None:
                    _assert_reader_artifact_invariant(path, artifacts)
                for index in range(start, end):
                    saver.add_result(
                        ("x", float(index % 2)), ("y", float(index // 2)),
                        ("a", float(index + 1)), ("b", float(index + 11)),
                    )
                saver.flush_data_to_database()
                # The fixture's explicit writer is the only allowed mutation.
                artifacts = _database_artifact_state(path)
                recorded = saver.dataset.get_parameter_data("b")["b"]["b"]
                np.testing.assert_array_equal(recorded.ravel(), np.arange(11., 11. + end))

            append(0, 2)
            window = MainWindow()
            window.startupDatabaseTimer.stop()
            window.monitor.stop()
            monkeypatch.setattr(window, "show_error", lambda *args: errors.append(args))
            window.close_database(status=False)
            assert window.load_file(str(path))
            _wait_for(lambda: not window._database_load_active)
            parameters = {param.name: param for param in saver.dataset.get_parameters()}

            def open_plot(name):
                count = len(window.windows)
                window.openPlot(guid=saver.dataset.guid, params=[parameters[name]])
                _wait_for(lambda: len(window.windows) > count)
                plot = window.windows[-1]
                plot.spinBox.setValue(0)
                _wait_for(lambda: _idle(plot))
                return plot

            host = open_plot("a")
            if visible:
                source = open_plot("b")
            run = SimpleNamespace(guid=saver.dataset.guid, name="b", axes=axes["b"])
            _drop(host, run)
            layer = _added(host, run)
            if visible:
                assert layer.from_win is source
            source = layer.from_win
            _wait_for(lambda: _idle(source))
            assert source.visible == visible
            assert source._closed == (not visible)
            assert source._merged_trace_users == 1
            for plot in (host, source):
                assert plot.spinBox.value() == 0
                assert not plot.monitor.isActive()
            if visible:
                # Exercise actual main-window polling while both plots pause.
                timeouts = []
                window.monitor.timeout.connect(lambda: timeouts.append(True))
                assert window.monitor.isActive()
                _wait_for(lambda: bool(timeouts))
            else:
                assert not window.monitor.isActive()
            try:
                yield window, host, layer, append
            finally:
                close_main_window(window)
                window = None
                _assert_reader_artifact_invariant(path, artifacts)
                assert errors == []
    finally:
        if window is not None:
            close_main_window(window)
        experiment.conn.close()


def _record_loads(monkeypatch, *plots):
    counts = {plot: 0 for plot in plots}
    for plot in plots:
        original = plot.load_data

        def load(*args, plot=plot, original=original, **kwargs):
            counts[plot] += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(plot, "load_data", load)
    return counts


def test_manual_refresh_does_not_recurse_through_mutual_overlays(tmp_path, monkeypatch):
    with _live_mixed_plot(tmp_path, monkeypatch, False, False, visible=True) as (
        _window, host, layer, append,
    ):
        source = layer.from_win
        run = SimpleNamespace(guid=host._guid, name=host.param.name, axes=host.param.depends_on_)
        _drop(source, run)
        reverse = _added(source, run)
        assert reverse.from_win is host
        counts = _record_loads(monkeypatch, host, source)
        append(2, 4)
        host.refreshPlotButton.click()
        _wait_for(lambda: _idle(host) and _idle(source))
        assert counts == {host: 1, source: 1}
        np.testing.assert_array_equal(layer.getOriginalDataset()[1], [11, 12, 13, 14])
        np.testing.assert_array_equal(reverse.getOriginalDataset()[1], [1, 2, 3, 4])


def test_manual_refresh_updates_cut_and_heatmap_from_same_dataset(tmp_path, monkeypatch):
    with _live_mixed_plot(tmp_path, monkeypatch, False, True, visible=True) as (
        window, host, layer, append,
    ):
        source = layer.from_win
        source.z_index = [0, 0]
        source.openSweep("h")
        cut = window.windows[-1]
        cut.spinBox.setValue(0)
        _wait_for(lambda: _idle(cut))
        window.post_admin()
        box = next(box for box in host.option_boxes if box.option_box.isEnabled())
        index = next(index for index in range(box.option_box.count())
                     if box.option_box.itemData(index) == cut._trace_key)
        box.option_box.setCurrentIndex(index)
        cut_layer = host.lines[cut._trace_key]
        counts = _record_loads(monkeypatch, host, source, cut)
        append(2, 6)
        host.refreshPlotButton.click()
        _wait_for(lambda: all(_idle(plot) for plot in (host, source, cut)))
        assert counts == {host: 1, source: 1, cut: 1}
        np.testing.assert_array_equal(layer.data_grid, [[11, 12], [13, 14], [15, 16]])
        assert cut.picker.slider.maximum() == 2
        cut.picker.slider.setValue(2)
        np.testing.assert_array_equal(cut_layer.getOriginalDataset()[1], [15, 16])
        assert source.sweep_lines[cut.sweep_id].value() == 2
        assert all(not plot.monitor.isActive() for plot in (host, source, cut))


@pytest.mark.parametrize("heatmap", [False, True], ids=["line", "heatmap"])
def test_manual_refresh_coalesces_busy_sources_and_close_cancels(tmp_path, monkeypatch, heatmap):
    with _live_mixed_plot(tmp_path, monkeypatch, heatmap, heatmap) as (
        window, host, layer, append,
    ):
        source = layer.from_win
        counts = _record_loads(monkeypatch, host, source)
        append(2, 4)
        # No GUI event processing between clicks: the first workers have not
        # published, so two more requests must coalesce into one final load.
        for _ in range(3):
            host.refreshPlotButton.click()
        _wait_for(lambda: _idle(host) and _idle(source))
        assert counts == {host: 2, source: 2}
        np.testing.assert_array_equal(_layer_values(layer, heatmap).ravel(), [11, 12, 13, 14])
        host.refreshPlotButton.click()
        host.close()
        _wait_for(lambda: _idle(host) and _idle(source))
        assert not window.windows
        assert source._merged_trace_users == 0
        assert not source._can_process_refresh()
        assert not host.monitor.isActive() and not source.monitor.isActive()


def test_applying_primary_operations_does_not_refresh_secondary_source(tmp_path, monkeypatch):
    with _live_mixed_plot(tmp_path, monkeypatch, False, True) as (
        _window, host, layer, append,
    ):
        source = layer.from_win
        counts = _record_loads(monkeypatch, host, source)
        append(2, 4)
        host.oper_widget.apply_but.click()
        _wait_for(lambda: _idle(host) and _idle(source))
        assert counts == {host: 1, source: 0}
        np.testing.assert_array_equal(host.line.getOriginalDataset()[1], [1, 2, 3, 4])
        np.testing.assert_array_equal(layer.data_grid, [[11, 12]])
        host.refreshPlotButton.click()
        _wait_for(lambda: _idle(host) and _idle(source))
        assert counts == {host: 2, source: 1}
        np.testing.assert_array_equal(layer.data_grid, [[11, 12], [13, 14]])


@pytest.mark.parametrize("host_heatmap", [False, True], ids=["line_host", "heatmap_host"])
@pytest.mark.parametrize("source_heatmap", [False, True], ids=["line_source", "heatmap_source"])
@pytest.mark.parametrize("visible", [False, True], ids=["hidden", "visible"])
@pytest.mark.parametrize("control", ["button", "menu"])
def test_manual_refresh_updates_paused_sources(
    tmp_path, monkeypatch, host_heatmap, source_heatmap, visible, control,
):
    with _live_mixed_plot(tmp_path, monkeypatch, host_heatmap, source_heatmap,
                          visible=visible) as (window, host, layer, append):
        source = layer.from_win
        users = window.dataset_holder[source._dataset_key].users
        for start, end in ((2, 4), (4, 6)):
            append(start, end)
            previous_source_worker = source.worker
            _manual_refresh(host, control)
            _wait_for(lambda: _idle(host) and _idle(source))
            np.testing.assert_array_equal(_values(host).ravel(), np.arange(1., 1. + end))
            np.testing.assert_array_equal(
                _layer_values(layer, source_heatmap).ravel(), np.arange(11., 11. + end),
            )
            assert source.worker is not previous_source_worker
            assert source.visible == visible
            assert source._merged_trace_users == 1
            assert window.dataset_holder[source._dataset_key].users == users
            for plot in (host, source):
                assert plot.spinBox.value() == 0
                assert not plot.monitor.isActive()
