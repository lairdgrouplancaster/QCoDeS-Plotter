"""Real heatmap warnings follow committed images, failures and source lifetime."""

import gc
import weakref

import pytest
from PyQt6 import QtWidgets as qtw

from qplot.tools.heatmap_resolution import (
    FULL_HEATMAP_POINTS_KEY,
    HEATMAP_GRID_CELLS_KEY,
    resolution_details,
    resolution_summary,
)
from tests.support.windows.sql_heatmap import idle, sql_heatmap, subset, view


@pytest.mark.parametrize("grid_cells, expected", [(4, HEATMAP_GRID_CELLS_KEY), (1000, FULL_HEATMAP_POINTS_KEY)])
def test_published_report_identifies_the_adjustable_limit(tmp_path, monkeypatch, grid_cells, expected):
    with sql_heatmap(tmp_path, monkeypatch, grid_cells=grid_cells) as (_window, plot):
        report = plot._heatmap_resolution_report
        assert report.limiting_preference == expected
        reason = ("display grid is limited to 4 cells" if grid_cells == 4
                  else "full-resolution heatmap limit of 4 points")
        assert reason in resolution_summary(report)


def test_hidden_source_releases_completed_publication_snapshot(tmp_path, monkeypatch):
    with sql_heatmap(tmp_path, monkeypatch, grid_cells=4) as (_window, plot):
        assert "_heatmap_resolution_ui" not in plot.__dict__
        owner = plot._publication_owner()
        original = owner.begin
        publications = []

        def begin(*args, **kwargs):
            publication = original(*args, **kwargs)
            publications.append(weakref.ref(publication))
            return publication

        monkeypatch.setattr(owner, "begin", begin)
        assert plot.load_data(force_sql_heatmap=True)
        idle(plot)
        gc.collect()
        # The committed report must not retain the worker and rollback arrays,
        # even when no warning widget or preview ever observes that report.
        assert publications
        assert all(reference() is None for reference in publications)
        assert plot._heatmap_resolution_report is not None


@pytest.mark.parametrize("failure_stage", ["_render_heatmap", "_complete_plot_publication"])
def test_failed_render_hides_report_and_open_dialog_then_retry_publishes(tmp_path, monkeypatch, failure_stage):
    with sql_heatmap(tmp_path, monkeypatch, grid_cells=4, show=True) as (_window, plot):
        old = plot._heatmap_resolution_report
        assert old is not None
        assert old.displayed.cells == plot.dataGrid.size
        ui = plot.__dict__["_heatmap_resolution_ui"]
        dialog = ui.new_dialog()
        dialog.show()
        original = getattr(plot, failure_stage)
        before_commit = []

        def fail(*args):
            original(*args)
            before_commit.append(plot._heatmap_resolution_report)
            raise RuntimeError("Injected resolution publication failure")

        zoom = subset(plot._viewport_state.full, .2, .8)
        view(plot, zoom)
        try:
            with monkeypatch.context() as fault:
                fault.setattr(plot, failure_stage, fail)
                plot._reload_visible_heatmap_data()
                idle(plot)
            assert before_commit == [old]
            assert plot._heatmap_geometry() is None
            assert plot._heatmap_resolution_report is None
            assert plot.heatmap_downsample_button.isHidden()
            assert not dialog.isVisible()

            plot._reload_visible_heatmap_data()
            idle(plot)
            report = plot._heatmap_resolution_report
            assert report is not None
            assert report is not old
            assert plot._heatmap_geometry() is not None
            assert not plot.heatmap_downsample_button.isHidden()
            assert report.displayed.cells == plot.dataGrid.size
            assert report.x_range == zoom.x
            assert report.y_range == zoom.y
        finally:
            dialog.deleteLater()


def test_newer_nested_publication_owns_report_and_preview(tmp_path, monkeypatch):
    with sql_heatmap(tmp_path, monkeypatch, grid_cells=4, show=True) as (_window, plot):
        older_report = plot._heatmap_resolution_report
        original = plot._complete_plot_publication
        replaced = []
        previews = []

        def replace(worker):
            original(worker)
            if replaced:
                return
            replaced.append(worker)
            plot.config.config["runtime_settings"]["max_heatmap_grid_cells"] = 9
            view(plot, plot._viewport_state.full)
            assert plot.load_data(wait_on_thread=True, force_sql_heatmap=True)

        monkeypatch.setattr(plot, "_complete_plot_publication", replace)
        monkeypatch.setattr(plot, "_plot_preview_sink",
                            lambda host, result: previews.append(host._heatmap_resolution_report), raising=False)
        view(plot, subset(plot._viewport_state.full, .2, .8))
        plot._reload_visible_heatmap_data()
        idle(plot)
        report = plot._heatmap_resolution_report
        assert report is not None
        assert report.displayed.cells == plot.dataGrid.size
        assert report.displayed.cells > older_report.displayed.cells
        assert previews == [report]
        assert not plot.heatmap_downsample_button.isHidden()


def test_open_report_updates_on_commit_and_closes_for_full_resolution(tmp_path, monkeypatch):
    with sql_heatmap(tmp_path, monkeypatch, grid_cells=4, show=True) as (_window, plot):
        ui = plot.__dict__["_heatmap_resolution_ui"]
        dialog = ui.new_dialog()
        dialog.show()
        summary = dialog.findChild(qtw.QLabel, "heatmapResolutionSummary")
        details = dialog.findChild(qtw.QPlainTextEdit, "heatmapResolutionDetails")
        dialog.findChild(qtw.QToolButton, "heatmapResolutionDetailsButton").click()
        width = dialog.width()
        try:
            plot.config.config["runtime_settings"]["max_heatmap_grid_cells"] = 9
            assert plot.load_data(force_sql_heatmap=True)
            idle(plot)
            report = plot._heatmap_resolution_report
            assert dialog.isVisible()
            assert summary.text() == resolution_summary(report)
            assert details.toPlainText() == resolution_details(report)
            assert details.isVisible()
            assert dialog.width() == width

            plot.config.config["runtime_settings"]["max_heatmap_grid_cells"] = 100
            plot.config.config["runtime_settings"]["max_full_heatmap_points"] = 100
            assert plot.load_data()
            idle(plot)
            assert plot._heatmap_resolution_report is None
            assert plot.heatmap_downsample_button.isHidden()
            assert not dialog.isVisible()
        finally:
            dialog.deleteLater()


def test_source_rejection_during_completion_clears_warning(tmp_path, monkeypatch):
    with sql_heatmap(tmp_path, monkeypatch, grid_cells=4, show=True) as (_window, plot):
        assert plot._heatmap_resolution_report is not None
        original = plot._complete_plot_publication
        rejected = []
        plot.database_replaced.disconnect()
        plot.database_replaced.connect(rejected.append)

        def reject(worker):
            original(worker)
            monkeypatch.setattr(plot, "_source_database_matches_key", lambda: False)

        monkeypatch.setattr(plot, "_complete_plot_publication", reject)
        assert plot.load_data(force_sql_heatmap=True)
        idle(plot)
        assert rejected == [plot._dataset_key.database_path]
        assert plot._heatmap_resolution_report is None
        assert plot.heatmap_downsample_button.isHidden()
