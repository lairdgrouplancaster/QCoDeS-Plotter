from types import SimpleNamespace
from unittest.mock import Mock, patch

import pyqtgraph as pg
import pytest
from PyQt6 import QtCore, QtGui, QtTest, QtWidgets

from qplot.windows._commands import _PlotKeyFocusFilter, create_action
from qplot.windows._plot_marquee import PlotMarqueeMixin
from qplot.windows._plot2d_sweeps import Plot2DSweepMixin
from qplot.windows._subplots.subplot1d import custom_viewbox


class Drag:
    def __init__(self, button=QtCore.Qt.MouseButton.LeftButton):
        self._button = button

    def button(self):
        return self._button

    def isStart(self):
        return True

    def isFinish(self):
        return True

    def buttonDownScenePos(self):
        return QtCore.QPointF(0, 0)

    def scenePos(self):
        return QtCore.QPointF(10, 20)

    def modifiers(self):
        return QtCore.Qt.KeyboardModifier.NoModifier

    def accept(self):
        pass


def test_new_cut_is_selected_highlighted_and_moves_by_arrow(qapplication):
    class Host(QtWidgets.QMainWindow, Plot2DSweepMixin):
        sweep_moved = QtCore.pyqtSignal(int, float)
        axis_options = {"x": "gate", "y": "field"}

        def _heatmap_geometry(self):
            return object()

        def sweep_index_at_value(self, axis, value, clamp=False):
            return int(value)

        def sweep_pixel_centre(self, axis, index):
            return float(index)

        def sweep_axis_count(self, axis):
            return 5

    host = Host()
    host.widget = pg.GraphicsLayoutWidget()
    host.setCentralWidget(host.widget)
    host.plot = host.widget.addPlot()
    host.sweep_lines = {}
    host.active_sweep_line_id = None
    try:
        host.update_sweep_line(1, "gate", "field", 2.0, pg.mkPen("r"))
        assert host.active_sweep_line_id == host._keyboard_selected_sweep_id == 1
        assert host.sweep_lines[1].pen.widthF() == 3.0
        host.move_sweep_with_arrow_key(QtCore.Qt.Key.Key_Up)
        assert host.sweep_lines[1].sweep_index == 3
        host.update_sweep_line(2, "gate", "field", 1.0, pg.mkPen("b"))
        assert host.active_sweep_line_id == host._keyboard_selected_sweep_id == 2
        assert host.sweep_lines[1].pen.widthF() == 1.0
        assert host.sweep_lines[2].pen.widthF() == 3.0
    finally:
        host.close()
        host.deleteLater()


@pytest.mark.parametrize("first,second,expected", [
    ((4, 1), (5, 20), (5, 0)),
    ((1, 4), (20, 5), (0, 5)),
])
def test_shift_marquee_move_locks_initial_axis_and_releases(first, second, expected):
    original = QtCore.QRectF(0, 0, 3, 3)
    owner = SimpleNamespace(
        marquee=original,
        _marquee_viewbox=lambda: SimpleNamespace(mapViewToScene=lambda p: p),
        set_marquee_rect=Mock(),
    )
    PlotMarqueeMixin.begin_marquee_drag(owner, QtCore.QPointF(0, 0), "move")
    shift = QtCore.Qt.KeyboardModifier.ShiftModifier
    PlotMarqueeMixin.drag_marquee_to(owner, QtCore.QPointF(*first), shift)
    PlotMarqueeMixin.drag_marquee_to(owner, QtCore.QPointF(*second), shift)
    owner.set_marquee_rect.assert_called_with(original.translated(*expected))
    PlotMarqueeMixin.drag_marquee_to(owner, QtCore.QPointF(*second))
    owner.set_marquee_rect.assert_called_with(original.translated(*second))


def test_default_drag_selects_and_inside_drag_moves(qapplication):
    view = custom_viewbox()
    owner = SimpleNamespace(
        marquee_drag_mode_at=Mock(return_value=None),
        marquee_contains_scene_pos=Mock(return_value=False),
        begin_marquee_drag=Mock(), is_marquee_dragging=lambda: True,
        _marquee_viewbox=lambda: SimpleNamespace(mapSceneToView=lambda p: p),
        drag_marquee_to=Mock(), finish_marquee_drag=Mock(),
    )
    view.set_marquee_owner(owner)
    with patch.object(view, "_update_marquee_cursor"):
        assert view._handle_marquee_mouse_drag(Drag())
        owner.begin_marquee_drag.assert_called_with(QtCore.QPointF(0, 0), None)
        owner.marquee_contains_scene_pos.return_value = True
        assert view._handle_marquee_mouse_drag(Drag())
        owner.begin_marquee_drag.assert_called_with(QtCore.QPointF(0, 0), "move")
        view._space_pan = True
        assert not view._handle_marquee_mouse_drag(Drag())
        assert not view._handle_marquee_mouse_drag(Drag(QtCore.Qt.MouseButton.MiddleButton))


def test_space_pan_tracks_press_release_and_focus_loss(qapplication):
    widget = pg.GraphicsLayoutWidget()
    view = custom_viewbox()
    view.set_marquee_owner(SimpleNamespace(widget=widget))
    widget.show()
    try:
        widget.setFocus()
        QtTest.QTest.keyPress(widget, QtCore.Qt.Key.Key_Space)
        assert view._space_pan
        QtTest.QTest.keyRelease(widget, QtCore.Qt.Key.Key_Space)
        assert not view._space_pan
        view._space_pan = True
        qapplication.sendEvent(widget, QtGui.QFocusEvent(QtCore.QEvent.Type.FocusOut))
        assert not view._space_pan
    finally:
        widget.close()


def test_right_drag_does_not_scale(qapplication):
    view = custom_viewbox()
    with patch.object(pg.ViewBox, "mouseDragEvent") as native_drag:
        view.mouseDragEvent(Drag(QtCore.Qt.MouseButton.RightButton))
        native_drag.assert_not_called()


def test_plot_letters_require_canvas_focus_but_f5_does_not(qapplication):
    window = QtWidgets.QMainWindow()
    window.widget = QtWidgets.QWidget(window)
    window.widget.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
    editor = QtWidgets.QLineEdit(window)
    window.setCentralWidget(editor)
    window.show()
    try:
        action = create_action("window.refresh", window)
        focus_filter = _PlotKeyFocusFilter(action)
        editor.setFocus()
        qapplication.processEvents()
        assert focus_filter.eventFilter(action, QtGui.QShortcutEvent(QtGui.QKeySequence("R"), 1))
        assert not focus_filter.eventFilter(action, QtGui.QShortcutEvent(QtGui.QKeySequence("F5"), 1))
        window.widget.setFocus()
        qapplication.processEvents()
        assert not focus_filter.eventFilter(action, QtGui.QShortcutEvent(QtGui.QKeySequence("R"), 1))
    finally:
        window.close()
