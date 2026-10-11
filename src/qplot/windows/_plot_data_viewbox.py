"""Drawing layers that share stacking order across independent axis pairs."""

from typing import Any

import pyqtgraph as pg
from PyQt6 import QtCore


class PlotDataViewBox(pg.ViewBox):
    """Mirror an axis ViewBox while drawing one dataset among sibling layers.

    Qt stacks an entire parent's descendants together. Renderer Z values alone
    therefore cannot interleave datasets in different axis ViewBoxes. These
    transparent, noninteractive siblings provide a common stacking parent.
    Items stay registered with their axis owner for native automatic bounds.
    """

    def __init__(self, primary: Any, coordinate_owner: Any):
        self.coordinate_owner = coordinate_owner
        self.primary = primary
        self._mirroring_view = True
        super().__init__(enableMouse=False, enableMenu=False)
        self.setParentItem(primary)
        self.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
        self.setFlag(self.GraphicsItemFlag.ItemIsFocusable, False)
        self.disableAutoRange()
        coordinate_owner.sigRangeChanged.connect(self.sync_view)
        coordinate_owner.sigStateChanged.connect(self.sync_view)
        coordinate_owner.sigResized.connect(self.sync_view)
        if primary is not coordinate_owner:
            primary.sigResized.connect(self.sync_view)
        self._mirroring_view = False
        self.sync_view()

    def sync_view(self, *_args):
        if self._mirroring_view:
            return
        self._mirroring_view = True
        try:
            self._sync_coordinate_view()
        finally:
            self._mirroring_view = False

    def _sync_coordinate_view(self):
        owner = self.coordinate_owner
        self.setGeometry(self.primary.mapRectFromScene(owner.mapRectToScene(owner.rect())))
        state = owner.getState(copy=False)
        self.invertX(state["xInverted"])
        self.invertY(state["yInverted"])
        ranges = owner.viewRange()
        self.setRange(xRange=ranges[0], yRange=ranges[1], padding=0, disableAutoRange=True)
        # Bounds can be measured before the next paint. Keep both transforms
        # current so native mapping between the two groups is an identity.
        owner.updateMatrix()
        self.updateMatrix()

    def setRange(self, *args, **kwargs):
        if self._mirroring_view:
            return super().setRange(*args, **kwargs)
        return self.coordinate_owner.setRange(*args, **kwargs)

    def invertX(self, inverted=True):
        if self._mirroring_view:
            return super().invertX(inverted)
        return self.coordinate_owner.invertX(inverted)

    def invertY(self, inverted=True):
        if self._mirroring_view:
            return super().invertY(inverted)
        return self.coordinate_owner.invertY(inverted)

    def itemBoundsChanged(self, item):
        self.coordinate_owner.itemBoundsChanged(item)

    def removeItem(self, item):
        self.coordinate_owner.removeItem(item)

    def detach(self):
        owner = self.coordinate_owner
        owner.sigRangeChanged.disconnect(self.sync_view)
        owner.sigStateChanged.disconnect(self.sync_view)
        owner.sigResized.disconnect(self.sync_view)
        if self.primary is not owner:
            self.primary.sigResized.disconnect(self.sync_view)
        self.setParentItem(None)
        scene = self.scene()
        if scene is not None:
            scene.removeItem(self)
        self.close()


def coordinate_viewbox(viewbox: Any) -> Any:
    """Return the axis owner of a drawing layer, or an ordinary ViewBox."""
    return viewbox.coordinate_owner if isinstance(viewbox, PlotDataViewBox) else viewbox
