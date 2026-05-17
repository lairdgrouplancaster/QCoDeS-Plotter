from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

import math
import numpy as np
import pyqtgraph as pg
from pyqtgraph.graphicsItems.ButtonItem import ButtonItem

from ._colorbar import (
    _CET_COLORBAR_SUBTYPES,
    _COLORBAR_COLORMAP_LABELS,
    _COLORBAR_COLORMAPS,
    _COLORBAR_TABLE_SORT_ROLE,
    _DEFAULT_HIDDEN_COLORBAR_NAMES,
    _DEFAULT_HIDDEN_COLORBAR_PREFIXES,
    _DEFAULT_HIDDEN_COLORBAR_SUFFIXES,
    _MATPLOTLIB_COLORBAR_SUBTYPES,
    _ColorbarColormapTableItem,
    _cet_colorbar_colormap_subtype,
    _colorbar_colormap_for_name,
    _colorbar_colormap_group,
    _colorbar_colormap_preview,
    _colorbar_colormap_type_label,
    _colorbar_subtype_config_key,
    _config_value,
    _letter_button_pixmap,
    _matplotlib_colorbar_colormap_subtype,
    _string_list,
)
from ._plot_axis_scaling import _axis_scale_power_text


class _CenteredIconDelegate(qtw.QStyledItemDelegate):
    """
    Paint icon-only table cells with the icon centered in the cell.

    """

    def paint(self, painter, option, index):
        icon = index.data(QtCore.Qt.ItemDataRole.DecorationRole)
        if not isinstance(icon, QtGui.QIcon) or icon.isNull():
            super().paint(painter, option, index)
            return

        opt = qtw.QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        opt.icon = QtGui.QIcon()

        widget = opt.widget
        style = widget.style() if widget else qtw.QApplication.style()
        style.drawControl(qtw.QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)

        icon_size = opt.decorationSize
        if not icon_size.isValid() or icon_size.isEmpty():
            icon_size = icon.actualSize(opt.rect.size())
        icon_size = icon_size.boundedTo(opt.rect.size())
        icon_rect = QtCore.QRect(QtCore.QPoint(0, 0), icon_size)
        icon_rect.moveCenter(opt.rect.center())

        mode = QtGui.QIcon.Normal
        if not opt.state & qtw.QStyle.StateFlag.State_Enabled:
            mode = QtGui.QIcon.Disabled
        elif opt.state & qtw.QStyle.StateFlag.State_Selected:
            mode = QtGui.QIcon.Selected

        icon.paint(painter, icon_rect, QtCore.Qt.AlignmentFlag.AlignCenter, mode)


class Plot2DColorbarMixin:
    """
    Heatmap colorbar controls for plot2d windows.

    This mixin owns color autoscaling, colorbar interaction handlers, color-map
    filtering, and the color scale dialog.
    """

    def _init_color_autoscale_button(self):
        """
        Add a lower-left color autoscale button beside pyqtgraph's axis autoscale.

        """
        if "color_auto_button" in self.__dict__:
            return

        self.color_auto_button = ButtonItem(
            pixmap=_letter_button_pixmap("C"),
            width=14,
            parentItem=self.plot,
            )
        self.color_auto_button.setToolTip("Autoscale color range")
        self.color_auto_button.clicked.connect(lambda _button: self.scaleColorbar())
        self.color_auto_button.hide()

        self._patch_color_autoscale_button_events()
        self._position_color_autoscale_button()
        self._update_color_autoscale_button()

    def _patch_color_autoscale_button_events(self):
        """
        Keep the color autoscale button in step with pyqtgraph's plot buttons.

        """
        if getattr(self.plot, "_qplot_color_auto_button_patched", False):
            return

        original_update_buttons = self.plot.updateButtons
        original_resize_event = self.plot.resizeEvent

        def update_buttons():
            original_update_buttons()
            self._update_color_autoscale_button()

        def resize_event(event):
            original_resize_event(event)
            self._position_color_autoscale_button()

        self.plot.updateButtons = update_buttons
        self.plot.resizeEvent = resize_event
        self.plot._qplot_color_auto_button_patched = True

    def _position_color_autoscale_button(self):
        """
        Position the C button just to the right of pyqtgraph's A button.

        """
        button = getattr(self, "color_auto_button", None)
        auto_button = getattr(self.plot, "autoBtn", None)
        if button is None or auto_button is None:
            return

        try:
            auto_rect = self.plot.mapRectFromItem(
                auto_button,
                auto_button.boundingRect(),
                )
            button_rect = self.plot.mapRectFromItem(button, button.boundingRect())
            x = auto_button.pos().x() + auto_rect.width() + 2
            y = self.plot.size().height() - button_rect.height()
            button.setPos(x, y)
        except RuntimeError:
            return

    def _update_color_autoscale_button(self):
        """
        Show the color autoscale button while plot controls are visible.

        """
        button = getattr(self, "color_auto_button", None)
        if button is None:
            return

        try:
            self._position_color_autoscale_button()
            if (
                    self.plot._exportOpts is False
                    and self.plot.mouseHovering
                    and not self.plot.buttonsHidden
                    and self._data_colorbar_levels() is not None
                    ):
                button.show()
            else:
                button.hide()
        except RuntimeError:
            return

    @QtCore.pyqtSlot(bool)
    def scaleColorbar(self, event = None):
        """
        Sets colorbar range to match heatmap value range, giving effect of
        rescaling color bar

        Parameters
        ----------
        Unused but required by slot

        """
        levels = self._data_colorbar_levels()
        if levels is None:
            return

        self._colorbar_manual_levels = None
        self._set_colorbar_levels(*levels)

    def _data_colorbar_levels(self):
        """
        Return finite min/max levels from the current heatmap data.

        """
        data = getattr(self, "dataGrid", None)
        if data is None:
            return None

        vmin, vmax = np.nanmin(data), np.nanmax(data)
        if not np.isfinite(vmin) or not np.isfinite(vmax) or vmin >= vmax:
            return None

        return float(vmin), float(vmax)

    def _set_colorbar_levels(self, vmin, vmax):
        """
        Apply levels to the colorbar and mirror them in the menu fields.

        """
        bar = self.__dict__.get("bar")
        if bar is not None:
            bar.setLevels((vmin, vmax))
            self._sync_colorbar_axis_scaling()

        self._sync_colorbar_level_fields(vmin, vmax)

    def _set_colorbar_tick_formatter(self):
        """
        Use plain scaled tick labels and show the scale in the colorbar title.

        """
        bar = self.__dict__.get("bar")
        axis = getattr(bar, "axis", None)
        if axis is None:
            return

        self._restore_colorbar_default_tick_formatter(axis)
        axis.autoSIPrefix = False
        axis.setWidth(70)
        axis.setStyle(tickTextWidth=60)
        self._set_colorbar_label_direction(bar)
        self._install_colorbar_axis_scale_sync(bar)
        self._sync_colorbar_axis_scaling()
        self._install_colorbar_scale_bar_handlers(bar)
        self._install_colorbar_scale_axis_handlers(axis)

    def _restore_colorbar_default_tick_formatter(self, axis):
        try:
            del axis.tickStrings
        except AttributeError:
            pass

    def _install_colorbar_axis_scale_sync(self, bar):
        axis = getattr(bar, "axis", None)
        if axis is None or getattr(axis, "_qplot_colorbar_scale_sync_installed", False):
            return

        previous_set_range = getattr(axis, "setRange", None)
        if previous_set_range is None:
            return

        def set_range(low, high, previous_set_range=previous_set_range):
            previous_set_range(low, high)
            self._sync_colorbar_axis_scaling()

        axis.setRange = set_range
        axis._qplot_colorbar_scale_sync_installed = True

    def _sync_colorbar_axis_scaling(self):
        bar = self.__dict__.get("bar")
        axis = getattr(bar, "axis", None)
        if axis is None:
            return

        scale = self._colorbar_axis_display_scale(bar, axis)
        axis.autoSIPrefixScale = scale
        axis.labelUnitPrefix = ""
        axis.picture = None
        axis.update()
        self._set_colorbar_scaled_label(bar, scale)

    def _colorbar_axis_display_scale(self, bar, axis):
        levels = self._colorbar_levels_from_bar(bar)
        if levels is None:
            levels = getattr(axis, "range", None)
        if levels is None:
            return 1.0

        low, high = levels
        scale_value = max(abs(low), abs(high))
        if not np.isfinite(scale_value):
            return 1.0

        scale, _prefix = pg.functions.siScale(scale_value, allowUnicode=False)
        return scale

    def _set_colorbar_scaled_label(self, bar, scale):
        param = self.__dict__.get("param")
        if param is None or not hasattr(bar, "setLabel"):
            return

        label = getattr(param, "label", "")
        unit = getattr(param, "unit", "")
        unit_scale = 1.0 / scale if scale else 1.0
        scale_text = _axis_scale_power_text(unit_scale)

        if unit and scale_text:
            unit_text = f"{scale_text} {unit}"
        elif unit:
            unit_text = unit
        else:
            unit_text = scale_text

        text = f"{label} ({unit_text})" if unit_text else label
        axis = "bottom" if getattr(bar, "horizontal", False) else "right"
        bar.setLabel(axis, text)
        self._set_colorbar_label_direction(bar)

    def _set_colorbar_label_direction(self, bar):
        if getattr(bar, "horizontal", False):
            return

        axis = getattr(bar, "axis", None)
        label = getattr(axis, "label", None)
        if axis is None or label is None or not hasattr(label, "setRotation"):
            return

        label.setRotation(90)
        if not getattr(axis, "_qplot_downward_colorbar_label_installed", False):
            previous_resize_event = getattr(axis, "resizeEvent", None)

            def resize_event(event=None, previous_handler=previous_resize_event):
                if previous_handler is not None:
                    previous_handler(event)
                self._position_downward_colorbar_label(axis)

            axis.resizeEvent = resize_event
            axis._qplot_downward_colorbar_label_installed = True

        self._position_downward_colorbar_label(axis)

    def _position_downward_colorbar_label(self, axis):
        label = getattr(axis, "label", None)
        if label is None or not all(
                hasattr(label, name) for name in ("boundingRect", "setPos")
                ):
            return

        nudge = 5
        bounds = label.boundingRect()
        size = axis.size()
        position = QtCore.QPointF(
            int(size.width() + nudge),
            int(size.height() / 2 - bounds.width() / 2),
            )
        label.setPos(position)
        axis.picture = None

    def _install_colorbar_scale_bar_handlers(self, bar):
        """
        Open the color-scale dialog from colorbar interactions.

        """
        self._install_colorbar_scale_double_click_handler(bar)
        self._suppress_colorbar_right_click_menu(bar)
        self._install_colorbar_level_sync_handlers(bar)
        self._install_colorbar_alt_range_drag_handler(bar)

    def _install_colorbar_scale_axis_handlers(self, axis):
        """
        Open the color-scale dialog from direct colorbar axis interactions.

        """
        self._install_colorbar_scale_double_click_handler(axis)

    def _install_colorbar_scale_double_click_handler(self, item):
        """
        Open the color-scale dialog when a colorbar item is double-clicked.

        """
        if getattr(item, "_qplot_colorbar_scale_handlers_installed", False):
            return

        previous_double_click_handler = getattr(item, "mouseDoubleClickEvent", None)

        def mouse_double_click(event, previous_handler=previous_double_click_handler):
            if event.button() == QtCore.Qt.MouseButton.LeftButton:
                self.open_colorbar_scale_dialog()
                event.accept()
                return

            if previous_handler is not None:
                previous_handler(event)

        item.mouseDoubleClickEvent = mouse_double_click
        item._qplot_colorbar_scale_handlers_installed = True

    def _suppress_colorbar_right_click_menu(self, bar):
        """
        Prevent pyqtgraph's colorbar right-click menu from opening.

        """
        if getattr(bar, "_qplot_colorbar_right_click_suppressed", False):
            return

        previous_mouse_click_handler = getattr(bar, "mouseClickEvent", None)

        def mouse_click(event, previous_handler=previous_mouse_click_handler):
            if event.button() == QtCore.Qt.MouseButton.RightButton:
                event.accept()
                return

            if previous_handler is not None:
                previous_handler(event)

        bar.mouseClickEvent = mouse_click
        bar._qplot_colorbar_right_click_suppressed = True

    def _install_colorbar_level_sync_handlers(self, bar):
        """
        Treat direct colorbar handle adjustment as a manual color range.

        """
        if getattr(bar, "_qplot_colorbar_level_sync_installed", False):
            return

        changed = getattr(bar, "sigLevelsChanged", None)
        if changed is not None:
            changed.connect(self._colorbar_interactive_levels_changed)

        finished = getattr(bar, "sigLevelsChangeFinished", None)
        if finished is not None:
            finished.connect(self._colorbar_interactive_levels_finished)

        bar._qplot_colorbar_level_sync_installed = True

    def _install_colorbar_alt_range_drag_handler(self, bar):
        """
        Add outside-bar drag on colorbar regions to widen or narrow levels.

        """
        region = getattr(bar, "region", None)
        if region is None:
            return

        if getattr(region, "_qplot_colorbar_alt_range_drag_installed", False):
            return

        previous_bounding_rect = getattr(region, "boundingRect", None)
        previous_paint = getattr(region, "paint", None)
        previous_mouse_drag_handler = getattr(region, "mouseDragEvent", None)
        previous_hover_handler = getattr(region, "hoverEvent", None)

        def bounding_rect(previous_handler=previous_bounding_rect):
            rect = self._colorbar_full_region_rect(region)
            if rect is not None:
                return rect
            if previous_handler is not None:
                return previous_handler()
            return QtCore.QRectF()

        def paint(painter, *args, previous_handler=previous_paint):
            mode = getattr(region, "_qplot_colorbar_drag_visual_area", "inside")
            if mode in ("inside", "outside"):
                rects = self._colorbar_drag_visual_rects(region, mode)
                if rects:
                    painter.setBrush(region.currentBrush)
                    painter.setPen(pg.mkPen(None))
                    for rect in rects:
                        painter.drawRect(rect)
                    return

            if previous_handler is not None:
                previous_handler(painter, *args)

        def mouse_drag(event, previous_handler=previous_mouse_drag_handler):
            active = getattr(region, "_qplot_colorbar_alt_range_drag_source", None)
            source, direction = self._colorbar_alt_range_drag_source_at(
                bar,
                region,
                event.buttonDownPos(),
                )
            if (
                    event.button() == QtCore.Qt.MouseButton.LeftButton
                    and (
                        active is not None
                        or (
                            source is not None
                            and getattr(region, "movable", True)
                            )
                        )
                    ):
                if active is not None:
                    source = active
                    direction = active[1]
                self._colorbar_alt_range_drag_event(
                    bar,
                    region,
                    event,
                    source,
                    direction,
                    region,
                    )
                return

            if previous_handler is not None:
                previous_handler(event)

        def hover(event, previous_handler=previous_hover_handler):
            if previous_handler is not None:
                previous_handler(event)

            if event.isExit():
                region._qplot_colorbar_drag_visual_area = None
            else:
                source, _direction = self._colorbar_alt_range_drag_source_at(
                    bar,
                    region,
                    event.pos(),
                    )
                region._qplot_colorbar_drag_visual_area = (
                    "outside" if source is not None else "inside"
                )
            region.update()

        region.boundingRect = bounding_rect
        region.paint = paint
        region.mouseDragEvent = mouse_drag
        region.hoverEvent = hover
        region._qplot_colorbar_drag_visual_area = None

        region._qplot_colorbar_alt_range_drag_installed = True

    def _install_colorbar_alt_handle_drag_handler(self, bar, region, line, index):
        """
        Patch one pyqtgraph colorbar handle for symmetric Alt/Option-drag.

        """
        if index not in (0, 1):
            return

        if getattr(line, "_qplot_colorbar_alt_range_drag_installed", False):
            return

        previous_mouse_drag_handler = getattr(line, "mouseDragEvent", None)
        source = ("line", index)
        direction = -1.0 if index == 0 else 1.0

        def mouse_drag(event, previous_handler=previous_mouse_drag_handler):
            modifiers = event.modifiers()
            active = getattr(region, "_qplot_colorbar_alt_range_drag_source", None)
            if (
                    event.button() == QtCore.Qt.MouseButton.LeftButton
                    and (
                        active == source
                        or (
                            modifiers & QtCore.Qt.KeyboardModifier.AltModifier
                            and getattr(line, "movable", True)
                            and getattr(region, "movable", True)
                            )
                        )
                    ):
                self._colorbar_alt_range_drag_event(
                    bar,
                    region,
                    event,
                    source,
                    direction,
                    line,
                    )
                return

            if previous_handler is not None:
                previous_handler(event)

        line.mouseDragEvent = mouse_drag
        line._qplot_colorbar_alt_range_drag_installed = True

    def _colorbar_full_region_rect(self, region):
        """
        Return the full colorbar interaction rectangle in region coordinates.

        """
        try:
            rect = QtCore.QRectF(region.viewRect())
        except (AttributeError, RuntimeError, TypeError):
            return None

        span = getattr(region, "span", (0, 1))
        orientation = getattr(region, "orientation", None)
        if orientation in ("vertical", 0):
            length = rect.height()
            rect.setBottom(rect.top() + length * span[1])
            rect.setTop(rect.top() + length * span[0])
        else:
            length = rect.width()
            rect.setRight(rect.left() + length * span[1])
            rect.setLeft(rect.left() + length * span[0])

        return rect.normalized()

    def _colorbar_drag_visual_rects(self, region, mode):
        """
        Return shaded rectangles for colorbar range-slide or outside-scale zones.

        """
        rect = self._colorbar_full_region_rect(region)
        if rect is None:
            return ()

        levels = self._colorbar_region_line_positions(region)
        if levels is None:
            return ()
        low, high = levels

        orientation = getattr(region, "orientation", None)
        if orientation in ("vertical", 0):
            inside = QtCore.QRectF(rect)
            inside.setLeft(low)
            inside.setRight(high)
            outside_low = QtCore.QRectF(
                rect.left(),
                rect.top(),
                low - rect.left(),
                rect.height(),
                )
            outside_high = QtCore.QRectF(
                high,
                rect.top(),
                rect.right() - high,
                rect.height(),
                )
        else:
            inside = QtCore.QRectF(rect)
            inside.setTop(low)
            inside.setBottom(high)
            outside_low = QtCore.QRectF(
                rect.left(),
                rect.top(),
                rect.width(),
                low - rect.top(),
                )
            outside_high = QtCore.QRectF(
                rect.left(),
                high,
                rect.width(),
                rect.bottom() - high,
                )

        if mode == "inside":
            return (inside.normalized(),)

        return tuple(
            outside.normalized()
            for outside in (outside_low, outside_high)
            if outside.width() > 0 and outside.height() > 0
            )

    def _colorbar_region_line_positions(self, region):
        """
        Return sorted colorbar handle positions in region coordinates.

        """
        lines = getattr(region, "lines", None)
        if lines is None or len(lines) != 2:
            return None

        positions = []
        for line in lines:
            value = getattr(line, "value", None)
            if callable(value):
                positions.append(float(value()))
            else:
                position = getattr(line, "position", None)
                if position is None:
                    return None
                positions.append(float(position))

        if not all(np.isfinite(position) for position in positions):
            return None

        return tuple(sorted(positions))

    def _colorbar_alt_range_drag_source_at(self, bar, region, position):
        """
        Return the outside-bar range-scale drag source and direction at a point.

        """
        levels = self._colorbar_region_line_positions(region)
        if levels is None:
            return None, 0.0

        axis_position = self._colorbar_alt_range_drag_axis_position(
            bar,
            position,
            region,
            )
        low, high = levels
        if axis_position < low:
            return ("bar", -1.0), -1.0
        if axis_position > high:
            return ("bar", 1.0), 1.0
        return None, 0.0

    def _colorbar_alt_range_drag_event(
            self,
            bar,
            region,
            event,
            source,
            direction,
            event_item,
            ):
        """
        Expand or contract the color range around its midpoint during Alt-drag.

        """
        event.accept()

        if event.isStart():
            levels = self._colorbar_levels_from_bar(bar)
            if levels is None:
                region._qplot_colorbar_alt_range_drag_active = False
                region._qplot_colorbar_alt_range_drag_source = None
                return

            region._qplot_colorbar_alt_range_drag_active = True
            region._qplot_colorbar_alt_range_drag_source = source
            region._qplot_colorbar_alt_range_drag_levels = levels
            region._qplot_colorbar_alt_range_drag_start_pos = (
                self._colorbar_alt_range_drag_axis_position(
                    bar,
                    event.buttonDownPos(),
                    event_item,
                    )
                )
            region._qplot_colorbar_drag_visual_area = "outside"

        if (
                not getattr(region, "_qplot_colorbar_alt_range_drag_active", False)
                or getattr(region, "_qplot_colorbar_alt_range_drag_source", None) != source
                ):
            return

        start_levels = region._qplot_colorbar_alt_range_drag_levels
        start_pos = region._qplot_colorbar_alt_range_drag_start_pos
        current_pos = self._colorbar_alt_range_drag_axis_position(
            bar,
            event.pos(),
            event_item,
            )
        axis_delta = (current_pos - start_pos) * direction
        self._set_colorbar_alt_range_drag_visual(region, axis_delta)
        levels = self._colorbar_alt_range_drag_levels(
            bar,
            start_levels,
            axis_delta,
            )
        if levels is not None:
            bar.setLevels(levels)
            self._colorbar_interactive_levels_changed(bar)

        if event.isFinish():
            region._qplot_colorbar_alt_range_drag_active = False
            region._qplot_colorbar_alt_range_drag_source = None
            region._qplot_colorbar_drag_visual_area = None
            self._set_colorbar_alt_range_drag_visual(region, 0.0)
            self._colorbar_interactive_levels_finished(bar)

    def _colorbar_alt_range_drag_axis_position(self, bar, position, item=None):
        """
        Return the colorbar-axis coordinate from a region mouse position.

        """
        if item is not None:
            try:
                position = item.mapToParent(position)
            except (AttributeError, RuntimeError):
                pass

        if getattr(bar, "horizontal", False):
            return position.x()

        return position.y()

    def _set_colorbar_alt_range_drag_visual(self, region, axis_delta):
        """
        Move pyqtgraph's range markers apart or together during Alt-drag.

        """
        lines = getattr(region, "lines", None)
        if lines is None or len(lines) != 2:
            return

        axis_delta = min(max(axis_delta, -63.0), 63.0)
        previous_block = getattr(region, "blockLineSignal", False)
        region.blockLineSignal = True
        try:
            lines[0].setPos(63.0 - axis_delta)
            lines[1].setPos(191.0 + axis_delta)
            region.prepareGeometryChange()
        finally:
            region.blockLineSignal = previous_block

    def _colorbar_alt_range_drag_levels(self, bar, start_levels, axis_delta):
        """
        Return levels widened or narrowed symmetrically by an Alt-drag delta.

        """
        low, high = start_levels
        span = high - low
        if not np.isfinite(span) or span <= 0:
            return None

        rounding = getattr(bar, "rounding", 0.0)
        try:
            rounding = float(rounding)
        except (TypeError, ValueError):
            rounding = 0.0
        if not np.isfinite(rounding) or rounding <= 0:
            rounding = 0.0

        drag_units = axis_delta / 64.0
        signed_drag = math.copysign(drag_units * drag_units, drag_units)
        scale = span + 2 * rounding
        center = (low + high) / 2.0
        half_span = span / 2.0 + scale * signed_drag
        min_span = rounding if rounding > 0 else max(abs(span) * 1e-9, 1e-300)
        half_span = max(half_span, min_span / 2.0)

        lo_lim = getattr(bar, "lo_lim", None)
        hi_lim = getattr(bar, "hi_lim", None)
        if lo_lim is not None and np.isfinite(lo_lim):
            half_span = min(half_span, center - lo_lim)
        if hi_lim is not None and np.isfinite(hi_lim):
            half_span = min(half_span, hi_lim - center)
        if half_span <= 0:
            return None

        low = center - half_span
        high = center + half_span

        if rounding > 0:
            low = rounding * round(low / rounding)
            high = rounding * round(high / rounding)

        if lo_lim is not None and np.isfinite(lo_lim):
            low = max(low, lo_lim)
        if hi_lim is not None and np.isfinite(hi_lim):
            high = min(high, hi_lim)
        if not np.isfinite(low) or not np.isfinite(high) or low >= high:
            return None

        return float(low), float(high)

    def _colorbar_levels_from_bar(self, bar):
        """
        Return validated numeric levels from a pyqtgraph colorbar.

        """
        try:
            low, high = bar.levels()
        except (AttributeError, TypeError, ValueError):
            return None

        try:
            low = float(low)
            high = float(high)
        except (TypeError, ValueError):
            return None

        if not np.isfinite(low) or not np.isfinite(high) or low >= high:
            return None

        return low, high

    def _colorbar_interactive_levels_changed(self, bar):
        """
        Mirror interactively adjusted levels into the scale dialog.

        """
        levels = self._colorbar_levels_from_bar(bar)
        if levels is None:
            return

        self._sync_colorbar_axis_scaling()
        self._sync_colorbar_level_fields(*levels)

    def _colorbar_interactive_levels_finished(self, bar):
        """
        Persist interactively adjusted colorbar levels as manual scaling.

        """
        levels = self._colorbar_levels_from_bar(bar)
        if levels is None:
            return

        self._colorbar_manual_levels = levels
        if "relevel_refresh" in self.__dict__:
            self.relevel_refresh.setChecked(False)
        if "colorbar_manual_radio" in self.__dict__:
            self.colorbar_manual_radio.setChecked(True)
        if "colorbar_auto_radio" in self.__dict__:
            self.colorbar_auto_radio.setChecked(False)
        self._sync_colorbar_level_fields(*levels)

    def _current_colorbar_levels(self):
        """
        Return the currently displayed colorbar levels for menu synchronisation.

        """
        bar = self.__dict__.get("bar")
        if bar is not None:
            return bar.levels()

        manual_levels = getattr(self, "_colorbar_manual_levels", None)
        if manual_levels is not None:
            return manual_levels

        return self._data_colorbar_levels()

    def _current_colorbar_colormap_name(self):
        """
        Return the selected colorbar color map preference.

        """
        available_colormaps = self._available_colorbar_colormaps()
        name = getattr(self, "_colorbar_colormap_name", None)
        if name in available_colormaps:
            return name

        name = _config_value(
            self.__dict__.get("config"),
            "user_preference.bar_colour",
            "viridis",
        )

        if name not in available_colormaps:
            name = self._fallback_colorbar_colormap_name()

        self._colorbar_colormap_name = name
        return name

    def _available_colorbar_colormaps(self):
        """
        Return color maps after applying persistent user filters.

        """
        config_obj = self.__dict__.get("config")
        include_cet = bool(_config_value(
            config_obj,
            "user_preference.bar_colour_include_cet",
            True,
        ))
        include_matplotlib = bool(_config_value(
            config_obj,
            "user_preference.bar_colour_include_matplotlib",
            True,
        ))
        include_local = bool(_config_value(
            config_obj,
            "user_preference.bar_colour_include_local",
            True,
        ))
        include_custom = bool(_config_value(
            config_obj,
            "user_preference.bar_colour_include_custom",
            True,
        ))
        include_cet_subtypes = {
            subtype: bool(_config_value(
                config_obj,
                _colorbar_subtype_config_key("cet", subtype),
                True,
            ))
            for subtype, _label in _CET_COLORBAR_SUBTYPES
        }
        include_matplotlib_subtypes = {
            subtype: bool(_config_value(
                config_obj,
                _colorbar_subtype_config_key("matplotlib", subtype),
                True,
            ))
            for subtype, _label in _MATPLOTLIB_COLORBAR_SUBTYPES
        }
        excluded_names = set(_string_list(_config_value(
            config_obj,
            "user_preference.bar_colour_excluded",
            [],
        )))
        excluded_prefixes = tuple(_string_list(_config_value(
            config_obj,
            "user_preference.bar_colour_excluded_prefixes",
            [],
        )))

        available = []
        for name in _COLORBAR_COLORMAPS:
            if name in _DEFAULT_HIDDEN_COLORBAR_NAMES:
                continue
            if name.startswith(_DEFAULT_HIDDEN_COLORBAR_PREFIXES):
                continue
            if name.endswith(_DEFAULT_HIDDEN_COLORBAR_SUFFIXES):
                continue
            if name in excluded_names:
                continue
            if excluded_prefixes and name.startswith(excluded_prefixes):
                continue

            group = _colorbar_colormap_group(name)
            if group == "cet" and not include_cet:
                continue
            if (
                    group == "cet"
                    and not include_cet_subtypes[_cet_colorbar_colormap_subtype(name)]
                    ):
                continue
            if group == "matplotlib" and not include_matplotlib:
                continue
            if (
                    group == "matplotlib"
                    and not include_matplotlib_subtypes[
                        _matplotlib_colorbar_colormap_subtype(name)
                        ]
                    ):
                continue
            if group == "pyqtgraph" and not include_local:
                continue
            if group == "custom" and not include_custom:
                continue

            available.append(name)

        return tuple(available)

    def _fallback_colorbar_colormap_name(self):
        """
        Choose a usable map when the saved preference is filtered out.

        """
        available_colormaps = self._available_colorbar_colormaps()
        if "viridis" in available_colormaps:
            return "viridis"
        if not available_colormaps:
            return "viridis"
        return available_colormaps[0]

    def _colorbar_colormap(self, name=None):
        """
        Return a pyqtgraph color map or built-in map name for the colorbar.

        """
        if name is None:
            name = self._current_colorbar_colormap_name()

        return _colorbar_colormap_for_name(name)

    def _init_colorbar_scale_controls(self):
        """
        Build manual/auto color scale controls for the dialog.

        """
        controls = qtw.QWidget()
        layout = qtw.QVBoxLayout(controls)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(8)

        self.colorbar_manual_radio = qtw.QRadioButton("Manual")
        self.colorbar_auto_radio = qtw.QRadioButton("Auto")
        self.colorbar_min_text = qtw.QLineEdit()
        self.colorbar_max_text = qtw.QLineEdit()
        self.colorbar_colormap_table = qtw.QTableWidget()
        self.colorbar_include_cet_check = qtw.QCheckBox("CET")
        self.colorbar_include_matplotlib_check = qtw.QCheckBox("Matplotlib")
        self.colorbar_include_local_check = qtw.QCheckBox("Local")
        self.colorbar_include_custom_check = qtw.QCheckBox("Custom")
        self.colorbar_cet_subtype_checks = {}
        self.colorbar_matplotlib_subtype_checks = {}
        for subtype, label in _CET_COLORBAR_SUBTYPES:
            self.colorbar_cet_subtype_checks[subtype] = qtw.QCheckBox(label)
        for subtype, label in _MATPLOTLIB_COLORBAR_SUBTYPES:
            self.colorbar_matplotlib_subtype_checks[subtype] = qtw.QCheckBox(label)
        self._init_colorbar_colormap_table()

        validator = QtGui.QDoubleValidator(self)
        self.colorbar_min_text.setValidator(validator)
        self.colorbar_max_text.setValidator(validator)
        for line_edit in (self.colorbar_min_text, self.colorbar_max_text):
            line_edit.setMinimumWidth(80)

        self.colorbar_button_group = qtw.QButtonGroup(self)
        self.colorbar_button_group.addButton(self.colorbar_manual_radio)
        self.colorbar_button_group.addButton(self.colorbar_auto_radio)
        self.colorbar_button_group.setExclusive(True)

        range_controls = qtw.QWidget()
        range_layout = qtw.QGridLayout(range_controls)
        range_layout.setContentsMargins(0, 0, 0, 0)
        range_layout.setHorizontalSpacing(4)
        range_layout.setVerticalSpacing(4)
        range_layout.addWidget(self.colorbar_manual_radio, 0, 0)
        range_layout.addWidget(self.colorbar_min_text, 0, 1)
        range_layout.addWidget(self.colorbar_max_text, 0, 2)
        range_layout.addWidget(self.colorbar_auto_radio, 1, 0)

        filter_controls = self._init_colorbar_filter_controls()

        layout.addWidget(qtw.QLabel("Colors"))
        layout.addWidget(filter_controls)
        layout.addWidget(self.colorbar_colormap_table, 1)
        layout.addWidget(range_controls)

        self.colorbar_scale_controls = controls

        self.colorbar_colormap_table.itemSelectionChanged.connect(
            self._colorbar_colormap_selection_changed
            )
        self.colorbar_include_cet_check.toggled.connect(
            self._colorbar_include_cet_changed
            )
        self.colorbar_include_matplotlib_check.toggled.connect(
            self._colorbar_include_matplotlib_changed
            )
        self.colorbar_include_local_check.toggled.connect(
            self._colorbar_include_local_changed
            )
        self.colorbar_include_custom_check.toggled.connect(
            self._colorbar_include_custom_changed
            )
        for subtype, widget in self.colorbar_cet_subtype_checks.items():
            widget.toggled.connect(
                lambda enabled, subtype=subtype: self._colorbar_include_subtype_changed(
                    "cet",
                    subtype,
                    enabled,
                )
            )
        for subtype, widget in self.colorbar_matplotlib_subtype_checks.items():
            widget.toggled.connect(
                lambda enabled, subtype=subtype: self._colorbar_include_subtype_changed(
                    "matplotlib",
                    subtype,
                    enabled,
                )
            )
        self.colorbar_manual_radio.clicked.connect(self._apply_colorbar_manual_fields)
        self.colorbar_min_text.editingFinished.connect(self._apply_colorbar_manual_fields)
        self.colorbar_max_text.editingFinished.connect(self._apply_colorbar_manual_fields)
        self.colorbar_auto_radio.clicked.connect(self.setColorbarAuto)

        self._sync_colorbar_scale_controls()

    def _init_colorbar_filter_controls(self):
        """
        Build broad and subtype color-map filter controls.

        """
        controls = qtw.QWidget()
        layout = qtw.QGridLayout(controls)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(2)

        layout.addWidget(qtw.QLabel("Show"), 0, 0)
        layout.addWidget(self.colorbar_include_cet_check, 0, 1)
        layout.addWidget(self.colorbar_include_matplotlib_check, 0, 2)
        layout.addWidget(self.colorbar_include_local_check, 0, 3)
        layout.addWidget(self.colorbar_include_custom_check, 0, 4)

        layout.addWidget(qtw.QLabel("CET"), 1, 0)
        for column, (_subtype, widget) in enumerate(
                self.colorbar_cet_subtype_checks.items(),
                1,
                ):
            layout.addWidget(widget, 1, column)

        layout.addWidget(qtw.QLabel("Matplotlib"), 2, 0)
        for column, (_subtype, widget) in enumerate(
                self.colorbar_matplotlib_subtype_checks.items(),
                1,
                ):
            layout.addWidget(widget, 2, column)

        layout.setColumnStretch(7, 1)
        return controls

    def _init_colorbar_colormap_table(self):
        """
        Build the scrollable color-map chooser with rendered previews.

        """
        table = self.colorbar_colormap_table
        table.setColumnCount(3)
        table.setHorizontalHeaderLabels(("Color map", "Preview", "Type"))
        table.verticalHeader().hide()
        table.setShowGrid(False)
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(qtw.QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(qtw.QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(qtw.QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSortingEnabled(True)
        table.setMinimumSize(560, 360)
        table.setIconSize(QtCore.QSize(220, 14))
        table.setItemDelegateForColumn(1, _CenteredIconDelegate(table))

        header = table.horizontalHeader()
        header.setSectionResizeMode(0, qtw.QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, qtw.QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, qtw.QHeaderView.ResizeMode.ResizeToContents)

        self._populate_colorbar_colormap_table()

    def _populate_colorbar_colormap_table(self):
        """
        Fill the color-map table using the current filter settings.

        """
        table = self.colorbar_colormap_table
        table.blockSignals(True)
        sorting_enabled = table.isSortingEnabled()
        table.setSortingEnabled(False)
        table.setRowCount(0)

        colormaps = self._available_colorbar_colormaps()
        table.setRowCount(len(colormaps))
        for row, name in enumerate(colormaps):
            label = _COLORBAR_COLORMAP_LABELS.get(name, name)
            type_label = _colorbar_colormap_type_label(name)
            name_item = _ColorbarColormapTableItem(label)
            name_item.setData(QtCore.Qt.ItemDataRole.UserRole, name)
            name_item.setData(_COLORBAR_TABLE_SORT_ROLE, label)
            type_item = _ColorbarColormapTableItem(type_label)
            type_item.setData(QtCore.Qt.ItemDataRole.UserRole, name)
            type_item.setData(_COLORBAR_TABLE_SORT_ROLE, type_label)
            preview_item = _ColorbarColormapTableItem()
            preview_item.setData(QtCore.Qt.ItemDataRole.UserRole, name)
            preview_item.setData(_COLORBAR_TABLE_SORT_ROLE, label)
            preview_item.setIcon(QtGui.QIcon(_colorbar_colormap_preview(name)))

            table.setItem(row, 0, name_item)
            table.setItem(row, 1, preview_item)
            table.setItem(row, 2, type_item)
            table.setRowHeight(row, 20)

        table.resizeColumnToContents(0)
        table.resizeColumnToContents(2)
        table.setSortingEnabled(sorting_enabled)
        table.blockSignals(False)

    def _sync_colorbar_filter_controls(self):
        """
        Mirror persisted broad color-map filters into the dialog controls.

        """
        config_obj = self.__dict__.get("config")
        for widget, key in (
                (
                    self.colorbar_include_cet_check,
                    "user_preference.bar_colour_include_cet",
                    ),
                (
                    self.colorbar_include_matplotlib_check,
                    "user_preference.bar_colour_include_matplotlib",
                    ),
                (
                    self.colorbar_include_local_check,
                    "user_preference.bar_colour_include_local",
                    ),
                (
                    self.colorbar_include_custom_check,
                    "user_preference.bar_colour_include_custom",
                    ),
                ):
            widget.blockSignals(True)
            widget.setChecked(bool(_config_value(config_obj, key, True)))
            widget.blockSignals(False)

        include_cet = self.colorbar_include_cet_check.isChecked()
        include_matplotlib = self.colorbar_include_matplotlib_check.isChecked()
        for subtype, widget in self.colorbar_cet_subtype_checks.items():
            key = _colorbar_subtype_config_key("cet", subtype)
            widget.blockSignals(True)
            widget.setChecked(bool(_config_value(config_obj, key, True)))
            widget.setEnabled(include_cet)
            widget.blockSignals(False)

        for subtype, widget in self.colorbar_matplotlib_subtype_checks.items():
            key = _colorbar_subtype_config_key("matplotlib", subtype)
            widget.blockSignals(True)
            widget.setChecked(bool(_config_value(config_obj, key, True)))
            widget.setEnabled(include_matplotlib)
            widget.blockSignals(False)

    def _set_colorbar_filter_setting(self, key, enabled):
        """
        Persist a broad color-map filter and rebuild the chooser.

        """
        config_obj = self.__dict__.get("config")
        if config_obj is not None:
            config_obj.update(key, bool(enabled))

        self._populate_colorbar_colormap_table()
        self._sync_colorbar_scale_controls()

    def _set_colorbar_subtype_filter_setting(self, group, subtype, enabled):
        """
        Persist a color-map subtype filter and rebuild the chooser.

        """
        self._set_colorbar_filter_setting(
            _colorbar_subtype_config_key(group, subtype),
            enabled,
        )

    @QtCore.pyqtSlot(bool)
    def _colorbar_include_cet_changed(self, enabled):
        """
        Include or exclude CET color maps in the chooser.

        """
        self._set_colorbar_filter_setting(
            "user_preference.bar_colour_include_cet",
            enabled,
        )

    @QtCore.pyqtSlot(bool)
    def _colorbar_include_matplotlib_changed(self, enabled):
        """
        Include or exclude matplotlib color maps in the chooser.

        """
        self._set_colorbar_filter_setting(
            "user_preference.bar_colour_include_matplotlib",
            enabled,
        )

    @QtCore.pyqtSlot(bool)
    def _colorbar_include_local_changed(self, enabled):
        """
        Include or exclude pyqtgraph local color maps in the chooser.

        """
        self._set_colorbar_filter_setting(
            "user_preference.bar_colour_include_local",
            enabled,
        )

    @QtCore.pyqtSlot(bool)
    def _colorbar_include_custom_changed(self, enabled):
        """
        Include or exclude app-provided custom color maps in the chooser.

        """
        self._set_colorbar_filter_setting(
            "user_preference.bar_colour_include_custom",
            enabled,
        )

    def _colorbar_include_subtype_changed(self, group, subtype, enabled):
        """
        Include or exclude a color-map subtype in the chooser.

        """
        self._set_colorbar_subtype_filter_setting(group, subtype, enabled)

    def _sync_colorbar_scale_controls(self):
        """
        Update color scale controls from the current colorbar state.

        """
        levels = self._current_colorbar_levels()
        if levels is not None:
            self._sync_colorbar_level_fields(*levels)

        table = getattr(self, "colorbar_colormap_table", None)
        if table is not None:
            self._sync_colorbar_filter_controls()
            self._select_colorbar_colormap(self._current_colorbar_colormap_name())

        manual = getattr(self, "_colorbar_manual_levels", None) is not None
        for widget in (self.colorbar_manual_radio, self.colorbar_auto_radio):
            widget.blockSignals(True)

        self.colorbar_manual_radio.setChecked(manual)
        self.colorbar_auto_radio.setChecked(not manual)

        for widget in (self.colorbar_manual_radio, self.colorbar_auto_radio):
            widget.blockSignals(False)

    def _sync_colorbar_level_fields(self, vmin, vmax):
        """
        Mirror colorbar levels into the dialog text fields.

        """
        if "colorbar_min_text" not in self.__dict__:
            return

        for widget, value in (
                (self.colorbar_min_text, vmin),
                (self.colorbar_max_text, vmax),
                ):
            widget.blockSignals(True)
            widget.setText(f"{value:.6g}")
            widget.blockSignals(False)

    def _colorbar_colormap_row(self, name):
        """
        Return the table row for a color map name.

        """
        table = getattr(self, "colorbar_colormap_table", None)
        if table is None:
            return -1

        for row in range(table.rowCount()):
            item = table.item(row, 0)
            if item is not None and item.data(QtCore.Qt.ItemDataRole.UserRole) == name:
                return row

        return -1

    def _select_colorbar_colormap(self, name):
        """
        Select the current color map row without applying it again.

        """
        table = getattr(self, "colorbar_colormap_table", None)
        if table is None:
            return

        row = self._colorbar_colormap_row(name)
        if row < 0:
            row = self._colorbar_colormap_row("viridis")
        if row < 0:
            return

        table.blockSignals(True)
        table.setCurrentCell(row, 0)
        table.selectRow(row)
        table.blockSignals(False)

    @QtCore.pyqtSlot()
    def _colorbar_colormap_selection_changed(self):
        """
        Apply the color map selected in the dialog.

        """
        table = self.colorbar_colormap_table
        row = table.currentRow()
        if row < 0:
            return

        item = table.item(row, 0)
        if item is None:
            return

        name = item.data(QtCore.Qt.ItemDataRole.UserRole)
        if name is None:
            return

        self.setColorbarColorMap(name)

    @QtCore.pyqtSlot(int)
    def _colorbar_colormap_changed(self, index):
        """
        Compatibility slot for older combo-box based tests or callers.

        """
        combo = getattr(self, "colorbar_colormap_combo", None)
        if combo is None:
            return

        name = combo.itemData(index)
        if name is not None:
            self.setColorbarColorMap(name)

    @QtCore.pyqtSlot()
    def open_colorbar_scale_dialog(self):
        """
        Opens the color scale controls in a dialog.

        """
        controls = getattr(self, "colorbar_scale_controls", None)
        if controls is None:
            return

        self._populate_colorbar_colormap_table()
        self._sync_colorbar_scale_controls()

        dialog = getattr(self, "colorbar_scale_dialog", None)
        if dialog is None:
            dialog = qtw.QDialog(self)
            dialog.setWindowTitle("Color scale")
            dialog.resize(520, 560)
            layout = qtw.QVBoxLayout(dialog)
            layout.addWidget(controls)

            buttons = qtw.QDialogButtonBox(qtw.QDialogButtonBox.StandardButton.Close)
            buttons.rejected.connect(dialog.close)
            layout.addWidget(buttons)

            self.colorbar_scale_dialog = dialog

        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    @QtCore.pyqtSlot()
    def _apply_colorbar_manual_fields(self):
        """
        Apply color scale levels entered in the dialog.

        """
        try:
            vmin = float(self.colorbar_min_text.text())
            vmax = float(self.colorbar_max_text.text())
        except ValueError:
            self.show_status("Invalid color scale range.", 5000)
            self._sync_colorbar_scale_controls()
            return

        self.setColorbarManualRange(vmin, vmax)

    def setColorbarColorMap(self, name):
        """
        Set and persist the colorbar color map.

        """
        if name not in self._available_colorbar_colormaps():
            show_status = getattr(self, "show_status", None)
            if show_status is not None:
                show_status("Unknown color map.", 5000)
            return False

        self._colorbar_colormap_name = name
        bar = self.__dict__.get("bar")
        if bar is not None:
            bar.setColorMap(self._colorbar_colormap(name))

        config_obj = self.__dict__.get("config")
        if config_obj is not None:
            config_obj.update("user_preference.bar_colour", name)

        return True

    def setColorbarManualRange(self, vmin, vmax):
        """
        Set a persistent manual color scale range.

        """
        if not np.isfinite(vmin) or not np.isfinite(vmax) or vmin >= vmax:
            self.show_status("Color scale minimum must be below maximum.", 5000)
            self._sync_colorbar_scale_controls()
            return False

        self._colorbar_manual_levels = (float(vmin), float(vmax))

        if "relevel_refresh" in self.__dict__:
            self.relevel_refresh.setChecked(False)

        self._set_colorbar_levels(*self._colorbar_manual_levels)
        if "colorbar_manual_radio" in self.__dict__:
            self.colorbar_manual_radio.setChecked(True)
        return True

    @QtCore.pyqtSlot()
    def setColorbarAuto(self):
        """
        Return the color scale to automatic data-range scaling.

        """
        self._colorbar_manual_levels = None
        if "relevel_refresh" in self.__dict__:
            self.relevel_refresh.setChecked(True)
        self.scaleColorbar()

    @QtCore.pyqtSlot(bool)
    def _colorbar_auto_refresh_changed(self, enabled):
        """
        Keep colorbar manual state in sync with the refresh toolbar checkbox.

        """
        if enabled:
            self._colorbar_manual_levels = None
            self.scaleColorbar()

###############################################################################
# Subplot control
