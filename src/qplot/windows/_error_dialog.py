"""Resizable, copyable error details for main and plot windows."""

from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from ._small_dialogs import (
    close_default,
    dialog_layout,
    equal_button_widths,
    resize_dialog,
)


class ErrorDialog(qtw.QDialog):
    def __init__(
        self,
        title: str,
        message: str,
        details: str | None = None,
        parent: qtw.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("errorDialog")
        self.setWindowTitle("Error details")
        self.setSizeGripEnabled(True)
        self.setWindowFlag(QtCore.Qt.WindowType.WindowMaximizeButtonHint, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        self._copy_text = "\n\n".join(
            str(text) for text in (title, message, details) if text
        )

        layout = dialog_layout(self)
        title_label = qtw.QLabel(title, self)
        title_label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        title_label.setWordWrap(True)
        font = title_label.font()
        font.setBold(True)
        title_label.setFont(font)
        layout.addWidget(title_label)
        heading = qtw.QHBoxLayout()
        heading.setSpacing(12)
        icon = qtw.QLabel(self)
        style = self.style()
        assert style is not None
        icon.setPixmap(
            style.standardIcon(
                qtw.QStyle.StandardPixmap.SP_MessageBoxWarning,
            )
            .pixmap(48, 48)
        )
        icon.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        heading.addWidget(icon)

        label = qtw.QLabel(message, self)
        label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
            | QtCore.Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        heading.addWidget(label, 1)
        layout.addLayout(heading)

        if details:
            text = qtw.QPlainTextEdit(self)
            text.setObjectName("errorDetails")
            text.setReadOnly(True)
            text.setTabChangesFocus(True)
            text.setPlainText(details)
            text.setAccessibleName("Error details")
            layout.addWidget(text, 1)

        buttons = qtw.QDialogButtonBox(qtw.QDialogButtonBox.StandardButton.Close, self)
        copy = buttons.addButton("Copy", qtw.QDialogButtonBox.ButtonRole.ActionRole)
        assert copy is not None
        copy.setObjectName("copyErrorButton")
        copy.clicked.connect(self.copy_error)
        # Keep the former OK button's accepted result for explicit dismissal.
        buttons.rejected.connect(self.accept)
        layout.addWidget(buttons)
        close = buttons.button(qtw.QDialogButtonBox.StandardButton.Close)
        assert close is not None
        equal_button_widths(copy, close)
        close_default(buttons)
        resize_dialog(self, 720 if details else 520, 420 if details else 220)

    def copy_error(self) -> None:
        clipboard = qtw.QApplication.clipboard()
        assert clipboard is not None
        clipboard.setText(self._copy_text)
