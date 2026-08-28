"""
Database access helpers used by the main window.

This module keeps blocking database probes, cloud-file hydration, background
load workers, and diagnostic report generation outside the GUI class.
"""

import json
import os
import queue
import stat
import subprocess
import sys
import threading
from datetime import datetime
from time import monotonic, perf_counter

from PyQt6 import QtCore

from qplot.datahandling.file_identity import (
    DatabaseInstance,
    database_file_identity,
    database_instance,
    database_instances_differ,
)
from qplot.datahandling.readonly import (
    DatabaseInstanceChangedError,
    UnverifiableDatabaseWalError,
    quarantine_wal_for_replaced_database,
    replacement_wal_is_quarantined,
    sqlite_read_only_connection,
)
from qplot.datahandling.readSQL import (
    find_new_runs,
    get_run_status,
    get_runs_basic_via_sql,
    get_snapshot_selected_run_detail,
    iter_run_detail_batches_via_sql,
    iter_run_shape_batches_via_sql,
    iter_run_storage_batches_via_sql,
)
from qplot.datahandling.trusted_live import (
    TrustedLiveReaderUnavailableError,
    TrustedLiveUnsupportedSourceError,
)
from qplot.datahandling.trusted_live_queries import run_records_as_dict
from qplot.datahandling.trusted_live_service import (
    SNAPSHOT_FALLBACK_MODE,
    TRUSTED_LIVE_MODE,
    TrustedLiveReadService,
    TrustedReadPriority,
    TrustedReadRequestCancelledError,
)
from qplot.datahandling.trusted_presentation import (
    bounded_presentation_error,
    bounded_selected_run_fields,
)
from qplot.diagnostics import log_exception

DATABASE_ACCESS_TIMEOUT_SECONDS = 3
DATABASE_CLOUD_SYNC_TIMEOUT_SECONDS = 120
DATABASE_CLOUD_SYNC_CHUNK_BYTES = 4 * 1024 * 1024
DATABASE_CLOUD_SYNC_STATUS_INTERVAL = 1.0
DATABASE_CLOUD_SYNC_RETRY_INTERVAL = 0.25
DATABASE_PREFETCH_STATUS_PREFIX = "QPLOT_PREFETCH_PROGRESS:"
CLOUD_PLACEHOLDER_XATTR_MARKERS = (
    "com.apple.fileprovider",
    "com.apple.fileutil.PlaceholderData",
    "com.microsoft.OneDrive",
    )
WINDOWS_CLOUD_PLACEHOLDER_ATTRIBUTES = (
    getattr(stat, "FILE_ATTRIBUTE_OFFLINE", 0x00001000)
    | getattr(stat, "FILE_ATTRIBUTE_RECALL_ON_OPEN", 0x00040000)
    | getattr(stat, "FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS", 0x00400000)
)
_DATABASE_ACCESS_ERROR_PREFIX = "QPLOT_DATABASE_ACCESS_ERROR:"


def _bounded_run_publication(fields):
    """Strip raw run payloads before any worker result crosses into Qt."""

    return {
        name: list(value) if isinstance(value, tuple) else value
        for name, value in bounded_selected_run_fields(fields or {})
    }


def _bounded_runs_publication(runs):
    return {
        run_id: _bounded_run_publication(fields)
        for run_id, fields in (runs or {}).items()
    }


def _bounded_worker_error(error):
    """Detach tracebacks/contexts and cap every object queued into Qt."""

    if error is None:
        return None
    message = bounded_presentation_error(error)
    if isinstance(error, DatabaseInstanceChangedError):
        return DatabaseInstanceChangedError(message)
    if isinstance(error, UnverifiableDatabaseWalError):
        return UnverifiableDatabaseWalError(message)
    return message


def trusted_open_failure_allows_snapshot_fallback(error):
    """Return true only for the two documented initial-open outcomes.

    Several supervisor/process failures intentionally inherit
    ``TrustedLiveReaderUnavailableError``.  Exact-type checks prevent helper
    crashes, protocol failures, cancellation, deadlines, and accepted-session
    failures from being converted into a legacy snapshot replay.
    """

    return type(error) in {
        TrustedLiveReaderUnavailableError,
        TrustedLiveUnsupportedSourceError,
    }


class _DatabaseAccessErrorMessage(str):
    """Probe text retaining the remote exception type for the load worker."""

    def __new__(cls, message, error_type=None):
        instance = super().__new__(cls, message)
        instance.error_type = error_type
        return instance


def _database_access_error_message(output):
    """Decode a probe error without exposing its transport marker to users."""
    output = (output or "").strip()
    for line in reversed(output.splitlines()):
        if not line.startswith(_DATABASE_ACCESS_ERROR_PREFIX):
            continue
        try:
            payload = json.loads(line.removeprefix(_DATABASE_ACCESS_ERROR_PREFIX))
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        message = payload.get("message")
        error_type = payload.get("type")
        if isinstance(message, str) and isinstance(error_type, str):
            return _DatabaseAccessErrorMessage(message, error_type)
    return _DatabaseAccessErrorMessage(output)


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


def database_access_error(
        database_path,
        timeout=DATABASE_ACCESS_TIMEOUT_SECONDS,
        *,
        expected_database_identity=None,
        cancelled_callback=None,
        deadline=None,
        ):
    """
    Return an error message if a database cannot be opened promptly.

    QCoDeS initialisation can block inside SQLite when another process or a
    cloud-sync provider holds the database. Probe in a short-lived interpreter
    first so a stuck access check can be timed out without freezing qPlot.

    """
    if cancelled_callback is not None and cancelled_callback():
        raise InterruptedError("Database access check cancelled.")
    if deadline is not None and monotonic() >= deadline:
        raise TimeoutError("Database access check deadline exceeded.")

    # The child has a fresh replacement registry. Carry the identity observed
    # before its launch so it cannot combine a later main-file replacement
    # with a retained WAL sidecar.
    if expected_database_identity is None:
        expected_database_identity = database_file_identity(database_path)
    ignore_unpaired_wal = replacement_wal_is_quarantined(database_path)
    timeout = float(timeout)
    if deadline is not None:
        timeout = min(timeout, max(0.0, float(deadline) - monotonic()))
    probe = (
        "import json\n"
        "import sys\n"
        "from time import monotonic\n"
        "from qplot.datahandling.readonly import probe_read_only_database\n"
        "try:\n"
        "    expected_database_identity = json.loads(sys.argv[4])\n"
        "    if expected_database_identity is not None:\n"
        "        expected_database_identity = tuple(expected_database_identity)\n"
        "    deadline = monotonic() + max(0.0, float(sys.argv[3]))\n"
        "    probe_read_only_database(\n"
        "        sys.argv[1],\n"
        "        ignore_unpaired_wal=(sys.argv[2] == '1'),\n"
        "        expected_database_identity=expected_database_identity,\n"
        "        deadline=deadline,\n"
        "        _check_sqlite_lock_bytes=True,\n"
        "    )\n"
        "except Exception as err:\n"
        "    payload = {'type': type(err).__name__, 'message': str(err)}\n"
        f"    print('{_DATABASE_ACCESS_ERROR_PREFIX}' + json.dumps(payload), "
        "file=sys.stderr)\n"
        "    raise SystemExit(1)\n"
        )

    command = [
        sys.executable,
        "-c",
        probe,
        database_path,
        "1" if ignore_unpaired_wal else "0",
        repr(timeout),
        json.dumps(expected_database_identity),
    ]

    try:
        if cancelled_callback is None:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                )
            returncode = result.returncode
            stdout = result.stdout
            stderr = result.stderr
        else:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                )
            probe_started_at = perf_counter()
            while True:
                if cancelled_callback():
                    process.kill()
                    process.communicate()
                    raise InterruptedError("Database access check cancelled.")
                remaining = timeout - (perf_counter() - probe_started_at)
                if remaining <= 0:
                    process.kill()
                    process.communicate()
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    stdout, stderr = process.communicate(
                        timeout=min(0.05, remaining),
                        )
                except subprocess.TimeoutExpired:
                    continue
                returncode = process.returncode
                break
    except InterruptedError:
        raise
    except subprocess.TimeoutExpired:
        if deadline is not None and monotonic() >= deadline:
            raise TimeoutError("Database access check deadline exceeded.") from None
        return (
            f"Timed out after {timeout:g} s while checking database access. "
            "The database may be locked by another qPlot, QCoDeS, Python, or "
            "notebook process, or blocked by cloud sync."
            )
    except OSError as err:
        if cancelled_callback is not None and cancelled_callback():
            raise InterruptedError("Database access check cancelled.") from err
        return str(err)

    if cancelled_callback is not None and cancelled_callback():
        raise InterruptedError("Database access check cancelled.")

    if returncode == 0:
        return None

    details = _database_access_error_message(stderr or stdout)
    if not details:
        details = _DatabaseAccessErrorMessage(
            f"SQLite access probe exited with code {returncode}."
        )
    if (
            deadline is not None
            and details.error_type == TimeoutError.__name__
            ):
        raise TimeoutError(str(details))
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
    file_attributes = getattr(info, "st_file_attributes", None)
    if file_attributes is not None:
        return bool(file_attributes & WINDOWS_CLOUD_PLACEHOLDER_ATTRIBUTES)

    blocks = getattr(info, "st_blocks", None)
    if blocks is None:
        return False

    allocated_size = blocks * 512
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

    script = _database_prefetch_script()

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


def _database_prefetch_script():
    """
    Returns the isolated cloud-hydration script used by the load worker.

    Cloud providers can report a transient OS-level TimeoutError while a
    placeholder is being materialised. The subprocess keeps retrying those
    reads; its parent remains responsible for the overall timeout and cancel.

    """
    return (
        "import errno, os, sys, time\n"
        f"prefix = {DATABASE_PREFETCH_STATUS_PREFIX!r}\n"
        f"chunk_size = {DATABASE_CLOUD_SYNC_CHUNK_BYTES!r}\n"
        f"status_interval = {DATABASE_CLOUD_SYNC_STATUS_INTERVAL!r}\n"
        f"retry_interval = {DATABASE_CLOUD_SYNC_RETRY_INTERVAL!r}\n"
        "transient_errors = {errno.ETIMEDOUT, errno.ECANCELED}\n"
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
        "while True:\n"
        "    try:\n"
        "        handle = open(path, 'rb')\n"
        "        break\n"
        "    except OSError as error:\n"
        "        if error.errno not in transient_errors:\n"
        "            raise\n"
        "        report(True)\n"
        "        time.sleep(retry_interval)\n"
        "with handle:\n"
        "    while True:\n"
        "        try:\n"
        "            chunk = handle.read(chunk_size)\n"
        "        except OSError as error:\n"
        "            if error.errno not in transient_errors:\n"
        "                raise\n"
        "            report(True)\n"
        "            time.sleep(retry_interval)\n"
        "            continue\n"
        "        if not chunk:\n"
        "            break\n"
        "        read += len(chunk)\n"
        "        report(False)\n"
        "report(True)\n"
        "print(read, flush=True)\n"
    )


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


class DatabaseDetailSignals(QtCore.QObject):
    """
    Signals emitted by progressive run-detail loading.

    """
    status = QtCore.pyqtSignal(int, str)
    batch_ready = QtCore.pyqtSignal(int, str, object)
    finished = QtCore.pyqtSignal(int, str, object)


class DatabaseSelectedRunSignals(QtCore.QObject):
    """Signals for one generation-bound plain selected-run view."""

    progress = QtCore.pyqtSignal(int, str, str, object)
    finished = QtCore.pyqtSignal(int, str, str, object, object)


class _InterruptibleSqlWorker:
    """Allows the GUI thread to interrupt an active read-only SQLite query."""

    def _init_database_binding(
            self,
            database_path,
            expected_database_instance=None,
            deadline=None,
            ):
        """Retain the exact database instance accepted by the scheduler."""
        supplied_instance = expected_database_instance is not None
        requested_database_path = os.fspath(database_path)
        if expected_database_instance is None:
            expected_database_instance = database_instance(requested_database_path)
        elif not isinstance(expected_database_instance, DatabaseInstance):
            raise TypeError(
                "expected_database_instance must be a DatabaseInstance or None"
            )

        self.database_instance = expected_database_instance
        self.database_path = expected_database_instance.logical_path
        self.requested_database_path = requested_database_path
        # Signals are matched against the paths stored by the UI, which are
        # logical_database_path values.  Keeping the original spelling here
        # makes an otherwise identical Windows path (for example ``D:`` vs
        # ``d:``) look like a different database to signal consumers.
        self._signal_database_path = requested_database_path
        self._read_database_path = (
            expected_database_instance.logical_path
            if supplied_instance
            else requested_database_path
        )
        self.logical_database_path = expected_database_instance.logical_path
        self.resolved_database_path = expected_database_instance.resolved_path
        self.database_identity = expected_database_instance.identity
        self.sidecar_identities = expected_database_instance.sidecar_identities
        self.deadline = deadline


    def _expected_database_identity_kwargs(self):
        """Keep legacy direct worker callers working when identity is unavailable."""
        if self.database_identity is None:
            return {}
        return {"expected_database_identity": self.database_identity}


    def _read_control_kwargs(self):
        """Forward cancellation, identity, and an optional absolute deadline."""
        kwargs = self._expected_database_identity_kwargs()
        kwargs["cancelled_callback"] = self._cancelled.is_set
        if self.deadline is not None:
            kwargs["deadline"] = self.deadline
        return kwargs


    def _require_current_database_instance(self):
        """Reject results if the worker's full logical source was replaced."""
        accepted_instance = self.database_instance
        current_instance = database_instance(accepted_instance.logical_path)
        if (
                not database_instances_differ(accepted_instance, current_instance)
                and accepted_instance.sidecar_identities.issubset(
                    current_instance.sidecar_identities
                )
                ):
            # SQLite may create WAL/SHM after the controller first accepts the
            # main file.  Permit that first directional appearance, but bind it
            # immediately so a later removal, rotation, or ABA substitution is
            # rejected by the worker's next pre-read/pre-publication guard.
            if (
                    accepted_instance.sidecar_identities
                    != current_instance.sidecar_identities
                    ):
                self.database_instance = current_instance
                self.sidecar_identities = current_instance.sidecar_identities
            return

        quarantine_wal_for_replaced_database(accepted_instance.logical_path)
        raise DatabaseInstanceChangedError(
            "The database or one of its SQLite sidecars was replaced while "
            "qPlot was loading background metadata."
        )


    @staticmethod
    def _close_batch_iterator(iterator):
        """Close a suspended SQL generator immediately on every early exit."""
        close = getattr(iterator, "close", None)
        if callable(close):
            close()

    def _init_sql_interrupt(self):
        self._sql_connection_lock = threading.Lock()
        self._publication_lock = threading.RLock()
        self._sql_connection = None
        self._trusted_request_lock = threading.Lock()
        self._trusted_request = None


    def _cancel_read(self):
        """Linearize cancellation against worker result publication."""
        with self._publication_lock:
            self._cancelled.set()
        with self._trusted_request_lock:
            trusted_request = self._trusted_request
        if trusted_request is not None:
            trusted_request.cancel()
        self._interrupt_sql()


    def _interrupt_sql(self):
        with self._sql_connection_lock:
            connection = self._sql_connection
        if connection is not None:
            try:
                connection.interrupt()
            except Exception:
                pass


    def _set_sql_connection(self, connection):
        with self._sql_connection_lock:
            self._sql_connection = connection
        if connection is not None and self._cancelled.is_set():
            self._interrupt_sql()


    def _set_trusted_request(self, request):
        with self._trusted_request_lock:
            self._trusted_request = request
        if request is not None and self._cancelled.is_set():
            request.cancel()


    def _wait_trusted_request(self, request):
        self._set_trusted_request(request)
        try:
            return request.wait()
        finally:
            with self._trusted_request_lock:
                if self._trusted_request is request:
                    self._trusted_request = None


class DatabaseRefreshSignals(QtCore.QObject):
    """Signals emitted by a coalesced main-window refresh worker."""

    new_runs_ready = QtCore.pyqtSignal(int, str, object)
    finished = QtCore.pyqtSignal(int, str, object, object, object)


class DatabaseRefreshWorker(_InterruptibleSqlWorker, QtCore.QRunnable):
    """Fetch new runs and live-run status without blocking the GUI thread."""

    def __init__(
            self,
            generation,
            database_path,
            last_run_id,
            watched_runs,
            *,
            expected_database_instance=None,
            deadline=None,
            trusted_service=None,
            require_publication_ack=False,
            ):
        super().__init__()
        self.signals = DatabaseRefreshSignals()
        self.generation = generation
        self._init_database_binding(
            database_path,
            expected_database_instance,
            deadline,
            )
        self.last_run_id = int(last_run_id or 0)
        self.watched_runs = list(watched_runs or [])
        self._cancelled = threading.Event()
        self._init_sql_interrupt()
        self.trusted_service = trusted_service
        self._require_publication_ack = bool(require_publication_ack)
        self._new_runs_publication_ack = threading.Event()
        self._new_runs_publication_lock = threading.Lock()
        self._new_runs_publication_error: BaseException | None = None


    def cancel(self):
        self._cancel_read()
        self._new_runs_publication_ack.set()


    def acknowledge_new_runs_published(self):
        """Release the off-GUI refresh only after its basic page is visible."""
        with self._new_runs_publication_lock:
            self._new_runs_publication_error = None
        self._new_runs_publication_ack.set()


    def reject_new_runs_publication(self, error):
        """Fail closed when a trusted basic page could not reach the GUI."""
        with self._new_runs_publication_lock:
            self._new_runs_publication_error = error
        # The adapter cursor already accepted this bounded page.  Retire the
        # session instead of letting a later refresh omit rows the GUI failed
        # to publish.  close_async is deliberately prompt and GUI-safe.
        if self.trusted_service is not None:
            self.trusted_service.close_async()
        self._new_runs_publication_ack.set()


    def run(self):
        new_runs = {}
        statuses = {}
        try:
            if self._cancelled.is_set():
                return

            self._require_current_database_instance()
            if self.trusted_service is not None:
                new_runs, statuses = self._run_trusted_refresh()
                self._require_current_database_instance()
                self._emit_finished({}, statuses, None)
                return
            new_runs = find_new_runs(
                self.last_run_id,
                database_path=self._read_database_path,
                connection_callback=self._set_sql_connection,
                **self._read_control_kwargs(),
                ) or {}
            for guid in self.watched_runs:
                if self._cancelled.is_set():
                    return
                self._require_current_database_instance()
                status = get_run_status(
                    guid,
                    database_path=self._read_database_path,
                    include_storage_bytes=False,
                    connection_callback=self._set_sql_connection,
                    **self._read_control_kwargs(),
                    )
                if status:
                    statuses[guid] = status
            self._require_current_database_instance()
        except InterruptedError:
            return
        except DatabaseInstanceChangedError as err:
            if self._cancelled.is_set():
                return
            self._emit_finished({}, {}, err)
            return
        except Exception as err:
            if self._cancelled.is_set():
                return
            log_exception("Database refresh worker failed", err, __name__)
            self._emit_finished(new_runs, statuses, err)
            return

        self._emit_finished(new_runs, statuses, None)


    def _run_trusted_refresh(self):
        header = self._wait_trusted_request(
            self.trusted_service.submit_refresh(
                self.last_run_id,
                deadline=self.deadline,
            )
        )
        new_runs = {}
        if header.data_version_changed:
            cursor = header.prior_run_id_watermark
            while True:
                if self._cancelled.is_set():
                    raise InterruptedError("Database refresh cancelled.")
                page = self._wait_trusted_request(
                    self.trusted_service.submit_basic_page(
                        cursor,
                        header.run_id_watermark,
                        priority=TrustedReadPriority.REFRESH,
                        deadline=self.deadline,
                    )
                )
                page_runs = run_records_as_dict(page.runs)
                new_runs.update(page_runs)
                if page_runs:
                    self._emit_new_runs_ready(page_runs)
                if page.complete:
                    break
                cursor = page.next_run_id

        statuses = {}
        if not header.data_version_changed:
            return new_runs, statuses
        for watched in self.watched_runs:
            if self._cancelled.is_set():
                raise InterruptedError("Database refresh cancelled.")
            if (
                    isinstance(watched, (tuple, list))
                    and len(watched) in {2, 3}
                    ):
                if len(watched) == 2:
                    run_id, guid = watched
                    category = "remaining"
                else:
                    run_id, guid, category = watched
            else:
                continue
            try:
                run_id = int(run_id)
            except (TypeError, ValueError):
                continue
            if category == "selected":
                cheap_priority = TrustedReadPriority.SELECTED_CHEAP
                expensive_priority = TrustedReadPriority.SELECTED_EXPENSIVE
            elif category == "visible":
                cheap_priority = TrustedReadPriority.VISIBLE_CHEAP
                expensive_priority = TrustedReadPriority.VISIBLE_EXPENSIVE
            else:
                cheap_priority = TrustedReadPriority.REMAINING_CHEAP
                expensive_priority = TrustedReadPriority.REMAINING_EXPENSIVE
            cheap = self._wait_trusted_request(
                self.trusted_service.submit_cheap_run(
                    run_id,
                    priority=cheap_priority,
                    deadline=self.deadline,
                )
            )
            expensive = self._wait_trusted_request(
                self.trusted_service.submit_expensive_run(
                    run_id,
                    priority=expensive_priority,
                    deadline=self.deadline,
                )
            )
            status = cheap.as_dict()
            status.update(expensive.as_dict())
            statuses[str(guid)] = status
        return new_runs, statuses


    def _emit_new_runs_ready(self, new_runs):
        self._require_current_database_instance()
        new_runs = _bounded_runs_publication(new_runs)
        if self._require_publication_ack:
            with self._new_runs_publication_lock:
                self._new_runs_publication_error = None
            self._new_runs_publication_ack.clear()
        with self._publication_lock:
            if self._cancelled.is_set():
                return
            self.signals.new_runs_ready.emit(
                self.generation,
                self._signal_database_path,
                new_runs,
            )
        if not self._require_publication_ack:
            return

        acknowledgement_deadline = monotonic() + 10.0
        if self.deadline is not None:
            acknowledgement_deadline = min(
                acknowledgement_deadline,
                self.deadline,
            )
        while not self._new_runs_publication_ack.wait(0.05):
            if self._cancelled.is_set():
                raise InterruptedError("Database refresh cancelled.")
            if monotonic() >= acknowledgement_deadline:
                raise TimeoutError(
                    "The GUI did not publish a trusted basic-run page in time."
                )
        with self._new_runs_publication_lock:
            publication_error = self._new_runs_publication_error
        if publication_error is not None:
            raise RuntimeError(
                "The GUI could not publish a trusted basic-run page; the "
                "accepted service retirement was requested to prevent an "
                "omitted run."
            ) from publication_error
        if self._cancelled.is_set():
            raise InterruptedError("Database refresh cancelled.")


    def _emit_finished(self, new_runs, statuses, error):
        if not isinstance(error, DatabaseInstanceChangedError):
            try:
                self._require_current_database_instance()
            except DatabaseInstanceChangedError as err:
                new_runs = {}
                statuses = {}
                error = err
        new_runs = _bounded_runs_publication(new_runs)
        statuses = _bounded_runs_publication(statuses)
        error = _bounded_worker_error(error)
        with self._publication_lock:
            if self._cancelled.is_set():
                return
            try:
                self.signals.finished.emit(
                    self.generation,
                    self._signal_database_path,
                    new_runs,
                    statuses,
                    error,
                    )
            except RuntimeError as err:
                message = str(err)
                if not (
                        "wrapped C/C++ object" in message
                        and "has been deleted" in message
                        ):
                    raise


class DatabaseLoadWorker(_InterruptibleSqlWorker, QtCore.QRunnable):
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
            *,
            expected_database_instance=None,
            deadline=None,
            trusted_service=None,
            ):
        super().__init__()
        self.signals = DatabaseLoadSignals()
        self.generation = generation
        self._init_database_binding(
            database_path,
            expected_database_instance,
            deadline,
            )
        self.cloud_sync_timeout = cloud_sync_timeout
        self._cancelled = threading.Event()
        self._init_sql_interrupt()
        self._owns_trusted_service = trusted_service is None
        self.trusted_service = trusted_service or TrustedLiveReadService(
            self._read_database_path,
            expected_database_instance=self.database_instance,
            session_generation=max(1, int(generation)),
        )
        self.access_mode = None
        self.fallback_reason = None


    def cancel(self):
        """
        Marks this load as cancelled so later phases do not run.

        """
        self._cancel_read()
        if self._owns_trusted_service:
            self.trusted_service.close_async()


    def run(self):
        try:
            if self._is_cancelled():
                return

            self._require_current_database_instance()
            if database_is_likely_cloud_placeholder(self._read_database_path):
                self._prefetch_cloud_file()
            if self._is_cancelled():
                return
            self._require_current_database_instance()
            try:
                runs = self._load_trusted_runs()
                self.access_mode = TRUSTED_LIVE_MODE
            except Exception as trusted_error:
                if self._is_cancelled():
                    return
                if (
                        self.trusted_service.accepted
                        or not trusted_open_failure_allows_snapshot_fallback(
                            trusted_error
                        )
                        ):
                    raise
                self.fallback_reason = type(trusted_error).__name__
                self._close_trusted_before_fallback()
                runs = self._load_snapshot_fallback()
                self.access_mode = SNAPSHOT_FALLBACK_MODE
            if self._is_cancelled():
                return
            self._require_current_database_instance()
        except (InterruptedError, TrustedReadRequestCancelledError):
            return
        except DatabaseInstanceChangedError as err:
            if self._is_cancelled():
                return
            self._emit_finished({}, err)
            return
        except Exception as err:
            if self._is_cancelled():
                return
            log_exception("Database load worker failed", err, __name__)
            self._emit_finished({}, err)
            return

        self._emit_finished(runs, None)


    def _load_trusted_runs(self):
        self._emit_status("Opening trusted live database...")
        header = self._wait_trusted_request(
            self.trusted_service.submit_bootstrap(deadline=self.deadline)
        )
        self._emit_status("Loading basic run list...")
        runs = {}
        cursor = 0
        while True:
            if self._is_cancelled():
                raise InterruptedError("Database load cancelled.")
            page = self._wait_trusted_request(
                self.trusted_service.submit_basic_page(
                    cursor,
                    header.run_id_watermark,
                    priority=TrustedReadPriority.BOOTSTRAP,
                    deadline=self.deadline,
                )
            )
            runs.update(run_records_as_dict(page.runs))
            if page.complete:
                break
            if page.next_run_id <= cursor:
                raise RuntimeError(
                    "Trusted run-list pagination did not advance its cursor."
                )
            cursor = page.next_run_id
        return runs


    def _close_trusted_before_fallback(self):
        self.trusted_service.close_async()
        if not self.trusted_service.wait_closed(10):
            raise TimeoutError(
                "The unavailable trusted reader did not retire before fallback."
            )
        if self.trusted_service.close_error is not None:
            raise self.trusted_service.close_error


    def _load_snapshot_fallback(self):
        self._emit_status("Checking database access for snapshot fallback...")
        access_error = database_access_error(
            self._read_database_path,
            **self._read_control_kwargs(),
            )
        if self._is_cancelled():
            raise InterruptedError("Database load cancelled.")

        if (
                access_error
                and getattr(access_error, "error_type", None) not in {
                    DatabaseInstanceChangedError.__name__,
                    UnverifiableDatabaseWalError.__name__,
                    }
                and database_cloud_storage_label(self._read_database_path)
                and os.path.isfile(self._read_database_path)
                ):
            self._prefetch_cloud_file()
            if self._is_cancelled():
                raise InterruptedError("Database load cancelled.")
            self._emit_status("Checking database access for snapshot fallback...")
            access_error = database_access_error(
                self._read_database_path,
                **self._read_control_kwargs(),
                )

        if access_error:
            error_type = getattr(access_error, "error_type", None)
            if error_type == DatabaseInstanceChangedError.__name__:
                raise DatabaseInstanceChangedError(access_error)
            if error_type == UnverifiableDatabaseWalError.__name__:
                raise UnverifiableDatabaseWalError(access_error)
            raise RuntimeError(access_error)

        self._emit_status("Opening snapshot fallback read-only...")
        self._emit_status("Loading basic run list...")
        return get_runs_basic_via_sql(
            self._read_database_path,
            connection_callback=self._set_sql_connection,
            **self._read_control_kwargs(),
            ) or {}


    def _is_cancelled(self):
        return self._cancelled.is_set()


    def _emit_status(self, message):
        with self._publication_lock:
            if self._cancelled.is_set():
                return
            try:
                self.signals.status.emit(self.generation, message)
            except RuntimeError as err:
                if not self._qt_signal_was_deleted(err):
                    raise


    def _emit_finished(self, runs, error):
        if not isinstance(error, DatabaseInstanceChangedError):
            try:
                self._require_current_database_instance()
            except DatabaseInstanceChangedError as err:
                runs = {}
                error = err
        if error is None:
            runs = _bounded_runs_publication(runs)
        else:
            runs = {}
        error = _bounded_worker_error(error)
        with self._publication_lock:
            if self._cancelled.is_set():
                return
            try:
                self.signals.finished.emit(
                    self.generation,
                    self._signal_database_path,
                    runs,
                    error,
                    )
            except RuntimeError as err:
                if not self._qt_signal_was_deleted(err):
                    raise


    def _qt_signal_was_deleted(self, err):
        message = str(err)
        return "wrapped C/C++ object" in message and "has been deleted" in message


    def _prefetch_cloud_file(self):
        timeout = self.cloud_sync_timeout
        if self.deadline is not None:
            remaining = self.deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("Database load deadline exceeded.")
            timeout = min(timeout, remaining)
        prefetch_database_file_with_timeout(
            self._read_database_path,
            timeout=timeout,
            status_callback=self._emit_status,
            cancelled_callback=self._is_cancelled,
            )


class _PrioritizedRunWorker(_InterruptibleSqlWorker, QtCore.QRunnable):
    """
    Base for background workers that need selected/visible run prioritisation.

    """

    _trusted_selected_priority = TrustedReadPriority.SELECTED_CHEAP
    _trusted_visible_priority = TrustedReadPriority.VISIBLE_CHEAP
    _trusted_remaining_priority = TrustedReadPriority.REMAINING_CHEAP

    def __init__(
            self,
            generation,
            database_path,
            run_ids,
            *,
            expected_database_instance=None,
            deadline=None,
            trusted_service=None,
            ):
        super().__init__()
        self.signals = DatabaseDetailSignals()
        self.generation = generation
        self._init_database_binding(
            database_path,
            expected_database_instance,
            deadline,
            )
        self.run_ids = list(run_ids or [])
        self._default_run_order = {
            run_id: index
            for index, run_id in enumerate(self.run_ids)
            }
        self._cancelled = threading.Event()
        self._init_sql_interrupt()
        self._priority_lock = threading.Lock()
        self._priority_scores = {}
        self._promoted_run_ids = []
        self._trusted_active_run_id = None
        self._accepting_run_ids = True
        self.trusted_service = trusted_service


    def cancel(self):
        with self._priority_lock:
            self._accepting_run_ids = False
        self._cancel_read()


    def add_run_ids(self, run_ids):
        """Append trusted incremental work without replaying completed runs."""
        candidates = []
        for run_id in run_ids or ():
            try:
                candidate = int(run_id)
            except (TypeError, ValueError):
                continue
            if candidate > 0:
                candidates.append(candidate)

        with self._priority_lock:
            if not self._accepting_run_ids or self._cancelled.is_set():
                return False
            known = {
                int(run_id)
                for run_id in self.run_ids
                if str(run_id).lstrip("-").isdigit()
            }
            additions = [run_id for run_id in candidates if run_id not in known]
            for run_id in additions:
                self._default_run_order[run_id] = len(self.run_ids)
                self.run_ids.append(run_id)
                known.add(run_id)
            return True


    def prioritize_run_ids(self, run_ids):
        normalised = []
        seen = set()
        for run_id in run_ids or []:
            run_id = self._normalise_run_id(run_id)
            if run_id is None or run_id in seen:
                continue
            normalised.append(run_id)
            seen.add(run_id)

        with self._priority_lock:
            # This is a replacement snapshot of the selected/visible rows, not
            # an accumulating promotion log.  Omitted rows immediately return
            # to their stable table order, and an empty snapshot clears every
            # prior viewport/selection promotion.
            base_score = -len(normalised)
            self._priority_scores = {
                run_id: base_score + offset
                for offset, run_id in enumerate(normalised)
            }
            self._promoted_run_ids = normalised
            active_run_id = self._trusted_active_run_id
            with self._trusted_request_lock:
                request = self._trusted_request
            if active_run_id is not None and request is not None:
                requested_priority = self._trusted_priority_for_run_locked(
                    active_run_id,
                    promoted_order=normalised,
                )
                reprioritize = getattr(request, "reprioritize", None)
                if callable(reprioritize):
                    reprioritize(requested_priority)
                elif active_run_id in normalised:
                    # Compatibility with narrow request doubles that predate
                    # bidirectional trusted request priorities.
                    request.promote(requested_priority)


    def _is_cancelled(self):
        return self._cancelled.is_set()


    def _normalise_run_id(self, run_id):
        if run_id in self._default_run_order:
            return run_id

        try:
            int_run_id = int(run_id)
        except (TypeError, ValueError):
            return None

        if int_run_id in self._default_run_order:
            return int_run_id
        text_run_id = str(int_run_id)
        if text_run_id in self._default_run_order:
            return text_run_id
        return None


    def _next_priority_batch(self, done, batch_size):
        with self._priority_lock:
            priority_scores = dict(self._priority_scores)
            run_ids = tuple(self.run_ids)
            default_run_order = dict(self._default_run_order)

        candidates = [run_id for run_id in run_ids if run_id not in done]
        if not candidates:
            return []

        def sort_key(run_id):
            return (
                priority_scores.get(run_id, 0),
                default_run_order.get(run_id, 0),
                )

        candidates.sort(key=sort_key)
        return candidates[:max(1, int(batch_size or 1))]


    def _next_trusted_run(self, done):
        """Choose one run or atomically stop accepting dynamic additions."""
        with self._priority_lock:
            candidates = [run_id for run_id in self.run_ids if run_id not in done]
            if not candidates:
                self._accepting_run_ids = False
                return None
            priority_scores = dict(self._priority_scores)
            default_run_order = dict(self._default_run_order)
        candidates.sort(
            key=lambda run_id: (
                priority_scores.get(run_id, 0),
                default_run_order.get(run_id, 0),
            )
        )
        return candidates[0]


    def _trusted_priority_for_run(self, run_id, promoted_order=None):
        with self._priority_lock:
            return self._trusted_priority_for_run_locked(
                run_id,
                promoted_order=promoted_order,
            )


    def _trusted_priority_for_run_locked(self, run_id, promoted_order=None):
        """Return one trusted priority while ``_priority_lock`` is held."""

        order = list(
            self._promoted_run_ids
            if promoted_order is None
            else promoted_order
        )
        if run_id in order:
            return (
                self._trusted_selected_priority
                if order.index(run_id) == 0
                else self._trusted_visible_priority
            )
        return self._trusted_remaining_priority


    def _submit_next_trusted_run(self, done, submit):
        """Choose, submit, and install one request as one priority transition."""

        with self._priority_lock:
            candidates = [run_id for run_id in self.run_ids if run_id not in done]
            if not candidates:
                self._accepting_run_ids = False
                return None
            candidates.sort(
                key=lambda run_id: (
                    self._priority_scores.get(run_id, 0),
                    self._default_run_order.get(run_id, 0),
                )
            )
            run_id = candidates[0]
            priority = self._trusted_priority_for_run_locked(run_id)
            self._trusted_active_run_id = run_id
            try:
                request = submit(run_id, priority)
            except Exception:
                self._trusted_active_run_id = None
                raise
            # ``prioritize_run_ids`` takes these locks in the same order.  It
            # therefore observes either no active run or an installed request;
            # promotion cannot fall into the former submit/install gap.
            with self._trusted_request_lock:
                self._trusted_request = request
            return run_id, request


    def _run_trusted_details(self, status_label, submit):
        with self._priority_lock:
            total = len(self.run_ids)
        if total == 0:
            self._emit_finished(None)
            return
        done = set()
        first_error = None
        self._emit_status(f"{status_label} 0/{total}")
        while True:
            if self._is_cancelled():
                return
            submitted = self._submit_next_trusted_run(done, submit)
            if submitted is None:
                break
            run_id, request = submitted
            try:
                record = self._wait_trusted_request(request)
            except Exception as error:
                if self._is_cancelled():
                    return
                service = self.trusted_service
                if (
                        service is None
                        or isinstance(error, DatabaseInstanceChangedError)
                        or TrustedLiveReadService._terminal_session_error(error)
                        or bool(getattr(service, "closing", False))
                        or bool(getattr(service, "closed", False))
                        ):
                    raise
                if first_error is None:
                    first_error = error
                done.add(run_id)
            finally:
                with self._priority_lock:
                    if self._trusted_active_run_id == run_id:
                        self._trusted_active_run_id = None
            if self._is_cancelled():
                return
            if run_id not in done:
                self._emit_batch_ready({record.run_id: record.as_dict()})
                done.add(run_id)
            with self._priority_lock:
                total = len(self.run_ids)
            self._emit_status(f"{status_label} {len(done)}/{total}")
        self._emit_finished(first_error)


    def _emit_status(self, message):
        with self._publication_lock:
            if self._cancelled.is_set():
                return
            try:
                self.signals.status.emit(self.generation, message)
            except RuntimeError as err:
                if not self._qt_signal_was_deleted(err):
                    raise


    def _emit_batch_ready(self, details):
        self._require_current_database_instance()
        details = _bounded_runs_publication(details)
        with self._publication_lock:
            if self._cancelled.is_set():
                return
            try:
                self.signals.batch_ready.emit(
                    self.generation,
                    self._signal_database_path,
                    details,
                    )
            except RuntimeError as err:
                if not self._qt_signal_was_deleted(err):
                    raise


    def _emit_finished(self, error):
        with self._priority_lock:
            self._accepting_run_ids = False
        if not isinstance(error, DatabaseInstanceChangedError):
            try:
                self._require_current_database_instance()
            except DatabaseInstanceChangedError as err:
                error = err
        error = _bounded_worker_error(error)
        with self._publication_lock:
            if self._cancelled.is_set():
                return
            try:
                self.signals.finished.emit(
                    self.generation,
                    self._signal_database_path,
                    error,
                    )
            except RuntimeError as err:
                if not self._qt_signal_was_deleted(err):
                    raise


    def _qt_signal_was_deleted(self, err):
        message = str(err)
        return "wrapped C/C++ object" in message and "has been deleted" in message


class DatabaseDetailWorker(_PrioritizedRunWorker):
    """
    Loads cheap per-run metadata after the basic run table is visible.

    """

    def __init__(
            self,
            generation,
            database_path,
            run_ids,
            batch_size=1,
            *,
            expected_database_instance=None,
            deadline=None,
            trusted_service=None,
            ):
        super().__init__(
            generation,
            database_path,
            run_ids,
            expected_database_instance=expected_database_instance,
            deadline=deadline,
            trusted_service=trusted_service,
            )
        self.batch_size = max(1, int(batch_size or 1))


    def run(self):
        total = len(self.run_ids)
        completed = 0
        try:
            if self._is_cancelled():
                return
            if self.trusted_service is not None:
                self._run_trusted_details(
                    "Loading cheap run metadata...",
                    lambda run_id, priority:
                    self.trusted_service.submit_cheap_run(
                        int(run_id),
                        priority=priority,
                        deadline=self.deadline,
                    ),
                )
                return
            if total == 0:
                self._emit_finished(None)
                return

            self._emit_status(f"Loading run details... 0/{total}")
            done = set()
            while len(done) < total:
                if self._is_cancelled():
                    return

                batch = self._next_priority_batch(done, self.batch_size)
                if not batch:
                    break
                self._require_current_database_instance()
                batches = iter_run_detail_batches_via_sql(
                        self._read_database_path,
                        batch,
                        batch_size=self.batch_size,
                        infer_missing_shapes=False,
                        include_storage_bytes=False,
                        include_storage_estimate=True,
                        include_read_setpoint_count=True,
                        connection_callback=self._set_sql_connection,
                        **self._read_control_kwargs(),
                        )
                try:
                    for details in batches:
                        if self._is_cancelled():
                            return
                        if details:
                            self._emit_batch_ready(details)
                finally:
                    self._close_batch_iterator(batches)

                done.update(batch)
                completed = len(done)
                self._emit_status(
                    f"Loading run details... {min(completed, total)}/{total}"
                    )
        except InterruptedError:
            return
        except DatabaseInstanceChangedError as err:
            if self._is_cancelled():
                return
            self._emit_finished(err)
            return
        except Exception as err:
            if self._is_cancelled():
                return
            log_exception("Database detail worker failed", err, __name__)
            self._emit_finished(err)
            return

        self._emit_finished(None)


class DatabaseExpensiveDetailWorker(_PrioritizedRunWorker):
    """
    Loads expensive shape and storage metadata in priority order.

    """

    _trusted_selected_priority = TrustedReadPriority.SELECTED_EXPENSIVE
    _trusted_visible_priority = TrustedReadPriority.VISIBLE_EXPENSIVE
    _trusted_remaining_priority = TrustedReadPriority.REMAINING_EXPENSIVE

    def __init__(
            self,
            generation,
            database_path,
            run_ids,
            batch_size=10,
            *,
            expected_database_instance=None,
            deadline=None,
            trusted_service=None,
            ):
        super().__init__(
            generation,
            database_path,
            run_ids,
            expected_database_instance=expected_database_instance,
            deadline=deadline,
            trusted_service=trusted_service,
            )
        self.batch_size = max(1, int(batch_size or 1))


    def run(self):
        total = len(self.run_ids)
        try:
            if self._is_cancelled():
                return
            if self.trusted_service is not None:
                self._run_trusted_details(
                    "Loading counts, shapes, and sizes...",
                    lambda run_id, priority:
                    self.trusted_service.submit_expensive_run(
                        int(run_id),
                        priority=priority,
                        deadline=self.deadline,
                    ),
                )
                return
            if total == 0:
                self._emit_finished(None)
                return

            shape_done = set()
            self._emit_status(f"Loading setpoint shapes... 0/{total}")
            while len(shape_done) < total:
                if self._is_cancelled():
                    return

                batch = self._next_priority_batch(
                    shape_done,
                    batch_size=self.batch_size,
                    )
                if not batch:
                    break

                self._require_current_database_instance()
                batches = iter_run_shape_batches_via_sql(
                        self._read_database_path,
                        batch,
                        batch_size=self.batch_size,
                        connection_callback=self._set_sql_connection,
                        **self._read_control_kwargs(),
                        )
                try:
                    for shapes in batches:
                        if self._is_cancelled():
                            return

                        if shapes:
                            self._emit_batch_ready(shapes)
                finally:
                    self._close_batch_iterator(batches)

                shape_done.update(batch)
                self._emit_status(
                    f"Loading setpoint shapes... {len(shape_done)}/{total}"
                    )

            storage_done = set()
            storage_batch_size = max(25, self.batch_size)
            self._emit_status(f"Loading exact run sizes... 0/{total}")
            while len(storage_done) < total:
                if self._is_cancelled():
                    return

                batch = self._next_priority_batch(
                    storage_done,
                    batch_size=storage_batch_size,
                    )
                if not batch:
                    break

                self._require_current_database_instance()
                batches = iter_run_storage_batches_via_sql(
                        self._read_database_path,
                        batch,
                        batch_size=storage_batch_size,
                        connection_callback=self._set_sql_connection,
                        **self._read_control_kwargs(),
                        )
                try:
                    for storage in batches:
                        if self._is_cancelled():
                            return

                        if storage:
                            self._emit_batch_ready(storage)
                finally:
                    self._close_batch_iterator(batches)

                storage_done.update(batch)
                self._emit_status(
                    f"Loading exact run sizes... {len(storage_done)}/{total}"
                    )
        except InterruptedError:
            return
        except DatabaseInstanceChangedError as err:
            if self._is_cancelled():
                return
            self._emit_finished(err)
            return
        except Exception as err:
            if self._is_cancelled():
                return
            log_exception("Expensive database detail worker failed", err, __name__)
            self._emit_finished(err)
            return

        self._emit_finished(None)


class DatabaseSelectedRunWorker(_InterruptibleSqlWorker, QtCore.QRunnable):
    """Load a plain selected-run view off-GUI for either accepted read mode."""

    def __init__(
            self,
            generation,
            database_path,
            run_id,
            guid,
            trusted_service=None,
            *,
            run_metadata=None,
            expected_database_instance=None,
            deadline=None,
            ):
        super().__init__()
        if trusted_service is not None and not isinstance(
                trusted_service,
                TrustedLiveReadService,
                ):
            raise TypeError(
                "trusted_service must be a TrustedLiveReadService or None."
            )
        self.signals = DatabaseSelectedRunSignals()
        self.generation = generation
        self.run_id = int(run_id)
        self.guid = str(guid)
        self.trusted_service = trusted_service
        self.run_metadata = dict(run_metadata or {})
        self._init_database_binding(
            database_path,
            expected_database_instance,
            deadline,
        )
        self._cancelled = threading.Event()
        self._init_sql_interrupt()


    def cancel(self):
        self._cancel_read()


    def run(self):
        try:
            if self._cancelled.is_set():
                return
            self._require_current_database_instance()
            if self.trusted_service is None:
                detail = self._run_snapshot_detail()
                self._require_current_database_instance()
                self._emit_finished(detail, None)
                return
            self._wait_trusted_request(
                self.trusted_service.submit_cheap_run(
                    self.run_id,
                    priority=TrustedReadPriority.SELECTED_CHEAP,
                    deadline=self.deadline,
                )
            )
            initial_detail = self._wait_trusted_request(
                self.trusted_service.submit_selected_run(
                    self.run_id,
                    priority=TrustedReadPriority.SELECTED_CHEAP,
                    deadline=self.deadline,
                )
            )
            self._emit_progress(initial_detail)
            if self._cancelled.is_set():
                return
            self._wait_trusted_request(
                self.trusted_service.submit_expensive_run(
                    self.run_id,
                    priority=TrustedReadPriority.SELECTED_EXPENSIVE,
                    deadline=self.deadline,
                )
            )
            detail = self._wait_trusted_request(
                self.trusted_service.submit_selected_run(
                    self.run_id,
                    priority=TrustedReadPriority.SELECTED_EXPENSIVE,
                    deadline=self.deadline,
                )
            )
            self._require_current_database_instance()
        except (InterruptedError, TrustedReadRequestCancelledError):
            return
        except Exception as error:
            if self._cancelled.is_set():
                return
            log_exception("Selected-run detail worker failed", error, __name__)
            self._emit_finished(None, error)
            return
        self._emit_finished(detail, None)


    def _run_snapshot_detail(self):
        return get_snapshot_selected_run_detail(
            self._read_database_path,
            self.run_id,
            self.guid,
            self.run_metadata,
            connection_callback=self._set_sql_connection,
            **self._read_control_kwargs(),
        )


    def _emit_progress(self, detail):
        self._require_current_database_instance()
        with self._publication_lock:
            if self._cancelled.is_set():
                return
            self.signals.progress.emit(
                self.generation,
                self._signal_database_path,
                self.guid,
                detail,
            )


    def _emit_finished(self, detail, error):
        if not isinstance(error, DatabaseInstanceChangedError):
            try:
                self._require_current_database_instance()
            except DatabaseInstanceChangedError as changed_error:
                detail = None
                error = changed_error
        if error is not None:
            detail = None
        error = _bounded_worker_error(error)
        with self._publication_lock:
            if self._cancelled.is_set():
                return
            self.signals.finished.emit(
                self.generation,
                self._signal_database_path,
                self.guid,
                detail,
                error,
            )


def database_info_report(database_path):
    """
    Build a diagnostic text report for a QCoDeS database file.

    """
    return _format_database_info(_database_info_summary(database_path))


def database_info_rows(database_path):
    """
    Build display rows for the database information dialog.

    """
    groups = _database_info_groups(_database_info_summary(database_path))
    return [row for group in groups for row in group]


def _database_info_summary(database_path):
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

    return summary


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


def _database_info_groups(summary):
    latest_run = summary["latest_run"]
    latest_run_rows = [("Latest run", "None")]
    if latest_run:
        status = "completed" if latest_run.get("is_completed") else "running or incomplete"
        latest_run_rows = [
            ("Latest run ID", _display_value(latest_run.get("run_id"))),
            ("Latest run name", _display_value(latest_run.get("name"))),
            ("Latest run status", status),
            ("Latest run started", _timestamp_value(latest_run.get("run_timestamp"))),
            ("Latest run completed", _timestamp_value(latest_run.get("completed_timestamp"))),
            ("Latest run GUID", _display_value(latest_run.get("guid"))),
            ]

    page_bytes = None
    if summary["page_count"] is not None and summary["page_size"] is not None:
        page_bytes = int(summary["page_count"]) * int(summary["page_size"])

    return [
        [
            ("Database", summary["filename"]),
            ("Path", summary["path"]),
            ("Folder", summary["folder"]),
            ("File size", _format_bytes(summary["file_size"])),
            ("Last modified", _timestamp_value(summary["file_modified"])),
            ("SQLite allocated size", _format_bytes(page_bytes)),
            ],
        [
            ("Database schema version", _display_value(summary["user_version"])),
            ("SQLite application_id", _display_value(summary["application_id"])),
            ],
        [
            ("Tables", _display_value(summary["table_count"])),
            ("Experiments", _display_value(summary["experiment_count"])),
            ("Runs", _display_value(summary["run_count"])),
            ],
        latest_run_rows,
        ]


def _format_database_info(summary):
    groups = _database_info_groups(summary)
    sections = [
        "\n".join(f"{label}: {_display_value(value)}" for label, value in group)
        for group in groups
        ]
    return "\n\n".join(sections)


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
