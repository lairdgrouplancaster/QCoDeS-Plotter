"""Transient emphasis for the dataset and axes supplying cursor coordinates."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from PyQt6 import QtCore, QtGui, sip
from PyQt6 import QtWidgets as qtw

_HALO_COLOUR = QtGui.QColor(60, 100, 145, 195)
_OUTLINE_COLOUR = QtGui.QColor(60, 100, 145, 195)
_DARK_HALO_COLOUR = QtGui.QColor(155, 190, 230, 210)
_DARK_OUTLINE_COLOUR = QtGui.QColor(155, 190, 230, 215)
_WARNING_HALO_COLOUR = QtGui.QColor(195, 125, 15, 225)
_DARK_WARNING_HALO_COLOUR = QtGui.QColor(255, 190, 65, 235)


class _ViewportHaloEffect(qtw.QGraphicsDropShadowEffect):
    """Bound Qt's temporary shadow pixmap by the current paint viewport.

    A zoomed trace can extend billions of screen pixels beyond its ViewBox.
    Qt's ordinary shadow effect allocates that full rectangle before clipping,
    which can exhaust memory or make the original trace disappear. During
    painting, limit the effective device rectangle before Qt requests its source
    pixmap. Outside painting, retain the ordinary bounds for scene invalidation.
    """

    def __init__(self):
        super().__init__()
        self._paint_clip: QtCore.QRectF | None = None

    def boundingRectFor(self, rect: QtCore.QRectF) -> QtCore.QRectF:
        bounds = super().boundingRectFor(rect)
        return bounds if self._paint_clip is None else bounds.intersected(self._paint_clip)

    def draw(self, painter: QtGui.QPainter | None) -> None:
        if painter is None:
            return
        self._paint_clip = QtCore.QRectF(painter.viewport())
        try:
            super().draw(painter)
        finally:
            self._paint_clip = None


class _HeatmapOutline(qtw.QGraphicsRectItem):
    _qplot_ui_overlay = True


class _AxisWarningBadge(qtw.QGraphicsObject):
    """A mouse-transparent warning symbol beside the existing axis label."""

    _qplot_ui_overlay = True

    def __init__(self, axis: Any):
        super().__init__(axis)
        self.axis = axis
        self.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
        self.setAcceptHoverEvents(False)
        self.setZValue(1)

    def boundingRect(self) -> QtCore.QRectF:
        return QtCore.QRectF(0, 0, 14, 14)

    def paint(self, painter: QtGui.QPainter | None, _option: Any, _widget: Any = None) -> None:
        if painter is None:
            return
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        border = QtGui.QPen(QtGui.QColor(130, 85, 15), 0.8)
        border.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
        painter.setPen(border)
        painter.setBrush(QtGui.QColor(245, 183, 55))
        painter.drawPolygon(QtGui.QPolygonF([
            QtCore.QPointF(7, 1), QtCore.QPointF(13, 12), QtCore.QPointF(1, 12),
        ]))
        ink = QtGui.QColor(65, 45, 10)
        pen = QtGui.QPen(ink, 1.3)
        pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawLine(QtCore.QPointF(7, 4.5), QtCore.QPointF(7, 7.6))
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(ink)
        painter.drawEllipse(QtCore.QPointF(7, 9.8), 0.75, 0.75)

    def refresh_position(self, enabled: bool) -> None:
        label = self.axis.label
        if label is None or sip.isdeleted(label):
            self.hide()
            return
        bounds = label.boundingRect()
        # Map the end of the text into the unrotated axis. The symbol stays
        # upright beside horizontal, upward and downward-facing labels alike.
        centre = label.mapToParent(QtCore.QPointF(bounds.right() + 8, bounds.center().y()))
        badge_bounds = self.boundingRect()
        position = centre - badge_bounds.center()
        view = self.axis.getViewWidget()
        if view is not None:
            available = self.axis.mapRectFromScene(
                view.mapToScene(view.viewport().rect()).boundingRect(),
            ).adjusted(1, 1, -1, -1)
            if not available.contains(badge_bounds.translated(position)):
                other_end = label.mapToParent(QtCore.QPointF(bounds.left() - 8, bounds.center().y()))
                alternative = other_end - badge_bounds.center()
                if available.contains(badge_bounds.translated(alternative)):
                    position = alternative
                else:
                    position = QtCore.QPointF(
                        min(max(position.x(), available.left()), available.right() - badge_bounds.width()),
                        min(max(position.y(), available.top()), available.bottom() - badge_bounds.height()),
                    )
        self.setPos(position)
        self.setVisible(enabled and label.isVisible() and bool(label.toPlainText().strip()))


class PlotHoverHighlight:
    """Decorate existing graphics without changing plotted data or styles.

    The readout owns target selection. Calling the setters with that same
    target keeps its emphasis in sync, including heatmap geometry changes.
    Effects remain on the existing graphics, so clipping, live trace updates,
    coordinate transforms and assigned drawing layers continue to apply.
    """

    def __init__(self, window: Any):
        self.window = window
        self.active_item: qtw.QGraphicsItem | None = None
        self.axis_sides: tuple[str, ...] = ()
        self.warning_axes: tuple[str, ...] = ()
        self.warning_badges: dict[str, _AxisWarningBadge] = {}
        self.outline: qtw.QGraphicsRectItem | None = None
        self._kind: str | None = None
        self._colourbar: Any = None
        self._effects: list[tuple[qtw.QGraphicsItem, qtw.QGraphicsDropShadowEffect]] = []
        self._warning_effects: set[qtw.QGraphicsDropShadowEffect] = set()
        self._suspend_count = 0
        self._signals: list[tuple[Any, Any]] = []
        self._label_signals: list[tuple[QtCore.QObject, Any, Any]] = []
        self._refreshing = False

    @property
    def effects(self) -> tuple[qtw.QGraphicsDropShadowEffect, ...]:
        return tuple(effect for _, effect in self._effects if not sip.isdeleted(effect))

    def set_trace(
        self, line: Any, axis_sides: tuple[str, str], *, warning_axes: tuple[str, ...] = (),
    ) -> None:
        self._set_target("trace", line, axis_sides, warning_axes=warning_axes)

    def set_heatmap(
        self, renderer: Any, axis_sides: tuple[str, str], colourbar: Any = None,
        *, warning_axes: tuple[str, ...] = (),
    ) -> None:
        self._set_target("heatmap", renderer, axis_sides, colourbar, warning_axes=warning_axes)

    def _set_target(
        self, kind: str, item: Any, sides: Any, colourbar: Any = None,
        *, warning_axes: tuple[str, ...] = (),
    ) -> None:
        if item is None or sip.isdeleted(item) or not item.isVisible():
            self.clear()
            return
        sides = tuple(str(side).lower() for side in sides)
        warnings = tuple(dict.fromkeys(
            side for side in warning_axes
            if side in sides or (side == "colour_scale" and colourbar is not None)
        ))
        if (kind == self._kind and item is self.active_item
                and sides == self.axis_sides and colourbar is self._colourbar
                and warnings == self.warning_axes):
            self.refresh()
            return

        self.clear()
        self._kind = kind
        self.active_item = item
        self.axis_sides = sides
        self.warning_axes = warnings
        self._colourbar = colourbar
        if item is not None:
            self._connect(item.visibleChanged, self.refresh)
            self._connect(item.destroyed, self._target_destroyed)

        for side in sides:
            axis = self.window.plot.getAxis(side)
            self._add_halo(axis, warning=side in warnings)
            if side in warnings:
                self._add_warning_badge(side, axis)
        if kind == "trace" and item is not None:
            # A trace can already have a compositing opacity effect. Its two
            # children carry the halos so that the parent's effect is preserved.
            self._add_halo(item.curve)
            self._add_halo(item.scatter)
        elif kind == "heatmap" and item is not None:
            self.outline = _HeatmapOutline(item)
            self.outline.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
            self.outline.setAcceptHoverEvents(False)
            self.outline.setBrush(QtGui.QBrush(QtCore.Qt.BrushStyle.NoBrush))
            pen = QtGui.QPen(_OUTLINE_COLOUR, 3)
            pen.setCosmetic(True)
            self.outline.setPen(pen)
            self.outline.setZValue(1)
            image_changed = getattr(item, "sigImageChanged", None)
            if image_changed is not None:
                self._connect(image_changed, self.refresh)
            if colourbar is not None:
                # The frame and labels identify the scale while its swatch
                # retains exactly the data colours used in the heatmap.
                for side in ("bottom", "left", "top", "right"):
                    self._add_halo(colourbar.getAxis(side), warning="colour_scale" in warnings)
                if "colour_scale" in warnings:
                    labelled_axes = [
                        colourbar.getAxis(side) for side in ("right", "bottom", "left", "top")
                        if colourbar.getAxis(side).label.isVisible()
                        and colourbar.getAxis(side).label.toPlainText().strip()
                    ]
                    self._add_warning_badge(
                        "colour_scale", labelled_axes[0] if labelled_axes else colourbar.axis,
                    )
        self.refresh()

    def _connect(self, signal: Any, slot: Any) -> None:
        signal.connect(slot)
        self._signals.append((signal, slot))

    def _target_destroyed(self) -> None:
        # Qt has already removed the item's derived signals by this point.
        # Its destruction releases the connections; disconnecting them here
        # would report nonexistent signals during ordinary window shutdown.
        self._signals.clear()
        self.clear()

    def _add_warning_badge(self, side: str, axis: Any) -> None:
        self.warning_badges[side] = _AxisWarningBadge(axis)
        label = axis.label
        document_layout = label.document().documentLayout()
        for owner, signal in (
            (axis, axis.geometryChanged),
            (label, label.xChanged), (label, label.yChanged),
            (label, label.rotationChanged), (label, label.visibleChanged),
            (document_layout, document_layout.documentSizeChanged),
        ):
            signal.connect(self.refresh)
            self._label_signals.append((owner, signal, self.refresh))

    def _add_halo(self, item: qtw.QGraphicsItem, *, warning: bool = False) -> None:
        if item.graphicsEffect() is not None:
            return
        effect = _ViewportHaloEffect()
        effect.setOffset(0, 0)
        effect.setBlurRadius(10)
        effect.setColor(_HALO_COLOUR)
        effect.setEnabled(self._suspend_count == 0)
        item.setGraphicsEffect(effect)
        self._effects.append((item, effect))
        if warning:
            self._warning_effects.add(effect)

    def refresh(self, *_args: Any) -> None:
        """Update a live heatmap's outline without changing its data bounds."""
        if self._refreshing:
            return
        self._refreshing = True
        try:
            self._refresh_graphics()
        finally:
            self._refreshing = False

    def _refresh_graphics(self) -> None:
        item = self.active_item
        if item is not None and (sip.isdeleted(item) or not item.isVisible()):
            self.clear()
            return
        view = self.window.plot.getViewWidget()
        dark = view is not None and view.backgroundBrush().color().lightnessF() < 0.5
        halo_colour = _DARK_HALO_COLOUR if dark else _HALO_COLOUR
        warning_colour = _DARK_WARNING_HALO_COLOUR if dark else _WARNING_HALO_COLOUR
        for effect in self.effects:
            colour = warning_colour if effect in self._warning_effects else halo_colour
            if effect.color() != colour:
                effect.setColor(colour)
        for badge in self.warning_badges.values():
            if not sip.isdeleted(badge):
                badge.refresh_position(self._suspend_count == 0)
        if item is not None and self.outline is not None and not sip.isdeleted(self.outline):
            pen = self.outline.pen()
            pen.setColor(_DARK_OUTLINE_COLOUR if dark else _OUTLINE_COLOUR)
            self.outline.setPen(pen)
            self.outline.setRect(item.boundingRect())
            self.outline.setVisible(self._suspend_count == 0)

    def clear(self) -> None:
        """Remove only graphics owned by this interaction."""
        for signal, slot in self._signals:
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._signals.clear()
        for owner, signal, slot in self._label_signals:
            if not sip.isdeleted(owner):
                try:
                    signal.disconnect(slot)
                except (TypeError, RuntimeError):
                    pass
        self._label_signals.clear()
        for item, effect in self._effects:
            if not sip.isdeleted(item) and not sip.isdeleted(effect):
                if item.graphicsEffect() is effect:
                    item.setGraphicsEffect(None)
        self._effects.clear()
        self._warning_effects.clear()
        for badge in self.warning_badges.values():
            if not sip.isdeleted(badge):
                badge.setParentItem(None)
                scene = badge.scene()
                if scene is not None:
                    scene.removeItem(badge)
        self.warning_badges.clear()
        if self.outline is not None and not sip.isdeleted(self.outline):
            self.outline.setParentItem(None)
            scene = self.outline.scene()
            if scene is not None:
                scene.removeItem(self.outline)
        self.outline = None
        self.active_item = None
        self.axis_sides = ()
        self.warning_axes = ()
        self._kind = None
        self._colourbar = None

    @contextmanager
    def suspended(self):
        """Keep transient emphasis out of captures, including nested exports."""
        self._suspend_count += 1
        try:
            self._set_enabled(False)
            yield
        finally:
            self._suspend_count -= 1
            if self._suspend_count == 0:
                self._set_enabled(True)

    def _set_enabled(self, enabled: bool) -> None:
        for effect in self.effects:
            effect.setEnabled(enabled)
        if self.outline is not None and not sip.isdeleted(self.outline):
            self.outline.setVisible(enabled)
        for badge in self.warning_badges.values():
            if not sip.isdeleted(badge):
                badge.refresh_position(enabled)
