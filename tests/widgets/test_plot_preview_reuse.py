from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from qplot.windows._dataset_handle import DatasetKey
from qplot.windows._plot_actions import PlotActionsMixin
from qplot.windows._plot_refresh import PlotRefreshMixin
from qplot.windows._widgets.preview import (
    PreviewTab,
    render_displayed_plot_preview,
)


def _preview(parameter):
    return render_displayed_plot_preview(
        SimpleNamespace(axis_data={"x": [0, 1], "y": [2, 3]}),
        SimpleNamespace(name=parameter, depends_on_=("x",)),
        64,
    )


def test_completed_plot_preview_uses_loaded_line_and_grid():
    line = _preview("signal")
    assert line["parameter"] == "signal"
    assert line["axes"] == ["x"]
    assert line["image"].size().width() == 64

    grid = render_displayed_plot_preview(
        SimpleNamespace(
            axis_data={"x": [0, 1], "y": [0, 1]},
            dataGrid=np.array([[1.0, 2.0], [3.0, np.nan]]),
        ),
        SimpleNamespace(name="heat", depends_on_=("y", "x")),
        64,
    )
    assert grid["parameter"] == "heat"
    assert grid["axes"] == ["y", "x"]
    assert grid["image"].size().width() == 64


def test_partial_plot_previews_fill_selected_run_then_yield_to_full_result(qapplication):
    tab = PreviewTab(preview_size=64)
    tab.set_trusted_derived_runs({1: {"guid": "run", "measure_parameters": ["a", "b"]}})
    tab.set_current_guid("run")
    ready = []
    tab.previewsReady.connect(lambda guid, previews: ready.append((guid, previews)))
    try:
        tab.publish_plot_preview("run", _preview("b"))
        assert [item["parameter"] for item in ready[-1][1]] == ["b"]
        assert "run" in tab._plot_previews

        tab.publish_plot_preview("run", _preview("a"))
        assert {item["parameter"] for item in ready[-1][1]} == {"a", "b"}

        tab.publish_trusted_previews("run", [_preview("a"), _preview("b")])
        assert "run" not in tab._plot_previews
        assert len(tab.cache["run"]) == 2
    finally:
        tab.shutdown()
        tab.deleteLater()


def test_completed_plot_publishes_only_unmodified_committed_data():
    published = []
    plot = SimpleNamespace(
        _source_database_matches_key=lambda: True,
    )
    # The callback is installed on each plot by MainWindow.
    plot.__dict__["_plot_preview_sink"] = lambda _plot, _worker: published.append(True)
    worker = SimpleNamespace(
        _qplot_publication_snapshot={"generation": 1},
        dataset_completed=True,
        operations={},
        is_cancelled=lambda: False,
    )
    PlotRefreshMixin._commit_refresh_publication(plot, worker, preview_ready=True)
    assert published == [True]
    assert worker._qplot_publication_snapshot is None

    worker.operations = {"differentiate": object()}
    PlotRefreshMixin._commit_refresh_publication(plot, worker, preview_ready=True)
    assert published == [True]

    worker.operations = {}
    PlotRefreshMixin._commit_refresh_publication(plot, worker)
    assert published == [True]


def test_plot_preview_rechecks_source_before_publishing():
    published = []
    key = DatasetKey("unopened.db", "run")
    preview = SimpleNamespace(
        run_metadata={"run": {}},
        preview_size=64,
        generation=1,
        database_instance=SimpleNamespace(
            logical_path=key.database_path,
            resolved_path=key.resolved_database_path,
            identity=key.database_identity,
        ),
        _trusted_derived_mode=False,
        _shutting_down=False,
        publish_plot_preview=lambda guid, image: published.append((guid, image)),
    )
    window = SimpleNamespace(infoBox=SimpleNamespace(preview=preview))
    plot = SimpleNamespace(
        _dataset_key=key,
        param=SimpleNamespace(name="signal", depends_on_=("x",)),
    )
    worker = SimpleNamespace(
        axis_data={"x": [0, 1], "y": [2, 3]},
        dataset_completed=True,
    )
    with (
        patch(
            "qplot.windows._plot_actions.dataset_key_matches_current_source",
            side_effect=[True, False],
        ),
    ):
        PlotActionsMixin._publish_displayed_plot_preview(window, plot, worker)
    assert published == []

    with patch(
        "qplot.windows._plot_actions.dataset_key_matches_current_source",
        return_value=True,
    ):
        PlotActionsMixin._publish_displayed_plot_preview(window, plot, worker)
    assert published[0][0] == "run"
    assert published[0][1]["parameter"] == "signal"

    worker.dataset_completed = False
    worker.dataset_length_at_start = 2
    preview.run_metadata["run"]["result_count"] = 1
    with patch(
        "qplot.windows._plot_actions.dataset_key_matches_current_source",
        return_value=True,
    ):
        PlotActionsMixin._publish_displayed_plot_preview(window, plot, worker)
    assert len(published) == 1

    preview.run_metadata["run"]["result_count"] = 2
    with patch(
        "qplot.windows._plot_actions.dataset_key_matches_current_source",
        return_value=True,
    ):
        PlotActionsMixin._publish_displayed_plot_preview(window, plot, worker)
    assert len(published) == 2
