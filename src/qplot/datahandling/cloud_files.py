"""Read-only cloud-file hydration, independent of Qt and database workers.

The child performs only sequential file reads. The parent owns the overall
wait timeout and cancellation because a cloud provider can stall an OS read.
"""

import os
import queue
import stat
import subprocess
import sys
import threading
from time import perf_counter

from qplot.datahandling._cloud_prefetch import (
    DATABASE_CLOUD_SYNC_CHUNK_BYTES,
    DATABASE_CLOUD_SYNC_STATUS_INTERVAL,
    DATABASE_PREFETCH_STATUS_PREFIX,
)

DATABASE_CLOUD_SYNC_TIMEOUT_SECONDS = 120
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

    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "qplot.datahandling._cloud_prefetch", "--", database_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            )
    except OSError as err:
        raise RuntimeError(str(err)) from err

    output_queue: queue.Queue[tuple[str, str]] = queue.Queue()
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
    stream: str | None
    line: str | None

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
