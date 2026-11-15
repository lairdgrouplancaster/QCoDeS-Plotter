"""Shared fixtures and assertions for hover axis hit testing."""

from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

__all__ = [
    '_viewport_move',
]


def _viewport_move(window, scene_point, *, expect_scene_signal=True):
    pixel = window.widget.mapFromScene(scene_point)
    viewport = window.widget.viewport()
    assert viewport.rect().contains(pixel)
    delivered = []
    scene = window.plot.scene()
    scene.sigMouseMoved.connect(delivered.append)
    try:
        event = QtGui.QMouseEvent(
            QtCore.QEvent.Type.MouseMove,
            QtCore.QPointF(pixel),
            QtCore.QPointF(viewport.mapToGlobal(pixel)),
            QtCore.Qt.MouseButton.NoButton,
            QtCore.Qt.MouseButton.NoButton,
            QtCore.Qt.KeyboardModifier.NoModifier,
        )
        qtw.QApplication.sendEvent(viewport, event)
        qtw.QApplication.processEvents()
    finally:
        scene.sigMouseMoved.disconnect(delivered.append)
    if expect_scene_signal:
        assert delivered, "The viewport event must reach the plot's actual mouse handler"
        return delivered[-1]
    assert not delivered, "The scene must discard this event to exercise unthrottled clearing"
    return window.widget.mapToScene(pixel)
