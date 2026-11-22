"""Resizable, copyable error details for main and plot windows."""

from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from ._small_dialogs import (
    close_default,
    copy_to_clipboard,
    dialog_layout,
    equal_button_widths,
    resize_dialog,
)


class ErrorDialog(qtw.QDialog):
    RecoveryRequested = 2

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
            if "\n" in details or len(details) > 160:
                toggle = qtw.QToolButton(self)
                toggle.setText("Technical details")
                toggle.setCheckable(True)
                toggle.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
                toggle.setArrowType(QtCore.Qt.ArrowType.RightArrow)
                layout.addWidget(toggle, 0, QtCore.Qt.AlignmentFlag.AlignLeft)
                text.hide()

                def show_details(expanded):
                    toggle.setArrowType(QtCore.Qt.ArrowType.DownArrow if expanded else QtCore.Qt.ArrowType.RightArrow)
                    text.setVisible(expanded)
                    self.resize(self.width(), self.sizeHint().height())

                toggle.toggled.connect(show_details)
            else:
                text.setFixedHeight(text.fontMetrics().lineSpacing() * 3 + 8)
            layout.addWidget(text, 1)

        buttons = qtw.QDialogButtonBox(qtw.QDialogButtonBox.StandardButton.Close, self)
        self.buttons = buttons
        copy = buttons.addButton("Copy details", qtw.QDialogButtonBox.ButtonRole.ActionRole)
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
        resize_dialog(self, 520, self.minimumSizeHint().height())

    def add_recovery_action(self, label):
        button = self.buttons.addButton(label, qtw.QDialogButtonBox.ButtonRole.ActionRole)
        assert button is not None
        button.clicked.connect(lambda: self.done(self.RecoveryRequested))

    def copy_error(self) -> None:
        copy_to_clipboard(self._copy_text)
