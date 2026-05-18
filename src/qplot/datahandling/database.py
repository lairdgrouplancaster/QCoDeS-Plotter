"""
Database access helpers used by the main window.

This module keeps blocking database probes, cloud-file hydration, background
load workers, and diagnostic report generation outside the GUI class.
"""

import os
import queue
import subprocess
import sys
import threading
from datetime import datetime
from time import perf_counter

from PyQt6 import QtCore

from qplot.datahandling.readonly import (
    set_qcodes_database_location,
    sqlite_read_only_connection,
)
from qplot.datahandling.readSQL import get_runs_via_sql
from qplot.diagnostics import log_exception

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
        "from pathlib import Path\n"
        "import sqlite3, sys\n"
        "uri = f'{Path(sys.argv[1]).resolve().as_uri()}?mode=ro'\n"
        "conn = sqlite3.connect(uri, timeout=1, uri=True)\n"
        "try:\n"
        "    conn.execute('PRAGMA user_version').fetchone()\n"
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
                        f"Timed out after {timeout:g} s while waiting for "
                        f"{provider} to download the database. Check that "
                        f"{provider} is running and signed in, or mark the "
                        "database folder as always available on this device."
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

            self._emit_status("Opening database read-only...")
            set_qcodes_database_location(self.database_path)
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

    conn = sqlite_read_only_connection(path, timeout=10)
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
