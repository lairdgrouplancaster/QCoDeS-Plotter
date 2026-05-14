import sys

from PyQt5 import QtWidgets as qtw
from PyQt5.QtGui import QKeySequence


def key_sequences(sequences):
    """
    Converts shortcut text to QKeySequence instances.

    """
    return [
        sequence if isinstance(sequence, QKeySequence) else QKeySequence(sequence)
        for sequence in sequences
        ]


def platform_key_sequences(mac=None, windows=None, other=None):
    """
    Returns shortcuts for the current platform.

    """
    if sys.platform == "darwin":
        sequences = mac or []
    elif sys.platform.startswith("win"):
        sequences = windows or []
    else:
        sequences = other if other is not None else windows or []

    return key_sequences(sequences)


def standard_key_sequences(standard_key, fallback):
    """
    Returns Qt's platform-specific shortcuts, with explicit fallbacks.

    """
    shortcuts = []
    if qtw.QApplication.instance() is not None:
        shortcuts = QKeySequence.keyBindings(standard_key)

    for sequence in reversed(key_sequences(fallback)):
        if sequence in shortcuts:
            shortcuts.remove(sequence)
        shortcuts.insert(0, sequence)

    return shortcuts
