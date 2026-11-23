"""Cancellable export serialization with GUI-owned publication and lifecycle."""

from __future__ import annotations

import os
import threading
from dataclasses import replace
from typing import Any

from PyQt6 import QtCore, QtGui, sip
from PyQt6 import QtWidgets as qtw

from qplot.diagnostics import log_exception

from ._error_dialog import ErrorDialog
from ._export_paths import ExportDestinationTransaction, StagedExport
from ._file_manager import reveal_file_in_file_manager
from ._window_controls import _window_is_retired


class ExportCancelled(InterruptedError):
    """An export was cancelled before its destination was published."""


class ExportBatchRow(qtw.QTreeWidgetItem):
    """Keep batch progress accurate when old completed rows leave the history."""

    def __init__(self, title: str):
        super().__init__([title])
        self.setFlags(self.flags() & ~QtCore.Qt.ItemFlag.ItemIsSelectable)
        self.queued = 0
        self.finished = 0

    def update_progress(self):
        self.setText(1, f"{self.finished} of {self.queued} finished")


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
        self.row = qtw.QTreeWidgetItem([os.path.basename(destination.filename), "Queued", ""])
        self.row.setToolTip(0, f"{title}\n{destination.filename}")
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
        self._single_export: ExportTask | None = None
        self._totals = {"saved": 0, "cancelled": 0, "failed": 0}
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
        self.dialog.resize(380, 100)
        layout = qtw.QVBoxLayout(self.dialog)
        self.summary = qtw.QLabel("No exports yet.")
        self.summary.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.summary.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.tree = qtw.QTreeWidget()
        self.tree.setToolTip(
            "Select a completed export to open it or show it in its folder. Select a failed "
            "export for details, or unfinished exports to cancel."
        )
        self.tree.setHeaderLabels(["Filename", "Status", "Progress"])
        self.tree.hide()
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(qtw.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setColumnWidth(0, 230)
        self.tree.setColumnWidth(1, 210)
        layout.addWidget(self.tree)
        buttons = qtw.QHBoxLayout()
        self.open_file_button = qtw.QPushButton("Open file")
        self.open_file_button.setToolTip("Open the selected completed export in its default application.")
        self.open_file_button.clicked.connect(lambda: self._open_selected_export(folder=False))
        buttons.addWidget(self.open_file_button)
        self.open_folder_button = qtw.QPushButton("Show in folder")
        self.open_folder_button.setToolTip("Show the selected completed export in your file manager.")
        self.open_folder_button.clicked.connect(lambda: self._open_selected_export(folder=True))
        buttons.addWidget(self.open_folder_button)
        self.details_button = qtw.QPushButton("Details…")
        self.details_button.setToolTip("View and copy the selected export's failure details.")
        self.details_button.clicked.connect(self._show_selected_failure)
        buttons.addWidget(self.details_button)
        buttons.addStretch()
        self.history_button = qtw.QPushButton("Export history")
        self.history_button.clicked.connect(self._show_history)
        self.history_button.hide()
        buttons.addWidget(self.history_button)
        self.cancel_selected_button = qtw.QPushButton("Cancel selected")
        self.cancel_selected_button.setToolTip("Cancel the selected unfinished exports. Completed files are kept.")
        self.cancel_selected_button.setEnabled(False)
        self.cancel_selected_button.clicked.connect(self.cancel_selected)
        self.tree.itemSelectionChanged.connect(self._update_selection_actions)
        self.tree.itemDoubleClicked.connect(self._export_row_activated)
        buttons.addWidget(self.cancel_selected_button)
        self.cancel_button = qtw.QPushButton("Cancel all")
        self.cancel_button.setToolTip("Cancel all unfinished exports. Completed files are kept.")
        self.cancel_button.clicked.connect(self.cancel_all)
        buttons.addWidget(self.cancel_button)
        self.dismiss_button = qtw.QPushButton("Close")
        self.dismiss_button.setToolTip("Close the export progress window.")
        self.dismiss_button.clicked.connect(self.dialog.hide)
        buttons.addWidget(self.dismiss_button)
        layout.addLayout(buttons)
        self._update_selection_actions()

    def start(self, owner, *, title, destination, writer, before_publish=None,
              on_success=None, on_failure=None, source_keys=(), batch=None) -> ExportTask:
        if _window_is_retired(owner) or getattr(self.main, "_shutdown_started", False):
            raise ExportCancelled("The window is closing.")
        if not isinstance(destination, ExportDestinationTransaction):
            from ._export_paths import prepare_export_destination
            destination = prepare_export_destination(owner, os.fspath(destination))
        if any(task.destination.filename == destination.filename for task in self.tasks):
            raise ValueError("An export to this destination is already in progress.")
        task = ExportTask(self, owner, title, destination, writer,
                          before_publish, on_success, on_failure, source_keys)
        self._single_export = task if not self.tasks and batch is None else None
        self.history_button.hide()
        self.tasks.add(task)
        self.history.append(task)
        while len(self.history) > 200:
            retired = next((item for item in self.history if item.done), None)
            if retired is None:
                break
            self.history.remove(retired)
            group = retired.row.parent()
            if group is None:
                self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(retired.row))
            else:
                group.removeChild(retired.row)
                if group.childCount() == 0:
                    self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(group))
            retired.bar.deleteLater()
        owner.__dict__.setdefault("_export_tasks", set()).add(task)
        if self.tree.isHidden():
            self.dialog.resize(660, 280)
            self.tree.show()
        first_output = batch is None or batch.queued == 0
        if batch is None:
            self.tree.addTopLevelItem(task.row)
        else:
            if batch.treeWidget() is None:
                self.tree.addTopLevelItem(batch)
                self.tree.setRootIsDecorated(True)
            batch.addChild(task.row)
            batch.queued += 1
            batch.update_progress()
            batch.setExpanded(True)
        self.tree.setItemWidget(task.row, 2, task.bar)
        if first_output or self.tree.currentItem() is None:
            self.tree.setCurrentItem(task.row)
        self._update_selection_actions()
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

    def _show_history(self) -> None:
        self._single_export = None
        self.history_button.hide()
        self.tree.show()
        self.dialog.resize(660, 280)
        self.poll()

    def cancel_all(self) -> None:
        for task in tuple(self.tasks):
            task.cancel()
        # Poll retires unstarted tasks without allocating a stage or worker.
        # The active worker retains its stage until its cancellation returns.
        self._update_selection_actions()

    def _selected_task(self) -> ExportTask | None:
        selected = self.tree.selectedItems()
        if len(selected) != 1:
            return None
        return next((task for task in self.history if task.row is selected[0]), None)

    @staticmethod
    def _has_failure_details(task: ExportTask | None) -> bool:
        return bool(task is not None and task.done and task.error is not None
                    and not task.cancelled and not task.stage.published)

    def _update_selection_actions(self) -> None:
        selected = self.tree.selectedItems()
        cancel_selected = any(
            task.row in selected and not task.cancelled for task in self.tasks
        )
        task = self._selected_task()
        completed = bool(task is not None and task.done and task.stage.published)
        for button, available in (
            (self.cancel_button, any(not item.cancelled for item in self.tasks)),
            (self.cancel_selected_button, cancel_selected),
            (self.open_file_button, completed), (self.open_folder_button, completed),
            (self.details_button, self._has_failure_details(task)),
        ):
            button.setEnabled(available)
            button.setVisible(available)

    def _open_selected_export(self, *, folder: bool) -> None:
        task = self._selected_task()
        if task is None or not task.done or not task.stage.published:
            return
        filename = task.destination.filename
        title = "Could not show export in folder" if folder else "Could not open exported file"
        if not os.path.isfile(filename):
            self._show_details(title, "The exported file is no longer available.", filename)
        elif folder:
            if not reveal_file_in_file_manager(filename):
                self._show_details(title, "Your file manager could not show the exported file.", filename)
        elif not QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(filename)):
            self._show_details(title, "Your system could not open the exported file.", filename)

    def _show_details(self, title: str, message: str, details: str) -> None:
        dialog = ErrorDialog(title, message, details, self.dialog)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _show_selected_failure(self) -> None:
        self._show_failure_details(self._selected_task())

    def _export_row_activated(self, row: qtw.QTreeWidgetItem, _column: int) -> None:
        task = next((task for task in self.history if task.row is row), None)
        self._show_failure_details(task)

    def _show_failure_details(self, task: ExportTask | None) -> None:
        if not self._has_failure_details(task) or task is None:
            return
        details = (
            f"Export: {task.title}\nDestination: {task.destination.filename}\n\n"
            f"{type(task.error).__name__}: {task.error}"
        )
        self._show_details("Export failed", "The export could not be completed.", details)

    def cancel_selected(self) -> None:
        selected = self.tree.selectedItems()
        for task in tuple(self.tasks):
            if task.row in selected:
                task.cancel()
        self._update_selection_actions()

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
        counts = self._totals
        details = (
            f"{counts['saved']} exported · {counts['cancelled']} cancelled · "
            f"{counts['failed']} failed"
        )
        if self.tasks:
            summary = f"Export in progress. This session: {details} · {len(self.tasks)} pending."
        elif self._single_export is not None and self._single_export.stage.published:
            task = self._single_export
            summary = f"Export complete\n{task.destination.filename}"
            if not self.tree.isHidden():
                self.tree.clearSelection()
                self.tree.setCurrentItem(task.row)
                task.row.setSelected(True)
                self.tree.hide()
            self.history_button.setVisible(len(self.history) > 1)
        elif counts['saved'] and not (counts['cancelled'] or counts['failed']):
            noun = "file" if counts["saved"] == 1 else "files"
            summary = f"Exports complete. {counts['saved']} {noun} saved this session."
        else:
            summary = f"This session: {details}." if self.history else "No exports yet."
        self.summary.setText(summary)
        self.dismiss_button.setText("Hide" if self.tasks else "Done" if self.history else "Close")
        self.dismiss_button.setToolTip(
            "Hide this window while exports continue in the background."
            if self.tasks else "Close the export progress window."
        )
        self.cancel_button.setEnabled(bool(self.tasks))
        self._update_selection_actions()

        if self.history and self.tree.isHidden():
            self.dialog.resize(440, self.dialog.heightForWidth(440))

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
        outcome = "saved" if task.stage.published else "cancelled" if task.cancelled else "failed"
        self._totals[outcome] += 1
        batch = task.row.parent()
        if isinstance(batch, ExportBatchRow):
            batch.finished += 1
            batch.update_progress()
        task.bar.setRange(0, 100)
        task.bar.setValue(100 if task.stage.published else 0)
        status = "Export complete" if task.stage.published else "Cancelled" if task.cancelled else "Failed"
        task.row.setText(1, status)
        task.bar.setFormat("Complete" if task.stage.published else status)
        if task.stage.published:
            style = self.dialog.style()
            if style is not None:
                task.row.setIcon(1, style.standardIcon(qtw.QStyle.StandardPixmap.SP_DialogApplyButton))
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
                      source_keys=(), batch=None) -> ExportTask:
    return export_task_manager(owner).start(
        owner, title=title, destination=destination, writer=writer,
        before_publish=before_publish, on_success=on_success,
        on_failure=on_failure, source_keys=source_keys, batch=batch,
    )


def cancel_owner_exports(owner) -> None:
    for task in tuple(getattr(owner, "_export_tasks", ())):
        task.cancel()
