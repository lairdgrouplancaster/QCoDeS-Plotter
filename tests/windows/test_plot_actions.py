"""Focused tests for selected-run plot actions."""

import time
from pathlib import Path

import pytest
import qcodes
from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.parameters import ManualParameter

from qplot.windows import main as main_window
from qplot.windows._plot_actions import PlotActionsMixin


class _Field:
    def __init__(self, value):
        self.value = str(value)

    def text(self):
        return self.value


class _ActionHarness(PlotActionsMixin):
    def __init__(self, database_path, dataset):
        self.fileTextbox = _Field(database_path)
        self.ds = dataset
        self._selected_dataset_key = self._current_dataset_key(dataset.guid)
        self.dataset_holder = {}
        self.plot_calls = []
        self.export_calls = []
        self.load_calls = []
        self.status_messages = []

    def openPlot(self, *args, **kwargs):
        self.plot_calls.append((args, kwargs))

    def _export_preview_csv(self, *args):
        self.export_calls.append(args)

    def _load_dataset(self, dataset_key):
        self.load_calls.append(dataset_key)
        raise AssertionError("A matching selected dataset must not be reloaded")

    def show_status(self, *args):
        self.status_messages.append(args)


def _assert_empty_dataset_actions(database_path, dataset):
    assert dataset.number_of_results == 0
    assert not dataset
    parameter = next(
        param for param in dataset.get_parameters() if param.name == "y"
    )
    harness = _ActionHarness(database_path, dataset)

    # Ctrl+1, selected-preview double-click, and run-row preview double-click
    # all enumerate the registered dependent parameter despite the empty run.
    harness.open_param_by_index(0)
    harness.open_preview_plot("y")
    harness.open_run_preview_plot(dataset.guid, "y")

    assert [call[1]["params"] for call in harness.plot_calls] == [
        [parameter],
        [parameter],
        [parameter],
    ]
    assert harness.load_calls == []

    # Selected-preview export reaches its implementation rather than treating
    # an empty dataset as no selection.
    harness.export_preview_csv("y")
    assert harness.export_calls == [(harness._selected_dataset_key, "y")]


@pytest.mark.parametrize("completed", (False, True), ids=("active", "completed"))
def test_empty_qcodes_dataset_actions_keep_the_selected_run(completed, tmp_path):
    """Registered parameters remain actionable before and after empty completion."""

    original_database_path = qcodes.config.core.db_location
    try:
        database_path = Path(tmp_path) / f"empty-{completed}.db"
        initialise_or_create_database_at(str(database_path))
        experiment = load_or_create_experiment("empty_actions", sample_name="sample")
        x = ManualParameter("x")
        y = ManualParameter("y")
        measurement = Measurement(exp=experiment, name="empty_actions")
        measurement.register_parameter(x)
        measurement.register_parameter(y, setpoints=(x,))

        with measurement.run(write_in_background=False) as datasaver:
            if not completed:
                _assert_empty_dataset_actions(database_path, datasaver.dataset)

        if completed:
            _assert_empty_dataset_actions(database_path, datasaver.dataset)
    finally:
        qcodes.config.core.db_location = original_database_path


def test_empty_qcodes_dataset_opens_the_waiting_plot_state(tmp_path, monkeypatch):
    """Opening a registered empty y(x) reaches the plot's waiting state."""

    original_database_path = qcodes.config.core.db_location
    window = None
    try:
        database_path = Path(tmp_path) / "empty-plot.db"
        initialise_or_create_database_at(str(database_path))
        experiment = load_or_create_experiment("empty_plot", sample_name="sample")
        x = ManualParameter("x")
        y = ManualParameter("y")
        measurement = Measurement(exp=experiment, name="empty_plot")
        measurement.register_parameter(x)
        measurement.register_parameter(y, setpoints=(x,))
        with measurement.run(write_in_background=False) as datasaver:
            dataset = datasaver.dataset
        assert not dataset

        qplot_home = tmp_path / ".qplot"
        monkeypatch.setattr(main_window.config, "default_path", str(qplot_home))
        monkeypatch.setattr(
            main_window.config,
            "default_file",
            str(qplot_home / main_window.config.config_file_name),
        )
        window = main_window.MainWindow()
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.fileTextbox.setText(str(database_path))
        window.ds = dataset
        window._selected_dataset_key = window._current_dataset_key(dataset.guid)

        window.open_param_by_index(0)
        assert len(window.windows) == 1
        plot = window.windows[0]
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline and getattr(plot.worker, "running", False):
            qtw.QApplication.processEvents()
            time.sleep(0.03)
        assert not plot.worker.running
        assert plot.plot_state_overlay.title_label.text() == "Waiting for plottable data"
    finally:
        if window is not None:
            window.close_plot_windows(confirm=False, status=False)
            window.threadPool.waitForDone(1000)
            window.hide()
            window.deleteLater()
            qtw.QApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
            qtw.QApplication.processEvents()
        qcodes.config.core.db_location = original_database_path


def test_dataset_presence_guards_do_not_evaluate_dataset_truthiness(tmp_path):
    class Parameter:
        name = "y"
        depends_on = "x"

    class RaisingDataset:
        guid = "raising-dataset"

        def __bool__(self):
            raise AssertionError("Dataset presence must not evaluate __bool__")

        def __len__(self):
            raise AssertionError("Dataset presence must not evaluate __len__")

        def get_parameters(self):
            return [Parameter()]

    harness = _ActionHarness(tmp_path / "presence.db", RaisingDataset())

    harness.open_param_by_index(0)
    harness.open_preview_plot("y")
    harness.open_run_preview_plot("raising-dataset", "y")
    harness.export_preview_csv("y")

    assert len(harness.plot_calls) == 3
    assert harness.load_calls == []
    assert harness.export_calls == [(harness._selected_dataset_key, "y")]


def test_selected_action_guards_keep_none_behavior(tmp_path):
    harness = _ActionHarness.__new__(_ActionHarness)
    harness.fileTextbox = _Field(tmp_path / "none.db")
    harness.ds = None
    harness._selected_dataset_key = None
    harness.dataset_holder = {}
    harness.plot_calls = []
    harness.export_calls = []
    harness.load_calls = []
    harness.status_messages = []

    harness.open_param_by_index(0)
    harness.open_preview_plot("y")
    harness.export_preview_csv("y")

    assert harness.plot_calls == []
    assert harness.export_calls == []
    assert len(harness.status_messages) == 3


def test_run_preview_loads_its_requested_dataset_without_a_selection(tmp_path):
    class Parameter:
        name = "y"
        depends_on = "x"

    class Dataset:
        guid = "run-preview-guid"

        def __init__(self):
            self.parameter = Parameter()

        def get_parameters(self):
            return [self.parameter]

    dataset = Dataset()
    harness = _ActionHarness.__new__(_ActionHarness)
    harness.fileTextbox = _Field(tmp_path / "run-preview.db")
    harness.ds = None
    harness._selected_dataset_key = None
    harness.dataset_holder = {}
    harness.plot_calls = []
    harness.export_calls = []
    harness.load_calls = []
    harness.status_messages = []
    harness._load_dataset = lambda dataset_key: (  # type: ignore[method-assign]
        harness.load_calls.append(dataset_key) or dataset
    )

    harness.open_run_preview_plot(dataset.guid, "y")

    assert len(harness.load_calls) == 1
    assert harness.ds is dataset
    assert harness.plot_calls[0][1]["params"] == [dataset.parameter]
