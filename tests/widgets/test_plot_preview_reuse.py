from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from qplot.windows._dataset_handle import DatasetKey
from qplot.windows._plot_actions import PlotActionsMixin
from qplot.windows._plot_refresh import PlotRefreshMixin
from qplot.windows._widgets.preview import (
    PreviewTab,
    render_displayed_plot_preview,
    render_heatmap_grid_preview,
    render_sparkline_preview,
)
from tests._plot_results import capture_fake_plot_result
from tests.support.plot_refresh import set_plot_worker


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


def test_swapped_heatmap_preview_keeps_acquisition_axes():
    field = SimpleNamespace(name="field")
    gate = SimpleNamespace(name="gate")
    current = SimpleNamespace(name="current", depends_on_=("field", "gate"))
    acquired = np.array([[2., 8., 11.], [17., 3., 4.]])
    displayed = SimpleNamespace(
        axis_data={"x": [5., 10.], "y": [-2., 0., 4.]},
        axis_param={"x": field, "y": gate},
        dataGrid=acquired.T,
    )
    preview = render_displayed_plot_preview(displayed, current, 64)
    assert preview["axes"] == ["field", "gate"]
    assert preview["image"] == render_heatmap_grid_preview(acquired, size=64)


@pytest.mark.parametrize("complex_mode", [None, "phase"])
def test_swapped_line_preview_uses_setpoint_horizontally(complex_mode):
    field = SimpleNamespace(name="field")
    signal = SimpleNamespace(name="signal", depends_on_=("field",))
    setpoints = np.array([-4., -3., 6., 8.])
    readings = np.array([2., 15., 4., -16.])
    displayed = SimpleNamespace(
        axis_data={"x": readings, "y": setpoints},
        axis_param={"x": signal, "y": field},
        complex_samples=SimpleNamespace(mode=complex_mode) if complex_mode else None,
    )
    preview = render_displayed_plot_preview(displayed, signal, 64)
    assert preview["axes"] == ["field"]
    assert preview["complex_mode"] == complex_mode
    assert preview["image"] == render_sparkline_preview(setpoints, readings, size=64)


def test_complex_plane_preview_keeps_real_and_imaginary_axes():
    signal = SimpleNamespace(name="signal", depends_on_=("field",))
    values = np.array([4+1j, 0-9j, 3+8j, -6+2j])
    displayed = SimpleNamespace(
        axis_data={"x": values.real, "y": values.imag},
        axis_param={"x": signal, "y": signal},
        complex_samples=SimpleNamespace(mode="imag_vs_real"),
    )
    preview = render_displayed_plot_preview(displayed, signal, 64)
    assert preview["complex_mode"] == "imag_vs_real"
    assert preview["image"] == render_sparkline_preview(values.real, values.imag, size=64)


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
    plot = PlotRefreshMixin()
    # The callback is installed on each plot by MainWindow.
    plot.__dict__["_plot_preview_sink"] = lambda _plot, _worker: published.append(True)
    worker = SimpleNamespace(
        dataset_completed=True,
        axis_data={"x": np.array([0, 1]), "y": np.array([2, 3])},
        axis_param={"x": object(), "y": object()},
        read_data=False,
        operations={},
        is_cancelled=lambda: False,
    )
    worker.result = capture_fake_plot_result(worker)
    set_plot_worker(plot, worker)
    plot._publication_owner().begin(worker, plot.__dict__, cache=None, parameter_name="signal")
    plot._commit_refresh_publication(worker, preview_ready=True)
    assert published == [True]
    assert plot._refresh_publication is None

    worker.operations = {"differentiate": object()}
    PlotRefreshMixin._commit_refresh_publication(plot, worker, preview_ready=True)
    assert published == [True]

    worker.operations = {}
    PlotRefreshMixin._commit_refresh_publication(plot, worker)
    assert published == [True]


@pytest.mark.parametrize("plot_mode", ["phase", "magnitude"])
def test_complex_plot_preview_reuse_requires_current_display_preference(plot_mode):
    published = []
    key = DatasetKey("unopened.db", "run")
    preview = SimpleNamespace(
        run_metadata={"run": {}}, preview_size=64, generation=1,
        database_instance=SimpleNamespace(logical_path=key.database_path,
                                         resolved_path=key.resolved_database_path,
                                         identity=key.database_identity),
        _trusted_derived_mode=False, _shutting_down=False,
        publish_plot_preview=lambda guid, image: published.append((guid, image)),
    )
    window = SimpleNamespace(infoBox=SimpleNamespace(preview=preview),
                             config={"user_preference.complex_1d": "phase"})
    plot = SimpleNamespace(_dataset_key=key, param=SimpleNamespace(name="S11", depends_on_=("frequency",)))
    result = SimpleNamespace(axis_data={"x": [0., 1.], "y": [3., 2.]}, dataset_completed=True,
                             complex_samples=SimpleNamespace(mode=plot_mode))
    with patch("qplot.windows._plot_actions.dataset_key_matches_current_source", return_value=True):
        PlotActionsMixin._publish_displayed_plot_preview(window, plot, result)
    if plot_mode == "phase":
        assert len(published) == 1
        assert published[0][1]["complex"]
        assert published[0][1]["complex_mode"] == "phase"
    else:
        assert not published


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
