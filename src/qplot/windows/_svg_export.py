"""SVG export using Qt's SVG 1.1 paint engine, including native clipping.

PyQtGraph 0.14's SVG coordinate rewriter cannot read Qt 6.11's closed
paths. Keep Qt's geometry, transforms, clip paths and gradient definitions
instead of reconstructing the scene with that rewriter.
"""

import math
import re
import xml.dom.minidom as xml
from typing import Any

from PyQt6 import QtCore, QtGui, QtSvg
from pyqtgraph.exporters import SVGExporter

_NUMBER = r"[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?"
_TOKEN = re.compile(rf"[A-Za-z]|{_NUMBER}")


def svg_bytes(exporter: SVGExporter) -> bytes:
    """Render the selected scene rectangle with all SVG dialog options."""
    width, height = (float(exporter.params[name]) for name in ("width", "height"))
    if not all(math.isfinite(v) and 0 < v <= 2_147_483_647 for v in (width, height)):
        raise ValueError("SVG dimensions must be finite, positive Qt device dimensions.")
    source = QtCore.QRectF(exporter.getSourceRect())
    if source.isEmpty():
        raise ValueError("The selected SVG source rectangle is empty.")
    target = QtCore.QRectF(0, 0, width, height)
    data = QtCore.QByteArray()
    buffer = QtCore.QBuffer(data)
    if not buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly):
        raise RuntimeError("Could not open the SVG rendering buffer.")
    # SVG Tiny (the generator default) does not support clipping. SVG 1.1
    # does, including ViewBoxes, secondary axes and image/colorbar items.
    generator = QtSvg.QSvgGenerator(QtSvg.QSvgGenerator.SvgVersion.Svg11)
    generator.setOutputDevice(buffer)
    generator.setSize(QtCore.QSize(math.ceil(width), math.ceil(height)))
    generator.setViewBox(target)
    generator.setResolution(96)  # one SVG user unit is one CSS pixel
    generator.setTitle("qPlot plot")
    generator.setDescription("Exported from qPlot")
    painter = QtGui.QPainter()
    try:
        if not painter.begin(generator):
            raise RuntimeError("Could not start the SVG painter.")
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing)
        painter.fillRect(target, exporter.params["background"])
        try:
            exporter.setExportMode(True, {
                "painter": painter,
                "antialias": True,
                "background": exporter.params["background"],
                "resolutionScale": width / exporter.getTargetRect().width(),
            })
            exporter.getScene().render(
                painter, target, source,
                QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
            )
        finally:
            exporter.setExportMode(False)
        if not painter.end():
            raise RuntimeError("Could not finish the SVG painter.")
    finally:
        if painter.isActive():
            painter.end()
        buffer.close()
    result = bytes(data.data())
    if exporter.params["scaling stroke"]:
        result = _scaling_strokes(result)
    return result


def write_svg_stage(filename: str, exporter: SVGExporter) -> bool:
    """Write only the private staging path supplied by qPlot's transaction."""
    data = svg_bytes(exporter)
    with open(filename, "wb") as output:
        output.write(data)
    return True


def _matrix(transform: QtGui.QTransform) -> str:
    return "matrix(" + ",".join(format(v, ".17g") for v in (
        transform.m11(), transform.m12(), transform.m21(), transform.m22(),
        transform.dx(), transform.dy(),
    )) + ")"


def _transform(value: str) -> QtGui.QTransform:
    if not value:
        return QtGui.QTransform()
    # Qt emits only matrix transforms. Reject unexpected output rather than
    # publishing geometry with a partially interpreted transform.
    match = re.fullmatch(r"matrix\(([^()]*)\)", value.strip())
    if match is None:
        raise ValueError("Unexpected Qt SVG transform.")
    values = [float(v) for v in re.split(r"[\s,]+", match[1].strip())]
    if len(values) != 6 or not all(map(math.isfinite, values)):
        raise ValueError("Invalid Qt SVG transform.")
    return QtGui.QTransform(*values)


def _map_pairs(values: list[str], transform: QtGui.QTransform) -> list[str]:
    if len(values) % 2:
        raise ValueError("Invalid Qt SVG coordinate pair.")
    result = []
    for index in range(0, len(values), 2):
        point = transform.map(QtCore.QPointF(float(values[index]), float(values[index + 1])))
        result.append(f"{point.x():.17g},{point.y():.17g}")
    return result


def _map_path(value: str, transform: QtGui.QTransform) -> str:
    """Map Qt's absolute M/L/C paths, preserving curves and closing Z commands.

    Qt serializes QPainterPath with this subset of SVG commands (quadratics
    become cubics). Tokenize SVG numbers, including exponents and adjacent
    signed numbers; never assume whitespace-delimited tokens are pairs.
    """
    tokens = _TOKEN.findall(value)
    if re.sub(r"[\s,]", "", _TOKEN.sub("", value)):
        raise ValueError("Invalid Qt SVG path.")
    result: list[str] = []
    index = 0
    while index < len(tokens):
        command = tokens[index]
        index += 1
        if command == "Z":
            result.append("Z")
            continue
        arity = {"M": 2, "L": 2, "C": 6}.get(command)
        if arity is None:
            raise ValueError(f"Unexpected Qt SVG path command: {command}")
        end = index
        while end < len(tokens) and not tokens[end].isalpha():
            end += 1
        values = tokens[index:end]
        if not values or len(values) % arity:
            raise ValueError("Invalid Qt SVG path coordinates.")
        result.append(command + " ".join(_map_pairs(values, transform)))
        index = end
    return " ".join(result)


def _shape_path(node: Any) -> str:
    """Convert Qt's rect/ellipse primitives to the same absolute path subset."""
    path = QtGui.QPainterPath()
    if node.tagName == "rect":
        path.addRoundedRect(QtCore.QRectF(*(
            float(node.getAttribute(key) or 0) for key in ("x", "y", "width", "height")
        )), float(node.getAttribute("rx") or 0), float(node.getAttribute("ry") or 0))
    else:
        cx, cy = (float(node.getAttribute(key) or 0) for key in ("cx", "cy"))
        rx = float(node.getAttribute("rx") or node.getAttribute("r"))
        ry = float(node.getAttribute("ry") or node.getAttribute("r"))
        path.addEllipse(QtCore.QRectF(cx - rx, cy - ry, 2 * rx, 2 * ry))
    parts = []
    for i in range(path.elementCount()):
        element = path.elementAt(i)
        command = {
            QtGui.QPainterPath.ElementType.MoveToElement: "M",
            QtGui.QPainterPath.ElementType.LineToElement: "L",
            QtGui.QPainterPath.ElementType.CurveToElement: "C",
            QtGui.QPainterPath.ElementType.CurveToDataElement: "",
        }[element.type]
        parts.append(f"{command}{element.x:.17g},{element.y:.17g}")
    return " ".join(parts) + " Z"


def _scaling_strokes(data: bytes) -> bytes:
    """Make cosmetic strokes scale on SVG resize, without changing geometry.

    Simply removing vector-effect would multiply widths by the data-to-view
    transform (often anisotropic). Move those primitives to document coordinates
    first, cancelling their inherited transform. Leave noncosmetic pens, text,
    images, gradients and clipping definitions as Qt generated them.
    """
    document = xml.parseString(data)

    def visit(node: Any, parent_transform: QtGui.QTransform) -> None:
        transform = _transform(node.getAttribute("transform")) * parent_transform
        if node.getAttribute("vector-effect") == "non-scaling-stroke":
            inverse, invertible = parent_transform.inverted()
            if not invertible:
                raise ValueError("Cannot scale strokes under a singular SVG transform.")
            if node.tagName in ("polyline", "polygon"):
                node.setAttribute("points", " ".join(_map_pairs(
                    re.findall(_NUMBER, node.getAttribute("points")), transform,
                )))
            elif node.tagName in ("path", "rect", "ellipse", "circle"):
                value = node.getAttribute("d") if node.tagName == "path" else _shape_path(node)
                node.tagName = "path"
                node.setAttribute("d", _map_path(value, transform))
                for name in ("x", "y", "width", "height", "rx", "ry", "cx", "cy", "r"):
                    if node.hasAttribute(name):
                        node.removeAttribute(name)
            else:
                raise ValueError(f"Unexpected Qt SVG cosmetic primitive: {node.tagName}")
            node.removeAttribute("vector-effect")
            node.setAttribute("transform", _matrix(inverse))
        for child in node.childNodes:
            if isinstance(child, xml.Element) and child.tagName != "defs":
                visit(child, transform)

    try:
        visit(document.documentElement, QtGui.QTransform())
        return document.toxml(encoding="utf-8")
    finally:
        document.unlink()
