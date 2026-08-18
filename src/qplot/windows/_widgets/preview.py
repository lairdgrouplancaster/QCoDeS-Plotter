import json
import threading
from collections import OrderedDict

import numpy as np
from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

from qplot.datahandling.dimensions import (
    MAX_SUPPORTED_PLOT_DIMENSIONS,
    unsupported_plot_message,
)
from qplot.datahandling.file_identity import database_sidecar_identities
from qplot.datahandling.readonly import sqlite_read_only_connection
from qplot.diagnostics import log_exception

from .._dragdrop import make_run_preview_mime

PREVIEW_SIZE = 200
PREVIEW_BACKGROUND_COLOR = "#f4f7fb"
PREVIEW_HEIGHT_PADDING = 48
COLLAPSE_MINIMUM_RATIO = 0.25
MAX_PREVIEW_ROWS = 50_000
MAX_PREVIEW_GRID_CELLS = 250_000
PREVIEW_SAMPLES_PER_CELL = 4
PREVIEW_ROWID_CHUNK = 900
PREVIEW_FILL_EMPTY_MIN_COVERAGE = 0.75
PREVIEW_REMAINING_PRIORITY = 0
PREVIEW_VISIBLE_PRIORITY = 50
PREVIEW_SELECTED_PRIORITY = 100
PREVIEW_PLOTTED_PRIORITY = 125
PREVIEW_MAX_ACTIVE_WORKERS = 2
PREVIEW_SQL_PROGRESS_OPCODES = 1_000
PREVIEW_SELECTED_PROPERTY = "previewSelected"
PREVIEW_CACHE_MAX_BYTES = 128 * 1024 * 1024
PREVIEW_CACHE_MAX_ENTRIES = 512
VIRIDIS_STOPS = np.asarray([
    (68, 1, 84),
    (72, 35, 116),
    (64, 67, 135),
    (52, 94, 141),
    (41, 120, 142),
    (32, 144, 140),
    (34, 167, 132),
    (68, 190, 112),
    (121, 209, 81),
    (189, 223, 38),
    (253, 231, 37),
    ], dtype=np.float64)


class PreviewTab(qtw.QWidget):
    """
    Displays background-generated preview images for database runs.

    """
    plotRequested = QtCore.pyqtSignal(str)
    exportRequested = QtCore.pyqtSignal(str)
    previewsReady = QtCore.pyqtSignal(str, object)
    previewGenerationChanged = QtCore.pyqtSignal(str, bool)

    def __init__(self, *args, preview_size=PREVIEW_SIZE):
        super().__init__(*args)

        self.preview_size = int(preview_size or PREVIEW_SIZE)
        self._update_minimum_height()
        self.database_path = ""
        self.generation = 0
        self.current_guid = None
        self.run_metadata = {}
        self.cache = OrderedDict()
        self.cache_bytes = 0
        self.errors = {}
        self.queue = {}
        self._explicit_guids: set[str] = set()
        self.active: set[tuple[int, str]] = set()
        self._active_priorities: dict[tuple[int, str], int] = {}
        self._workers = {}
        self.metadata_signatures = {}
        self._start_scheduled = False
        self._shutting_down = False

        # A widget-owned QThreadPool waits for its runnables in the QObject
        # destructor.  If a Python QRunnable needs the GIL while SIP is
        # deleting this widget, both threads can wait forever.  The shared Qt
        # pool outlives the widget, so deletion never waits on preview work.
        thread_pool = QtCore.QThreadPool.globalInstance()
        if thread_pool is None:
            raise RuntimeError("Qt global thread pool is unavailable")
        self.thread_pool = thread_pool

        self.scroll_area = qtw.QScrollArea()
        self.scroll_area.setWidgetResizable(True)

        self.content = qtw.QWidget()
        self.content_layout = qtw.QHBoxLayout()
        self.content_layout.setContentsMargins(8, 8, 8, 8)
        self.content_layout.setSpacing(8)
        self.content_layout.addStretch()
        self.content.setLayout(self.content_layout)
        self.scroll_area.setWidget(self.content)

        layout = qtw.QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.scroll_area)
        self.setLayout(layout)

        self._show_message("Select a run")


    def shutdown(self):
        """Stop scheduling preview work without waiting for active queries."""
        if self._shutting_down:
            return

        self._shutting_down = True
        self.generation += 1
        self.queue = {}
        self._explicit_guids = set()
        self.active = set()
        self._active_priorities = {}
        self._start_scheduled = False
        for worker in tuple(self._workers.values()):
            worker.cancel()


    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)


    def preferred_tab_height(self):
        return self.preview_size + PREVIEW_HEIGHT_PADDING


    def _update_minimum_height(self):
        self.setMinimumHeight(
            max(1, round(self.preferred_tab_height() * COLLAPSE_MINIMUM_RATIO))
            )


    def has_database(self, database_path):
        return bool(database_path) and self.database_path == database_path


    def set_preview_size(self, preview_size):
        preview_size = int(preview_size)
        if preview_size == self.preview_size:
            return

        self.preview_size = preview_size
        self._update_minimum_height()
        self._cancel_workers()
        self.generation += 1
        self.cache = OrderedDict()
        self.cache_bytes = 0
        self.errors = {}
        self.queue = {}
        self.active = set()
        self._active_priorities = {}
        self.metadata_signatures = {
            guid: self._metadata_signature(metadata)
            for guid, metadata in self.run_metadata.items()
            }

        if self.current_guid:
            self._show_message("Generating preview...")
            self._enqueue(
                self.current_guid,
                priority=PREVIEW_SELECTED_PRIORITY,
                allow_active=True,
                )
        for guid in self._explicit_guids:
            self._enqueue(guid, priority=PREVIEW_PLOTTED_PRIORITY)
        self._schedule_start_next()


    def set_database_runs(self, database_path, runs):
        self._cancel_workers()
        self.generation += 1
        self.database_path = database_path
        self.current_guid = None
        self.run_metadata = self._normalise_runs(runs)
        self.cache = OrderedDict()
        self.cache_bytes = 0
        self.errors = {}
        self.queue = {}
        self._explicit_guids = set()
        self.active = set()
        self._active_priorities = {}
        self.metadata_signatures = {
            guid: self._metadata_signature(metadata)
            for guid, metadata in self.run_metadata.items()
            }

        self._show_message("Select a run")


    def add_runs(self, runs, queue_previews=True):
        if not self.database_path:
            return

        for guid, metadata in self._normalise_runs(runs).items():
            signature = self._metadata_signature(metadata)
            old_signature = self.metadata_signatures.get(guid)
            changed = old_signature is not None and signature != old_signature
            self.run_metadata[guid] = metadata
            self.metadata_signatures[guid] = signature

            if changed:
                self._drop_cached(guid)
                self.errors.pop(guid, None)
                active_worker = self._workers.get((self.generation, guid))
                if active_worker is not None:
                    active_worker.cancel()

            if queue_previews and guid == self.current_guid:
                self._enqueue(
                    guid,
                    priority=PREVIEW_REMAINING_PRIORITY,
                    allow_active=changed,
                    )
        self._start_next()


    def set_current_run(self, dataset):
        guid = getattr(dataset, "guid", None)
        if not guid:
            self.clear_current_run()
            return

        self.current_guid = guid
        if not self.database_path or guid not in self.run_metadata:
            self._show_message("No preview available")
            return

        if guid in self.cache:
            self._show_previews(self._cached_previews(guid))
            return

        if guid in self.errors:
            self.errors.pop(guid, None)
            self._show_message("Retrying preview...")
        else:
            self._show_message("Generating preview...")

        self._enqueue(guid, priority=PREVIEW_SELECTED_PRIORITY)
        self._start_next()


    def clear_current_run(self):
        self.current_guid = None
        self._show_message("Select a run")


    def _normalise_runs(self, runs):
        if not runs:
            return {}

        out = {}
        for run_id, metadata in runs.items():
            guid = metadata.get("guid")
            if not guid:
                continue

            run_metadata = dict(metadata)
            run_metadata["run_id"] = run_id
            out[guid] = run_metadata
        return out


    def _metadata_signature(self, metadata):
        return tuple(
            _signature_value(metadata.get(key))
            for key in (
                "result_table_name",
                "result_count",
                "run_description",
                "measure_parameters",
                "sweep_parameters",
                "setpoint_shape",
                "point_shape",
                )
            )


    def prioritize_runs(self, selected_run_ids=None, visible_run_ids=None):
        if not self.database_path:
            return

        selected_guids = set(self._guids_for_run_ids(selected_run_ids))
        visible_guids = set(self._guids_for_run_ids(visible_run_ids))
        if self.current_guid:
            selected_guids.add(self.current_guid)
            visible_guids.discard(self.current_guid)

        requested_priorities = {
            guid: PREVIEW_VISIBLE_PRIORITY
            for guid in visible_guids
            }
        requested_priorities.update({
            guid: PREVIEW_SELECTED_PRIORITY
            for guid in selected_guids
            })
        requested_priorities.update({
            guid: PREVIEW_PLOTTED_PRIORITY
            for guid in self._explicit_guids
            })
        requested_guids = set(requested_priorities)
        for guid in list(self.queue):
            if guid not in requested_guids:
                self.queue.pop(guid, None)

        for active_key in tuple(self.active):
            generation, guid = active_key
            if generation != self.generation:
                continue

            requested_priority = requested_priorities.get(guid)
            worker = self._workers.get(active_key)
            if requested_priority is None:
                if worker is not None:
                    worker.cancel()
                continue

            if (
                    worker is not None
                    and getattr(worker, "is_cancelled", lambda: False)()
                    ):
                self._active_priorities[active_key] = requested_priority
                self._enqueue(
                    guid,
                    priority=requested_priority,
                    allow_active=True,
                    )
                continue

            active_priority = self._active_priorities.get(
                active_key,
                PREVIEW_VISIBLE_PRIORITY,
                )
            if requested_priority > active_priority:
                # A visible job that becomes selected already contains exactly
                # the work the interactive slot would perform.
                self._active_priorities[active_key] = requested_priority
            elif requested_priority < active_priority:
                # Release the interactive role when selection moves.  Queue
                # the old run again as background work because cancellation
                # can interrupt its current SQL statement.
                if worker is not None:
                    worker.cancel()
                self._active_priorities[active_key] = requested_priority
                self._enqueue(
                    guid,
                    priority=requested_priority,
                    allow_active=True,
                    )

        for guid in visible_guids:
            self._enqueue(guid, priority=PREVIEW_VISIBLE_PRIORITY)
        for guid in selected_guids:
            self._enqueue(guid, priority=PREVIEW_SELECTED_PRIORITY)
        for guid in self._explicit_guids:
            self._enqueue(guid, priority=PREVIEW_PLOTTED_PRIORITY)

        self._start_next()


    def request_guids(self, guids):
        """Queue plotted runs until their preview generation completes."""
        if not self.database_path:
            return
        if isinstance(guids, (str, bytes)) or not hasattr(guids, "__iter__"):
            guids = [guids]

        for guid in guids:
            if not guid or guid not in self.run_metadata or guid in self.cache:
                continue

            self.errors.pop(guid, None)
            self._explicit_guids.add(guid)
            active_key = (self.generation, guid)
            worker = self._workers.get(active_key)
            if active_key not in self.active:
                self._enqueue(guid, priority=PREVIEW_PLOTTED_PRIORITY)
            elif (
                    worker is not None
                    and getattr(worker, "is_cancelled", lambda: False)()
                    ):
                self._enqueue(
                    guid,
                    priority=PREVIEW_PLOTTED_PRIORITY,
                    allow_active=True,
                    )
            else:
                self._active_priorities[active_key] = PREVIEW_PLOTTED_PRIORITY

        self._start_next()


    def _guids_for_run_ids(self, run_ids):
        if run_ids is None:
            return []
        if isinstance(run_ids, (str, bytes)) or not hasattr(run_ids, "__iter__"):
            run_ids = [run_ids]

        lookup = self._run_id_lookup()
        guids = []
        seen = set()
        for run_id in run_ids:
            for key in self._run_id_keys(run_id):
                guid = lookup.get(key)
                if guid is None or guid in seen:
                    continue
                guids.append(guid)
                seen.add(guid)
                break
        return guids


    def _run_id_lookup(self):
        lookup: dict[int | str, str] = {}
        for guid, metadata in self.run_metadata.items():
            for key in self._run_id_keys(metadata.get("run_id")):
                lookup.setdefault(key, guid)
        return lookup


    def _run_id_keys(self, run_id):
        if run_id is None:
            return []

        keys = []

        def add(value):
            if value not in keys:
                keys.append(value)

        add(run_id)
        try:
            int_run_id = int(run_id)
        except (TypeError, ValueError):
            add(str(run_id))
        else:
            add(int_run_id)
            add(str(int_run_id))
        return keys


    def _enqueue_all(self, priority=PREVIEW_REMAINING_PRIORITY):
        for guid in self.run_metadata:
            self._enqueue(guid, priority=priority)


    def _enqueue(self, guid, priority=0, allow_active=False):
        if self._shutting_down:
            return
        if guid in self.cache:
            return
        if guid in self.errors:
            return
        if (self.generation, guid) in self.active and not allow_active:
            return
        if guid not in self.run_metadata:
            return
        if not self.database_path:
            return

        self.queue[guid] = max(priority, self.queue.get(guid, priority))


    def _cancel_workers(self):
        for worker in tuple(self._workers.values()):
            worker.cancel()


    def _cached_previews(self, guid):
        previews = self.cache.pop(guid)
        self.cache[guid] = previews
        return previews


    def _drop_cached(self, guid):
        previews = self.cache.pop(guid, None)
        if previews is not None:
            self.cache_bytes = max(
                0,
                self.cache_bytes - self._preview_bytes(previews),
                )


    def _store_cached(self, guid, previews):
        self._drop_cached(guid)
        self.cache[guid] = previews
        self.cache_bytes += self._preview_bytes(previews)
        while (
                len(self.cache) > PREVIEW_CACHE_MAX_ENTRIES
                or self.cache_bytes > PREVIEW_CACHE_MAX_BYTES
                ):
            evict_guid = next(
                (cached_guid for cached_guid in self.cache if cached_guid != self.current_guid),
                None,
                )
            if evict_guid is None:
                break
            self._drop_cached(evict_guid)


    @staticmethod
    def _preview_bytes(previews):
        total = 0
        for preview in previews or []:
            image = preview.get("image") if isinstance(preview, dict) else None
            size_in_bytes = getattr(image, "sizeInBytes", None)
            if callable(size_in_bytes):
                total += max(0, int(size_in_bytes()))
        return total


    def _schedule_start_next(self):
        if self._shutting_down or self._start_scheduled:
            return

        self._start_scheduled = True
        QtCore.QTimer.singleShot(0, self._scheduled_start_next)


    def _scheduled_start_next(self):
        self._start_scheduled = False
        self._start_next()


    def _start_next(self):
        if self._shutting_down or not self.queue:
            return

        active_keys = {
            active_key
            for active_key in self.active
            if active_key[0] == self.generation
            }
        available_slots = max(0, PREVIEW_MAX_ACTIVE_WORKERS - len(active_keys))
        if available_slots == 0:
            return
        active_guids = {guid for _generation, guid in active_keys}
        foreground_active = any(
            self._active_priorities.get(active_key, PREVIEW_VISIBLE_PRIORITY)
            >= PREVIEW_SELECTED_PRIORITY
            for active_key in active_keys
            )
        background_active = any(
            self._active_priorities.get(active_key, PREVIEW_VISIBLE_PRIORITY)
            < PREVIEW_SELECTED_PRIORITY
            for active_key in active_keys
            )

        if not foreground_active:
            guid = self._next_queued_guid(
                lambda priority: priority >= PREVIEW_SELECTED_PRIORITY,
                active_guids,
                )
            if guid is not None:
                self._start_worker(guid)
                active_guids.add(guid)
                available_slots -= 1

        if available_slots and not background_active:
            guid = self._next_queued_guid(
                lambda priority: priority < PREVIEW_SELECTED_PRIORITY,
                active_guids,
                )
            if guid is not None:
                self._start_worker(guid)


    def _next_queued_guid(self, accepts_priority, active_guids):
        candidates = [
            guid
            for guid, priority in self.queue.items()
            if accepts_priority(priority) and guid not in active_guids
            ]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda guid: (
                self.queue[guid],
                self.run_metadata[guid].get("run_id", 0),
                ),
            )


    def _start_worker(self, guid):
        priority = self.queue.pop(guid)
        active_key = (self.generation, guid)
        self.active.add(active_key)
        self._active_priorities[active_key] = priority
        self.previewGenerationChanged.emit(guid, True)

        worker = PreviewWorker(
            self.generation,
            self.database_path,
            guid,
            self.run_metadata[guid],
            self.preview_size,
            )
        worker.signals.finished.connect(self._worker_finished)
        self._workers[active_key] = worker
        self.thread_pool.start(worker)


    @QtCore.pyqtSlot(int, str, object, object)
    def _worker_finished(self, generation, guid, previews, error):
        active_key = (generation, guid)
        worker = self._workers.pop(active_key, None)
        was_cancelled = bool(
            worker is not None
            and getattr(worker, "is_cancelled", lambda: False)()
            )
        was_active = active_key in self.active
        self.active.discard(active_key)
        self._active_priorities.pop(active_key, None)
        if self._shutting_down:
            return
        if was_active and generation == self.generation:
            self.previewGenerationChanged.emit(guid, False)

        if generation != self.generation:
            self._start_next()
            return

        if was_cancelled:
            self._start_next()
            return

        self._explicit_guids.discard(guid)
        if error:
            self.errors[guid] = str(error)
        else:
            self._store_cached(guid, previews)
            self.previewsReady.emit(guid, previews)

        if guid == self.current_guid:
            if error:
                self._show_message("Preview failed", str(error))
            else:
                self._show_previews(previews)

        self._start_next()


    def _clear_layout(self):
        while self.content_layout.count():
            item = self.content_layout.takeAt(0)
            if item is None:
                continue
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()


    def _show_message(self, message, tooltip=None):
        self._clear_layout()
        label = qtw.QLabel(message)
        label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        label.setMinimumHeight(120)
        if tooltip:
            label.setToolTip(tooltip)
        self.content_layout.addWidget(label)
        self.content_layout.addStretch()


    def _show_previews(self, previews):
        self._clear_layout()

        if not previews:
            self._show_message("No 1D or 2D previews")
            return

        for preview in previews:
            card = PreviewCard(preview, self.preview_size, self.current_guid, self)
            card.plotRequested.connect(self.plotRequested)
            card.exportRequested.connect(self.exportRequested)
            self.content_layout.addWidget(card)
        self.content_layout.addStretch()


class PreviewCard(qtw.QWidget):
    plotRequested = QtCore.pyqtSignal(str)
    exportRequested = QtCore.pyqtSignal(str)

    def __init__(self, preview, preview_size, guid=None, *args):
        super().__init__(*args)
        self.parameter = preview.get("parameter", "")

        if preview.get("unsupported"):
            label = unsupported_preview_label(
                preview,
                preview_size,
                "previewUnsupported",
                )
            layout = qtw.QHBoxLayout()
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(label)
            self.setLayout(layout)
            return

        image = DraggablePreviewImageLabel(
            guid,
            self.parameter,
            preview.get("axes") or [],
            )
        image.setObjectName("previewImage")
        image.setFixedSize(preview_size, preview_size)
        image.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        image.setPixmap(QtGui.QPixmap.fromImage(preview["image"]))
        image.setToolTip(preview["title"])
        image.set_preview_accessibility(preview["title"])
        image.plotRequested.connect(self.plotRequested)
        image.exportRequested.connect(self.exportRequested)

        layout = qtw.QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(image)
        self.setLayout(layout)


class PreviewImageLabel(qtw.QLabel):
    plotRequested = QtCore.pyqtSignal(str)
    exportRequested = QtCore.pyqtSignal(str)

    def __init__(self, parameter, *args):
        super().__init__(*args)
        self.parameter = parameter
        self._selected = False
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.setProperty(PREVIEW_SELECTED_PROPERTY, False)
        self.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.DefaultContextMenu)
        self.set_preview_accessibility(parameter)


    def set_preview_accessibility(self, title=None):
        title = str(title or self.parameter or "").strip()
        accessible_name = f"Plot preview: {title}" if title else "Plot preview"
        self.setAccessibleName(accessible_name)
        self.setAccessibleDescription(
            "Press Enter or Space to plot. Press Menu or Shift+F10 for plot and "
            "export actions."
            )


    def set_selected(self, selected):
        self._selected = bool(selected)
        self.setProperty(PREVIEW_SELECTED_PROPERTY, self._selected)
        self.update()


    def select_preview(self):
        scope = self._selection_scope()
        if scope is not None:
            for label in scope.findChildren(PreviewImageLabel):
                if label is not self:
                    label.set_selected(False)
        self.set_selected(True)


    def _selection_scope(self):
        parent = self.parentWidget()
        while parent is not None:
            if isinstance(parent, (PreviewTab, qtw.QTreeWidget)):
                return parent
            parent = parent.parentWidget()
        return self.window()


    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton and self.parameter:
            self.select_preview()
        super().mousePressEvent(event)


    def focusInEvent(self, event):
        if self.parameter:
            self.select_preview()
        super().focusInEvent(event)


    def paintEvent(self, event):
        super().paintEvent(event)

        if not self._selected:
            return

        painter = QtGui.QPainter(self)
        pen = QtGui.QPen(self.palette().color(QtGui.QPalette.ColorRole.Highlight))
        pen.setWidth(2)
        painter.setPen(pen)
        painter.drawRect(self.rect().adjusted(1, 1, -2, -2))


    def mouseDoubleClickEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton and self.parameter:
            self.plotRequested.emit(self.parameter)
            event.accept()
            return

        super().mouseDoubleClickEvent(event)


    def keyPressEvent(self, event):
        key = event.key()
        if self.parameter and key in (
                QtCore.Qt.Key.Key_Return,
                QtCore.Qt.Key.Key_Enter,
                QtCore.Qt.Key.Key_Space,
                ):
            self.select_preview()
            self.plotRequested.emit(self.parameter)
            event.accept()
            return

        context_menu_key = key == QtCore.Qt.Key.Key_Menu
        shift_f10 = (
            key == QtCore.Qt.Key.Key_F10
            and bool(event.modifiers() & QtCore.Qt.KeyboardModifier.ShiftModifier)
            )
        if self.parameter and (context_menu_key or shift_f10):
            self.select_preview()
            self._show_context_menu(self.mapToGlobal(self.rect().center()))
            event.accept()
            return

        super().keyPressEvent(event)


    def contextMenuEvent(self, event):
        if not self.parameter:
            super().contextMenuEvent(event)
            return

        self.select_preview()
        self._show_context_menu(event.globalPos())
        event.accept()


    def _show_context_menu(self, global_position):
        menu = qtw.QMenu(self)

        plot_action = QtGui.QAction("&Plot", menu)
        plot_action.triggered.connect(lambda: self.plotRequested.emit(self.parameter))
        menu.addAction(plot_action)

        export_action = QtGui.QAction("&Export CSV...", menu)
        export_action.triggered.connect(lambda: self.exportRequested.emit(self.parameter))
        menu.addAction(export_action)

        menu.exec(global_position)


class DraggablePreviewImageLabel(PreviewImageLabel):
    def __init__(self, guid, parameter, axes=None, *args):
        super().__init__(parameter, *args)
        self.guid = guid or ""
        self.axes = list(axes or [])
        self._drag_start_pos = None
        if self.guid:
            self.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)


    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._drag_start_pos = event.pos()
        super().mousePressEvent(event)


    def mouseMoveEvent(self, event):
        if not (
            event.buttons() & QtCore.Qt.MouseButton.LeftButton
            and self._drag_start_pos is not None
            and self.guid
            and self.parameter
            ):
            super().mouseMoveEvent(event)
            return

        distance = (event.pos() - self._drag_start_pos).manhattanLength()
        if distance < qtw.QApplication.startDragDistance():
            super().mouseMoveEvent(event)
            return

        self._start_drag()
        event.accept()


    def _start_drag(self):
        drag = QtGui.QDrag(self)
        drag.setMimeData(make_run_preview_mime(self.guid, self.parameter, self.axes))

        pixmap = self.pixmap()
        if pixmap is not None and not pixmap.isNull():
            drag.setPixmap(pixmap)
            drag.setHotSpot(QtCore.QPoint(pixmap.width() // 2, pixmap.height() // 2))

        drag.exec(QtCore.Qt.DropAction.CopyAction)


class PreviewWorker(QtCore.QRunnable):
    def __init__(self, generation, database_path, guid, metadata, preview_size):
        super().__init__()
        self.signals = PreviewSignals()
        self.generation = generation
        self.database_path = database_path
        self.sidecar_identities = database_sidecar_identities(database_path)
        self.guid = guid
        self.metadata = metadata
        self.preview_size = preview_size
        self._cancelled = threading.Event()
        self._connection_lock = threading.Lock()
        self._connection = None


    def cancel(self):
        self._cancelled.set()
        with self._connection_lock:
            connection = self._connection
        if connection is not None:
            try:
                connection.interrupt()
            except Exception:
                # The worker may have closed the connection between the lock
                # release and this cross-thread cancellation request.
                pass


    def is_cancelled(self):
        return self._cancelled.is_set()


    def _set_connection(self, connection):
        with self._connection_lock:
            self._connection = connection
        if connection is not None and self._cancelled.is_set():
            try:
                connection.interrupt()
            except Exception:
                pass


    def _emit_finished(self, previews, error):
        try:
            self.signals.finished.emit(
                self.generation,
                self.guid,
                previews,
                error,
            )
        except RuntimeError as err:
            message = str(err)
            if not (
                    "wrapped C/C++ object" in message
                    and "has been deleted" in message
                    ):
                raise


    def run(self):
        try:
            if self._cancelled.is_set():
                previews = []
            else:
                previews = generate_run_previews(
                    self.database_path,
                    self.metadata,
                    size=self.preview_size,
                    is_cancelled=self._cancelled.is_set,
                    connection_callback=self._set_connection,
                )
            if self._cancelled.is_set():
                previews = []
            self._emit_finished(previews, None)
        except Exception as error:
            if self._cancelled.is_set():
                self._emit_finished([], None)
                return
            log_exception("Preview generation failed", error, __name__)
            self._emit_finished([], error)


class PreviewSignals(QtCore.QObject):
    finished = QtCore.pyqtSignal(int, str, object, object)


def generate_run_previews(
        database_path,
        metadata,
        size=PREVIEW_SIZE,
        is_cancelled=None,
        connection_callback=None,
        ):
    if is_cancelled is not None and is_cancelled():
        return []

    table_name = metadata.get("result_table_name")
    if not database_path or not table_name:
        return []

    dependencies = _dependencies_from_metadata(metadata)
    if not dependencies:
        return []

    previews = []
    conn = sqlite_read_only_connection(database_path, timeout=10)
    if connection_callback is not None:
        connection_callback(conn)
    cursor = None
    try:
        if is_cancelled is not None:
            conn.set_progress_handler(
                lambda: int(bool(is_cancelled())),
                PREVIEW_SQL_PROGRESS_OPCODES,
                )
        cursor = conn.cursor()
        available_columns = _table_columns(cursor, table_name)
        for parameter, axes in dependencies.items():
            if is_cancelled is not None and is_cancelled():
                break
            if parameter not in available_columns:
                continue

            declared_axes = [str(axis) for axis in axes]
            if len(declared_axes) > MAX_SUPPORTED_PLOT_DIMENSIONS:
                previews.append(_unsupported_preview(parameter, declared_axes))
                continue

            axes = [axis for axis in declared_axes if axis in available_columns]
            if len(axes) == 1:
                preview = _preview_1d(cursor, table_name, metadata, parameter, axes[0], size)
            elif len(axes) >= 2:
                preview = _preview_2d(
                    cursor,
                    table_name,
                    metadata,
                    parameter,
                    axes[:2],
                    size,
                    is_cancelled=is_cancelled,
                    )
            else:
                continue

            if preview is not None:
                previews.append(preview)
    finally:
        if cursor is not None:
            cursor.close()
        if connection_callback is not None:
            connection_callback(None)
        conn.close()

    return previews


def _unsupported_preview(parameter, axes):
    axes = [str(axis) for axis in axes]
    return {
        "parameter": str(parameter),
        "axes": axes,
        "dimension_count": len(axes),
        "title": unsupported_plot_message(parameter, axes),
        "unsupported": True,
        }


def unsupported_preview_label(preview, size, object_name):
    dimensions = int(preview.get("dimension_count") or len(preview.get("axes") or []))
    text = f"{dimensions}D" if int(size) <= 32 else f"{dimensions}D\nunsupported"
    label = qtw.QLabel(text)
    label.setObjectName(object_name)
    label.setAccessibleName(f"{dimensions}D measurement unsupported")
    label.setFixedSize(int(size), int(size))
    label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
    label.setWordWrap(True)
    label.setToolTip(preview.get("title", "Unsupported measurement dimensionality"))
    label.setFrameShape(qtw.QFrame.Shape.Box)
    label.setFrameShadow(qtw.QFrame.Shadow.Plain)
    return label


def _preview_1d(cursor, table_name, metadata, parameter, axis, size):
    x, y = _select_arrays(
        cursor,
        table_name,
        [axis, parameter],
        metadata,
        eligible_columns=[axis, parameter],
        )
    image = render_sparkline_preview(x, y, size=size)
    return {
        "parameter": parameter,
        "axes": [axis],
        "title": _preview_title(parameter, [axis]),
        "image": image,
        }


def _preview_2d(
        cursor,
        table_name,
        metadata,
        parameter,
        axes,
        size,
        *,
        is_cancelled=None,
        ):
    grid_shape = _preview_grid_shape(metadata, parameter)
    grid = _aggregate_large_heatmap_preview(
        cursor,
        table_name,
        metadata,
        parameter,
        grid_shape,
        size,
        axes=axes,
        is_cancelled=is_cancelled,
        )
    if grid is not None:
        image = render_heatmap_grid_preview(grid, size=size)
        return {
            "parameter": parameter,
            "axes": list(axes),
            "title": _preview_title(parameter, axes),
            "image": image,
            "downsample_strategy": "spatial mean",
            }

    x, y, z = _select_arrays(
        cursor,
        table_name,
        [axes[1], axes[0], parameter],
        metadata,
        max_rows=_preview_2d_row_limit(grid_shape),
        sampling="stratified",
        )
    image = render_heatmap_preview(
        x,
        y,
        z,
        size=size,
        grid_shape=grid_shape,
        )
    return {
        "parameter": parameter,
        "axes": list(axes),
        "title": _preview_title(parameter, axes),
        "image": image,
        }


def _preview_title(parameter, axes):
    axes = [str(axis) for axis in axes if axis]
    if len(axes) == 0:
        return str(parameter)
    if len(axes) == 1:
        axis_text = axes[0]
    elif len(axes) == 2:
        axis_text = f"{axes[0]} and {axes[1]}"
    else:
        axis_text = f"{', '.join(axes[:-1])}, and {axes[-1]}"
    return f"{parameter} vs {axis_text}"


def _preview_2d_row_limit(grid_shape):
    shape = _normalise_grid_shape(grid_shape)
    if shape is None:
        return MAX_PREVIEW_ROWS

    rows, columns = shape
    grid_cells = rows * columns
    if grid_cells <= MAX_PREVIEW_GRID_CELLS:
        return max(MAX_PREVIEW_ROWS, grid_cells)

    return MAX_PREVIEW_ROWS


def render_sparkline_preview(x, y, size=PREVIEW_SIZE):
    image = QtGui.QImage(size, size, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor(PREVIEW_BACKGROUND_COLOR))

    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    valid = np.isfinite(x) & np.isfinite(y)
    x = x[valid]
    y = y[valid]
    if x.size == 0:
        return image

    x_range = _finite_range(x)
    y_range = _finite_range(y)
    plot_margin = 10
    plot_size = size - 2 * plot_margin

    def scale(values, data_range, invert=False):
        low, high = data_range
        if high == low:
            scaled = np.full(values.shape, 0.5)
        else:
            scaled = (values - low) / (high - low)
        if invert:
            scaled = 1 - scaled
        return plot_margin + scaled * plot_size

    xs = scale(x, x_range)
    ys = scale(y, y_range, invert=True)

    painter = QtGui.QPainter(image)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    painter.setPen(QtGui.QPen(QtGui.QColor(210, 0, 0), 3))

    if x.size == 1:
        painter.drawEllipse(QtCore.QPointF(float(xs[0]), float(ys[0])), 3, 3)
    else:
        path = QtGui.QPainterPath(QtCore.QPointF(float(xs[0]), float(ys[0])))
        for x_value, y_value in zip(xs[1:], ys[1:], strict=False):
            path.lineTo(float(x_value), float(y_value))
        painter.drawPath(path)

    painter.end()
    return image


def render_heatmap_preview(x, y, z, size=PREVIEW_SIZE, grid_shape=None):
    image = QtGui.QImage(size, size, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor(PREVIEW_BACKGROUND_COLOR))

    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    z = np.asarray(z, dtype=float)
    valid = np.isfinite(x) & np.isfinite(y) & np.isfinite(z)
    if not np.any(valid):
        return image

    x = x[valid]
    y = y[valid]
    z = z[valid]

    grid = _fixed_heatmap_grid(
        x,
        y,
        z,
        grid_shape,
        max_cells=MAX_PREVIEW_GRID_CELLS,
        )
    if grid is None:
        grid = _unique_axis_heatmap_grid(
            x,
            y,
            z,
            max_cells=MAX_PREVIEW_GRID_CELLS,
            )
    if grid is None:
        grid = _binned_heatmap_grid(x, y, z, size=size, grid_shape=grid_shape)

    return render_heatmap_grid_preview(grid, size=size)


def render_heatmap_grid_preview(grid, size=PREVIEW_SIZE):
    image = QtGui.QImage(size, size, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor(PREVIEW_BACKGROUND_COLOR))

    grid = np.asarray(grid, dtype=float)
    if grid.size == 0 or np.all(np.isnan(grid)):
        return image

    grid = _prepare_heatmap_display_grid(grid, size)
    rgb = _viridis_rgb(grid)
    rgb = np.flipud(rgb)
    rgb_bytes = rgb.tobytes()
    source = QtGui.QImage(
        rgb_bytes,
        rgb.shape[1],
        rgb.shape[0],
        rgb.shape[1] * 3,
        QtGui.QImage.Format.Format_RGB888,
        ).copy()

    return source.scaled(
        size,
        size,
        QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
        QtCore.Qt.TransformationMode.FastTransformation,
        ).convertToFormat(QtGui.QImage.Format.Format_RGB32)


def _aggregate_large_heatmap_preview(
        cursor,
        table_name,
        metadata,
        parameter,
        grid_shape,
        size,
        *,
        axes=None,
        is_cancelled=None,
        ):
    shape = _normalise_grid_shape(grid_shape)
    if not _preview_requires_spatial_aggregation(
            cursor,
            table_name,
            metadata,
            shape,
            ):
        return None

    if axes is None:
        axes = _dependencies_from_metadata(metadata).get(parameter, [])
    if len(axes) < 2:
        return None

    try:
        return _spatial_mean_preview_grid(
            cursor,
            table_name,
            parameter,
            x_axis=axes[1],
            y_axis=axes[0],
            grid_shape=shape,
            size=size,
            )
    except Exception as error:
        if is_cancelled is not None and is_cancelled():
            raise
        log_exception(
            "SQL preview aggregation failed; using streaming spatial means",
            error,
            __name__,
            )
        return _streaming_spatial_mean_preview_grid(
            cursor,
            table_name,
            parameter,
            x_axis=axes[1],
            y_axis=axes[0],
            grid_shape=shape,
            size=size,
            )


def _preview_requires_spatial_aggregation(
        cursor,
        table_name,
        metadata,
        grid_shape,
        ):
    shape = _normalise_grid_shape(grid_shape)
    if shape is not None and shape[0] * shape[1] > MAX_PREVIEW_GRID_CELLS:
        return True

    row_limit = _preview_2d_row_limit(shape)
    if _metadata_result_count(metadata) > row_limit:
        return True

    rowid_span = _rowid_span(cursor, table_name)
    if rowid_span is not None:
        first_rowid, last_rowid = rowid_span
        return last_rowid - first_rowid + 1 > row_limit

    table = _sqlite_identifier(table_name)
    cursor.execute(f"SELECT COUNT(*) FROM {table}")
    row = cursor.fetchone()
    return bool(row and row[0] and int(row[0]) > row_limit)


def _spatial_mean_preview_grid(
        cursor,
        table_name,
        parameter,
        *,
        x_axis,
        y_axis,
        grid_shape,
        size,
        ):
    setup = _spatial_preview_aggregation_setup(
        cursor,
        table_name,
        parameter,
        x_axis=x_axis,
        y_axis=y_axis,
        grid_shape=grid_shape,
        size=size,
        )
    if setup is None:
        return None

    (
        table,
        x_column,
        y_column,
        z_column,
        where_sql,
        full_shape,
        aggregate_shape,
        x_lower_edge,
        x_scale,
        y_lower_edge,
        y_scale,
        ) = setup
    target_rows, target_columns = aggregate_shape
    x_bin_sql = f"MIN(CAST(({x_column} - ?) * ? AS INTEGER), ?)"
    y_bin_sql = f"MIN(CAST(({y_column} - ?) * ? AS INTEGER), ?)"
    cursor.execute(
        (
            "SELECT x_bin, y_bin, AVG(z_value) FROM ("
            f"SELECT {x_bin_sql} AS x_bin, {y_bin_sql} AS y_bin, "
            f"{z_column} AS z_value FROM {table} WHERE {where_sql}"
            ") GROUP BY x_bin, y_bin ORDER BY y_bin, x_bin"
            ),
        (
            x_lower_edge,
            x_scale,
            target_columns - 1,
            y_lower_edge,
            y_scale,
            target_rows - 1,
            ),
        )

    grid = np.full(aggregate_shape, np.nan, dtype=float)
    for x_index, y_index, mean_value in cursor:
        if mean_value is None:
            continue
        try:
            mean_value = float(mean_value)
        except (TypeError, ValueError):
            continue
        if not np.isfinite(mean_value):
            continue
        grid[int(y_index), int(x_index)] = mean_value

    if not np.any(np.isfinite(grid)):
        return None

    return _pad_spatial_preview_grid(grid, full_shape)


def _streaming_spatial_mean_preview_grid(
        cursor,
        table_name,
        parameter,
        *,
        x_axis,
        y_axis,
        grid_shape,
        size,
        ):
    setup = _spatial_preview_aggregation_setup(
        cursor,
        table_name,
        parameter,
        x_axis=x_axis,
        y_axis=y_axis,
        grid_shape=grid_shape,
        size=size,
        )
    if setup is None:
        return None

    (
        table,
        x_column,
        y_column,
        z_column,
        where_sql,
        full_shape,
        aggregate_shape,
        x_lower_edge,
        x_scale,
        y_lower_edge,
        y_scale,
        ) = setup
    target_rows, target_columns = aggregate_shape
    grid_sum = np.zeros(aggregate_shape, dtype=float)
    grid_count = np.zeros(aggregate_shape, dtype=np.int64)
    cursor.execute(
        f"SELECT {x_column}, {y_column}, {z_column} FROM {table} "
        f"WHERE {where_sql} ORDER BY {y_column}, {x_column}, {z_column}"
        )
    for x_value, y_value, z_value in cursor:
        try:
            x_value = float(x_value)
            y_value = float(y_value)
            z_value = float(z_value)
        except (TypeError, ValueError):
            continue
        if not (
                np.isfinite(x_value)
                and np.isfinite(y_value)
                and np.isfinite(z_value)
                ):
            continue

        x_index = int((x_value - x_lower_edge) * x_scale)
        y_index = int((y_value - y_lower_edge) * y_scale)
        x_index = min(max(x_index, 0), target_columns - 1)
        y_index = min(max(y_index, 0), target_rows - 1)
        grid_sum[y_index, x_index] += z_value
        grid_count[y_index, x_index] += 1

    grid = np.full(aggregate_shape, np.nan, dtype=float)
    populated = grid_count > 0
    grid[populated] = grid_sum[populated] / grid_count[populated]
    if not np.any(populated):
        return None

    return _pad_spatial_preview_grid(grid, full_shape)


def _spatial_preview_aggregation_setup(
        cursor,
        table_name,
        parameter,
        *,
        x_axis,
        y_axis,
        grid_shape,
        size,
        ):
    table = _sqlite_identifier(table_name)
    x_column = _sqlite_identifier(x_axis)
    y_column = _sqlite_identifier(y_axis)
    z_column = _sqlite_identifier(parameter)
    where_sql = (
        f"{x_column} IS NOT NULL AND {y_column} IS NOT NULL "
        f"AND {z_column} IS NOT NULL"
        )
    cursor.execute(
        f"SELECT COUNT(*), MIN({x_column}), MAX({x_column}), "
        f"COUNT(DISTINCT {x_column}), MIN({y_column}), "
        f"MAX({y_column}), COUNT(DISTINCT {y_column}) "
        f"FROM {table} WHERE {where_sql}"
        )
    summary = cursor.fetchone()
    if summary is None:
        return None

    (
        source_rows,
        x_min,
        x_max,
        x_count,
        y_min,
        y_max,
        y_count,
        ) = summary
    if (
            not source_rows
            or x_min is None
            or x_max is None
            or y_min is None
            or y_max is None
            or not x_count
            or not y_count
            ):
        return None

    full_shape, aggregate_shape = _spatial_preview_grid_shapes(
        grid_shape,
        observed_rows=int(y_count),
        observed_columns=int(x_count),
        size=size,
        )
    target_rows, target_columns = aggregate_shape
    x_lower_edge, x_scale = _spatial_preview_axis_bins(
        float(x_min),
        float(x_max),
        int(x_count),
        target_columns,
        )
    y_lower_edge, y_scale = _spatial_preview_axis_bins(
        float(y_min),
        float(y_max),
        int(y_count),
        target_rows,
        )
    return (
        table,
        x_column,
        y_column,
        z_column,
        where_sql,
        full_shape,
        aggregate_shape,
        x_lower_edge,
        x_scale,
        y_lower_edge,
        y_scale,
        )


def _spatial_preview_grid_shapes(
        grid_shape,
        *,
        observed_rows,
        observed_columns,
        size,
        ):
    max_cells = min(MAX_PREVIEW_ROWS, max(1, int(size) * int(size)))
    observed_shape = (int(observed_rows), int(observed_columns))
    shape = _normalise_grid_shape(grid_shape)
    if shape is None:
        target_shape = _preview_bin_shape(
            observed_shape,
            size,
            max_cells=max_cells,
            )
        return target_shape, target_shape

    full_shape = _preview_bin_shape(shape, size, max_cells=max_cells)
    aggregate_shape = (
        _covered_preview_bin_count(observed_rows, shape[0], full_shape[0]),
        _covered_preview_bin_count(observed_columns, shape[1], full_shape[1]),
        )
    return full_shape, aggregate_shape


def _covered_preview_bin_count(observed_count, planned_count, target_count):
    observed_count = max(1, int(observed_count))
    planned_count = max(1, int(planned_count))
    target_count = max(1, int(target_count))
    covered = (
        observed_count * target_count + planned_count - 1
        ) // planned_count
    return min(target_count, max(1, covered))


def _pad_spatial_preview_grid(grid, full_shape):
    if grid.shape == full_shape:
        return grid

    padded = np.full(full_shape, np.nan, dtype=float)
    rows = min(grid.shape[0], full_shape[0])
    columns = min(grid.shape[1], full_shape[1])
    padded[:rows, :columns] = grid[:rows, :columns]
    return padded


def _spatial_preview_axis_bins(lower, upper, source_count, bin_count):
    if not np.isfinite(lower) or not np.isfinite(upper):
        raise ValueError("Preview axis bounds must be finite")
    if source_count <= 1 or bin_count <= 1 or lower == upper:
        return lower, 1.0

    source_step = (upper - lower) / (source_count - 1)
    lower_edge = lower - source_step / 2
    bin_width = (upper - lower + source_step) / bin_count
    return lower_edge, 1.0 / bin_width


def _fixed_heatmap_grid(x, y, z, grid_shape, max_cells=None):
    shape = _normalise_grid_shape(grid_shape)
    if shape is None:
        return None

    rows, columns = shape
    if max_cells is not None and rows * columns > max_cells:
        return None

    grid = np.full((rows, columns), np.nan, dtype=float)
    x_index = _axis_value_indices(x, columns)
    y_index = _axis_value_indices(y, rows)
    placed = 0

    if x_index is not None and y_index is not None:
        for x_value, y_value, z_value in zip(x, y, z, strict=False):
            column = x_index.get(float(x_value))
            row = y_index.get(float(y_value))
            if row is None or column is None:
                continue

            grid[row, column] = z_value
            placed += 1

    if placed:
        return grid

    flat_grid = grid.ravel()
    point_count = min(flat_grid.size, z.size)
    flat_grid[:point_count] = z[:point_count]
    return grid


def _unique_axis_heatmap_grid(x, y, z, max_cells):
    unique_x = np.unique(x[np.isfinite(x)])
    unique_y = np.unique(y[np.isfinite(y)])
    if unique_x.size == 0 or unique_y.size == 0:
        return None

    if unique_x.size * unique_y.size > max_cells:
        return None

    x_index = {
        float(value): index
        for index, value in enumerate(unique_x)
        }
    y_index = {
        float(value): index
        for index, value in enumerate(unique_y)
        }
    grid = np.full((unique_y.size, unique_x.size), np.nan, dtype=float)

    for x_value, y_value, z_value in zip(x, y, z, strict=False):
        row = y_index.get(float(y_value))
        column = x_index.get(float(x_value))
        if row is None or column is None:
            continue
        grid[row, column] = z_value

    return grid


def _binned_heatmap_grid(x, y, z, size=PREVIEW_SIZE, grid_shape=None):
    rows, columns = _preview_bin_shape(
        grid_shape,
        size,
        max_cells=max(1, z.size // PREVIEW_SAMPLES_PER_CELL),
        )
    grid_sum = np.zeros((rows, columns), dtype=float)
    grid_count = np.zeros((rows, columns), dtype=float)

    x_bins = _scaled_axis_indices(x, columns)
    y_bins = _scaled_axis_indices(y, rows)
    for row, column, value in zip(y_bins, x_bins, z, strict=False):
        grid_sum[row, column] += value
        grid_count[row, column] += 1

    grid = np.full((rows, columns), np.nan, dtype=float)
    populated = grid_count > 0
    grid[populated] = grid_sum[populated] / grid_count[populated]
    return _fill_empty_heatmap_bins(grid)


def _fill_empty_heatmap_bins(grid):
    if grid.size == 0 or np.all(np.isfinite(grid)):
        return grid
    if not np.any(np.isfinite(grid)):
        return grid

    filled = np.array(grid, dtype=float, copy=True)
    row_positions = np.arange(filled.shape[0], dtype=float)
    column_positions = np.arange(filled.shape[1], dtype=float)

    for column in range(filled.shape[1]):
        values = filled[:, column]
        finite = np.isfinite(values)
        if np.any(finite) and not np.all(finite):
            values[~finite] = np.interp(
                row_positions[~finite],
                row_positions[finite],
                values[finite],
                )

    for row in range(filled.shape[0]):
        values = filled[row, :]
        finite = np.isfinite(values)
        if np.any(finite) and not np.all(finite):
            values[~finite] = np.interp(
                column_positions[~finite],
                column_positions[finite],
                values[finite],
                )

    return filled


def _prepare_heatmap_display_grid(grid, size):
    grid = np.asarray(grid, dtype=float)
    if grid.size == 0:
        return grid

    if _finite_fraction(grid) >= PREVIEW_FILL_EMPTY_MIN_COVERAGE:
        grid = _fill_empty_heatmap_bins(grid)

    rows, columns = grid.shape
    target_rows = min(max(1, int(size)), rows)
    target_columns = min(max(1, int(size)), columns)

    if rows > target_rows:
        grid = _downsample_heatmap_axis(grid, target_rows, axis=0)
    if columns > target_columns:
        grid = _downsample_heatmap_axis(grid, target_columns, axis=1)

    return grid


def _downsample_heatmap_axis(grid, target_count, axis):
    grid = np.asarray(grid, dtype=float)
    axis_length = grid.shape[axis]
    target_count = max(1, min(int(target_count), axis_length))
    if target_count == axis_length:
        return grid

    parts = np.array_split(np.arange(axis_length), target_count)
    if axis == 0:
        out = np.full((target_count, grid.shape[1]), np.nan, dtype=float)
        for index, part in enumerate(parts):
            out[index, :] = _nanmean_no_warning(grid[part, :], axis=0)
    else:
        out = np.full((grid.shape[0], target_count), np.nan, dtype=float)
        for index, part in enumerate(parts):
            out[:, index] = _nanmean_no_warning(grid[:, part], axis=1)

    return out


def _nanmean_no_warning(values, axis):
    values = np.asarray(values, dtype=float)
    finite = np.isfinite(values)
    count = np.sum(finite, axis=axis)
    total = np.nansum(values, axis=axis)
    out = np.full(total.shape, np.nan, dtype=float)
    np.divide(total, count, out=out, where=count > 0)
    return out


def _finite_fraction(values):
    values = np.asarray(values)
    if values.size == 0:
        return 0.0
    return float(np.count_nonzero(np.isfinite(values))) / float(values.size)


def _preview_bin_shape(grid_shape, size, max_cells):
    size = max(1, int(size))
    max_cells = max(1, min(int(max_cells), size * size))
    shape = _normalise_grid_shape(grid_shape)
    if shape is None:
        rows = size
        columns = size
    else:
        rows, columns = shape

    rows = max(1, min(int(rows), size))
    columns = max(1, min(int(columns), size))
    if rows * columns > max_cells:
        scale = np.sqrt(max_cells / (rows * columns))
        rows = max(1, int(rows * scale))
        columns = max(1, int(columns * scale))

    while rows * columns > max_cells:
        if rows >= columns and rows > 1:
            rows -= 1
        elif columns > 1:
            columns -= 1
        else:
            break

    return rows, columns


def _scaled_axis_indices(values, size):
    data_range = _finite_range(values)
    low, high = data_range
    if high == low:
        return np.zeros(values.shape, dtype=int)

    scaled = (values - low) / (high - low)
    indices = np.floor(scaled * size).astype(int)
    return np.clip(indices, 0, size - 1)


def _axis_value_indices(values, size):
    unique_values = np.unique(values[np.isfinite(values)])
    if unique_values.size == 0 or unique_values.size > size:
        return None

    return {
        float(value): index
        for index, value in enumerate(unique_values)
        }


def _normalise_grid_shape(grid_shape):
    if grid_shape is None:
        return None

    try:
        if len(grid_shape) < 2:
            return None
    except TypeError:
        return None

    try:
        rows = int(grid_shape[0])
        columns = int(grid_shape[1])
    except (TypeError, ValueError):
        return None

    if rows <= 0 or columns <= 0:
        return None
    return rows, columns


def _viridis_rgb(values):
    low = np.nanmin(values)
    high = np.nanmax(values)
    if high == low:
        scaled = np.full(values.shape, 0.5, dtype=np.float64)
    else:
        scaled = (values - low) / (high - low)

    nan_values = ~np.isfinite(scaled)
    scaled = np.nan_to_num(scaled, nan=0.0)
    positions = scaled * (len(VIRIDIS_STOPS) - 1)
    lower = np.floor(positions).astype(int)
    upper = np.clip(lower + 1, 0, len(VIRIDIS_STOPS) - 1)
    fraction = (positions - lower)[..., None]

    rgb = VIRIDIS_STOPS[lower] * (1 - fraction) + VIRIDIS_STOPS[upper] * fraction
    rgb = np.clip(rgb, 0, 255).astype(np.uint8)
    rgb[nan_values] = np.array([230, 230, 230], dtype=np.uint8)
    return rgb


def _preview_grid_shape(metadata, parameter):
    run_description = _json_dict(metadata.get("run_description"))
    shapes = run_description.get("shapes")
    if isinstance(shapes, dict):
        shape = shapes.get(parameter)
        normalised = _normalise_grid_shape(shape)
        if normalised is not None:
            return normalised

    for key in ("setpoint_shape", "point_shape"):
        normalised = _normalise_grid_shape(metadata.get(key))
        if normalised is not None:
            return normalised

    return None


def _select_arrays(
        cursor,
        table_name,
        columns,
        metadata,
        max_rows=None,
        sampling="stride",
        eligible_columns=None,
        ):
    max_rows = int(max_rows or MAX_PREVIEW_ROWS)
    count = _metadata_result_count(metadata)

    selected_columns = ", ".join(_sqlite_identifier(column) for column in columns)
    table = _sqlite_identifier(table_name)
    if eligible_columns:
        rows = _select_eligible_rows(
            cursor,
            table,
            selected_columns,
            eligible_columns,
            max_rows,
            )
        return _rows_to_float_arrays(rows, len(columns))

    rowid_span = _rowid_span(cursor, table_name)
    if rowid_span is None:
        cursor.execute(
            f"SELECT {selected_columns} FROM {table} LIMIT ?",
            (max_rows, )
            )
    else:
        first_rowid, last_rowid = rowid_span
        span = max(0, last_rowid - first_rowid + 1)
        if count and count <= max_rows:
            cursor.execute(f"SELECT {selected_columns} FROM {table} ORDER BY rowid")
        elif span and span <= max_rows:
            cursor.execute(f"SELECT {selected_columns} FROM {table} ORDER BY rowid")
        elif sampling == "stratified":
            rows = _select_stratified_rows(
                cursor,
                table,
                selected_columns,
                first_rowid,
                last_rowid,
                max_rows,
                )
            return _rows_to_float_arrays(rows, len(columns))
        else:
            step_source = count if count else span
            step = max(1, (step_source + max_rows - 1) // max_rows)
            cursor.execute(
                f"""
                WITH RECURSIVE sample(rowid) AS (
                    VALUES(?)
                    UNION ALL
                    SELECT rowid + ? FROM sample WHERE rowid + ? <= ?
                )
                SELECT {selected_columns}
                FROM {table}
                WHERE rowid IN (SELECT rowid FROM sample)
                ORDER BY rowid
                LIMIT ?
                """,
                (first_rowid, step, step, last_rowid, max_rows),
                )

    rows = cursor.fetchall()
    if not rows:
        return [np.array([], dtype=float) for _ in columns]

    return _rows_to_float_arrays(rows, len(columns))


def _select_eligible_rows(
        cursor,
        table,
        selected_columns,
        eligible_columns,
        max_rows,
        ):
    predicate = " AND ".join(
        f"{_sqlite_identifier(column)} IS NOT NULL"
        for column in dict.fromkeys(eligible_columns)
        )
    cursor.execute(f"SELECT COUNT(*) FROM {table} WHERE {predicate}")
    eligible_count = int(cursor.fetchone()[0] or 0)
    if eligible_count <= max_rows:
        cursor.execute(
            f"SELECT {selected_columns} FROM {table} "
            f"WHERE {predicate} ORDER BY rowid"
            )
        return cursor.fetchall()

    step = max(1, (eligible_count + max_rows - 1) // max_rows)
    cursor.execute(
        f"""
        WITH eligible AS (
            SELECT {selected_columns},
                   ROW_NUMBER() OVER (ORDER BY rowid) AS qplot_preview_row_number
            FROM {table}
            WHERE {predicate}
        )
        SELECT {selected_columns}
        FROM eligible
        WHERE (qplot_preview_row_number - 1) % ? = 0
        ORDER BY qplot_preview_row_number
        LIMIT ?
        """,
        (step, max_rows),
        )
    return cursor.fetchall()


def _select_stratified_rows(
        cursor,
        table,
        selected_columns,
        first_rowid,
        last_rowid,
        max_rows,
        ):
    rowids = _sample_rowids(first_rowid, last_rowid, max_rows)
    rows = []
    for start in range(0, len(rowids), PREVIEW_ROWID_CHUNK):
        chunk = [int(value) for value in rowids[start:start + PREVIEW_ROWID_CHUNK]]
        placeholders = ", ".join("?" for _ in chunk)
        cursor.execute(
            (
                f"SELECT {selected_columns} FROM {table} "
                f"WHERE rowid IN ({placeholders}) ORDER BY rowid"
                ),
            chunk,
            )
        rows.extend(cursor.fetchall())

    return rows


def _sample_rowids(first_rowid, last_rowid, max_rows):
    span = max(0, last_rowid - first_rowid + 1)
    count = min(max_rows, span)
    if count <= 0:
        return np.array([], dtype=np.int64)

    starts = (np.arange(count, dtype=np.int64) * span) // count
    ends = ((np.arange(1, count + 1, dtype=np.int64) * span) // count) - 1
    widths = np.maximum(ends - starts + 1, 1)
    jitter = (
        np.arange(count, dtype=np.int64) * 1_103_515_245 + 12_345
        ) % widths
    rowids = first_rowid + starts + jitter
    rowids[0] = first_rowid
    rowids[-1] = last_rowid
    return np.unique(rowids)


def _rowid_span(cursor, table_name):
    table = _sqlite_identifier(table_name)
    try:
        cursor.execute(f"SELECT MIN(rowid), MAX(rowid) FROM {table}")
        first_rowid, last_rowid = cursor.fetchone()
    except Exception:
        return None

    if first_rowid is None or last_rowid is None:
        return None

    try:
        return int(first_rowid), int(last_rowid)
    except (TypeError, ValueError):
        return None


def _metadata_result_count(metadata):
    count = metadata.get("result_count")
    try:
        return int(count)
    except (TypeError, ValueError):
        return 0


def _rows_to_float_arrays(rows, column_count):
    columns = []
    for index in range(column_count):
        values = []
        for row in rows:
            value = row[index]
            try:
                values.append(float(value))
            except (TypeError, ValueError):
                values.append(np.nan)
        columns.append(np.asarray(values, dtype=float))
    return columns


def _dependencies_from_metadata(metadata):
    run_description = _json_dict(metadata.get("run_description"))
    dependencies = (
        run_description
        .get("interdependencies_", {})
        .get("dependencies", {})
        )
    if not dependencies:
        dependencies = _legacy_dependencies(run_description)

    normalised = {}
    for parameter, axes in dependencies.items():
        parameter = _parameter_name(parameter)
        if axes is None:
            axes = []
        elif isinstance(axes, (str, dict)):
            axes = [axes]
        axes = [_parameter_name(axis) for axis in axes]
        axes = [axis for axis in axes if axis]
        if parameter and axes:
            normalised[parameter] = axes

    if normalised:
        return normalised

    measure_parameters = metadata.get("measure_parameters") or []
    sweep_parameters = metadata.get("sweep_parameters") or []
    return {
        parameter: list(sweep_parameters)
        for parameter in measure_parameters
        if sweep_parameters
        }


def _legacy_dependencies(run_description):
    out = {}
    paramspecs = run_description.get("interdependencies", {}).get("paramspecs", [])
    for paramspec in paramspecs:
        if not isinstance(paramspec, dict):
            continue
        name = paramspec.get("name")
        depends_on = paramspec.get("depends_on") or []
        if name and depends_on:
            out[name] = depends_on
    return out


def _parameter_name(value):
    if isinstance(value, dict):
        return value.get("name", "")
    return str(value)


def _json_dict(value):
    if not value:
        return {}

    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}

    return decoded if isinstance(decoded, dict) else {}


def _signature_value(value):
    """Freeze nested metadata so in-place mutations cannot hide changes."""

    if isinstance(value, dict):
        return tuple(
            (str(key), _signature_value(item))
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            )
    if isinstance(value, (list, tuple)):
        return tuple(_signature_value(item) for item in value)
    return value


def _table_columns(cursor, table_name):
    cursor.execute(f"PRAGMA table_info({_sqlite_identifier(table_name)})")
    return {row[1] for row in cursor.fetchall()}


def _sqlite_identifier(name):
    return f'"{str(name).replace(chr(34), chr(34) * 2)}"'


def _finite_range(values):
    low = float(np.nanmin(values))
    high = float(np.nanmax(values))
    return low, high
