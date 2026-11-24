from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import pyqtgraph as pg
import pytest
from PyQt6 import QtCore, QtGui, QtTest, QtWidgets

from qplot.windows._commands import _PlotKeyFocusFilter, create_action
from qplot.windows._plot2d_layers import Plot2DLayerMixin
from qplot.windows._plot2d_sweeps import Plot2DSweepMixin
from qplot.windows._plot_marquee import PlotMarqueeMixin, _moved_interval
from qplot.windows._subplots.subplot1d import custom_viewbox
from tests.support.windows.mixed_plot_drops import mixed_plot as mixed_plot


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


@pytest.fixture
def cut_host(qapplication):
    class Host(QtWidgets.QMainWindow, Plot2DSweepMixin):
        sweep_moved = QtCore.pyqtSignal(int, float)
        close_sweeps_requested = QtCore.pyqtSignal(object, object)
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
    host._init_cut_keyboard_shortcuts()
    try:
        yield host
    finally:
        host.close()
        host.deleteLater()


def test_new_cut_is_selected_highlighted_and_moves_by_arrow(cut_host):
    host = cut_host
    host.update_sweep_line(1, "gate", "field", 2.0, pg.mkPen("r"))
    assert host.active_sweep_line_id == host._keyboard_selected_sweep_id == 1
    assert host.sweep_lines[1].pen.widthF() == 3.0
    host.move_sweep_with_arrow_key(QtCore.Qt.Key.Key_Up)
    assert host.sweep_lines[1].sweep_index == 3
    host.update_sweep_line(2, "gate", "field", 1.0, pg.mkPen("b"))
    assert host.active_sweep_line_id == host._keyboard_selected_sweep_id == 2
    assert host.sweep_lines[1].pen.widthF() == 1.0
    assert host.sweep_lines[2].pen.widthF() == 3.0


def test_cut_keyboard_cycle_wraps_moves_only_selection_and_keeps_tab_navigation(cut_host, qapplication):
    host = cut_host
    dock = QtWidgets.QDockWidget(host)
    editor = QtWidgets.QLineEdit()
    dock.setWidget(editor)
    host.addDockWidget(QtCore.Qt.DockWidgetArea.LeftDockWidgetArea, dock)
    host.show()
    host.activateWindow()
    qapplication.processEvents()
    host.widget.setFocus()
    control = QtCore.Qt.KeyboardModifier.ControlModifier
    backwards = control | QtCore.Qt.KeyboardModifier.ShiftModifier

    def press(key, modifiers=QtCore.Qt.KeyboardModifier.NoModifier):
        QtTest.QTest.keyClick(qapplication.focusWidget(), key, modifiers)

    # Empty canvases accept the shortcut without selecting a nonexistent cut.
    press(QtCore.Qt.Key.Key_Tab, control)
    assert host.active_sweep_line_id is None
    for sweep_id, sweep, fixed in ((1, "gate", "field"), (2, "gate", "field"), (3, "field", "gate")):
        host.update_sweep_line(sweep_id, sweep, fixed, 1., pg.mkPen("r"))
    assert host.active_sweep_line_id == 3
    press(QtCore.Qt.Key.Key_Tab, control)
    assert host.active_sweep_line_id == 1
    assert host.sweep_lines[1].pen.widthF() == 3.
    assert host.sweep_lines[3].pen.widthF() == 1.
    press(QtCore.Qt.Key.Key_Up)
    assert [host.sweep_lines[i].sweep_index for i in (1, 2, 3)] == [2, 1, 1]
    press(QtCore.Qt.Key.Key_Tab, backwards)
    assert host.active_sweep_line_id == 3
    press(QtCore.Qt.Key.Key_Right)
    assert host.sweep_lines[3].sweep_index == 2
    assert host.widget.hasFocus()

    # Neither ordinary Tab direction changes the selected probe.
    press(QtCore.Qt.Key.Key_Tab)
    assert not host.widget.hasFocus()
    assert host.active_sweep_line_id == 3
    editor.setFocus()
    press(QtCore.Qt.Key.Key_Tab, control)
    assert host.active_sweep_line_id == 3
    host.widget.setFocus()
    press(QtCore.Qt.Key.Key_Backtab, QtCore.Qt.KeyboardModifier.ShiftModifier)
    assert not host.widget.hasFocus()
    assert host.active_sweep_line_id == 3

    # A removed selection and hidden cursors do not break cycling.
    host.widget.setFocus()
    host.remove_sweep(3)
    host.sweep_lines[1].hide()
    press(QtCore.Qt.Key.Key_Tab, backwards)
    assert host.active_sweep_line_id == 2
    press(QtCore.Qt.Key.Key_Tab, control)
    assert host.active_sweep_line_id == 2


@pytest.mark.parametrize("vertical", [False, True])
@pytest.mark.parametrize("all_cuts", [False, True])
@pytest.mark.parametrize("mesh", [False, True])
@pytest.mark.parametrize("pixel_offset", [0, -5, 5, 12])
@pytest.mark.parametrize("zoomed_out", [False, True])
def test_native_probe_double_click_removes_cut_before_heatmap_appearance(
    cut_host, qapplication, vertical, all_cuts, mesh, pixel_offset, zoomed_out,
):
    host = cut_host
    values = np.arange(25.).reshape(5, 5)
    image = pg.PColorMeshItem(values) if mesh else pg.ImageItem(values)
    host.plot.addItem(image)
    host._heatmap_render_items = lambda layer: (image,)
    host.open_heatmap_appearance_dialog = Mock()
    Plot2DLayerMixin._install_heatmap_double_click_handlers(host, host, "signal")
    sweep, fixed = ("field", "gate") if vertical else ("gate", "field")
    host.update_sweep_line(1, sweep, fixed, 1., pg.mkPen("r"))
    host.update_sweep_line(2, sweep, fixed, 3., pg.mkPen("b"))
    removed = []

    def close_cuts(source, ids):
        assert source is host
        removed.append(ids)
        for sweep_id in ids:
            host.remove_sweep(sweep_id)

    host.close_sweeps_requested.connect(close_cuts)
    host.resize(640, 480)
    host.show()
    view_range = (-5., 10.) if zoomed_out else (0., 5.)
    host.plot.setRange(xRange=view_range, yRange=view_range, padding=0.)
    qapplication.processEvents()
    QtTest.QTest.qWait(30)  # Settle tick-label widths before mapping mouse positions.
    viewport = host.widget.viewport()

    def double_click(x, y, modifiers=QtCore.Qt.KeyboardModifier.NoModifier, offset=0):
        position = host.widget.mapFromScene(host.plot.vb.mapViewToScene(QtCore.QPointF(x, y)))
        position += QtCore.QPoint(offset, 0) if vertical else QtCore.QPoint(0, offset)
        QtTest.QTest.mouseMove(viewport, position)
        QtTest.QTest.mouseClick(viewport, QtCore.Qt.MouseButton.LeftButton, modifiers, position)
        QtTest.QTest.mouseDClick(viewport, QtCore.Qt.MouseButton.LeftButton, modifiers, position)
        QtTest.QTest.mouseRelease(viewport, QtCore.Qt.MouseButton.LeftButton, modifiers, position)
        qapplication.processEvents()

    modifiers = (QtCore.Qt.KeyboardModifier.ShiftModifier if all_cuts
                 else QtCore.Qt.KeyboardModifier.NoModifier)
    x, y = (1., 2.5) if vertical else (2.5, 1.)
    scene_position = host.widget.mapToScene(host.widget.mapFromScene(
        host.plot.vb.mapViewToScene(QtCore.QPointF(x, y))))
    assert host.sweep_lines[1].contains(host.sweep_lines[1].mapFromScene(scene_position))
    double_click(x, y, modifiers, offset=pixel_offset)
    if abs(pixel_offset) <= 5:
        host.open_heatmap_appearance_dialog.assert_not_called()
        assert removed == [(1, 2) if all_cuts else (1,)]
        assert set(host.sweep_lines) == (set() if all_cuts else {2})
    else:
        host.open_heatmap_appearance_dialog.assert_called_once_with("signal")
        assert removed == []
        assert set(host.sweep_lines) == {1, 2}
        host.open_heatmap_appearance_dialog.reset_mock()

    # Double-clicking the heatmap away from probes still opens its appearance.
    double_click(4., 4.)
    host.open_heatmap_appearance_dialog.assert_called_once_with("signal")


def test_probe_hit_area_favours_nearest_probe_and_ignores_hidden_probes(cut_host, qapplication):
    host = cut_host
    host.update_sweep_line(1, "gate", "field", 1., pg.mkPen("r"))
    host.update_sweep_line(2, "gate", "field", 2., pg.mkPen("b"))
    host.show()
    host.plot.setRange(xRange=(0., 5.), yRange=(0., 5.), padding=0.)
    qapplication.processEvents()
    QtTest.QTest.qWait(30)
    first, second = host.sweep_lines.values()
    # Put the newer probe six screen pixels from the first. A click just outside
    # the first's shape must favour it over the newer probe's expanded hit area.
    transform = host.widget.viewportTransform()
    centre = transform.map(host.plot.vb.mapViewToScene(QtCore.QPointF(2., 1.)))
    nearby = centre + QtCore.QPointF(0., -6.)
    scene_inverse, invertible = transform.inverted()
    assert invertible
    second.setValue(host.plot.vb.mapSceneToView(scene_inverse.map(nearby)).y())
    near_first = scene_inverse.map(centre + QtCore.QPointF(0., -2.))
    assert host._probe_at_scene_position([first, second], near_first) is first
    assert host._probe_at_scene_position([first, second], scene_inverse.map(nearby)) is second
    first.hide()
    assert host._probe_at_scene_position([first, second], near_first) is second
    second.hide()
    assert host._probe_at_scene_position([first, second], near_first) is None


@pytest.mark.parametrize("first,second,expected", [
    ((4, 1), (5, 20), (5, 0)),
    ((1, 4), (20, 5), (0, 5)),
])
def test_shift_marquee_move_locks_initial_axis_and_releases(first, second, expected):
    original = QtCore.QRectF(0, 0, 3, 3)
    owner = SimpleNamespace(
        marquee=original,
        _marquee_viewbox=lambda: SimpleNamespace(mapViewToScene=lambda p: p),
        _marquee_move_edges=lambda: (None, None),
        _draw_marquee_rect=Mock(),
    )
    PlotMarqueeMixin.begin_marquee_drag(owner, QtCore.QPointF(0, 0), "move")
    shift = QtCore.Qt.KeyboardModifier.ShiftModifier
    PlotMarqueeMixin.drag_marquee_to(owner, QtCore.QPointF(*first), shift)
    PlotMarqueeMixin.drag_marquee_to(owner, QtCore.QPointF(*second), shift)
    owner._draw_marquee_rect.assert_called_with(original.translated(*expected))
    PlotMarqueeMixin.drag_marquee_to(owner, QtCore.QPointF(*second))
    owner._draw_marquee_rect.assert_called_with(original.translated(*second))


def test_dragging_inside_marquee_preserves_snapped_sample_count(mixed_plot, qapplication):
    _main, plot, _runs = mixed_plot
    heatmap = hasattr(plot, "dataGrid")
    plot.resize(850, 600)
    plot.show()
    plot.activateWindow()
    viewbox = plot._marquee_viewbox()
    viewbox.setRange(xRange=(-1, 3), yRange=(0, 30) if heatmap else (-1, 3), padding=0)
    qapplication.processEvents()
    viewport = plot.widget.viewport()
    start = QtCore.QPointF(0, 10 if heatmap else 1)
    modifier = QtCore.Qt.KeyboardModifier
    button = QtCore.Qt.MouseButton

    def drag(dx, dy, keys):
        # Select one X sample and, for the heatmap, one Y sample.
        plot.set_marquee_rect(QtCore.QRectF(-0.2, start.y() - 1, 0.4, 2))
        original = QtCore.QRectF(plot.marquee)
        anchor = start + QtCore.QPointF(-0.15 if keys == modifier.NoModifier else 0.15, 0)
        first, last = [plot.widget.mapFromScene(viewbox.mapViewToScene(point))
                       for point in (anchor, anchor + QtCore.QPointF(dx, dy))]
        QtTest.QTest.mouseMove(viewport, first)
        QtTest.QTest.mousePress(viewport, button.LeftButton, keys, pos=first)
        QtTest.QTest.qWait(30)  # Allow a new drag through the scene's mouse-move rate limit.
        move = QtGui.QMouseEvent(
            QtCore.QEvent.Type.MouseMove, QtCore.QPointF(last),
            QtCore.QPointF(viewport.mapToGlobal(last)), button.NoButton, button.LeftButton, keys,
        )
        qapplication.sendEvent(viewport, move)
        assert plot.current_marquee_drag_mode() == "move"
        QtTest.QTest.mouseRelease(viewport, button.LeftButton, keys, pos=last)
        return original

    original = drag(0.2, 1 if heatmap else 0.2, modifier.NoModifier)
    assert (plot.marquee.left(), plot.marquee.right()) == (-0.5, 0.5)
    if heatmap:
        assert plot.marquee == original
        np.testing.assert_array_equal(plot._marquee_selected_data(), [[10]])
    else:
        assert plot.marquee.height() == pytest.approx(original.height())
        assert plot.marquee.top() == pytest.approx(original.top() + 0.2, abs=0.03)
    original = drag(0.9, 1 if heatmap else 0.1, modifier.ShiftModifier)
    assert plot.marquee == original.translated(1, 0)
    if heatmap:
        np.testing.assert_array_equal(plot._marquee_selected_data(), [[11]])


def test_marquee_move_uses_nearest_whole_block_on_irregular_axis():
    edges = (-1000., -1., 0.1, 4., 10., 19.)
    # The same two cells can occupy different physical widths after translation.
    assert _moved_interval(edges, -1., 5., 4.) == (0.1, 9.9)
    assert _moved_interval(edges, -1., 5., 200.) == (4., 15.)
    left, width = _moved_interval(edges, -1., 5., -1000.)
    assert left == -1000.
    assert left + width == pytest.approx(0.1)
    assert left + width <= edges[2]


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
        action = create_action("plot.refresh", window)
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
