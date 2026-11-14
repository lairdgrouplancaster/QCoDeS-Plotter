"""Shared layout and keyboard conventions for informational dialogs."""

from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw


def dialog_layout(dialog: qtw.QDialog) -> qtw.QVBoxLayout:
    dialog.setSizeGripEnabled(True)
    layout = qtw.QVBoxLayout(dialog)
    layout.setContentsMargins(12, 12, 12, 12)
    layout.setSpacing(10)
    return layout


def equal_button_widths(*buttons: qtw.QPushButton) -> None:
    width = max(button.sizeHint().width() for button in buttons)
    for button in buttons:
        button.setMinimumWidth(width)


def close_default(buttons: qtw.QDialogButtonBox) -> None:
    """Return initially dismisses; Tab retains native button activation."""
    for button in buttons.buttons():
        if isinstance(button, qtw.QPushButton):
            button.setDefault(False)
    close = buttons.button(qtw.QDialogButtonBox.StandardButton.Close)
    assert close is not None
    close.setDefault(True)
    close.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)


def resize_dialog(dialog: qtw.QDialog, width: int, height: int) -> None:
    """Scale the initial size with text, leaving resizing to the native layout."""
    scale = max(1.0, dialog.fontMetrics().height() / 16)
    size = QtCore.QSize(round(width * scale), round(height * scale))
    screen = dialog.screen()
    if screen is not None:
        available = screen.availableGeometry().size() - QtCore.QSize(40, 80)
        size = size.boundedTo(available)
    dialog.resize(size.expandedTo(dialog.minimumSizeHint()))
