import json
import sqlite3

import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets as qtw

from qplot.tools.general import data2matrix

from .._dragdrop import make_run_preview_mime


PREVIEW_SIZE = 200
PREVIEW_BACKGROUND_COLOR = "#f4f7fb"
PREVIEW_HEIGHT_PADDING = 48
COLLAPSE_MINIMUM_RATIO = 0.25
MAX_PREVIEW_ROWS = 250000
PREVIEW_SELECTED_PROPERTY = "previewSelected"
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
    Displays background-generated preview images for the selected run.

    """
    plotRequested = QtCore.pyqtSignal(str)
    previewsReady = QtCore.pyqtSignal(str, object)

    def __init__(self, *args, preview_size=PREVIEW_SIZE):
        super().__init__(*args)

        self.preview_size = int(preview_size or PREVIEW_SIZE)
        self._update_minimum_height()
        self.database_path = ""
        self.generation = 0
        self.current_guid = None
        self.run_metadata = {}
        self.cache = {}
        self.errors = {}
        self.queue = {}
        self.active = set()

        self.thread_pool = QtCore.QThreadPool(self)
        self.thread_pool.setMaxThreadCount(1)

        self.scroll = qtw.QScrollArea()
        self.scroll.setWidgetResizable(True)

        self.content = qtw.QWidget()
        self.content_layout = qtw.QHBoxLayout()
        self.content_layout.setContentsMargins(8, 8, 8, 8)
        self.content_layout.setSpacing(8)
        self.content_layout.addStretch()
        self.content.setLayout(self.content_layout)
        self.scroll.setWidget(self.content)

        layout = qtw.QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.scroll)
        self.setLayout(layout)

        self._show_message("Select a run")


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
        self.generation += 1
        self.cache = {}
        self.errors = {}
        self.queue = {}

        for guid in self.run_metadata:
            priority = 100 if guid == self.current_guid else 0
            self._enqueue(guid, priority=priority, allow_active=True)

        if self.current_guid:
            self._show_message("Generating preview...")
        self._start_next()


    def set_database_runs(self, database_path, runs):
        self.generation += 1
        self.database_path = database_path
        self.current_guid = None
        self.run_metadata = self._normalise_runs(runs)
        self.cache = {}
        self.errors = {}
        self.queue = {}
        self.active = set()

        self._show_message("Select a run")
        for guid in self.run_metadata:
            self._enqueue(guid, priority=0)
        self._start_next()


    def add_runs(self, runs):
        if not self.database_path:
            return

        for guid, metadata in self._normalise_runs(runs).items():
            self.run_metadata[guid] = metadata
            self._enqueue(guid, priority=0)
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
            self._show_previews(self.cache[guid])
            return

        if guid in self.errors:
            self._show_message("Preview failed", self.errors[guid])
            return

        self._show_message("Generating preview...")
        self._enqueue(guid, priority=100)
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


    def _enqueue(self, guid, priority=0, allow_active=False):
        if guid in self.cache:
            return
        if guid in self.active and not allow_active:
            return
        if guid not in self.run_metadata:
            return

        self.queue[guid] = max(priority, self.queue.get(guid, priority))


    def _start_next(self):
        if self.active or not self.queue:
            return

        guid = max(self.queue, key=lambda item: (self.queue[item], self.run_metadata[item].get("run_id", 0)))
        self.queue.pop(guid, None)
        self.active.add(guid)

        worker = PreviewWorker(
            self.generation,
            self.database_path,
            guid,
            self.run_metadata[guid],
            self.preview_size,
            )
        worker.signals.finished.connect(self._worker_finished)
        self.thread_pool.start(worker)


    @QtCore.pyqtSlot(int, str, object, object)
    def _worker_finished(self, generation, guid, previews, error):
        self.active.discard(guid)

        if generation != self.generation:
            self._start_next()
            return

        if error:
            self.errors[guid] = str(error)
        else:
            self.cache[guid] = previews
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
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()


    def _show_message(self, message, tooltip=None):
        self._clear_layout()
        label = qtw.QLabel(message)
        label.setAlignment(QtCore.Qt.AlignCenter)
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
            self.content_layout.addWidget(card)
        self.content_layout.addStretch()


class PreviewCard(qtw.QWidget):
    plotRequested = QtCore.pyqtSignal(str)

    def __init__(self, preview, preview_size, guid=None, *args):
        super().__init__(*args)
        self.parameter = preview.get("parameter", "")

        image = DraggablePreviewImageLabel(
            guid,
            self.parameter,
            preview.get("axes") or [],
            )
        image.setObjectName("previewImage")
        image.setFixedSize(preview_size, preview_size)
        image.setAlignment(QtCore.Qt.AlignCenter)
        image.setPixmap(QtGui.QPixmap.fromImage(preview["image"]))
        image.setToolTip(preview["title"])
        image.plotRequested.connect(self.plotRequested)

        layout = qtw.QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(image)
        self.setLayout(layout)


class PreviewImageLabel(qtw.QLabel):
    plotRequested = QtCore.pyqtSignal(str)

    def __init__(self, parameter, *args):
        super().__init__(*args)
        self.parameter = parameter
        self._selected = False
        self.setFocusPolicy(QtCore.Qt.ClickFocus)
        self.setProperty(PREVIEW_SELECTED_PROPERTY, False)


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
        if event.button() == QtCore.Qt.LeftButton and self.parameter:
            self.select_preview()
        super().mousePressEvent(event)


    def paintEvent(self, event):
        super().paintEvent(event)

        if not self._selected:
            return

        painter = QtGui.QPainter(self)
        pen = QtGui.QPen(self.palette().color(QtGui.QPalette.Highlight))
        pen.setWidth(2)
        painter.setPen(pen)
        painter.drawRect(self.rect().adjusted(1, 1, -2, -2))


    def mouseDoubleClickEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton and self.parameter:
            self.plotRequested.emit(self.parameter)
            event.accept()
            return

        super().mouseDoubleClickEvent(event)


class DraggablePreviewImageLabel(PreviewImageLabel):
    def __init__(self, guid, parameter, axes=None, *args):
        super().__init__(parameter, *args)
        self.guid = guid or ""
        self.axes = list(axes or [])
        self._drag_start_pos = None
        if self.guid:
            self.setCursor(QtCore.Qt.OpenHandCursor)


    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            self._drag_start_pos = event.pos()
        super().mousePressEvent(event)


    def mouseMoveEvent(self, event):
        if not (
            event.buttons() & QtCore.Qt.LeftButton
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

        drag.exec_(QtCore.Qt.CopyAction)


class PreviewWorker(QtCore.QRunnable):
    def __init__(self, generation, database_path, guid, metadata, preview_size):
        super().__init__()
        self.signals = PreviewSignals()
        self.generation = generation
        self.database_path = database_path
        self.guid = guid
        self.metadata = metadata
        self.preview_size = preview_size


    def run(self):
        try:
            previews = generate_run_previews(
                self.database_path,
                self.metadata,
                size=self.preview_size,
                )
            self.signals.finished.emit(self.generation, self.guid, previews, None)
        except Exception as error:
            self.signals.finished.emit(self.generation, self.guid, [], error)


class PreviewSignals(QtCore.QObject):
    finished = QtCore.pyqtSignal(int, str, object, object)


def generate_run_previews(database_path, metadata, size=PREVIEW_SIZE):
    table_name = metadata.get("result_table_name")
    if not database_path or not table_name:
        return []

    dependencies = _dependencies_from_metadata(metadata)
    if not dependencies:
        return []

    previews = []
    conn = sqlite3.connect(database_path, timeout=10)
    try:
        cursor = conn.cursor()
        available_columns = _table_columns(cursor, table_name)
        for parameter, axes in dependencies.items():
            if parameter not in available_columns:
                continue

            axes = [axis for axis in axes if axis in available_columns]
            if len(axes) == 1:
                preview = _preview_1d(cursor, table_name, metadata, parameter, axes[0], size)
            elif len(axes) >= 2:
                preview = _preview_2d(cursor, table_name, metadata, parameter, axes[:2], size)
            else:
                continue

            if preview is not None:
                previews.append(preview)
    finally:
        conn.close()

    return previews


def _preview_1d(cursor, table_name, metadata, parameter, axis, size):
    x, y = _select_arrays(cursor, table_name, [axis, parameter], metadata)
    image = render_sparkline_preview(x, y, size=size)
    return {
        "parameter": parameter,
        "axes": [axis],
        "title": _preview_title(parameter, [axis]),
        "image": image,
        }


def _preview_2d(cursor, table_name, metadata, parameter, axes, size):
    x, y, z = _select_arrays(cursor, table_name, [axes[1], axes[0], parameter], metadata)
    image = render_heatmap_preview(x, y, z, size=size)
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


def render_sparkline_preview(x, y, size=PREVIEW_SIZE):
    image = QtGui.QImage(size, size, QtGui.QImage.Format_RGB32)
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
    painter.setRenderHint(QtGui.QPainter.Antialiasing)
    painter.setPen(QtGui.QPen(QtGui.QColor(210, 0, 0), 3))

    if x.size == 1:
        painter.drawEllipse(QtCore.QPointF(float(xs[0]), float(ys[0])), 3, 3)
    else:
        path = QtGui.QPainterPath(QtCore.QPointF(float(xs[0]), float(ys[0])))
        for x_value, y_value in zip(xs[1:], ys[1:]):
            path.lineTo(float(x_value), float(y_value))
        painter.drawPath(path)

    painter.end()
    return image


def render_heatmap_preview(x, y, z, size=PREVIEW_SIZE):
    image = QtGui.QImage(size, size, QtGui.QImage.Format_RGB32)
    image.fill(QtGui.QColor(PREVIEW_BACKGROUND_COLOR))

    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    z = np.asarray(z, dtype=float)
    valid = np.isfinite(x) & np.isfinite(y) & np.isfinite(z)
    if not np.any(valid):
        return image

    grid = data2matrix(y[valid], x[valid], z[valid]).to_numpy(float)
    if grid.size == 0 or np.all(np.isnan(grid)):
        return image

    rgb = _viridis_rgb(grid)
    rgb = np.flipud(rgb)
    rgb_bytes = rgb.tobytes()
    source = QtGui.QImage(
        rgb_bytes,
        rgb.shape[1],
        rgb.shape[0],
        rgb.shape[1] * 3,
        QtGui.QImage.Format_RGB888,
        ).copy()

    return source.scaled(
        size,
        size,
        QtCore.Qt.IgnoreAspectRatio,
        QtCore.Qt.FastTransformation,
        ).convertToFormat(QtGui.QImage.Format_RGB32)


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


def _select_arrays(cursor, table_name, columns, metadata):
    count = metadata.get("result_count")
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = 0

    step = max(1, count // MAX_PREVIEW_ROWS) if count else 1
    selected_columns = ", ".join(_sqlite_identifier(column) for column in columns)
    table = _sqlite_identifier(table_name)
    if step > 1:
        cursor.execute(
            f"SELECT {selected_columns} FROM {table} WHERE rowid % ? = 0 ORDER BY rowid",
            (step, )
            )
    else:
        cursor.execute(f"SELECT {selected_columns} FROM {table} ORDER BY rowid")

    rows = cursor.fetchall()
    if not rows:
        return [np.array([], dtype=float) for _ in columns]

    return _rows_to_float_arrays(rows, len(columns))


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


def _table_columns(cursor, table_name):
    cursor.execute(f"PRAGMA table_info({_sqlite_identifier(table_name)})")
    return {row[1] for row in cursor.fetchall()}


def _sqlite_identifier(name):
    return f'"{str(name).replace(chr(34), chr(34) * 2)}"'


def _finite_range(values):
    low = float(np.nanmin(values))
    high = float(np.nanmax(values))
    return low, high
