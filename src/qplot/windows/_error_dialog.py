"""Resizable, copyable error details for main and plot windows."""

from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw


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
        self.setWindowTitle(title)
        self.setSizeGripEnabled(True)
        self.setWindowFlag(QtCore.Qt.WindowType.WindowMaximizeButtonHint, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        self._copy_text = "\n\n".join(
            str(text) for text in (title, message, details) if text
        )

        layout = qtw.QVBoxLayout(self)
        heading = qtw.QHBoxLayout()
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
        )
        heading.addWidget(label, 1)
        layout.addLayout(heading)

        if details:
            text = qtw.QPlainTextEdit(self)
            text.setObjectName("errorDetails")
            text.setReadOnly(True)
            text.setPlainText(details)
            text.setAccessibleName("Error details")
            layout.addWidget(text, 1)

        buttons = qtw.QDialogButtonBox(qtw.QDialogButtonBox.StandardButton.Ok, self)
        copy = buttons.addButton("Copy", qtw.QDialogButtonBox.ButtonRole.ActionRole)
        assert copy is not None
        copy.setObjectName("copyErrorButton")
        copy.clicked.connect(self.copy_error)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
        self.setMinimumSize(400, 240 if details else 140)
        self.resize(720 if details else 520, 420 if details else 180)

    def copy_error(self) -> None:
        clipboard = qtw.QApplication.clipboard()
        assert clipboard is not None
        clipboard.setText(self._copy_text)
