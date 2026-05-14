import math

from PyQt5 import QtWidgets as qtw
from PyQt5 import QtCore, QtGui

import pyqtgraph as pg
from pyqtgraph.graphicsItems.ButtonItem import ButtonItem

import numpy as np

from ._plotWin import _axis_scale_power_text, plotWidget
from ._subplots.subplot2d import sweeper


_PREFERRED_COLORBAR_COLORMAPS = (
    "viridis",
    "plasma",
    "inferno",
    "magma",
    "cividis",
    "turbo",
    "Greys",
    "Purples",
    "Blues",
    "Greens",
    "Oranges",
    "Reds",
)


_DEFAULT_HIDDEN_COLORBAR_PREFIXES = ("gist",)
_DEFAULT_HIDDEN_COLORBAR_SUFFIXES = ("_r",)
_DEFAULT_HIDDEN_COLORBAR_NAMES = ("gray", "grey", "Grays")


_CET_COLORBAR_SUBTYPES = (
    ("linear", "Linear"),
    ("divergent", "Divergent"),
    ("cyclic", "Cyclic"),
    ("rainbow", "Rainbow"),
    ("isoluminant", "Isoluminant"),
    ("other", "Other"),
)


_MATPLOTLIB_COLORBAR_SUBTYPES = (
    ("perceptual", "Perceptual"),
    ("sequential", "Sequential"),
    ("divergent", "Divergent"),
    ("cyclic", "Cyclic"),
    ("qualitative", "Qualitative"),
    ("other", "Other"),
)


_MATPLOTLIB_PERCEPTUAL_COLORBAR_COLORMAPS = {
    "cividis",
    "inferno",
    "magma",
    "plasma",
    "turbo",
    "viridis",
}


_MATPLOTLIB_SEQUENTIAL_COLORBAR_COLORMAPS = {
    "afmhot",
    "autumn",
    "binary",
    "Blues",
    "bone",
    "BuGn",
    "BuPu",
    "cool",
    "copper",
    "GnBu",
    "Greens",
    "Greys",
    "hot",
    "Oranges",
    "OrRd",
    "pink",
    "PuBu",
    "PuBuGn",
    "PuRd",
    "Purples",
    "RdPu",
    "Reds",
    "spring",
    "summer",
    "Wistia",
    "winter",
    "YlGn",
    "YlGnBu",
    "YlOrBr",
    "YlOrRd",
}


_MATPLOTLIB_DIVERGENT_COLORBAR_COLORMAPS = {
    "berlin",
    "BrBG",
    "bwr",
    "coolwarm",
    "managua",
    "PiYG",
    "PRGn",
    "PuOr",
    "RdBu",
    "RdGy",
    "RdYlBu",
    "RdYlGn",
    "seismic",
    "Spectral",
    "vanimo",
}


_MATPLOTLIB_CYCLIC_COLORBAR_COLORMAPS = {
    "hsv",
    "twilight",
    "twilight_shifted",
}


_MATPLOTLIB_QUALITATIVE_COLORBAR_COLORMAPS = {
    "Accent",
    "Dark2",
    "Paired",
    "Pastel1",
    "Pastel2",
    "Set1",
    "Set2",
    "Set3",
    "tab10",
    "tab20",
    "tab20b",
    "tab20c",
}


_COLORBAR_COLORMAP_LABELS = {
    "cividis": "Cividis",
    "inferno": "Inferno",
    "magma": "Magma",
    "plasma": "Plasma",
    "turbo": "Turbo",
    "viridis": "Viridis",
}


_COLORBAR_TABLE_SORT_ROLE = QtCore.Qt.UserRole + 1


class _ColorbarColormapTableItem(qtw.QTableWidgetItem):
    """
    Table item that sorts by an explicit, case-insensitive key.

    """

    def __lt__(self, other):
        left = self.data(_COLORBAR_TABLE_SORT_ROLE)
        right = other.data(_COLORBAR_TABLE_SORT_ROLE)
        if left is not None and right is not None:
            return str(left).casefold() < str(right).casefold()
        return super().__lt__(other)


_CUSTOM_COLORBAR_COLORMAPS = {
    "Greys": [
        (255, 255, 255),
        (217, 217, 217),
        (150, 150, 150),
        (82, 82, 82),
        (0, 0, 0),
    ],
    "Purples": [
        (252, 251, 253),
        (218, 218, 235),
        (158, 154, 200),
        (106, 81, 163),
        (63, 0, 125),
    ],
    "Blues": [
        (247, 251, 255),
        (198, 219, 239),
        (107, 174, 214),
        (33, 113, 181),
        (8, 48, 107),
    ],
    "Greens": [
        (247, 252, 245),
        (199, 233, 192),
        (116, 196, 118),
        (35, 139, 69),
        (0, 68, 27),
    ],
    "Oranges": [
        (255, 245, 235),
        (253, 208, 162),
        (253, 141, 60),
        (217, 72, 1),
        (127, 39, 4),
    ],
    "Reds": [
        (255, 245, 240),
        (252, 187, 161),
        (251, 106, 74),
        (203, 24, 29),
        (103, 0, 13),
    ],
}


def _list_pyqtgraph_colormaps(source=None):
    """
    Return color maps advertised by pyqtgraph for an optional source.

    """
    try:
        return list(pg.colormap.listMaps(source=source))
    except Exception:
        return []


def _config_value(config_obj, key, default):
    """
    Read a config value, falling back when running against older configs.

    """
    if config_obj is None:
        return default

    try:
        return config_obj.get(key)
    except (AttributeError, KeyError):
        return default


def _string_list(value):
    """
    Normalise config list values used by color-map filters.

    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value]

    try:
        return [str(item) for item in value if str(item)]
    except TypeError:
        return []


def _colorbar_subtype_config_key(group, subtype):
    return f"user_preference.bar_colour_include_{group}_{subtype}"


def _build_colorbar_colormap_catalog():
    """
    Build the colorbar map list and remember which pyqtgraph source owns each.

    """
    colormaps = []
    sources = {}
    default_maps = _list_pyqtgraph_colormaps()
    matplotlib_maps = _list_pyqtgraph_colormaps(source="matplotlib")

    def add_colormap(name, source=None):
        if name in sources:
            return
        colormaps.append(name)
        sources[name] = source

    for name in _PREFERRED_COLORBAR_COLORMAPS:
        if name in _CUSTOM_COLORBAR_COLORMAPS:
            add_colormap(name, "custom")
        elif name in matplotlib_maps:
            add_colormap(name, "matplotlib")
        elif name in default_maps:
            add_colormap(name)

    for source, maps in ((None, default_maps), ("matplotlib", matplotlib_maps)):
        for name in maps:
            add_colormap(name, source)

    for name in _CUSTOM_COLORBAR_COLORMAPS:
        add_colormap(name, "custom")

    return tuple(colormaps), sources


_COLORBAR_COLORMAPS, _COLORBAR_COLORMAP_SOURCES = _build_colorbar_colormap_catalog()


def _colorbar_colormap_group(name):
    """
    Return the broad group used for color-map filtering.

    """
    source = _COLORBAR_COLORMAP_SOURCES.get(name)
    if source == "custom":
        return "custom"
    if name.startswith("CET-"):
        return "cet"
    if source == "matplotlib":
        return "matplotlib"
    return "pyqtgraph"


def _cet_colorbar_colormap_subtype(name):
    """
    Return the CET subtype used by the color-map filters.

    """
    if name.startswith(("CET-CBL", "CET-CBTL", "CET-L")):
        return "linear"
    if name.startswith(("CET-CBD", "CET-CBTD", "CET-D")):
        return "divergent"
    if name.startswith("CET-C"):
        return "cyclic"
    if name.startswith("CET-R"):
        return "rainbow"
    if name.startswith("CET-I"):
        return "isoluminant"
    return "other"


def _matplotlib_colorbar_colormap_subtype(name):
    """
    Return the matplotlib subtype used by the color-map filters.

    """
    if name in _MATPLOTLIB_PERCEPTUAL_COLORBAR_COLORMAPS:
        return "perceptual"
    if name in _MATPLOTLIB_SEQUENTIAL_COLORBAR_COLORMAPS:
        return "sequential"
    if name in _MATPLOTLIB_DIVERGENT_COLORBAR_COLORMAPS:
        return "divergent"
    if name in _MATPLOTLIB_CYCLIC_COLORBAR_COLORMAPS:
        return "cyclic"
    if name in _MATPLOTLIB_QUALITATIVE_COLORBAR_COLORMAPS:
        return "qualitative"
    return "other"


def _colorbar_colormap_type_label(name):
    """
    Return the source/type label shown in the color scale chooser.

    """
    group = _colorbar_colormap_group(name)
    if group == "cet":
        subtype = _cet_colorbar_colormap_subtype(name)
        labels = dict(_CET_COLORBAR_SUBTYPES)
        return f"CET - {labels.get(subtype, subtype.title())}"
    if group == "matplotlib":
        subtype = _matplotlib_colorbar_colormap_subtype(name)
        labels = dict(_MATPLOTLIB_COLORBAR_SUBTYPES)
        return f"Matplotlib - {labels.get(subtype, subtype.title())}"
    if group == "custom":
        return "QCoDeS Plotter - Custom"
    return "PyQtGraph - Local"


def _colorbar_colormap_for_name(name):
    """
    Return a color map object, custom map, or special pyqtgraph map name.

    """
    source = _COLORBAR_COLORMAP_SOURCES.get(name)
    if source == "custom":
        colors = _CUSTOM_COLORBAR_COLORMAPS.get(name)
        if colors is not None:
            color_map = pg.ColorMap(
                np.linspace(0.0, 1.0, len(colors)),
                colors,
            )
            color_map.name = name
            return color_map

    try:
        return pg.colormap.get(name, source=source)
    except Exception:
        pass

    colors = _CUSTOM_COLORBAR_COLORMAPS.get(name)
    if colors is None:
        return name

    color_map = pg.ColorMap(
        np.linspace(0.0, 1.0, len(colors)),
        colors,
    )
    color_map.name = name
    return color_map


def _colorbar_colormap_preview(name, width=220, height=14):
    """
    Render a compact pixmap preview for a colorbar color map.

    """
    pixmap = QtGui.QPixmap(width, height)
    pixmap.fill(QtGui.QColor("transparent"))

    painter = QtGui.QPainter(pixmap)
    try:
        color_map = _colorbar_colormap_for_name(name)
        if isinstance(color_map, pg.ColorMap):
            lookup_table = color_map.getLookupTable(nPts=width, alpha=True)
            for x, color in enumerate(lookup_table):
                painter.setPen(QtGui.QColor(*[int(channel) for channel in color[:4]]))
                painter.drawLine(x, 0, x, height - 1)
        else:
            painter.fillRect(0, 0, width, height, QtGui.QColor(255, 255, 255))

        painter.setPen(QtGui.QColor(120, 120, 120))
        painter.drawRect(0, 0, width - 1, height - 1)
    finally:
        painter.end()

    return pixmap


def _letter_button_pixmap(letter, size=20):
    """
    Render a pyqtgraph-style circular letter button.

    """
    pixmap = QtGui.QPixmap(size, size)
    pixmap.fill(QtCore.Qt.transparent)

    painter = QtGui.QPainter(pixmap)
    try:
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.setPen(QtGui.QPen(QtGui.QColor(70, 70, 70, 230), 1.2))
        painter.setBrush(QtGui.QColor(235, 235, 235, 225))
        painter.drawEllipse(QtCore.QRectF(1, 1, size - 2, size - 2))

        font = QtGui.QFont()
        font.setBold(True)
        font.setPointSize(11)
        painter.setFont(font)
        painter.setPen(QtGui.QColor(40, 40, 40))
        painter.drawText(pixmap.rect(), QtCore.Qt.AlignCenter, letter)
    finally:
        painter.end()

    return pixmap


class plot2d(plotWidget):
    """
    Plot window for 2d and higher plots, aka Heatmaps.
    Inherits and wraps several functions from qplot.windows._plotWin.plotWidget.
    PlotWidget handles majority of set up, recommend to view first.
    
    Key functions to see in plot2d:
        initFrame
        refreshPlot
        
    """
    open_subplot = QtCore.pyqtSignal([object, str, tuple])
    sweep_moved = QtCore.pyqtSignal([int, int])
    
    def __init__(self, 
                 *args,
                 **kargs,
                 ):
        super().__init__(*args, **kargs)
        self.sweep_id = 0
        self.sweep_lines = {}
        self.active_sweep_line_id = None
        self.rotate = None # FOR SUBPLOT CURSOR
        self._colorbar_manual_levels = None

        
    def initFrame(self):
        """
        Sets up the initial plot and starting data.

        """
        self.image = pg.ImageItem(axisOrder='row-major')
        self.image.setZValue(0) # Like *Send to back*
        # self.image.setPxMode(True)
        
        self.plot.addItem(self.image)
        self._init_color_autoscale_button()
        self.hover_pixel_outline = qtw.QGraphicsRectItem()
        self.hover_pixel_outline.setPen(
            pg.mkPen((255, 255, 255, 190), width=1.5, cosmetic=True)
        )
        self.hover_pixel_outline.setBrush(QtGui.QBrush(QtCore.Qt.NoBrush))
        self.hover_pixel_outline.setZValue(10)
        self.hover_pixel_outline.hide()
        self.plot.addItem(self.hover_pixel_outline)
        
        # Wait for loader to finish to enure needed data is collected.
        self.load_data()
        self.show_status("Heatmap ready; loading data...", 5000)
      

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
     
        
    def initRefresh(self, refresh):
        super().initRefresh(refresh)
        
        self.toolbarRef.addSeparator()
        self.toolbarRef.addWidget(qtw.QLabel("On refresh:  "))
        
        self.toolbarRef.addWidget(qtw.QLabel("Re-Map Colors "))
        
        self.relevel_refresh = qtw.QCheckBox()
        self.relevel_refresh.setToolTip("Autoscale the heatmap colour range on each refresh")
        self.relevel_refresh.toggled.connect(self._colorbar_auto_refresh_changed)
        self.toolbarRef.addWidget(self.relevel_refresh)
     
    
    def initContextMenu(self):
        super().initContextMenu()

        autoColor = qtw.QAction("Autoscale Color", self)
        self.register_shortcut(autoColor, "Ctrl+Shift+C", "Autoscale color range")
        autoColor.triggered.connect(self.scaleColorbar)
        self.vbMenu.insertAction(self.autoscaleSep, autoColor)

        actions = self.vbMenu.actions()
        
        sep = self.vbMenu.insertSeparator(actions[3])
        
        ### Sweep control
        h_sweep = qtw.QAction("Plot Horizontal Cut", self)
        self.register_shortcut(h_sweep, "Ctrl+Shift+H", "Plot horizontal cut")
        h_sweep.triggered.connect(lambda _: self.openSweep("h"))
        self.vbMenu.insertAction(sep, h_sweep)
        
        v_sweep = qtw.QAction("Plot Vertical Cut", self)
        self.register_shortcut(v_sweep, "Ctrl+Shift+V", "Plot vertical cut")
        v_sweep.triggered.connect(lambda _: self.openSweep("v"))
        self.vbMenu.insertAction(sep, v_sweep)
        
        # Link finish update with check for rotation of sweep cursor
        self.end_wait.connect(self.rotate_sweeps)
        self.vbMenu.insertSeparator(h_sweep)

        self._init_colorbar_scale_controls()

        for key, text in (
                (QtCore.Qt.Key_Left, "Move selected cut left"),
                (QtCore.Qt.Key_Right, "Move selected cut right"),
                (QtCore.Qt.Key_Up, "Move selected cut up"),
                (QtCore.Qt.Key_Down, "Move selected cut down"),
                ):
            action = qtw.QAction(text, self)
            action.setShortcut(QtGui.QKeySequence(key))
            action.setShortcutContext(QtCore.Qt.WindowShortcut)
            action.triggered.connect(
                lambda _, key=key: self.move_sweep_with_arrow_key(key)
                )
            self.addAction(action)
        
        
    def initLabels(self):
        super().initLabels()
        self.z_index = None
        
        self.pos_labels["y"].setText(self.pos_labels["y"].text() + ";")
        
        posLabelx = qtw.QLabel("z= ")
        self.toolbarCo_ord.addWidget(posLabelx)
        self.pos_labels["z"] = posLabelx
        
###############################################################################
    
    def refreshPlot(self, finished : bool = True, worker=None):
        """
        Updates plot based on data produced by the thread worker. Data is 
        assigned in plotWidget.refreshPlot, then all plot items are produced
        here.

        Parameters
        ----------
        finished : bool
            In the event the worker had to abort, finished is False and refresh
            is not ran.
        """
        if not super().refreshPlot(finished, worker=worker):
            return
        
        autoLevels=self.relevel_refresh.isChecked()
        # Produce Heatmap
        self.image.setImage(
            self.dataGrid,
            autoLevels=autoLevels,
            autoRange=True
            )
        
        #set axis values
        xmin = min(self.axis_data["x"])
        ymin = min(self.axis_data["y"])
        xrange = max(self.axis_data["x"]) - xmin
        yrange = max(self.axis_data["y"]) - ymin
        
        if xrange == 0:
            xrange = xmin / 100 
        if yrange == 0:
            yrange = ymin / 100 
        
        # Link x/y axis values with Heatmap data
        self.rect = pg.QtCore.QRectF(
            xmin,
            ymin, 
            xrange,
            yrange
        )
        self.image.setRect(self.rect)
        
        # Produce color bar on first run
        if not hasattr(self, "bar"):
            self.bar = self.plot.addColorBar(
                self.image,
                colorMap=self._colorbar_colormap(),
                rounding=(
                    np.nanmax(self.dataGrid) - np.nanmin(self.dataGrid)
                    ) / 1e5,  # Add 10,000 colours
                colorMapMenu=False,
                )
            self._set_colorbar_tick_formatter()
            if self._colorbar_manual_levels is None:
                self.scaleColorbar()
            else:
                self._set_colorbar_levels(*self._colorbar_manual_levels)
        
        if autoLevels:
            self._colorbar_manual_levels = None
            self.scaleColorbar()
        elif self._colorbar_manual_levels is not None:
            self._set_colorbar_levels(*self._colorbar_manual_levels)
        
        self._update_hover_pixel_outline_from_index()
        if self.marquee is not None:
            self.set_marquee_rect(self.marquee)
        self._snap_sweep_lines_to_pixel_centres()
            
        # Allow new worker to be produced
        self.worker.running = False


    def show_hover_pixel_outline(self, i, j):
        """
        Move the hover outline to the heatmap pixel at the given data indices.

        Parameters
        ----------
        i : int
            Column index within the heatmap data grid.
        j : int
            Row index within the heatmap data grid.
        """
        self.z_index = [i, j]
        self._update_hover_pixel_outline_from_index()


    def hide_hover_pixel_outline(self):
        """
        Hide the heatmap hover outline and clear the saved hover index.

        """
        self.z_index = None
        if hasattr(self, "hover_pixel_outline"):
            self.hover_pixel_outline.hide()


    def _update_hover_pixel_outline_from_index(self):
        if (
                not hasattr(self, "hover_pixel_outline")
                or not hasattr(self, "rect")
                or not hasattr(self, "dataGrid")
                or getattr(self, "z_index", None) is None
                ):
            if hasattr(self, "hover_pixel_outline"):
                self.hover_pixel_outline.hide()
            return

        i, j = self.z_index
        rows, cols = self.dataGrid.shape
        if rows <= 0 or cols <= 0 or i < 0 or j < 0 or i >= cols or j >= rows:
            self.hover_pixel_outline.hide()
            return

        cell_width = self.rect.width() / cols
        cell_height = self.rect.height() / rows
        if cell_width <= 0 or cell_height <= 0:
            self.hover_pixel_outline.hide()
            return

        self.hover_pixel_outline.setRect(QtCore.QRectF(
            self.rect.x() + i * cell_width,
            self.rect.y() + j * cell_height,
            cell_width,
            cell_height,
        ))
        self.hover_pixel_outline.show()


    def _snap_marquee_rect(self, rect):
        """
        Snap marquee edges to heatmap pixel boundaries.

        """
        if not hasattr(self, "rect") or not hasattr(self, "dataGrid"):
            return rect

        rows, cols = self.dataGrid.shape
        if rows <= 0 or cols <= 0 or self.rect.width() <= 0 or self.rect.height() <= 0:
            return rect

        left, right = self._snap_marquee_axis_to_cells(
            rect.left(),
            rect.right(),
            self.rect.x(),
            self.rect.width(),
            cols,
            )
        bottom, top = self._snap_marquee_axis_to_cells(
            rect.top(),
            rect.bottom(),
            self.rect.y(),
            self.rect.height(),
            rows,
            )

        return QtCore.QRectF(left, bottom, right - left, top - bottom)


    def _snap_marquee_axis_to_cells(self, low, high, origin, span, count):
        cell_size = span / count
        min_value = origin
        max_value = origin + span
        low = min(max(low, min_value), max_value)
        high = min(max(high, min_value), max_value)

        low_index = int(np.floor((low - origin) / cell_size))
        high_index = int(np.ceil((high - origin) / cell_size))
        low_index = min(max(low_index, 0), count - 1)
        high_index = min(max(high_index, low_index + 1), count)

        return (
            origin + low_index * cell_size,
            origin + high_index * cell_size,
            )


    def _add_marquee_color_context_action(self, menu):
        action = self._add_marquee_context_action(
            menu,
            "Zoom color",
            self.zoom_marquee_color,
            )
        if self._marquee_color_levels() is None:
            action.setEnabled(False)
            action.setToolTip("No finite data range inside the marquee.")
        return action


    def zoom_marquee_color(self):
        levels = self._marquee_color_levels()
        if levels is None:
            return False

        return self.setColorbarManualRange(*levels)


    def _marquee_stats_text(self):
        selected = self._marquee_selected_data()
        if selected is None:
            return None

        values = selected[np.isfinite(selected)]
        if values.size == 0:
            return None

        rows, cols = selected.shape
        rect = self._snap_marquee_rect(self.marquee.normalized())
        return self._format_marquee_stats_text(f"{cols}x{rows} points", values, rect)


    def _marquee_color_levels(self):
        selected = self._marquee_selected_data()
        if selected is None:
            return None

        values = selected[np.isfinite(selected)]
        if values.size == 0:
            return None

        vmin = float(values.min())
        vmax = float(values.max())
        if not np.isfinite(vmin) or not np.isfinite(vmax) or vmin >= vmax:
            return None

        return vmin, vmax


    def _marquee_selected_data(self):
        if (
                self.__dict__.get("marquee") is None
                or "rect" not in self.__dict__
                or "dataGrid" not in self.__dict__
                ):
            return None

        slices = self._marquee_cell_slices()
        if slices is None:
            return None

        row_slice, col_slice = slices
        selected = np.asarray(self.dataGrid[row_slice, col_slice], dtype=float)
        if selected.size == 0:
            return None

        return selected


    def _marquee_cell_slices(self):
        rows, cols = self.dataGrid.shape
        if rows <= 0 or cols <= 0 or self.rect.width() <= 0 or self.rect.height() <= 0:
            return None

        rect = self._snap_marquee_rect(self.marquee.normalized())
        if rect is None:
            return None

        col_slice = self._marquee_axis_slice(
            rect.left(),
            rect.right(),
            self.rect.x(),
            self.rect.width(),
            cols,
            )
        row_slice = self._marquee_axis_slice(
            rect.top(),
            rect.bottom(),
            self.rect.y(),
            self.rect.height(),
            rows,
            )
        if row_slice is None or col_slice is None:
            return None

        return row_slice, col_slice


    def _marquee_axis_slice(self, low, high, origin, span, count):
        if count <= 0 or span <= 0:
            return None

        cell_size = span / count
        min_value = origin
        max_value = origin + span
        low = min(max(low, min_value), max_value)
        high = min(max(high, min_value), max_value)

        start = int(np.floor((low - origin) / cell_size))
        stop = int(np.ceil((high - origin) / cell_size))
        start = min(max(start, 0), count - 1)
        stop = min(max(stop, start + 1), count)

        return slice(start, stop)


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
            if event.button() == QtCore.Qt.LeftButton:
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
            if event.button() == QtCore.Qt.RightButton:
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
                    event.button() == QtCore.Qt.LeftButton
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
                    event.button() == QtCore.Qt.LeftButton
                    and (
                        active == source
                        or (
                            modifiers & QtCore.Qt.AltModifier
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
        table.setSelectionBehavior(qtw.QAbstractItemView.SelectRows)
        table.setSelectionMode(qtw.QAbstractItemView.SingleSelection)
        table.setEditTriggers(qtw.QAbstractItemView.NoEditTriggers)
        table.setSortingEnabled(True)
        table.setMinimumSize(560, 360)
        table.setIconSize(QtCore.QSize(220, 14))

        header = table.horizontalHeader()
        header.setSectionResizeMode(0, qtw.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, qtw.QHeaderView.Stretch)
        header.setSectionResizeMode(2, qtw.QHeaderView.ResizeToContents)

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
            name_item.setData(QtCore.Qt.UserRole, name)
            name_item.setData(_COLORBAR_TABLE_SORT_ROLE, label)
            type_item = _ColorbarColormapTableItem(type_label)
            type_item.setData(QtCore.Qt.UserRole, name)
            type_item.setData(_COLORBAR_TABLE_SORT_ROLE, type_label)
            preview_item = _ColorbarColormapTableItem()
            preview_item.setData(QtCore.Qt.UserRole, name)
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
            if item is not None and item.data(QtCore.Qt.UserRole) == name:
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

        name = item.data(QtCore.Qt.UserRole)
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

            buttons = qtw.QDialogButtonBox(qtw.QDialogButtonBox.Close)
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

    def openSweep(self, side):
        """
        Emits a signal to the Main window to open the sweep via 
        MainWindow.openWin()

        Parameters
        ----------
        side : str
            "h": horizontal, or "v": vertical. Along which axes the sweep will
            be performed.

        Raises
        ------
        KeyError
            Invalid side parameter.

        """
        # Quit out if not on heatmap
        if self.z_index is None:
            return
        
        # Fetch axes names
        axes = self.axis_options
        
        # Get fixed and sweep parameter
        if side == "v":
            fixed_var = axes["x"]
            sweep_var = axes["y"]
            fixed_index = self.z_index[0]
        elif side == "h":
            fixed_var = axes["y"]
            sweep_var = axes["x"]
            fixed_index = self.z_index[1]
        else:
            raise KeyError(f"Invalid sweep side, {side=}, must be 'v' or 'h'.")
            
        # Emit to Main window to open new window
        self.open_subplot.emit(
                sweeper,
                self._guid,
                (
                self.sweep_id,
                sweep_var,
                fixed_var,
                fixed_index,
                self.param
                )
            )
        # Update interal id for multiple sweeps
        self.sweep_id += 1
        

    @QtCore.pyqtSlot(int, str, str, int, object)
    def update_sweep_line(self, sweep_id, sweep_param, fixed_param, fixed_index, line_col):
        """
        Event handler for update to suplot sweep
        Updates the sweep cursor on the main plot in response to changes in the
        subplot

        Parameters
        ----------
        sweep_id : int
            The track of subplots to know which subplot cursor to edit.
        sweep_param : str
            The parameter over which the sweep subplot looks. Used to confirm
            that a cursor can be plotted
        fixed_param : str
            The static parameter and the parameter to place the line on.
        fixed_index : int
            index of on heatmap to place the line.
        line_col : QPen
            The plen color of the line.


        """
        # Check if display is possible on current axes
        if sweep_param not in self.axis_options.values() or fixed_param not in self.axis_options.values():
            return
        
        # get axis of fixed_param
        index = list(self.axis_options.values()).index(fixed_param)
        axis = list(self.axis_options.keys())[index]
    
        at_value = self.sweep_pixel_centre(axis, fixed_index)
    
        if self.sweep_lines.get(sweep_id, None) is not None:
            line = self.sweep_lines[sweep_id]
            
            # Update line data
            line.angle = (90 if axis == "x" else 0)
            line.pen = line_col
            line.hoverPen = line_col
            line.currentPen = line_col
            
            # refresh
            line.resetTransform()
            line.setRotation(line.angle)
            line.setPos(at_value)
            
    
        # Set up new line
        else:
            # Produce line
            if axis == "x":
                line = self.plot.addLine(
                    x=at_value, 
                    pen=line_col, 
                    movable=True
                    )
            else:
                line = self.plot.addLine(
                    y=at_value, 
                    pen=line_col, 
                    movable=True
                    )
                
                
            line.setZValue(1) # Move to top
            line.sigDragged.connect(self.moving_sweep)
            line.sigClicked.connect(self.activate_sweep_line)
            self.sweep_lines[sweep_id] = line # Track for update/delete
            line.sweep_id = sweep_id # give copy of id if needed
        
        self.set_sweep_line_index(line, fixed_index, emit=False)
    
    
    @QtCore.pyqtSlot(int)
    def remove_sweep(self, sweep_id):
        """
        Event handler for subplot closing.
        Removes line sweep display from plot

        Parameters
        ----------
        sweep_id : int
            Number Id of Sweep.

        """
        #check exists, then remove
        if self.sweep_lines.get(sweep_id, None) is None:
            return
        self.plot.removeItem(self.sweep_lines[sweep_id])
        self.sweep_lines.pop(sweep_id)
        
        
    @QtCore.pyqtSlot()
    def change_axis(self, key : str):
        
        # Rotate lines in case of duplciates
        options = self.axis_options
        if options["x"] == options["y"]:
            self.rotate = True
        else: # Otherwise delete them
            self.rotate = False
            
        super().change_axis(key)

    
    @QtCore.pyqtSlot()  
    def rotate_sweeps(self):
        """
        Event handler for changing assigned axes (is connected to self.end_wait
                                                  in self.refreshPlot)
        
        Rotates sweep cursors if the axis is flipped. Otherwise removes them

        Returns
        -------
        None.

        """
        if self.rotate is None: # Not from changing axis parameters
            return
        
        # remote lines as parameters have changed
        if not self.rotate:
            for key in self.sweep_lines.keys():
                self.remove_sweep(key)
            self.rotate = None
            return
            
        # Rotate lines as parameters switched
        for key, line in self.sweep_lines.items():
            line = self.sweep_lines[key]
            # Rotate
            pos = line.value()
            line.angle = 90 if line.angle == 0 else 0
            
            line.resetTransform()
            line.setRotation(line.angle)
            line.setPos(pos) # force line placement into correct spot
            
        self.rotate = None
    
    
    def sweep_axis_count(self, axis):
        if axis == "x":
            return self.dataGrid.shape[1]
        return self.dataGrid.shape[0]


    def sweep_pixel_centre(self, axis, index):
        """
        Return the plot coordinate at the centre of a heatmap pixel.

        """
        count = self.sweep_axis_count(axis)
        index = min(max(int(index), 0), count - 1)

        if axis == "x":
            return self.rect.x() + (index + 0.5) * self.rect.width() / count
        return self.rect.y() + (index + 0.5) * self.rect.height() / count


    def sweep_index_at_value(self, axis, value):
        """
        Return the heatmap pixel index containing a plot coordinate.

        """
        count = self.sweep_axis_count(axis)
        if axis == "x":
            start = self.rect.x()
            width = self.rect.width()
        else:
            start = self.rect.y()
            width = self.rect.height()

        if count <= 0 or width <= 0:
            return None

        index = int((value - start) / width * count)
        return min(max(index, 0), count - 1)


    def line_sweep_axis(self, line):
        return "x" if line.angle == 90 else "y"


    def activate_sweep_line(self, line, event=None):
        self.active_sweep_line_id = line.sweep_id
        if event is not None:
            event.accept()


    def set_sweep_line_index(self, line, index, emit=True):
        axis = self.line_sweep_axis(line)
        count = self.sweep_axis_count(axis)
        index = min(max(int(index), 0), count - 1)

        line.setBounds((
            self.sweep_pixel_centre(axis, 0),
            self.sweep_pixel_centre(axis, count - 1)
            ))
        line.setPos(self.sweep_pixel_centre(axis, index))
        line.sweep_index = index
        self.active_sweep_line_id = line.sweep_id

        if emit:
            self.sweep_moved.emit(line.sweep_id, index)


    def _snap_sweep_lines_to_pixel_centres(self):
        for line in self.sweep_lines.values():
            axis = self.line_sweep_axis(line)
            index = getattr(line, "sweep_index", None)
            if index is None:
                index = self.sweep_index_at_value(axis, line.value())
            if index is not None:
                self.set_sweep_line_index(line, index, emit=False)


    def move_sweep_with_arrow_key(self, key):
        moves = {
            QtCore.Qt.Key_Left: ("x", -1),
            QtCore.Qt.Key_Right: ("x", 1),
            QtCore.Qt.Key_Down: ("y", -1),
            QtCore.Qt.Key_Up: ("y", 1),
            }
        if key not in moves:
            return

        axis, step = moves[key]
        line = self.sweep_line_for_keyboard_move(axis)
        if line is None:
            return

        index = getattr(line, "sweep_index", None)
        if index is None:
            index = self.sweep_index_at_value(axis, line.value())
        if index is None:
            return

        self.set_sweep_line_index(line, index + step)


    def sweep_line_for_keyboard_move(self, axis):
        matching_lines = [
            line for line in self.sweep_lines.values()
            if self.line_sweep_axis(line) == axis
            ]
        if not matching_lines:
            return None

        active_line = self.sweep_lines.get(getattr(self, "active_sweep_line_id", None))
        if active_line in matching_lines:
            return active_line

        return max(matching_lines, key=lambda line: line.sweep_id)


    @QtCore.pyqtSlot(object)
    def moving_sweep(self, line):
        """
        Event handler for dragging sweep cursor.
        
        Uses line possition to find index of fixed parameter and sends to 
        signal to subplot window to move sweep scan to new location.

        Parameters
        ----------
        line : pyqtgraph.graphicsItems.InfiniteLine
            The line being dragged.

        """        
        pos = line.value()
        axis = self.line_sweep_axis(line)
        index = self.sweep_index_at_value(axis, pos)

        if index is not None:
            self.set_sweep_line_index(line, index)
