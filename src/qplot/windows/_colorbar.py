import numpy as np
import pyqtgraph as pg
from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

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


_COLORBAR_TABLE_SORT_ROLE = QtCore.Qt.ItemDataRole.UserRole + 1


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
    pixmap.fill(QtCore.Qt.GlobalColor.transparent)

    painter = QtGui.QPainter(pixmap)
    try:
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setPen(QtGui.QPen(QtGui.QColor(70, 70, 70, 230), 1.2))
        painter.setBrush(QtGui.QColor(235, 235, 235, 225))
        painter.drawEllipse(QtCore.QRectF(1, 1, size - 2, size - 2))

        font = QtGui.QFont()
        font.setBold(True)
        font.setPointSize(11)
        painter.setFont(font)
        painter.setPen(QtGui.QColor(40, 40, 40))
        painter.drawText(pixmap.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, letter)
    finally:
        painter.end()

    return pixmap
