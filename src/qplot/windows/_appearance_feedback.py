"""Display mixed selection values without publishing edits to the plot."""

from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw


def mixed_values(bindings):
    """Mark controls whose selected values differ; return their visible names."""
    names = []
    for name, control, values in bindings:
        mixed = len(set(values)) > 1
        with QtCore.QSignalBlocker(control):
            if isinstance(control, qtw.QCheckBox):
                control.setTristate(mixed)
                if mixed:
                    control.setCheckState(QtCore.Qt.CheckState.PartiallyChecked)
            elif isinstance(control, qtw.QComboBox):
                control.setPlaceholderText("Mixed")
                if mixed:
                    control.setCurrentIndex(-1)
            elif isinstance(control, qtw.QAbstractSpinBox):
                control.lineEdit().setPlaceholderText("Mixed")
                if mixed:
                    control.lineEdit().setText("Mixed")
        if mixed:
            names.append(name)
    return names
