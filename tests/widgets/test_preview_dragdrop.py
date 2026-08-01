import unittest

from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from qplot.windows import main as main_window
from qplot.windows._dataset_handle import DatasetKey, TraceKey
from qplot.windows._dragdrop import (
    make_run_preview_mime,
    preview_drop_is_compatible,
    run_preview_payload_from_mime,
    )


class RunPreviewDragDropTestCase(unittest.TestCase):
    def test_run_preview_drag_payload_round_trips_and_checks_axes(self):
        payload = run_preview_payload_from_mime(
            make_run_preview_mime("run-guid", "signal", ["x"])
            )

        self.assertEqual(payload, {
            "guid": "run-guid",
            "parameter": "signal",
            "axes": ["x"],
            })
        self.assertTrue(preview_drop_is_compatible(("x",), payload))
        self.assertFalse(preview_drop_is_compatible(("y",), payload))
        self.assertFalse(preview_drop_is_compatible(("x", "y"), payload))
        self.assertIsNone(run_preview_payload_from_mime(QtCore.QMimeData()))

    def test_add_trace_to_plot_uses_existing_add_path(self):
        class Param:
            def __init__(self, name, depends_on):
                self.name = name
                self.depends_on = depends_on
                self.depends_on_ = (depends_on,)

        class Dataset:
            def __init__(self, guid):
                self.guid = guid
                self.running = False

        class Combo:
            def __init__(self, items):
                self.items = items
                self.index = None

            def findText(self, text):
                try:
                    return self.items.index(text)
                except ValueError:
                    return -1

            def setCurrentIndex(self, index):
                self.index = index

        class Box:
            def __init__(self, items):
                self.option_box = Combo(items)

            def isEnabled(self):
                return True

        class Window:
            def __init__(self, guid, param, label):
                self.ds = Dataset(guid)
                self._dataset_key = DatasetKey("database.db", guid)
                self._trace_key = (self._dataset_key, param.name)
                self.param = param
                self.label = label
                self.visible = True
                self.closed = False

            def close(self):
                self.closed = True

        class Harness:
            _plot_window_for_param = main_window.MainWindow._plot_window_for_param
            add_trace_to_plot = main_window.MainWindow.add_trace_to_plot

            def __init__(self):
                self.status_messages = []
                self.errors = []

            def show_status(self, message, timeout=5000):
                self.status_messages.append((message, timeout))

            def show_error(self, title, message, details=None):
                self.errors.append((title, message, details))

            def get_1d_wins(self, win):
                pass

        source_param = Param("signal", "x")
        target_param = Param("target", "x")
        source = Window("source-guid", source_param, "ID:1 signal")
        target = Window("target-guid", target_param, "ID:2 target")
        target.option_boxes = [Box([source.label])]

        harness = Harness()
        harness.windows = [target, source]

        added = harness.add_trace_to_plot(
            target,
            source._dataset_key,
            "signal",
            param=source_param
            )

        self.assertTrue(added)
        self.assertEqual(target.option_boxes[0].option_box.index, 0)
        self.assertFalse(source.closed)
        self.assertEqual(harness.status_messages, [])

    def test_add_trace_selects_exact_same_label_source_and_keeps_it_open(self):
        class Param:
            name = "signal"
            depends_on = "x"
            depends_on_ = ("x",)

        class Box:
            def __init__(self, items):
                self.option_box = qtw.QComboBox()
                for label, trace_key in items:
                    self.option_box.addItem(label, trace_key)

            def isEnabled(self):
                return True

        class Window:
            def __init__(self, database_path):
                self._dataset_key = DatasetKey(database_path, "shared-guid")
                self._trace_key = TraceKey(self._dataset_key, Param.name)
                self.param = Param()
                self.label = "ID:1 signal"
                self.visible = True
                self.closed = False

            def close(self):
                self.closed = True

        class Harness:
            _plot_window_for_param = main_window.MainWindow._plot_window_for_param
            add_trace_to_plot = main_window.MainWindow.add_trace_to_plot

            def show_status(self, *_args):
                pass

            def show_error(self, *_args):
                self.errors.append(_args)

            def get_1d_wins(self, _win):
                pass

        first = Window("database-a.db")
        second = Window("database-b.db")
        target = type(
            "Target",
            (),
            {
                "param": Param(),
                "label": "ID:2 target",
                "option_boxes": [
                    Box([
                        (first.label, first._trace_key),
                        (second.label, second._trace_key),
                    ])
                ],
            },
        )()
        harness = Harness()
        harness.errors = []
        harness.windows = [target, first, second]

        added = harness.add_trace_to_plot(
            target,
            second._dataset_key,
            Param.name,
            param=second.param,
        )

        self.assertTrue(added)
        self.assertEqual(target.option_boxes[0].option_box.currentIndex(), 1)
        self.assertEqual(
            target.option_boxes[0].option_box.currentData(),
            second._trace_key,
        )
        self.assertFalse(second.closed)
        self.assertEqual(harness.errors, [])

    def test_add_trace_closes_temporary_hidden_source(self):
        class Param:
            name = "signal"
            depends_on = "x"
            depends_on_ = ("x",)

        class Combo:
            def __init__(self, trace_key):
                self.trace_key = trace_key
                self.index = None

            def count(self):
                return 1

            def itemData(self, _index):
                return self.trace_key

            def findText(self, _text):
                return -1

            def setCurrentIndex(self, index):
                self.index = index

        class Box:
            def __init__(self, trace_key):
                self.option_box = Combo(trace_key)

            def isEnabled(self):
                return True

        source_key = DatasetKey("database.db", "source-guid")
        source = type(
            "Source",
            (),
            {
                "_dataset_key": source_key,
                "_trace_key": (source_key, Param.name),
                "param": Param(),
                "label": "ID:1 signal",
                "visible": False,
                "closed": False,
                "close": lambda self: setattr(self, "closed", True),
            },
        )()
        target = type(
            "Target",
            (),
            {
                "param": Param(),
                "label": "ID:2 target",
                "option_boxes": [Box(source._trace_key)],
            },
        )()
        harness = type(
            "Harness",
            (),
            {
                "add_trace_to_plot": main_window.MainWindow.add_trace_to_plot,
                "_plot_window_for_param": lambda *_args: source,
                "get_1d_wins": lambda *_args: None,
                "show_status": lambda *_args: None,
                "show_error": lambda *_args: None,
                "windows": [target, source],
            },
        )()

        added = harness.add_trace_to_plot(
            target,
            source_key,
            Param.name,
            param=source.param,
        )

        self.assertTrue(added)
        self.assertTrue(source.closed)
