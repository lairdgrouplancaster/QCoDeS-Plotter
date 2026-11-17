"""Qt and source-validation boundary for preview job ownership."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from weakref import ref

from PyQt6 import QtCore

from qplot.datahandling.readonly import DatabaseInstanceChangedError

from ._preview_controller import PreviewJob

if TYPE_CHECKING:
    from .preview import PreviewTab


class PreviewQtHost:
    def __init__(self, tab: PreviewTab) -> None:
        # The widget owns its controller. A strong reverse link would also
        # retain it through the controller's destroyed-signal callback.
        self._tab = ref(tab)

    @property
    def tab(self) -> PreviewTab:
        tab = self._tab()
        if tab is None:
            raise RuntimeError("Preview widget is no longer available")
        return tab

    def create_worker(self, job: PreviewJob) -> Any:
        from .preview import (
            PREVIEW_SELECTED_THUMBNAIL_PRIORITY,
            PREVIEW_THUMBNAIL_SIZE,
            PREVIEW_VISIBLE_PRIORITY,
            PreviewWorker,
        )

        tab = self.tab
        binding = tab._database_binding
        if binding is None:
            return None
        try:
            tab.database_instance = binding.require_current()
        except DatabaseInstanceChangedError:
            tab._database_was_replaced(binding.database_instance)
            return None
        worker = PreviewWorker(
            job.generation, binding, job.guid, tab.run_metadata[job.guid],
            PREVIEW_THUMBNAIL_SIZE if job.priority in (
                PREVIEW_VISIBLE_PRIORITY, PREVIEW_SELECTED_THUMBNAIL_PRIORITY,
            ) else tab.preview_size,
        )
        return worker

    def connect_worker(self, job: PreviewJob) -> None:
        job.worker.complex_modes = self.tab.complex_modes
        job.worker.signals.finished.connect(self.tab._worker_signal_finished)

    def submit_worker(self, job: PreviewJob) -> None:
        self.tab.thread_pool.start(job.worker)

    def activity(self, job: PreviewJob, active: bool) -> None:
        self.tab.previewGenerationChanged.emit(job.guid, active)

    def publish(self, job: PreviewJob, previews: Any, error: Any) -> None:
        self.tab._publish_preview(job, previews, error)

    def failed(self, job: PreviewJob, error: Exception) -> None:
        from .preview import (
            PREVIEW_SELECTED_THUMBNAIL_PRIORITY,
            PREVIEW_VISIBLE_PRIORITY,
            _bounded_preview_error,
        )

        tab = self.tab
        errors = tab.thumbnail_errors if job.priority in (
            PREVIEW_VISIBLE_PRIORITY, PREVIEW_SELECTED_THUMBNAIL_PRIORITY,
        ) else tab.errors
        errors[job.guid] = _bounded_preview_error(error)
        tab._explicit_guids.discard(job.guid)
        if job.guid == tab.current_guid:
            tab._show_message("Preview failed", errors[job.guid])

    def schedule(self, callback: Callable[[], None]) -> None:
        timer = QtCore.QTimer(self.tab)
        timer.setSingleShot(True)

        def run() -> None:
            try:
                callback()
            finally:
                timer.deleteLater()

        timer.timeout.connect(run)
        timer.start(0)

    def settled(self) -> None:
        self.tab._start_next()
