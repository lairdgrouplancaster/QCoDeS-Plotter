import unittest

from PyQt6 import QtCore

from qplot.windows import main as main_window
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
            "source-guid",
            "signal",
            param=source_param
            )

        self.assertTrue(added)
        self.assertEqual(target.option_boxes[0].option_box.index, 0)
        self.assertTrue(source.closed)
        self.assertEqual(harness.status_messages, [])


