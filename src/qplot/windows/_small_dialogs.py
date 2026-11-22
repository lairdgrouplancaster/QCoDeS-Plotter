"""Shared layout and keyboard conventions for informational dialogs."""

from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw


class _FullWidthTabBar(qtw.QTabBar):
    """Fill the page width while retaining the framed tab widget and native overflow."""

    def resizeEvent(self, event: QtGui.QResizeEvent | None) -> None:
        super().resizeEvent(event)
        parent = self.parentWidget()
        if parent is not None and self.width() != parent.width():
            self.resize(parent.width(), self.height())


class DialogTabs(qtw.QTabWidget):
    """Top-level dialog pages with evenly sized tabs and keyboard navigation."""

    def __init__(self, parent: qtw.QWidget) -> None:
        super().__init__(parent)
        bar = _FullWidthTabBar(self)
        self.setTabBar(bar)
        bar.setExpanding(True)
        bar.setUsesScrollButtons(True)
        self.setElideMode(QtCore.Qt.TextElideMode.ElideNone)


def confirm_action(parent, title: str, message: str, *, affirmative: str) -> bool:
    """Ask for one named action, with Cancel as the keyboard default."""
    box = qtw.QMessageBox(qtw.QMessageBox.Icon.Question, title, "",
                          qtw.QMessageBox.StandardButton.Cancel, parent)
    box.setTextFormat(QtCore.Qt.TextFormat.PlainText)
    box.setText(message)
    action = box.addButton(affirmative, qtw.QMessageBox.ButtonRole.AcceptRole)
    box.setDefaultButton(qtw.QMessageBox.StandardButton.Cancel)
    box.setEscapeButton(qtw.QMessageBox.StandardButton.Cancel)
    box.exec()
    return box.clickedButton() is action


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


def copy_to_clipboard(text: str) -> None:
    clipboard = qtw.QApplication.clipboard()
    if clipboard is not None:
        clipboard.setText(text)


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
