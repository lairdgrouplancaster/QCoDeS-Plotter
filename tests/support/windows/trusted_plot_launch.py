"""Shared fixtures and assertions for trusted plot launch."""

import time

from PyQt6 import QtWidgets

__all__ = [
    'wait_for',
]


def wait_for(predicate, timeout=20):
    app = QtWidgets.QApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("Timed out waiting for trusted plot display")
