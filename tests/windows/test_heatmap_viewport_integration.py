"""Real SQL heatmaps retain only committed ranges across viewport changes."""

import pytest

from qplot.windows._heatmap_viewport import AxisRanges
from tests.support.windows.plot_integration import build_synthetic_database, wait_for
from tests.support.windows.sql_heatmap import (
    assert_source_values,
    idle,
    sql_heatmap,
    subset,
    view,
)


@pytest.mark.parametrize("stage", ["_render_heatmap", "_complete_plot_publication"])
@pytest.mark.parametrize("return_to_full", [False, True], ids=["zoom", "full"])
def test_failed_viewport_render_does_not_advance_range_and_retry_succeeds(
    tmp_path, monkeypatch, stage, return_to_full,
):
    with sql_heatmap(tmp_path, monkeypatch) as (_window, plot):
        full = plot._viewport_state.full
        zoom = subset(full, .2, .8)
        if return_to_full:
            view(plot, zoom)
            plot._reload_visible_heatmap_data()
            idle(plot)
            assert plot._viewport_state.displayed.matches(zoom)
        before = plot._viewport_state
        original = getattr(plot, stage)

        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("injected viewport rendering failure")

        def reload():
            if return_to_full:
                plot._zoom_large_heatmap_to_all()
            else:
                view(plot, zoom)
                plot._reload_visible_heatmap_data()

        with monkeypatch.context() as fault:
            fault.setattr(plot, stage, fail)
            reload()
            idle(plot)
        assert plot._viewport_state is before
        assert plot._heatmap_geometry() is None
        assert plot.plot_state_overlay.title_label.text() == "Plot display failed"
        reload()
        idle(plot)
        assert plot._heatmap_geometry() is not None
        if return_to_full:
            assert plot._viewport_state.displayed is None
        else:
            assert plot._viewport_state.displayed.matches(zoom)
        assert_source_values(plot)


@pytest.mark.parametrize("return_to_full", [False, True], ids=["latest-zoom", "full"])
def test_new_viewport_intent_survives_a_busy_publication(tmp_path, monkeypatch, return_to_full):
    with sql_heatmap(tmp_path, monkeypatch) as (_window, plot):
        full = plot._viewport_state.full
        first = subset(full, .1, .6)
        latest = subset(full, .3, .9)
        original = plot._complete_plot_publication
        loads = []
        load_data = plot.load_data

        def record_load(**kwargs):
            loads.append(kwargs["heatmap_axis_ranges"])
            return load_data(**kwargs)

        triggered = []
        def change_view(worker):
            original(worker)
            if triggered:
                return
            triggered.append(True)
            assert plot._refresh_controller().busy
            if return_to_full:
                plot._zoom_large_heatmap_to_all()
            else:
                for ranges in (subset(full, .2, .7), latest):
                    view(plot, ranges)
                    plot._schedule_visible_heatmap_reload()

        monkeypatch.setattr(plot, "load_data", record_load)
        monkeypatch.setattr(plot, "_complete_plot_publication", change_view)
        view(plot, first)
        plot._reload_visible_heatmap_data()
        idle(plot)
        assert triggered
        assert len(loads) == 2
        assert AxisRanges.normalise(loads[0]).matches(first)
        if return_to_full:
            assert loads[1] is None
            assert plot._viewport_state.displayed is None
        else:
            assert AxisRanges.normalise(loads[1]).matches(latest)
            assert plot._viewport_state.displayed.matches(latest)
        assert_source_values(plot)


@pytest.mark.parametrize("replace_source", [False, True], ids=["close", "source-rejected"])
def test_retired_viewport_callback_cannot_start_another_read(tmp_path, monkeypatch, replace_source):
    with sql_heatmap(tmp_path, monkeypatch) as (window, plot):
        controller = plot._viewport_controller()
        ui = controller.host
        callbacks = []
        schedule = ui.schedule

        def capture(callback):
            callbacks.append(callback)
            schedule(callback)

        monkeypatch.setattr(ui, "schedule", capture)
        view(plot, subset(controller.accepted.full, .2, .8))
        plot._schedule_visible_heatmap_reload()
        assert controller.pending
        reads = []
        monkeypatch.setattr(plot, "load_data", lambda **kwargs: reads.append(kwargs))
        if replace_source:
            replacements = []
            plot.database_replaced.disconnect()
            plot.database_replaced.connect(replacements.append)
            # Inject rejection at the identity boundary, exercising the real
            # cancellation path without modifying the protected source file.
            monkeypatch.setattr(plot, "_source_database_matches_key", lambda: False)
            current = plot._source_database_is_current()
            assert not current
            assert replacements == [plot._dataset_key.database_path]
        else:
            plot.close()
        for callback in callbacks:
            callback()
        assert not controller.pending
        assert not controller.accepted.enabled
        assert not reads


def test_switching_selected_database_preserves_retained_plot_viewport(tmp_path, monkeypatch):
    with sql_heatmap(tmp_path, monkeypatch) as (window, plot):
        key = plot._dataset_key
        zoom = subset(plot._viewport_state.full, .2, .8)
        other = tmp_path / "other.db"
        build_synthetic_database(other)
        loaded = window.load_file(str(other))
        assert loaded
        wait_for(lambda: not window._database_load_active)
        view(plot, zoom)
        plot._schedule_visible_heatmap_reload()
        idle(plot)
        assert plot._dataset_key == key
        assert plot._viewport_state.displayed.matches(zoom)
        assert_source_values(plot)


@pytest.mark.parametrize("new_full", [False, True], ids=["detail", "full"])
def test_nested_newer_sql_publication_owns_accepted_range_and_preview(tmp_path, monkeypatch, new_full):
    with sql_heatmap(tmp_path, monkeypatch) as (_window, plot):
        full = plot._viewport_state.full
        older = subset(full, .1, .6)
        newer = subset(full, .3, .9)
        original = plot._complete_plot_publication
        replaced = []
        previews = []
        monkeypatch.setattr(plot, "_plot_preview_sink", lambda host, result: previews.append(
            (AxisRanges.normalise(result.heatmap_axis_ranges), host._viewport_state.displayed)), raising=False)

        def replace(worker):
            original(worker)
            if replaced:
                return
            replaced.append(worker)
            view(plot, full if new_full else newer)
            started = plot.load_data(wait_on_thread=True, force_sql_heatmap=True,
                                     heatmap_axis_ranges=None if new_full else newer.as_dict(),
                                     heatmap_full_axis_ranges=full.as_dict())
            assert started

        monkeypatch.setattr(plot, "_complete_plot_publication", replace)
        view(plot, older)
        plot._reload_visible_heatmap_data()
        idle(plot)
        assert plot.worker is not replaced[0]
        if new_full:
            assert plot._viewport_state.displayed is None
            assert previews == [(None, None)]
        else:
            assert plot._viewport_state.displayed.matches(newer)
            # Cropped detail results must never replace the full-run preview.
            assert previews == []
        assert_source_values(plot)
