"""Database/run provenance drawn in the plot scene, including exports."""

from html import escape
from pathlib import Path

import pyqtgraph as pg
from PyQt6 import QtCore, QtGui, QtWidgets

from qplot.configuration.fonts import FONT_SIZE_KEY

SHOW_SOURCE_ANNOTATION_KEY = "user_preference.show_source_annotation"


def _run_ranges(run_ids):
    """Compress sequences of three or more; leave pairs as separate IDs."""
    runs = sorted(set(run_ids))
    groups = []
    start = 0
    while start < len(runs):
        end = start
        while end + 1 < len(runs) and runs[end + 1] == runs[end] + 1:
            end += 1
        if end - start >= 2:
            groups.append(f"{runs[start]}-{runs[end]}")
        else:
            groups.extend(str(run) for run in runs[start:end + 1])
        start = end + 1
    return ", ".join(groups)


def format_plot_sources(sources, *, rich_text=False):
    """Group (database path, run ID, parameter label) in encounter order."""
    databases = {}
    for database, run_id, parameter in sources:
        parameters = databases.setdefault(database, {})
        parameters.setdefault(parameter, set()).add(run_id)
    names = [Path(database).stem for database in databases]
    lines = []
    for database, parameters in databases.items():
        name = Path(database).stem
        if names.count(name) > 1:
            name = str(database)
        groups = "; ".join(
            f"{_run_ranges(runs)} ({parameter})"
            for parameter, runs in parameters.items()
        )
        lines.append(
            f"<b>{escape(name)}:</b> {escape(groups)}"
            if rich_text else f"{name}: {groups}"
        )
    return ("<br>" if rich_text else "\n").join(lines)


class PlotSourceLabel(pg.GraphicsWidget):
    """A shaded source strip that wraps to the plot width."""

    def __init__(self):
        super().__init__()
        self.item = QtWidgets.QGraphicsTextItem(self)
        self.item.document().setDocumentMargin(0)
        self.text = ""
        self.rich_text = None
        self.background = QtGui.QColor("white")
        self._height = 0.0
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )

    def set_source_text(self, text, font, color, *, rich_text, background=None):
        background = QtGui.QColor("white") if background is None else background
        if (text == self.text and font == self.item.font()
                and color == self.item.defaultTextColor()
                and rich_text == self.rich_text and background == self.background):
            return
        self.text = text
        self.rich_text = rich_text
        self.background = background
        self.item.setFont(font)
        self.item.setDefaultTextColor(color)
        self.item.setHtml(rich_text)
        self._wrap()
        self.update()

    def paint(self, painter, option, widget=None):
        if not self.text:
            return
        foreground = self.item.defaultTextColor()

        def tint(amount):
            return QtGui.QColor.fromRgbF(*(
                base * (1 - amount) + ink * amount
                for base, ink in zip(
                    self.background.getRgbF()[:3], foreground.getRgbF()[:3], strict=True,
                )
            ))

        painter.setBrush(tint(0.05))
        painter.setPen(QtGui.QPen(tint(0.18), 1))
        painter.drawRect(self.boundingRect().adjusted(0.5, 0.5, -0.5, -4.5))

    def _wrap(self):
        horizontal, vertical = 8, 5
        self.item.setPos(horizontal, vertical)
        self.item.setTextWidth(max(1.0, (self.size().width() or 600.0) - 2 * horizontal))
        height = self.item.boundingRect().height() + 2 * vertical + 4 if self.text else 0.0
        if height != self._height:
            self._height = height
            self.setMinimumHeight(height)
            self.setMaximumHeight(height)
            self.updateGeometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._wrap()

    def sizeHint(self, which, constraint=None):
        width = {
            QtCore.Qt.SizeHint.MinimumSize: 0,
            QtCore.Qt.SizeHint.PreferredSize: 300,
            QtCore.Qt.SizeHint.MaximumSize: 16777215,
        }.get(which, 0)
        return QtCore.QSizeF(width, self._height)


class PlotAnnotationMixin:
    def _init_plot_annotation(self):
        self.source_annotation = PlotSourceLabel()
        # PlotItem reserves row zero for its title, aligned with the data axes.
        self.plot.layout.removeItem(self.plot.titleLabel)
        self.plot.titleLabel.hide()
        self.plot.layout.setRowMinimumHeight(0, 0)
        self.plot.layout.setRowMaximumHeight(0, 16777215)
        self.plot.layout.setRowPreferredHeight(0, -1)
        self.plot.layout.addItem(self.source_annotation, 0, 1)
        self._annotation_in_layout = True
        self.trace_updated.connect(self._update_plot_annotation)
        self._update_plot_annotation()

    def _set_plot_annotation_visible(self, visible):
        if visible != self._annotation_in_layout:
            if visible:
                self.plot.layout.setRowMinimumHeight(0, 0)
                self.plot.layout.setRowMaximumHeight(0, 16777215)
                self.plot.layout.setRowPreferredHeight(0, -1)
                self.plot.layout.addItem(self.source_annotation, 0, 1)
            else:
                self.plot.layout.removeItem(self.source_annotation)
                self.plot.layout.setRowFixedHeight(0, 0)
            self._annotation_in_layout = visible
        self.source_annotation.setVisible(visible)

    def _update_plot_annotation(self):
        annotation = self.__dict__.get("source_annotation")
        if annotation is None:
            return
        visible = self.config.get(SHOW_SOURCE_ANNOTATION_KEY)
        self._set_plot_annotation_visible(visible)
        if not visible:
            return
        sources = []
        if "lines" not in self.__dict__ and self.__dict__.get("line") is not None:
            sources.append(self)  # A standalone heatmap cut has no trace registry.
        styles = self.__dict__.get("_trace_styles", {})
        for key, line in self.__dict__.get("lines", {}).items():
            if line is None:
                continue
            style = styles.get(key)
            if style is not None and (not style.visible or style.opacity == 0):
                continue
            source = self if line is self.__dict__.get("line") else line.from_win
            sources.append(source)
        for layer in self.__dict__.get("heatmaps", {}).values():
            if self._heatmap_is_visible(layer) and self._heatmap_opacity(layer) > 0:
                sources.append(self if layer is self else layer.from_win)
        # Keep the primary source first even when appearance rows are reordered.
        sources.sort(key=lambda source: source is not self)
        source_info = [source._plot_source_info for source in sources]
        text = format_plot_sources(source_info)
        font = QtGui.QFont(QtWidgets.QApplication.font())
        font.setPointSize(self.config.get(FONT_SIZE_KEY))
        annotation.set_source_text(
            text, font, pg.mkColor(self.config.theme.plot_foreground),
            rich_text=format_plot_sources(source_info, rich_text=True),
            background=pg.mkColor(self.config.theme.plot_background),
        )
