from PyQt5 import (
    QtWidgets as qtw,
    QtCore
    )
from PyQt5.QtGui import (
    QDesktopServices,
    QIntValidator,
    QKeySequence,
    )

from qplot.windows import (
    plot1d,
    plot2d,
    )
from ._widgets import (
    RunList,
    moreInfo,
    )
from ._widgets.preview import PREVIEW_SIZE
from ._shortcuts import standard_key_sequences
from ._help import add_help_menu
from ._window_controls import (
    add_confirmation_options,
    add_restore_defaults_option,
    add_standard_window_controls,
    close_all_warning_enabled,
    )
from qplot.diagnostics import log_event, log_exception, log_user_error
from qplot.datahandling import (
    find_new_runs,
    get_runs_via_sql,
    )
from qplot import config

import subprocess
import sys
import queue
import threading
from qcodes.dataset import (
    initialise_or_create_database_at,
    load_by_id,
    load_by_guid
    )
from qcodes.dataset.sqlite.database import (
    get_DB_location
    )

import sqlite3
import os
from datetime import datetime
from time import perf_counter

import numpy as np
import pandas as pd

DATABASE_ACCESS_TIMEOUT_SECONDS = 3
DATABASE_CLOUD_SYNC_TIMEOUT_SECONDS = 120
DATABASE_CLOUD_SYNC_CHUNK_BYTES = 4 * 1024 * 1024
DATABASE_CLOUD_SYNC_STATUS_INTERVAL = 1.0
DATABASE_PREFETCH_STATUS_PREFIX = "QPLOT_PREFETCH_PROGRESS:"
CLOUD_PLACEHOLDER_XATTR_MARKERS = (
    "com.apple.fileprovider",
    "com.apple.fileutil.PlaceholderData",
    "com.microsoft.OneDrive",
    )


def database_path_from_mime_data(mime_data):
    """
    Return a dropped local .db path, if the drop contains exactly one.

    """
    if not mime_data.hasUrls():
        return None

    urls = mime_data.urls()
    if len(urls) != 1:
        return None

    url = urls[0]
    if not url.isLocalFile():
        return None

    path = os.path.normpath(url.toLocalFile())
    if os.path.isfile(path) and path.lower().endswith(".db"):
        return path

    return None


def database_access_error(database_path, timeout=DATABASE_ACCESS_TIMEOUT_SECONDS):
    """
    Return an error message if a database cannot be opened promptly.

    QCoDeS initialisation can block inside SQLite when another process or a
    cloud-sync provider holds the database. Probe in a short-lived interpreter
    first so a stuck access check can be timed out without freezing qPlot.

    """
    probe = (
        "import sqlite3, sys\n"
        "conn = sqlite3.connect(sys.argv[1], timeout=1)\n"
        "try:\n"
        "    conn.isolation_level = None\n"
        "    conn.execute('BEGIN')\n"
        "    conn.execute('PRAGMA user_version').fetchone()\n"
        "    conn.commit()\n"
        "finally:\n"
        "    conn.close()\n"
    )

    try:
        result = subprocess.run(
            [sys.executable, "-c", probe, database_path],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            )
    except subprocess.TimeoutExpired:
        return (
            f"Timed out after {timeout:g} s while checking database access. "
            "The database may be locked by another qPlot, QCoDeS, Python, or "
            "notebook process, or blocked by cloud sync."
            )
    except OSError as err:
        return str(err)

    if result.returncode == 0:
        return None

    details = (result.stderr or result.stdout or "").strip()
    if not details:
        details = f"SQLite access probe exited with code {result.returncode}."
    return details


def database_cloud_storage_label(database_path):
    """
    Returns a user-facing cloud provider label when the path looks cloud-backed.

    """
    path = os.path.abspath(str(database_path or ""))
    lower_path = path.lower()
    if "onedrive" in lower_path:
        return "OneDrive"
    if "dropbox" in lower_path:
        return "Dropbox"
    if "google drive" in lower_path:
        return "Google Drive"
    if f"{os.sep}box{os.sep}" in lower_path:
        return "Box"
    if f"{os.sep}library{os.sep}cloudstorage{os.sep}" in lower_path:
        return "cloud storage"
    return None


def database_is_likely_cloud_placeholder(database_path):
    """
    Returns true when a database appears to be a cloud placeholder.

    """
    listxattr = getattr(os, "listxattr", None)
    if listxattr is None:
        attributes = []
    else:
        try:
            attributes = listxattr(database_path)
        except OSError:
            attributes = []

    for attribute in attributes:
        if any(marker in attribute for marker in CLOUD_PLACEHOLDER_XATTR_MARKERS):
            return True

    if database_cloud_storage_label(database_path) is None:
        return False

    try:
        info = os.stat(database_path)
    except OSError:
        return False

    logical_size = getattr(info, "st_size", 0)
    allocated_size = getattr(info, "st_blocks", 0) * 512
    return logical_size > 0 and allocated_size == 0


def prefetch_database_file(
        database_path,
        status_callback=None,
        chunk_size=DATABASE_CLOUD_SYNC_CHUNK_BYTES,
        status_interval=DATABASE_CLOUD_SYNC_STATUS_INTERVAL,
        ):
    """
    Reads a database sequentially to trigger cloud Files-On-Demand hydration.

    """
    total_bytes = os.path.getsize(database_path)
    if total_bytes <= 0:
        return 0

    provider = database_cloud_storage_label(database_path) or "cloud storage"
    bytes_read = 0
    last_status = 0.0

    def emit_status(force=False):
        nonlocal last_status
        if status_callback is None:
            return

        now = perf_counter()
        if not force and now - last_status < status_interval:
            return

        percent = min(100.0, (bytes_read / total_bytes) * 100.0)
        status_callback(
            f"Waiting for {provider} sync... {percent:.0f}% available"
            )
        last_status = now

    emit_status(force=True)
    with open(database_path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            bytes_read += len(chunk)
            emit_status()

    emit_status(force=True)
    return bytes_read


def prefetch_database_file_with_timeout(
        database_path,
        timeout=DATABASE_CLOUD_SYNC_TIMEOUT_SECONDS,
        status_callback=None,
        cancelled_callback=None,
        ):
    """
    Runs cloud prefetch in a subprocess so stalled providers can be timed out.

    """
    timeout = float(timeout)
    provider = database_cloud_storage_label(database_path) or "cloud storage"
    if status_callback is not None:
        status_callback(f"Waiting for {provider} sync...")

    script = (
        "import os, sys, time\n"
        f"prefix = {DATABASE_PREFETCH_STATUS_PREFIX!r}\n"
        f"chunk_size = {DATABASE_CLOUD_SYNC_CHUNK_BYTES!r}\n"
        f"status_interval = {DATABASE_CLOUD_SYNC_STATUS_INTERVAL!r}\n"
        "path = sys.argv[1]\n"
        "total = os.path.getsize(path)\n"
        "read = 0\n"
        "last = 0.0\n"
        "def report(force=False):\n"
        "    global last\n"
        "    if total <= 0:\n"
        "        percent = 100.0\n"
        "    else:\n"
        "        percent = min(100.0, (read / total) * 100.0)\n"
        "    now = time.perf_counter()\n"
        "    if force or now - last >= status_interval:\n"
        "        print(prefix + f'{percent:.0f}', flush=True)\n"
        "        last = now\n"
        "report(True)\n"
        "with open(path, 'rb') as handle:\n"
        "    while True:\n"
        "        chunk = handle.read(chunk_size)\n"
        "        if not chunk:\n"
        "            break\n"
        "        read += len(chunk)\n"
        "        report(False)\n"
        "report(True)\n"
        "print(read, flush=True)\n"
    )

    try:
        process = subprocess.Popen(
            [sys.executable, "-c", script, database_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            )
    except OSError as err:
        raise RuntimeError(str(err)) from err

    output_queue = queue.Queue()
    stdout_thread = threading.Thread(
        target=_read_prefetch_pipe,
        args=(process.stdout, "stdout", output_queue),
        daemon=True,
        )
    stderr_thread = threading.Thread(
        target=_read_prefetch_pipe,
        args=(process.stderr, "stderr", output_queue),
        daemon=True,
        )
    stdout_thread.start()
    stderr_thread.start()

    start = perf_counter()
    stderr_lines = []
    bytes_read = 0

    try:
        while True:
            try:
                stream, line = output_queue.get(timeout=0.1)
            except queue.Empty:
                stream = line = None

            if stream == "stdout":
                parsed = _handle_prefetch_stdout_line(line, provider, status_callback)
                if parsed is not None:
                    bytes_read = parsed
            elif stream == "stderr" and line:
                stderr_lines.append(line)

            if process.poll() is not None:
                stdout_thread.join(timeout=0.2)
                stderr_thread.join(timeout=0.2)
                parsed = _drain_prefetch_queue(
                    output_queue,
                    provider,
                    status_callback,
                    stderr_lines,
                    )
                if parsed is not None:
                    bytes_read = parsed
                break

            if cancelled_callback is not None and cancelled_callback():
                process.kill()
                process.wait()
                raise InterruptedError("Database load cancelled.")

            if perf_counter() - start > timeout:
                process.kill()
                process.wait()
                raise TimeoutError(
                    (
                        f"Timed out after {timeout:g} s while waiting for "
                        f"{provider} to download the database. Check that "
                        f"{provider} is running and signed in, or mark the "
                        "database folder as always available on this device."
                    )
                    )
    finally:
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()

    stdout_thread.join(timeout=0.5)
    stderr_thread.join(timeout=0.5)

    if process.returncode != 0:
        details = "\n".join(line for line in stderr_lines if line)
        if not details:
            details = f"Cloud sync prefetch exited with code {process.returncode}."
        raise RuntimeError(details)

    return bytes_read


def _read_prefetch_pipe(pipe, stream_name, output_queue):
    if pipe is None:
        return

    for line in pipe:
        output_queue.put((stream_name, line.rstrip()))


def _handle_prefetch_stdout_line(line, provider, status_callback):
    if not line:
        return None

    if line.startswith(DATABASE_PREFETCH_STATUS_PREFIX):
        percent = line[len(DATABASE_PREFETCH_STATUS_PREFIX):]
        if status_callback is not None:
            status_callback(f"Waiting for {provider} sync... {percent}% available")
        return None

    try:
        return int(line)
    except ValueError:
        return None


def _drain_prefetch_queue(output_queue, provider, status_callback, stderr_lines):
    bytes_read = None
    while True:
        try:
            stream, line = output_queue.get_nowait()
        except queue.Empty:
            break

        if stream == "stdout":
            parsed = _handle_prefetch_stdout_line(line, provider, status_callback)
            if parsed is not None:
                bytes_read = parsed
        elif stream == "stderr" and line:
            stderr_lines.append(line)

    return bytes_read


class DatabaseLoadSignals(QtCore.QObject):
    """
    Signals emitted by a background database load.

    """
    status = QtCore.pyqtSignal(int, str)
    finished = QtCore.pyqtSignal(int, str, object, object)


class DatabaseLoadWorker(QtCore.QRunnable):
    """
    Loads database metadata away from the GUI thread.

    The worker is responsible for the blocking parts of opening a database:
    access probing, QCoDeS initialisation, and collecting the run table
    metadata. Widget updates stay in MainWindow on the GUI thread.

    """

    def __init__(
            self,
            generation,
            database_path,
            cloud_sync_timeout=DATABASE_CLOUD_SYNC_TIMEOUT_SECONDS,
            ):
        super().__init__()
        self.signals = DatabaseLoadSignals()
        self.generation = generation
        self.database_path = database_path
        self.cloud_sync_timeout = cloud_sync_timeout
        self._cancelled = threading.Event()


    def cancel(self):
        """
        Marks this load as cancelled so later phases do not run.

        """
        self._cancelled.set()


    def run(self):
        try:
            if self._is_cancelled():
                return

            if database_is_likely_cloud_placeholder(self.database_path):
                self._prefetch_cloud_file()
            if self._is_cancelled():
                return

            self._emit_status("Checking database access...")
            access_error = database_access_error(self.database_path)
            if self._is_cancelled():
                return

            if (
                    access_error
                    and database_cloud_storage_label(self.database_path)
                    and os.path.isfile(self.database_path)
                    ):
                self._prefetch_cloud_file()
                if self._is_cancelled():
                    return
                self._emit_status("Checking database access...")
                access_error = database_access_error(self.database_path)
                if self._is_cancelled():
                    return

            if access_error:
                raise RuntimeError(access_error)

            self._emit_status("Initialising database...")
            initialise_or_create_database_at(self.database_path)
            if self._is_cancelled():
                return

            self._emit_status("Loading run list...")
            runs = get_runs_via_sql() or {}
            if self._is_cancelled():
                return
        except InterruptedError:
            return
        except Exception as err:
            log_exception("Database load worker failed", err, __name__)
            self._emit_finished({}, err)
            return

        self._emit_finished(runs, None)


    def _is_cancelled(self):
        return self._cancelled.is_set()


    def _emit_status(self, message):
        try:
            self.signals.status.emit(self.generation, message)
        except RuntimeError as err:
            if not self._qt_signal_was_deleted(err):
                raise


    def _emit_finished(self, runs, error):
        try:
            self.signals.finished.emit(self.generation, self.database_path, runs, error)
        except RuntimeError as err:
            if not self._qt_signal_was_deleted(err):
                raise


    def _qt_signal_was_deleted(self, err):
        message = str(err)
        return "wrapped C/C++ object" in message and "has been deleted" in message


    def _prefetch_cloud_file(self):
        prefetch_database_file_with_timeout(
            self.database_path,
            timeout=self.cloud_sync_timeout,
            status_callback=self._emit_status,
            cancelled_callback=self._is_cancelled,
            )


def database_info_report(database_path):
    """
    Build a diagnostic text report for a QCoDeS database file.

    """
    if not database_path:
        raise ValueError("No database is loaded.")

    if not os.path.isfile(database_path):
        raise FileNotFoundError(database_path)

    path = os.path.abspath(database_path)
    file_size = os.path.getsize(path)

    conn = sqlite3.connect(path, timeout=10)
    try:
        cursor = conn.cursor()
        user_version = _pragma_value(cursor, "user_version")
        summary = {
            "path": path,
            "folder": os.path.dirname(path),
            "filename": os.path.basename(path),
            "file_size": file_size,
            "file_modified": os.path.getmtime(path),
            "user_version": user_version,
            "application_id": _pragma_value(cursor, "application_id"),
            "page_count": _pragma_value(cursor, "page_count"),
            "page_size": _pragma_value(cursor, "page_size"),
            "table_count": _table_count(cursor),
            "experiment_count": _row_count(cursor, "experiments"),
            "run_count": _row_count(cursor, "runs"),
            "latest_run": _latest_run(cursor),
            }
    finally:
        conn.close()

    return _format_database_info(summary)


def _pragma_value(cursor, name):
    cursor.execute(f"PRAGMA {name}")
    value = cursor.fetchone()
    return value[0] if value else None


def _table_count(cursor):
    cursor.execute("""
      SELECT COUNT(*)
      FROM sqlite_master
      WHERE type='table' AND name NOT LIKE 'sqlite_%'
    """)
    return cursor.fetchone()[0]


def _row_count(cursor, table_name):
    if not _table_exists(cursor, table_name):
        return None

    cursor.execute(f"SELECT COUNT(*) FROM {_sqlite_identifier(table_name)}")
    return cursor.fetchone()[0]


def _table_exists(cursor, table_name):
    cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table_name, )
        )
    return cursor.fetchone() is not None


def _latest_run(cursor):
    if not _table_exists(cursor, "runs"):
        return None

    cursor.execute("""
      SELECT run_id, name, run_timestamp, completed_timestamp, is_completed, guid
      FROM runs
      ORDER BY run_id DESC
      LIMIT 1
    """)
    value = cursor.fetchone()
    if value is None:
        return None

    return {
        "run_id": value[0],
        "name": value[1],
        "run_timestamp": value[2],
        "completed_timestamp": value[3],
        "is_completed": value[4],
        "guid": value[5],
        }


def _sqlite_identifier(name):
    return f'"{str(name).replace(chr(34), chr(34) * 2)}"'


def _format_database_info(summary):
    latest_run = summary["latest_run"]
    latest_run_lines = ["Latest run: None"]
    if latest_run:
        status = "completed" if latest_run.get("is_completed") else "running or incomplete"
        latest_run_lines = [
            f"Latest run ID: {latest_run.get('run_id')}",
            f"Latest run name: {_display_value(latest_run.get('name'))}",
            f"Latest run status: {status}",
            f"Latest run started: {_timestamp_value(latest_run.get('run_timestamp'))}",
            f"Latest run completed: {_timestamp_value(latest_run.get('completed_timestamp'))}",
            f"Latest run GUID: {_display_value(latest_run.get('guid'))}",
            ]

    page_bytes = None
    if summary["page_count"] is not None and summary["page_size"] is not None:
        page_bytes = int(summary["page_count"]) * int(summary["page_size"])

    lines = [
        f"Database: {summary['filename']}",
        f"Path: {summary['path']}",
        f"Folder: {summary['folder']}",
        f"File size: {_format_bytes(summary['file_size'])}",
        f"Last modified: {_timestamp_value(summary['file_modified'])}",
        f"SQLite allocated size: {_format_bytes(page_bytes)}",
        "",
        f"Database schema version: {_display_value(summary['user_version'])}",
        f"SQLite application_id: {_display_value(summary['application_id'])}",
        "",
        f"Tables: {_display_value(summary['table_count'])}",
        f"Experiments: {_display_value(summary['experiment_count'])}",
        f"Runs: {_display_value(summary['run_count'])}",
        "",
        *latest_run_lines,
        ]
    return "\n".join(lines)


def _format_bytes(value):
    if value is None:
        return "Unknown"

    value = int(value)
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{value} B"
        size /= 1024


def _timestamp_value(value):
    if value in (None, ""):
        return "Not recorded"

    try:
        return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return str(value)


def _display_value(value):
    if value in (None, ""):
        return "Unknown"
    return str(value)


class DatabasePathLineEdit(qtw.QLineEdit):
    """
    Read-only database path field that accepts dropped QCoDeS database files.

    """
    databaseDropped = QtCore.pyqtSignal(str)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._database_path = ""
        self.setAcceptDrops(True)

    def setText(self, text):
        self._database_path = str(text or "")

        if not self._database_path:
            super().setText("")
            self.setToolTip("Current database path. Drop a QCoDeS .db file here to load it.")
            return

        super().setText(os.path.basename(self._database_path) or self._database_path)
        self.setCursorPosition(0)
        self.setToolTip(
            "Current database:\n"
            f"{self._database_path}\n\n"
            "Drop a QCoDeS .db file here to load it."
            )

    def text(self):
        return self._database_path

    def dragEnterEvent(self, event):
        if database_path_from_mime_data(event.mimeData()) is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        path = database_path_from_mime_data(event.mimeData())
        if path is None:
            event.ignore()
            return

        event.acceptProposedAction()
        self.databaseDropped.emit(os.path.abspath(path))


class MainWindow(qtw.QMainWindow):
    """
    The Main application which connects/initialises QCoDeS database, displays
    available options plots to open, and opens windows.
    
    This window can be opened by calling qplot.run()
    
    Holds a shallow copy of all other open windows to prevent deletion by 
    python's garbarge collector
    """
    
    def __init__(self):
        startup_start = perf_counter()
        super().__init__()
       
        #vars
        self.config = config() # Connect to config.json in :/users/<user>/.qplot/
        self.windows = [] # prevent auto delete of windows
        self.ds = None
        self.preview_size = self._configured_preview_size()
        self.dataset_holder = {}
        self.monitor = QtCore.QTimer()
        self.threadPool = QtCore.QThreadPool()
        self.threadPool.setMaxThreadCount(self.config.get("runtime_settings.max_threads"))
        self.databaseLoadThreadPool = QtCore.QThreadPool(self)
        self.databaseLoadThreadPool.setMaxThreadCount(1)
        self._database_load_generation = 0
        self._database_load_active = False
        self._database_load_state = None
        self._database_load_worker = None
        self.x = 0
        self.y = 0
        self.localLastFile = None
        
        # Set GUI color and style from user choice in qplot.configuration.themes
        self.setStyleSheet(self.config.theme.main)
        
        #widgets
        self.l = qtw.QVBoxLayout()
        self.l.setContentsMargins(8, 8, 8, 4)
        self.l.setSpacing(6)
        
        #Core initialisation functions
        self.initRefresh()
        self.initMenu()
        self.initFile()
        self.initRunDisplay()
        self.initShortcuts()
        self.startupDatabaseTimer = QtCore.QTimer(self)
        self.startupDatabaseTimer.setSingleShot(True)
        self.startupDatabaseTimer.timeout.connect(self.load_startup_database)
        
        #Final Setup
        w = qtw.QFrame()
        w.setLayout(self.l)
        self.setCentralWidget(w)
       
        # Fetch window size from config.json
        self.resize(*self.config.get("GUI.main_frame_size"))
        self.setWindowTitle("qPlot")
        startup_elapsed = perf_counter() - startup_start
        self.show_status(f"Ready - QPlot opened in {startup_elapsed:.2f} s")
        
        # Get user's window dimensions to control new window position
        self.screenrect = qtw.QApplication.primaryScreen().availableGeometry()
        self.x = self.screenrect.left() 
        self.y = self.screenrect.top()
        
        # Try to bring window to top 
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowStaysOnTopHint)
        self.show() 
        self.setWindowFlags(self.windowFlags() & ~QtCore.Qt.WindowStaysOnTopHint) 
        self.show()
        self.startupDatabaseTimer.start(0)


    def load_startup_database(self):
        """
        Load the last database on startup when it is still available.

        Missing or unset paths are ignored so first-run and moved-file startup
        behaviour stays the same as an empty launch.

        """
        try:
            last_file = self.config.get("file.last_file_path")
        except KeyError:
            return False

        if not last_file:
            return False

        last_file = os.path.abspath(last_file)
        if not os.path.isfile(last_file):
            return False

        return self.load_database_path(last_file)


    def initRefresh(self):
        """
        Initialise the main window refresh.Refresh checks for any new runs 
        added to the dataset.
        
        """
        self.spinBox = qtw.QDoubleSpinBox()
        self.spinBox.setSingleStep(0.1)
        self.spinBox.setDecimals(1)
        self.spinBox.setSuffix(" s")
        self.spinBox.setFixedWidth(84)
        self.spinBox.setAlignment(QtCore.Qt.AlignRight)
        self.spinBox.setToolTip("Refresh interval in seconds")
        self.spinBox.setValue(self.config.get("user_preference.default_refresh_rate"))
    
        # Slot connections
        self.spinBox.valueChanged.connect(self.monitorIntervalChanged)
        self.monitor.timeout.connect(self.refreshMain)
        
        self.autoPlotBox = qtw.QCheckBox()
        self.autoPlotBox.setToolTip("Automatically open plots for newly detected runs")

        self.refreshDatabaseButton = qtw.QToolButton()
        self.refreshDatabaseButton.setObjectName("refreshIconButton")
        self.refreshDatabaseButton.setIcon(
            self.style().standardIcon(qtw.QStyle.SP_BrowserReload)
            )
        self.refreshDatabaseButton.setToolTip("Refresh the database run list (R)")
        self.refreshDatabaseButton.setAccessibleName("Refresh database")
        self.refreshDatabaseButton.setFixedSize(28, 26)
        self.refreshDatabaseButton.clicked.connect(self.refreshMain)

        self.closeAllPlotsButton = qtw.QToolButton()
        self.closeAllPlotsButton.setObjectName("closeAllPlotsButton")
        self.closeAllPlotsButton.setIcon(
            self.style().standardIcon(qtw.QStyle.SP_TitleBarCloseButton)
            )
        self.closeAllPlotsButton.setToolTip("Close all plot windows (Ctrl+Shift+W)")
        self.closeAllPlotsButton.setAccessibleName("Close all plot windows")
        self.closeAllPlotsButton.setFixedSize(28, 26)
        self.closeAllPlotsButton.clicked.connect(self.closeAll)
    
    
    def initMenu(self):
        """
        Produces the menu bar and all menu's contained at the top of the window

        """
        menu = self.menuBar()
        # First dropdown menu
        fileMenu = menu.addMenu("&File") # Not sure why these all have &, but they do
        
        # Load database file
        loadAction = qtw.QAction("&Load Database...", self)
        loadAction.setShortcut("Ctrl+L")
        loadAction.setStatusTip("Load a QCoDeS database")
        loadAction.triggered.connect(self.getfile)
        fileMenu.addAction(loadAction)
        
        self.recentDatabaseMenu = fileMenu.addMenu("Load &Recent Database")
        self.refresh_recent_database_menu()

        open_folder_action = qtw.QAction("Open Database &Folder", self)
        open_folder_action.setShortcut("Ctrl+Shift+D")
        open_folder_action.setStatusTip("Open the folder containing the current database")
        open_folder_action.triggered.connect(self.open_database_location)
        fileMenu.addAction(open_folder_action)
        
        # Force update check on database
        refreshAction = qtw.QAction("&Refresh", self)
        refreshAction.setShortcut("R")
        refreshAction.triggered.connect(self.refreshMain)
        fileMenu.addAction(refreshAction)

        fileMenu.addSeparator()

        self.closeAllPlotsAction = qtw.QAction("Close All &Plot Windows", self)
        self.closeAllPlotsAction.setShortcut("Ctrl+Shift+W")
        self.closeAllPlotsAction.setShortcutContext(QtCore.Qt.WindowShortcut)
        self.closeAllPlotsAction.setStatusTip("Close all open plot windows")
        self.closeAllPlotsAction.triggered.connect(self.closeAll)
        fileMenu.addAction(self.closeAllPlotsAction)

        closeAction = qtw.QAction("&Close Window", self)
        closeAction.setShortcuts(
            standard_key_sequences(QKeySequence.Close, ["Ctrl+W"])
            )
        closeAction.setShortcutContext(QtCore.Qt.WindowShortcut)
        closeAction.setStatusTip("Close the main qPlot window")
        closeAction.triggered.connect(self.close)
        fileMenu.addAction(closeAction)

        quitAction = qtw.QAction("&Quit qPlot", self)
        quitAction.setShortcuts(
            standard_key_sequences(QKeySequence.Quit, ["Ctrl+Q"])
            )
        quitAction.setShortcutContext(QtCore.Qt.WindowShortcut)
        quitAction.setStatusTip("Quit qPlot")
        quitAction.triggered.connect(self.close)
        fileMenu.addAction(quitAction)

        add_standard_window_controls(self)
        
        # Second dropdown menu
        prefMenu = menu.addMenu("&Options")
        
        # Sets default open location for loadACtion
        default_load_picker = qtw.QAction("&Open Location", self)
        default_load_picker.triggered.connect(self.change_default_file)
        prefMenu.addAction(default_load_picker)
        
        # Change app stylesheet/theme
        themeMenu = prefMenu.addMenu("&Theme")
        
        current_theme = self.config.get("user_preference.theme")
        self.themes = []
        # Add all options to menu
        for itr, theme in enumerate(["Light", "Dark", "PyQt"]):
            self.themes.append(qtw.QAction(f'&{theme}', self, checkable=True))
            
            self.themes[itr].triggered.connect(
                lambda _, theme=theme.lower(), action=self.themes[itr]:
                    self.change_theme(theme, action) 
                )
                
            themeMenu.addAction(self.themes[itr])
            if theme.lower() == current_theme:
                self.themes[itr].setChecked(True)

        preview_menu = prefMenu.addMenu("&Preview Size")
        self.previewSizeGroup = qtw.QActionGroup(self)
        self.previewSizeGroup.setExclusive(True)
        self.previewSizeActions = []
        for size in (100, 150, 200, 300, 500):
            action = qtw.QAction(f"{size} px", self, checkable=True)
            action.setData(size)
            action.setChecked(size == self.preview_size)
            action.triggered.connect(
                lambda _, preview_size=size: self.change_preview_size(preview_size)
                )
            self.previewSizeGroup.addAction(action)
            self.previewSizeActions.append(action)
            preview_menu.addAction(action)

        prefMenu.addSeparator()
        add_restore_defaults_option(self, prefMenu)
        prefMenu.addSeparator()
        add_confirmation_options(self, prefMenu)
        add_help_menu(self)

    def initFile(self):
        """
        Display text box for current selected database
        
        """
        self.targetLayout = qtw.QHBoxLayout()
        self.targetLayout.setContentsMargins(8, 2, 8, 2)
        self.targetLayout.setSpacing(6)

        database_label = qtw.QLabel("Database:")
        database_label.setToolTip("Current QCoDeS database")
        self.targetLayout.addWidget(database_label)

        self.fileTextbox = DatabasePathLineEdit()
        self.fileTextbox.setObjectName("databasePathField")
        self.fileTextbox.setReadOnly(True)
        self.fileTextbox.setPlaceholderText("Drop a QCoDeS .db file here or use File -> Load")
        self.fileTextbox.setToolTip(
            "Current database path. Drop a QCoDeS .db file here to load it."
            )
        self.fileTextbox.databaseDropped.connect(self.load_database_path)
        self.targetLayout.addWidget(self.fileTextbox, 1)

        self.copyDatabasePathButton = qtw.QToolButton()
        self.copyDatabasePathButton.setObjectName("databaseIconButton")
        self.copyDatabasePathButton.setIcon(
            self.style().standardIcon(qtw.QStyle.SP_FileDialogDetailedView)
            )
        self.copyDatabasePathButton.setToolTip("Copy the full database path")
        self.copyDatabasePathButton.setAccessibleName("Copy database path")
        self.copyDatabasePathButton.setFixedSize(28, 26)
        self.copyDatabasePathButton.clicked.connect(self.copy_database_path)
        self.targetLayout.addWidget(self.copyDatabasePathButton)

        self.databaseInfoButton = qtw.QToolButton()
        self.databaseInfoButton.setObjectName("databaseIconButton")
        self.databaseInfoButton.setIcon(
            self.style().standardIcon(qtw.QStyle.SP_MessageBoxInformation)
            )
        self.databaseInfoButton.setToolTip("Show database information")
        self.databaseInfoButton.setAccessibleName("Show database information")
        self.databaseInfoButton.setFixedSize(28, 26)
        self.databaseInfoButton.clicked.connect(self.show_database_info)
        self.targetLayout.addWidget(self.databaseInfoButton)

        self.loadDatabaseButton = qtw.QToolButton()
        self.loadDatabaseButton.setObjectName("databaseIconButton")
        self.loadDatabaseButton.setIcon(
            self.style().standardIcon(qtw.QStyle.SP_DialogOpenButton)
            )
        self.loadDatabaseButton.setToolTip("Load a QCoDeS .db database (Ctrl+L)")
        self.loadDatabaseButton.setAccessibleName("Load database")
        self.loadDatabaseButton.setFixedSize(28, 26)
        self.loadDatabaseButton.clicked.connect(self.getfile)
        self.targetLayout.addWidget(self.loadDatabaseButton)

        self.openDatabaseFolderButton = qtw.QToolButton()
        self.openDatabaseFolderButton.setObjectName("databaseIconButton")
        self.openDatabaseFolderButton.setIcon(
            self.style().standardIcon(qtw.QStyle.SP_DirOpenIcon)
            )
        self.openDatabaseFolderButton.setToolTip(
            "Open the folder containing the current database (Ctrl+Shift+D)"
        )
        self.openDatabaseFolderButton.setAccessibleName("Open database folder")
        self.openDatabaseFolderButton.setFixedSize(28, 26)
        self.openDatabaseFolderButton.clicked.connect(self.open_database_location)
        self.targetLayout.addWidget(self.openDatabaseFolderButton)

        self.targetLayout.addStretch()
        self.targetLayout.addSpacing(18)
        self.targetLayout.addWidget(self.closeAllPlotsButton)

        self.databaseLoadFrame = qtw.QFrame()
        self.databaseLoadFrame.setObjectName("databaseLoadFrame")
        database_load_layout = qtw.QHBoxLayout(self.databaseLoadFrame)
        database_load_layout.setContentsMargins(8, 0, 8, 2)
        database_load_layout.setSpacing(6)

        self.databaseLoadProgress = qtw.QProgressBar()
        self.databaseLoadProgress.setObjectName("databaseLoadProgress")
        self.databaseLoadProgress.setRange(0, 0)
        self.databaseLoadProgress.setTextVisible(False)
        self.databaseLoadProgress.setFixedWidth(120)
        self.databaseLoadProgress.setMaximumHeight(16)
        self.databaseLoadProgress.setAccessibleName("Database load progress")
        database_load_layout.addWidget(self.databaseLoadProgress)

        self.databaseLoadLabel = qtw.QLabel("")
        self.databaseLoadLabel.setObjectName("databaseLoadLabel")
        self.databaseLoadLabel.setSizePolicy(
            qtw.QSizePolicy.Expanding,
            qtw.QSizePolicy.Preferred,
            )
        database_load_layout.addWidget(self.databaseLoadLabel, 1)

        self.databaseLoadCancelButton = qtw.QToolButton()
        self.databaseLoadCancelButton.setObjectName("databaseIconButton")
        self.databaseLoadCancelButton.setIcon(
            self.style().standardIcon(qtw.QStyle.SP_DialogCancelButton)
            )
        self.databaseLoadCancelButton.setText("Cancel")
        self.databaseLoadCancelButton.setToolButtonStyle(
            QtCore.Qt.ToolButtonTextBesideIcon
            )
        self.databaseLoadCancelButton.setToolTip("Cancel the current database load")
        self.databaseLoadCancelButton.setAccessibleName("Cancel database load")
        self.databaseLoadCancelButton.setFixedSize(78, 24)
        self.databaseLoadCancelButton.clicked.connect(self.cancel_database_load)
        database_load_layout.addWidget(self.databaseLoadCancelButton)
        self.databaseLoadFrame.setVisible(False)
        
        if os.path.isfile(get_DB_location()):
            self.fileTextbox.setText(str(get_DB_location()))
        
        
    def initRunDisplay(self):
        sublayout = qtw.QHBoxLayout()
        sublayout.setContentsMargins(8, 0, 8, 2)
        sublayout.setSpacing(6)

        sublayout.addWidget(qtw.QLabel("ID:"))
        
        self.selected_run_id = None
        
        # Box for User to enter specific run_id
        self.run_idBox = qtw.QLineEdit()
        self.run_idBox.setMaximumWidth(58)
        self.run_idBox.setFixedWidth(58)
        # Only allow int in box between 1 and 9999999
        self.run_idBox.setValidator(QIntValidator())
        self.run_idBox.setPlaceholderText("ID")
        self.run_idBox.setToolTip("Run ID to plot")
        self.run_idBox.textEdited.connect(self.update_run_id)
        self.run_idBox.editingFinished.connect(self.sync_run_id_selection)
        self.run_idBox.returnPressed.connect(self.openRun)
        sublayout.addWidget(self.run_idBox)

        sublayout.addWidget(qtw.QLabel("Measurement:"))

        self.measurementBox = qtw.QLineEdit()
        self.measurementBox.setMaximumWidth(46)
        self.measurementBox.setFixedWidth(46)
        self.measurementBox.setText("*")
        self.measurementBox.setToolTip("Measurement to plot; * to plot all")
        self.measurementBox.returnPressed.connect(self.openRun)
        sublayout.addWidget(self.measurementBox)

        self.plotRunButton = qtw.QToolButton()
        self.plotRunButton.setObjectName("plotIconButton")
        self.plotRunButton.setIcon(
            self.style().standardIcon(qtw.QStyle.SP_MediaPlay)
            )
        self.plotRunButton.setToolTip("Plot (Ctrl+Return)")
        self.plotRunButton.setAccessibleName("Plot measurement")
        self.plotRunButton.setFixedSize(28, 26)
        self.plotRunButton.clicked.connect(self.openRun)
        sublayout.addWidget(self.plotRunButton)

        self.exportCsvButton = qtw.QToolButton()
        self.exportCsvButton.setObjectName("exportIconButton")
        self.exportCsvButton.setIcon(
            self.style().standardIcon(qtw.QStyle.SP_DialogSaveButton)
            )
        self.exportCsvButton.setToolTip("Export CSV")
        self.exportCsvButton.setAccessibleName("Export measurement to CSV")
        self.exportCsvButton.setFixedSize(28, 26)
        self.exportCsvButton.clicked.connect(self.exportRunCsv)
        sublayout.addWidget(self.exportCsvButton)

        sublayout.addStretch()

        sublayout.addWidget(qtw.QLabel("Auto-plot"))
        sublayout.addWidget(self.autoPlotBox)

        sublayout.addSpacing(12)
        sublayout.addWidget(qtw.QLabel("Refresh:"))
        sublayout.addWidget(self.spinBox)
        sublayout.addWidget(self.refreshDatabaseButton)

        self.l.addLayout(self.targetLayout)
        self.l.addWidget(self.databaseLoadFrame)
        self.l.addLayout(sublayout)
        
        # Long QTreeWidget/list to display all runs with small detail
        self.RunList = RunList()
        self.RunList.selected.connect(self.updateSelected)
        self.RunList.plot.connect(self.openPlot)
        self.RunList.previewPlotRequested.connect(self.open_run_preview_plot)
        self.RunList.previewExportRequested.connect(self.export_run_preview_csv)
        
        # Show all available info on the selected item in self.RunList
        self.infoBox = moreInfo(preview_size=self.preview_size)
        self.infoBox.preview.plotRequested.connect(self.open_preview_plot)
        self.infoBox.preview.exportRequested.connect(self.export_preview_csv)
        self.infoBox.preview.previewsReady.connect(self.RunList.set_run_previews)
        if self.fileTextbox.text() and self.RunList.topLevelItemCount():
            self.infoBox.preview.set_database_runs(
                self.fileTextbox.text(),
                self.RunList.all_run_metadata()
                )

        self.runInfoSplitter = qtw.QSplitter(QtCore.Qt.Vertical)
        self.runInfoSplitter.setHandleWidth(8)
        self.runInfoSplitter.setChildrenCollapsible(True)
        self.runInfoSplitter.setOpaqueResize(True)
        self.runInfoSplitter.addWidget(self.RunList)
        self.runInfoSplitter.addWidget(self.infoBox)
        self.runInfoSplitter.setCollapsible(0, False)
        self.runInfoSplitter.setCollapsible(1, True)
        self.runInfoSplitter.setStretchFactor(0, 3)
        self.runInfoSplitter.setStretchFactor(1, 2)
        self.runInfoSplitter.setSizes([380, self._details_pane_height()])
        self.runInfoSplitter.handle(1).setToolTip(
            "Drag to resize the run list and details panes"
            )
        self.l.addWidget(self.runInfoSplitter, 1)


    def initShortcuts(self):
        """
        Register keyboard shortcuts for context menu and common run actions.

        """
        plot_entered = qtw.QAction("Plot Entered Run and Measurement", self)
        plot_entered.setShortcut("Ctrl+Return")
        plot_entered.setShortcutContext(QtCore.Qt.WindowShortcut)
        plot_entered.setStatusTip("Plot the run and measurement entered above")
        plot_entered.triggered.connect(lambda _: self.plotRunButton.click())
        self.addAction(plot_entered)

        plot_selected_all = qtw.QAction("Plot All Measurements in Selected Run", self)
        plot_selected_all.setShortcut("Ctrl+Shift+Return")
        plot_selected_all.setShortcutContext(QtCore.Qt.WindowShortcut)
        plot_selected_all.setStatusTip("Plot all measurements in the selected run")
        plot_selected_all.triggered.connect(self.open_selected_run_all)
        self.addAction(plot_selected_all)

        self.open_param_actions = []
        for itr in range(9):
            action = qtw.QAction(f"Plot Measurement {itr + 1} in Selected Run", self)
            action.setShortcut(f"Ctrl+{itr + 1}")
            action.setShortcutContext(QtCore.Qt.WindowShortcut)
            action.setStatusTip(f"Plot measurement {itr + 1} in the selected run")
            action.triggered.connect(lambda _, index=itr: self.open_param_by_index(index))
            self.addAction(action)
            self.open_param_actions.append(action)
        
###############################################################################
#Open/Close events

    @QtCore.pyqtSlot(bool)
    def closeEvent(self, event):
        """
        Event handler for closing Main Window.

        Also handles some closing admin        

        """
        # Confirm exit
        if self.config.get("user_preference.confirm_close"):
            reply = qtw.QMessageBox.question(self, "Confirm Exit", "Are you sure you want to exit?",
                                         qtw.QMessageBox.Yes | qtw.QMessageBox.No)
            if reply == qtw.QMessageBox.Yes:
                event.accept()
            else:
                event.ignore()
                return

        self.startupDatabaseTimer.stop()
        worker = getattr(self, "_database_load_worker", None)
        if worker is not None:
            worker.cancel()
        self._database_load_generation += 1
        self._database_load_active = False
        self._database_load_state = None
        self._database_load_worker = None
        self.monitor.stop()
        qtw.QApplication.closeAllWindows()
    
   
    @QtCore.pyqtSlot()
    def closeAll(self):
        """
        Event handler for close all menu button.
        Closes all windows other than the main window.

        """
        self.close_plot_windows(confirm=True, status=True)


    def close_plot_windows(self, confirm=True, status=True):
        """
        Closes all plot windows, optionally asking for confirmation.

        """
        plot_windows = self.windows.copy()
        if not plot_windows:
            if status:
                self.show_status("No plot windows to close.", 3000)
            return True

        if confirm and close_all_warning_enabled(self.config):
            count = len(plot_windows)
            noun = "window" if count == 1 else "windows"
            reply = qtw.QMessageBox.question(
                self,
                "Close All Plot Windows",
                f"Close {count} plot {noun}?",
                qtw.QMessageBox.Yes | qtw.QMessageBox.No,
                qtw.QMessageBox.No,
                )
            if reply != qtw.QMessageBox.Yes:
                if status:
                    self.show_status("Close all plot windows cancelled.", 3000)
                return False

        if status:
            self.show_status("Closing plot windows...", 3000)
        for win in plot_windows:
            win.close()
        return True
        
        
    @QtCore.pyqtSlot(object)
    def onClose(self, win):
        """
        Event handler for closing a Plot window

        Parameters
        ----------
        win : qplot.windows.plotWin.plotWidget
            The window that is closing.


        """
        self.windows.remove(win)
        self.remove_ds_at(win._guid)
        self.post_admin() # Update other plot windows
        self.show_status(f"Closed {win.label}", 3000)
        del win
    
    
    @QtCore.pyqtSlot(object, str, tuple)
    def openWin(self, widget, guid_or_ds, *args, show=True, **kargs):
        """
        Handles opening Plot window, widget.
        Passes all attributes to widget(). Also passes other critical objects.

        Connected to plot2d for openning it's secondary plots.        

        Parameters
        ----------
        widget : qplot.windows.plotWin.plotWidget
            Takes window class to be openned.
        *args 
            Passed to widget.__init__().
        show : bool, optional
            Whether the windows is dsiplayed to the user or held as a 
            background process. Is also passed to widget.__init__(). 
            The default is True.
        **kargs
            Passed to widget.__init__().


        """
        # Convert args to usable form if passed as iterable
        if len(args) == 1 and (isinstance(args[0], tuple) or isinstance(args[0], list)):
            args = tuple(args[0])
        
        # Find if guid or ds was passed
        if isinstance(guid_or_ds, str):
            ds = None
            guid = guid_or_ds
        else: 
            ds = guid_or_ds
            guid = ds.guid
            
        # add dataset to store
        self.add_ds_at(guid, ds=ds)
        
        win = widget(
            guid,
            *args, 
            self.config, 
            self.threadPool,
            self.dataset_holder,
            show=show, 
            **kargs
            )
        
        # Store copy in Main Window to prevent python auto delete
        self.windows.append(win)
        
        # Slot connectons
        win.closed.connect(self.onClose)
        win.make_ds.connect(self.add_ds_at)
        win.previewTraceDropRequested.connect(self.add_dropped_preview_to_plot)
        if win.__class__.__name__ == "plot1d":
            win.get_mergables.connect(lambda: self.get_1d_wins(win))
            win.remove_dataset.connect(self.remove_ds_at)
            
        elif win.__class__.__name__ == "plot2d":
            win.open_subplot.connect(self.openWin)
            
        elif win.__class__.__name__ == "sweeper":
            # find win's parent
            for item in self.windows:
                if item.ds == win.ds and item.param == win.param and isinstance(item, plot2d):
                    win.sweep_moved.connect(item.update_sweep_line) # Update event
                    win.remove_sweep.connect(item.remove_sweep) # Close event
                    item.sweep_moved.connect(win.update_sweep_line)
                    break
            
        else:
            raise TypeError(f"Unknown window of type: {win.__class__.__name__}")

        # Place window on screen so it doesnt overlap with last openned
        if show:    
            # match style/theme to main window
            win.update_theme(self.config)
            
            win.move(self.x, self.y)
            win.show()
        
            #set next position
            tolerance = 30
            self.x += win.width
            if self.x + win.width - tolerance > self.screenrect.right():
                self.x = self.screenrect.left()
                self.y += win.height
                
                if self.y + win.height - tolerance > self.screenrect.bottom():
                    self.y = self.screenrect.top()
        
###############################################################################
#Slots
    
    @QtCore.pyqtSlot(float)
    def monitorIntervalChanged(self, interval):
        """
        Updates the refresh interval for checking for new runs in database

        Parameters
        ----------
        interval : flaot
            Refresh interval to be set, in seconds.

        """
        self._save_refresh_interval(interval)
        self._apply_refresh_interval(interval)


    def _apply_refresh_interval(self, interval):
        """
        Applies the current refresh interval to the main-window timer.

        """
        self.monitor.stop()
        if interval > 0:
            self.monitor.start(int(interval * 1000)) #convert to seconds


    def _save_refresh_interval(self, interval):
        """
        Persists the main refresh interval as the user's default.

        """
        interval = float(interval)
        try:
            current_interval = float(self.config.get("user_preference.default_refresh_rate"))
        except (KeyError, TypeError, ValueError):
            current_interval = None

        if current_interval != interval:
            self.config.update("user_preference.default_refresh_rate", interval)


    @QtCore.pyqtSlot()
    def open_database_location(self):
        """
        Opens the current database folder in the system file browser.

        """
        database_path = self.fileTextbox.text()
        if not database_path:
            self.show_status("No database is loaded.", 5000)
            return

        folder = os.path.dirname(database_path)
        if not os.path.isdir(folder):
            self.show_error(
                "Database Location Not Found",
                "The current database folder could not be found.",
                database_path
                )
            return

        opened = QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(folder))
        if opened:
            self.show_status(f"Opened database folder: {folder}", 5000)
        else:
            self.show_error(
                "Open Folder Failed",
                "The database folder could not be opened.",
                folder
                )


    @QtCore.pyqtSlot()
    def copy_database_path(self):
        """
        Copies the full current database path to the clipboard.

        """
        database_path = self.fileTextbox.text()
        if not database_path:
            self.show_status("No database path to copy.", 3000)
            return

        qtw.QApplication.clipboard().setText(database_path)
        self.show_status("Copied database path.", 3000)


    def close_database(self, status=True):
        """
        Clears the current database from the main window state.

        """
        worker = getattr(self, "_database_load_worker", None)
        if worker is not None:
            worker.cancel()

        self._database_load_generation = getattr(self, "_database_load_generation", 0) + 1
        self._database_load_active = False
        self._database_load_state = None
        self._database_load_worker = None
        if hasattr(self, "_set_database_load_controls_enabled"):
            self._set_database_load_controls_enabled(True)
        if hasattr(self, "_hide_database_load_panel"):
            self._hide_database_load_panel()

        self.monitor.stop()
        self.fileTextbox.setText("")
        self.run_idBox.setText("")
        self.measurementBox.setText("*")
        self.selected_run_id = None
        self.ds = None
        self.localLastFile = None

        for holder in self.dataset_holder.values():
            del_timer = holder.get("del_timer")
            if del_timer is not None:
                del_timer.stop()
        self.dataset_holder.clear()

        self.RunList.blockSignals(True)
        self.RunList.clearSelection()
        self.RunList.clear()
        self.RunList.watching = []
        self.RunList.maxTime = 0
        self.RunList.blockSignals(False)
        self.RunList.scrollToTop()

        self.infoBox.clear()
        self.infoBox.preview.set_database_runs("", {})
        self.infoBox.scrollToTop()

        if status:
            self.show_status("Database closed.", 3000)


    @QtCore.pyqtSlot()
    def show_database_info(self):
        """
        Shows a diagnostic report for the current database.

        """
        database_path = self.fileTextbox.text()
        if not database_path:
            self.show_status("No database is loaded.", 5000)
            return

        try:
            report = database_info_report(database_path)
        except Exception as err:
            log_exception("Database information failed", err, __name__)
            self.show_error(
                "Database Information Failed",
                "Could not read database information.",
                str(err)
                )
            return

        box = qtw.QMessageBox(qtw.QMessageBox.Information, "Database Information", report, parent=self)
        copy_button = box.addButton("Copy", qtw.QMessageBox.ActionRole)
        box.addButton(qtw.QMessageBox.Close)
        box.exec_()

        if box.clickedButton() == copy_button:
            qtw.QApplication.clipboard().setText(report)
            self.show_status("Copied database information.", 3000)


    @QtCore.pyqtSlot()
    def refreshMain(self):
        """
        On self.monitor timer or force refresh, check for new runs in Database        

        """
        if not self.fileTextbox.text(): # If no selected database
            self.show_status("Load a database before refreshing.", 5000)
            return

        self.show_status("Checking for new runs...", 0)

        try:
            # Find any runs after the last highest time
            newRuns = find_new_runs(self.RunList.maxTime)

            # Check runs markes as "Ongoing" to see if they have finished
            self.RunList.checkWatching()
        except Exception as err:
            log_exception("Main-window refresh failed", err, __name__)
            self.show_error("Refresh Failed", "Could not refresh the run list.", str(err))
            return
        
        if not newRuns: # Nothing found
            self.show_status("No new runs found.", 3000)
            return
        
        # Convert to numpy array to handle Nan/null values which occur in rare cases
        self.RunList.maxTime = max(
            np.array([subDict["run_timestamp"] for subDict in newRuns.values()], dtype=float),
            default=0
            )
        self.RunList.addRuns(newRuns)
        self.infoBox.preview.add_runs(newRuns)
        count = len(newRuns)
        noun = "run" if count == 1 else "runs"
        self.show_status(f"Found {count} new {noun}.", 5000)


        if self.autoPlotBox.isChecked():
            for run in newRuns.values():
                self.openPlot(run["guid"])


    @QtCore.pyqtSlot()
    def getfile(self):
        """
        Handles event for load action in file menu to load new database.
        Opens file directory dialog for use to select file and loads that 
        database

        """
        filename = qtw.QFileDialog.getOpenFileName(
            self, 
            'Open file', # Dialog button display
            self.database_open_directory(), # Default look location
            "Data Base File (*.db)" # What to show
            )[0] # Returns array even if only 1 item is selected
        
        # Confirm user did not cancel
        if os.path.isfile(filename):
            self.load_database_path(filename)
        else:
            self.show_status("Database load cancelled.", 3000)


    def database_open_directory(self):
        """
        Returns the directory the database-open dialog should start in.

        """
        current_database = self.fileTextbox.text()
        if current_database:
            current_directory = os.path.dirname(os.path.abspath(current_database))
            if os.path.isdir(current_directory):
                return current_directory

        try:
            default_load_path = self.config.get("file.default_load_path")
        except KeyError:
            default_load_path = ""

        if os.path.isdir(default_load_path):
            return default_load_path

        return os.getcwd()
            
    
    @QtCore.pyqtSlot()
    def change_default_file(self):
        """
        Event handle for for Open Location action in options menu.
        Changes default open location in config.json for usage in
        self.getfile()


        """
        # Open at last default load location
        if os.path.isdir(self.config.get("file.default_load_path")):
            openDir = self.config.get("file.default_load_path")
        else:
            openDir = os.getcwd()
        
        foldername = qtw.QFileDialog.getExistingDirectory(
            self, 
            'Select Folder', # Dialog button display
            openDir, # Default look location
            )
        
        # Confirm user did not cancel
        if os.path.isdir(foldername):
            self.config.update("file.default_load_path", foldername)
            self.show_status(f"Default load folder set to {foldername}", 5000)
        else:
            self.show_status("Default load folder unchanged.", 3000)
              
            
    @QtCore.pyqtSlot(str)
    def load_database_path(self, filename):
        """
        Load a database path chosen from the file dialog or dropped by the user.

        """
        load_started_at = perf_counter()
        log_event("Database load requested: %s", filename, logger_name=__name__)

        if not os.path.isfile(filename):
            self.show_error(
                "Database Load Failed",
                "The selected database file could not be found.",
                str(filename)
                )
            return False

        abspath = os.path.abspath(filename)
        if not abspath.lower().endswith(".db"):
            self.show_error(
                "Database Load Failed",
                "qPlot can only load QCoDeS .db database files.",
                abspath
                )
            return False

        return self.load_file(abspath, load_started_at)


    def recent_database_paths(self):
        """
        Returns recent database paths, newest first.

        """
        try:
            paths = list(self.config.get("file.recent_file_paths"))
        except KeyError:
            paths = []

        try:
            last_file = self.config.get("file.last_file_path")
        except KeyError:
            last_file = ""

        if last_file:
            paths.insert(0, last_file)

        deduped = []
        seen = set()
        for path in paths:
            abspath = os.path.abspath(path)
            if abspath in seen:
                continue
            seen.add(abspath)
            deduped.append(abspath)

        return deduped[:10]


    def remember_recent_database(self, filename):
        """
        Stores a database path in the recent database list.

        """
        abspath = os.path.abspath(filename)
        paths = [path for path in self.recent_database_paths() if path != abspath]
        paths.insert(0, abspath)
        paths = paths[:10]

        try:
            current_paths = list(self.config.get("file.recent_file_paths"))
        except KeyError:
            current_paths = []

        if current_paths == paths:
            return

        self.config.config.setdefault("file", {})["recent_file_paths"] = paths
        self.config.save_config(self.config.default_file)
        self.refresh_recent_database_menu()


    def remember_loaded_database(self, filename):
        """
        Persists the successfully loaded database path.

        """
        abspath = os.path.abspath(filename)
        try:
            current_last_file = self.config.get("file.last_file_path")
        except KeyError:
            current_last_file = None

        try:
            if current_last_file != abspath:
                self.config.update("file.last_file_path", abspath)
            self.remember_recent_database(abspath)
        except Exception as err:
            log_exception("Remember database path failed", err, __name__)


    def refresh_recent_database_menu(self):
        """
        Rebuilds the File -> Load Recent Database menu.

        """
        if not hasattr(self, "recentDatabaseMenu"):
            return

        self.recentDatabaseMenu.clear()
        paths = self.recent_database_paths()
        self.recentDatabaseMenu.setEnabled(bool(paths))

        if not paths:
            empty_action = qtw.QAction("No Recent Databases", self)
            empty_action.setEnabled(False)
            self.recentDatabaseMenu.addAction(empty_action)
            return

        for index, path in enumerate(paths, start=1):
            label = f"{index}. {os.path.basename(path) or path}"
            action = qtw.QAction(label, self)
            action.setToolTip(path)
            action.setStatusTip(path)
            action.setEnabled(os.path.isfile(path))
            action.triggered.connect(lambda _, filename=path: self.load_database_path(filename))
            self.recentDatabaseMenu.addAction(action)
    
    
    @QtCore.pyqtSlot(str)
    def updateSelected(self, guid):
        """
        Event Handler for clicking on RunList.
        Loads the selected run into memory using the row's guid.
        It then displays metadata and other available info into the InfoList.

        Parameters
        ----------
        guid : str
            The unique id to load the dataset from.

        """
        self.show_status("Loading selected run...", 0)
        try:
            # Load from store if possible
            if self.dataset_holder.get(guid, 0) == 0:
                self.ds = load_by_guid(guid)
            else:
                self.ds = self.dataset_holder[guid]["dataset"]
        except Exception as err:
            log_exception("Selected run load failed", err, __name__)
            self.show_error("Run Load Failed", f"Could not load run with GUID {guid}.", str(err))
            return
        
        self.selected_run_id = self.ds.run_id
        self.run_idBox.blockSignals(True)
        self.run_idBox.setText(str(self.ds.run_id))
        self.run_idBox.blockSignals(False)
        
        # Get metadata (snapshot) from dataset
        if hasattr(self.ds, "snapshot"):
            snap = self.ds.snapshot
        else:
            snap = None
        
        paramspec = self.ds.get_parameters()
        # Create dict to convert into a QTreeWidget for display
        structure = {"Data points" : self.ds.number_of_results}
        # Unpack parameter metadata
        for param in paramspec:
            if len(param.depends_on) > 0:
                structure[param.name] = {"unit" : param.unit,
                                         "label" : param.label,
                                         "axes" : list(param.depends_on_)
                                         }
            else:
                structure[param.name] = {"unit" : param.unit,
                                         "label" : param.label
                                         }
        info = {"Data Structure" : structure,
                "MetaData" : self.ds.metadata,
                "Snapshot" : snap
                }
        # Update infoBox
        self.infoBox.setInfo(info, self.ds)
        self.show_status(
            f"Selected run {self.ds.run_id} with {self.ds.number_of_results:,} points.",
            5000
            )
        
        
    @QtCore.pyqtSlot()
    def openRun(self):
        """
        Event handler for the plot button.
        Plots the requested measurement for the requested run.

        Required in specific cases for error catching.

        """
        ds = self._dataset_for_plot_target()
        if ds is None:
            return

        params = self._selected_measurement_params(ds)
        if params is None:
            return

        self.ds = ds
        self.openPlot(params=params)


    @QtCore.pyqtSlot()
    def open_selected_run_all(self):
        """
        Opens every plottable measurement in the currently selected table row.

        """
        if self.ds is None:
            self.show_status("Select a run before plotting all measurements.", 5000)
            return

        self.openPlot()


    @QtCore.pyqtSlot()
    def exportRunCsv(self):
        """
        Exports the requested run and measurement data to a CSV file.

        """
        ds = self._dataset_for_plot_target()
        if ds is None:
            return

        params = self._selected_measurement_params(ds)
        if params is None:
            return

        self._export_measurement_csv(ds, params)


    @QtCore.pyqtSlot(str)
    def export_preview_csv(self, parameter_name):
        """
        Exports the measurement represented by a selected-run preview image.

        """
        if not self.ds:
            self.show_status("Select a run before exporting a preview.", 5000)
            return

        self._export_preview_csv(self.ds, parameter_name)


    @QtCore.pyqtSlot(str, str)
    def export_run_preview_csv(self, guid, parameter_name):
        """
        Exports the measurement represented by a run-table preview image.

        """
        if not guid:
            self.show_status("Select a run before exporting a preview.", 5000)
            return

        try:
            ds = self._dataset_for_guid(guid)
        except Exception as err:
            log_exception("Preview CSV run load failed", err, __name__)
            self.show_error("Run Load Failed", f"Could not load run with GUID {guid}.", str(err))
            return

        self._export_preview_csv(ds, parameter_name)


    def _export_preview_csv(self, dataset, parameter_name):
        param = self._measurement_param_by_name(dataset, parameter_name)
        if param is None:
            self.show_status(f"No preview export found for {parameter_name}.", 5000)
            return

        self._export_measurement_csv(dataset, [param])


    def _export_measurement_csv(self, ds, params):
        if not params:
            self.show_status("No plottable measurements to export for this run.", 5000)
            return

        default_name = self._default_export_filename(ds, params)
        filename = qtw.QFileDialog.getSaveFileName(
            self,
            "Export CSV",
            default_name,
            "CSV files (*.csv)"
            )[0]
        if not filename:
            self.show_status("CSV export cancelled.", 3000)
            return
        if not filename.lower().endswith(".csv"):
            filename = f"{filename}.csv"

        try:
            frame = self._measurement_dataframe(ds, params)
            frame.to_csv(filename, index=False)
        except Exception as err:
            log_exception("CSV export failed", err, __name__)
            self.show_error(
                "CSV Export Failed",
                "Could not export the selected measurement data.",
                str(err)
                )
            return

        self.show_status(f"Exported CSV: {filename}", 5000)
    
    
    @QtCore.pyqtSlot(str)
    def openPlot(self, 
                 guid : str=None, 
                 params : list=None, 
                 show : bool=True
                 ):
        """
        Event handler for:
            Plot button,
            RunList double click
            RunList context menu actions
        Takes the currently selected run and passes to Open Win to produce 
        new Plot windows.
        
        Parameters
        ----------
        guid : str, optional
            If given, overrides the currently selected dataset and loads a new
            one with the given unique run code, GUID.
        params : list[qcodes.dataset.descriptions.param_spec.ParamSpec], optional
            The parameters in the dataset to be openned. Primarally used in
            RunList context menu actions.
            The default is None, which opens all dependant parameters in the 
            dataset.
        show : bool
            Whether to display the window to the user. The default is True.
        
        """
        if not self.ds and not guid:
            self.show_status("Select a run before opening plots.", 5000)
            return

        self.show_status("Opening plots...", 0)

        try:
            # Get dataset with GUID or default
            if not self.ds or (guid and self.ds.guid != guid):
                # Load from store if possible
                if self.dataset_holder.get(guid, 0) == 0:
                    ds = load_by_guid(guid)
                else:
                    ds = self.dataset_holder[guid]["dataset"]
            else:
                ds = self.ds
        except Exception as err:
            log_exception("Plot run load failed", err, __name__)
            self.show_error("Run Load Failed", "Could not load the selected run.", str(err))
            return
            
        if not params:
            params = ds.get_parameters()
           
        opened = 0
        skipped = 0
        try:
            for param in params:
                if param.depends_on != "":
                    depends_on = param.depends_on_
                    skip = False
                    
                    if len(depends_on) == 1:
                        for win in self.windows:
                            # Check if window is open
                            if win.ds == ds and win.param == param and isinstance(win, plot1d):
                                skipped += 1
                                skip = True
                                break
                        if skip: continue
                        
                        self.openWin(
                            plot1d, 
                            ds, 
                            param, 
                            refrate = self.spinBox.value(),
                            show = show
                            )
                        opened += 1
                        
                    else:
                        for win in self.windows:
                            # Check if window is open
                            if (win.ds == ds and win.param == param and isinstance(win, plot2d)):
                                skipped += 1
                                skip = True
                                break
                        if skip: continue
                            
                        self.openWin(
                            plot2d, 
                            ds, 
                            param, 
                            refrate = self.spinBox.value(),
                            show = show
                            )
                        opened += 1
                        
            self.post_admin() # Updates currently open windows

            if opened:
                noun = "plot" if opened == 1 else "plots"
                self.show_status(f"Opened {opened} {noun}.", 5000)
            elif skipped:
                self.show_status("Selected plot windows are already open.", 5000)
            else:
                self.show_status("No plottable parameters found for this run.", 5000)
            
        except Exception as err:
            # atempt to prevent SQL lock outs
            try:
                ds.conn.close()
            except Exception:
                pass
            log_exception("Plot open failed", err, __name__)
            self.show_error("Plot Open Failed", "Could not open plot windows.", str(err))


    def open_param_by_index(self, index : int):
        """
        Open the indexed dependent parameter for the selected run.

        """
        if not self.ds:
            self.show_status("Select a run before opening a parameter.", 5000)
            return

        params = [param for param in self.ds.get_parameters() if param.depends_on != ""]
        if index >= len(params):
            self.show_status(f"Run has no parameter {index + 1}.", 5000)
            return

        self.openPlot(params=[params[index]])


    @QtCore.pyqtSlot(str)
    def open_preview_plot(self, parameter_name):
        """
        Open the plot represented by a double-clicked preview image.

        """
        if not self.ds:
            self.show_status("Select a run before opening a preview plot.", 5000)
            return

        for param in self.ds.get_parameters():
            if param.name == parameter_name and param.depends_on != "":
                self.openPlot(params=[param])
                return

        self.show_status(f"No preview plot found for {parameter_name}.", 5000)


    @QtCore.pyqtSlot(str, str)
    def open_run_preview_plot(self, guid, parameter_name):
        """
        Open the plot represented by a double-clicked run-table preview image.

        """
        if not guid:
            self.show_status("Select a run before opening a preview plot.", 5000)
            return

        if not self.ds or self.ds.guid != guid:
            try:
                if self.dataset_holder.get(guid, 0) == 0:
                    self.ds = load_by_guid(guid)
                else:
                    self.ds = self.dataset_holder[guid]["dataset"]
            except Exception as err:
                log_exception("Preview plot run load failed", err, __name__)
                self.show_error("Run Load Failed", f"Could not load run with GUID {guid}.", str(err))
                return

        self.open_preview_plot(parameter_name)


    @QtCore.pyqtSlot(object, str, str)
    def add_dropped_preview_to_plot(self, target_win, guid, parameter_name):
        """
        Add a run-table preview trace to the plot it was dropped onto.

        """
        self.add_trace_to_plot(target_win, guid, parameter_name)


    def add_trace_to_plot(self, target_win, source_guid, parameter_name, param=None):
        """
        Adds a plottable 1D parameter to an existing compatible 1D plot.

        This is the shared implementation for the run-table context menu and
        run-table preview drag/drop.

        """
        if target_win is None or not hasattr(target_win, "option_boxes"):
            self.show_status("Drop traces onto a compatible line plot.", 5000)
            return False

        if param is None:
            try:
                param = self._parameter_from_guid(source_guid, parameter_name)
            except Exception as err:
                log_exception("Trace source run load failed", err, __name__)
                self.show_error(
                    "Run Load Failed",
                    f"Could not load run with GUID {source_guid}.",
                    str(err)
                    )
                return False

        if param is None or not getattr(param, "depends_on", ""):
            self.show_status(f"No preview plot found for {parameter_name}.", 5000)
            return False

        if len(getattr(param, "depends_on_", ())) != 1:
            self.show_status("Only 1D measurements can be added as traces.", 5000)
            return False

        if tuple(param.depends_on_) != tuple(target_win.param.depends_on_):
            self.show_status(
                f"Cannot add {parameter_name}; the plot axes do not match.",
                5000
                )
            return False

        from_win = self._plot_window_for_param(source_guid, param)
        if from_win == target_win:
            self.show_status(f"Skipped {target_win.label}; source and target are the same.", 5000)
            return False

        if from_win is None:
            from_win = self._open_hidden_trace_window(source_guid, param, target_win)
            if from_win is None:
                return False

        self.get_1d_wins(target_win)
        if target_win.option_boxes[-1].isEnabled():
            box = target_win.option_boxes[-1]
        else:
            target_win.add_option_box()
            box = target_win.option_boxes[-1]

        index = box.option_box.findText(from_win.label)
        if index < 0:
            self.show_status(
                f"Cannot add {parameter_name}; it is already shown or incompatible.",
                5000
                )
            if not from_win.visible:
                from_win.close()
            return False

        box.option_box.setCurrentIndex(index)
        from_win.close()
        return True


    def _parameter_from_guid(self, guid, parameter_name):
        ds = self._dataset_for_guid(guid)
        return self._measurement_param_by_name(ds, parameter_name)


    def _measurement_param_by_name(self, dataset, parameter_name):
        for param in dataset.get_parameters():
            if param.name == parameter_name:
                return param
        return None


    def _dataset_for_guid(self, guid):
        if self.dataset_holder.get(guid, 0) != 0:
            return self.dataset_holder[guid]["dataset"]
        return load_by_guid(guid)


    def _plot_window_for_param(self, guid, param):
        for win in self.windows:
            try:
                if win.ds.guid == guid and win.param.name == param.name:
                    return win
            except AttributeError:
                continue
        return None


    def _open_hidden_trace_window(self, source_guid, param, target_win):
        before = set(id(win) for win in self.windows)
        self.openPlot(guid=source_guid, params=[param], show=False)

        for win in reversed(self.windows):
            if id(win) in before:
                continue
            try:
                if win.ds.guid == source_guid and win.param.name == param.name:
                    if win.ds.running and not target_win.monitor.isActive():
                        target_win.monitorIntervalChanged(target_win.spinBox.value())
                        target_win.toolbarRef.show()
                    return win
            except AttributeError:
                continue

        self.show_status(f"Could not prepare {param.name} for adding to the plot.", 5000)
        return None


    def _selected_measurement_params(self, dataset):
        """
        Returns the measurement parameters requested by the Measurement field.

        """
        params = [param for param in dataset.get_parameters() if param.depends_on != ""]
        measurement = self.measurementBox.text().strip()

        if measurement in ("", "*"):
            return params

        try:
            index = int(measurement)
        except ValueError:
            self.show_status("Measurement must be a number or *.", 5000)
            return None

        if index < 1 or index > len(params):
            self.show_status(
                f"Run {dataset.run_id} has no measurement {index}.",
                5000
                )
            return None

        return [params[index - 1]]


    def _dataset_for_plot_target(self):
        """
        Loads the dataset requested by the Run field.

        """
        if not self.fileTextbox.text():
            self.show_status("Load a database before plotting or exporting.", 5000)
            return None

        if self.selected_run_id is None:
            self.show_status("Enter a Run ID before plotting or exporting.", 5000)
            return None

        try:
            return load_by_id(self.selected_run_id)
        except Exception as error:
            log_exception("Run ID load failed", error, __name__)
            self.show_error(
                "Run Load Failed",
                f"Could not load Run ID {self.selected_run_id}.",
                str(error)
                )
            return None


    def _measurement_dataframe(self, dataset, params):
        """
        Builds a flat CSV-friendly dataframe for the selected measurement data.

        """
        frames = []
        prefix_columns = len(params) > 1
        for param in params:
            param_data = dataset.get_parameter_data(param.name).get(param.name, {})
            columns = {}
            for name, values in param_data.items():
                column_name = f"{param.name}.{name}" if prefix_columns else name
                columns[column_name] = pd.Series(np.asarray(values).ravel())
            frames.append(pd.DataFrame(columns))

        return pd.concat(frames, axis=1) if frames else pd.DataFrame()


    def _default_export_filename(self, dataset, params):
        """
        Returns a default CSV export path.

        """
        database_folder = os.path.dirname(self.fileTextbox.text())
        measurement = "all" if len(params) != 1 else params[0].name
        filename = self._safe_filename(f"run_{dataset.run_id}_{measurement}.csv")
        return os.path.join(database_folder or os.getcwd(), filename)


    def _safe_filename(self, filename):
        """
        Replaces path-hostile characters in a suggested filename.

        """
        return "".join(char if char.isalnum() or char in "._-" else "_" for char in filename)
    
    
    @QtCore.pyqtSlot(str)
    def update_run_id(self, text):
        """
        Updates the Run ID target entered into the Run text box.

        Parameters
        ----------
        text : str/int
            Run ID number to be plotted.

        """
        self.RunList.blockSignals(True)
        self.RunList.clearSelection()
        self.RunList.blockSignals(False)
        self.ds = None
        self.infoBox.clear()

        try:
            self.selected_run_id = int(text)
        except ValueError:
            self.selected_run_id = None
            return


    @QtCore.pyqtSlot()
    def sync_run_id_selection(self):
        """
        Selects the typed Run ID in the table if it is currently visible.

        """
        if self.selected_run_id is None:
            return

        matches = self.RunList.findItems(
            str(self.selected_run_id),
            QtCore.Qt.MatchExactly,
            0
            )
        if not matches:
            return

        item = matches[0]
        self.RunList.setCurrentItem(item)
        self.RunList.scrollToItem(item, qtw.QAbstractItemView.PositionAtCenter)
        
        
    def change_theme(self, theme, action):
        """
        Event handler for changing style/theme.
        Updates Main Window theme and all other Plot windows.

        Parameters
        ----------
        theme : str
            Name of the theme to change to.
        action : PyQt5.QtWidgets.QAction
            Button which sent the signal for the action.

        """
        if self.config.get("user_preference.theme") == theme: #already selected
            action.setChecked(True)
            self.show_status(f"{theme.title()} theme already selected.", 3000)
            return
        for QActions in self.themes: # Untick other options
            if QActions != action:
                QActions.setChecked(False)
                
        # Update config.jon
        self.config.update("user_preference.theme", theme)
        
        # Update all windows.
        self.setStyleSheet(self.config.theme.main)
        for win in self.windows:
            win.update_theme(self.config)
        self.show_status(f"Theme changed to {theme}.", 2000)


    def change_preview_size(self, preview_size):
        """
        Updates preview image size and regenerates preview thumbnails.

        """
        preview_size = int(preview_size)
        if preview_size == self.preview_size:
            return

        self.preview_size = preview_size
        self._save_preview_size(preview_size)
        if hasattr(self, "infoBox"):
            self.infoBox.set_preview_size(preview_size)
            if hasattr(self, "runInfoSplitter"):
                self.runInfoSplitter.setSizes([380, self._details_pane_height()])
        self.show_status(f"Preview size set to {preview_size} px.", 3000)


    @QtCore.pyqtSlot()
    def restore_default_settings(self):
        """
        Confirms and restores all user settings to schema defaults.

        """
        reply = qtw.QMessageBox.question(
            self,
            "Restore Default Settings",
            "Restore all qPlot settings to their defaults?",
            qtw.QMessageBox.Yes | qtw.QMessageBox.No,
            qtw.QMessageBox.No,
            )
        if reply != qtw.QMessageBox.Yes:
            self.show_status("Default settings restore cancelled.", 3000)
            return

        self.close_plot_windows(confirm=False, status=False)
        self.config.reset_to_defaults()
        self.apply_current_settings()
        self.close_database(status=False)
        self.show_status("Default settings restored.", 5000)


    def apply_current_settings(self):
        """
        Applies config-backed settings that can be updated in open windows.

        """
        self._sync_theme_actions()
        self._sync_preview_size_actions()
        self._sync_refresh_interval()
        self.setStyleSheet(self.config.theme.main)
        for win in self.windows:
            win.update_theme(self.config)


    def _sync_theme_actions(self):
        current_theme = self.config.get("user_preference.theme")
        for action in getattr(self, "themes", []):
            action.blockSignals(True)
            action.setChecked(action.text().replace("&", "").lower() == current_theme)
            action.blockSignals(False)


    def _sync_preview_size_actions(self):
        self.preview_size = self._configured_preview_size()
        for action in getattr(self, "previewSizeActions", []):
            action.blockSignals(True)
            action.setChecked(action.data() == self.preview_size)
            action.blockSignals(False)

        if hasattr(self, "infoBox"):
            self.infoBox.set_preview_size(self.preview_size)
            if hasattr(self, "runInfoSplitter"):
                self.runInfoSplitter.setSizes([380, self._details_pane_height()])


    def _sync_refresh_interval(self):
        interval = self.config.get("user_preference.default_refresh_rate")
        if not hasattr(self, "spinBox"):
            return

        self.spinBox.blockSignals(True)
        self.spinBox.setValue(interval)
        self.spinBox.blockSignals(False)
        self._apply_refresh_interval(self.spinBox.value())


    def _configured_preview_size(self):
        try:
            return int(self.config.get("GUI.preview_size"))
        except (KeyError, TypeError, ValueError):
            return PREVIEW_SIZE


    def _save_preview_size(self, preview_size):
        gui_config = self.config.config.setdefault("GUI", {})
        if "preview_size" not in gui_config:
            gui_config["preview_size"] = self.preview_size
        self.config.update("GUI.preview_size", int(preview_size))


    def _details_pane_height(self):
        return max(260, int(self.preview_size) + 84)

###############################################################################
#Other funcs

    def show_status(self, message : str, timeout : int = 5000):
        """
        Shows a short message in the main window status bar.

        """
        self.statusBar().showMessage(message, timeout)


    def show_error(self, title : str, message : str, details : str = None):
        """
        Shows an error both in the status bar and in a message box.

        """
        log_user_error(title, message, details, __name__)
        self.show_status(message, 10000)

        box = qtw.QMessageBox(qtw.QMessageBox.Warning, title, message, parent=self)
        if details:
            box.setDetailedText(details)
        box.exec_()


    @QtCore.pyqtSlot(str)
    def add_ds_at(self, guid : str, ds = None):
        """
        Uses the guid of a dataset to update self.dataset_holder with a new 
        tracker of a dataset
        
        If the dataset is already stored, increases the tracker of the number
        of windows that use that dataset (users).
        If the dataset is not stored, load a new dataset with that guid

        Parameters
        ----------
        guid : str
            guid of the dataset being added.
        ds : TYPE, optional
            An already loaded dataset with a guid. 
            This may be from using that dataset elsewhere in the app and is 
            passed to prevent loading again.

        """
        # dataset does not exist
        if self.dataset_holder.get(guid, 0) == 0:
            # load ds unless ds is already provided
            ds = load_by_guid(guid) if ds is None else ds
            assert ds.guid == guid
            
            self.dataset_holder[guid] = {
                "dataset" : ds,
                "users" : 1,
                "del_timer" : None
                }
        # increment users and stop deletion timer if needed
        else:
            self.dataset_holder[guid]["users"] += 1
            if self.dataset_holder[guid]["del_timer"] is not None:
                self.dataset_holder[guid]["del_timer"].stop() # Stop delete timer
                self.dataset_holder[guid]["del_timer"] = None
            
    
    @QtCore.pyqtSlot(str)
    def remove_ds_at(self, guid : str):
        """
        Decreases the count of users for a dataset.
        If dataset has no more users, begin timer to delete object if unused.
        If dataset gets a new user, timer is stopped.
        Timer length can be found in config file.

        Parameters
        ----------
        guid : str
            guid of the dataset being removed.

        """
        # Check dataset is available to be removed
        if self.dataset_holder.get(guid, 0) == 0:
            self.show_status("Trying to remove dataset that does not exist.", 5000)
            return
        
        # Track removal
        self.dataset_holder[guid]["users"] -= 1
        
        # Check for no windows using
        if self.dataset_holder[guid]["users"] <= 0:
            del_time = self.config.get("runtime_settings.del_grace_period")
            
            # Remove now if no grace period
            if del_time == 0: # Remove now if no grace period
                self.dataset_holder.pop(guid)
                
            # Set up removal timer, remove after del_time seconds
            elif self.dataset_holder[guid]["del_timer"] is None:
                del_timer = QtCore.QTimer()
                del_timer.setSingleShot(True)
                self.dataset_holder[guid]["del_timer"] = del_timer
                # Link timer to delete
                del_timer.timeout.connect(lambda guid=guid:
                    self.dataset_holder.pop(guid)
                    )
                    
                del_timer.start(int(del_time*1000)) # convert to seconds
        

    def load_file(self, abspath, load_started_at = None):
        """
        Updates the database for RunList display and loading datasets.
        Used by file selection, drag-and-drop, recent databases, and startup loading.

        Parameters
        ----------
        abspath : str
            Path to database.

        """
        if load_started_at is None:
            load_started_at = perf_counter()
        log_event("Loading database file: %s", abspath, logger_name=__name__)
        
        if abspath == get_DB_location() and self.fileTextbox.text() == abspath:
            # Already initialised in QCoDeS and still open in this window.
            if not self.infoBox.preview.has_database(abspath):
                self.infoBox.preview.set_database_runs(
                    abspath,
                    self.RunList.all_run_metadata()
                    )
            elapsed = perf_counter() - load_started_at
            self.show_status(f"Database is already loaded ({elapsed:.2f} s).", 3000)
            self.remember_loaded_database(abspath)
            return True

        if self._database_load_active:
            self.show_status("Wait for the current database load to finish.", 5000)
            return False

        previous_file = self.fileTextbox.text()
        previous_runs = self._current_run_metadata()
        monitorTimer = self.spinBox.value()
        load_message = f"Loading database {os.path.basename(abspath)}..."

        # Pause refresh while working
        self.monitor.stop()

        self._database_load_generation += 1
        generation = self._database_load_generation
        self._database_load_active = True
        self._database_load_state = {
            "abspath": abspath,
            "load_started_at": load_started_at,
            "monitorTimer": monitorTimer,
            "previous_file": previous_file,
            "previous_runs": previous_runs,
            }

        self._prepare_database_load_ui(abspath)
        self._set_database_load_controls_enabled(False)
        self._show_database_load_panel(load_message)

        try:
            cloud_sync_timeout = self.config.get("runtime_settings.cloud_sync_timeout")
        except KeyError:
            cloud_sync_timeout = DATABASE_CLOUD_SYNC_TIMEOUT_SECONDS

        worker = DatabaseLoadWorker(generation, abspath, cloud_sync_timeout)
        self._database_load_worker = worker
        worker.signals.status.connect(self.database_load_status)
        worker.signals.finished.connect(self.database_load_finished)
        self.databaseLoadThreadPool.start(worker)
        return True


    def _prepare_database_load_ui(self, abspath):
        """
        Clears the main-window state for a new database load.

        """
        self.run_idBox.setText("")
        self.measurementBox.setText("*")
        self.selected_run_id = None
        self.ds = None

        self.RunList.clearSelection()
        self.RunList.clear()
        self.RunList.watching = []
        self.RunList.maxTime = 0
        self.RunList.scrollToTop()

        self.infoBox.clear()
        self.infoBox.scrollToTop()

        if self.fileTextbox.text() and self.fileTextbox.text() != self.localLastFile:
            self.localLastFile = self.fileTextbox.text()

        self.fileTextbox.setText(abspath)


    def _set_database_load_controls_enabled(self, enabled):
        """
        Enables or disables controls that start overlapping database actions.

        """
        for attr in (
            "loadDatabaseButton",
            "refreshDatabaseButton",
            "databaseInfoButton",
            "openDatabaseFolderButton",
            ):
            widget = getattr(self, attr, None)
            if widget is not None:
                widget.setEnabled(enabled)


    def _current_run_metadata(self):
        """
        Returns the currently displayed run metadata, if available.

        """
        all_run_metadata = getattr(self.RunList, "all_run_metadata", None)
        if not callable(all_run_metadata):
            return {}

        try:
            return all_run_metadata()
        except Exception as err:
            log_exception("Could not capture current run metadata", err, __name__)
            return {}


    def _restore_database_load_previous_state(self, state):
        """
        Restores the visible database state after a cancelled or failed load.

        """
        previous_file = state.get("previous_file", "")
        previous_runs = state.get("previous_runs") or {}

        self.fileTextbox.setText(previous_file)
        self.run_idBox.setText("")
        self.measurementBox.setText("*")
        self.selected_run_id = None
        self.ds = None

        self.RunList.clearSelection()
        self.RunList.clear()
        self.RunList.watching = []
        self.RunList.maxTime = 0
        if previous_runs:
            self.RunList.addRuns(previous_runs)
        self.RunList.scrollToTop()

        self.infoBox.clear()
        self.infoBox.scrollToTop()
        self.infoBox.preview.set_database_runs(previous_file, previous_runs)


    def _show_database_load_panel(self, message):
        """
        Shows the inline database-load progress panel.

        """
        if hasattr(self, "databaseLoadLabel"):
            self.databaseLoadLabel.setText(message)
            self.databaseLoadLabel.setToolTip(message)
        if hasattr(self, "databaseLoadFrame"):
            self.databaseLoadFrame.setVisible(True)
        self.show_status(message, 0)


    def _hide_database_load_panel(self):
        """
        Hides the inline database-load progress panel.

        """
        if hasattr(self, "databaseLoadLabel"):
            self.databaseLoadLabel.setText("")
            self.databaseLoadLabel.setToolTip("")
        if hasattr(self, "databaseLoadFrame"):
            self.databaseLoadFrame.setVisible(False)


    @QtCore.pyqtSlot()
    def cancel_database_load(self):
        """
        Cancels the active database load and restores the previous view.

        """
        if not getattr(self, "_database_load_active", False):
            self._hide_database_load_panel()
            return

        state = self._database_load_state or {}
        worker = getattr(self, "_database_load_worker", None)
        if worker is not None:
            worker.cancel()

        self._database_load_generation += 1
        self._database_load_active = False
        self._database_load_state = None
        self._database_load_worker = None
        self._set_database_load_controls_enabled(True)
        self._restore_database_load_previous_state(state)

        monitorTimer = state.get("monitorTimer", 0)
        if monitorTimer > 0:
            self.monitor.start(int(monitorTimer * 1000))

        self._hide_database_load_panel()
        self.show_status("Database load cancelled.", 3000)


    @QtCore.pyqtSlot(int, str)
    def database_load_status(self, generation, message):
        """
        Shows progress from the active database load.

        """
        if generation != self._database_load_generation or not self._database_load_active:
            return

        self._show_database_load_panel(message)


    @QtCore.pyqtSlot(int, str, object, object)
    def database_load_finished(self, generation, abspath, runs, error):
        """
        Applies the background database load result on the GUI thread.

        """
        if generation != self._database_load_generation:
            return

        state = self._database_load_state or {}
        self._database_load_active = False
        self._database_load_state = None
        self._database_load_worker = None
        self._set_database_load_controls_enabled(True)
        self._hide_database_load_panel()

        monitorTimer = state.get("monitorTimer", 0)
        load_started_at = state.get("load_started_at") or perf_counter()

        if error is not None:
            self._restore_database_load_previous_state(state)
            log_exception("Database load failed", error, __name__)
            self.show_error(
                "Database Load Failed",
                f"Could not load database {abspath}.",
                str(error)
                )
            if monitorTimer > 0:
                self.monitor.start(int(monitorTimer * 1000))
            return

        runs = runs or {}
        self.RunList.clear()
        self.RunList.watching = []
        self.RunList.maxTime = 0
        self.RunList.addRuns(runs)
        self.infoBox.preview.set_database_runs(abspath, runs)
        self.select_default_run()

        if monitorTimer > 0:
            self.monitor.start(int(monitorTimer * 1000))

        elapsed = perf_counter() - load_started_at
        self.remember_loaded_database(abspath)
        self.show_status(
            (
                f"Loaded {self.RunList.topLevelItemCount()} runs from "
                f"{os.path.basename(abspath)} in {elapsed:.2f} s."
                ),
            5000
            )
        log_event(
            "Loaded %s runs from %s in %.2f s",
            self.RunList.topLevelItemCount(),
            abspath,
            elapsed,
            logger_name=__name__,
            )


    def select_default_run(self):
        """
        Select the first visible run so the details pane is not left empty.

        """
        if self.RunList.topLevelItemCount() == 0:
            return

        first_item = self.RunList.topLevelItem(0)
        if first_item is None:
            return

        self.RunList.setCurrentItem(first_item)
        self.RunList.scrollToItem(first_item, qtw.QAbstractItemView.PositionAtTop)
            
    
    def post_admin(self):
        """
        Updates the Plot windows internal track of other open windows.

        """
        for item in self.windows:
            if isinstance(item, plot1d):
                self.get_1d_wins(item)
                
    
    def get_1d_wins(self, win):
        """
        Finds compatable Plot windows for adding secondary plot to for win.

        Parameters
        ----------
        win : qplot.windows.plotWin.plotWidget
            The window which is being refreshed.

        """
        wins = []
        
        for item in self.windows:
            # Find compatible windows
            try:
                if item.param.depends_on == win.param.depends_on:
                    if not item.label in win.lines.keys():
                        wins.append(item)
                        
                elif (item.__class__.__name__ == "sweeper" 
                      and 
                      item.axis_options["x"] == win.param.depends_on):
                    if not item.label in win.lines.keys():
                        wins.append(item)
                        
                    
            except AttributeError: # If not initisiased properly
                continue
        
        # Update within win
        win.update_line_picker(wins)
