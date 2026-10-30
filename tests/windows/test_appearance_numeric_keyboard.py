"""Appearance numeric edits commit intact through real Qt keyboard events."""

import json
import os
import sys
import time
from collections import deque
from contextlib import contextmanager

import numpy as np
import pytest
from PyQt6 import QtCore, QtGui, QtTest, sip
from PyQt6 import QtWidgets as qtw

from tests.windows.test_plot_integration import wait_for
from tests.windows.test_refresh_interval_precision import live_view, wait_for_plot_load
from tests.windows.test_typed_run_keyboard_launch import (
    _drain,
    _launch,
    _type_run,
)
from tests.windows.test_typed_run_keyboard_launch import (
    typed_run_window as _typed_run_window,
)

typed_run_window = _typed_run_window


class _NumericEditTrace(QtCore.QObject):
    """Observe one pending edit without delivering or consuming Qt events."""

    FOCUS_EVENTS = (QtCore.QEvent.Type.FocusIn, QtCore.QEvent.Type.FocusOut)
    WINDOW_EVENTS = (QtCore.QEvent.Type.Show, QtCore.QEvent.Type.Hide,
                     QtCore.QEvent.Type.WindowActivate, QtCore.QEvent.Type.WindowDeactivate)
    KEY_EVENTS = (QtCore.QEvent.Type.KeyPress, QtCore.QEvent.Type.KeyRelease)

    def __init__(self, plot, dialog, spin, style_name):
        super().__init__()
        self.plot, self.dialog, self.spin = plot, dialog, spin
        self.editor = spin.lineEdit()
        self.style_name = style_name
        self.started = time.monotonic_ns()
        self.events = deque(maxlen=64)
        self.checkpoints = {}
        self.total_events = 0
        self.phase = "setup"

    @staticmethod
    def identity(obj):
        if obj is None:
            return None
        try:
            title = obj.windowTitle() if isinstance(obj, qtw.QWidget) and obj.isWindow() else ""
            if isinstance(obj, QtGui.QWindow):
                title = obj.title()
            return f"{type(obj).__name__}@{id(obj):x}:{obj.objectName()[:40]}:{title[:80]}"
        except RuntimeError:
            return "<deleted Qt object>"

    @staticmethod
    def callers():
        callers = []
        frame = sys._getframe(1)
        try:
            for _index in range(3):
                if frame is None:
                    break
                code = frame.f_code
                callers.append(f"{os.path.basename(code.co_filename)}:{frame.f_lineno}:{code.co_name}")
                frame = frame.f_back
        finally:
            del frame
        return callers[::-1]

    def record(self, kind, **detail):
        app = qtw.QApplication.instance()
        self.total_events += 1
        entry = {"ns": time.monotonic_ns() - self.started,
                 "phase": self.phase, "event": kind, **detail}
        try:
            native = self.dialog.windowHandle()
            style = self.plot._trace_styles.get(self.plot.label)
            entry.update(
                text=self.editor.text(), focus=self.editor.hasFocus(),
                value=self.spin.value(), style=getattr(style, self.style_name, None),
                active=self.identity(app.activeWindow()),
                focused=self.identity(app.focusWidget()),
                focus_window=self.identity(app.focusWindow()),
                native_active=native.isActive() if native is not None else None,
                native_exposed=native.isExposed() if native is not None else None,
                worker=hex(id(self.plot.worker)),
                worker_running=getattr(self.plot.worker, "running", None),
            )
        except RuntimeError as error:
            entry["snapshot_error"] = str(error)[:160]
        self.events.append(entry)

    def checkpoint(self, phase):
        self.phase = phase
        self.record("checkpoint")
        self.checkpoints[phase] = dict(self.events[-1])

    def eventFilter(self, obj, event):
        kind = event.type()
        focus = kind in self.FOCUS_EVENTS
        window = (kind in self.WINDOW_EVENTS
                  and (isinstance(obj, QtGui.QWindow)
                       or isinstance(obj, qtw.QWidget) and obj.isWindow()))
        if focus or window:
            self.record(kind.name, subject=self.identity(obj),
                        reason=event.reason().name if focus else None,
                        spontaneous=event.spontaneous())
        elif kind in self.KEY_EVENTS and obj in (self.spin, self.editor):
            self.record(kind.name, subject=self.identity(obj), key=event.key(), modifiers=event.modifiers().value,
                        key_text=event.text(), spontaneous=event.spontaneous())
        return False

    def report(self):
        app = qtw.QApplication.instance()
        rows = list(self.events)
        payload = {"pid": os.getpid(), "parent_pid": os.getppid(),
                   "worker": os.environ.get("PYTEST_XDIST_WORKER", "main"),
                   "platform": app.platformName(), "qt_runtime": QtCore.qVersion(),
                   "qt_compile": QtCore.QT_VERSION_STR, "python": sys.version,
                   "field": self.style_name, "checkpoints": dict(self.checkpoints), "events": rows}
        # The Windows wrapper retains only the last 32 KiB of pytest output.
        while True:
            payload["dropped_events"] = self.total_events - len(rows)
            encoded = json.dumps(payload, separators=(",", ":"))
            if len(encoded) <= 12000:
                break
            if rows:
                rows.pop(0)
            elif payload["checkpoints"]:
                payload["checkpoints"].pop(next(iter(payload["checkpoints"])))
            else:
                encoded = json.dumps({"diagnostic_error": "trace metadata exceeded output bound"})
                break
        print("QPLOT_NUMERIC_EDIT_TRACE " + encoded, flush=True)


@contextmanager
def _numeric_edit_diagnostics(plot, dialog, spin, style_name):
    trace = _NumericEditTrace(plot, dialog, spin, style_name)
    app = qtw.QApplication.instance()
    connections = []
    patches = []
    missing = object()
    failure_active = False

    def watch_method(obj, name):
        original = getattr(obj, name)
        patches.append((obj, name, obj.__dict__.get(name, missing)))

        def call(*args, **kwargs):
            trace.record(name, arguments=[str(value)[:60] for value in args],
                         callers=trace.callers())
            result = original(*args, **kwargs)
            if name == "refreshPlot":
                trace.record("refreshPlot.return")
            return result

        setattr(obj, name, call)

    try:
        for name in ("refresh_rows", "_sync_controls_from_selection"):
            watch_method(dialog, name)
        watch_method(spin, "setValue")
        watch_method(plot, "refreshPlot")
        for name, owner, signal in (("editingFinished", spin, spin.editingFinished),
                                    ("valueChanged", spin, spin.valueChanged),
                                    ("textChanged", trace.editor, trace.editor.textChanged),
                                    ("applicationStateChanged", app, app.applicationStateChanged)):
            def record_signal(*args, name=name):
                trace.record(name, arguments=[str(value)[:60] for value in args],
                             callers=trace.callers())

            signal.connect(record_signal)
            connections.append((owner, signal, record_signal))

        def focus_changed(old, new):
            trace.record("focusChanged", old=trace.identity(old), new=trace.identity(new))

        def focus_window_changed(window):
            trace.record("focusWindowChanged", new=trace.identity(window))

        for signal, callback in ((app.focusChanged, focus_changed),
                                 (app.focusWindowChanged, focus_window_changed)):
            signal.connect(callback)
            connections.append((app, signal, callback))
        app.installEventFilter(trace)
        trace.checkpoint("before_toggle")
        yield trace.checkpoint
    except BaseException:
        failure_active = True
        try:
            trace.checkpoint("failure")
            trace.report()
        except BaseException:
            # A diagnostic failure must preserve the original test failure.
            pass
        raise
    finally:
        cleanup_errors = []

        def clean(action):
            try:
                action()
            except BaseException as error:
                cleanup_errors.append(error)

        clean(lambda: app.removeEventFilter(trace))
        for owner, signal, callback in connections:
            def disconnect(owner=owner, signal=signal, callback=callback):
                if sip.isdeleted(owner):
                    return
                try:
                    signal.disconnect(callback)
                except (RuntimeError, TypeError):
                    # The owner may already have disconnected this signal.
                    pass

            clean(disconnect)
        for obj, name, original in reversed(patches):
            def restore(obj=obj, name=name, original=original):
                if original is missing:
                    obj.__dict__.pop(name, None)
                else:
                    obj.__dict__[name] = original

            clean(restore)
        if cleanup_errors and not failure_active:
            raise cleanup_errors[0]


@pytest.fixture
def trace_editor(typed_run_window):
    window, runs = typed_run_window
    assert window._database_access_mode == "trusted_live"
    for run_id, _guid, _offset in runs:
        _type_run(window, run_id)
        _launch(window, "Key_Return")
        _drain(window)
    host, source = window.windows
    assert window.add_trace_to_plot(
        host, source._dataset_key, source.param.name, param=source.param,
    )
    host.appearance_action.trigger()
    dialog = host._plot_appearance_dialog
    assert dialog.isVisible()
    lines = [host.line, next(line for line in host.lines.values() if line is not host.line)]
    originals = [tuple(np.array(values, copy=True) for values in line.getOriginalDataset())
                 for line in lines]
    yield host, dialog, lines
    for line, original in zip(lines, originals, strict=True):
        for values, expected in zip(line.getOriginalDataset(), original, strict=True):
            np.testing.assert_array_equal(values, expected)


def _select_line(host, dialog, line, modifier=QtCore.Qt.KeyboardModifier.NoModifier):
    key = next(key for key, candidate in host.lines.items() if candidate is line)
    row = next(row for row in range(dialog.table.rowCount())
               if dialog._row_entry(row) == ("trace", key))
    item = dialog.table.item(row, 0)
    dialog.table.scrollToItem(item)
    qtw.QApplication.processEvents()
    QtTest.QTest.mouseClick(
        dialog.table.viewport(), QtCore.Qt.MouseButton.LeftButton, modifier,
        dialog.table.visualItemRect(item).center(),
    )
    qtw.QApplication.processEvents()
    return key


def _replace_text(spin, value):
    editor = spin.lineEdit()
    window = spin.window()
    # Window exposure/activation is asynchronous on the Windows CI runner.
    # Finish it before typing: a late focus transition commits a spin box's
    # partial text even when keyboard tracking is disabled.
    assert QtTest.QTest.qWaitForWindowExposed(window)
    window.activateWindow()
    assert QtTest.QTest.qWaitForWindowActive(window)
    editor.setFocus()
    wait_for(editor.hasFocus)
    QtTest.QTest.keyClick(editor, QtCore.Qt.Key.Key_A,
                         QtCore.Qt.KeyboardModifier.ControlModifier)
    assert editor.selectedText() == editor.text()
    QtTest.QTest.keyClicks(editor, str(value))
    return editor


def _rendered_value(line, field):
    return line.opts["pen"].widthF() if field == "line_width" else line.opts["symbolSize"]


_FIELDS = [
    ("line_width", "line_width", None, 4.5),
    ("dots_size", "dots_size", "dots_enable", 7.5),
    ("marker_size", "markers_size", "marker_enable", 12.5),
]


@pytest.mark.parametrize("field,style_name,toggle,value", _FIELDS)
@pytest.mark.parametrize("target", [0, 1], ids=["primary", "secondary"])
@pytest.mark.parametrize("commit", ["return", "tab", "other-row"])
def test_fractional_edit_commits_to_original_selected_trace(
    trace_editor, field, style_name, toggle, value, target, commit,
):
    host, dialog, lines = trace_editor
    line, other = lines[target], lines[1 - target]
    key = _select_line(host, dialog, line)
    other_key = next(key for key, candidate in host.lines.items() if candidate is other)
    other_style_before = getattr(host._trace_styles[other_key], style_name)
    other_rendered_before = _rendered_value(other, field)
    if toggle is not None:
        QtTest.QTest.mouseClick(getattr(dialog, toggle), QtCore.Qt.MouseButton.LeftButton)
    editor = _replace_text(getattr(dialog, field), value)
    if commit == "other-row":
        _select_line(host, dialog, other)
    else:
        QtTest.QTest.keyClick(editor, QtCore.Qt.Key.Key_Return if commit == "return"
                             else QtCore.Qt.Key.Key_Tab)
    qtw.QApplication.processEvents()

    assert getattr(host._trace_styles[key], style_name) == value
    assert _rendered_value(line, field) == value
    assert getattr(host._trace_styles[other_key], style_name) == other_style_before
    assert _rendered_value(other, field) == other_rendered_before


def test_fractional_multi_selection_and_arrow_steps(trace_editor):
    host, dialog, lines = trace_editor
    _select_line(host, dialog, lines[0])
    _select_line(host, dialog, lines[1], QtCore.Qt.KeyboardModifier.ControlModifier)
    assert len(dialog._selected_entries()) == 2
    editor = _replace_text(dialog.line_width, 4.5)
    QtTest.QTest.keyClick(editor, QtCore.Qt.Key.Key_Return)
    assert [_rendered_value(line, "line_width") for line in lines] == [4.5, 4.5]

    QtTest.QTest.keyClick(dialog.line_width, QtCore.Qt.Key.Key_Up)
    assert [_rendered_value(line, "line_width") for line in lines] == [5., 5.]
    QtTest.QTest.keyClick(dialog.line_width, QtCore.Qt.Key.Key_Down)
    assert [_rendered_value(line, "line_width") for line in lines] == [4.5, 4.5]


@pytest.mark.parametrize("field,style_name,toggle,value", _FIELDS)
def test_uncommitted_fraction_survives_live_refresh(
    tmp_path, monkeypatch, field, style_name, toggle, value,
):
    with live_view(tmp_path, monkeypatch, .1) as (window, open_plot):
        assert window._database_access_mode == "trusted_live"
        plot = open_plot("signal_a")
        plot.appearance_action.trigger()
        dialog = plot._plot_appearance_dialog
        with _numeric_edit_diagnostics(
            plot, dialog, getattr(dialog, field), style_name,
        ) as checkpoint:
            if toggle is not None:
                QtTest.QTest.mouseClick(getattr(dialog, toggle), QtCore.Qt.MouseButton.LeftButton)
            editor = _replace_text(getattr(dialog, field), int(value))
            checkpoint("after_typing")
            original_style = getattr(plot._trace_styles[plot.label], style_name)
            assert original_style != int(value)
            committed = QtTest.QSignalSpy(getattr(dialog, field).editingFinished)
            worker = plot.worker
            wait_for(lambda: plot.worker is not worker)
            # Observe a complete publication, independent of runner speed, while
            # keeping both live timers active and restoring timeout delivery.
            checkpoint("before_publication_wait")
            wait_for_plot_load(window, plot)
            checkpoint("after_publication_wait")
            assert editor.hasFocus()
            assert len(committed) == 0
            assert getattr(plot._trace_styles[plot.label], style_name) == original_style
            assert editor.text() == str(int(value))
            QtTest.QTest.keyClicks(editor, ".5")
            QtTest.QTest.keyClick(editor, QtCore.Qt.Key.Key_Tab)
            assert getattr(plot._trace_styles[plot.label], style_name) == value
            assert _rendered_value(plot.line, field) == value
            np.testing.assert_array_equal(plot.line.getOriginalDataset()[1], [1., 2., 3., 4.])
