"""Rebuild the shipped desktop icons from the editable SVG masters.

Run with the project development environment. Setup copies these prebuilt
assets, so ordinary installations do not need Pillow or an SVG renderer.
"""

import io
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PyQt6 import QtCore, QtGui, QtSvg

from qplot._auxiliary_paths import ensure_safe_auxiliary_path

ASSETS = Path(__file__).resolve().parents[1] / "src/qplot/assets"
ICON_STEMS = ("qplot", "qplot-database", "sqlite-wal", "sqlite-shm")
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def render(stem: str, size: int, *, small: bool = False) -> Image.Image:
    source = ASSETS / f"{stem}{'-small' if small else ''}.svg"
    renderer = QtSvg.QSvgRenderer(str(source))
    if not renderer.isValid():
        raise ValueError(f"Invalid icon source: {source}")
    image = QtGui.QImage(size, size, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(image)
    renderer.render(painter)
    painter.end()
    buffer = QtCore.QBuffer()
    buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise OSError("Could not render the desktop icon")
    with Image.open(io.BytesIO(bytes(buffer.data()))) as decoded:
        return decoded.convert("RGBA")


def main() -> None:
    app = QtGui.QGuiApplication([])
    for stem in ICON_STEMS:
        for extension in ("png", "ico", "icns"):
            ensure_safe_auxiliary_path(ASSETS / f"{stem}.{extension}")
    for stem in ICON_STEMS:
        master = render(stem, 1024)
        master.save(ASSETS / f"{stem}.png")
        master.save(
            ASSETS / f"{stem}.ico",
            sizes=[(size, size) for size in ICO_SIZES],
            append_images=[render(stem, size, small=size == 16) for size in ICO_SIZES],
        )
        master.save(
            ASSETS / f"{stem}.icns",
            # ICNS's 32-pixel entry is the 16-point Retina representation.
            append_images=[render(stem, size, small=size == 32) for size in (32, 64, 128, 256, 512)],
        )
    app.quit()
    print(f"Generated PNG, ICO and ICNS icons for {', '.join(ICON_STEMS)} in {ASSETS}")


if __name__ == "__main__":
    main()
