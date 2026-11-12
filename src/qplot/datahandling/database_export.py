"""Isolated, cancellable SQLite backups of an accepted database source."""

from __future__ import annotations

import multiprocessing
import os
import sqlite3
import stat
import tempfile
import threading
import time
from contextlib import closing
from multiprocessing.connection import wait
from pathlib import Path

from .file_identity import database_instance, database_instances_differ


def require_source(instance):
    if instance.identity is None or database_instances_differ(
        instance, database_instance(instance.logical_path),
    ):
        raise RuntimeError("The source database was replaced or is no longer available.")


def _backup_child(channel, instance, live, output, deadline):
    """Only this process opens SQLite; the parent owns its temporary folder."""
    # Snapshot-reader and native-VFS scratch files must also be recoverable by
    # the parent after forced child termination.
    tempfile.tempdir = os.path.dirname(output)
    stopped = threading.Event()
    operation_deadline = [time.monotonic() + 60]
    parent = multiprocessing.parent_process()

    def watchdog():
        # Also release native source locks if the GUI process dies or a native
        # backup step never returns. Only private output can be incomplete.
        while not stopped.is_set():
            if (time.monotonic() >= min(deadline, operation_deadline[0])
                    or (parent is not None and wait([parent.sentinel], timeout=0.1))):
                os._exit(1)
            if parent is None:
                stopped.wait(0.1)

    threading.Thread(target=watchdog, name="database-export-watchdog", daemon=True).start()
    try:
        require_source(instance)

        def progress(completed, total):
            operation_deadline[0] = time.monotonic() + 10
            channel.send(("progress", completed, total))

        if live:
            import apsw

            from .trusted_live import TrustedLiveReader

            with TrustedLiveReader.open(
                instance.logical_path, expected_database_instance=instance,
            ) as source:
                with closing(apsw.Connection(output)) as target:
                    progress(0, 0)
                    source.export_backup(target, progress=progress)
                    target.execute("PRAGMA journal_mode=DELETE").fetchall()
        else:
            from .readonly import sqlite_read_only_connection

            with closing(sqlite_read_only_connection(
                instance.logical_path, expected_database_identity=instance.identity,
                deadline=time.monotonic() + 60,
            )) as source, closing(sqlite3.connect(output)) as target:
                source.backup(target, pages=256, progress=lambda _status, left, total:
                              progress(total - left, total))
                target.execute("PRAGMA journal_mode=DELETE").fetchall()
        require_source(instance)
        # Verify that the published file can be read alone, without sidecars.
        operation_deadline[0] = time.monotonic() + 60
        channel.send(("validating",))
        with closing(sqlite3.connect(
            Path(output).as_uri() + "?mode=ro&immutable=1", uri=True,
        )) as check:
            check.execute("SELECT run_id FROM runs LIMIT 1").fetchall()
            columns = {row[1] for row in check.execute("PRAGMA table_info(runs)")}
            if "raw_data_db_path" in columns and check.execute(
                "SELECT 1 FROM runs WHERE raw_data_db_path IS NOT NULL LIMIT 1"
            ).fetchone():
                raise RuntimeError(
                    "This database stores measurement data in separate files; "
                    "it cannot be exported as a standalone database."
                )
            if check.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise RuntimeError("The exported database failed its integrity check.")
        channel.send(("done",))
    except BaseException as error:
        channel.send(("error", str(error)[:500]))
    finally:
        stopped.set()
        channel.close()


def export_database_copy(filename, *, instance, live, cancelled, progress,
                         timeout=1800):
    """Write a private export stage, never an existing user database.

    The caller supplies an owned empty stage and publishes it atomically only
    after success. A spawned process isolates native locks from other readers.
    Its scratch database and sidecars stay in a parent-owned temporary folder.
    """
    def check():
        if cancelled():
            raise InterruptedError("Database export cancelled.")
        if time.monotonic() >= deadline:
            raise TimeoutError("Database export timed out. Pause acquisition and retry.")

    deadline = time.monotonic() + timeout
    check()
    require_source(instance)
    context = multiprocessing.get_context("spawn")
    with tempfile.TemporaryDirectory(prefix="qplot-database-export-") as folder:
        output = os.path.join(folder, "copy.db")
        receive, send = context.Pipe(duplex=False)
        process = context.Process(
            target=_backup_child, args=(send, instance, live, output, deadline), daemon=True,
            name="qplot-database-export",
        )
        started = False
        try:
            progress("Preparing database copy", 0, None)
            process.start()
            started = True
            send.close()
            last_message = time.monotonic()
            while True:
                check()
                if receive.poll(0.05):
                    try:
                        message = receive.recv()
                    except EOFError as error:
                        raise RuntimeError("The database export process stopped unexpectedly.") from error
                    last_message = time.monotonic()
                    if message[0] == "done":
                        break
                    if message[0] == "error":
                        raise RuntimeError(message[1])
                    if message[0] == "validating":
                        progress("Checking standalone database", 0, None)
                    else:
                        progress("Copying committed data", message[1], message[2] or None)
                elif not process.is_alive():
                    raise RuntimeError("The database export process stopped unexpectedly.")
                elif time.monotonic() - last_message > 60:
                    raise TimeoutError("Database export stopped responding. Pause acquisition and retry.")
            process.join(2)
            if process.is_alive() or process.exitcode != 0:
                raise RuntimeError("The database export process did not close cleanly.")
            require_source(instance)
            size = os.path.getsize(output)
            copied = 0
            with open(output, "rb") as source, open(filename, "r+b") as target:
                stage = os.fstat(target.fileno())
                if not stat.S_ISREG(stage.st_mode) or stage.st_size or stage.st_nlink != 1:
                    raise ValueError("Database export requires an empty, private staging file.")
                while chunk := source.read(1024 * 1024):
                    check()
                    target.write(chunk)
                    copied += len(chunk)
                    progress("Preparing exported file", copied, size)
            check()
            require_source(instance)
        finally:
            send.close()
            receive.close()
            if started or process.pid is not None:
                if process.is_alive():
                    process.terminate()
                    process.join(2)
                if process.is_alive():
                    process.kill()
                    process.join(2)
                if process.is_alive():
                    raise RuntimeError("Could not stop the database export process.")
                process.close()
