"""Shared fixtures and assertions for preview placeholders."""

from PyQt6 import QtCore, QtGui

__all__ = [
    '_white_preview',
]


def _white_preview(parameter, *, axes=("x",)):
    image = QtGui.QImage(22, 22, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtCore.Qt.GlobalColor.white)
    return {
        "parameter": parameter,
        "axes": list(axes),
        "title": f"{parameter} preview",
        "image": image,
    }
