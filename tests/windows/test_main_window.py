import errno
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import ANY, patch

from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw
from qcodes.dataset.sqlite.database import get_DB_location

from qplot.datahandling import database as database_module
from qplot.datahandling.readonly import set_qcodes_database_location
from qplot.windows import _database_actions as database_actions
from qplot.windows import main as main_window
from qplot.windows._dataset_handle import DatasetHandle, DatasetKey, TraceKey
from qplot.windows._plot_actions import PlotActionsMixin
from qplot.windows._plotWin import plotWidget
from qplot.windows._run_controls import AUTO_PLOT_KEY
from qplot.windows._window_controls import (
    CONFIRM_CLOSE_ALL_KEY,
    CONFIRM_QUIT_KEY,
    DO_NOT_ASK_AGAIN_LABEL,
    add_confirmation_options,
    add_restore_defaults_option,
    ask_confirmation_with_dont_ask_again,
)
from qplot.windows.plot1d import plot1d


class MeasurementExportDataFrameTestCase(unittest.TestCase):
    def test_measurement_dataframe_flattens_and_prefixes_multiple_parameters(self):
        class Param:
            def __init__(self, name):
                self.name = name

        class Dataset:
            def __init__(self):
                self.data = {
                    "signal": {
                        "x": [[0.0, 1.0], [0.0, 1.0]],
                        "signal": [[10.0, 11.0], [12.0, 13.0]],
                        },
                    "current": {
                        "gate": [0.0, 1.0, 2.0, 3.0],
                        "current": [20.0, 21.0, 22.0, 23.0],
                        },
                    }

            def get_parameter_data(self, name):
                return {name: self.data[name]}

        frame = PlotActionsMixin._measurement_dataframe(
            object(),
            Dataset(),
            [Param("signal"), Param("current")],
            )

        self.assertEqual(
            list(frame.columns),
            ["signal.x", "signal.signal", "current.gate", "current.current"],
            )
        self.assertEqual(frame["signal.signal"].tolist(), [10.0, 11.0, 12.0, 13.0])
        self.assertEqual(frame["current.current"].tolist(), [20.0, 21.0, 22.0, 23.0])

    def test_default_export_filename_uses_database_folder_and_safe_measurement_name(self):
        class Field:
            def text(self):
                return str(Path("C:/data/source.db"))

        class Host(PlotActionsMixin):
            fileTextbox = Field()

        class Dataset:
            run_id = 7

        class Param:
            name = "gate/current"

        filename = Host()._default_export_filename(Dataset(), [Param()])

        self.assertEqual(Path(filename).name, "run_7_gate_current.csv")
        self.assertEqual(Path(filename).parent, Path("C:/data"))


class DatasetHandleTestCase(unittest.TestCase):
    def test_close_is_idempotent_and_closes_backing_connection(self):
        class Connection:
            def __init__(self):
                self.close_count = 0

            def close(self):
                self.close_count += 1

        dataset = type("Dataset", (), {"conn": Connection()})()
        handle = DatasetHandle(dataset)

        self.assertTrue(handle.close())
        self.assertFalse(handle.close())
        self.assertEqual(dataset.conn.close_count, 1)

    def test_retain_cancels_pending_delete_timer(self):
        class Timer:
            def __init__(self):
                self.stopped = False

            def stop(self):
                self.stopped = True

        timer = Timer()
        handle = DatasetHandle(object(), delete_timer=timer)

        handle.retain()

        self.assertEqual(handle.users, 2)
        self.assertTrue(timer.stopped)
        self.assertIsNone(handle.delete_timer)

    def test_plot_actions_manage_dataset_handles(self):
        class Config:
            def get(self, key):
                if key == "runtime_settings.del_grace_period":
                    return 0
                raise KeyError(key)

        class Dataset:
            guid = "guid"

        class Harness(PlotActionsMixin):
            def __init__(self):
                self.dataset_holder = {}
                self.config = Config()
                self.status_messages = []

            def show_status(self, message, timeout=5000):
                self.status_messages.append((message, timeout))

        harness = Harness()
        dataset = Dataset()
        dataset_key = DatasetKey("database.db", "guid")

        harness.add_ds_at(dataset_key, dataset)
        self.assertIs(harness.dataset_holder[dataset_key].dataset, dataset)
        self.assertEqual(harness.dataset_holder[dataset_key].users, 1)

        harness.add_ds_at(dataset_key, dataset)
        self.assertEqual(harness.dataset_holder[dataset_key].users, 2)

        harness.remove_ds_at(dataset_key)
        self.assertEqual(harness.dataset_holder[dataset_key].users, 1)

        harness.remove_ds_at(dataset_key)
        self.assertEqual(harness.dataset_holder, {})

    def test_evicting_unselected_handle_closes_connection(self):
        class Config:
            def get(self, key):
                self.assert_key = key
                return 0

        class Connection:
            closed = False

            def close(self):
                self.closed = True

        dataset = type("Dataset", (), {"guid": "guid", "conn": Connection()})()
        harness = type("Harness", (PlotActionsMixin,), {})()
        harness.config = Config()
        harness.dataset_holder = {}
        harness.ds = None
        harness.show_status = lambda *args: None
        key = DatasetKey("database.db", "guid")
        harness.dataset_holder[key] = DatasetHandle(dataset)

        harness.remove_ds_at(key)

        self.assertTrue(dataset.conn.closed)
        self.assertEqual(harness.dataset_holder, {})

    def test_replacing_selected_dataset_closes_only_unheld_previous_dataset(self):
        class Connection:
            closed = False

            def close(self):
                self.closed = True

        old_dataset = type("Dataset", (), {"conn": Connection()})()
        new_dataset = type("Dataset", (), {"conn": Connection()})()
        harness = type("Harness", (PlotActionsMixin,), {})()
        harness.ds = old_dataset
        harness.dataset_holder = {}

        harness._replace_selected_dataset(
            new_dataset,
            DatasetKey("database.db", "new-guid"),
            )

        self.assertTrue(old_dataset.conn.closed)
        self.assertFalse(new_dataset.conn.closed)

    def test_failed_plot_construction_preserves_existing_dataset_ownership(self):
        class Connection:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class Timer:
            def __init__(self):
                self.stopped = False

            def stop(self):
                self.stopped = True

        class Dataset:
            guid = "guid"

            def __init__(self):
                self.conn = Connection()

        class Harness(PlotActionsMixin):
            def __init__(self, dataset_key, handle):
                self.config = object()
                self.dataset_holder = {dataset_key: handle}
                self.threadPool = object()
                self.windows = []

        def failing_widget(*args, **kwargs):
            construction_handle = args[-1][dataset_key]
            construction_handle.cancel_delete_timer()
            raise RuntimeError("plot construction failed")

        timer = Timer()
        dataset = Dataset()
        dataset_key = DatasetKey("database.db", "guid")
        handle = DatasetHandle(dataset, users=0, delete_timer=timer)
        harness = Harness(dataset_key, handle)

        with self.assertRaisesRegex(RuntimeError, "plot construction failed"):
            harness.openWin(failing_widget, dataset_key, show=False)

        self.assertEqual(harness.dataset_holder, {dataset_key: handle})
        self.assertIs(harness.dataset_holder[dataset_key].dataset.conn, dataset.conn)
        self.assertEqual(handle.users, 0)
        self.assertIs(handle.delete_timer, timer)
        self.assertFalse(timer.stopped)
        self.assertFalse(dataset.conn.closed)
        self.assertEqual(harness.windows, [])


class OpenPlotDatasetOwnershipTestCase(unittest.TestCase):
    class Connection:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class Dataset:
        guid = "guid"

        def __init__(self, params=()):
            self.conn = OpenPlotDatasetOwnershipTestCase.Connection()
            self.params = list(params)

        def get_parameters(self):
            return self.params

    class Field:
        def text(self):
            return "database.db"

    class SpinBox:
        def value(self):
            return 1.0

    class Param:
        def __init__(self, name, depends_on="x"):
            self.name = name
            self.depends_on = depends_on
            self.depends_on_ = ("x",) if depends_on else ()

    class Harness(PlotActionsMixin):
        def __init__(self, dataset, selected=False):
            self.fileTextbox = OpenPlotDatasetOwnershipTestCase.Field()
            self.spinBox = OpenPlotDatasetOwnershipTestCase.SpinBox()
            self.dataset_holder = {}
            self.windows = []
            self.ds = dataset if selected else None
            self._selected_dataset_key = (
                self._current_dataset_key(dataset.guid) if selected else None
            )
            self.loaded_dataset = dataset
            self.load_count = 0
            self.open_win = lambda *args, **kwargs: None
            self.errors = []
            self.status_messages = []

        def _load_dataset(self, dataset_key):
            self.load_count += 1
            return self.loaded_dataset

        def openWin(self, *args, **kwargs):
            return self.open_win(*args, **kwargs)

        def post_admin(self):
            pass

        def show_error(self, *args):
            self.errors.append(args)

        def show_status(self, *args):
            self.status_messages.append(args)

    @staticmethod
    def fail_plot_open(*args, **kwargs):
        raise RuntimeError("plot construction failed")

    def test_failure_does_not_close_selected_dataset(self):
        param = self.Param("signal")
        dataset = self.Dataset([param])
        harness = self.Harness(dataset, selected=True)
        harness.open_win = self.fail_plot_open

        harness.openPlot(params=[param], show=False)

        self.assertFalse(dataset.conn.closed)
        self.assertEqual(harness.load_count, 0)
        self.assertEqual(len(harness.errors), 1)

    def test_failure_does_not_close_cached_dataset_handle(self):
        param = self.Param("signal")
        dataset = self.Dataset([param])
        harness = self.Harness(dataset)
        dataset_key = harness._current_dataset_key(dataset.guid)
        handle = DatasetHandle(dataset)
        harness.dataset_holder[dataset_key] = handle
        harness.open_win = self.fail_plot_open

        harness.openPlot(dataset_key, params=[param], show=False)

        self.assertFalse(dataset.conn.closed)
        self.assertIs(harness.dataset_holder[dataset_key], handle)
        self.assertEqual(harness.load_count, 0)
        self.assertEqual(len(harness.errors), 1)

    def test_partial_success_keeps_published_dataset_connection_open(self):
        params = [self.Param("first"), self.Param("second")]
        dataset = self.Dataset(params)
        harness = self.Harness(dataset)
        dataset_key = harness._current_dataset_key(dataset.guid)
        opened_window = type(
            "OpenedWindow",
            (),
            {"_dataset_key": dataset_key, "ds": dataset, "param": params[0]},
        )()

        def open_then_fail(widget, opened_dataset, param, **kwargs):
            if param is params[0]:
                harness.dataset_holder[dataset_key] = DatasetHandle(opened_dataset)
                harness.windows.append(opened_window)
                return
            raise RuntimeError("plot construction failed")

        harness.open_win = open_then_fail

        harness.openPlot(dataset_key, params=params, show=False)

        self.assertFalse(dataset.conn.closed)
        self.assertIs(harness.dataset_holder[dataset_key].dataset, dataset)
        self.assertEqual(harness.windows, [opened_window])
        self.assertIs(opened_window.ds, dataset)
        self.assertEqual(len(harness.errors), 1)

    def test_failure_before_ownership_is_published_closes_transient_dataset(self):
        param = self.Param("signal")
        dataset = self.Dataset([param])
        harness = self.Harness(dataset)
        dataset_key = harness._current_dataset_key(dataset.guid)
        harness.open_win = self.fail_plot_open

        harness.openPlot(dataset_key, params=[param], show=False)

        self.assertTrue(dataset.conn.closed)
        self.assertNotIn(dataset_key, harness.dataset_holder)
        self.assertEqual(harness.load_count, 1)
        self.assertEqual(len(harness.errors), 1)

    def test_transient_dataset_without_plottable_parameters_is_closed(self):
        independent = self.Param("setpoint", depends_on="")
        dataset = self.Dataset([independent])
        harness = self.Harness(dataset)
        dataset_key = harness._current_dataset_key(dataset.guid)

        harness.openPlot(dataset_key, show=False)

        self.assertTrue(dataset.conn.closed)
        self.assertNotIn(dataset_key, harness.dataset_holder)
        self.assertEqual(harness.load_count, 1)
        self.assertEqual(harness.errors, [])

    def test_three_dimensional_parameter_is_rejected_before_window_creation(self):
        param = self.Param("volume")
        param.depends_on = "x, y, z"
        param.depends_on_ = ("x", "y", "z")
        dataset = self.Dataset([param])
        harness = self.Harness(dataset)
        harness.open_win = lambda *args, **kwargs: self.fail(
            "Unsupported parameter must not create a plot window"
            )

        harness.openPlot(harness._current_dataset_key(dataset.guid), show=False)

        self.assertTrue(dataset.conn.closed)
        self.assertEqual(harness.windows, [])
        self.assertIn("3 independent axes", harness.status_messages[-1][0])


class DatabaseAwareDatasetCacheTestCase(unittest.TestCase):
    class Field:
        def __init__(self, value):
            self.value = value

        def text(self):
            return self.value

        def setText(self, value):
            self.value = value

        def blockSignals(self, _blocked):
            return False

    class Config:
        def get(self, key):
            if key == "runtime_settings.del_grace_period":
                return 0
            raise KeyError(key)

    class Dataset:
        guid = "shared-guid"

        def __init__(self, database_name, run_id=1):
            self.database_name = database_name
            self.run_id = run_id
            self.metadata = {}
            self.snapshot = None

        def get_parameters(self):
            return []

    class Param:
        name = "signal"

    def test_cut_compatibility_changes_refresh_add_to_plot_candidates(self):
        class Signal:
            def __init__(self):
                self.slots = []

            def connect(self, slot):
                self.slots.append(slot)

            def emit(self, *args):
                for slot in list(self.slots):
                    slot(*args)

        class sweeper:
            def __init__(self, dataset_key, *_args, **_kwargs):
                holder = _args[-1]
                self._dataset_key = dataset_key
                self.ds = holder[dataset_key].dataset
                self.param = object()
                self.closed = Signal()
                self.make_ds = Signal()
                self.previewTraceDropRequested = Signal()
                self.merge_compatibility_changed = Signal()
                self.sweep_moved = Signal()
                self.remove_sweep = Signal()

        class Harness(PlotActionsMixin):
            def __init__(self):
                self.config = DatabaseAwareDatasetCacheTestCase.Config()
                self.threadPool = object()
                self.dataset_holder = {}
                self.windows = []
                self.fileTextbox = DatabaseAwareDatasetCacheTestCase.Field("database.db")
                self.admin_calls = 0

            def post_admin(self):
                self.admin_calls += 1

        harness = Harness()
        dataset = self.Dataset("database.db")
        dataset_key = DatasetKey("database.db", dataset.guid)

        harness.openWin(sweeper, dataset, show=False, dataset_key=dataset_key)
        cut = harness.windows[0]

        self.assertEqual(harness.admin_calls, 1)
        cut.merge_compatibility_changed.emit()
        self.assertEqual(harness.admin_calls, 2)

    def test_database_path_aliases_produce_the_same_key(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "database.db"
            database_path.touch()

            direct = DatasetKey(str(database_path), "shared-guid")
            aliased = DatasetKey(
                str(database_path.parent / "." / database_path.name),
                "shared-guid",
            )

        self.assertEqual(direct, aliased)

    def test_same_guid_in_two_databases_creates_distinct_cache_entries(self):
        harness = type("Harness", (PlotActionsMixin,), {})()
        harness.dataset_holder = {}
        harness.config = self.Config()
        key_a = DatasetKey("database-a.db", "shared-guid")
        key_b = DatasetKey("database-b.db", "shared-guid")
        dataset_a = self.Dataset("A")
        dataset_b = self.Dataset("B")

        harness.add_ds_at(key_a, dataset_a)
        harness.add_ds_at(key_b, dataset_b)

        self.assertEqual(len(harness.dataset_holder), 2)
        self.assertIs(harness.dataset_holder[key_a].dataset, dataset_a)
        self.assertIs(harness.dataset_holder[key_b].dataset, dataset_b)

    def test_current_database_selection_cannot_return_other_database_dataset(self):
        harness = type("Harness", (PlotActionsMixin,), {})()
        harness.fileTextbox = self.Field("database-b.db")
        harness.run_idBox = self.Field("")
        harness.infoBox = type("InfoBox", (), {"setInfo": lambda *args, **kwargs: None})()
        harness.ds = None
        harness.selected_run_id = None
        harness.show_status = lambda *args, **kwargs: None
        harness.show_error = lambda *args, **kwargs: self.fail("selection load failed")
        key_a = DatasetKey("database-a.db", "shared-guid")
        dataset_a = self.Dataset("A")
        dataset_b = self.Dataset("B")
        harness.dataset_holder = {key_a: DatasetHandle(dataset_a)}

        with patch(
            "qplot.windows._plot_actions.load_by_guid_read_only",
            return_value=dataset_b,
        ) as load_dataset:
            harness.updateSelected("shared-guid")

        self.assertIs(harness.ds, dataset_b)
        load_dataset.assert_called_once_with(
            "shared-guid",
            DatasetKey("database-b.db", "x").database_path,
        )

    def test_same_guid_and_parameter_plots_in_different_databases_coexist(self):
        class PlotWindow:
            pass

        class SpinBox:
            def value(self):
                return 1.0

        key_a = DatasetKey("database-a.db", "shared-guid")
        key_b = DatasetKey("database-b.db", "shared-guid")
        param = self.Param()
        param.depends_on = "x"
        param.depends_on_ = ("x",)
        dataset_b = self.Dataset("B")
        window_a = PlotWindow()
        window_a._dataset_key = key_a
        window_a.param = param

        harness = type("Harness", (PlotActionsMixin,), {})()
        harness.fileTextbox = self.Field("database-b.db")
        harness.dataset_holder = {key_b: DatasetHandle(dataset_b)}
        harness.ds = None
        harness._selected_dataset_key = None
        harness.windows = [window_a]
        harness.spinBox = SpinBox()
        harness.status_messages = []
        harness.show_status = lambda *args: harness.status_messages.append(args)
        harness.show_error = lambda *args: self.fail("plot load failed")
        harness.post_admin = lambda: None
        opened = []
        harness.openWin = lambda *args, **kwargs: opened.append((args, kwargs))

        with patch("qplot.windows._plot_actions.plot1d", PlotWindow):
            harness.openPlot("shared-guid", params=[param], show=False)

        self.assertEqual(len(opened), 1)
        self.assertIs(opened[0][0][1], dataset_b)

    def test_open_plot_preserves_explicit_noncurrent_database_key(self):
        class Signal:
            def __init__(self):
                self.slots = []

            def connect(self, slot):
                self.slots.append(slot)

        class SpinBox:
            def value(self):
                return 1.0

        class plot1d:
            def __init__(
                    self,
                    dataset_key,
                    param,
                    _config,
                    _thread_pool,
                    dataset_holder,
                    **_kwargs,
                    ):
                self._dataset_key = dataset_key
                self.param = param
                self.dataset = dataset_holder[dataset_key].dataset
                self.closed = Signal()
                self.make_ds = Signal()
                self.previewTraceDropRequested = Signal()
                self.get_mergables = Signal()
                self.remove_dataset = Signal()

        key_a = DatasetKey("database-a.db", "shared-guid")
        key_b = DatasetKey("database-b.db", "shared-guid")
        dataset_a = self.Dataset("A")
        dataset_b = self.Dataset("B")
        param = self.Param()
        param.depends_on = "x"
        param.depends_on_ = ("x",)

        harness = type("Harness", (PlotActionsMixin,), {})()
        harness.fileTextbox = self.Field(key_b.database_path)
        harness.spinBox = SpinBox()
        harness.config = object()
        harness.threadPool = object()
        harness.dataset_holder = {key_b: DatasetHandle(dataset_b)}
        harness.windows = []
        harness.ds = None
        harness._selected_dataset_key = None
        harness._load_dataset = lambda key: (
            dataset_a
            if key == key_a
            else self.fail(f"unexpected dataset load: {key}")
        )
        harness.post_admin = lambda: None
        harness.show_status = lambda *_args: None
        harness.show_error = lambda *_args: self.fail("plot load failed")

        with patch("qplot.windows._plot_actions.plot1d", plot1d):
            harness.openPlot(key_a, params=[param], show=False)

        self.assertEqual(len(harness.windows), 1)
        self.assertEqual(harness.windows[0]._dataset_key, key_a)
        self.assertIs(harness.windows[0].dataset, dataset_a)
        self.assertIs(harness.dataset_holder[key_a].dataset, dataset_a)
        self.assertIs(harness.dataset_holder[key_b].dataset, dataset_b)

    def test_same_label_plot_from_another_database_is_a_merge_candidate(self):
        class Combo:
            def __init__(self, text):
                self._text = text

            def currentText(self):
                return self._text

        key_a = DatasetKey("database-a.db", "shared-guid")
        key_b = DatasetKey("database-b.db", "shared-guid")
        param_a = self.Param()
        param_a.depends_on = "x"
        param_b = self.Param()
        param_b.depends_on = "x"

        target = plot1d.__new__(plot1d)
        target._trace_key = TraceKey(key_a, param_a.name)
        target.param = param_a
        target.label = "ID:1 signal"
        target.line = object()
        target.lines = {target.label: target.line}
        target.axis_dropdown = {"x": Combo("x"), "y": Combo("signal")}
        candidates = []
        target.update_line_picker = lambda wins: candidates.extend(wins)

        source = plot1d.__new__(plot1d)
        source._trace_key = TraceKey(key_b, param_b.name)
        source.param = param_b
        source.label = target.label
        source.axis_dropdown = {"x": Combo("x"), "y": Combo("signal")}

        harness = type("Harness", (PlotActionsMixin,), {})()
        harness.windows = [target, source]

        harness.get_1d_wins(target)

        self.assertEqual(candidates, [source])

    def test_sibling_heatmap_cuts_remain_distinct_merge_candidates(self):
        class Combo:
            def __init__(self, text):
                self._text = text

            def currentText(self):
                return self._text

        dataset_key = DatasetKey("database.db", "guid")

        target_param = self.Param()
        target_param.name = "line_signal"
        target_param.depends_on = "gate"
        target = plot1d.__new__(plot1d)
        target._trace_key = TraceKey(dataset_key, target_param.name)
        target.param = target_param
        target.label = "ID:1 line_signal"
        target.line = object()
        target.axis_dropdown = {
            "x": Combo("gate"),
            "y": Combo("line_signal"),
            }

        heatmap_param = self.Param()
        heatmap_param.name = "heatmap_signal"
        heatmap_param.depends_on = "gate, field"

        class sweeper:
            def __init__(self, sweep_id):
                self._trace_key = TraceKey(
                    dataset_key,
                    heatmap_param.name,
                    sweep_id=sweep_id,
                    )
                self.param = heatmap_param
                self.label = f"ID:1 heatmap_signal [cut {sweep_id + 1}]"
                self.axis_options = {"x": target_param.depends_on}

        first_cut = sweeper(0)
        second_cut = sweeper(1)
        first_line = type("Line", (), {"from_win": first_cut})()
        target.lines = {
            target.label: target.line,
            first_cut._trace_key: first_line,
            }
        candidates = []
        target.update_line_picker = lambda wins: candidates.extend(wins)

        harness = type("Harness", (PlotActionsMixin,), {})()
        harness.windows = [target, first_cut, second_cut]

        harness.get_1d_wins(target)

        self.assertNotEqual(first_cut._trace_key, second_cut._trace_key)
        self.assertEqual(candidates, [second_cut])

    def test_closing_one_database_plot_keeps_other_database_handle(self):
        harness = type("Harness", (PlotActionsMixin,), {})()
        harness.config = self.Config()
        harness.status_messages = []
        harness.show_status = lambda message, timeout=5000: harness.status_messages.append(
            (message, timeout)
        )
        key_a = DatasetKey("database-a.db", "shared-guid")
        key_b = DatasetKey("database-b.db", "shared-guid")
        handle_a = DatasetHandle(self.Dataset("A"))
        handle_b = DatasetHandle(self.Dataset("B"))
        harness.dataset_holder = {key_a: handle_a, key_b: handle_b}
        window_a = type("Window", (), {"_dataset_key": key_a, "label": "A"})()
        window_b = type("Window", (), {"_dataset_key": key_b, "label": "B"})()
        harness.windows = [window_a, window_b]
        harness.post_admin = lambda: None

        harness.onClose(window_a)

        self.assertNotIn(key_a, harness.dataset_holder)
        self.assertIs(harness.dataset_holder[key_b], handle_b)
        self.assertEqual(harness.windows, [window_b])

    def test_database_a_plot_keeps_using_database_a_handle_after_switch(self):
        key_a = DatasetKey("database-a.db", "shared-guid")
        key_b = DatasetKey("database-b.db", "shared-guid")
        dataset_a = self.Dataset("A")
        dataset_b = self.Dataset("B")
        window = plotWidget.__new__(plotWidget)
        window._guid = key_a.guid
        window._dataset_key = key_a
        window._dataset_holder = {
            key_a: DatasetHandle(dataset_a),
            key_b: DatasetHandle(dataset_b),
        }

        self.assertIs(window.ds, dataset_a)

    def test_failed_plot_construction_does_not_publish_new_dataset(self):
        class Connection:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class Dataset:
            guid = "guid"

            def __init__(self):
                self.conn = Connection()

        class Harness(PlotActionsMixin):
            def __init__(self):
                self.config = object()
                self.dataset_holder = {}
                self.threadPool = object()
                self.windows = []
                self.fileTextbox = DatabaseAwareDatasetCacheTestCase.Field("database.db")

        constructor_connections = []

        def failing_widget(*args, **kwargs):
            construction_holder = args[-1]
            key = DatasetKey("database.db", "guid")
            constructor_connections.append(construction_holder[key].dataset.conn)
            raise RuntimeError("plot construction failed")

        dataset = Dataset()
        harness = Harness()

        with self.assertRaisesRegex(RuntimeError, "plot construction failed"):
            harness.openWin(failing_widget, dataset, show=False)

        self.assertEqual(constructor_connections, [dataset.conn])
        self.assertFalse(dataset.conn.closed)
        self.assertEqual(harness.dataset_holder, {})
        self.assertEqual(harness.windows, [])

    def test_failed_plot_construction_closes_transient_loaded_connection(self):
        class Connection:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class Dataset:
            guid = "guid"

            def __init__(self):
                self.conn = Connection()

        class Harness(PlotActionsMixin):
            def __init__(self):
                self.config = object()
                self.dataset_holder = {}
                self.threadPool = object()
                self.windows = []

        def failing_widget(*args, **kwargs):
            raise RuntimeError("plot construction failed")

        dataset = Dataset()
        harness = Harness()
        dataset_key = DatasetKey("database.db", "guid")

        with (
            patch(
                "qplot.windows._plot_actions.load_by_guid_read_only",
                return_value=dataset,
            ),
            self.assertRaisesRegex(RuntimeError, "plot construction failed"),
        ):
            harness.openWin(failing_widget, dataset_key, show=False)

        self.assertTrue(dataset.conn.closed)
        self.assertEqual(harness.dataset_holder, {})
        self.assertEqual(harness.windows, [])


class DatabaseOpenDirectoryTestCase(unittest.TestCase):
    def test_database_open_directory_prefers_current_database_folder(self):
        class Field:
            def __init__(self, text):
                self._text = text

            def text(self):
                return self._text

        class FakeConfig:
            def __init__(self, default_path):
                self.default_path = default_path

            def get(self, key):
                if key == "file.default_load_path":
                    return self.default_path
                raise KeyError(key)

        class Harness:
            database_open_directory = main_window.MainWindow.database_open_directory

            def __init__(self, database_path, default_path):
                self.fileTextbox = Field(database_path)
                self.config = FakeConfig(default_path)

        with (
            tempfile.TemporaryDirectory() as current_dir,
            tempfile.TemporaryDirectory() as default_dir,
        ):
            database_path = str(Path(current_dir) / "current.db")
            Path(database_path).touch()

            harness = Harness(database_path, default_dir)

            self.assertEqual(harness.database_open_directory(), current_dir)

    def test_database_open_directory_falls_back_to_default_load_path(self):
        class Field:
            def text(self):
                return ""

        class FakeConfig:
            def __init__(self, default_path):
                self.default_path = default_path

            def get(self, key):
                if key == "file.default_load_path":
                    return self.default_path
                raise KeyError(key)

        class Harness:
            database_open_directory = main_window.MainWindow.database_open_directory

            def __init__(self, default_path):
                self.fileTextbox = Field()
                self.config = FakeConfig(default_path)

        with tempfile.TemporaryDirectory() as default_dir:
            harness = Harness(default_dir)

            self.assertEqual(harness.database_open_directory(), default_dir)


class OptionsMenuTestCase(unittest.TestCase):
    class FakeConfig:
        def get(self, key):
            if key == "user_preference.theme":
                return "light"
            raise KeyError(key)

    class Harness(qtw.QMainWindow):
        initMenu = main_window.MainWindow.initMenu

        def __init__(self):
            super().__init__()
            self.config = OptionsMenuTestCase.FakeConfig()
            self.preview_size = 200

        def refresh_recent_database_menu(self):
            pass

        def getfile(self):
            pass

        def open_database_location(self):
            pass

        def refreshMain(self):
            pass

        def create_test_database_csv(self):
            pass

        def export_test_database_csv_collection(self):
            pass

        def generate_test_database_from_csv(self):
            pass

        def closeAll(self):
            pass

        def restore_default_settings(self):
            pass

        def show_preferences_dialog(self):
            pass

    def test_main_options_menu_uses_preferences_for_shared_settings(self):
        window = self.Harness()

        try:
            window.initMenu()
            menus = {
                action.text().replace("&", ""): action.menu()
                for action in window.menuBar().actions()
                }
            option_texts = [
                action.text().replace("&", "")
                for action in menus["Options"].actions()
                if not action.isSeparator()
                ]
            preferences_action = next(
                action for action in menus["Options"].actions()
                if action.text().replace("&", "") == "Preferences..."
                )

            self.assertIn("Preferences...", option_texts)
            self.assertEqual(
                preferences_action.menuRole(),
                QtGui.QAction.MenuRole.PreferencesRole,
                )
            self.assertIn("Reset All Settings...", option_texts)
            self.assertNotIn("Open Location", option_texts)
            self.assertNotIn("Theme", option_texts)
            self.assertNotIn("Preview Size", option_texts)
            self.assertNotIn("Confirm Before Closing All Plot Windows", option_texts)
            self.assertNotIn("Confirm Before Quit", option_texts)
        finally:
            window.deleteLater()

    def test_file_menu_exposes_test_data_generation_actions(self):
        window = self.Harness()

        try:
            window.initMenu()
            menus = {
                action.text().replace("&", ""): action.menu()
                for action in window.menuBar().actions()
                }
            file_menu = menus["File"]
            test_data_menu = next(
                action.menu()
                for action in file_menu.actions()
                if action.text().replace("&", "") == "Generate Test Data"
                )
            actions = {
                action.objectName(): action.text().replace("&", "")
                for action in test_data_menu.actions()
                }

            self.assertEqual(
                actions,
                {
                    "createTestDatabaseCsvAction": "Create Example CSV...",
                    "exportTestDatabaseCsvCollectionAction": "Export CSV Collection...",
                    "generateTestDatabaseAction": "Generate Database from CSV...",
                },
                )
        finally:
            window.deleteLater()


class CloseAllPlotsTestCase(unittest.TestCase):
    def test_close_all_can_be_cancelled_when_warning_enabled(self):
        old_confirmation = main_window.ask_confirmation_with_dont_ask_again
        confirmation_keys = []
        closed = []

        class FakeConfig:
            def get(self, key):
                if key == CONFIRM_CLOSE_ALL_KEY:
                    return True
                raise KeyError(key)

        class FakeWindow:
            def close(self):
                closed.append(self)

        class Harness:
            closeAll = main_window.MainWindow.closeAll
            close_plot_windows = main_window.MainWindow.close_plot_windows

            def __init__(self):
                self.config = FakeConfig()
                self.windows = [FakeWindow()]
                self.status_messages = []

            def show_status(self, message, timeout=5000):
                self.status_messages.append((message, timeout))

        try:
            def fake_confirmation(window, title, message, config_key, *args):
                confirmation_keys.append(config_key)
                return qtw.QMessageBox.StandardButton.No

            main_window.ask_confirmation_with_dont_ask_again = fake_confirmation
            harness = Harness()
            harness.closeAll()
        finally:
            main_window.ask_confirmation_with_dont_ask_again = old_confirmation

        self.assertEqual(closed, [])
        self.assertEqual(confirmation_keys, [CONFIRM_CLOSE_ALL_KEY])
        self.assertEqual(harness.status_messages[-1][0], "Close all plot windows cancelled.")

    def test_close_all_without_warning_closes_each_window(self):
        closed = []

        class FakeConfig:
            def get(self, key):
                if key == CONFIRM_CLOSE_ALL_KEY:
                    return False
                raise KeyError(key)

        class FakeWindow:
            def close(self):
                closed.append(self)

        class Harness:
            closeAll = main_window.MainWindow.closeAll
            close_plot_windows = main_window.MainWindow.close_plot_windows

            def __init__(self):
                self.config = FakeConfig()
                self.windows = [FakeWindow(), FakeWindow()]
                self.status_messages = []

            def show_status(self, message, timeout=5000):
                self.status_messages.append((message, timeout))

        harness = Harness()
        harness.closeAll()

        self.assertEqual(closed, harness.windows)
        self.assertEqual(harness.status_messages[-1][0], "Closing plot windows...")

    def test_close_sweeps_from_plot_closes_matching_cut_windows(self):
        closed = []
        source_ds = object()
        source_param = object()

        class Plot:
            def __init__(self, ds, param):
                self.ds = ds
                self.param = param

        class sweeper:
            def __init__(self, ds, param, sweep_id):
                self.ds = ds
                self.param = param
                self.sweep_id = sweep_id

            def close(self):
                closed.append(self)

        class Harness:
            close_sweeps_from_plot = main_window.MainWindow.close_sweeps_from_plot

            def __init__(self, windows):
                self.windows = windows

        source = Plot(source_ds, source_param)
        target = sweeper(source_ds, source_param, 2)
        other_id = sweeper(source_ds, source_param, 3)
        other_plot = sweeper(object(), source_param, 2)
        harness = Harness([target, other_id, other_plot])

        harness.close_sweeps_from_plot(source, (2,))

        self.assertEqual(closed, [target])

    def test_confirmation_dialog_can_disable_future_warning_after_confirm(self):
        old_exec = qtw.QMessageBox.exec
        updates = []
        labels = []

        class FakeConfig:
            def update(self, key, value):
                updates.append((key, value))

        window = qtw.QMainWindow()
        window.config = FakeConfig()

        def fake_exec(box):
            labels.append(box.checkBox().text())
            box.checkBox().setChecked(True)
            return qtw.QMessageBox.StandardButton.Yes

        try:
            qtw.QMessageBox.exec = fake_exec
            reply = ask_confirmation_with_dont_ask_again(
                window,
                "Close All Plot Windows",
                "Close 2 plot windows?",
                CONFIRM_CLOSE_ALL_KEY,
                )
        finally:
            qtw.QMessageBox.exec = old_exec
            window.deleteLater()

        self.assertEqual(reply, qtw.QMessageBox.StandardButton.Yes)
        self.assertEqual(labels, [DO_NOT_ASK_AGAIN_LABEL])
        self.assertEqual(updates, [(CONFIRM_CLOSE_ALL_KEY, False)])

    def test_confirmation_dialog_cancel_does_not_disable_future_warning(self):
        old_exec = qtw.QMessageBox.exec
        updates = []

        class FakeConfig:
            def update(self, key, value):
                updates.append((key, value))

        window = qtw.QMainWindow()
        window.config = FakeConfig()

        def fake_exec(box):
            box.checkBox().setChecked(True)
            return qtw.QMessageBox.StandardButton.No

        try:
            qtw.QMessageBox.exec = fake_exec
            reply = ask_confirmation_with_dont_ask_again(
                window,
                "Confirm Exit",
                "Are you sure you want to exit?",
                CONFIRM_QUIT_KEY,
                )
        finally:
            qtw.QMessageBox.exec = old_exec
            window.deleteLater()

        self.assertEqual(reply, qtw.QMessageBox.StandardButton.No)
        self.assertEqual(updates, [])

    def test_close_event_can_disable_future_quit_warning_after_confirm(self):
        old_confirmation = main_window.ask_confirmation_with_dont_ask_again
        old_close_all_windows = qtw.QApplication.closeAllWindows
        confirmations = []
        closed_all_windows = []
        updates = []

        class FakeConfig:
            def __init__(self):
                self.values = {CONFIRM_QUIT_KEY: True}

            def get(self, key):
                return self.values[key]

            def update(self, key, value):
                updates.append((key, value))
                self.values[key] = value

        class Timer:
            def __init__(self):
                self.stopped = False

            def stop(self):
                self.stopped = True

        class Worker:
            def __init__(self):
                self.cancelled = False

            def cancel(self):
                self.cancelled = True

        class Preview:
            def __init__(self):
                self.shut_down = False

            def shutdown(self):
                self.shut_down = True

        class Event:
            def __init__(self):
                self.accepted = False
                self.ignored = False

            def accept(self):
                self.accepted = True

            def ignore(self):
                self.ignored = True

        class Harness:
            closeEvent = main_window.MainWindow.closeEvent

            def __init__(self):
                self.config = FakeConfig()
                self.startupDatabaseTimer = Timer()
                self._database_load_worker = Worker()
                self._database_load_generation = 0
                self._database_load_active = True
                self._database_load_state = {"loading": True}
                self.monitor = Timer()
                self.infoBox = type("InfoBox", (), {"preview": Preview()})()

        def fake_confirmation(window, title, message, config_key, *args):
            confirmations.append((title, message, config_key))
            window.config.update(config_key, False)
            return qtw.QMessageBox.StandardButton.Yes

        try:
            main_window.ask_confirmation_with_dont_ask_again = fake_confirmation
            qtw.QApplication.closeAllWindows = lambda: closed_all_windows.append(True)
            harness = Harness()
            worker = harness._database_load_worker
            event = Event()

            harness.closeEvent(event)
        finally:
            main_window.ask_confirmation_with_dont_ask_again = old_confirmation
            qtw.QApplication.closeAllWindows = old_close_all_windows

        self.assertTrue(event.accepted)
        self.assertFalse(event.ignored)
        self.assertEqual(
            confirmations,
            [("Confirm Exit", "Are you sure you want to exit?", CONFIRM_QUIT_KEY)],
            )
        self.assertEqual(updates, [(CONFIRM_QUIT_KEY, False)])
        self.assertTrue(harness.startupDatabaseTimer.stopped)
        self.assertTrue(worker.cancelled)
        self.assertFalse(harness._database_load_active)
        self.assertIsNone(harness._database_load_state)
        self.assertIsNone(harness._database_load_worker)
        self.assertTrue(harness.infoBox.preview.shut_down)
        self.assertTrue(harness.monitor.stopped)
        self.assertEqual(closed_all_windows, [True])

    def test_confirmation_options_use_shared_labels_and_config_keys(self):
        updates = []

        class FakeConfig:
            values = {
                "user_preference.confirm_close_all": True,
                "user_preference.confirm_close": False,
                }

            def get(self, key):
                return self.values[key]

            def update(self, key, value):
                updates.append((key, value))
                self.values[key] = value

        window = qtw.QMainWindow()
        window.config = FakeConfig()
        menu = qtw.QMenu(window)

        try:
            add_confirmation_options(window, menu)
            actions = [
                action for action in menu.actions()
                if not action.isSeparator()
                ]

            self.assertEqual(
                [action.text() for action in actions],
                [
                    "Confirm Before Closing All Plot Windows",
                    "Confirm Before Quit",
                    ]
                )
            self.assertTrue(actions[0].isChecked())
            self.assertFalse(actions[1].isChecked())

            actions[0].setChecked(False)
            actions[1].setChecked(True)

            self.assertEqual(updates, [
                ("user_preference.confirm_close_all", False),
                ("user_preference.confirm_close", True),
                ])
        finally:
            window.deleteLater()

    def test_restore_defaults_option_requests_main_window_reset(self):
        called = []
        window = qtw.QMainWindow()
        window.restore_default_settings = lambda: called.append(True)
        menu = qtw.QMenu(window)

        try:
            action = add_restore_defaults_option(window, menu)
            self.assertEqual(action.text(), "Reset All Settings...")

            action.trigger()

            self.assertEqual(called, [True])
        finally:
            window.deleteLater()

    def test_restore_default_settings_can_be_cancelled(self):
        old_question = qtw.QMessageBox.question

        class FakeConfig:
            def __init__(self):
                self.reset_called = False

            def reset_to_defaults(self):
                self.reset_called = True

        class Harness:
            restore_default_settings = main_window.MainWindow.restore_default_settings

            def __init__(self):
                self.config = FakeConfig()
                self.status_messages = []

            def close_plot_windows(self, confirm=True, status=True):
                raise AssertionError("Plot windows should not close after cancelling")

            def close_database(self, status=True):
                raise AssertionError("Database should not close after cancelling")

            def apply_current_settings(self):
                raise AssertionError("Settings should not be applied after cancelling")

            def show_status(self, message, timeout=5000):
                self.status_messages.append((message, timeout))

        try:
            qtw.QMessageBox.question = lambda *args, **kwargs: qtw.QMessageBox.StandardButton.No
            harness = Harness()
            harness.restore_default_settings()
        finally:
            qtw.QMessageBox.question = old_question

        self.assertFalse(harness.config.reset_called)
        self.assertEqual(harness.status_messages[-1][0], "Settings reset cancelled.")

    def test_restore_default_settings_resets_and_applies_defaults(self):
        old_question = qtw.QMessageBox.question
        questions = []

        class FakeConfig:
            def __init__(self):
                self.reset_called = False

            def reset_to_defaults(self):
                self.reset_called = True

        class Harness:
            restore_default_settings = main_window.MainWindow.restore_default_settings

            def __init__(self):
                self.config = FakeConfig()
                self.applied = False
                self.closed_plots = []
                self.closed_database = []
                self.status_messages = []

            def close_plot_windows(self, confirm=True, status=True):
                self.closed_plots.append((confirm, status))

            def close_database(self, status=True):
                self.closed_database.append(status)

            def apply_current_settings(self):
                self.applied = True

            def show_status(self, message, timeout=5000):
                self.status_messages.append((message, timeout))

        try:
            def answer_yes(*args, **kwargs):
                questions.append((args, kwargs))
                return qtw.QMessageBox.StandardButton.Yes

            qtw.QMessageBox.question = answer_yes
            harness = Harness()
            harness.restore_default_settings()
        finally:
            qtw.QMessageBox.question = old_question

        self.assertTrue(harness.config.reset_called)
        self.assertTrue(harness.applied)
        self.assertEqual(harness.closed_plots, [(False, False)])
        self.assertEqual(harness.closed_database, [False])
        self.assertEqual(harness.status_messages[-1][0], "Settings reset to defaults.")
        self.assertIn("close the current database", questions[0][0][2])
        self.assertIn("all plot windows", questions[0][0][2])


    def test_close_database_clears_loaded_database_state(self):
        class Field:
            def __init__(self, text=""):
                self.value = text

            def setText(self, text):
                self.value = text

            def text(self):
                return self.value

        class Timer:
            def __init__(self):
                self.stopped = False

            def stop(self):
                self.stopped = True

        class RunList:
            def __init__(self):
                self.signals_blocked = None
                self.cleared = False
                self.selection_cleared = False
                self.scrolled = False
                self.watching = ["run"]
                self.maxRunId = 123

            def blockSignals(self, blocked):
                self.signals_blocked = blocked

            def clearSelection(self):
                self.selection_cleared = True

            def clear(self):
                self.cleared = True

            def scrollToTop(self):
                self.scrolled = True

            def topLevelItemCount(self):
                return 0

        class EmptyState:
            def __init__(self):
                self.visible = None

            def setVisible(self, visible):
                self.visible = visible

        class Preview:
            def __init__(self):
                self.database_runs = None

            def set_database_runs(self, database_path, runs):
                self.database_runs = (database_path, runs)

        class InfoBox:
            def __init__(self):
                self.preview = Preview()
                self.cleared = False
                self.scrolled = False

            def clear(self):
                self.cleared = True

            def scrollToTop(self):
                self.scrolled = True

        class Harness:
            close_database = main_window.MainWindow.close_database
            _sync_empty_state = main_window.MainWindow._sync_empty_state

            def __init__(self):
                self.monitor = Timer()
                self.fileTextbox = Field("test.db")
                self.run_idBox = Field("7")
                self.measurementBox = Field("x")
                self.selected_run_id = 7
                self.ds = object()
                self.localLastFile = "test.db"
                self.dataset_handle = DatasetHandle(object(), delete_timer=Timer())
                self.dataset_holder = {"guid": self.dataset_handle}
                self.RunList = RunList()
                self.infoBox = InfoBox()
                self.emptyStateFrame = EmptyState()

            def show_status(self, message, timeout=5000):
                raise AssertionError("Status should not be shown when disabled")

        harness = Harness()
        del_timer = harness.dataset_handle.delete_timer

        harness.close_database(status=False)

        self.assertTrue(harness.monitor.stopped)
        self.assertEqual(harness.fileTextbox.text(), "")
        self.assertEqual(harness.run_idBox.text(), "")
        self.assertEqual(harness.measurementBox.text(), "*")
        self.assertIsNone(harness.selected_run_id)
        self.assertIsNone(harness.ds)
        self.assertIsNone(harness.localLastFile)
        self.assertTrue(del_timer.stopped)
        self.assertEqual(harness.dataset_holder, {})
        self.assertTrue(harness.RunList.selection_cleared)
        self.assertTrue(harness.RunList.cleared)
        self.assertEqual(harness.RunList.watching, [])
        self.assertEqual(harness.RunList.maxRunId, 0)
        self.assertTrue(harness.infoBox.cleared)
        self.assertEqual(harness.infoBox.preview.database_runs, ("", {}))
        self.assertTrue(harness.emptyStateFrame.visible)



class DatabaseAccessProbeTestCase(unittest.TestCase):
    def test_database_access_error_returns_none_for_readable_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = str(Path(temp_dir) / "readable.db")
            conn = sqlite3.connect(database_path)
            try:
                conn.execute("PRAGMA user_version")
            finally:
                conn.close()

            self.assertIsNone(database_module.database_access_error(database_path))

    def test_database_access_error_reports_timeout(self):
        old_run = database_module.subprocess.run

        def run(*args, **kwargs):
            raise database_module.subprocess.TimeoutExpired(
                cmd=args[0],
                timeout=kwargs["timeout"],
                )

        database_module.subprocess.run = run
        try:
            error = database_module.database_access_error("locked.db", timeout=0.5)
        finally:
            database_module.subprocess.run = old_run

        self.assertIn("Timed out after 0.5 s", error)
        self.assertIn("locked", error)


class DatabaseLoadUiTestCase(unittest.TestCase):
    class Field:
        def __init__(self, text=""):
            self.value = text
            self.signals_blocked = False

        def setText(self, text):
            self.value = text

        def text(self):
            return self.value

        def blockSignals(self, blocked):
            previous = self.signals_blocked
            self.signals_blocked = blocked
            return previous

    class Button:
        def __init__(self):
            self.enabled = True
            self.visible = True

        def setEnabled(self, enabled):
            self.enabled = enabled

        def setVisible(self, visible):
            self.visible = visible

    class Frame:
        def __init__(self):
            self.visible = False

        def setVisible(self, visible):
            self.visible = visible

    class SpinBox:
        def __init__(self, value=1.5):
            self._value = value

        def value(self):
            return self._value

        def setValue(self, value):
            self._value = value

    class Label:
        def __init__(self):
            self.text = ""
            self.tooltip = ""

        def setText(self, text):
            self.text = text

        def setToolTip(self, tooltip):
            self.tooltip = tooltip

    class Timer:
        def __init__(self):
            self.started = []
            self.stopped = False

        def start(self, interval):
            self.started.append(interval)

        def stop(self):
            self.stopped = True

    class RunList:
        def __init__(self):
            self.runs = {}
            self.signals_blocked = False
            self.selection_cleared = False
            self.scrolled = False
            self.watching = ["old"]
            self.maxRunId = 9
            self.selected_ids = [8]
            self.visible_ids = [9, 7, 6]

        def clearSelection(self):
            self.selection_cleared = True

        def blockSignals(self, blocked):
            previous = self.signals_blocked
            self.signals_blocked = blocked
            return previous

        def clear(self):
            self.runs = {}

        def addRuns(self, runs):
            self.runs = runs
            self.maxRunId = max(runs, default=0)

        def scrollToTop(self):
            self.scrolled = True

        def topLevelItemCount(self):
            return len(self.runs)

        def all_run_metadata(self):
            return self.runs

        def selected_run_ids(self):
            return list(self.selected_ids)

        def visible_run_ids(self):
            return list(self.visible_ids)

    class Preview:
        def __init__(self):
            self.database_runs = None

        def set_database_runs(self, database_path, runs):
            self.database_runs = (database_path, runs)

        def has_database(self, database_path):
            return (
                self.database_runs is not None
                and self.database_runs[0] == database_path
                )

    class InfoBox:
        def __init__(self):
            self.preview = DatabaseLoadUiTestCase.Preview()
            self.cleared = False
            self.scrolled = False

        def clear(self):
            self.cleared = True

        def scrollToTop(self):
            self.scrolled = True

    class Worker:
        def __init__(self):
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

    class ThreadPool:
        def __init__(self):
            self.started = []

        def start(self, worker):
            self.started.append(worker)

    class Config:
        def __init__(self, values=None):
            self.values = values or {}

        def get(self, key):
            if key == "runtime_settings.cloud_sync_timeout":
                return 30
            return self.values[key]

    class Harness(QtCore.QObject):
        load_file = main_window.MainWindow.load_file
        load_database_path = main_window.MainWindow.load_database_path
        load_startup_database = main_window.MainWindow.load_startup_database
        close_database = main_window.MainWindow.close_database
        cancel_database_load = main_window.MainWindow.cancel_database_load
        database_load_finished = main_window.MainWindow.database_load_finished
        database_load_status = main_window.MainWindow.database_load_status
        _cancel_database_detail_load = (
            main_window.MainWindow._cancel_database_detail_load
            )
        _hide_database_load_panel = main_window.MainWindow._hide_database_load_panel
        _prepare_database_load_ui = (
            main_window.MainWindow._prepare_database_load_ui
            )
        _set_database_load_controls_enabled = (
            main_window.MainWindow._set_database_load_controls_enabled
            )
        _show_database_load_panel = main_window.MainWindow._show_database_load_panel
        _sync_empty_state = main_window.MainWindow._sync_empty_state
        _sync_no_database_empty_state = (
            main_window.MainWindow._sync_no_database_empty_state
            )
        _sync_loaded_empty_state = (
            main_window.MainWindow._sync_loaded_empty_state
            )
        _set_empty_state_button_visible = (
            main_window.MainWindow._set_empty_state_button_visible
            )
        _loaded_empty_database_detail = (
            main_window.MainWindow._loaded_empty_database_detail
            )
        _current_refresh_interval = main_window.MainWindow._current_refresh_interval
        _loaded_empty_database_status = (
            main_window.MainWindow._loaded_empty_database_status
            )
        _empty_database_refresh_status = (
            main_window.MainWindow._empty_database_refresh_status
            )
        _main_refresh_interval = main_window.MainWindow._main_refresh_interval
        _apply_refresh_interval = main_window.MainWindow._apply_refresh_interval
        _database_detail_priority_run_ids = (
            main_window.MainWindow._database_detail_priority_run_ids
            )

        def __init__(self):
            super().__init__()
            self._database_load_generation = 2
            self._database_load_active = False
            self._database_load_state = None
            self._database_load_worker = None
            self._database_detail_generation = 4
            self._database_detail_active = False
            self._database_detail_worker = None
            self._database_expensive_detail_generation = 6
            self._database_expensive_detail_active = False
            self._database_expensive_detail_worker = None
            self.fileTextbox = DatabaseLoadUiTestCase.Field()
            self.run_idBox = DatabaseLoadUiTestCase.Field()
            self.measurementBox = DatabaseLoadUiTestCase.Field()
            self.selected_run_id = 3
            self.ds = object()
            self.RunList = DatabaseLoadUiTestCase.RunList()
            self.infoBox = DatabaseLoadUiTestCase.InfoBox()
            self.monitor = DatabaseLoadUiTestCase.Timer()
            self.loadDatabaseButton = DatabaseLoadUiTestCase.Button()
            self.refreshDatabaseButton = DatabaseLoadUiTestCase.Button()
            self.databaseInfoButton = DatabaseLoadUiTestCase.Button()
            self.openDatabaseFolderButton = DatabaseLoadUiTestCase.Button()
            self.databaseLoadFrame = DatabaseLoadUiTestCase.Frame()
            self.databaseLoadLabel = DatabaseLoadUiTestCase.Label()
            self.emptyStateFrame = DatabaseLoadUiTestCase.Frame()
            self.emptyStateTitle = DatabaseLoadUiTestCase.Label()
            self.emptyStateDetail = DatabaseLoadUiTestCase.Label()
            self.emptyStateLoadButton = DatabaseLoadUiTestCase.Button()
            self.emptyStateRefreshButton = DatabaseLoadUiTestCase.Button()
            self.emptyStateHelpButton = DatabaseLoadUiTestCase.Button()
            self.spinBox = DatabaseLoadUiTestCase.SpinBox()
            self.config = DatabaseLoadUiTestCase.Config()
            self.databaseLoadThreadPool = DatabaseLoadUiTestCase.ThreadPool()
            self.dataset_holder = {}
            self.localLastFile = None
            self.startup_database_path = None
            self.status_messages = []
            self.error_messages = []
            self.remembered_databases = []
            self.detail_loads = []

        def show_status(self, message, timeout=5000):
            self.status_messages.append((message, timeout))

        def show_error(self, title, message, details=None):
            self.error_messages.append((title, message, details))

        def select_default_run(self):
            self.database_at_default_selection = get_DB_location()

        def remember_loaded_database(self, database_path):
            self.remembered_databases.append(database_path)

        def _start_database_detail_load(self, database_path, runs):
            self.detail_loads.append((database_path, runs))

    @staticmethod
    def _database_view(harness):
        return {
            "path": harness.fileTextbox.text(),
            "run_id": harness.run_idBox.text(),
            "measurement": harness.measurementBox.text(),
            "selected_run_id": harness.selected_run_id,
            "dataset": harness.ds,
            "runs": harness.RunList.runs,
            "selected_ids": harness.RunList.selected_ids,
            "selection_cleared": harness.RunList.selection_cleared,
            "run_list_signals_blocked": harness.RunList.signals_blocked,
            "run_id_signals_blocked": harness.run_idBox.signals_blocked,
            "watching": harness.RunList.watching,
            "max_run_id": harness.RunList.maxRunId,
            "previews": harness.infoBox.preview.database_runs,
            "info_cleared": harness.infoBox.cleared,
            "monitor_stopped": harness.monitor.stopped,
            "detail_worker": harness._database_detail_worker,
            "detail_active": harness._database_detail_active,
            "expensive_detail_worker": harness._database_expensive_detail_worker,
            "expensive_detail_active": harness._database_expensive_detail_active,
            }

    def _active_database_harness(self):
        old_runs = {5: {"guid": "guid-5", "run_timestamp": 123.0}}
        harness = self.Harness()
        harness.fileTextbox.setText("database-a.db")
        harness.run_idBox.setText("5")
        harness.measurementBox.setText("signal")
        harness.selected_run_id = 5
        harness.ds = object()
        harness.RunList.runs = old_runs
        harness.RunList.selected_ids = [5]
        harness.infoBox.preview.set_database_runs("database-a.db", old_runs)
        harness._database_detail_active = True
        harness._database_detail_worker = self.Worker()
        harness._database_expensive_detail_active = True
        harness._database_expensive_detail_worker = self.Worker()
        return harness

    def test_current_view_remains_unchanged_while_another_database_is_pending(self):
        active_database = get_DB_location()
        set_qcodes_database_location("database-a.db")
        try:
            harness = self._active_database_harness()
            previous_view = self._database_view(harness)

            self.assertTrue(harness.load_file("database-b.db"))

            self.assertEqual(self._database_view(harness), previous_view)
            self.assertEqual(get_DB_location(), "database-a.db")
            self.assertTrue(harness._database_load_active)
            self.assertEqual(harness._database_load_state["abspath"], "database-b.db")
            self.assertEqual(len(harness.databaseLoadThreadPool.started), 1)
            self.assertFalse(harness.loadDatabaseButton.enabled)
            self.assertFalse(harness.refreshDatabaseButton.enabled)
            self.assertFalse(harness.databaseInfoButton.enabled)
            self.assertFalse(harness.openDatabaseFolderButton.enabled)
        finally:
            set_qcodes_database_location(active_database)

    def test_existing_qcodes_database_is_not_displayed_before_it_is_loaded(self):
        class Harness(qtw.QMainWindow):
            load_database_path = lambda *_args: True
            copy_database_path = lambda *_args: None
            show_database_info = lambda *_args: None
            getfile = lambda *_args: None
            open_database_location = lambda *_args: None
            cancel_database_load = lambda *_args: None

            def __init__(self):
                super().__init__()
                self.closeAllPlotsButton = qtw.QToolButton()

        active_database = get_DB_location()
        with tempfile.NamedTemporaryFile(suffix=".db") as database:
            set_qcodes_database_location(database.name)
            harness = Harness()
            try:
                main_window.MainWindow.initFile(harness)
                self.assertEqual(harness.fileTextbox.text(), "")
            finally:
                harness.deleteLater()
                set_qcodes_database_location(active_database)

    def test_startup_last_file_equal_to_qcodes_database_starts_worker(self):
        active_database = get_DB_location()
        with tempfile.NamedTemporaryFile(suffix=".db") as database:
            database_path = os.path.abspath(database.name)
            set_qcodes_database_location(database_path)
            try:
                harness = self.Harness()
                harness.config = self.Config({"file.last_file_path": database_path})

                self.assertTrue(harness.load_startup_database())

                self.assertEqual(harness.fileTextbox.text(), "")
                self.assertTrue(harness._database_load_active)
                self.assertEqual(harness._database_load_state["abspath"], database_path)
                self.assertEqual(len(harness.databaseLoadThreadPool.started), 1)
            finally:
                set_qcodes_database_location(active_database)

    def test_startup_uses_existing_qcodes_database_without_last_file(self):
        active_database = get_DB_location()
        with tempfile.NamedTemporaryFile(suffix=".db") as database:
            database_path = os.path.abspath(database.name)
            set_qcodes_database_location(database_path)
            try:
                harness = self.Harness()

                self.assertTrue(harness.load_startup_database())

                self.assertEqual(harness._database_load_state["abspath"], database_path)
                self.assertEqual(len(harness.databaseLoadThreadPool.started), 1)
            finally:
                set_qcodes_database_location(active_database)

    def test_startup_missing_last_file_falls_back_to_qcodes_database(self):
        active_database = get_DB_location()
        with tempfile.TemporaryDirectory() as temp_dir:
            qcodes_database = Path(temp_dir) / "qcodes.db"
            qcodes_database.touch()
            missing_database = Path(temp_dir) / "missing.db"
            set_qcodes_database_location(str(qcodes_database))
            try:
                harness = self.Harness()
                harness.config = self.Config(
                    {"file.last_file_path": str(missing_database)}
                    )

                self.assertTrue(harness.load_startup_database())

                self.assertEqual(
                    harness._database_load_state["abspath"],
                    str(qcodes_database),
                    )
            finally:
                set_qcodes_database_location(active_database)

    def test_explicit_startup_database_takes_precedence_over_fallbacks(self):
        active_database = get_DB_location()
        with tempfile.TemporaryDirectory() as temp_dir:
            startup_database = Path(temp_dir) / "startup.db"
            last_database = Path(temp_dir) / "last.db"
            qcodes_database = Path(temp_dir) / "qcodes.db"
            for database in (startup_database, last_database, qcodes_database):
                database.touch()
            set_qcodes_database_location(str(qcodes_database))
            try:
                harness = self.Harness()
                harness.startup_database_path = str(startup_database)
                harness.config = self.Config(
                    {"file.last_file_path": str(last_database)}
                    )

                self.assertTrue(harness.load_startup_database())

                self.assertEqual(
                    harness._database_load_state["abspath"],
                    str(startup_database),
                    )
            finally:
                set_qcodes_database_location(active_database)

    def test_invalid_explicit_startup_database_does_not_fall_back(self):
        active_database = get_DB_location()
        with tempfile.TemporaryDirectory() as temp_dir:
            missing_database = Path(temp_dir) / "missing.db"
            qcodes_database = Path(temp_dir) / "qcodes.db"
            qcodes_database.touch()
            set_qcodes_database_location(str(qcodes_database))
            try:
                harness = self.Harness()
                harness.startup_database_path = str(missing_database)

                self.assertFalse(harness.load_startup_database())

                self.assertEqual(harness.databaseLoadThreadPool.started, [])
                self.assertEqual(harness.error_messages[0][0], "Database Load Failed")
                self.assertEqual(harness.error_messages[0][2], str(missing_database))
            finally:
                set_qcodes_database_location(active_database)

    def test_successful_startup_load_commits_complete_database_view(self):
        active_database = get_DB_location()
        runs = {9: {"guid": "guid-9", "run_timestamp": 456.0}}
        with tempfile.NamedTemporaryFile(suffix=".db") as database:
            database_path = os.path.abspath(database.name)
            set_qcodes_database_location(database_path)
            try:
                harness = self.Harness()
                harness.config = self.Config({"file.last_file_path": database_path})

                self.assertTrue(harness.load_startup_database())
                generation = harness._database_load_generation
                harness.database_load_finished(
                    generation,
                    database_path,
                    runs,
                    None,
                    )

                self.assertEqual(harness.fileTextbox.text(), database_path)
                self.assertEqual(harness.RunList.runs, runs)
                self.assertEqual(
                    harness.infoBox.preview.database_runs,
                    (database_path, runs),
                    )
                self.assertEqual(harness.detail_loads, [(database_path, runs)])
                self.assertEqual(harness.monitor.started, [1500])
            finally:
                set_qcodes_database_location(active_database)

    def test_committed_empty_database_uses_already_loaded_shortcut(self):
        active_database = get_DB_location()
        with tempfile.NamedTemporaryFile(suffix=".db") as database:
            database_path = os.path.abspath(database.name)
            try:
                harness = self.Harness()
                self.assertTrue(harness.load_database_path(database_path))
                generation = harness._database_load_generation
                harness.database_load_finished(generation, database_path, {}, None)
                started_workers = list(harness.databaseLoadThreadPool.started)

                self.assertTrue(harness.load_database_path(database_path))

                self.assertEqual(
                    harness.databaseLoadThreadPool.started,
                    started_workers,
                    )
                self.assertIn("Database is already loaded", harness.status_messages[-1][0])
            finally:
                set_qcodes_database_location(active_database)

    def test_close_then_reopen_same_empty_database_starts_new_load(self):
        active_database = get_DB_location()
        with tempfile.NamedTemporaryFile(suffix=".db") as database:
            database_path = os.path.abspath(database.name)
            try:
                harness = self.Harness()
                self.assertTrue(harness.load_database_path(database_path))
                generation = harness._database_load_generation
                harness.database_load_finished(generation, database_path, {}, None)

                harness.close_database(status=False)
                self.assertTrue(harness.load_database_path(database_path))

                self.assertEqual(harness.fileTextbox.text(), "")
                self.assertTrue(harness._database_load_active)
                self.assertEqual(len(harness.databaseLoadThreadPool.started), 2)
            finally:
                set_qcodes_database_location(active_database)

    def test_active_load_blocks_already_loaded_shortcut(self):
        active_database = get_DB_location()
        try:
            set_qcodes_database_location("committed.db")
            harness = self.Harness()
            harness.fileTextbox.setText("committed.db")
            harness.infoBox.preview.set_database_runs("committed.db", {})

            self.assertTrue(harness.load_file("pending.db"))
            self.assertFalse(harness.load_file("committed.db"))

            self.assertEqual(
                harness.status_messages[-1],
                ("Wait for the current database load to finish.", 5000),
                )
            self.assertEqual(len(harness.databaseLoadThreadPool.started), 1)
        finally:
            set_qcodes_database_location(active_database)

    def test_successful_load_commits_path_runs_and_previews_together(self):
        active_database = get_DB_location()
        new_runs = {9: {"guid": "guid-9", "run_timestamp": 456.0}}
        set_qcodes_database_location("database-a.db")
        try:
            harness = self._active_database_harness()
            harness.load_file("database-b.db")
            generation = harness._database_load_generation

            harness.database_load_finished(
                generation,
                "database-b.db",
                new_runs,
                None,
                )

            self.assertEqual(get_DB_location(), "database-b.db")
            self.assertEqual(harness.fileTextbox.text(), "database-b.db")
            self.assertEqual(harness.RunList.runs, new_runs)
            self.assertEqual(
                harness.infoBox.preview.database_runs,
                ("database-b.db", new_runs),
                )
            self.assertEqual(harness.database_at_default_selection, "database-b.db")
            self.assertFalse(harness.RunList.signals_blocked)
            self.assertFalse(harness.run_idBox.signals_blocked)
        finally:
            set_qcodes_database_location(active_database)

    def test_cancellation_preserves_existing_database_state(self):
        active_database = get_DB_location()
        set_qcodes_database_location("database-a.db")
        try:
            harness = self._active_database_harness()
            previous_view = self._database_view(harness)
            harness.load_file("database-b.db")

            harness.cancel_database_load()

            self.assertEqual(self._database_view(harness), previous_view)
            self.assertEqual(get_DB_location(), "database-a.db")
            self.assertFalse(harness._database_load_active)
        finally:
            set_qcodes_database_location(active_database)

    def test_failure_preserves_existing_database_state(self):
        active_database = get_DB_location()
        set_qcodes_database_location("database-a.db")
        try:
            harness = self._active_database_harness()
            previous_view = self._database_view(harness)
            harness.load_file("database-b.db")
            generation = harness._database_load_generation

            harness.database_load_finished(
                generation,
                "database-b.db",
                {},
                RuntimeError("broken database"),
                )

            self.assertEqual(self._database_view(harness), previous_view)
            self.assertEqual(get_DB_location(), "database-a.db")
            self.assertFalse(harness._database_load_active)
        finally:
            set_qcodes_database_location(active_database)

    def test_stale_callbacks_cannot_commit(self):
        active_database = get_DB_location()
        stale_runs = {12: {"guid": "guid-12"}}
        set_qcodes_database_location("database-a.db")
        try:
            harness = self._active_database_harness()
            previous_view = self._database_view(harness)
            harness._database_load_generation = 20
            harness._database_load_active = False
            harness._database_load_state = None

            harness.database_load_finished(
                20,
                "database-b.db",
                stale_runs,
                None,
                )

            self.assertEqual(self._database_view(harness), previous_view)
            self.assertEqual(get_DB_location(), "database-a.db")
            self.assertEqual(harness.remembered_databases, [])
            self.assertEqual(harness.detail_loads, [])
        finally:
            set_qcodes_database_location(active_database)

    def test_database_load_status_shows_inline_progress(self):
        harness = self.Harness()
        harness._database_load_active = True

        harness.database_load_status(2, "Waiting for OneDrive sync...")

        self.assertTrue(harness.databaseLoadFrame.visible)
        self.assertEqual(harness.databaseLoadLabel.text, "Waiting for OneDrive sync...")
        self.assertEqual(harness.databaseLoadLabel.tooltip, "Waiting for OneDrive sync...")
        self.assertEqual(
            harness.status_messages,
            [("Waiting for OneDrive sync...", 0)],
            )

    def test_database_detail_batch_updates_rows_and_previews(self):
        class RunList:
            def __init__(self):
                self.updated_runs = []

            def updateRuns(self, runs):
                self.updated_runs.append(runs)
                return runs

        class Preview:
            def __init__(self):
                self.added_runs = []

            def add_runs(self, runs, queue_previews=True):
                self.added_runs.append((runs, queue_previews))

        class InfoBox:
            def __init__(self):
                self.preview = Preview()

        class Harness:
            database_detail_batch_ready = main_window.MainWindow.database_detail_batch_ready
            _apply_database_detail_batch = (
                main_window.MainWindow._apply_database_detail_batch
                )

            def __init__(self):
                self._database_detail_generation = 4
                self._database_detail_active = True
                self.fileTextbox = DatabaseLoadUiTestCase.Field("loaded.db")
                self.RunList = RunList()
                self.infoBox = InfoBox()
                self.refreshed_runs = []

            def _refresh_selected_run_details(self, runs):
                self.refreshed_runs.append(runs)

        harness = Harness()
        runs = {1: {"guid": "guid-1", "result_count": 10}}

        harness.database_detail_batch_ready(4, "loaded.db", runs)

        self.assertEqual(harness.RunList.updated_runs, [runs])
        self.assertEqual(harness.infoBox.preview.added_runs, [(runs, False)])
        self.assertEqual(harness.refreshed_runs, [runs])

    def test_database_detail_priority_uses_explicit_selected_and_visible_runs(self):
        harness = self.Harness()
        priority = harness._database_detail_priority_run_ids(run_ids=[7, 8])

        self.assertEqual(priority, [7, 8, 9, 6])

    def test_cancel_database_load_cancels_worker_and_preserves_current_view(self):
        previous_runs = {5: {"guid": "guid-5", "run_timestamp": 123.0}}
        worker = self.Worker()
        harness = self.Harness()
        harness.fileTextbox.setText("old.db")
        harness.RunList.runs = previous_runs
        harness.infoBox.preview.set_database_runs("old.db", previous_runs)
        harness._database_load_active = True
        harness._database_load_worker = worker
        harness._database_load_state = {
            "abspath": "pending.db",
            }

        harness.cancel_database_load()

        self.assertTrue(worker.cancelled)
        self.assertEqual(harness._database_load_generation, 3)
        self.assertFalse(harness._database_load_active)
        self.assertIsNone(harness._database_load_state)
        self.assertIsNone(harness._database_load_worker)
        self.assertEqual(harness.fileTextbox.text(), "old.db")
        self.assertEqual(harness.RunList.runs, previous_runs)
        self.assertEqual(
            harness.infoBox.preview.database_runs,
            ("old.db", previous_runs),
            )
        self.assertEqual(harness.RunList.watching, ["old"])
        self.assertEqual(harness.RunList.maxRunId, 9)
        self.assertEqual(harness.monitor.started, [])
        self.assertFalse(harness.databaseLoadFrame.visible)
        self.assertEqual(harness.databaseLoadLabel.text, "")
        self.assertTrue(harness.loadDatabaseButton.enabled)
        self.assertTrue(harness.refreshDatabaseButton.enabled)
        self.assertFalse(harness.emptyStateFrame.visible)
        self.assertEqual(harness.status_messages[-1], ("Database load cancelled.", 3000))

    def test_empty_state_is_visible_only_without_database_runs_or_loading(self):
        harness = self.Harness()

        harness._sync_empty_state()
        self.assertTrue(harness.emptyStateFrame.visible)
        self.assertEqual(harness.emptyStateTitle.text, "No database loaded")
        self.assertTrue(harness.emptyStateLoadButton.visible)
        self.assertFalse(harness.emptyStateRefreshButton.visible)
        self.assertTrue(harness.emptyStateHelpButton.visible)

        harness.fileTextbox.setText("loaded.db")
        harness._sync_empty_state()
        self.assertTrue(harness.emptyStateFrame.visible)
        self.assertEqual(harness.emptyStateTitle.text, "Waiting for measurements")
        self.assertIn("loaded.db is loaded", harness.emptyStateDetail.text)
        self.assertIn("checking every 1.5 s", harness.emptyStateDetail.text)
        self.assertTrue(harness.emptyStateLoadButton.visible)
        self.assertTrue(harness.emptyStateRefreshButton.visible)
        self.assertFalse(harness.emptyStateHelpButton.visible)

        harness.fileTextbox.setText("")
        harness.RunList.addRuns({1: {"guid": "guid-1"}})
        harness._sync_empty_state()
        self.assertFalse(harness.emptyStateFrame.visible)

        harness.RunList.clear()
        harness._database_load_active = True
        harness._sync_empty_state()
        self.assertFalse(harness.emptyStateFrame.visible)

    def test_loaded_empty_state_reports_manual_refresh(self):
        harness = self.Harness()
        harness.spinBox.setValue(0)

        detail = harness._loaded_empty_database_detail("manual.db")
        status = harness._loaded_empty_database_status("manual.db", 0.25)

        self.assertIn("Refresh is set to manual", detail)
        self.assertIn("refresh manually", status)
        self.assertEqual(
            harness._empty_database_refresh_status(),
            "No measurements found yet.",
            )


class RefreshMainEmptyDatabaseTestCase(unittest.TestCase):
    class ThreadPool:
        def __init__(self):
            self.workers = []

        def start(self, worker):
            self.workers.append(worker)

    class RunList:
        def __init__(self):
            self.maxRunId = 0
            self.checked_watching = False
            self.watching = []

        def checkWatching(self, statuses=None):
            self.checked_watching = True
            return {}

        def topLevelItemCount(self):
            return 0

    class Harness:
        refreshMain = main_window.MainWindow.refreshMain
        database_refresh_finished = main_window.MainWindow.database_refresh_finished
        _apply_database_refresh_result = (
            main_window.MainWindow._apply_database_refresh_result
            )
        _empty_database_refresh_status = (
            main_window.MainWindow._empty_database_refresh_status
            )
        _main_refresh_interval = main_window.MainWindow._main_refresh_interval
        _current_refresh_interval = main_window.MainWindow._current_refresh_interval

        def __init__(self):
            self.fileTextbox = DatabaseLoadUiTestCase.Field("empty.db")
            self.RunList = RefreshMainEmptyDatabaseTestCase.RunList()
            self.spinBox = DatabaseLoadUiTestCase.SpinBox(1.5)
            self.status_messages = []
            self.sync_count = 0
            self.databaseRefreshThreadPool = RefreshMainEmptyDatabaseTestCase.ThreadPool()

        def _sync_empty_state(self):
            self.sync_count += 1

        def show_status(self, message, timeout=5000):
            self.status_messages.append((message, timeout))

        def show_error(self, title, message, details=None):
            raise AssertionError((title, message, details))

    def test_refresh_empty_database_reports_waiting_state(self):
        harness = self.Harness()
        harness.refreshMain()
        harness.database_refresh_finished(
            harness._database_refresh_generation,
            "empty.db",
            {},
            {},
            None,
            )

        self.assertTrue(harness.RunList.checked_watching)
        self.assertEqual(harness.sync_count, 1)
        self.assertEqual(
            harness.status_messages,
            [
                ("Checking for new runs...", 0),
                ("No measurements found yet; still waiting for new runs.", 3000),
            ],
            )

    def test_repeated_refresh_requests_are_coalesced_while_worker_is_active(self):
        harness = self.Harness()

        harness.refreshMain()
        harness.refreshMain()

        self.assertEqual(len(harness.databaseRefreshThreadPool.workers), 1)
        self.assertTrue(harness._database_refresh_active)
        self.assertTrue(harness._database_refresh_pending)
        self.assertEqual(
            harness.status_messages[-1],
            ("Database refresh queued.", 3000),
            )

    def test_refresh_request_during_error_dialog_is_coalesced(self):
        harness = self.Harness()
        harness.refreshMain()

        def show_error(_title, _message, _details=None):
            harness.refreshMain()

        harness.show_error = show_error
        with patch(
                "qplot.windows._database_actions.QtCore.QTimer.singleShot",
                side_effect=lambda _delay, callback: callback(),
                ):
            harness.database_refresh_finished(
                harness._database_refresh_generation,
                "empty.db",
                {},
                {},
                RuntimeError("database temporarily unavailable"),
                )

        self.assertEqual(len(harness.databaseRefreshThreadPool.workers), 2)
        self.assertTrue(harness._database_refresh_active)


class RefreshMainPreviewUpdateTestCase(unittest.TestCase):
    class RunList:
        def __init__(self, updated_runs):
            self.maxRunId = 3
            self.updated_runs = updated_runs
            self.watching = []

        def checkWatching(self, statuses=None):
            return self.updated_runs

        def topLevelItemCount(self):
            return 1

    class Preview:
        def __init__(self):
            self.added_runs = []

        def add_runs(self, runs):
            self.added_runs.append(runs)

    class InfoBox:
        def __init__(self):
            self.preview = RefreshMainPreviewUpdateTestCase.Preview()

    class Harness:
        refreshMain = main_window.MainWindow.refreshMain
        database_refresh_finished = main_window.MainWindow.database_refresh_finished
        _apply_database_refresh_result = (
            main_window.MainWindow._apply_database_refresh_result
            )

        def __init__(self, updated_runs):
            self.fileTextbox = DatabaseLoadUiTestCase.Field("loaded.db")
            self.RunList = RefreshMainPreviewUpdateTestCase.RunList(updated_runs)
            self.infoBox = RefreshMainPreviewUpdateTestCase.InfoBox()
            self.status_messages = []
            self.sync_count = 0
            self.databaseRefreshThreadPool = RefreshMainEmptyDatabaseTestCase.ThreadPool()

        def _sync_empty_state(self):
            self.sync_count += 1

        def show_status(self, message, timeout=5000):
            self.status_messages.append((message, timeout))

        def show_error(self, title, message, details=None):
            raise AssertionError((title, message, details))

    def test_refresh_requeues_previews_for_updated_watched_runs(self):
        updated_runs = {
            4: {
                "guid": "guid-4",
                "result_count": 1000,
                "is_completed": True,
                },
            }
        harness = self.Harness(updated_runs)
        harness.refreshMain()
        harness.database_refresh_finished(
            harness._database_refresh_generation,
            "loaded.db",
            {},
            {"guid-4": updated_runs[4]},
            None,
            )

        self.assertEqual(harness.infoBox.preview.added_runs, [updated_runs])
        self.assertEqual(
            harness.status_messages[-1],
            ("No new runs found.", 3000),
            )


class RefreshMainAutoPlotTestCase(unittest.TestCase):
    class RunList:
        def __init__(self):
            self.maxRunId = 10
            self.checked_watching = False
            self.added_runs = None
            self.watching = []

        def checkWatching(self, statuses=None):
            self.checked_watching = True
            return {}

        def addRuns(self, runs):
            self.added_runs = runs

        def topLevelItemCount(self):
            return 1

    class Preview:
        def __init__(self):
            self.added_runs = None

        def add_runs(self, runs):
            self.added_runs = runs

    class InfoBox:
        def __init__(self):
            self.preview = RefreshMainAutoPlotTestCase.Preview()

    class AutoPlotBox:
        def __init__(self, checked):
            self.checked = checked

        def isChecked(self):
            return self.checked

    class Harness:
        refreshMain = main_window.MainWindow.refreshMain
        database_refresh_finished = main_window.MainWindow.database_refresh_finished
        _apply_database_refresh_result = (
            main_window.MainWindow._apply_database_refresh_result
            )

        def __init__(self, auto_plot_checked):
            self.fileTextbox = DatabaseLoadUiTestCase.Field("loaded.db")
            self.RunList = RefreshMainAutoPlotTestCase.RunList()
            self.infoBox = RefreshMainAutoPlotTestCase.InfoBox()
            self.autoPlotBox = RefreshMainAutoPlotTestCase.AutoPlotBox(
                auto_plot_checked
                )
            self.status_messages = []
            self.plotted_guids = []
            self.sync_count = 0
            self.databaseRefreshThreadPool = RefreshMainEmptyDatabaseTestCase.ThreadPool()

        def _sync_empty_state(self):
            self.sync_count += 1

        def show_status(self, message, timeout=5000):
            self.status_messages.append((message, timeout))

        def show_error(self, title, message, details=None):
            raise AssertionError((title, message, details))

        def openPlot(self, guid):
            self.plotted_guids.append(guid)

    def test_refresh_auto_plots_new_runs_when_enabled(self):
        new_runs = {
            11: {"guid": "guid-11", "run_timestamp": None},
            12: {"guid": "guid-12", "run_timestamp": 12.5},
            13: {"guid": "guid-13", "run_timestamp": 12.5},
            }
        harness = self.Harness(auto_plot_checked=True)
        harness.refreshMain()
        worker = harness.databaseRefreshThreadPool.workers[0]
        self.assertEqual(worker.last_run_id, 10)
        harness.database_refresh_finished(
            harness._database_refresh_generation,
            "loaded.db",
            new_runs,
            {},
            None,
            )
        self.assertTrue(harness.RunList.checked_watching)
        self.assertEqual(harness.RunList.maxRunId, 13)
        expected_runs = new_runs
        self.assertEqual(harness.RunList.added_runs, expected_runs)
        self.assertEqual(harness.infoBox.preview.added_runs, expected_runs)
        self.assertEqual(harness.sync_count, 1)
        self.assertEqual(
            harness.plotted_guids,
            ["guid-11", "guid-12", "guid-13"],
            )
        self.assertEqual(
            harness.status_messages,
            [
                ("Checking for new runs...", 0),
                ("Found 3 new runs.", 5000),
                ],
            )

    def test_refresh_does_not_auto_plot_new_runs_when_disabled(self):
        new_runs = {
            11: {"guid": "guid-11", "run_timestamp": 11.0},
            }
        harness = self.Harness(auto_plot_checked=False)
        harness.refreshMain()
        harness.database_refresh_finished(
            harness._database_refresh_generation,
            "loaded.db",
            new_runs,
            {},
            None,
            )

        self.assertEqual(harness.plotted_guids, [])
        self.assertEqual(
            harness.status_messages[-1],
            ("Found 1 new run.", 5000),
            )


class AutoPlotToggleTestCase(unittest.TestCase):
    class Config:
        def __init__(self):
            self.updates = []

        def update(self, key, value):
            self.updates.append((key, value))

    class RunList:
        def __init__(self, metadata):
            self.metadata = metadata

        def all_run_metadata(self):
            return self.metadata

    class Harness:
        _auto_plot_changed = main_window.MainWindow._auto_plot_changed
        _auto_plot_current_running_run = (
            main_window.MainWindow._auto_plot_current_running_run
            )

        def __init__(self, metadata):
            self.config = AutoPlotToggleTestCase.Config()
            self.RunList = AutoPlotToggleTestCase.RunList(metadata)
            self.plotted_guids = []

        def openPlot(self, guid):
            self.plotted_guids.append(guid)

    def test_enabling_auto_plot_opens_newest_incomplete_run(self):
        harness = self.Harness({
            1: {
                "guid": "older-running",
                "run_timestamp": 10.0,
                "is_completed": False,
                },
            2: {
                "guid": "complete",
                "run_timestamp": 12.0,
                "is_completed": True,
                },
            3: {
                "guid": "newer-running",
                "run_timestamp": 15.0,
                "is_completed": False,
                },
            })

        harness._auto_plot_changed(True)

        self.assertEqual(harness.config.updates, [(AUTO_PLOT_KEY, True)])
        self.assertEqual(harness.plotted_guids, ["newer-running"])

    def test_disabling_auto_plot_does_not_open_running_run(self):
        harness = self.Harness({
            1: {
                "guid": "running",
                "run_timestamp": 10.0,
                "is_completed": False,
                },
            })

        harness._auto_plot_changed(False)

        self.assertEqual(harness.config.updates, [(AUTO_PLOT_KEY, False)])
        self.assertEqual(harness.plotted_guids, [])


class CloudDatabasePrefetchTestCase(unittest.TestCase):
    def test_prefetch_subprocess_retries_transient_timeout_errors(self):
        class Handle:
            def __init__(self):
                self.read_calls = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def read(self, _chunk_size):
                self.read_calls += 1
                if self.read_calls == 1:
                    raise OSError(errno.ECANCELED, "Operation canceled")
                if self.read_calls == 2:
                    return b"database"
                return b""

        handle = Handle()
        output = []
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = str(Path(temp_dir) / "placeholder.db")
            Path(database_path).write_bytes(b"database")
            with (
                    patch(
                        "builtins.open",
                        side_effect=[
                            TimeoutError(errno.ETIMEDOUT, "Operation timed out"),
                            handle,
                            ],
                        ) as open_file,
                    patch(
                        "builtins.print",
                        side_effect=lambda value, **_kwargs: output.append(value),
                        ),
                    patch("time.sleep"),
                    patch.object(sys, "argv", ["prefetch", database_path]),
                    ):
                exec(database_module._database_prefetch_script(), {})

        self.assertEqual(open_file.call_count, 2)
        self.assertEqual(handle.read_calls, 3)
        self.assertEqual(output[-1], 8)

    def test_prefetch_database_file_reads_file_and_reports_cloud_sync_progress(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = str(Path(temp_dir) / "prefetch.db")
            Path(database_path).write_bytes(b"x" * 10)
            statuses = []
            old_label = database_module.database_cloud_storage_label
            database_module.database_cloud_storage_label = lambda _path: "OneDrive"
            try:
                bytes_read = database_module.prefetch_database_file(
                    database_path,
                    status_callback=statuses.append,
                    chunk_size=4,
                    status_interval=0,
                    )
            finally:
                database_module.database_cloud_storage_label = old_label

        self.assertEqual(bytes_read, 10)
        self.assertTrue(statuses[0].startswith("Waiting for OneDrive sync..."))
        self.assertIn("100% available", statuses[-1])

    def test_prefetch_database_file_with_timeout_uses_subprocess(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = str(Path(temp_dir) / "prefetch-timeout.db")
            Path(database_path).write_bytes(b"x" * 10)
            statuses = []
            old_label = database_module.database_cloud_storage_label
            database_module.database_cloud_storage_label = lambda _path: "OneDrive"
            try:
                bytes_read = database_module.prefetch_database_file_with_timeout(
                    database_path,
                    timeout=5,
                    status_callback=statuses.append,
                    )
            finally:
                database_module.database_cloud_storage_label = old_label

        self.assertEqual(bytes_read, 10)
        self.assertIn("Waiting for OneDrive sync...", statuses)
        self.assertIn("Waiting for OneDrive sync... 100% available", statuses)

    def test_prefetch_database_file_with_timeout_kills_stalled_process(self):
        old_popen = database_module.subprocess.Popen
        killed = []

        class Pipe:
            def __iter__(self):
                return iter(())

            def close(self):
                pass

        class Process:
            stdout = Pipe()
            stderr = Pipe()
            returncode = None

            def poll(self):
                return None

            def kill(self):
                killed.append(True)

            def wait(self):
                self.returncode = -9

        database_module.subprocess.Popen = lambda *args, **kwargs: Process()
        try:
            with self.assertRaises(TimeoutError) as caught:
                database_module.prefetch_database_file_with_timeout(
                    "OneDrive/test.db",
                    timeout=0.01,
                    status_callback=lambda _message: None,
                    )
        finally:
            database_module.subprocess.Popen = old_popen

        self.assertEqual(killed, [True])
        self.assertIn("Timed out after 0.01 s", str(caught.exception))
        self.assertIn("OneDrive", str(caught.exception))

    def test_prefetch_database_file_with_timeout_stops_when_cancelled(self):
        old_popen = database_module.subprocess.Popen
        killed = []

        class Pipe:
            def __iter__(self):
                return iter(())

            def close(self):
                pass

        class Process:
            stdout = Pipe()
            stderr = Pipe()
            returncode = None

            def poll(self):
                return None

            def kill(self):
                killed.append(True)

            def wait(self):
                self.returncode = -9

        database_module.subprocess.Popen = lambda *args, **kwargs: Process()
        try:
            with self.assertRaises(InterruptedError):
                database_module.prefetch_database_file_with_timeout(
                    "OneDrive/test.db",
                    timeout=5,
                    cancelled_callback=lambda: True,
                    )
        finally:
            database_module.subprocess.Popen = old_popen

        self.assertEqual(killed, [True])


class DatabaseRefreshWorkerTestCase(unittest.TestCase):
    def test_worker_fetches_new_runs_and_lightweight_live_statuses(self):
        results = []
        seen_status_calls = []

        def get_status(guid, **kwargs):
            seen_status_calls.append((guid, kwargs))
            return {"result_count": 12}

        worker = database_module.DatabaseRefreshWorker(
            4,
            "example.db",
            10,
            ["guid-1", "guid-2"],
            )
        worker.signals.finished.connect(lambda *args: results.append(args))

        with (
            patch.object(
                database_module,
                "find_new_runs",
                return_value={11: {"guid": "guid-11"}},
                ) as find_runs,
            patch.object(database_module, "get_run_status", side_effect=get_status),
            ):
            worker.run()

        find_runs.assert_called_once_with(
            10,
            database_path="example.db",
            cancelled_callback=ANY,
            )
        self.assertEqual(seen_status_calls, [
            ("guid-1", {
                "database_path": "example.db",
                "include_storage_bytes": False,
                "cancelled_callback": ANY,
                }),
            ("guid-2", {
                "database_path": "example.db",
                "include_storage_bytes": False,
                "cancelled_callback": ANY,
                }),
            ])
        self.assertEqual(results, [(
            4,
            "example.db",
            {11: {"guid": "guid-11"}},
            {
                "guid-1": {"result_count": 12},
                "guid-2": {"result_count": 12},
                },
            None,
            )])


class DatabaseLoadWorkerTestCase(unittest.TestCase):
    def test_database_load_worker_opens_database_read_only_and_returns_runs(self):
        old_access_error = database_module.database_access_error
        old_get_runs = database_module.get_runs_basic_via_sql
        calls = []

        def access_error(database_path):
            calls.append(("access", database_path))
            return None

        def get_runs(database_path, cancelled_callback=None):
            self.assertTrue(callable(cancelled_callback))
            calls.append(("basic_runs", database_path))
            return {1: {"guid": "guid-1", "run_timestamp": 123.0}}

        database_module.database_access_error = access_error
        database_module.get_runs_basic_via_sql = get_runs
        try:
            worker = main_window.DatabaseLoadWorker(7, "example.db")
            statuses = []
            finished = []
            worker.signals.status.connect(lambda *args: statuses.append(args))
            worker.signals.finished.connect(lambda *args: finished.append(args))

            worker.run()
        finally:
            database_module.database_access_error = old_access_error
            database_module.get_runs_basic_via_sql = old_get_runs

        self.assertEqual(calls, [
            ("access", "example.db"),
            ("basic_runs", "example.db"),
            ])
        self.assertEqual(statuses, [
            (7, "Checking database access..."),
            (7, "Opening database read-only..."),
            (7, "Loading basic run list..."),
            ])
        self.assertEqual(finished, [
            (7, "example.db", {1: {"guid": "guid-1", "run_timestamp": 123.0}}, None)
            ])

    def test_database_load_worker_reports_access_error(self):
        old_access_error = database_module.database_access_error

        database_module.database_access_error = lambda _path: "locked database"
        try:
            worker = main_window.DatabaseLoadWorker(3, "locked.db")
            finished = []
            worker.signals.finished.connect(lambda *args: finished.append(args))

            worker.run()
        finally:
            database_module.database_access_error = old_access_error

        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0][:3], (3, "locked.db", {}))
        self.assertIsInstance(finished[0][3], RuntimeError)
        self.assertIn("locked database", str(finished[0][3]))

    def test_failed_database_load_preserves_active_database(self):
        active_database = get_DB_location()
        old_access_error = database_module.database_access_error
        old_get_runs = database_module.get_runs_basic_via_sql

        set_qcodes_database_location("active.db")
        database_module.database_access_error = lambda _path: None

        def fail_to_get_runs(_database_path, cancelled_callback=None):
            self.assertTrue(callable(cancelled_callback))
            raise RuntimeError("broken run table")

        database_module.get_runs_basic_via_sql = fail_to_get_runs
        try:
            worker = main_window.DatabaseLoadWorker(10, "failed.db")
            worker.run()

            self.assertEqual(get_DB_location(), "active.db")
        finally:
            database_module.database_access_error = old_access_error
            database_module.get_runs_basic_via_sql = old_get_runs
            set_qcodes_database_location(active_database)

    def test_cancelled_database_load_preserves_active_database(self):
        active_database = get_DB_location()
        old_access_error = database_module.database_access_error
        old_get_runs = database_module.get_runs_basic_via_sql

        set_qcodes_database_location("active.db")
        database_module.database_access_error = lambda _path: None
        try:
            worker = main_window.DatabaseLoadWorker(11, "cancelled.db")

            def cancel_while_getting_runs(_database_path, cancelled_callback=None):
                self.assertTrue(callable(cancelled_callback))
                worker.cancel()
                return {}

            database_module.get_runs_basic_via_sql = cancel_while_getting_runs
            worker.run()

            self.assertEqual(get_DB_location(), "active.db")
        finally:
            database_module.database_access_error = old_access_error
            database_module.get_runs_basic_via_sql = old_get_runs
            set_qcodes_database_location(active_database)

    def test_stale_database_load_preserves_active_database(self):
        active_database = get_DB_location()
        old_access_error = database_module.database_access_error
        old_get_runs = database_module.get_runs_basic_via_sql

        set_qcodes_database_location("active.db")
        database_module.database_access_error = lambda _path: None
        database_module.get_runs_basic_via_sql = lambda _path, **_kwargs: {}
        try:
            harness = DatabaseLoadUiTestCase.Harness()
            harness._database_load_generation = 13
            worker = main_window.DatabaseLoadWorker(12, "stale.db")
            worker.signals.finished.connect(
                lambda *args: harness.database_load_finished(*args)
                )

            worker.run()

            self.assertEqual(get_DB_location(), "active.db")
        finally:
            database_module.database_access_error = old_access_error
            database_module.get_runs_basic_via_sql = old_get_runs
            set_qcodes_database_location(active_database)

    def test_current_successful_database_load_commits_database(self):
        active_database = get_DB_location()
        old_access_error = database_module.database_access_error
        old_get_runs = database_module.get_runs_basic_via_sql
        runs = {1: {"guid": "guid-1", "run_timestamp": 123.0}}

        set_qcodes_database_location("active.db")
        database_module.database_access_error = lambda _path: None
        database_module.get_runs_basic_via_sql = lambda _path, **_kwargs: runs
        try:
            harness = DatabaseLoadUiTestCase.Harness()
            harness._database_load_generation = 14
            harness._database_load_active = True
            harness._database_load_state = {"abspath": "committed.db"}
            harness.fileTextbox.setText("active.db")
            worker = main_window.DatabaseLoadWorker(14, "committed.db")
            worker.signals.finished.connect(
                lambda *args: harness.database_load_finished(*args)
                )

            worker.run()

            self.assertEqual(get_DB_location(), "committed.db")
            self.assertEqual(harness.remembered_databases, ["committed.db"])
            self.assertEqual(harness.detail_loads, [("committed.db", runs)])
        finally:
            database_module.database_access_error = old_access_error
            database_module.get_runs_basic_via_sql = old_get_runs
            set_qcodes_database_location(active_database)

    def test_database_load_worker_does_not_start_when_cancelled(self):
        old_placeholder = database_module.database_is_likely_cloud_placeholder
        old_access_error = database_module.database_access_error
        calls = []

        database_module.database_is_likely_cloud_placeholder = lambda _path: calls.append(
            "placeholder"
            )
        database_module.database_access_error = lambda _path: calls.append("access")
        try:
            worker = main_window.DatabaseLoadWorker(4, "example.db")
            finished = []
            worker.signals.finished.connect(lambda *args: finished.append(args))

            worker.cancel()
            worker.run()
        finally:
            database_module.database_is_likely_cloud_placeholder = old_placeholder
            database_module.database_access_error = old_access_error

        self.assertEqual(calls, [])
        self.assertEqual(finished, [])

    def test_database_load_worker_stops_after_cancelled_prefetch(self):
        old_placeholder = database_module.database_is_likely_cloud_placeholder
        old_prefetch = database_module.prefetch_database_file_with_timeout
        old_access_error = database_module.database_access_error
        calls = []

        def prefetch(
                database_path,
                timeout=None,
                status_callback=None,
                cancelled_callback=None,
                ):
            calls.append(("prefetch", database_path, timeout))
            raise InterruptedError("Database load cancelled.")

        database_module.database_is_likely_cloud_placeholder = lambda _path: True
        database_module.prefetch_database_file_with_timeout = prefetch
        database_module.database_access_error = lambda _path: calls.append("access")
        try:
            worker = main_window.DatabaseLoadWorker(5, "cloud.db", 8)
            finished = []
            worker.signals.finished.connect(lambda *args: finished.append(args))

            worker.run()
        finally:
            database_module.database_is_likely_cloud_placeholder = old_placeholder
            database_module.prefetch_database_file_with_timeout = old_prefetch
            database_module.database_access_error = old_access_error

        self.assertEqual(calls, [("prefetch", "cloud.db", 8)])
        self.assertEqual(finished, [])

    def test_database_load_worker_ignores_deleted_qt_signals_at_shutdown(self):
        class DeletedSignal:
            def emit(self, *args):
                raise RuntimeError(
                    "wrapped C/C++ object of type DatabaseLoadSignals has been deleted"
                    )

        class DeletedSignals:
            status = DeletedSignal()
            finished = DeletedSignal()

        worker = main_window.DatabaseLoadWorker(6, "example.db")
        worker.signals = DeletedSignals()

        worker._emit_status("Checking database access...")
        worker._emit_finished({}, None)

    def test_database_load_worker_waits_for_cloud_sync_and_retries_probe(self):
        old_access_error = database_module.database_access_error
        old_label = database_module.database_cloud_storage_label
        old_placeholder = database_module.database_is_likely_cloud_placeholder
        old_prefetch = database_module.prefetch_database_file_with_timeout
        old_get_runs = database_module.get_runs_basic_via_sql
        calls = []

        access_results = iter(["timed out", None])

        def access_error(database_path):
            calls.append(("access", database_path))
            return next(access_results)

        def prefetch(
                database_path,
                timeout=None,
                status_callback=None,
                cancelled_callback=None,
                ):
            calls.append(("prefetch", database_path, timeout))
            self.assertIsNotNone(cancelled_callback)
            status_callback("Waiting for OneDrive sync... 100% available")
            return 10

        database_module.database_access_error = access_error
        database_module.database_cloud_storage_label = lambda _path: "OneDrive"
        database_module.database_is_likely_cloud_placeholder = lambda _path: False
        database_module.prefetch_database_file_with_timeout = prefetch
        database_module.get_runs_basic_via_sql = lambda _path, **_kwargs: {}
        try:
            with tempfile.NamedTemporaryFile(suffix=".db") as database:
                worker = main_window.DatabaseLoadWorker(9, database.name, 12)
                statuses = []
                finished = []
                worker.signals.status.connect(lambda *args: statuses.append(args))
                worker.signals.finished.connect(lambda *args: finished.append(args))

                worker.run()
                expected_path = database.name
        finally:
            database_module.database_access_error = old_access_error
            database_module.database_cloud_storage_label = old_label
            database_module.database_is_likely_cloud_placeholder = old_placeholder
            database_module.prefetch_database_file_with_timeout = old_prefetch
            database_module.get_runs_basic_via_sql = old_get_runs

        self.assertEqual(calls, [
            ("access", expected_path),
            ("prefetch", expected_path, 12),
            ("access", expected_path),
            ])
        self.assertIn((9, "Waiting for OneDrive sync... 100% available"), statuses)
        self.assertEqual(finished, [(9, expected_path, {}, None)])

    def test_database_detail_worker_emits_incremental_batches(self):
        old_iter_details = database_module.iter_run_detail_batches_via_sql
        calls = []

        def iter_details(
                database_path,
                run_ids,
                batch_size=1,
                infer_missing_shapes=True,
                include_storage_bytes=True,
                include_storage_estimate=False,
                include_read_setpoint_count=True,
                cancelled_callback=None,
                ):
            self.assertTrue(callable(cancelled_callback))
            calls.append((
                database_path,
                run_ids,
                batch_size,
                infer_missing_shapes,
                include_storage_bytes,
                include_storage_estimate,
                include_read_setpoint_count,
                ))
            for run_id in run_ids:
                if run_id == 2:
                    yield {
                        2: {
                            "guid": "guid-2",
                            "result_count": 20,
                            "storage_bytes": 1000,
                            }
                        }
                elif run_id == 1:
                    yield {1: {"guid": "guid-1", "result_count": 10}}

        database_module.iter_run_detail_batches_via_sql = iter_details
        try:
            worker = main_window.DatabaseDetailWorker(
                11,
                "details.db",
                [2, 1],
                batch_size=1,
                )
            statuses = []
            batches = []
            finished = []
            worker.signals.status.connect(lambda *args: statuses.append(args))
            worker.signals.batch_ready.connect(lambda *args: batches.append(args))
            worker.signals.finished.connect(lambda *args: finished.append(args))
            worker.prioritize_run_ids([1])

            worker.run()
        finally:
            database_module.iter_run_detail_batches_via_sql = old_iter_details

        self.assertEqual(calls, [
            ("details.db", [1], 1, False, False, True, False),
            ("details.db", [2], 1, False, False, True, False),
            ])
        self.assertEqual(batches, [
            (11, "details.db", {1: {"guid": "guid-1", "result_count": 10}}),
            (11, "details.db", {2: {"guid": "guid-2", "result_count": 20, "storage_bytes": 1000}}),
            ])
        self.assertEqual(statuses, [
            (11, "Loading run details... 0/2"),
            (11, "Loading run details... 1/2"),
            (11, "Loading run details... 2/2"),
            ])
        self.assertEqual(finished, [(11, "details.db", None)])

    def test_database_expensive_detail_worker_prioritizes_shape_and_storage_batches(self):
        old_iter_shapes = database_module.iter_run_shape_batches_via_sql
        old_iter_storage = database_module.iter_run_storage_batches_via_sql
        calls = []

        def iter_shapes(
                database_path,
                run_ids,
                batch_size=1,
                cancelled_callback=None,
                ):
            self.assertTrue(callable(cancelled_callback))
            calls.append(("shapes", database_path, run_ids, batch_size))
            if 1 in run_ids:
                yield {1: {"guid": "guid-1", "setpoint_shape": [10], "setpoint_count": 10}}

        def iter_storage(
                database_path,
                run_ids,
                batch_size=25,
                cancelled_callback=None,
                ):
            self.assertTrue(callable(cancelled_callback))
            calls.append(("storage", database_path, run_ids, batch_size))
            if 1 in run_ids:
                yield {1: {"guid": "guid-1", "storage_bytes": 2000}}

        database_module.iter_run_shape_batches_via_sql = iter_shapes
        database_module.iter_run_storage_batches_via_sql = iter_storage
        try:
            worker = main_window.DatabaseExpensiveDetailWorker(
                12,
                "details.db",
                [2, 1],
                batch_size=2,
                )
            statuses = []
            batches = []
            finished = []
            worker.signals.status.connect(lambda *args: statuses.append(args))
            worker.signals.batch_ready.connect(lambda *args: batches.append(args))
            worker.signals.finished.connect(lambda *args: finished.append(args))
            worker.prioritize_run_ids([1])

            worker.run()
        finally:
            database_module.iter_run_shape_batches_via_sql = old_iter_shapes
            database_module.iter_run_storage_batches_via_sql = old_iter_storage

        self.assertEqual(calls, [
            ("shapes", "details.db", [1, 2], 2),
            ("storage", "details.db", [1, 2], 25),
            ])
        self.assertEqual(batches, [
            (12, "details.db", {1: {"guid": "guid-1", "setpoint_shape": [10], "setpoint_count": 10}}),
            (12, "details.db", {1: {"guid": "guid-1", "storage_bytes": 2000}}),
            ])
        self.assertEqual(statuses, [
            (12, "Loading setpoint shapes... 0/2"),
            (12, "Loading setpoint shapes... 2/2"),
            (12, "Loading exact run sizes... 0/2"),
            (12, "Loading exact run sizes... 2/2"),
            ])
        self.assertEqual(finished, [(12, "details.db", None)])


class DatabaseLoadRequestTestCase(unittest.TestCase):
    def test_load_database_path_rejects_missing_file_before_starting_worker(self):
        class Harness:
            load_database_path = main_window.MainWindow.load_database_path

            def __init__(self):
                self.errors = []

            def show_error(self, title, message, details=None):
                self.errors.append((title, message, details))

        harness = Harness()

        self.assertFalse(harness.load_database_path("missing.db"))
        self.assertEqual(harness.errors[0][0], "Database Load Failed")
        self.assertIn("could not be found", harness.errors[0][1])


class DatabaseDropTestCase(unittest.TestCase):
    def test_database_info_report_summarises_qcodes_tables(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = str(Path(temp_dir) / "info.db")
            conn = sqlite3.connect(database_path)
            try:
                conn.execute(
                    "CREATE TABLE experiments (exp_id INTEGER PRIMARY KEY, name TEXT, sample_name TEXT)"
                    )
                conn.execute("""
                  CREATE TABLE runs (
                      run_id INTEGER PRIMARY KEY,
                      name TEXT,
                      run_timestamp REAL,
                      completed_timestamp REAL,
                      is_completed INTEGER,
                      guid TEXT
                  )
                """)
                conn.execute(
                    "INSERT INTO experiments (exp_id, name, sample_name) VALUES (1, 'exp', 'sample')"
                    )
                conn.execute(
                    """
                    INSERT INTO runs
                    (run_id, name, run_timestamp, completed_timestamp, is_completed, guid)
                    VALUES (3, 'measurement', 1768129603, 1768129626, 1, 'guid-3')
                    """
                    )
                conn.commit()
            finally:
                conn.close()

            report = main_window.database_info_report(database_path)
            rows = database_module.database_info_rows(database_path)

        self.assertIn("Runs: 1", report)
        self.assertIn("Experiments: 1", report)
        self.assertIn("Latest run ID: 3", report)
        self.assertIn("Latest run GUID: guid-3", report)
        self.assertIn(("Runs", "1"), rows)
        self.assertIn(("Latest run GUID", "guid-3"), rows)
        self.assertIn("Database schema version:", report)
        self.assertIn("Last modified:", report)
        self.assertNotIn("Selected run ID:", report)
        self.assertNotIn("Installed QCoDeS version:", report)
        self.assertNotIn("QCoDeS active database:", report)
        self.assertNotIn("SQLite version:", report)

    def test_database_info_dialog_displays_copyable_table(self):
        dialog = database_actions.DatabaseInfoDialog([
            ("Database", "demo.db"),
            ("Path", "C:/data/demo.db"),
            ])

        try:
            table = dialog.table

            self.assertIsInstance(table, database_actions.CopyableTableWidget)
            self.assertEqual(dialog.windowTitle(), "Database Information")
            self.assertEqual(table.objectName(), "databaseInfoTable")
            self.assertEqual(
                [table.horizontalHeaderItem(col).text() for col in range(2)],
                ["Field", "Value"],
                )
            self.assertEqual(table.selectionBehavior(), qtw.QAbstractItemView.SelectionBehavior.SelectRows)
            self.assertEqual(table.item(0, 0).text(), "Database")
            self.assertEqual(table.item(0, 1).text(), "demo.db")

            table.selectRow(1)
            table.copySelection()

            self.assertEqual(qtw.QApplication.clipboard().text(), "Path\tC:/data/demo.db")

            dialog.copyAll()

            self.assertEqual(
                qtw.QApplication.clipboard().text(),
                "Database\tdemo.db\nPath\tC:/data/demo.db",
                )
        finally:
            dialog.deleteLater()

    def test_database_path_from_mime_data_accepts_one_local_db_file(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as database:
            mime_data = QtCore.QMimeData()
            mime_data.setUrls([QtCore.QUrl.fromLocalFile(database.name)])

            self.assertEqual(
                main_window.database_path_from_mime_data(mime_data),
                database.name
                )

    def test_database_path_from_mime_data_rejects_ambiguous_or_non_db_drops(self):
        with (
            tempfile.NamedTemporaryFile(suffix=".db") as database,
            tempfile.NamedTemporaryFile(suffix=".txt") as text_file,
        ):
            text_drop = QtCore.QMimeData()
            text_drop.setUrls([QtCore.QUrl.fromLocalFile(text_file.name)])

            multiple_drop = QtCore.QMimeData()
            multiple_drop.setUrls([
                QtCore.QUrl.fromLocalFile(database.name),
                QtCore.QUrl.fromLocalFile(text_file.name),
                ])

            self.assertIsNone(main_window.database_path_from_mime_data(text_drop))
            self.assertIsNone(main_window.database_path_from_mime_data(multiple_drop))
