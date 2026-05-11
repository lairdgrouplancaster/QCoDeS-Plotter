from PyQt5 import QtWidgets as qtw
from PyQt5 import QtCore, QtGui

import pyqtgraph as pg

import numpy as np

from ._plotWin import plotWidget
from ._subplots.subplot2d import sweeper


_ENGINEERING_PREFIXES = {
    -24: "y",
    -21: "z",
    -18: "a",
    -15: "f",
    -12: "p",
    -9: "n",
    -6: "u",
    -3: "m",
    0: "",
    3: "k",
    6: "M",
    9: "G",
    12: "T",
    15: "P",
    18: "E",
    21: "Z",
    24: "Y",
}


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


def _trim_decimal_places(value, decimal_places=3):
    """
    Format a float with up to decimal_places, trimming trailing zeros.

    """
    text = f"{value:.{decimal_places}f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _format_engineering_tick(value, decimal_places=3):
    """
    Format an axis tick using compact engineering notation.

    """
    if not np.isfinite(value):
        return ""

    if value == 0:
        return "0"

    exponent = int(np.floor(np.log10(abs(value)) / 3) * 3)
    exponent = max(min(exponent, 24), -24)
    scaled = value / 10**exponent
    rounded_scaled = round(scaled, decimal_places)
    if abs(rounded_scaled) >= 1000 and exponent < 24:
        exponent += 3
        scaled = value / 10**exponent

    prefix = _ENGINEERING_PREFIXES[exponent]

    return f"{_trim_decimal_places(scaled, decimal_places)}{prefix}"


def _engineering_tick_strings(values, scale=1.0, spacing=None):
    """
    Return engineering-notation labels for pyqtgraph AxisItem ticks.

    """
    return [_format_engineering_tick(value * scale) for value in values]


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

        self._add_colorbar_scale_context_action()
        
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
        
        
    def _add_colorbar_scale_context_action(self):
        """
        Add a flat context-menu action for the color scale dialog.

        """
        self.colorbarScaleAction = qtw.QAction("Color Scale...", self)
        self.colorbarScaleAction.setStatusTip("Open the color scale dialog")
        self.colorbarScaleAction.triggered.connect(self.open_colorbar_scale_dialog)
        self.vbMenu.insertAction(self.autoscaleSep, self.colorbarScaleAction)


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
                label=f"{self.param.label} ({self.param.unit})",
                rounding=(np.nanmax(self.dataGrid) - np.nanmin(self.dataGrid))/1e5 #Add 10,000 colours
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

        self._sync_colorbar_level_fields(vmin, vmax)


    def _set_colorbar_tick_formatter(self):
        """
        Use engineering notation for colorbar tick labels.

        """
        bar = self.__dict__.get("bar")
        axis = getattr(bar, "axis", None)
        if axis is None:
            return

        axis.tickStrings = _engineering_tick_strings
        axis.setWidth(70)
        axis.setStyle(tickTextWidth=60)
        axis.picture = None
        axis.update()
        self._install_colorbar_scale_axis_handlers(axis)


    def _install_colorbar_scale_axis_handlers(self, axis):
        """
        Open the color-scale dialog from direct colorbar axis interactions.

        """
        if getattr(axis, "_qplot_colorbar_scale_handlers_installed", False):
            return

        previous_double_click_handler = getattr(axis, "mouseDoubleClickEvent", None)

        def mouse_double_click(event, previous_handler=previous_double_click_handler):
            if event.button() == QtCore.Qt.LeftButton:
                self.open_colorbar_scale_dialog()
                event.accept()
                return

            if previous_handler is not None:
                previous_handler(event)

        def context_menu(event):
            menu = qtw.QMenu(self)
            action = menu.addAction("Color Scale...")
            action.triggered.connect(self.open_colorbar_scale_dialog)
            position = self._colorbar_scale_context_menu_position(event)
            menu.exec_(position)
            event.accept()

        axis.mouseDoubleClickEvent = mouse_double_click
        axis.contextMenuEvent = context_menu
        axis._qplot_colorbar_scale_handlers_installed = True


    def _colorbar_scale_context_menu_position(self, event):
        """
        Return a global position for colorbar axis context menus.

        """
        if hasattr(event, "screenPos"):
            return event.screenPos()

        if hasattr(event, "globalPos"):
            return event.globalPos()

        return QtGui.QCursor.pos()


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
        table.setColumnCount(2)
        table.setHorizontalHeaderLabels(("Color map", "Preview"))
        table.verticalHeader().hide()
        table.setShowGrid(False)
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(qtw.QAbstractItemView.SelectRows)
        table.setSelectionMode(qtw.QAbstractItemView.SingleSelection)
        table.setEditTriggers(qtw.QAbstractItemView.NoEditTriggers)
        table.setMinimumSize(440, 360)
        table.setIconSize(QtCore.QSize(220, 14))

        header = table.horizontalHeader()
        header.setSectionResizeMode(0, qtw.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, qtw.QHeaderView.Stretch)

        self._populate_colorbar_colormap_table()


    def _populate_colorbar_colormap_table(self):
        """
        Fill the color-map table using the current filter settings.

        """
        table = self.colorbar_colormap_table
        table.blockSignals(True)
        table.setRowCount(0)

        colormaps = self._available_colorbar_colormaps()
        table.setRowCount(len(colormaps))
        for row, name in enumerate(colormaps):
            name_item = qtw.QTableWidgetItem(
                _COLORBAR_COLORMAP_LABELS.get(name, name)
            )
            name_item.setData(QtCore.Qt.UserRole, name)
            preview_item = qtw.QTableWidgetItem()
            preview_item.setData(QtCore.Qt.UserRole, name)
            preview_item.setIcon(QtGui.QIcon(_colorbar_colormap_preview(name)))

            table.setItem(row, 0, name_item)
            table.setItem(row, 1, preview_item)
            table.setRowHeight(row, 20)

        table.resizeColumnToContents(0)
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
