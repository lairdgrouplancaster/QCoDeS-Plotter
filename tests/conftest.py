"""Shared pytest setup for qPlot's Qt-based tests."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6 import QtWidgets as qtw


def ensure_qapplication():
    """Return the process-wide QApplication, creating it for headless tests."""
    app = qtw.QApplication.instance()
    if app is None:
        app = qtw.QApplication([])
    return app


@pytest.fixture(scope="session", autouse=True)
def qapplication():
    return ensure_qapplication()
