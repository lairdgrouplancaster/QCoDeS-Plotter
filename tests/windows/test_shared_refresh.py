"""One main-window control reaches retained sources without duplicate work."""

from types import SimpleNamespace

import pytest

from qplot.windows._shared_refresh import (
    apply_shared_refresh_interval,
    iter_plot_sources,
    refresh_plot_sources,
)
from tests.windows.test_refresh_interval_precision import (
    assert_interval,
    live_view,
)


def test_source_graph_includes_closed_overlays_once_and_handles_cycles():
    first = SimpleNamespace(lines={}, heatmaps={})
    second = SimpleNamespace(lines={}, heatmaps={})
    hidden = SimpleNamespace(lines={}, heatmaps={}, _closed=True, _merged_trace_users=2)
    first.lines["hidden"] = SimpleNamespace(from_win=hidden)
    second.heatmaps["hidden"] = SimpleNamespace(from_win=hidden)
    hidden.lines["cycle"] = SimpleNamespace(from_win=first)
    owner = SimpleNamespace(windows=[first, second])
    found = list(iter_plot_sources(owner))
    assert len(found) == 3
    assert {id(plot) for plot in found} == {id(first), id(second), id(hidden)}


def test_manual_shared_refresh_skips_frozen_snapshots_and_deduplicates_sources():
    calls = []
    source = SimpleNamespace(refreshWindow=lambda **kwargs: calls.append(("source", kwargs)))
    plot = SimpleNamespace(
        refreshWindow=lambda **kwargs: calls.append(("plot", kwargs)),
        lines={"line": SimpleNamespace(from_win=source)},
        heatmaps={"image": SimpleNamespace(from_win=source)},
    )
    frozen = SimpleNamespace(
        _frozen_result=object(),
        refreshWindow=lambda **kwargs: calls.append(("frozen", kwargs)),
    )
    refresh_plot_sources(SimpleNamespace(windows=[plot, source, frozen]))
    assert sorted(calls) == [("plot", {"force": True}), ("source", {"force": True})]


@pytest.mark.parametrize("forced", [False, True])
def test_disabling_shared_refresh_discards_only_automatic_pending_work(tmp_path, monkeypatch, forced):
    with live_view(tmp_path, monkeypatch, .149) as (window, open_plot):
        plot = open_plot("signal_a")
        plot._refresh_pending = True
        plot._refresh_pending_force = forced
        apply_shared_refresh_interval(window, 0.)
        assert_interval(plot, 0.)
        assert plot._refresh_pending == forced
        assert plot._refresh_pending_force == forced
        plot._refresh_pending = False
        plot._refresh_pending_force = False


def test_main_refresh_button_updates_visible_and_closed_overlay_sources(tmp_path, monkeypatch):
    with live_view(tmp_path, monkeypatch, 0.) as (window, open_plot):
        target = open_plot("signal_a")
        source = open_plot("signal_b", show=False)
        assert window.add_trace_to_plot(
            target, source._dataset_key, source.param.name, param=source.param,
        )
        assert source._closed
        assert source not in window.windows
        calls = []
        monkeypatch.setattr(target, "refreshWindow", lambda **kwargs: calls.append(("target", kwargs)))
        monkeypatch.setattr(source, "refreshWindow", lambda **kwargs: calls.append(("source", kwargs)))
        window.refreshDatabaseButton.click()
        assert sorted(calls) == [("source", {"force": True}), ("target", {"force": True})]
        assert_interval(target, 0.)
        assert_interval(source, 0.)


def test_failed_shared_interval_save_keeps_all_plot_timers(tmp_path, monkeypatch):
    with live_view(tmp_path, monkeypatch, .149) as (window, open_plot):
        plot = open_plot("signal_a")
        errors = []
        monkeypatch.setattr(window, "show_error", lambda *args: errors.append(args))
        def fail_save(_path):
            raise OSError("settings unavailable")
        with monkeypatch.context() as failure:
            failure.setattr(window.config, "save_config", fail_save)
            window.spinBox.setValue(.2)
        assert errors
        assert_interval(window, .149)
        assert_interval(plot, .149)
