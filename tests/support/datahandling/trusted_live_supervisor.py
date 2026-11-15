"""Shared fixtures and assertions for trusted live supervisor."""

from __future__ import annotations

import multiprocessing
import traceback
from dataclasses import dataclass
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

import apsw

from qplot.datahandling.trusted_live_supervisor import (
    TrustedLiveReaderSupervisor,
)

__all__ = [
    '_ApswWalWriter',
    '_assert_helper_stopped',
    '_protected_artifact_contents',
]


def _apsw_wal_writer_process(database_path: str, control: Connection) -> None:
    """Hold a WAL writer open and service spawn-safe test commands."""

    connection: apsw.Connection | None = None
    try:
        connection = apsw.Connection(database_path)
        journal_mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
        if journal_mode is None or str(journal_mode[0]).casefold() != "wal":
            raise RuntimeError(f"Could not enable WAL mode: {journal_mode!r}")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute(
            "CREATE TABLE supervisor_probe ("
            "seq INTEGER PRIMARY KEY, value TEXT NOT NULL, payload BLOB NOT NULL)"
        )
        connection.execute(
            "INSERT INTO supervisor_probe(seq, value, payload) "
            "VALUES(0, 'initial', X'')"
        )
        control.send(("ready", None))

        while True:
            command, arguments = control.recv()
            try:
                if command == "commit":
                    next_seq = connection.execute(
                        "SELECT coalesce(max(seq), -1) + 1 FROM supervisor_probe"
                    ).fetchone()[0]
                    value = str(arguments["value"])
                    payload = bytes(arguments.get("payload", b""))
                    connection.execute(
                        "INSERT INTO supervisor_probe(seq, value, payload) "
                        "VALUES(?, ?, ?)",
                        (next_seq, value, payload),
                    )
                    control.send(("ok", next_seq))
                elif command == "checkpoint":
                    mode = str(arguments.get("mode", "TRUNCATE")).upper()
                    if mode not in {"PASSIVE", "RESTART", "TRUNCATE"}:
                        raise ValueError(f"Unsupported checkpoint mode {mode!r}")
                    result = connection.execute(
                        f"PRAGMA wal_checkpoint({mode})"
                    ).fetchone()
                    control.send(("ok", tuple(result)))
                elif command == "barrier":
                    count = connection.execute(
                        "SELECT count(*) FROM supervisor_probe"
                    ).fetchone()[0]
                    control.send(("ok", count))
                elif command == "stop":
                    control.send(("ok", None))
                    break
                else:
                    raise ValueError(f"Unknown writer command {command!r}")
            except BaseException:
                control.send(("error", traceback.format_exc()))
    except EOFError:
        # Controller EOF ends this test writer; its finally block still closes the
        # database and pipe.
        pass
    except BaseException:
        try:
            control.send(("startup_error", traceback.format_exc()))
        except (BrokenPipeError, EOFError, OSError):
            # The test controller has disappeared, so the child cannot deliver its
            # startup diagnostic.
            pass
    finally:
        if connection is not None:
            connection.close(True)
        control.close()


@dataclass
class _ApswWalWriter:
    database_path: Path
    process: multiprocessing.Process
    control: Connection

    @classmethod
    def start(cls, database_path: Path) -> _ApswWalWriter:
        context = multiprocessing.get_context("spawn")
        parent_control, child_control = context.Pipe(duplex=True)
        process = context.Process(
            target=_apsw_wal_writer_process,
            args=(str(database_path), child_control),
            name="qplot-test-supervisor-wal-writer",
        )
        process.start()
        child_control.close()
        if not parent_control.poll(30):
            process.terminate()
            process.join(10)
            if process.is_alive() and hasattr(process, "kill"):
                process.kill()
                process.join(10)
            parent_control.close()
            process.close()
            raise AssertionError("The APSW WAL writer did not start in time")
        kind, payload = parent_control.recv()
        if kind != "ready":
            process.join(10)
            if process.is_alive():
                process.terminate()
                process.join(10)
            if process.is_alive() and hasattr(process, "kill"):
                process.kill()
                process.join(10)
            parent_control.close()
            process.close()
            raise AssertionError(f"The APSW WAL writer failed:\n{payload}")
        return cls(database_path, process, parent_control)

    def request(
        self,
        command: str,
        *,
        timeout: float = 30.0,
        **arguments: Any,
    ) -> Any:
        if not self.process.is_alive():
            raise AssertionError(
                f"The APSW WAL writer exited with {self.process.exitcode}"
            )
        self.control.send((command, arguments))
        if not self.control.poll(timeout):
            raise AssertionError(f"Writer command {command!r} timed out")
        kind, payload = self.control.recv()
        if kind != "ok":
            raise AssertionError(
                f"Writer command {command!r} failed in the child:\n{payload}"
            )
        return payload

    def close(self) -> None:
        if self.process.is_alive():
            try:
                self.request("stop", timeout=10)
            except (AssertionError, BrokenPipeError, EOFError, OSError):
                # A dead test writer cannot acknowledge stop; join and forced
                # termination below still clean it up.
                pass
        self.process.join(10)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(10)
        if self.process.is_alive() and hasattr(self.process, "kill"):
            self.process.kill()
            self.process.join(10)
        exitcode = self.process.exitcode
        self.control.close()
        assert not self.process.is_alive()
        self.process.close()
        assert exitcode == 0


def _protected_artifact_contents(database_path: Path) -> dict[str, bytes | None]:
    return {
        suffix: (
            None
            if not Path(f"{database_path}{suffix}").exists()
            else Path(f"{database_path}{suffix}").read_bytes()
        )
        for suffix in ("", "-wal", "-journal")
    }


def _assert_helper_stopped(
    supervisor: TrustedLiveReaderSupervisor,
    helper_pid: int,
) -> None:
    assert not supervisor.helper_alive
    assert all(
        process.pid != helper_pid for process in multiprocessing.active_children()
    )
