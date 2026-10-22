"""Replacing a mixed plot's primary source preserves its refresh choice."""

from types import SimpleNamespace

import pytest
from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from tests.windows.test_mixed_plot_drops import (
    _added,
    _drop,
    _wait_for,
    mixed_plot,  # noqa: F401
)
from tests.windows.test_refresh_interval_precision import (
    assert_interval,
    live_view,
    wait_for_plot_load,
)


def _promote_from_appearance(main, target):
    target.open_plot_appearance_dialog()
    dialog = target._plot_appearance_dialog
    dialog.table.selectRow(next(
        row for row in range(dialog.table.rowCount())
        if dialog._is_primary(*dialog._row_entry(row))
    ))
    assert dialog.remove_item_button.isEnabled()
    previous = set(main.windows)
    dialog.remove_item_button.click()
    replacement = next(win for win in main.windows if win not in previous)
    _wait_for(lambda: not replacement.worker.running
              and not getattr(replacement, "_refresh_pending", False))
    return replacement


@pytest.mark.parametrize("promoted", ["other_line", "other_heatmap"])
@pytest.mark.parametrize("interval", [0.0, 0.4])
def test_promotion_keeps_plot_refresh_control(mixed_plot, monkeypatch, promoted, interval):  # noqa: F811
    main, target, runs = mixed_plot
    monkeypatch.setattr(qtw.QMessageBox, "exec", lambda box: qtw.QMessageBox.StandardButton.Ok)
    assert main.config.get("user_preference.default_refresh_rate") == 1.0
    _drop(target, runs[promoted])
    _added(target, runs[promoted])
    target.spinBox.setValue(interval)
    if interval == 0:
        assert not target.monitor.isActive()

    replacement = _promote_from_appearance(main, target)
    assert replacement.spinBox.value() == interval
    if interval == 0:
        assert not replacement.monitor.isActive()
    assert main.config.get("user_preference.default_refresh_rate") == 1.0


@pytest.mark.parametrize("heatmap", [False, True], ids=["line", "heatmap"])
def test_live_source_promotion_preserves_pause_and_manual_refresh(tmp_path, monkeypatch, heatmap):
    with live_view(tmp_path, monkeypatch, 1.0, heatmap=heatmap) as (main, open_plot):
        target = open_plot("signal_a")
        source = open_plot("signal_b")
        assert source.ds.running
        run = SimpleNamespace(guid=source._guid, name=source.param.name,
                              axes=source.param.depends_on_)
        _drop(target, run)
        _added(target, run)
        target.spinBox.setValue(0)
        assert_interval(target, 0)

        with QtCore.QSignalBlocker(main.monitor), QtCore.QSignalBlocker(source.monitor):
            replacement = _promote_from_appearance(main, target)
            assert_interval(replacement, 0)
            previous_worker = replacement.worker
            replacement.refreshPlotButton.click()
            wait_for_plot_load(main, replacement)
            assert replacement.worker is not previous_worker
            assert_interval(replacement, 0)

            replacement.spinBox.setValue(0.2)
            assert_interval(replacement, 0.2)
            replacement.spinBox.setValue(0)
            assert_interval(replacement, 0)
