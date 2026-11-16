"""Publication failures retire partial rendering and permit a real retry."""

import numpy as np
import pytest

from qplot.windows._plot_publication import PublicationOutcome
from tests.support.windows.plot_integration import wait_for
from tests.support.windows.refresh_interval_precision import (
    live_view,
    wait_for_plot_load,
)


@pytest.mark.parametrize("heatmap,stage", [
    (False, "refresh_secondary_lines"), (False, "_complete_plot_publication"),
    (True, "_render_heatmap"), (True, "_restore_heatmap_interactions"),
    (True, "_complete_plot_publication"),
])
def test_interrupted_render_restores_plot_fields_and_can_retry(tmp_path, monkeypatch, heatmap, stage):
    with live_view(tmp_path, monkeypatch, 0., heatmap=heatmap) as (window, open_plot):
        plot = open_plot("signal_a")
        previous_axes = plot.axis_data
        previous_grid = getattr(plot, "dataGrid", None)
        published = []
        monkeypatch.setattr(plot, "_plot_preview_sink", lambda *args: published.append(args), raising=False)
        records = []
        owner = plot._publication_owner()
        begin = owner.begin

        def capture(*args, **kwargs):
            record = begin(*args, **kwargs)
            records.append(record)
            return record

        monkeypatch.setattr(owner, "begin", capture)
        original = getattr(plot, stage)

        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("injected render failure")

        with monkeypatch.context() as fault:
            fault.setattr(plot, stage, fail)
            assert plot.load_data()
            wait_for_plot_load(window, plot)

        assert records[-1].outcome is PublicationOutcome.RENDER_FAILED
        assert owner.current is None
        assert plot.axis_data is previous_axes
        if heatmap:
            assert plot.dataGrid is previous_grid
            assert plot._heatmap_geometry() is None
        else:
            assert plot.line.xData is None or not plot.line.xData.size
        assert not plot._qplot_display_synchronized
        assert not plot._refresh_controller().busy
        assert plot.worker not in window._plot_workers
        assert not published
        assert plot.plot_state_overlay.title_label.text() == "Plot display failed"
        assert plot.load_data()
        wait_for_plot_load(window, plot)
        assert records[-1].outcome is PublicationOutcome.COMMITTED
        np.testing.assert_array_equal(plot.axis_data["y"], previous_axes["y"])
        if heatmap:
            assert plot._heatmap_geometry() is not None
        else:
            np.testing.assert_array_equal(plot.line.yData, previous_axes["y"])
        assert len(published) == 1


@pytest.mark.parametrize("heatmap", [False, True])
@pytest.mark.parametrize("boundary", ["_set_param_axis_labels", "_complete_plot_publication"])
def test_nested_publication_is_not_finalised_by_the_outer_callback(
    tmp_path, monkeypatch, heatmap, boundary,
):
    with live_view(tmp_path, monkeypatch, 0., heatmap=heatmap) as (window, open_plot):
        plot = open_plot("signal_a")
        original = getattr(plot, boundary)
        older = []
        replacement = []
        previews = []
        monkeypatch.setattr(plot, "_plot_preview_sink", lambda host, result: previews.append(result), raising=False)

        def replace(*args, **kwargs):
            original(*args, **kwargs)
            if older:
                return
            older.append(plot._refresh_publication)
            assert plot.load_data(wait_on_thread=True)
            replacement.append(plot.worker)

        monkeypatch.setattr(plot, boundary, replace)
        assert plot.load_data()
        wait_for(lambda: bool(replacement) and not plot._refresh_controller().busy)
        assert plot.worker is replacement[0]
        assert older[0].outcome is PublicationOutcome.DISCARDED
        assert plot._refresh_publication is None
        assert previews == [replacement[0].result]
        assert plot.worker not in window._plot_workers


@pytest.mark.parametrize("heatmap", [False, True])
def test_cancel_during_drawing_cannot_commit_or_publish_a_preview(tmp_path, monkeypatch, heatmap):
    with live_view(tmp_path, monkeypatch, 0., heatmap=heatmap) as (window, open_plot):
        plot = open_plot("signal_a")
        previous_axes = plot.axis_data
        stage = "_restore_heatmap_interactions" if heatmap else "refresh_secondary_lines"
        original = getattr(plot, stage)
        records = []
        previews = []
        monkeypatch.setattr(plot, "_plot_preview_sink", lambda *args: previews.append(args), raising=False)

        def cancel():
            original()
            records.append(plot._refresh_publication)
            plot._cancel_plot_refresh()

        with monkeypatch.context() as fault:
            fault.setattr(plot, stage, cancel)
            assert plot.load_data()
            wait_for_plot_load(window, plot)
        assert records[0].outcome is PublicationOutcome.ROLLED_BACK
        assert plot.axis_data is previous_axes
        assert plot._refresh_publication is None
        assert plot.worker.is_cancelled()
        assert not plot._refresh_controller().busy
        assert not previews
        if heatmap:
            assert plot._heatmap_geometry() is None
        else:
            assert plot.line.xData is None or not plot.line.xData.size
        assert plot.load_data()
        wait_for_plot_load(window, plot)
        assert len(previews) == 1
