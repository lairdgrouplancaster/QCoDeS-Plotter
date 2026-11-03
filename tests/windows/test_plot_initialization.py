"""Plot setup belongs to the owner, after all constructors have returned."""

from types import SimpleNamespace

import pytest
from PyQt6 import QtCore, sip

from qplot.configuration.config import config
from qplot.windows._dataset_handle import DatasetHandle, DatasetKey
from qplot.windows._plot_actions import PlotActionsMixin
from qplot.windows._plot_refresh import PlotRefreshMixin
from qplot.windows._subplots.subplot2d import sweeper
from qplot.windows.main import MainWindow
from qplot.windows.plot1d import plot1d
from qplot.windows.plot2d import plot2d
from tests._window_lifecycle import close_main_window
from tests.windows.test_plot_integration import (
    assert_database_artifacts_unchanged_except_trusted_shm,
    build_line_database,
    configure_temp_qplot,
    trusted_database_artifact_state,
    wait_for,
)


@pytest.fixture
def construction_inputs(tmp_path, monkeypatch):
    configure_temp_qplot(monkeypatch, tmp_path)
    key = DatasetKey(str(tmp_path / "unopened.db"), "guid")
    dataset = SimpleNamespace(guid=key.guid, run_id=1, number_of_results=0)
    parameter = SimpleNamespace(name="signal", label="Signal", depends_on_=("x",))
    return key, dataset, parameter, config(), QtCore.QThreadPool()


@pytest.mark.parametrize("window_type", [plot1d, plot2d, sweeper])
def test_constructor_defers_controls_and_reading_until_explicit_setup(
    construction_inputs, window_type, monkeypatch,
):
    key, dataset, parameter, settings, pool = construction_inputs
    holder = {key: DatasetHandle(dataset)}
    cut_args = (0, "x", "y", 0.0) if window_type is sweeper else ()
    entered = []

    def reject_axes(window):
        entered.append(window)
        # These fields used to be assigned only after base construction had
        # already called the feature hooks and submitted the initial read.
        if window_type is plot2d:
            assert window.sweep_lines == {}
            assert window.active_sweep_line_id is None
        if window_type is sweeper:
            assert window.sweep_indep == "x"
            assert window.fixed_indep == "y"
        raise RuntimeError("axes setup failed")

    monkeypatch.setattr(window_type, "initAxes", reject_axes)
    window = window_type(
        key, *cut_args, parameter, settings, pool, holder, show=False,
    )
    try:
        assert entered == []
        assert not window._plot_setup_started
        assert not window._plot_setup_complete
        assert "worker" not in window.__dict__
        assert "widget" not in window.__dict__
        assert not window.monitor.isActive()
        assert pool.activeThreadCount() == 0
        window.refreshWindow(force=True)
        assert "worker" not in window.__dict__

        with pytest.raises(RuntimeError, match="axes setup failed"):
            window.initialize_plot_window()
        assert entered == [window]
        assert not window._plot_setup_complete
        with pytest.raises(RuntimeError, match="already started"):
            window.initialize_plot_window()

        # Even failure before the overlay/refresh toolbar exists is closeable.
        assert window.close()
        assert window._closed
        assert not window.monitor.isActive()
    finally:
        sip.delete(window)


def test_owner_does_not_publish_a_failed_setup_or_steal_dataset_ownership(
    construction_inputs, monkeypatch,
):
    key, dataset, parameter, settings, pool = construction_inputs
    delete_timer = QtCore.QTimer()
    delete_timer.start(60_000)
    handle = DatasetHandle(dataset, users=0, delete_timer=delete_timer)

    class Owner(PlotActionsMixin):
        def __init__(self):
            self.config = settings
            self.threadPool = pool
            self.dataset_holder = {key: handle}
            self.windows = []

    entered = []

    def reject_axes(window):
        entered.append(window)
        assert window.sweep_lines == {}
        raise RuntimeError("axes setup failed")

    monkeypatch.setattr(plot2d, "initAxes", reject_axes)
    owner = Owner()
    try:
        with pytest.raises(RuntimeError, match="axes setup failed"):
            owner.openWin(plot2d, key, parameter, show=False)
        assert len(entered) == 1
        assert entered[0]._closed
        assert not entered[0].monitor.isActive()
        assert owner.windows == []
        assert owner.dataset_holder == {key: handle}
        assert handle.users == 0
        assert handle.delete_timer is delete_timer
        assert delete_timer.isActive()
    finally:
        delete_timer.stop()
        for window in entered:
            sip.delete(window)


@pytest.fixture(params=[False, True], ids=["hidden", "visible"])
def ready_plot(tmp_path, monkeypatch, request):
    configure_temp_qplot(monkeypatch, tmp_path)
    path = tmp_path / "setup.db"
    _, guid, _ = build_line_database(path, 5)
    protected = trusted_database_artifact_state(path)
    reads = []
    original_load = PlotRefreshMixin.load_data

    def load_after_controls(window, *args, **kwargs):
        assert window._plot_setup_complete
        assert window.centralWidget() is not None
        if window.visible:
            assert window.pos_labels
            assert window.vbMenu is not None
            assert window.snap_to_trace_action is not None
            assert window.appearance_action is not None
        reads.append(window)
        return original_load(window, *args, **kwargs)

    monkeypatch.setattr(PlotRefreshMixin, "load_data", load_after_controls)
    owner = MainWindow()
    try:
        owner.startupDatabaseTimer.stop()
        owner.monitor.stop()
        owner.config.config["user_preference"]["confirm_close"] = False
        owner.config.config["user_preference"]["confirm_close_all"] = False
        owner.close_database(status=False)
        assert owner.load_file(str(path))
        wait_for(lambda: not owner._database_load_active)
        owner.openPlot(guid=guid, show=request.param)
        wait_for(lambda: len(owner.windows) == 1)
        source = owner.windows[0]
        wait_for(lambda: "axis_data" in source.__dict__ and not source.worker.running)
        source.monitor.stop()
        assert reads and reads[0] is source
        yield owner, source, reads
    finally:
        close_main_window(owner)
        assert_database_artifacts_unchanged_except_trusted_shm(path, protected)


def test_successful_setup_cannot_be_repeated_or_replace_renderers(ready_plot):
    _, source, reads = ready_plot
    original_line, original_worker, original_canvas = source.line, source.worker, source.widget
    prior_reads = len(reads)
    with pytest.raises(RuntimeError, match="already started"):
        source.initialize_plot_window()
    assert source.line is original_line
    assert source.worker is original_worker
    assert source.widget is original_canvas
    assert len(reads) == prior_reads


@pytest.mark.parametrize("phase", ["initAxes", "initLabels"])
def test_failed_controls_submit_no_read_and_keep_existing_source_usable(
    ready_plot, monkeypatch, phase,
):
    owner, source, reads = ready_plot
    original_windows = list(owner.windows)
    handle = owner.dataset_holder[source._dataset_key]
    prior_users = handle.users
    prior_reads = len(reads)
    failed = []

    def reject_controls(window):
        failed.append(window)
        raise RuntimeError("controls failed")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(plot1d, phase, reject_controls)
            with pytest.raises(RuntimeError, match="controls failed"):
                owner.openWin(plot1d, source._dataset_key, source.param, show=True)
        assert len(failed) == 1
        assert failed[0]._closed
        assert not failed[0]._plot_setup_complete
        assert "worker" not in failed[0].__dict__
        assert not failed[0].monitor.isActive()
        assert owner.windows == original_windows
        assert owner.dataset_holder[source._dataset_key] is handle
        assert handle.users == prior_users
        assert len(reads) == prior_reads

        source.refreshWindow(force=True)
        wait_for(lambda: not source.worker.running)
        assert reads[-1] is source
        assert source.axis_data["x"].size == 5
    finally:
        for window in failed:
            sip.delete(window)
