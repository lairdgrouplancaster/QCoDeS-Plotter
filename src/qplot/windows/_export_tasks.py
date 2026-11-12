"""Cancellable export serialization with GUI-owned publication and lifecycle."""

from __future__ import annotations

import os
import threading
from dataclasses import replace
from typing import Any

from PyQt6 import QtCore, sip
from PyQt6 import QtWidgets as qtw

from qplot.diagnostics import log_exception

from ._export_paths import ExportDestinationTransaction, StagedExport
from ._window_controls import _window_is_retired


class ExportCancelled(InterruptedError):
    """An export was cancelled before its destination was published."""


class _ExportWorker(QtCore.QRunnable):
    def __init__(self, task: ExportTask):
        super().__init__()
        self.task = task

    def run(self) -> None:
        task = self.task
        try:
            task.check_cancelled()
            task.result = task.stage.write(
                lambda filename: task.writer(filename, task.is_cancelled, task.progress)
            )
            task.check_cancelled()
            if task.result is False:
                raise RuntimeError("The export writer did not produce an export.")
        except BaseException as error:
            task.error = error.with_traceback(None)
        finally:
            task._written.set()


class ExportTask:
    """One immutable request; workers never dereference its Qt owner."""

    def __init__(self, manager, owner, title, destination, writer,
                 before_publish, on_success, on_failure, source_keys):
        self.manager = manager
        self.owner = owner
        self.title = title
        self.destination = destination
        self.source_keys = tuple(source_keys)
        self.writer = writer
        self.before_publish = before_publish
        self.on_success = on_success
        self.on_failure = on_failure
        self.stage = StagedExport(destination, defer=True)
        self.result: Any = None
        self.error: BaseException | None = None
        self.done = False
        self._cancelled = threading.Event()
        self._written = threading.Event()
        self._lock = threading.RLock()
        self._progress: tuple[str, int, int | None] = ("Queued", 0, None)
        self._started = False
        self._worker: _ExportWorker | None = None
        self.row = qtw.QTreeWidgetItem([title, "Queued", ""])
        self.row.setToolTip(0, destination.filename)
        self.bar = qtw.QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setFormat("Waiting")
        self.bar.setMaximumHeight(18)

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    def check_cancelled(self) -> None:
        if self.cancelled:
            raise ExportCancelled("Export cancelled.")

    def cancel(self, *_args) -> None:
        with self._lock:
            if not self.done and not self.stage.published:
                self._cancelled.set()

    def progress(self, phase: str, completed: int = 0, total: int | None = None) -> None:
        with self._lock:
            self._progress = (str(phase), int(completed), total)


class ExportTaskManager(QtCore.QObject):
    """One bounded worker pool and nonmodal progress window per main window."""

    def __init__(self, main):
        super().__init__(main)
        self.main = main
        self.tasks: set[ExportTask] = set()
        self.history: list[ExportTask] = []
        self._polling = False
        self.pool = main._shutdown.resources.create_pool(
            "exportThreadPool", parent=main, max_threads=1,
        )
        main._shutdown.resources.watch_worker(
            "_export_task_manager", cancel=self.cancel_all,
        )
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.poll)
        self.dialog = qtw.QDialog(main)
        self.dialog.setWindowTitle("Exports")
        self.dialog.setModal(False)
        self.dialog.resize(660, 280)
        layout = qtw.QVBoxLayout(self.dialog)
        self.summary = qtw.QLabel("Exports run in the background. Completed files are kept if you cancel.")
        self.summary.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.summary.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.tree = qtw.QTreeWidget()
        self.tree.setToolTip("Select exports to cancel. Each row shows its destination, status and progress.")
        self.tree.setHeaderLabels(["Export", "Status", "Progress"])
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(qtw.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setColumnWidth(0, 230)
        self.tree.setColumnWidth(1, 210)
        layout.addWidget(self.tree)
        buttons = qtw.QHBoxLayout()
        self.cancel_selected_button = qtw.QPushButton("Cancel selected")
        self.cancel_selected_button.setToolTip("Cancel the selected unfinished exports. Completed files are kept.")
        self.cancel_selected_button.setEnabled(False)
        self.cancel_selected_button.clicked.connect(self.cancel_selected)
        self.tree.itemSelectionChanged.connect(self._update_cancel_selected_button)
        buttons.addWidget(self.cancel_selected_button)
        self.cancel_button = qtw.QPushButton("Cancel all")
        self.cancel_button.setToolTip("Cancel all unfinished exports. Completed files are kept.")
        self.cancel_button.clicked.connect(self.cancel_all)
        buttons.addWidget(self.cancel_button)
        buttons.addStretch()
        self.dismiss_button = qtw.QPushButton("Done")
        self.dismiss_button.setToolTip("Close the export progress window.")
        self.dismiss_button.clicked.connect(self.dialog.hide)
        buttons.addWidget(self.dismiss_button)
        layout.addLayout(buttons)

    def start(self, owner, *, title, destination, writer, before_publish=None,
              on_success=None, on_failure=None, source_keys=()) -> ExportTask:
        if _window_is_retired(owner) or getattr(self.main, "_shutdown_started", False):
            raise ExportCancelled("The window is closing.")
        if not isinstance(destination, ExportDestinationTransaction):
            from ._export_paths import prepare_export_destination
            destination = prepare_export_destination(owner, os.fspath(destination))
        if any(task.destination.filename == destination.filename for task in self.tasks):
            raise ValueError("An export to this destination is already in progress.")
        task = ExportTask(self, owner, title, destination, writer,
                          before_publish, on_success, on_failure, source_keys)
        self.tasks.add(task)
        self.history.append(task)
        while len(self.history) > 200:
            retired = next((item for item in self.history if item.done), None)
            if retired is None:
                break
            self.history.remove(retired)
            self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(retired.row))
            retired.bar.deleteLater()
        owner.__dict__.setdefault("_export_tasks", set()).add(task)
        self.tree.addTopLevelItem(task.row)
        self.tree.setItemWidget(task.row, 2, task.bar)
        owner.destroyed.connect(task.cancel)
        closed = getattr(owner, "closed", None)
        if closed is not None and hasattr(closed, "connect"):
            closed.connect(task.cancel)
        self.cancel_button.setEnabled(True)
        self.dismiss_button.setText("Hide")
        self.dismiss_button.setToolTip("Hide this window while exports continue in the background.")
        self.summary.setText("Export in progress. Completed files are kept if you cancel.")
        self.dialog.show()
        self.timer.start()
        self._dispatch()
        return task

    def _dispatch(self) -> None:
        """Keep one active stage, irrespective of the size of the batch."""
        if any(task._started for task in self.tasks):
            return
        task = next((item for item in self.history
                     if item in self.tasks and not item.cancelled), None)
        if task is None:
            return
        task._started = True
        try:
            task.check_cancelled()
            task.stage.prepare()
            task._worker = _ExportWorker(task)
            self.pool.start(task._worker)
        except BaseException as error:
            task.error = error.with_traceback(None)
            task._written.set()

    def cancel_all(self) -> None:
        for task in tuple(self.tasks):
            task.cancel()
        # Poll retires unstarted tasks without allocating a stage or worker.
        # The active worker retains its stage until its cancellation returns.
        self._update_cancel_selected_button()

    def _update_cancel_selected_button(self) -> None:
        selected = self.tree.selectedItems()
        self.cancel_selected_button.setEnabled(any(
            task.row in selected and not task.cancelled for task in self.tasks
        ))

    def cancel_selected(self) -> None:
        selected = self.tree.selectedItems()
        for task in tuple(self.tasks):
            if task.row in selected:
                task.cancel()
        self._update_cancel_selected_button()

    def cancel_owner(self, owner) -> None:
        for task in tuple(self.tasks):
            if task.owner is owner:
                task.cancel()

    def poll(self) -> None:
        # Completion callbacks can close a window; shutdown inspection polls
        # export work again while this outer publication is still unwinding.
        if self._polling:
            return
        self._polling = True
        try:
            self._poll()
        finally:
            self._polling = False

    def _poll(self) -> None:
        for task in tuple(self.tasks):
            if _window_is_retired(task.owner) or getattr(self.main, "_shutdown_started", False):
                task.cancel()
            if task.cancelled and not task._started:
                task._written.set()
            if not task._written.is_set():
                with task._lock:
                    phase, count, total = task._progress
                task.row.setText(1, "Cancelling…" if task.cancelled else phase)
                if not task._started:
                    task.bar.setRange(0, 100)
                    task.bar.setValue(0)
                    task.bar.setFormat("Waiting")
                elif total is not None and total > 0:
                    task.bar.setRange(0, 1000)
                    task.bar.setValue(min(1000, max(0, int(count * 1000 / total))))
                    task.bar.setFormat("%p%")
                else:
                    task.bar.setRange(0, 0)
                    task.bar.setFormat("%p%")
                continue
            self._finish(task)
        self._dispatch()
        if not self.tasks:
            self.timer.stop()
        counts = {"saved": 0, "cancelled": 0, "failed": 0}
        for task in self.history:
            if task.done:
                key = "saved" if task.stage.published else "cancelled" if task.cancelled else "failed"
                counts[key] += 1
        details = (
            f"{counts['saved']} exported · {counts['cancelled']} cancelled · "
            f"{counts['failed']} failed"
        )
        if self.tasks:
            summary = f"Export in progress. {details} · {len(self.tasks)} pending."
        elif counts['saved'] == len(self.history) and self.history:
            if len(self.history) == 1:
                summary = (
                    "Export complete. Your file is ready:\n"
                    f"{self.history[0].destination.filename}"
                )
            else:
                summary = f"Exports complete. {counts['saved']} files are ready."
        else:
            summary = f"Exports finished. {details}."
        self.summary.setText(summary)
        self.dismiss_button.setText("Hide" if self.tasks else "Done")
        self.dismiss_button.setToolTip(
            "Hide this window while exports continue in the background."
            if self.tasks else "Close the export progress window."
        )
        self.cancel_button.setEnabled(bool(self.tasks))
        self._update_cancel_selected_button()

    def _finish(self, task: ExportTask) -> None:
        try:
            with task._lock:
                task.check_cancelled()
                if task.error is not None:
                    raise task.error
                if _window_is_retired(task.owner) or getattr(self.main, "_shutdown_started", False):
                    raise ExportCancelled("The window closed before export completed.")
                def validate():
                    task.check_cancelled()
                    if task.before_publish is not None:
                        task.before_publish()
                    task.check_cancelled()
                task.stage.publish(validate)
        except InterruptedError as error:
            task.cancel()
            task.error = error.with_traceback(None)
        except BaseException as error:
            task.error = error.with_traceback(None)
            log_exception("Export failed", error, __name__)
        finally:
            try:
                task.stage.close()
            except Exception as error:
                task.error = error.with_traceback(None)
                log_exception("Export cleanup failed", error, __name__)
            task.done = True
            self.tasks.discard(task)
            task.owner.__dict__.get("_export_tasks", set()).discard(task)
            if not sip.isdeleted(task.owner):
                try:
                    task.owner.destroyed.disconnect(task.cancel)
                    closed = getattr(task.owner, "closed", None)
                    if closed is not None and hasattr(closed, "disconnect"):
                        closed.disconnect(task.cancel)
                except (RuntimeError, TypeError):
                    pass
            task.writer = None
            task.before_publish = None
        task.bar.setRange(0, 100)
        task.bar.setValue(100 if task.stage.published else 0)
        status = "Export complete" if task.stage.published else "Cancelled" if task.cancelled else "Failed"
        task.row.setText(1, status)
        task.bar.setFormat("Complete" if task.stage.published else status)
        if task.stage.published:
            task.row.setIcon(1, self.dialog.style().standardIcon(qtw.QStyle.StandardPixmap.SP_DialogApplyButton))
        if task.error is not None:
            task.row.setToolTip(1, str(task.error))
        if task.stage.published and task.on_success is not None:
            try:
                task.on_success(task.result)
            except Exception as error:
                log_exception("Export completion notification failed", error, __name__)
        elif task.error is not None and not task.cancelled and not _window_is_retired(task.owner):
            handled = False
            if task.on_failure is not None:
                try:
                    handled = bool(task.on_failure(task.error))
                except Exception as error:
                    log_exception("Export failure notification failed", error, __name__)
            show_status = getattr(task.owner, "show_status", None)
            if not handled and callable(show_status):
                show_status(f"Export failed: {task.error}", 8000)
        task.on_success = None
        task.on_failure = None
        # Completed progress rows must not retain a closed plot's datasets,
        # callback closures or live reader through their original Qt owner.
        task.owner = None
        task.source_keys = ()
        task.destination = replace(task.destination, owner=None, existing_target_validator=None)
        task.stage.destination = task.destination
        task._worker = None


def export_task_manager(owner) -> ExportTaskManager:
    """Resolve a plot's exact pool owner, rather than an arbitrary main window."""
    main: Any = owner if hasattr(owner, "_shutdown") else None
    if main is None:
        for candidate in qtw.QApplication.topLevelWidgets():
            if (hasattr(candidate, "_shutdown")
                    and getattr(candidate, "threadPool", None) is getattr(owner, "threadPool", None)):
                main = candidate
                break
    if main is None:
        raise RuntimeError("The export has no active main window.")
    manager = getattr(main, "_export_task_manager", None)
    if manager is None:
        manager = ExportTaskManager(main)
        main._export_task_manager = manager
    return manager


def start_export_task(owner, *, title, destination, writer,
                      before_publish=None, on_success=None, on_failure=None,
                      source_keys=()) -> ExportTask:
    return export_task_manager(owner).start(
        owner, title=title, destination=destination, writer=writer,
        before_publish=before_publish, on_success=on_success,
        on_failure=on_failure, source_keys=source_keys,
    )


def cancel_owner_exports(owner) -> None:
    for task in tuple(getattr(owner, "_export_tasks", ())):
        task.cancel()
