from time import perf_counter
from typing import Any

from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from qplot.datahandling.plot_progress import PlotProgress

from ._plot_status import PlotStatusWidget


class PlotStateOverlay(QtCore.QObject):
    """
    Keep empty/error explanations in the plot and loading progress in its toolbar.
    """

    _styles: dict[str, str] = {
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

    def __init__(self, target: qtw.QWidget, feedback: PlotStatusWidget) -> None:
        super().__init__(target)
        self.feedback = feedback
        self.owner: qtw.QWidget = target
        target_obj: Any = target
        viewport = target_obj.viewport() if hasattr(target_obj, "viewport") else None
        self.target: qtw.QWidget = viewport or target

        self.frame = qtw.QFrame(self.target)
        self.frame.setObjectName("plotStateOverlay")
        self.frame.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.frame.setAutoFillBackground(False)

        layout = qtw.QVBoxLayout(self.frame)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(4)

        self.title_label = qtw.QLabel(self.frame)
        self.title_label.setObjectName("plotStateOverlayTitle")
        title_font = self.title_label.font()
        title_font.setBold(True)
        self.title_label.setFont(title_font)
        self.title_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.title_label)

        self.detail_label = qtw.QLabel(self.frame)
        self.detail_label.setObjectName("plotStateOverlayDetail")
        self.detail_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.detail_label.setWordWrap(True)
        layout.addWidget(self.detail_label)

        self.progress_bar = feedback.progress_bar
        self.elapsed_label = feedback.elapsed_label
        self._worker: Any = None
        self._last_progress: PlotProgress | None = None
        self._started_at: float | None = None
        self._rendering = False
        self.progress_timer = QtCore.QTimer(self)
        self.progress_timer.setInterval(100)
        self.progress_timer.timeout.connect(self._poll_progress)

        self.target.installEventFilter(self)
        if self.owner is not self.target:
            self.owner.installEventFilter(self)
        self.hide()

    def show(
            self,
            title: object,
            detail: object | None = None,
            kind: str = "info",
            ) -> None:
        if kind != "loading":
            self._stop_progress()
        self.title_label.setText(str(title or ""))
        self.detail_label.setText(str(detail or ""))
        self.detail_label.setVisible(bool(detail))
        if kind == "loading":
            self.frame.hide()
            return
        self.feedback.show_message(" — ".join(str(value) for value in (title, detail) if value))
        self.frame.setStyleSheet(self._stylesheet(kind))
        self._sync_geometry()
        self.frame.show()
        self.frame.raise_()

    def hide(self) -> None:
        self._stop_progress()
        self.frame.hide()

    def track(self, worker: Any) -> None:
        """Poll one latest value, never queue an event per reader chunk."""
        self._worker = worker
        self._last_progress = None
        self._started_at = getattr(worker, "started_at", perf_counter())
        self._rendering = False
        self.feedback.start_progress()
        self._poll_progress()
        if self._worker is worker:
            self.progress_timer.start()

    def _stop_progress(self) -> None:
        self.progress_timer.stop()
        self._worker = None
        self._last_progress = None
        self._started_at = None
        self._rendering = False
        self.progress_bar.hide()
        self.elapsed_label.hide()
        self.feedback.finish_progress()

    def _update_elapsed(self) -> None:
        if self._started_at is None:
            return
        seconds = max(0, int(perf_counter() - self._started_at))
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        duration = (f"{hours}h {minutes:02d}m {seconds:02d}s" if hours else
                    f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s")
        text = duration
        if self.elapsed_label.text() != text:
            self.elapsed_label.setText(text)
        self.elapsed_label.show()

    def _poll_progress(self) -> None:
        worker = self._worker
        if worker is None:
            return
        if worker.is_cancelled():
            self.show("Plot load cancelled")
            return
        self._update_elapsed()
        self.feedback.show_progress()
        if self._rendering:
            return
        progress = worker.progress
        if progress == self._last_progress:
            return
        self._last_progress = progress
        self.feedback.set_progress_details(progress, self.title_label.text())
        self.detail_label.setText(progress.title)
        self.detail_label.show()
        if progress.total is None or progress.total <= 0:
            self.progress_bar.setRange(0, 0)
            self.progress_bar.setFormat("")
        else:
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(min(1000, max(0, progress.completed * 1000 // progress.total)))
            self.progress_bar.setFormat("%p%")
        self.progress_bar.show()

    def rendering(self, worker: Any) -> None:
        if worker is not self._worker:
            return
        self._rendering = True
        self._update_elapsed()
        progress = PlotProgress("Rendering plot", stage=2)
        self.detail_label.setText(progress.title)
        self.feedback.set_progress_details(progress, self.title_label.text())
        self.feedback.show_progress()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setFormat("")

    def finish(self, worker: Any) -> None:
        if worker is self._worker:
            # Only a loading state is removed. Empty/error explanations stay.
            self.hide()

    def eventFilter(
            self,
            source: QtCore.QObject | None,
            event: QtCore.QEvent | None,
            ) -> bool:
        if (
                event is not None
                and (source is self.owner or source is self.target)
                and event.type() in (QtCore.QEvent.Type.Resize, QtCore.QEvent.Type.Show)
                ):
            self._sync_geometry()
        return super().eventFilter(source, event)

    def _stylesheet(self, kind: str) -> str:
        panel_style = self._styles.get(kind, self._styles["info"])
        return (
            "QFrame#plotStateOverlay {"
            f"{panel_style}"
            "border-radius: 6px;"
            "}"
            "QLabel#plotStateOverlayTitle {"
            "background: transparent;"
            "font-size: 10pt;"
            "}"
            "QLabel#plotStateOverlayDetail {"
            "background: transparent;"
            "font-size: 8pt;"
            "}"
            "QLabel#plotStateOverlayElapsed {"
            "background: transparent;"
            "font-size: 8pt;"
            "}"
            )

    def _sync_geometry(self) -> None:
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
