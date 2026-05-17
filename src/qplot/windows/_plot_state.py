from PyQt5 import QtCore
from PyQt5 import QtWidgets as qtw


class PlotStateOverlay(QtCore.QObject):
    """
    Lightweight status overlay shown inside a plot widget.

    The status bar is easy to miss while a blank plot is loading. This overlay
    keeps transient plot states visible in the plot area without intercepting
    mouse interaction once it is hidden.
    """

    _styles = {
        "info": (
            "background-color: rgba(250, 252, 255, 235);"
            "border: 1px solid rgba(119, 135, 153, 190);"
            "color: #1f2933;"
            ),
        "loading": (
            "background-color: rgba(246, 251, 255, 238);"
            "border: 1px solid rgba(70, 130, 180, 190);"
            "color: #102a43;"
            ),
        "empty": (
            "background-color: rgba(255, 251, 235, 238);"
            "border: 1px solid rgba(180, 132, 35, 190);"
            "color: #3b2f12;"
            ),
        "error": (
            "background-color: rgba(255, 245, 245, 240);"
            "border: 1px solid rgba(190, 72, 72, 200);"
            "color: #3b0d0c;"
            ),
    }

    def __init__(self, target):
        super().__init__(target)
        self.owner = target
        viewport = target.viewport() if hasattr(target, "viewport") else None
        self.target = viewport or target

        self.frame = qtw.QFrame(self.target)
        self.frame.setObjectName("plotStateOverlay")
        self.frame.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self.frame.setAutoFillBackground(False)

        layout = qtw.QVBoxLayout(self.frame)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(4)

        self.title_label = qtw.QLabel(self.frame)
        self.title_label.setObjectName("plotStateOverlayTitle")
        title_font = self.title_label.font()
        title_font.setBold(True)
        self.title_label.setFont(title_font)
        self.title_label.setAlignment(QtCore.Qt.AlignCenter)
        layout.addWidget(self.title_label)

        self.detail_label = qtw.QLabel(self.frame)
        self.detail_label.setObjectName("plotStateOverlayDetail")
        self.detail_label.setAlignment(QtCore.Qt.AlignCenter)
        self.detail_label.setWordWrap(True)
        layout.addWidget(self.detail_label)

        self.target.installEventFilter(self)
        if self.owner is not self.target:
            self.owner.installEventFilter(self)
        self.hide()

    def show(self, title, detail=None, kind="info"):
        self.title_label.setText(str(title or ""))
        self.detail_label.setText(str(detail or ""))
        self.detail_label.setVisible(bool(detail))
        self.frame.setStyleSheet(self._stylesheet(kind))
        self._sync_geometry()
        self.frame.show()
        self.frame.raise_()

    def hide(self):
        self.frame.hide()

    def eventFilter(self, source, event):
        if (
                (source is self.owner or source is self.target)
                and event.type() in (QtCore.QEvent.Resize, QtCore.QEvent.Show)
                ):
            self._sync_geometry()
        return super().eventFilter(source, event)

    def _stylesheet(self, kind):
        panel_style = self._styles.get(kind, self._styles["info"])
        return (
            "QFrame#plotStateOverlay {"
            f"{panel_style}"
            "border-radius: 6px;"
            "}"
            "QLabel#plotStateOverlayTitle {"
            "background: transparent;"
            "font-size: 13px;"
            "}"
            "QLabel#plotStateOverlayDetail {"
            "background: transparent;"
            "font-size: 11px;"
            "}"
            )

    def _sync_geometry(self):
        if not self.frame.isVisible() and not self.title_label.text():
            return

        target_rect = self.target.rect()
        margin = 24
        max_width = max(160, min(420, target_rect.width() - (2 * margin)))
        self.frame.setMaximumWidth(max_width)
        self.frame.adjustSize()

        size = self.frame.sizeHint()
        width = min(max_width, max(160, size.width()))
        height = size.height()
        x = target_rect.left() + max(margin, (target_rect.width() - width) // 2)
        y = target_rect.top() + max(margin, (target_rect.height() - height) // 2)
        self.frame.setGeometry(x, y, width, height)
