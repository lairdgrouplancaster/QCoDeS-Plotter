from __future__ import annotations

import ctypes
import json
import os
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from qplot import _shutdown_supervisor as supervisor

# Keep nested launcher/GUI subprocesses on the same qPlot import root as the
# test process.  In a source checkout this is ``src``; in the extracted-sdist
# validation it is the isolated environment's site-packages directory, which
# also contains the freshly built native VFS extension.
_IMPORT_ROOT = Path(supervisor.__file__).resolve().parents[1]
_LAUNCHER_SOURCE = textwrap.dedent(
    """
    import json
    import os
    import signal
    import sys
    import time
    from pathlib import Path

    from qplot import _shutdown_supervisor as supervisor

    Path(os.environ["_QPLOT_TEST_LAUNCHER_PID_PATH"]).write_text(
        str(os.getpid()), encoding="utf-8"
    )
    if os.environ.get("_QPLOT_TEST_INHERIT_SIGCHLD_IGNORED") == "1":
        signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    if os.environ.get("_QPLOT_TEST_INHERIT_SIGTERM_BLOCKED") == "1":
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
    if os.environ.get("_QPLOT_TEST_DROP_ARM_ACK") == "1":
        supervisor._send_arm_acknowledgement = lambda _channel, _frame: None
    if os.environ.get("_QPLOT_TEST_FAIL_ARM_ACK_ENCODING") == "1":
        original_encode_frame = supervisor._encode_frame

        def fail_arm_ack_encoding(frame_type, **options):
            if frame_type == supervisor._ARM_ACK:
                raise RuntimeError("exact injected ARM ACK construction failure")
            return original_encode_frame(frame_type, **options)

        supervisor._encode_frame = fail_arm_ack_encoding
    if os.environ.get("_QPLOT_TEST_FAIL_ARM_ACK_SEND") == "1":
        def fail_arm_ack_send(_channel, _frame):
            raise BrokenPipeError("exact injected ARM ACK send failure")

        supervisor._send_arm_acknowledgement = fail_arm_ack_send
    hello_decode_delay = os.environ.get("_QPLOT_TEST_HELLO_DECODE_DELAY")
    if hello_decode_delay:
        original_decode_frame = supervisor._decode_frame

        def delay_hello_decode(*args, **options):
            decoded = original_decode_frame(*args, **options)
            if decoded[0] == supervisor._HELLO:
                time.sleep(float(hello_decode_delay))
            return decoded

        supervisor._decode_frame = delay_hello_decode
    ack_marker = os.environ.get("_QPLOT_TEST_ACK_MARKER")
    if ack_marker:
        def record_unexpected_ack(_channel, _frame):
            Path(ack_marker).write_text("ACK", encoding="utf-8")

        supervisor._send_arm_acknowledgement = record_unexpected_ack

    fault_counts = {"killpg": 0, "waitpid": 0}
    if os.name != "nt" and os.environ.get("_QPLOT_TEST_FAIL_FIRST_KILLPG") == "1":
        original_killpg = os.killpg

        def fail_first_killpg(process_group, signal_number):
            fault_counts["killpg"] += 1
            if fault_counts["killpg"] == 1:
                raise InterruptedError("exact injected first killpg interruption")
            return original_killpg(process_group, signal_number)

        os.killpg = fail_first_killpg
    if os.name != "nt" and os.environ.get("_QPLOT_TEST_FAIL_FIRST_WAITPID") == "1":
        original_waitpid = os.waitpid

        def fail_first_waitpid(pid, options):
            fault_counts["waitpid"] += 1
            if fault_counts["waitpid"] == 1:
                raise InterruptedError("exact injected first waitpid interruption")
            return original_waitpid(pid, options)

        os.waitpid = fail_first_waitpid

    child_argv = [
        sys.executable,
        "-c",
        os.environ["_QPLOT_TEST_CHILD_SOURCE"],
        *json.loads(os.environ.get("_QPLOT_TEST_CHILD_ARGS", "[]")),
    ]
    database_path = os.environ.get("_QPLOT_TEST_DATABASE_PATH")
    outcome = supervisor._supervise_child_outcome(
        child_argv,
        env=os.environ,
        startup_timeout=float(
            os.environ.get("_QPLOT_TEST_STARTUP_TIMEOUT", "2.0")
        ),
        database_path=database_path,
    )
    for diagnostic in outcome.diagnostics:
        supervisor._report_launcher_failure(diagnostic)
    fault_record_path = os.environ.get("_QPLOT_TEST_FAULT_RECORD_PATH")
    if fault_record_path:
        Path(fault_record_path).write_text(
            json.dumps(fault_counts, sort_keys=True), encoding="utf-8"
        )
    if os.name != "nt" and outcome.signal_number is not None and not outcome.forced:
        signal_number = outcome.signal_number
        signal.signal(signal_number, signal.SIG_DFL)
        if hasattr(signal, "pthread_sigmask"):
            signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal_number})
        os.kill(os.getpid(), signal_number)
        os._exit(128 + signal_number)
    raise SystemExit(outcome.return_code)
    """
)


@dataclass(frozen=True)
class _LaunchResult:
    returncode: int
    stdout: str
    stderr: str
    elapsed: float
    completed_at: float
    launcher_pid: int
    child_pid: int
    related_pids: tuple[int, ...] = ()


def _subprocess_environment(**updates: str) -> dict[str, str]:
    environment = dict(os.environ)
    existing_pythonpath = environment.get("PYTHONPATH")
    pythonpath = os.fspath(_IMPORT_ROOT)
    if existing_pythonpath:
        pythonpath = os.pathsep.join((pythonpath, existing_pythonpath))
    environment.update(
        {
            "PYTHONPATH": pythonpath,
            "PYTHONUTF8": "1",
            **updates,
        }
    )
    return environment


def _process_is_running(pid: int) -> bool:
    if os.name == "nt":
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        open_process.restype = wintypes.HANDLE
        wait_for_single_object = kernel32.WaitForSingleObject
        wait_for_single_object.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        wait_for_single_object.restype = wintypes.DWORD
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        handle = open_process(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return False
        try:
            return wait_for_single_object(handle, 0) == 0x00000102
        finally:
            close_handle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _force_cleanup_pid(pid: int) -> None:
    if not _process_is_running(pid):
        return
    if os.name == "nt":
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        open_process.restype = wintypes.HANDLE
        terminate_process = kernel32.TerminateProcess
        terminate_process.argtypes = (wintypes.HANDLE, wintypes.UINT)
        terminate_process.restype = wintypes.BOOL
        wait_for_single_object = kernel32.WaitForSingleObject
        wait_for_single_object.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        wait_for_single_object.restype = wintypes.DWORD
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        handle = open_process(
            0x0001 | 0x00100000,  # PROCESS_TERMINATE | SYNCHRONIZE
            False,
            pid,
        )
        if not handle:
            return
        try:
            terminate_process(handle, 91)
            wait_for_single_object(handle, 1000)
        finally:
            close_handle(handle)
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _run_supervised_child(
    tmp_path: Path,
    child_source: str,
    *,
    child_args: tuple[str, ...] = (),
    environment_updates: dict[str, str] | None = None,
    stdin: str | None = None,
    drop_arm_ack: bool = False,
    related_pid_paths: tuple[Path, ...] = (),
    launcher_signal: int | None = None,
    child_signal: int | None = None,
    signal_ready_path: Path | None = None,
    timeout: float = 5.0,
) -> _LaunchResult:
    child_pid_path = tmp_path / "gui.pid"
    launcher_pid_path = tmp_path / "launcher.pid"
    environment = _subprocess_environment(
        _QPLOT_TEST_CHILD_SOURCE=textwrap.dedent(child_source),
        _QPLOT_TEST_CHILD_ARGS=json.dumps(child_args),
        _QPLOT_TEST_CHILD_PID_PATH=os.fspath(child_pid_path),
        _QPLOT_TEST_LAUNCHER_PID_PATH=os.fspath(launcher_pid_path),
        _QPLOT_TEST_DROP_ARM_ACK="1" if drop_arm_ack else "0",
    )
    if environment_updates:
        environment.update(environment_updates)

    started_at = time.monotonic()
    launcher = subprocess.Popen(
        [sys.executable, "-c", _LAUNCHER_SOURCE],
        env=environment,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        if launcher_signal is not None or child_signal is not None:
            if launcher_signal is not None and child_signal is not None:
                raise AssertionError("a regression may target only one process")
            if signal_ready_path is None:
                raise AssertionError("a process signal requires a readiness marker")
            marker_deadline = time.monotonic() + min(2.0, timeout)
            while not signal_ready_path.exists():
                if launcher.poll() is not None:
                    raise AssertionError(
                        "launcher exited before the signal-readiness marker"
                    )
                if time.monotonic() >= marker_deadline:
                    raise TimeoutError("signal-readiness marker was not published")
                time.sleep(0.005)
            if launcher_signal is not None:
                os.kill(launcher.pid, launcher_signal)
            else:
                assert child_signal is not None
                child_pid = int(child_pid_path.read_text(encoding="utf-8"))
                os.kill(child_pid, child_signal)
        stdout, stderr = launcher.communicate(input=stdin, timeout=timeout)
    except BaseException:
        launcher.kill()
        launcher.communicate()
        if child_pid_path.exists():
            _force_cleanup_pid(int(child_pid_path.read_text(encoding="utf-8")))
        for related_pid_path in related_pid_paths:
            if related_pid_path.exists():
                _force_cleanup_pid(int(related_pid_path.read_text(encoding="utf-8")))
        raise
    completed_at = time.monotonic()
    assert launcher_pid_path.exists(), stderr
    assert child_pid_path.exists(), stderr
    for related_pid_path in related_pid_paths:
        assert related_pid_path.exists(), (related_pid_path, stdout, stderr)
    return _LaunchResult(
        returncode=launcher.returncode,
        stdout=stdout,
        stderr=stderr,
        elapsed=completed_at - started_at,
        completed_at=completed_at,
        launcher_pid=int(launcher_pid_path.read_text(encoding="utf-8")),
        child_pid=int(child_pid_path.read_text(encoding="utf-8")),
        related_pids=tuple(
            int(path.read_text(encoding="utf-8")) for path in related_pid_paths
        ),
    )


def _assert_supervised_processes_gone(result: _LaunchResult) -> None:
    supervised_pids = (
        result.launcher_pid,
        result.child_pid,
        *result.related_pids,
    )
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if all(not _process_is_running(pid) for pid in supervised_pids):
            return
        time.sleep(0.01)
    assert {pid: _process_is_running(pid) for pid in supervised_pids} == dict.fromkeys(
        supervised_pids, False
    )


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


_WAL_WRITER_SOURCE = textwrap.dedent(
    """
    import json
    import sqlite3
    import sys

    connection = sqlite3.connect(sys.argv[1], isolation_level=None)
    journal_mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
    if journal_mode is None or str(journal_mode[0]).casefold() != "wal":
        raise AssertionError(f"WAL mode was not installed: {journal_mode!r}")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute(
        "CREATE TABLE process_tree_probe ("
        "seq INTEGER PRIMARY KEY, value TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO process_tree_probe(seq, value) VALUES(1, 'preserved')"
    )
    print(json.dumps({"ready": True}), flush=True)
    if sys.stdin.readline().strip() != "close":
        raise AssertionError("unexpected WAL-writer control command")
    connection.close()
    print(json.dumps({"closed": True}), flush=True)
    """
)


def _protected_artifact_state(
    database_path: Path,
) -> dict[str, tuple[bytes, tuple[int, ...]] | None]:
    state: dict[str, tuple[bytes, tuple[int, ...]] | None] = {}
    for suffix in ("", "-wal", "-journal"):
        path = Path(f"{database_path}{suffix}")
        if not path.exists():
            state[suffix] = None
            continue
        payload = path.read_bytes()
        metadata = path.stat()
        state[suffix] = (
            payload,
            (
                metadata.st_mode,
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_nlink,
                metadata.st_uid,
                metadata.st_gid,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
            ),
        )
    return state


def _run_public_api_driver(
    source: str,
    *,
    environment_updates: dict[str, str],
    timeout: float = 8.0,
) -> subprocess.CompletedProcess[str]:
    """Run a qplot.run() regression outside pytest's process boundary."""

    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        env=_subprocess_environment(**environment_updates),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def test_launcher_setup_failure_before_popen_is_exact(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    popen_called = False

    def fail_socket(*_args, **_kwargs):
        raise OSError("exact listener setup failure")

    def unexpected_popen(*_args, **_kwargs):
        nonlocal popen_called
        popen_called = True
        raise AssertionError("Popen must not run after listener setup failure")

    monkeypatch.setattr(supervisor.socket, "socket", fail_socket)

    result = supervisor._supervise_child(
        [sys.executable, "-c", "raise AssertionError('not launched')"],
        popen_factory=unexpected_popen,
    )

    assert result == supervisor._FORCED_SHUTDOWN_EXIT_CODE
    assert not popen_called
    assert (
        "shutdown launcher setup raised OSError: exact listener setup failure"
        in capsys.readouterr().err
    )


def test_launcher_popen_failure_is_exact(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_popen(*_args, **_kwargs):
        raise OSError("exact GUI Popen failure")

    result = supervisor._supervise_child(
        [sys.executable, "-c", "raise AssertionError('not launched')"],
        popen_factory=fail_popen,
    )

    assert result == supervisor._FORCED_SHUTDOWN_EXIT_CODE
    assert (
        "shutdown launcher GUI child launch raised OSError: exact GUI Popen failure"
        in capsys.readouterr().err
    )


def test_external_launcher_alone_kills_a_gil_holding_child(tmp_path: Path) -> None:
    record_path = tmp_path / "gil-child.json"
    result = _run_supervised_child(
        tmp_path,
        """
        import json
        import os
        import sys
        import time
        from pathlib import Path

        from qplot._shutdown_supervisor import ShutdownSupervisorClient

        client = ShutdownSupervisorClient.from_environment().connect()
        deadline = time.monotonic() + 0.25
        arm_error = client.arm(deadline)
        Path(os.environ["_QPLOT_TEST_CHILD_PID_PATH"]).write_text(
            str(os.getpid()), encoding="utf-8"
        )
        Path(os.environ["_QPLOT_TEST_RECORD_PATH"]).write_text(
            json.dumps({"deadline": deadline, "arm_error": arm_error}),
            encoding="utf-8",
        )
        # No GUI-side timer or os._exit fallback exists in this process.  The
        # long switch interval also prevents an ordinary Python timer thread
        # from running before the external deadline.
        sys.setswitchinterval(1000.0)
        while True:
            pass
        """,
        environment_updates={"_QPLOT_TEST_RECORD_PATH": os.fspath(record_path)},
    )

    record = _read_json(record_path)
    deadline = float(record["deadline"])
    assert record["arm_error"] is None
    assert result.returncode == supervisor._FORCED_SHUTDOWN_EXIT_CODE
    assert deadline - 0.03 <= result.completed_at < deadline + 0.75
    assert result.elapsed < 2.0
    _assert_supervised_processes_gone(result)


def test_forced_shutdown_terminates_the_complete_contained_process_tree(
    tmp_path: Path,
) -> None:
    """A descendant is contained, while an external sentinel is never targeted."""

    grandchild_pid_path = tmp_path / "grandchild.pid"
    sentinel = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30.0)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        result = _run_supervised_child(
            tmp_path,
            """
            import ctypes
            import os
            import subprocess
            import sys
            import time
            from pathlib import Path

            from qplot._shutdown_supervisor import ShutdownSupervisorClient

            client = ShutdownSupervisorClient.from_environment().connect()
            grandchild = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    (
                        "import os,sys,time; from pathlib import Path; "
                        "Path(sys.argv[1]).write_text(str(os.getpid()), "
                        "encoding='utf-8'); time.sleep(30.0)"
                    ),
                    os.environ["_QPLOT_TEST_GRANDCHILD_PID_PATH"],
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            grandchild_pid_path = Path(
                os.environ["_QPLOT_TEST_GRANDCHILD_PID_PATH"]
            )
            while not grandchild_pid_path.exists():
                if grandchild.poll() is not None:
                    raise AssertionError("contained grandchild exited during startup")
                time.sleep(0.005)
            deadline = time.monotonic() + 0.30
            arm_error = client.arm(deadline)
            if arm_error is not None:
                raise AssertionError(arm_error)
            Path(os.environ["_QPLOT_TEST_CHILD_PID_PATH"]).write_text(
                str(os.getpid()), encoding="utf-8"
            )
            if os.name == "nt":
                sleep = ctypes.PyDLL("kernel32", use_last_error=True).Sleep
                sleep.argtypes = (ctypes.c_ulong,)
                sleep(30_000)
            else:
                sleep = ctypes.PyDLL(None).sleep
                sleep.argtypes = (ctypes.c_uint,)
                sleep(30)
            raise AssertionError("contained GUI tree survived its hard deadline")
            """,
            environment_updates={
                "_QPLOT_TEST_GRANDCHILD_PID_PATH": os.fspath(grandchild_pid_path)
            },
            related_pid_paths=(grandchild_pid_path,),
            timeout=3.0,
        )

        assert result.returncode == supervisor._FORCED_SHUTDOWN_EXIT_CODE
        assert result.child_pid != result.related_pids[0]
        assert sentinel.pid not in (result.child_pid, *result.related_pids)
        assert sentinel.poll() is None
        _assert_supervised_processes_gone(result)
    finally:
        sentinel.terminate()
        try:
            sentinel.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            sentinel.kill()
            sentinel.wait(timeout=1.0)


def test_forced_shutdown_reaps_a_real_stuck_trusted_live_helper_and_preserves_wal(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "process-tree-live.db"
    helper_pid_path = tmp_path / "trusted-helper.pid"
    record_path = tmp_path / "trusted-helper-deadline.json"
    writer = subprocess.Popen(
        [sys.executable, "-c", _WAL_WRITER_SOURCE, os.fspath(database_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert writer.stdout is not None
        ready_line = writer.stdout.readline()
        if not ready_line:
            assert writer.stderr is not None
            raise AssertionError(
                f"external WAL writer did not start: {writer.stderr.read()}"
            )
        assert json.loads(ready_line) == {"ready": True}
        assert Path(f"{database_path}-wal").is_file()
        before = _protected_artifact_state(database_path)

        result = _run_supervised_child(
            tmp_path,
            """
            import ctypes
            import json
            import os
            import time
            from pathlib import Path

            from qplot._shutdown_supervisor import ShutdownSupervisorClient
            from qplot.datahandling.trusted_live_supervisor import (
                TrustedLiveReaderSupervisor,
            )

            client = ShutdownSupervisorClient.from_environment().connect()
            Path(os.environ["_QPLOT_TEST_CHILD_PID_PATH"]).write_text(
                str(os.getpid()), encoding="utf-8"
            )
            reader = TrustedLiveReaderSupervisor.open(
                os.environ["_QPLOT_TEST_DATABASE_PATH"],
                reply_timeout_seconds=20.0,
                shutdown_timeout_seconds=20.0,
                terminate_timeout_seconds=20.0,
                kill_timeout_seconds=20.0,
                _test_fault="hang_before_operation",
            )
            helper_pid = reader.helper_pid
            if helper_pid is None:
                raise AssertionError("trusted live helper did not expose its PID")
            Path(os.environ["_QPLOT_TEST_HELPER_PID_PATH"]).write_text(
                str(helper_pid), encoding="utf-8"
            )
            reader.submit_query("SELECT 1", timeout=20.0)
            reader._wait_for_test_notification(b"operation_started", 10.0)
            reader._wait_for_test_notification(b"operation_hang", 10.0)
            hard_deadline = time.monotonic() + 0.45
            arm_error = client.arm(hard_deadline)
            if arm_error is not None:
                raise AssertionError(arm_error)
            Path(os.environ["_QPLOT_TEST_RECORD_PATH"]).write_text(
                json.dumps(
                    {
                        "hard_deadline": hard_deadline,
                        "helper_pid": helper_pid,
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            if os.name == "nt":
                sleep = ctypes.PyDLL("kernel32", use_last_error=True).Sleep
                sleep.argtypes = (ctypes.c_ulong,)
                sleep(30_000)
            else:
                sleep = ctypes.PyDLL(None).sleep
                sleep.argtypes = (ctypes.c_uint,)
                sleep(30)
            raise AssertionError("launcher returned without killing reader tree")
            """,
            environment_updates={
                "_QPLOT_TEST_DATABASE_PATH": os.fspath(database_path),
                "_QPLOT_TEST_HELPER_PID_PATH": os.fspath(helper_pid_path),
                "_QPLOT_TEST_RECORD_PATH": os.fspath(record_path),
            },
            related_pid_paths=(helper_pid_path,),
            timeout=5.0,
        )

        record = _read_json(record_path)
        hard_deadline = float(record["hard_deadline"])
        helper_pid = int(record["helper_pid"])
        assert result.returncode == supervisor._FORCED_SHUTDOWN_EXIT_CODE
        assert result.related_pids == (helper_pid,)
        assert hard_deadline - 0.03 <= result.completed_at < hard_deadline + 0.75
        # Completion is not merely app/event-loop return: every contained OS
        # process must already have disappeared before the launcher completes.
        assert not _process_is_running(result.child_pid)
        assert not _process_is_running(helper_pid)
        assert writer.poll() is None, "external WAL writer entered containment"
        assert _protected_artifact_state(database_path) == before
        _assert_supervised_processes_gone(result)
    finally:
        if helper_pid_path.exists():
            _force_cleanup_pid(int(helper_pid_path.read_text(encoding="utf-8")))
        if writer.poll() is None:
            assert writer.stdin is not None
            writer.stdin.write("close\n")
            writer.stdin.flush()
        try:
            writer_stdout, writer_stderr = writer.communicate(timeout=2.0)
        except subprocess.TimeoutExpired:
            writer.kill()
            writer_stdout, writer_stderr = writer.communicate()
            raise
        assert writer.returncode == 0, writer_stderr
        if writer_stdout:
            assert json.loads(writer_stdout.splitlines()[-1]) == {"closed": True}


def test_never_hello_gil_holding_child_is_boundedly_terminated(
    tmp_path: Path,
) -> None:
    result = _run_supervised_child(
        tmp_path,
        """
        import ctypes
        import os
        from pathlib import Path

        # Deliberately never consume the bootstrap marker or send HELLO.
        Path(os.environ["_QPLOT_TEST_CHILD_PID_PATH"]).write_text(
            str(os.getpid()), encoding="utf-8"
        )
        if os.name == "nt":
            sleep = ctypes.PyDLL("kernel32", use_last_error=True).Sleep
            sleep.argtypes = (ctypes.c_ulong,)
            sleep(30_000)
        else:
            sleep = ctypes.PyDLL(None).sleep
            sleep.argtypes = (ctypes.c_uint,)
            sleep(30)
        raise AssertionError("never-HELLO child survived startup authority")
        """,
        # Leave enough time for a cold interpreter to publish its PID before
        # deliberately withholding HELLO.  This injected startup bound is
        # independent of every armed shutdown deadline.
        environment_updates={"_QPLOT_TEST_STARTUP_TIMEOUT": "0.75"},
        timeout=3.0,
    )

    assert result.returncode == supervisor._FORCED_SHUTDOWN_EXIT_CODE
    assert result.elapsed < 2.0
    assert "shutdown launcher child readiness" in result.stderr
    _assert_supervised_processes_gone(result)


def test_slow_hello_authentication_cannot_commit_ready_after_startup_deadline(
    tmp_path: Path,
) -> None:
    result = _run_supervised_child(
        tmp_path,
        """
        import ctypes
        import os
        from pathlib import Path

        from qplot._shutdown_supervisor import ShutdownSupervisorClient

        Path(os.environ["_QPLOT_TEST_CHILD_PID_PATH"]).write_text(
            str(os.getpid()), encoding="utf-8"
        )
        try:
            ShutdownSupervisorClient.from_environment().connect()
        except BaseException:
            # Keep the exact child live after its own short socket timeout so
            # only bounded pre-READY startup authority can finish the test.
            if os.name == "nt":
                sleep = ctypes.PyDLL("kernel32", use_last_error=True).Sleep
                sleep.argtypes = (ctypes.c_ulong,)
                sleep(30_000)
            else:
                sleep = ctypes.PyDLL(None).sleep
                sleep.argtypes = (ctypes.c_uint,)
                sleep(30)
        raise AssertionError("late READY converted startup into observe-only state")
        """,
        environment_updates={
            # Leave enough time for an isolated-environment child import under
            # full-suite load, then hold authenticated HELLO decoding beyond
            # that same absolute startup deadline.
            "_QPLOT_TEST_STARTUP_TIMEOUT": "1.0",
            "_QPLOT_TEST_HELLO_DECODE_DELAY": "1.15",
        },
        timeout=3.0,
    )

    assert result.returncode == supervisor._FORCED_SHUTDOWN_EXIT_CODE
    assert result.elapsed < 2.25
    assert "deadline expired before READY" in result.stderr
    _assert_supervised_processes_gone(result)


@pytest.mark.parametrize("exit_code", (0, 17))
def test_armed_graceful_exit_code_is_preserved(
    tmp_path: Path,
    exit_code: int,
) -> None:
    result = _run_supervised_child(
        tmp_path,
        """
        import os
        import sys
        import time
        from pathlib import Path

        from qplot._shutdown_supervisor import ShutdownSupervisorClient

        client = ShutdownSupervisorClient.from_environment().connect()
        arm_error = client.arm(time.monotonic() + 1.0)
        if arm_error is not None:
            raise AssertionError(arm_error)
        Path(os.environ["_QPLOT_TEST_CHILD_PID_PATH"]).write_text(
            str(os.getpid()), encoding="utf-8"
        )
        raise SystemExit(int(os.environ["_QPLOT_TEST_EXIT_CODE"]))
        """,
        environment_updates={"_QPLOT_TEST_EXIT_CODE": str(exit_code)},
    )

    assert result.returncode == exit_code, result.stderr
    _assert_supervised_processes_gone(result)


def test_child_exit_before_readiness_is_reaped_without_an_orphan(
    tmp_path: Path,
) -> None:
    result = _run_supervised_child(
        tmp_path,
        """
        import os
        from pathlib import Path

        # Exit before consuming the private marker, connecting to the
        # launcher, or sending the authenticated startup HELLO.
        Path(os.environ["_QPLOT_TEST_CHILD_PID_PATH"]).write_text(
            str(os.getpid()), encoding="utf-8"
        )
        raise SystemExit(19)
        """,
    )

    assert result.returncode == 19, result.stderr
    _assert_supervised_processes_gone(result)


@pytest.mark.parametrize(
    ("mode", "exit_code", "expected_diagnostic"),
    (
        (
            "eof",
            21,
            "shutdown launcher control channel closed before ARM",
        ),
        (
            "invalid",
            22,
            "invalid supervisor frame authentication",
        ),
    ),
)
def test_pre_arm_channel_failure_is_observe_only(
    tmp_path: Path,
    mode: str,
    exit_code: int,
    expected_diagnostic: str,
) -> None:
    result = _run_supervised_child(
        tmp_path,
        """
        import os
        import sys
        import time
        from pathlib import Path

        from qplot import _shutdown_supervisor as supervisor

        client = supervisor.ShutdownSupervisorClient.from_environment().connect()
        Path(os.environ["_QPLOT_TEST_CHILD_PID_PATH"]).write_text(
            str(os.getpid()), encoding="utf-8"
        )
        if os.environ["_QPLOT_TEST_MODE"] == "invalid":
            client._require_channel().sendall(b"x" * supervisor._FRAME_SIZE)
        client.close()
        time.sleep(0.15)
        raise SystemExit(int(os.environ["_QPLOT_TEST_EXIT_CODE"]))
        """,
        environment_updates={
            "_QPLOT_TEST_MODE": mode,
            "_QPLOT_TEST_EXIT_CODE": str(exit_code),
        },
    )

    assert result.returncode == exit_code
    assert result.elapsed >= 0.12
    assert expected_diagnostic in result.stderr
    _assert_supervised_processes_gone(result)


@pytest.mark.parametrize("mode", ("eof", "invalid", "duplicate", "lost-ack"))
def test_post_arm_traffic_cannot_cancel_or_extend_first_deadline(
    tmp_path: Path,
    mode: str,
) -> None:
    record_path = tmp_path / f"post-arm-{mode}.json"
    result = _run_supervised_child(
        tmp_path,
        """
        import json
        import os
        import secrets
        import socket
        import sys
        import time
        from pathlib import Path

        from qplot import _shutdown_supervisor as supervisor

        client = supervisor.ShutdownSupervisorClient.from_environment().connect()
        first_deadline = time.monotonic() + 0.25
        later_deadline = first_deadline + 1.0
        client._send_arm(first_deadline)
        channel = client._require_channel()
        mode = os.environ["_QPLOT_TEST_MODE"]
        if mode == "eof":
            channel.shutdown(socket.SHUT_WR)
        elif mode == "invalid":
            channel.sendall(b"!" * supervisor._FRAME_SIZE)
        elif mode == "duplicate":
            channel.sendall(
                supervisor._encode_arm(
                    client._bootstrap.authentication_key,
                    client._bootstrap.session_nonce,
                    secrets.token_bytes(supervisor._NONCE_BYTES),
                    later_deadline,
                )
            )
        Path(os.environ["_QPLOT_TEST_CHILD_PID_PATH"]).write_text(
            str(os.getpid()), encoding="utf-8"
        )
        Path(os.environ["_QPLOT_TEST_RECORD_PATH"]).write_text(
            json.dumps(
                {
                    "first_deadline": first_deadline,
                    "later_deadline": later_deadline,
                }
            ),
            encoding="utf-8",
        )
        sys.setswitchinterval(1000.0)
        while True:
            pass
        """,
        environment_updates={
            "_QPLOT_TEST_MODE": mode,
            "_QPLOT_TEST_RECORD_PATH": os.fspath(record_path),
        },
        drop_arm_ack=mode == "lost-ack",
    )

    record = _read_json(record_path)
    first_deadline = float(record["first_deadline"])
    later_deadline = float(record["later_deadline"])
    assert result.returncode == supervisor._FORCED_SHUTDOWN_EXIT_CODE
    assert first_deadline - 0.03 <= result.completed_at < first_deadline + 0.75
    assert result.completed_at < later_deadline
    _assert_supervised_processes_gone(result)


def test_already_expired_arm_kills_before_acknowledgement(tmp_path: Path) -> None:
    ack_marker = tmp_path / "unexpected-ack"
    result = _run_supervised_child(
        tmp_path,
        """
        import os
        import sys
        import time
        from pathlib import Path

        from qplot._shutdown_supervisor import ShutdownSupervisorClient

        client = ShutdownSupervisorClient.from_environment().connect()
        Path(os.environ["_QPLOT_TEST_CHILD_PID_PATH"]).write_text(
            str(os.getpid()), encoding="utf-8"
        )
        client._send_arm(time.monotonic() - 1.0)
        sys.setswitchinterval(1000.0)
        while True:
            pass
        """,
        environment_updates={"_QPLOT_TEST_ACK_MARKER": os.fspath(ack_marker)},
    )

    assert result.returncode == supervisor._FORCED_SHUTDOWN_EXIT_CODE
    assert not ack_marker.exists()
    _assert_supervised_processes_gone(result)


@pytest.mark.parametrize(
    ("failure_environment", "expected_diagnostic"),
    (
        (
            "_QPLOT_TEST_FAIL_ARM_ACK_ENCODING",
            "exact injected ARM ACK construction failure",
        ),
        (
            "_QPLOT_TEST_FAIL_ARM_ACK_SEND",
            "exact injected ARM ACK send failure",
        ),
    ),
)
def test_arm_ack_failure_remains_armed_for_the_original_deadline(
    tmp_path: Path,
    failure_environment: str,
    expected_diagnostic: str,
) -> None:
    record_path = tmp_path / "ack-construction-failure.json"
    result = _run_supervised_child(
        tmp_path,
        """
        import ctypes
        import json
        import os
        import time
        from pathlib import Path

        from qplot._shutdown_supervisor import ShutdownSupervisorClient

        client = ShutdownSupervisorClient.from_environment().connect()
        deadline = time.monotonic() + 0.30
        Path(os.environ["_QPLOT_TEST_CHILD_PID_PATH"]).write_text(
            str(os.getpid()), encoding="utf-8"
        )
        Path(os.environ["_QPLOT_TEST_RECORD_PATH"]).write_text(
            json.dumps({"deadline": deadline}), encoding="utf-8"
        )
        client._send_arm(deadline)
        if os.name == "nt":
            sleep = ctypes.PyDLL("kernel32", use_last_error=True).Sleep
            sleep.argtypes = (ctypes.c_ulong,)
            sleep(30_000)
        else:
            sleep = ctypes.PyDLL(None).sleep
            sleep.argtypes = (ctypes.c_uint,)
            sleep(30)
        raise AssertionError("ACK-construction failure disarmed the launcher")
        """,
        environment_updates={
            failure_environment: "1",
            "_QPLOT_TEST_RECORD_PATH": os.fspath(record_path),
        },
        timeout=2.0,
    )

    deadline = float(_read_json(record_path)["deadline"])
    assert result.returncode == supervisor._FORCED_SHUTDOWN_EXIT_CODE
    assert deadline - 0.03 <= result.completed_at < deadline + 0.75
    assert expected_diagnostic in result.stderr
    _assert_supervised_processes_gone(result)


@pytest.mark.skipif(os.name == "nt", reason="POSIX SIGCHLD disposition regression")
def test_inherited_sigchld_ignored_cannot_release_the_exact_child_identity(
    tmp_path: Path,
) -> None:
    sentinel = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30.0)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        result = _run_supervised_child(
            tmp_path,
            """
            import os
            import time
            from pathlib import Path

            from qplot._shutdown_supervisor import ShutdownSupervisorClient

            client = ShutdownSupervisorClient.from_environment().connect()
            arm_error = client.arm(time.monotonic() + 1.0)
            if arm_error is not None:
                raise AssertionError(arm_error)
            Path(os.environ["_QPLOT_TEST_CHILD_PID_PATH"]).write_text(
                str(os.getpid()), encoding="utf-8"
            )
            raise SystemExit(23)
            """,
            environment_updates={"_QPLOT_TEST_INHERIT_SIGCHLD_IGNORED": "1"},
        )

        assert result.returncode == 23, result.stderr
        assert sentinel.poll() is None
        assert sentinel.pid != result.child_pid
        _assert_supervised_processes_gone(result)
    finally:
        sentinel.terminate()
        sentinel.wait(timeout=1.0)


def test_child_exit_immediately_before_deadline_does_not_signal_sentinel(
    tmp_path: Path,
) -> None:
    sentinel = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(5.0)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    record_path = tmp_path / "near-deadline.json"
    try:
        result = _run_supervised_child(
            tmp_path,
            """
            import json
            import os
            import time
            from pathlib import Path

            from qplot._shutdown_supervisor import ShutdownSupervisorClient

            client = ShutdownSupervisorClient.from_environment().connect()
            deadline = time.monotonic() + 0.30
            arm_error = client.arm(deadline)
            if arm_error is not None:
                raise AssertionError(arm_error)
            Path(os.environ["_QPLOT_TEST_CHILD_PID_PATH"]).write_text(
                str(os.getpid()), encoding="utf-8"
            )
            Path(os.environ["_QPLOT_TEST_RECORD_PATH"]).write_text(
                json.dumps({"deadline": deadline}), encoding="utf-8"
            )
            time.sleep(max(0.0, deadline - time.monotonic() - 0.05))
            raise SystemExit(31)
            """,
            environment_updates={"_QPLOT_TEST_RECORD_PATH": os.fspath(record_path)},
        )
        deadline = float(_read_json(record_path)["deadline"])
        time.sleep(max(0.0, deadline + 0.08 - time.monotonic()))

        assert result.returncode == 31, result.stderr
        assert result.child_pid != sentinel.pid
        assert sentinel.poll() is None
        _assert_supervised_processes_gone(result)
    finally:
        sentinel.terminate()
        try:
            sentinel.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            sentinel.kill()
            sentinel.wait(timeout=1.0)


@pytest.mark.skipif(os.name == "nt", reason="POSIX wait-status regression")
def test_deadline_race_preserves_an_already_exited_child_status() -> None:
    child = subprocess.Popen(
        [sys.executable, "-c", "raise SystemExit(31)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    process_group = supervisor._PosixProcessGroup()
    process_group.assign(child)
    # Do not poll/wait: the direct child must remain unreaped and identity-bound.
    time.sleep(0.10)

    outcome = supervisor._wait_for_armed_posix_child(
        child,
        time.monotonic() - 1.0,
        process_group,
        supervisor._LauncherSignalState(),
    )

    assert outcome == supervisor._SupervisionOutcome(31, forced=False)
    assert child.returncode == 31


@pytest.mark.skipif(os.name == "nt", reason="POSIX zombie-group regression")
def test_known_child_exit_accepts_darwin_eperm_during_group_stabilization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExitedChild:
        pid = 4312
        returncode: int | None = None

    class ZombieOnlyGroup:
        def __init__(self) -> None:
            self.termination_calls = 0

        def terminate(self) -> None:
            self.termination_calls += 1
            if self.termination_calls > 1:
                raise PermissionError("exact Darwin zombie-only EPERM")

        def active(self) -> bool:
            return False

    child = ExitedChild()
    process_group = ZombieOnlyGroup()
    monkeypatch.setattr(
        supervisor.os,
        "waitpid",
        lambda pid, options: (pid, 0),
    )

    outcome = supervisor._terminate_and_reap_posix_child(
        child,
        process_group,  # type: ignore[arg-type]
        child_exit_observed=True,
    )

    assert outcome == supervisor._SupervisionOutcome(0)
    assert child.returncode == 0
    assert process_group.termination_calls == 2


@pytest.mark.skipif(os.name == "nt", reason="POSIX absolute-deadline regression")
@pytest.mark.parametrize("wait_error", (InterruptedError, ChildProcessError))
def test_posix_observation_retry_cannot_cross_deadline_before_group_kill(
    monkeypatch: pytest.MonkeyPatch,
    wait_error: type[BaseException],
) -> None:
    class FakeTime:
        def __init__(self) -> None:
            self.now = 100.0

        def monotonic(self) -> float:
            return self.now

        def sleep(self, delay: float) -> None:
            self.now += delay

    class LiveChild:
        pid = 4312
        returncode: int | None = None

    class OwnedGroup:
        def terminate(self) -> None:
            events.append(("terminate", clock.now))

        def active(self) -> bool:
            events.append(("active", clock.now))
            return False

    clock = FakeTime()
    events: list[tuple[str, float]] = []
    deadline = clock.now + 0.010

    def failing_waitid(idtype: int, pid: int, options: int) -> None:
        del idtype, pid, options
        events.append(("waitid", clock.now))
        # The retained-handle observation began before the deadline but its
        # retry result arrived after it.  The next external action must kill.
        clock.now += 0.011
        raise wait_error("exact observation retry")

    original_materialize = supervisor._materialize_termination_diagnostics

    def recording_materialize(*args, **kwargs) -> list[str]:
        events.append(("diagnostics", clock.now))
        return original_materialize(*args, **kwargs)

    monkeypatch.setattr(supervisor, "time", clock)
    monkeypatch.setattr(supervisor.os, "waitid", failing_waitid)
    monkeypatch.setattr(
        supervisor.os,
        "waitpid",
        lambda pid, options: (pid, signal.SIGKILL),
    )
    monkeypatch.setattr(
        supervisor,
        "_materialize_termination_diagnostics",
        recording_materialize,
    )

    outcome = supervisor._wait_for_armed_posix_child(
        LiveChild(),
        deadline,
        OwnedGroup(),  # type: ignore[arg-type]
        supervisor._LauncherSignalState(),
    )

    assert outcome == supervisor._SupervisionOutcome(70, forced=True)
    assert events[0] == ("waitid", 100.0)
    assert events[1][0] == "terminate"
    assert events[1][1] >= deadline
    assert events[2][0] == "diagnostics"


@pytest.mark.skipif(os.name == "nt", reason="POSIX absolute-deadline regression")
@pytest.mark.parametrize("wait_advance", (0.009, 0.011))
def test_posix_live_observation_rechecks_deadline_before_sleep(
    monkeypatch: pytest.MonkeyPatch,
    wait_advance: float,
) -> None:
    class FakeTime:
        def __init__(self) -> None:
            self.now = 100.0

        def monotonic(self) -> float:
            return self.now

        def sleep(self, delay: float) -> None:
            self.now += delay

    class LiveChild:
        pid = 4312
        returncode: int | None = None

    class OwnedGroup:
        def terminate(self) -> None:
            events.append(("terminate", clock.now))

        def active(self) -> bool:
            events.append(("active", clock.now))
            return False

    clock = FakeTime()
    events: list[tuple[str, float]] = []
    deadline = clock.now + 0.010

    def delayed_live_waitid(idtype: int, pid: int, options: int) -> None:
        del idtype, pid, options
        events.append(("waitid", clock.now))
        clock.now += wait_advance
        return None

    original_materialize = supervisor._materialize_termination_diagnostics

    def recording_materialize(*args, **kwargs) -> list[str]:
        events.append(("diagnostics", clock.now))
        return original_materialize(*args, **kwargs)

    monkeypatch.setattr(supervisor, "time", clock)
    monkeypatch.setattr(supervisor.os, "waitid", delayed_live_waitid)
    monkeypatch.setattr(
        supervisor.os,
        "waitpid",
        lambda pid, options: (pid, signal.SIGKILL),
    )
    monkeypatch.setattr(
        supervisor,
        "_materialize_termination_diagnostics",
        recording_materialize,
    )

    outcome = supervisor._wait_for_armed_posix_child(
        LiveChild(),
        deadline,
        OwnedGroup(),  # type: ignore[arg-type]
        supervisor._LauncherSignalState(),
    )

    assert outcome == supervisor._SupervisionOutcome(70, forced=True)
    assert events[0] == ("waitid", 100.0)
    assert events[1][0] == "terminate"
    assert events[1][1] >= deadline
    assert events[2][0] == "diagnostics"


@pytest.mark.skipif(os.name == "nt", reason="POSIX kill/reap retry regression")
@pytest.mark.parametrize(
    ("fault_environment", "counter_name", "expected_diagnostic"),
    (
        (
            "_QPLOT_TEST_FAIL_FIRST_KILLPG",
            "killpg",
            "shutdown launcher POSIX process-group termination raised "
            "InterruptedError: exact injected first killpg interruption",
        ),
        (
            "_QPLOT_TEST_FAIL_FIRST_WAITPID",
            "waitpid",
            "shutdown launcher POSIX child reap raised InterruptedError: "
            "exact injected first waitpid interruption",
        ),
    ),
)
def test_first_posix_tree_kill_or_reap_interruption_is_retried(
    tmp_path: Path,
    fault_environment: str,
    counter_name: str,
    expected_diagnostic: str,
) -> None:
    fault_record_path = tmp_path / f"{counter_name}-retry.json"
    result = _run_supervised_child(
        tmp_path,
        """
        import ctypes
        import os
        import time
        from pathlib import Path

        from qplot._shutdown_supervisor import ShutdownSupervisorClient

        client = ShutdownSupervisorClient.from_environment().connect()
        arm_error = client.arm(time.monotonic() + 0.25)
        if arm_error is not None:
            raise AssertionError(arm_error)
        Path(os.environ["_QPLOT_TEST_CHILD_PID_PATH"]).write_text(
            str(os.getpid()), encoding="utf-8"
        )
        sleep = ctypes.PyDLL(None).sleep
        sleep.argtypes = (ctypes.c_uint,)
        sleep(30)
        raise AssertionError("injected interruption released process ownership")
        """,
        environment_updates={
            fault_environment: "1",
            "_QPLOT_TEST_FAULT_RECORD_PATH": os.fspath(fault_record_path),
        },
        timeout=2.0,
    )

    fault_counts = _read_json(fault_record_path)
    assert result.returncode == supervisor._FORCED_SHUTDOWN_EXIT_CODE
    assert int(fault_counts[counter_name]) >= 2
    assert expected_diagnostic in result.stderr
    _assert_supervised_processes_gone(result)


@pytest.mark.skipif(os.name == "nt", reason="POSIX launcher signal regression")
@pytest.mark.parametrize("launcher_signal", (signal.SIGTERM, signal.SIGINT))
@pytest.mark.parametrize("phase", ("ready-pre-arm", "armed"))
def test_targeted_launcher_signal_terminates_and_reaps_the_owned_tree(
    tmp_path: Path,
    launcher_signal: int,
    phase: str,
) -> None:
    signal_ready_path = tmp_path / f"{phase}-{launcher_signal}.ready"
    result = _run_supervised_child(
        tmp_path,
        """
        import ctypes
        import os
        import signal
        import time
        from pathlib import Path

        from qplot._shutdown_supervisor import ShutdownSupervisorClient

        client = ShutdownSupervisorClient.from_environment().connect()
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        Path(os.environ["_QPLOT_TEST_CHILD_PID_PATH"]).write_text(
            str(os.getpid()), encoding="utf-8"
        )
        if os.environ["_QPLOT_TEST_SIGNAL_PHASE"] == "armed":
            arm_error = client.arm(time.monotonic() + 3.0)
            if arm_error is not None:
                raise AssertionError(arm_error)
        Path(os.environ["_QPLOT_TEST_SIGNAL_READY_PATH"]).write_text(
            "ready", encoding="utf-8"
        )
        sleep = ctypes.PyDLL(None).sleep
        sleep.argtypes = (ctypes.c_uint,)
        sleep(30)
        raise AssertionError("targeted launcher signal was ignored")
        """,
        environment_updates={
            "_QPLOT_TEST_SIGNAL_PHASE": phase,
            "_QPLOT_TEST_SIGNAL_READY_PATH": os.fspath(signal_ready_path),
        },
        launcher_signal=launcher_signal,
        signal_ready_path=signal_ready_path,
        timeout=2.0,
    )

    assert result.returncode == -launcher_signal, result.stderr
    assert result.elapsed < 1.5
    _assert_supervised_processes_gone(result)


@pytest.mark.skipif(os.name == "nt", reason="POSIX inherited signal-mask regression")
def test_inherited_blocked_sigterm_is_unblocked_for_launcher_and_child(
    tmp_path: Path,
) -> None:
    signal_ready_path = tmp_path / "blocked-sigterm.ready"
    result = _run_supervised_child(
        tmp_path,
        """
        import ctypes
        import os
        import signal
        from pathlib import Path

        from qplot._shutdown_supervisor import ShutdownSupervisorClient

        ShutdownSupervisorClient.from_environment().connect()
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        Path(os.environ["_QPLOT_TEST_CHILD_PID_PATH"]).write_text(
            str(os.getpid()), encoding="utf-8"
        )
        Path(os.environ["_QPLOT_TEST_SIGNAL_READY_PATH"]).write_text(
            "ready", encoding="utf-8"
        )
        sleep = ctypes.PyDLL(None).sleep
        sleep.argtypes = (ctypes.c_uint,)
        sleep(30)
        raise AssertionError("inherited blocked SIGTERM remained ineffective")
        """,
        environment_updates={
            "_QPLOT_TEST_INHERIT_SIGTERM_BLOCKED": "1",
            "_QPLOT_TEST_SIGNAL_READY_PATH": os.fspath(signal_ready_path),
        },
        launcher_signal=signal.SIGTERM,
        signal_ready_path=signal_ready_path,
        timeout=2.0,
    )

    assert result.returncode == -signal.SIGTERM, result.stderr
    assert result.elapsed < 1.5
    _assert_supervised_processes_gone(result)


@pytest.mark.skipif(os.name == "nt", reason="POSIX child signal-status regression")
def test_gui_sigterm_is_propagated_as_a_real_launcher_signal_status(
    tmp_path: Path,
) -> None:
    signal_ready_path = tmp_path / "gui-sigterm.ready"
    result = _run_supervised_child(
        tmp_path,
        """
        import ctypes
        import os
        import signal
        from pathlib import Path

        from qplot._shutdown_supervisor import ShutdownSupervisorClient

        ShutdownSupervisorClient.from_environment().connect()
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        Path(os.environ["_QPLOT_TEST_CHILD_PID_PATH"]).write_text(
            str(os.getpid()), encoding="utf-8"
        )
        Path(os.environ["_QPLOT_TEST_SIGNAL_READY_PATH"]).write_text(
            "ready", encoding="utf-8"
        )
        sleep = ctypes.PyDLL(None).sleep
        sleep.argtypes = (ctypes.c_uint,)
        sleep(30)
        raise AssertionError("GUI SIGTERM was not delivered")
        """,
        environment_updates={
            "_QPLOT_TEST_SIGNAL_READY_PATH": os.fspath(signal_ready_path)
        },
        child_signal=signal.SIGTERM,
        signal_ready_path=signal_ready_path,
        timeout=2.0,
    )

    assert result.returncode == -signal.SIGTERM, result.stderr
    assert result.returncode != 128 + signal.SIGTERM
    assert result.returncode != 256 - signal.SIGTERM
    _assert_supervised_processes_gone(result)


def test_child_inherits_argv_environment_and_standard_streams_and_consumes_marker(
    tmp_path: Path,
) -> None:
    record_path = tmp_path / "inheritance.json"
    database_path = tmp_path / "database path 雪.db"
    result = _run_supervised_child(
        tmp_path,
        """
        import json
        import os
        import sys
        from pathlib import Path

        from qplot import _shutdown_supervisor as supervisor

        client = supervisor.ShutdownSupervisorClient.from_environment().connect()
        standard_input = sys.stdin.readline().rstrip("\\n")
        record = {
            "argv": sys.argv[1:],
            "custom_environment": os.environ["QPLOT_TEST_PRESERVED_ENV"],
            "marker_present": (
                supervisor._BOOTSTRAP_ENVIRONMENT_KEY in os.environ
            ),
            "database_path": client.database_path,
            "stdin": standard_input,
        }
        Path(os.environ["_QPLOT_TEST_CHILD_PID_PATH"]).write_text(
            str(os.getpid()), encoding="utf-8"
        )
        Path(os.environ["_QPLOT_TEST_RECORD_PATH"]).write_text(
            json.dumps(record), encoding="utf-8"
        )
        print("GUI_STDOUT:" + standard_input, flush=True)
        print("GUI_STDERR:" + standard_input, file=sys.stderr, flush=True)
        """,
        child_args=("argument with spaces", "雪", "--qt-looking-option"),
        environment_updates={
            "_QPLOT_TEST_RECORD_PATH": os.fspath(record_path),
            "_QPLOT_TEST_DATABASE_PATH": os.fspath(database_path),
            "QPLOT_TEST_PRESERVED_ENV": "preserved-π",
        },
        stdin="inherited standard input\n",
    )

    record = _read_json(record_path)
    assert result.returncode == 0
    assert record == {
        "argv": ["argument with spaces", "雪", "--qt-looking-option"],
        "custom_environment": "preserved-π",
        "marker_present": False,
        "database_path": os.fspath(database_path),
        "stdin": "inherited standard input",
    }
    assert "GUI_STDOUT:inherited standard input" in result.stdout
    assert "GUI_STDERR:inherited standard input" in result.stderr
    _assert_supervised_processes_gone(result)


def test_launch_gui_preserves_original_argv_behind_private_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def capture(child_argv, **options):
        captured["child_argv"] = child_argv
        captured.update(options)
        return supervisor._SupervisionOutcome(29)

    monkeypatch.setattr(supervisor, "_supervise_child_outcome", capture)
    result = supervisor.launch_gui(
        ["qplot-original", "database path.db", "--platform", "offscreen"],
        database_path="explicit path.db",
    )

    assert result == 29
    assert captured["child_argv"] == [
        sys.executable,
        "-m",
        "qplot._shutdown_supervisor",
        supervisor._GUI_CHILD_SENTINEL,
        "qplot-original",
        "database path.db",
        "--platform",
        "offscreen",
    ]
    assert captured["env"] is os.environ
    assert captured["database_path"] == "explicit path.db"


@pytest.mark.parametrize(
    ("outcome", "expected_forced_exits"),
    (
        (supervisor._SupervisionOutcome(70, forced=False), []),
        (supervisor._SupervisionOutcome(70, forced=True), [70]),
    ),
)
def test_launch_gui_hard_exits_only_for_a_forced_outcome(
    monkeypatch: pytest.MonkeyPatch,
    outcome: supervisor._SupervisionOutcome,
    expected_forced_exits: list[int],
) -> None:
    forced_exits: list[int] = []
    monkeypatch.setattr(
        supervisor,
        "_supervise_child_outcome",
        lambda *_args, **_kwargs: outcome,
    )

    result = supervisor.launch_gui(
        ["qplot-original"],
        _force_launcher_exit=forced_exits.append,
    )

    assert result == 70
    assert forced_exits == expected_forced_exits


@pytest.mark.skipif(os.name == "nt", reason="POSIX signal-status propagation")
def test_launch_gui_propagates_an_ordinary_child_signal_as_a_real_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    propagated_signals: list[int] = []
    monkeypatch.setattr(
        supervisor,
        "_supervise_child_outcome",
        lambda *_args, **_kwargs: supervisor._SupervisionOutcome(
            -signal.SIGTERM,
            forced=False,
            signal_number=signal.SIGTERM,
        ),
    )

    result = supervisor.launch_gui(
        ["qplot-original"],
        _force_launcher_signal=propagated_signals.append,
    )

    assert result == -signal.SIGTERM
    assert propagated_signals == [signal.SIGTERM]


def test_launch_gui_publishes_retained_exact_supervisor_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        supervisor,
        "_supervise_child_outcome",
        lambda *_args, **_kwargs: supervisor._SupervisionOutcome(
            17,
            diagnostics=("exact retained launcher diagnostic",),
        ),
    )

    assert supervisor.launch_gui(["qplot-original"]) == 17
    assert "exact retained launcher diagnostic" in capsys.readouterr().err


def test_diagnostic_publication_has_a_strict_blocking_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    entered = threading.Event()

    def block_report(_diagnostic: str) -> None:
        entered.set()
        release.wait(1.0)

    monkeypatch.setattr(supervisor, "_report_launcher_failure", block_report)
    started_at = time.monotonic()
    try:
        published = supervisor._publish_outcome_diagnostics(
            supervisor._SupervisionOutcome(
                17,
                diagnostics=("exact retained launcher diagnostic",),
            )
        )
        elapsed = time.monotonic() - started_at
        assert entered.is_set()
        assert not published
        assert elapsed < 0.15
    finally:
        release.set()


def test_distinct_termination_retry_diagnostics_are_memory_bounded() -> None:
    diagnostics: list[str] = []

    for index in range(supervisor._TERMINATION_DIAGNOSTIC_LIMIT * 3):
        supervisor._record_termination_diagnostic(
            diagnostics,
            "exact injected retry",
            OSError(f"failure {index}"),
        )

    assert len(diagnostics) == supervisor._TERMINATION_DIAGNOSTIC_LIMIT
    assert diagnostics[0].endswith("OSError: failure 0")
    assert "additional distinct termination diagnostics omitted" in diagnostics[-1]


def test_launch_gui_hard_exits_if_diagnostic_sink_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forced_exits: list[int] = []
    monkeypatch.setattr(
        supervisor,
        "_supervise_child_outcome",
        lambda *_args, **_kwargs: supervisor._SupervisionOutcome(
            17,
            diagnostics=("blocked diagnostic",),
        ),
    )
    monkeypatch.setattr(
        supervisor,
        "_publish_outcome_diagnostics",
        lambda _outcome: False,
    )

    assert (
        supervisor.launch_gui(
            ["qplot-original"],
            _force_launcher_exit=forced_exits.append,
        )
        == 17
    )
    assert forced_exits == [17]


def test_return_objects_mode_is_intentionally_in_process_and_caller_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Library callers retain objects without acquiring launcher containment."""

    import qplot
    import qplot.__main__ as qplot_entrypoint

    application = object()
    window = object()
    direct_calls: list[tuple[bool, str | None]] = []

    def run_direct(*, return_objects=False, database_path=None, **_options):
        direct_calls.append((return_objects, database_path))
        return application, window

    def unexpected_launcher(*_args, **_kwargs):
        raise AssertionError("return_objects=True must remain caller-owned")

    monkeypatch.setattr(qplot_entrypoint, "_run_gui", run_direct)
    monkeypatch.setattr(supervisor, "launch_gui", unexpected_launcher)

    result = qplot.run(
        return_objects=True,
        database_path="caller-owned.db",
    )

    assert result == (application, window)
    assert direct_calls == [(True, "caller-owned.db")]


@pytest.mark.skipif(os.name == "nt", reason="POSIX foreign-reaper regression")
def test_public_run_survives_a_deterministic_foreign_waitpid_reaper(
    tmp_path: Path,
) -> None:
    """The caller never needs the dedicated launcher's waitpid status."""

    record_path = tmp_path / "public-foreign-reaper.json"
    gui_pid_path = tmp_path / "public-gui.pid"
    helper_pid_path = tmp_path / "public-helper.pid"
    gui_source = textwrap.dedent(
        """
        import os
        import subprocess
        import sys
        import time
        from pathlib import Path

        from qplot._shutdown_supervisor import ShutdownSupervisorClient

        ShutdownSupervisorClient.from_environment().connect()
        Path(os.environ["_QPLOT_TEST_GUI_PID_PATH"]).write_text(
            str(os.getpid()), encoding="utf-8"
        )
        helper = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import os,sys,time; from pathlib import Path; "
                    "Path(sys.argv[1]).write_text(str(os.getpid()), "
                    "encoding='utf-8'); time.sleep(30.0)"
                ),
                os.environ["_QPLOT_TEST_HELPER_PID_PATH"],
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        helper_path = Path(os.environ["_QPLOT_TEST_HELPER_PID_PATH"])
        deadline = time.monotonic() + 2.0
        while not helper_path.exists():
            if helper.poll() is not None:
                raise AssertionError("contained helper exited during startup")
            if time.monotonic() >= deadline:
                raise TimeoutError("contained helper PID was not published")
            time.sleep(0.005)
        raise SystemExit(17)
        """
    )
    result = _run_public_api_driver(
        """
        import json
        import os
        import sys
        import threading
        import time
        from pathlib import Path

        import qplot
        from qplot import _shutdown_supervisor as supervisor

        supervisor._public_api_gui_child_argv = lambda _argv: [
            sys.executable,
            "-c",
            os.environ["_QPLOT_TEST_GUI_SOURCE"],
        ]
        original_spawn = supervisor._spawn_public_api_launcher
        launcher_pid = []

        def capture_spawn(argv, environment):
            child = original_spawn(argv, environment)
            launcher_pid.append(child.pid)
            return child

        supervisor._spawn_public_api_launcher = capture_spawn
        reaped = {}
        reaped_lock = threading.Lock()
        stop = threading.Event()

        def reap_every_child():
            while not stop.is_set():
                try:
                    pid, status = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    time.sleep(0.001)
                    continue
                if pid == 0:
                    time.sleep(0.001)
                    continue
                with reaped_lock:
                    reaped[pid] = status

        reaper = threading.Thread(target=reap_every_child, daemon=True)
        reaper.start()
        original_wait = supervisor._wait_for_public_api_launcher_exit
        wait_gate_entered = threading.Event()

        def require_foreign_reap(child):
            wait_gate_entered.set()
            deadline = time.monotonic() + 2.0
            while True:
                with reaped_lock:
                    was_reaped = child.pid in reaped
                if was_reaped:
                    break
                if time.monotonic() >= deadline:
                    raise AssertionError(
                        "foreign reaper did not collect the exact launcher"
                    )
                time.sleep(0.001)
            original_wait(child)

        supervisor._wait_for_public_api_launcher_exit = require_foreign_reap
        started_at = time.monotonic()
        public_result = qplot.run()
        elapsed = time.monotonic() - started_at
        stop.set()
        reaper.join(timeout=1.0)
        if not launcher_pid:
            raise AssertionError("public API launcher was not spawned")
        gui_pid = int(
            Path(os.environ["_QPLOT_TEST_GUI_PID_PATH"]).read_text(
                encoding="utf-8"
            )
        )
        helper_pid = int(
            Path(os.environ["_QPLOT_TEST_HELPER_PID_PATH"]).read_text(
                encoding="utf-8"
            )
        )

        def running(pid):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            except PermissionError:
                return True
            return True

        with reaped_lock:
            launcher_was_reaped = launcher_pid[0] in reaped
        Path(os.environ["_QPLOT_TEST_RECORD_PATH"]).write_text(
            json.dumps(
                {
                    "result": public_result,
                    "elapsed": elapsed,
                    "launcher_pid": launcher_pid[0],
                    "gui_pid": gui_pid,
                    "helper_pid": helper_pid,
                    "launcher_reaped": launcher_was_reaped,
                    "wait_gate_entered": wait_gate_entered.is_set(),
                    "running": {
                        "launcher": running(launcher_pid[0]),
                        "gui": running(gui_pid),
                        "helper": running(helper_pid),
                    },
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        """,
        environment_updates={
            "_QPLOT_TEST_GUI_SOURCE": gui_source,
            "_QPLOT_TEST_GUI_PID_PATH": os.fspath(gui_pid_path),
            "_QPLOT_TEST_HELPER_PID_PATH": os.fspath(helper_pid_path),
            "_QPLOT_TEST_RECORD_PATH": os.fspath(record_path),
        },
    )

    assert result.returncode == 0, result.stderr
    record = _read_json(record_path)
    assert record["result"] == 17
    assert float(record["elapsed"]) < 4.0
    assert record["launcher_reaped"] is True
    assert record["wait_gate_entered"] is True
    assert record["running"] == {
        "launcher": False,
        "gui": False,
        "helper": False,
    }


@pytest.mark.parametrize(
    ("first_exception", "later_exception"),
    [
        (KeyboardInterrupt("exact first interrupt"), None),
        (SystemExit(37), KeyboardInterrupt("ignored cleanup interrupt")),
    ],
)
def test_public_run_preserves_control_flow_until_tree_and_launcher_are_gone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_exception: BaseException,
    later_exception: BaseException | None,
) -> None:
    launcher_pid: list[int] = []
    gui_pid_path = tmp_path / "interrupted-gui.pid"
    helper_pid_path = tmp_path / "interrupted-helper.pid"
    gui_source = textwrap.dedent(
        """
        import os
        import subprocess
        import sys
        import time
        from pathlib import Path

        from qplot._shutdown_supervisor import ShutdownSupervisorClient

        ShutdownSupervisorClient.from_environment().connect()
        Path(os.environ["_QPLOT_TEST_GUI_PID_PATH"]).write_text(
            str(os.getpid()), encoding="utf-8"
        )
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import os,sys,time; from pathlib import Path; "
                    "Path(sys.argv[1]).write_text(str(os.getpid()), "
                    "encoding='utf-8'); time.sleep(30.0)"
                ),
                os.environ["_QPLOT_TEST_HELPER_PID_PATH"],
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(30.0)
        """
    )
    monkeypatch.setenv("_QPLOT_TEST_GUI_PID_PATH", os.fspath(gui_pid_path))
    monkeypatch.setenv("_QPLOT_TEST_HELPER_PID_PATH", os.fspath(helper_pid_path))
    monkeypatch.setattr(
        supervisor,
        "_public_api_gui_child_argv",
        lambda _argv: [sys.executable, "-c", gui_source],
    )
    original_spawn = supervisor._spawn_public_api_launcher

    def capture_spawn(argv, environment):
        launcher = original_spawn(argv, environment)
        launcher_pid.append(launcher.pid)
        return launcher

    monkeypatch.setattr(supervisor, "_spawn_public_api_launcher", capture_spawn)
    original_wait = supervisor._wait_for_public_api_result_completion
    injected = [first_exception]
    if later_exception is not None:
        injected.append(later_exception)

    def interrupt_result_wait(completed: threading.Event) -> None:
        if gui_pid_path.exists() and helper_pid_path.exists() and injected:
            raise injected.pop(0)
        original_wait(completed)

    monkeypatch.setattr(
        supervisor,
        "_wait_for_public_api_result_completion",
        interrupt_result_wait,
    )

    caught: BaseException | None = None
    immediate_running: dict[int, bool] = {}
    try:
        supervisor.launch_gui_for_api(["qplot-interrupted-public-api"])
    except BaseException as error:
        caught = error
    finally:
        recorded_pids = list(launcher_pid)
        for path in (gui_pid_path, helper_pid_path):
            if path.exists():
                recorded_pids.append(int(path.read_text(encoding="utf-8")))
        immediate_running = {pid: _process_is_running(pid) for pid in recorded_pids}
        for pid in recorded_pids:
            if immediate_running[pid]:
                _force_cleanup_pid(pid)

    assert caught is first_exception
    if isinstance(first_exception, SystemExit):
        assert isinstance(caught, SystemExit)
        assert caught.code == 37
    assert not injected
    assert len(launcher_pid) == 1
    gui_pid = int(gui_pid_path.read_text(encoding="utf-8"))
    helper_pid = int(helper_pid_path.read_text(encoding="utf-8"))
    assert immediate_running == {
        launcher_pid[0]: False,
        gui_pid: False,
        helper_pid: False,
    }


@pytest.mark.parametrize("start_failure", [False, True])
def test_cancellation_sender_commits_one_worker_or_one_eof_fallback(
    monkeypatch: pytest.MonkeyPatch,
    start_failure: bool,
) -> None:
    caller_channel, launcher_channel = socket.socketpair()
    caller_channel.settimeout(2.0)
    launcher_channel.settimeout(2.0)
    frame = bytes(range(supervisor._FRAME_SIZE))
    sender = supervisor._ApiLauncherCancellationSender(caller_channel, frame)
    lookup_barrier = threading.Barrier(2)
    phase_barriers = {
        name: threading.Barrier(2)
        for name in ("worker_creation", "worker_assignment", "worker_start")
    }
    boundary_visits: list[tuple[str, int]] = []
    boundary_lock = threading.Lock()
    lookup_threads: set[int] = set()
    original_boundary = supervisor._public_api_cancellation_boundary

    def synchronize_startup(name: str) -> None:
        if name == "worker_lookup":
            thread_id = threading.get_ident()
            with boundary_lock:
                first_lookup = thread_id not in lookup_threads
                lookup_threads.add(thread_id)
            if first_lookup:
                lookup_barrier.wait(timeout=2.0)
        elif name in phase_barriers:
            phase_barriers[name].wait(timeout=2.0)
        with boundary_lock:
            boundary_visits.append((name, threading.get_ident()))
        original_boundary(name)

    monkeypatch.setattr(
        supervisor,
        "_public_api_cancellation_boundary",
        synchronize_startup,
    )
    created_workers: list[threading.Thread] = []
    start_calls: list[int] = []

    class ObservedWorker(threading.Thread):
        def start(self) -> None:
            start_calls.append(threading.get_ident())
            if start_failure:
                raise RuntimeError("exact concurrent worker-start failure")
            super().start()

    def create_worker(
        owner: supervisor._ApiLauncherCancellationSender,
    ) -> threading.Thread:
        worker = ObservedWorker(
            target=owner._run,
            name="qplot-public-api-cancellation-sender",
            daemon=True,
        )
        created_workers.append(worker)
        return worker

    monkeypatch.setattr(
        supervisor,
        "_new_public_api_cancellation_worker",
        create_worker,
    )
    send_threads: list[int] = []
    sent_bytes = bytearray()
    original_send = supervisor._public_api_cancellation_send

    def observe_send(channel: socket.socket, data: bytes) -> int:
        written = original_send(channel, data)
        send_threads.append(threading.get_ident())
        sent_bytes.extend(data[:written])
        return written

    monkeypatch.setattr(
        supervisor,
        "_public_api_cancellation_send",
        observe_send,
    )
    shutdown_threads: list[int] = []
    original_shutdown = supervisor._public_api_cancellation_shutdown

    def observe_shutdown(channel: socket.socket) -> None:
        shutdown_threads.append(threading.get_ident())
        original_shutdown(channel)

    monkeypatch.setattr(
        supervisor,
        "_public_api_cancellation_shutdown",
        observe_shutdown,
    )
    requester_completion_ids: list[int] = []
    requester_results: list[bool] = []

    def request_from_result_reader() -> None:
        sender.request()
        requester_completion_ids.append(id(sender.completed))
        requester_results.append(sender.completed.wait(2.0))

    def release_serialized_phases() -> None:
        for barrier in phase_barriers.values():
            barrier.wait(timeout=2.0)

    coordinator = threading.Thread(
        target=release_serialized_phases,
        name="qplot-test-cancellation-start-coordinator",
        daemon=True,
    )
    result_reader = threading.Thread(
        target=request_from_result_reader,
        name="qplot-public-api-result-reader",
    )
    coordinator.start()
    result_reader.start()
    try:
        sender.request()
        requester_completion_ids.append(id(sender.completed))
        requester_results.append(sender.completed.wait(2.0))
        result_reader.join(timeout=2.0)
        coordinator.join(timeout=2.0)

        assert not result_reader.is_alive()
        assert not coordinator.is_alive()
        assert len(created_workers) == 1
        assert len(start_calls) == 1
        assert requester_results == [True, True]
        assert len(set(requester_completion_ids)) == 1
        assert sender.completed.is_set()
        assert sender._worker_state is supervisor._CancellationWorkerState.COMPLETE
        assert [name for name, _thread_id in boundary_visits].count(
            "worker_creation"
        ) == 1
        assert [name for name, _thread_id in boundary_visits].count(
            "worker_assignment"
        ) == 1
        assert [name for name, _thread_id in boundary_visits].count("worker_start") == 1

        if start_failure:
            assert launcher_channel.recv(len(frame)) == b""
            assert not send_threads
            assert len(shutdown_threads) == 1
            assert sender.diagnostic is not None
            assert "exact concurrent worker-start failure" in sender.diagnostic
        else:
            received = launcher_channel.recv(len(frame))
            assert received == frame
            assert bytes(sent_bytes) == frame
            assert len(set(send_threads)) == 1
            assert not shutdown_threads
            launcher_channel.settimeout(0.05)
            with pytest.raises(TimeoutError):
                launcher_channel.recv(1)
    finally:
        caller_channel.close()
        launcher_channel.close()


def test_cancellation_sender_does_not_retry_start_after_its_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller_channel, launcher_channel = socket.socketpair()
    launcher_channel.settimeout(2.0)
    frame = bytes(range(supervisor._FRAME_SIZE))
    sender = supervisor._ApiLauncherCancellationSender(caller_channel, frame)
    start_calls = 0
    shutdown_calls = 0

    class StartThenInterruptWorker(threading.Thread):
        def start(self) -> None:
            nonlocal start_calls
            start_calls += 1
            super().start()
            raise KeyboardInterrupt("after real worker start side effect")

    def create_worker(
        owner: supervisor._ApiLauncherCancellationSender,
    ) -> threading.Thread:
        return StartThenInterruptWorker(
            target=owner._run,
            name="qplot-public-api-cancellation-sender",
            daemon=True,
        )

    original_shutdown = supervisor._public_api_cancellation_shutdown

    def observe_shutdown(channel: socket.socket) -> None:
        nonlocal shutdown_calls
        shutdown_calls += 1
        original_shutdown(channel)

    monkeypatch.setattr(
        supervisor,
        "_new_public_api_cancellation_worker",
        create_worker,
    )
    monkeypatch.setattr(
        supervisor,
        "_public_api_cancellation_shutdown",
        observe_shutdown,
    )
    try:
        sender.request()
        sender.request()
        assert sender.completed.wait(2.0)
        assert launcher_channel.recv(len(frame)) == frame
        assert start_calls == 1
        assert shutdown_calls == 0
        assert sender.diagnostic is not None
        assert "after real worker start side effect" in sender.diagnostic
    finally:
        caller_channel.close()
        launcher_channel.close()


@pytest.mark.parametrize(
    "interrupted_commit",
    ["starting_state", "worker_assignment", "start_attempt"],
)
def test_cancellation_sender_recovers_from_committed_owner_interruption(
    monkeypatch: pytest.MonkeyPatch,
    interrupted_commit: str,
) -> None:
    caller_channel, launcher_channel = socket.socketpair()
    launcher_channel.settimeout(2.0)
    frame = bytes(range(supervisor._FRAME_SIZE))

    class InterruptAfterCommitSender(supervisor._ApiLauncherCancellationSender):
        injection_armed = False
        injected = False

        def __setattr__(self, name: str, value: object) -> None:
            super().__setattr__(name, value)
            if not self.injection_armed or self.injected:
                return
            matching_commit = (
                (
                    interrupted_commit == "starting_state"
                    and name == "_worker_state"
                    and value is supervisor._CancellationWorkerState.STARTING
                )
                or (
                    interrupted_commit == "worker_assignment"
                    and name == "_thread"
                    and value is not None
                )
                or (
                    interrupted_commit == "start_attempt"
                    and name == "_start_attempted"
                    and value is True
                )
            )
            if matching_commit:
                self.injected = True
                raise KeyboardInterrupt(
                    f"immediately after {interrupted_commit} commit"
                )

    sender = InterruptAfterCommitSender(caller_channel, frame)
    sender.injection_armed = True
    created_workers: list[threading.Thread] = []
    start_calls: list[int] = []
    send_threads: list[int] = []
    shutdown_threads: list[int] = []

    class ObservedWorker(threading.Thread):
        def start(self) -> None:
            start_calls.append(threading.get_ident())
            super().start()

    def create_worker(
        owner: supervisor._ApiLauncherCancellationSender,
    ) -> threading.Thread:
        worker = ObservedWorker(
            target=owner._run,
            name="qplot-public-api-cancellation-sender",
            daemon=True,
        )
        created_workers.append(worker)
        return worker

    original_send = supervisor._public_api_cancellation_send

    def observe_send(channel: socket.socket, data: bytes) -> int:
        send_threads.append(threading.get_ident())
        return original_send(channel, data)

    original_shutdown = supervisor._public_api_cancellation_shutdown

    def observe_shutdown(channel: socket.socket) -> None:
        shutdown_threads.append(threading.get_ident())
        original_shutdown(channel)

    monkeypatch.setattr(
        supervisor,
        "_new_public_api_cancellation_worker",
        create_worker,
    )
    monkeypatch.setattr(
        supervisor,
        "_public_api_cancellation_send",
        observe_send,
    )
    monkeypatch.setattr(
        supervisor,
        "_public_api_cancellation_shutdown",
        observe_shutdown,
    )
    requester_barrier = threading.Barrier(3)
    completion_ids: list[int] = []
    completion_results: list[bool] = []

    def requester() -> None:
        requester_barrier.wait(timeout=2.0)
        sender.request()
        completion_ids.append(id(sender.completed))
        completion_results.append(sender.completed.wait(2.0))

    requesters = [
        threading.Thread(
            target=requester,
            name=f"qplot-cancellation-owner-loss-requester-{index}",
            daemon=True,
        )
        for index in range(2)
    ]
    for thread in requesters:
        thread.start()
    requester_barrier.wait(timeout=2.0)
    stranded_starting = False
    try:
        for thread in requesters:
            thread.join(timeout=2.0)

        assert sender.injected
        assert not any(thread.is_alive() for thread in requesters)
        assert completion_results == [True, True]
        assert len(set(completion_ids)) == 1
        assert sender.completed.is_set()
        assert len(created_workers) <= 1
        assert len(start_calls) <= 1

        for worker in created_workers:
            if worker.ident is not None:
                worker.join(timeout=2.0)
            assert not worker.is_alive()

        if interrupted_commit == "start_attempt":
            assert launcher_channel.recv(len(frame)) == b""
            assert not send_threads
            assert len(shutdown_threads) == 1
        else:
            assert launcher_channel.recv(len(frame)) == frame
            assert len(set(send_threads)) == 1
            assert not shutdown_threads
            launcher_channel.settimeout(0.05)
            with pytest.raises(TimeoutError):
                launcher_channel.recv(1)

        assert sender.diagnostic is not None
        assert f"immediately after {interrupted_commit} commit" in sender.diagnostic
        subsequent = threading.Thread(target=sender.request, daemon=True)
        subsequent.start()
        subsequent.join(timeout=0.5)
        assert not subsequent.is_alive()
    finally:
        with sender._worker_condition:
            stranded_starting = (
                sender._worker_state is supervisor._CancellationWorkerState.STARTING
            )
            if stranded_starting:
                sender._worker_state = supervisor._CancellationWorkerState.EOF_FALLBACK
                sender._worker_condition.notify_all()
        if stranded_starting:
            try:
                original_shutdown(caller_channel)
            except OSError:
                pass
            sender._finish()
        for thread in requesters:
            thread.join(timeout=0.5)
        caller_channel.close()
        launcher_channel.close()


def test_interrupt_guard_retries_capture_and_pre_install_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_getsignal = signal.getsignal
    real_signal = signal.signal
    prior_handler = real_getsignal(signal.SIGINT)

    def custom_handler(_signum: int, _frame: object) -> None:
        return None

    real_signal(signal.SIGINT, custom_handler)
    guard = supervisor._CallerCleanupInterruptGuard()
    getsignal_interrupted = False
    installation_interrupted = False

    def interrupted_getsignal(signum: int) -> object:
        nonlocal getsignal_interrupted
        if not getsignal_interrupted:
            getsignal_interrupted = True
            raise KeyboardInterrupt("before original SIGINT capture")
        return real_getsignal(signum)

    def interrupted_signal(signum: int, handler: object) -> object:
        nonlocal installation_interrupted
        if handler is guard._absorber and not installation_interrupted:
            installation_interrupted = True
            raise KeyboardInterrupt("before SIGINT guard installation")
        return real_signal(signum, handler)

    monkeypatch.setattr(signal, "getsignal", interrupted_getsignal)
    monkeypatch.setattr(signal, "signal", interrupted_signal)
    try:
        with pytest.raises(KeyboardInterrupt, match="before original"):
            guard.engage()
        assert guard.previous_handler is None
        with pytest.raises(KeyboardInterrupt, match="before SIGINT guard"):
            guard.engage()
        assert guard.previous_handler is custom_handler
        assert real_getsignal(signal.SIGINT) is custom_handler

        guard.engage()
        assert guard.active
        assert real_getsignal(signal.SIGINT) is guard._absorber
        guard.restore()
        assert not guard.active
        assert real_getsignal(signal.SIGINT) is custom_handler
    finally:
        real_signal(signal.SIGINT, prior_handler)


def test_interrupt_guard_recovers_install_after_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_getsignal = signal.getsignal
    real_signal = signal.signal
    prior_handler = real_getsignal(signal.SIGINT)

    def custom_handler(_signum: int, _frame: object) -> None:
        return None

    real_signal(signal.SIGINT, custom_handler)
    guard = supervisor._CallerCleanupInterruptGuard()
    installation_calls = 0

    def install_then_interrupt(signum: int, handler: object) -> object:
        nonlocal installation_calls
        result = real_signal(signum, handler)
        if handler is guard._absorber:
            installation_calls += 1
            if installation_calls == 1:
                raise KeyboardInterrupt("after SIGINT guard installation")
        return result

    monkeypatch.setattr(signal, "signal", install_then_interrupt)
    try:
        with pytest.raises(KeyboardInterrupt, match="after SIGINT guard"):
            guard.engage()
        assert guard.previous_handler is custom_handler
        assert real_getsignal(signal.SIGINT) is guard._absorber
        assert not guard.active

        guard.engage()
        guard.engage()
        assert guard.active
        assert installation_calls == 1
        guard.restore()
        assert real_getsignal(signal.SIGINT) is custom_handler
    finally:
        real_signal(signal.SIGINT, prior_handler)


@pytest.mark.parametrize("after_side_effect", [False, True])
def test_interrupt_guard_retries_restoration_and_remains_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    after_side_effect: bool,
) -> None:
    real_getsignal = signal.getsignal
    real_signal = signal.signal
    prior_handler = real_getsignal(signal.SIGINT)
    delivered: list[int] = []

    def custom_handler(signum: int, _frame: object) -> None:
        delivered.append(signum)

    real_signal(signal.SIGINT, custom_handler)
    guard = supervisor._CallerCleanupInterruptGuard()
    try:
        guard.engage()
        assert guard.active
        restoration_interrupted = False

        def interrupt_restoration(signum: int, handler: object) -> object:
            nonlocal restoration_interrupted
            if handler is custom_handler and not restoration_interrupted:
                restoration_interrupted = True
                if not after_side_effect:
                    raise KeyboardInterrupt("before SIGINT handler restoration")
                real_signal(signum, handler)
                raise KeyboardInterrupt("after SIGINT handler restoration")
            return real_signal(signum, handler)

        monkeypatch.setattr(signal, "signal", interrupt_restoration)
        expected = "after" if after_side_effect else "before"
        with pytest.raises(KeyboardInterrupt, match=expected):
            guard.restore()
        if after_side_effect:
            assert real_getsignal(signal.SIGINT) is custom_handler
        else:
            assert real_getsignal(signal.SIGINT) is guard._absorber
        assert guard.active

        guard.restore()
        guard.restore()
        assert not guard.active
        assert real_getsignal(signal.SIGINT) is custom_handler
        signal.raise_signal(signal.SIGINT)
        assert delivered == [signal.SIGINT]
    finally:
        real_signal(signal.SIGINT, prior_handler)


def test_interrupt_guard_is_a_safe_noop_outside_main_thread() -> None:
    guard = supervisor._CallerCleanupInterruptGuard()
    observations: list[tuple[bool, bool]] = []

    def engage_outside_main() -> None:
        guard.engage()
        guard.restore()
        observations.append((guard.engagement_complete, guard.active))

    worker = threading.Thread(target=engage_outside_main)
    worker.start()
    worker.join(timeout=2.0)

    assert not worker.is_alive()
    assert observations == [(True, False)]


_REPEATED_INTERRUPT_GUI_SOURCE = textwrap.dedent(
    """
    import ctypes
    import os
    from pathlib import Path

    from qplot._shutdown_supervisor import ShutdownSupervisorClient
    from qplot.datahandling.trusted_live_supervisor import (
        TrustedLiveReaderSupervisor,
    )

    ShutdownSupervisorClient.from_environment().connect()
    Path(os.environ["_QPLOT_TEST_GUI_PID_PATH"]).write_text(
        str(os.getpid()), encoding="utf-8"
    )
    reader = TrustedLiveReaderSupervisor.open(
        os.environ["_QPLOT_TEST_DATABASE_PATH"],
        reply_timeout_seconds=20.0,
        shutdown_timeout_seconds=20.0,
        terminate_timeout_seconds=20.0,
        kill_timeout_seconds=20.0,
        _test_fault="hang_before_operation",
    )
    helper_pid = reader.helper_pid
    if helper_pid is None:
        raise AssertionError("trusted helper PID is unavailable")
    Path(os.environ["_QPLOT_TEST_HELPER_PID_PATH"]).write_text(
        str(helper_pid), encoding="utf-8"
    )
    reader.submit_query("SELECT 1", timeout=20.0)
    reader._wait_for_test_notification(b"operation_started", 10.0)
    reader._wait_for_test_notification(b"operation_hang", 10.0)
    if os.environ.get("_QPLOT_TEST_EXIT_GUI_AFTER_READY") == "1":
        os._exit(17)
    if os.name == "nt":
        sleep = ctypes.PyDLL("kernel32", use_last_error=True).Sleep
        sleep.argtypes = (ctypes.c_ulong,)
        sleep(30_000)
    else:
        sleep = ctypes.PyDLL(None).sleep
        sleep.argtypes = (ctypes.c_uint,)
        sleep(30)
    raise AssertionError("repeated-interrupt GUI returned")
    """
)


_REPEATED_INTERRUPT_DRIVER_SOURCE = textwrap.dedent(
    """
    import ctypes
    import json
    import os
    import queue
    import signal
    import sqlite3
    import subprocess
    import sys
    import threading
    import time
    from pathlib import Path

    import qplot
    from qplot import _shutdown_supervisor as supervisor

    database_path = Path(os.environ["_QPLOT_TEST_DATABASE_PATH"])
    target = os.environ["_QPLOT_TEST_INTERRUPT_TARGET"]
    first_kind = os.environ.get("_QPLOT_TEST_FIRST_EXCEPTION", "system-exit")
    rapid_real_signals = target == "rapid-real-signals"
    ownership_loss_commit = {
        "concurrent-owner-loss-starting": "starting_state",
        "concurrent-owner-loss-assignment": "worker_assignment",
        "concurrent-owner-loss-start-attempt": "start_attempt",
    }.get(target)
    concurrent_worker_race = target in {
        "concurrent-worker-start",
        "concurrent-worker-start-failure",
    } or ownership_loss_commit is not None
    guard_case = target.startswith("guard:")
    caller_original_sigint = signal.getsignal(signal.SIGINT)
    post_cleanup_sigints = []

    def custom_original_sigint(signum, _frame):
        post_cleanup_sigints.append(signum)

    if guard_case:
        signal.signal(signal.SIGINT, custom_original_sigint)
    writer_ready = threading.Event()
    writer_commit = threading.Event()
    writer_committed = threading.Event()
    writer_failures = queue.Queue()

    def writer():
        try:
            connection = sqlite3.connect(database_path, isolation_level=None)
            mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
            if mode is None or str(mode[0]).casefold() != "wal":
                raise AssertionError(f"WAL mode unavailable: {mode!r}")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute(
                "CREATE TABLE acquisition_writer ("
                "seq INTEGER PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO acquisition_writer VALUES(1, 'before')"
            )
            writer_ready.set()
            if not writer_commit.wait(15.0):
                raise TimeoutError("post-interrupt writer commit was not requested")
            connection.execute(
                "INSERT INTO acquisition_writer VALUES(2, 'after')"
            )
            count = connection.execute(
                "SELECT COUNT(*) FROM acquisition_writer"
            ).fetchone()[0]
            if count != 2:
                raise AssertionError(f"writer row count is {count}")
            connection.close()
            writer_committed.set()
        except BaseException as error:
            writer_failures.put(f"{type(error).__name__}: {error}")
            writer_ready.set()
            writer_committed.set()

    writer_thread = threading.Thread(
        target=writer,
        name="qplot-repeated-interrupt-writer",
    )
    writer_thread.start()
    if not writer_ready.wait(8.0):
        raise TimeoutError("WAL writer did not start")
    if not writer_failures.empty():
        raise AssertionError(writer_failures.get_nowait())

    def protected_state():
        state = {}
        for suffix in ("", "-wal", "-journal"):
            path = Path(f"{database_path}{suffix}")
            if not path.exists():
                state[suffix] = None
                continue
            metadata = path.stat()
            state[suffix] = {
                "bytes": path.read_bytes().hex(),
                "metadata": [
                    metadata.st_mode,
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_nlink,
                    getattr(metadata, "st_uid", 0),
                    getattr(metadata, "st_gid", 0),
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    metadata.st_ctime_ns,
                ],
            }
        return state

    before = protected_state()
    sentinel = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30.0)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    Path(os.environ["_QPLOT_TEST_SENTINEL_PID_PATH"]).write_text(
        str(sentinel.pid), encoding="utf-8"
    )
    supervisor._public_api_gui_child_argv = lambda _argv: [
        sys.executable,
        "-c",
        os.environ["_QPLOT_TEST_GUI_SOURCE"],
    ]
    original_spawn = supervisor._spawn_public_api_launcher
    launchers = []

    def capture_spawn(argv, environment):
        launcher = original_spawn(argv, environment)
        launchers.append(launcher)
        Path(os.environ["_QPLOT_TEST_LAUNCHER_PID_PATH"]).write_text(
            str(launcher.pid), encoding="utf-8"
        )
        return launcher

    supervisor._spawn_public_api_launcher = capture_spawn
    first_exception = (
        SystemExit(37)
        if first_kind == "system-exit"
        else KeyboardInterrupt("identifiable first caller interrupt")
    )
    second_exception = KeyboardInterrupt(
        f"injected second caller interrupt at {target}"
    )
    injection = {
        "first": False,
        "second": False,
        "partial": False,
        "send_failure": False,
        "rapid_second_sent": False,
        "rapid_second_delivered": 0,
        "worker_objects": 0,
        "worker_start_calls": 0,
        "worker_fallback_calls": 0,
    }
    sender_instances = []
    requester_completion_ids = []
    requester_completion_observed = []
    cancellation_send_threads = []
    cancellation_sent_bytes = bytearray()
    cancellation_metrics_lock = threading.Lock()
    concurrent_release = threading.Event()
    concurrent_lookup_barrier = threading.Barrier(2)
    concurrent_phase_barriers = {
        name: threading.Barrier(2)
        for name in ("worker_creation", "worker_assignment", "worker_start")
    }
    concurrent_phase_counts = {
        "worker_creation": 0,
        "worker_assignment": 0,
        "worker_start": 0,
    }
    concurrent_lookup_threads = set()
    helper_path = Path(os.environ["_QPLOT_TEST_HELPER_PID_PATH"])
    original_result_wait = supervisor._wait_for_public_api_result_completion

    def inject_first_during_result_wait(completed):
        if (
            not rapid_real_signals
            and target != "launcher-exit-before-cancellation"
            and helper_path.exists()
            and not injection["first"]
        ):
            injection["first"] = True
            if concurrent_worker_race:
                concurrent_release.set()
            raise first_exception
        return original_result_wait(completed)

    supervisor._wait_for_public_api_result_completion = (
        inject_first_during_result_wait
    )
    original_cancellation_boundary = supervisor._public_api_cancellation_boundary

    def inject_cancellation_boundary(name):
        if concurrent_worker_race and name == "worker_lookup":
            thread_id = threading.get_ident()
            with cancellation_metrics_lock:
                first_lookup = thread_id not in concurrent_lookup_threads
                concurrent_lookup_threads.add(thread_id)
            if first_lookup:
                concurrent_lookup_barrier.wait(timeout=5.0)
        if concurrent_worker_race and name in concurrent_phase_barriers:
            with cancellation_metrics_lock:
                first_phase_visit = concurrent_phase_counts[name] == 0
                if first_phase_visit:
                    concurrent_phase_counts[name] = 1
            if first_phase_visit:
                concurrent_phase_barriers[name].wait(timeout=5.0)
        should_inject = target == f"cancellation:{name}"
        if target == "launcher-exit-before-cancellation":
            should_inject = name == "lock_acquisition"
        if target == "concurrent-worker-start":
            should_inject = name == "worker_assignment"
        if injection["first"] and should_inject and not injection["second"]:
            injection["second"] = True
            raise second_exception
        return original_cancellation_boundary(name)

    supervisor._public_api_cancellation_boundary = inject_cancellation_boundary
    original_cleanup_boundary = supervisor._public_api_caller_cleanup_boundary

    def inject_cleanup_boundary(name):
        if (
            target == "launcher-exit-before-cancellation"
            and name == "launcher_exit_wait"
            and not injection["first"]
        ):
            injection["first"] = True
            raise first_exception
        if (
            injection["first"]
            and target == f"cleanup:{name}"
            and not injection["second"]
        ):
            injection["second"] = True
            raise second_exception
        return original_cleanup_boundary(name)

    supervisor._public_api_caller_cleanup_boundary = inject_cleanup_boundary
    original_guard_boundary = supervisor._public_api_interrupt_guard_boundary

    def inject_guard_boundary(name):
        if (
            injection["first"]
            and target == f"guard:{name}"
            and not injection["second"]
        ):
            injection["second"] = True
            raise second_exception
        return original_guard_boundary(name)

    supervisor._public_api_interrupt_guard_boundary = inject_guard_boundary
    original_cancellation_send = supervisor._public_api_cancellation_send

    def controlled_cancellation_send(channel, data):
        if (
            target == "cancellation:socket_send"
            and injection["first"]
            and not injection["second"]
        ):
            injection["second"] = True
            raise second_exception
        if (
            target == "cancellation:after_partial_send"
            and not injection["partial"]
            and len(data) > 1
        ):
            injection["partial"] = True
            return original_cancellation_send(
                channel,
                data[: max(1, len(data) // 2)],
            )
        if (
            target == "cancellation:write_side_shutdown"
            and not injection["send_failure"]
        ):
            injection["send_failure"] = True
            raise OSError("injected cancellation send failure before EOF fallback")
        written = original_cancellation_send(channel, data)
        if concurrent_worker_race:
            with cancellation_metrics_lock:
                cancellation_send_threads.append(threading.get_ident())
                cancellation_sent_bytes.extend(data[:written])
        return written

    supervisor._public_api_cancellation_send = controlled_cancellation_send
    original_cancellation_shutdown = supervisor._public_api_cancellation_shutdown

    def observe_cancellation_shutdown(channel):
        if concurrent_worker_race:
            with cancellation_metrics_lock:
                injection["worker_fallback_calls"] += 1
        return original_cancellation_shutdown(channel)

    supervisor._public_api_cancellation_shutdown = observe_cancellation_shutdown
    original_absorb_sigint = supervisor._CallerCleanupInterruptGuard._absorb_sigint
    original_engage_interrupt_guard = (
        supervisor._CallerCleanupInterruptGuard.engage
    )
    interrupt_guard_engaged = threading.Event()

    def observe_interrupt_guard_engaged(self):
        result = original_engage_interrupt_guard(self)
        if self.active:
            interrupt_guard_engaged.set()
        return result

    supervisor._CallerCleanupInterruptGuard.engage = (
        observe_interrupt_guard_engaged
    )

    def observe_absorbed_sigint(self, signum, frame):
        injection["rapid_second_delivered"] += 1
        return original_absorb_sigint(self, signum, frame)

    supervisor._CallerCleanupInterruptGuard._absorb_sigint = observe_absorbed_sigint
    original_worker_factory = supervisor._new_public_api_cancellation_worker
    original_sender_class = supervisor._ApiLauncherCancellationSender

    class OwnershipLossCancellationSender(original_sender_class):
        def __setattr__(self, name, value):
            super().__setattr__(name, value)
            if (
                ownership_loss_commit is None
                or not injection["first"]
                or injection["second"]
            ):
                return
            matching_commit = (
                ownership_loss_commit == "starting_state"
                and name == "_worker_state"
                and value is supervisor._CancellationWorkerState.STARTING
            ) or (
                ownership_loss_commit == "worker_assignment"
                and name == "_thread"
                and value is not None
            ) or (
                ownership_loss_commit == "start_attempt"
                and name == "_start_attempted"
                and value is True
            )
            if matching_commit:
                injection["second"] = True
                raise second_exception

    if ownership_loss_commit is not None:
        supervisor._ApiLauncherCancellationSender = (
            OwnershipLossCancellationSender
        )
    original_sender_request = original_sender_class.request
    original_result_observer = supervisor._observe_public_api_launcher_result

    class ObservedCancellationWorker(threading.Thread):
        def start(self):
            with cancellation_metrics_lock:
                injection["worker_start_calls"] += 1
            if target == "concurrent-worker-start-failure":
                injection["second"] = True
                raise second_exception
            return super().start()

    def observed_worker_factory(sender):
        if not concurrent_worker_race:
            return original_worker_factory(sender)
        with cancellation_metrics_lock:
            injection["worker_objects"] += 1
            sender_instances.append(sender)
        return ObservedCancellationWorker(
            target=sender._run,
            name="qplot-public-api-cancellation-sender",
            daemon=True,
        )

    def observed_sender_request(self):
        result = original_sender_request(self)
        if concurrent_worker_race:
            with cancellation_metrics_lock:
                sender_instances.append(self)
                requester_completion_ids.append(id(self.completed))
            completed = self.completed.wait(5.0)
            with cancellation_metrics_lock:
                requester_completion_observed.append(completed)
        return result

    def concurrent_result_observer(
        channel,
        observation,
        cancellation_sender,
        **options,
    ):
        if concurrent_worker_race:
            if not concurrent_release.wait(5.0):
                raise TimeoutError("concurrent cancellation race was not released")
            cancellation_sender.request()
        return original_result_observer(
            channel,
            observation,
            cancellation_sender,
            **options,
        )

    supervisor._new_public_api_cancellation_worker = observed_worker_factory
    supervisor._ApiLauncherCancellationSender.request = observed_sender_request
    supervisor._observe_public_api_launcher_result = concurrent_result_observer
    phase_coordinator = None
    if concurrent_worker_race:
        def release_worker_phases():
            for phase_name in (
                "worker_creation",
                "worker_assignment",
                "worker_start",
            ):
                concurrent_phase_barriers[phase_name].wait(timeout=5.0)

        phase_coordinator = threading.Thread(
            target=release_worker_phases,
            name="qplot-concurrent-worker-phase-coordinator",
            daemon=True,
        )
        phase_coordinator.start()

    if rapid_real_signals:
        def send_rapid_sigints():
            deadline = time.monotonic() + 8.0
            while not helper_path.exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError("trusted helper did not become ready")
                time.sleep(0.005)
            os.kill(os.getpid(), signal.SIGINT)
            if not interrupt_guard_engaged.wait(2.0):
                raise TimeoutError("caller cleanup SIGINT guard did not engage")
            injection["rapid_second_sent"] = True
            os.kill(os.getpid(), signal.SIGINT)

        threading.Thread(
            target=send_rapid_sigints,
            name="qplot-rapid-double-sigint",
            daemon=True,
        ).start()

    def running(pid):
        if os.name != "nt":
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            except PermissionError:
                return True
            return True
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = (
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        )
        open_process.restype = wintypes.HANDLE
        wait_for_single_object = kernel32.WaitForSingleObject
        wait_for_single_object.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        wait_for_single_object.restype = wintypes.DWORD
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        handle = open_process(0x00100000, False, pid)
        if not handle:
            return False
        try:
            return wait_for_single_object(handle, 0) == 0x102
        finally:
            close_handle(handle)

    caught = None
    try:
        qplot.run(database_path=database_path)
    except BaseException as error:
        caught = error
        if not launchers:
            raise AssertionError("public launcher was not captured")
        gui_pid = int(
            Path(os.environ["_QPLOT_TEST_GUI_PID_PATH"]).read_text(
                encoding="utf-8"
            )
        )
        helper_pid = int(helper_path.read_text(encoding="utf-8"))
        immediate_running = {
            "launcher": running(launchers[0].pid),
            "gui": running(gui_pid),
            "helper": running(helper_pid),
        }
        protected_unchanged = protected_state() == before
        writer_alive_at_catch = writer_thread.is_alive()
        sentinel_alive_at_catch = sentinel.poll() is None
        writer_commit.set()
        if not writer_committed.wait(8.0):
            raise TimeoutError("writer could not commit after repeated interrupt")
        writer_thread.join(timeout=1.0)
        if writer_thread.is_alive():
            raise AssertionError("writer thread did not finish")
        if not writer_failures.empty():
            raise AssertionError(writer_failures.get_nowait())
        exact_first = rapid_real_signals or caught is first_exception
        traceback_names = []
        current_traceback = caught.__traceback__
        while current_traceback is not None:
            traceback_names.append(current_traceback.tb_frame.f_code.co_name)
            current_traceback = current_traceback.tb_next
        expected_first_traceback = (
            "inject_cleanup_boundary"
            if target == "launcher-exit-before-cancellation"
            else "inject_first_during_result_wait"
        )
        first_traceback_retained = (
            rapid_real_signals or expected_first_traceback in traceback_names
        )
        second_traceback_absent = not any(
            name in traceback_names
            for name in (
                "inject_cancellation_boundary",
                "inject_guard_boundary",
                "ObservedCancellationWorker.start",
            )
        )
        guard_original_restored = True
        guard_followup_delivered = True
        if guard_case:
            guard_original_restored = (
                signal.getsignal(signal.SIGINT) is custom_original_sigint
            )
            before_followup = len(post_cleanup_sigints)
            signal.raise_signal(signal.SIGINT)
            guard_followup_delivered = (
                len(post_cleanup_sigints) == before_followup + 1
            )
        if phase_coordinator is not None:
            phase_coordinator.join(timeout=1.0)
            if phase_coordinator.is_alive():
                raise AssertionError("worker phase coordinator did not finish")
        subsequent_request_returned = True
        if ownership_loss_commit is not None:
            requested_sender_ids = set()
            for sender in sender_instances:
                sender_id = id(sender)
                if sender_id in requested_sender_ids:
                    continue
                requested_sender_ids.add(sender_id)
                sender.request()
        sender_threads_alive = [
            thread.name
            for thread in threading.enumerate()
            if thread.name == "qplot-public-api-cancellation-sender"
        ]
        sender_frame = sender_instances[0].frame if sender_instances else b""
        unique_sender_instances = len({id(item) for item in sender_instances})
        Path(os.environ["_QPLOT_TEST_RECORD_PATH"]).write_text(
            json.dumps(
                {
                    "exception_type": type(caught).__name__,
                    "exception_text": str(caught),
                    "exact_first": exact_first,
                    "first_traceback_retained": first_traceback_retained,
                    "second_traceback_absent": second_traceback_absent,
                    "system_exit_code": getattr(caught, "code", None),
                    "first_injected": injection["first"],
                    "second_injected": injection["second"],
                    "rapid_second_delivered": injection[
                        "rapid_second_delivered"
                    ],
                    "rapid_second_sent": injection["rapid_second_sent"],
                    "partial_send_exercised": injection["partial"],
                    "shutdown_fallback_exercised": injection[
                        "send_failure"
                    ],
                    "immediate_running": immediate_running,
                    "protected_unchanged": protected_unchanged,
                    "writer_alive_at_catch": writer_alive_at_catch,
                    "writer_committed_after_catch": True,
                    "sentinel_alive_at_catch": sentinel_alive_at_catch,
                    "guard_original_restored": guard_original_restored,
                    "guard_followup_delivered": guard_followup_delivered,
                    "worker_objects": injection["worker_objects"],
                    "worker_start_calls": injection["worker_start_calls"],
                    "worker_fallback_calls": injection[
                        "worker_fallback_calls"
                    ],
                    "unique_sender_instances": unique_sender_instances,
                    "requester_completion_event_count": len(
                        set(requester_completion_ids)
                    ),
                    "requester_completion_observed": (
                        requester_completion_observed
                    ),
                    "unique_send_threads": len(set(cancellation_send_threads)),
                    "sent_bytes": bytes(cancellation_sent_bytes).hex(),
                    "sender_frame": sender_frame.hex(),
                    "sender_threads_alive": sender_threads_alive,
                    "subsequent_request_returned": subsequent_request_returned,
                    "worker_phase_counts": concurrent_phase_counts,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    finally:
        writer_commit.set()
        sentinel.terminate()
        try:
            sentinel.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            sentinel.kill()
            sentinel.wait(timeout=1.0)
        if guard_case:
            signal.signal(signal.SIGINT, caller_original_sigint)
    if caught is None:
        raise AssertionError("public API returned without caller control flow")
    if rapid_real_signals:
        if not isinstance(caught, KeyboardInterrupt):
            raise AssertionError(f"expected KeyboardInterrupt, received {caught!r}")
    elif caught is not first_exception:
        raise AssertionError("the exact first caller exception was not re-raised")
    """
)


_REPEATED_INTERRUPT_BOUNDARIES = (
    "cancellation:worker_lookup",
    "cancellation:worker_creation",
    "cancellation:worker_assignment",
    "cancellation:worker_start",
    "cancellation:worker_start_commit",
    "cancellation:lock_acquisition",
    "cancellation:deadline_time_monotonic",
    "cancellation:deadline_setup",
    "cancellation:timeout_within",
    "cancellation:select",
    "cancellation:send_before_progress",
    "cancellation:socket_send",
    "cancellation:after_partial_send",
    "cancellation:send_offset_accounting",
    "cancellation:write_side_shutdown",
    "cancellation:diagnostic_construction",
    "launcher-exit-before-cancellation",
    "cleanup:result_eof_processing",
    "cleanup:result_channel_close",
    "cleanup:launcher_exit_wait",
    "cleanup:final_diagnostic_publication",
    "guard:installation_signal_after",
    "guard:restoration_signal_after",
)


def _run_repeated_interrupt_boundary_probe(
    tmp_path: Path,
    target: str,
    *,
    first_kind: str,
) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    safe_target = target.replace(":", "-")
    database_path = tmp_path / f"repeated-{safe_target}.db"
    record_path = tmp_path / f"repeated-{safe_target}.json"
    launcher_pid_path = tmp_path / f"repeated-{safe_target}-launcher.pid"
    gui_pid_path = tmp_path / f"repeated-{safe_target}-gui.pid"
    helper_pid_path = tmp_path / f"repeated-{safe_target}-helper.pid"
    sentinel_pid_path = tmp_path / f"repeated-{safe_target}-sentinel.pid"
    paths = (
        launcher_pid_path,
        gui_pid_path,
        helper_pid_path,
        sentinel_pid_path,
    )
    try:
        result = _run_public_api_driver(
            _REPEATED_INTERRUPT_DRIVER_SOURCE,
            environment_updates={
                "_QPLOT_TEST_DATABASE_PATH": os.fspath(database_path),
                "_QPLOT_TEST_GUI_SOURCE": _REPEATED_INTERRUPT_GUI_SOURCE,
                "_QPLOT_TEST_RECORD_PATH": os.fspath(record_path),
                "_QPLOT_TEST_LAUNCHER_PID_PATH": os.fspath(launcher_pid_path),
                "_QPLOT_TEST_GUI_PID_PATH": os.fspath(gui_pid_path),
                "_QPLOT_TEST_HELPER_PID_PATH": os.fspath(helper_pid_path),
                "_QPLOT_TEST_SENTINEL_PID_PATH": os.fspath(sentinel_pid_path),
                "_QPLOT_TEST_INTERRUPT_TARGET": target,
                "_QPLOT_TEST_FIRST_EXCEPTION": first_kind,
                "_QPLOT_TEST_EXIT_GUI_AFTER_READY": (
                    "1" if target == "launcher-exit-before-cancellation" else "0"
                ),
            },
            timeout=20.0,
        )
        return result, _read_json(record_path)
    finally:
        for path in paths:
            if path.exists():
                _force_cleanup_pid(int(path.read_text(encoding="utf-8")))


@pytest.mark.parametrize("target", _REPEATED_INTERRUPT_BOUNDARIES)
def test_public_run_absorbs_second_exception_at_every_cleanup_boundary(
    tmp_path: Path,
    target: str,
) -> None:
    first_kind = (
        "system-exit"
        if _REPEATED_INTERRUPT_BOUNDARIES.index(target) % 2 == 0
        else "keyboard-interrupt"
    )
    result, record = _run_repeated_interrupt_boundary_probe(
        tmp_path,
        target,
        first_kind=first_kind,
    )

    assert result.returncode == 0, result.stderr
    assert record["exact_first"] is True
    assert record["first_traceback_retained"] is True
    assert record["second_traceback_absent"] is True
    assert record["first_injected"] is True
    assert record["second_injected"] is True
    if first_kind == "system-exit":
        assert record["exception_type"] == "SystemExit"
        assert record["system_exit_code"] == 37
    else:
        assert record["exception_type"] == "KeyboardInterrupt"
        assert record["exception_text"] == "identifiable first caller interrupt"
    assert record["immediate_running"] == {
        "launcher": False,
        "gui": False,
        "helper": False,
    }
    assert record["protected_unchanged"] is True
    assert record["writer_alive_at_catch"] is True
    assert record["writer_committed_after_catch"] is True
    assert record["sentinel_alive_at_catch"] is True
    if target == "cancellation:after_partial_send":
        assert record["partial_send_exercised"] is True
    if target == "cancellation:write_side_shutdown":
        assert record["shutdown_fallback_exercised"] is True
    if target.startswith("guard:"):
        assert record["guard_original_restored"] is True
        assert record["guard_followup_delivered"] is True


@pytest.mark.parametrize("start_failure", [False, True])
def test_public_run_concurrent_cancellation_requesters_share_one_lifecycle(
    tmp_path: Path,
    start_failure: bool,
) -> None:
    target = (
        "concurrent-worker-start-failure"
        if start_failure
        else "concurrent-worker-start"
    )
    result, record = _run_repeated_interrupt_boundary_probe(
        tmp_path,
        target,
        first_kind="system-exit",
    )

    assert result.returncode == 0, result.stderr
    assert record["exact_first"] is True
    assert record["first_traceback_retained"] is True
    assert record["second_traceback_absent"] is True
    assert record["exception_type"] == "SystemExit"
    assert record["system_exit_code"] == 37
    assert record["first_injected"] is True
    assert record["second_injected"] is True
    assert record["worker_objects"] == 1
    assert record["worker_start_calls"] == 1
    assert record["unique_sender_instances"] == 1
    assert record["requester_completion_event_count"] == 1
    assert len(record["requester_completion_observed"]) >= 2
    assert all(record["requester_completion_observed"])
    assert record["worker_phase_counts"] == {
        "worker_assignment": 1,
        "worker_creation": 1,
        "worker_start": 1,
    }
    if start_failure:
        assert record["unique_send_threads"] == 0
        assert record["sent_bytes"] == ""
        assert record["worker_fallback_calls"] == 1
    else:
        assert record["unique_send_threads"] == 1
        assert record["sent_bytes"] == record["sender_frame"]
        assert record["worker_fallback_calls"] == 0
    assert record["immediate_running"] == {
        "launcher": False,
        "gui": False,
        "helper": False,
    }
    assert record["protected_unchanged"] is True
    assert record["writer_alive_at_catch"] is True
    assert record["writer_committed_after_catch"] is True
    assert record["sentinel_alive_at_catch"] is True


@pytest.mark.parametrize(
    ("target", "expected_start_calls", "expected_fallback_calls"),
    [
        ("concurrent-owner-loss-starting", 1, 0),
        ("concurrent-owner-loss-assignment", 1, 0),
        ("concurrent-owner-loss-start-attempt", 0, 1),
    ],
)
def test_public_run_recovers_from_cancellation_owner_loss(
    tmp_path: Path,
    target: str,
    expected_start_calls: int,
    expected_fallback_calls: int,
) -> None:
    result, record = _run_repeated_interrupt_boundary_probe(
        tmp_path,
        target,
        first_kind="system-exit",
    )

    assert result.returncode == 0, result.stderr
    assert record["exact_first"] is True
    assert record["first_traceback_retained"] is True
    assert record["second_traceback_absent"] is True
    assert record["exception_type"] == "SystemExit"
    assert record["system_exit_code"] == 37
    assert record["first_injected"] is True
    assert record["second_injected"] is True
    assert record["worker_objects"] == 1
    assert record["worker_start_calls"] == expected_start_calls
    assert record["worker_fallback_calls"] == expected_fallback_calls
    assert record["unique_sender_instances"] == 1
    assert record["requester_completion_event_count"] == 1
    assert len(record["requester_completion_observed"]) >= 3
    assert all(record["requester_completion_observed"])
    assert record["sender_threads_alive"] == []
    assert record["subsequent_request_returned"] is True
    assert record["worker_phase_counts"] == {
        "worker_assignment": 1,
        "worker_creation": 1,
        "worker_start": 1,
    }
    if expected_fallback_calls:
        assert record["unique_send_threads"] == 0
        assert record["sent_bytes"] == ""
    else:
        assert record["unique_send_threads"] == 1
        assert record["sent_bytes"] == record["sender_frame"]
    assert record["immediate_running"] == {
        "launcher": False,
        "gui": False,
        "helper": False,
    }
    assert record["protected_unchanged"] is True
    assert record["writer_alive_at_catch"] is True
    assert record["writer_committed_after_catch"] is True
    assert record["sentinel_alive_at_catch"] is True


@pytest.mark.skipif(os.name == "nt", reason="real rapid POSIX SIGINT regression")
def test_public_run_real_rapid_double_sigint_is_no_escape(
    tmp_path: Path,
) -> None:
    result, record = _run_repeated_interrupt_boundary_probe(
        tmp_path,
        "rapid-real-signals",
        first_kind="keyboard-interrupt",
    )

    assert result.returncode == 0, result.stderr
    assert record["exception_type"] == "KeyboardInterrupt"
    assert record["rapid_second_sent"] is True
    assert int(record["rapid_second_delivered"]) >= 1
    assert record["immediate_running"] == {
        "launcher": False,
        "gui": False,
        "helper": False,
    }
    assert record["protected_unchanged"] is True
    assert record["writer_alive_at_catch"] is True
    assert record["writer_committed_after_catch"] is True
    assert record["sentinel_alive_at_catch"] is True


def test_public_run_real_interrupt_preserves_writer_and_cleans_trusted_tree(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "interrupted-public-writer.db"
    record_path = tmp_path / "interrupted-public-result.json"
    launcher_pid_path = tmp_path / "interrupted-public-launcher.pid"
    gui_pid_path = tmp_path / "interrupted-public-gui.pid"
    helper_pid_path = tmp_path / "interrupted-public-helper.pid"
    sentinel_pid_path = tmp_path / "interrupted-public-sentinel.pid"
    gui_source = textwrap.dedent(
        """
        import ctypes
        import os
        from pathlib import Path

        from qplot._shutdown_supervisor import ShutdownSupervisorClient
        from qplot.datahandling.trusted_live_supervisor import (
            TrustedLiveReaderSupervisor,
        )

        ShutdownSupervisorClient.from_environment().connect()
        Path(os.environ["_QPLOT_TEST_GUI_PID_PATH"]).write_text(
            str(os.getpid()), encoding="utf-8"
        )
        reader = TrustedLiveReaderSupervisor.open(
            os.environ["_QPLOT_TEST_DATABASE_PATH"],
            reply_timeout_seconds=20.0,
            shutdown_timeout_seconds=20.0,
            terminate_timeout_seconds=20.0,
            kill_timeout_seconds=20.0,
            _test_fault="hang_before_operation",
        )
        helper_pid = reader.helper_pid
        if helper_pid is None:
            raise AssertionError("trusted helper PID is unavailable")
        Path(os.environ["_QPLOT_TEST_HELPER_PID_PATH"]).write_text(
            str(helper_pid), encoding="utf-8"
        )
        reader.submit_query("SELECT 1", timeout=20.0)
        reader._wait_for_test_notification(b"operation_started", 10.0)
        reader._wait_for_test_notification(b"operation_hang", 10.0)
        if os.name == "nt":
            sleep = ctypes.PyDLL("kernel32", use_last_error=True).Sleep
            sleep.argtypes = (ctypes.c_ulong,)
            sleep(30_000)
        else:
            sleep = ctypes.PyDLL(None).sleep
            sleep.argtypes = (ctypes.c_uint,)
            sleep(30)
        raise AssertionError("interrupted stuck GUI returned")
        """
    )
    pid_paths = (
        launcher_pid_path,
        gui_pid_path,
        helper_pid_path,
        sentinel_pid_path,
    )
    try:
        result = _run_public_api_driver(
            """
            import _thread
            import ctypes
            import json
            import os
            import queue
            import signal
            import sqlite3
            import subprocess
            import sys
            import threading
            import time
            from pathlib import Path

            import qplot
            from qplot import _shutdown_supervisor as supervisor

            database_path = Path(os.environ["_QPLOT_TEST_DATABASE_PATH"])
            writer_ready = threading.Event()
            writer_commit = threading.Event()
            writer_committed = threading.Event()
            writer_failures = queue.Queue()

            def writer():
                try:
                    connection = sqlite3.connect(
                        database_path,
                        isolation_level=None,
                    )
                    mode = connection.execute(
                        "PRAGMA journal_mode=WAL"
                    ).fetchone()
                    if mode is None or str(mode[0]).casefold() != "wal":
                        raise AssertionError(f"WAL mode unavailable: {mode!r}")
                    connection.execute("PRAGMA wal_autocheckpoint=0")
                    connection.execute(
                        "CREATE TABLE acquisition_writer ("
                        "seq INTEGER PRIMARY KEY, value TEXT NOT NULL)"
                    )
                    connection.execute(
                        "INSERT INTO acquisition_writer VALUES(1, 'before')"
                    )
                    writer_ready.set()
                    if not writer_commit.wait(10.0):
                        raise TimeoutError("post-interrupt commit was not requested")
                    connection.execute(
                        "INSERT INTO acquisition_writer VALUES(2, 'after')"
                    )
                    count = connection.execute(
                        "SELECT COUNT(*) FROM acquisition_writer"
                    ).fetchone()[0]
                    if count != 2:
                        raise AssertionError(f"writer row count is {count}")
                    connection.close()
                    writer_committed.set()
                except BaseException as error:
                    writer_failures.put(f"{type(error).__name__}: {error}")
                    writer_ready.set()
                    writer_committed.set()

            writer_thread = threading.Thread(
                target=writer,
                name="qplot-interrupted-acquisition-writer",
            )
            writer_thread.start()
            if not writer_ready.wait(5.0):
                raise TimeoutError("WAL writer did not start")
            if not writer_failures.empty():
                raise AssertionError(writer_failures.get_nowait())

            def protected_state():
                state = {}
                for suffix in ("", "-wal", "-journal"):
                    path = Path(f"{database_path}{suffix}")
                    if not path.exists():
                        state[suffix] = None
                        continue
                    metadata = path.stat()
                    state[suffix] = {
                        "bytes": path.read_bytes().hex(),
                        "metadata": [
                            metadata.st_mode,
                            metadata.st_dev,
                            metadata.st_ino,
                            metadata.st_nlink,
                            getattr(metadata, "st_uid", 0),
                            getattr(metadata, "st_gid", 0),
                            metadata.st_size,
                            metadata.st_mtime_ns,
                            metadata.st_ctime_ns,
                        ],
                    }
                return state

            before = protected_state()
            sentinel = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30.0)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            Path(os.environ["_QPLOT_TEST_SENTINEL_PID_PATH"]).write_text(
                str(sentinel.pid), encoding="utf-8"
            )
            supervisor._public_api_gui_child_argv = lambda _argv: [
                sys.executable,
                "-c",
                os.environ["_QPLOT_TEST_GUI_SOURCE"],
            ]
            original_spawn = supervisor._spawn_public_api_launcher
            launchers = []

            def capture_spawn(argv, environment):
                launcher = original_spawn(argv, environment)
                launchers.append(launcher)
                Path(os.environ["_QPLOT_TEST_LAUNCHER_PID_PATH"]).write_text(
                    str(launcher.pid), encoding="utf-8"
                )
                return launcher

            supervisor._spawn_public_api_launcher = capture_spawn
            interrupt_delivered_at = []

            def interrupt_when_trusted_helper_is_stuck():
                helper_path = Path(os.environ["_QPLOT_TEST_HELPER_PID_PATH"])
                deadline = time.monotonic() + 6.0
                while not helper_path.exists():
                    if time.monotonic() >= deadline:
                        raise TimeoutError("trusted helper did not become ready")
                    time.sleep(0.005)
                time.sleep(0.05)
                interrupt_delivered_at.append(time.monotonic())
                if os.name == "nt":
                    _thread.interrupt_main()
                else:
                    os.kill(os.getpid(), signal.SIGINT)

            interrupter = threading.Thread(
                target=interrupt_when_trusted_helper_is_stuck,
                name="qplot-real-public-interrupter",
                daemon=True,
            )
            interrupter.start()

            def running(pid):
                if os.name != "nt":
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        return False
                    except PermissionError:
                        return True
                    return True
                from ctypes import wintypes

                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                open_process = kernel32.OpenProcess
                open_process.argtypes = (
                    wintypes.DWORD,
                    wintypes.BOOL,
                    wintypes.DWORD,
                )
                open_process.restype = wintypes.HANDLE
                wait_for_single_object = kernel32.WaitForSingleObject
                wait_for_single_object.argtypes = (
                    wintypes.HANDLE,
                    wintypes.DWORD,
                )
                wait_for_single_object.restype = wintypes.DWORD
                close_handle = kernel32.CloseHandle
                close_handle.argtypes = (wintypes.HANDLE,)
                close_handle.restype = wintypes.BOOL
                handle = open_process(0x00100000, False, pid)
                if not handle:
                    return False
                try:
                    return wait_for_single_object(handle, 0) == 0x102
                finally:
                    close_handle(handle)

            caught = None
            try:
                qplot.run(database_path=database_path)
            except BaseException as error:
                caught = error
                caught_at = time.monotonic()
                if not launchers:
                    raise AssertionError("public launcher was not captured")
                gui_pid = int(
                    Path(os.environ["_QPLOT_TEST_GUI_PID_PATH"]).read_text(
                        encoding="utf-8"
                    )
                )
                helper_pid = int(
                    Path(os.environ["_QPLOT_TEST_HELPER_PID_PATH"]).read_text(
                        encoding="utf-8"
                    )
                )
                immediate_running = {
                    "launcher": running(launchers[0].pid),
                    "gui": running(gui_pid),
                    "helper": running(helper_pid),
                }
                protected_unchanged = protected_state() == before
                writer_alive_at_catch = writer_thread.is_alive()
                sentinel_alive_at_catch = sentinel.poll() is None
                writer_commit.set()
                if not writer_committed.wait(5.0):
                    raise TimeoutError("writer could not commit after interrupt")
                writer_thread.join(timeout=1.0)
                if writer_thread.is_alive():
                    raise AssertionError("writer thread did not finish")
                if not writer_failures.empty():
                    raise AssertionError(writer_failures.get_nowait())
                Path(os.environ["_QPLOT_TEST_RECORD_PATH"]).write_text(
                    json.dumps(
                        {
                            "exception_type": type(caught).__name__,
                            "exception_text": str(caught),
                            "interrupt_to_catch": (
                                caught_at - interrupt_delivered_at[0]
                            ),
                            "immediate_running": immediate_running,
                            "protected_unchanged": protected_unchanged,
                            "writer_alive_at_catch": writer_alive_at_catch,
                            "writer_committed_after_catch": True,
                            "sentinel_alive_at_catch": sentinel_alive_at_catch,
                            "launcher_pid": launchers[0].pid,
                            "gui_pid": gui_pid,
                            "helper_pid": helper_pid,
                            "sentinel_pid": sentinel.pid,
                        },
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
            finally:
                writer_commit.set()
                sentinel.terminate()
                try:
                    sentinel.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    sentinel.kill()
                    sentinel.wait(timeout=1.0)
            if not isinstance(caught, KeyboardInterrupt):
                raise AssertionError(
                    f"expected KeyboardInterrupt, received {caught!r}"
                )
            """,
            environment_updates={
                "_QPLOT_TEST_DATABASE_PATH": os.fspath(database_path),
                "_QPLOT_TEST_GUI_SOURCE": gui_source,
                "_QPLOT_TEST_RECORD_PATH": os.fspath(record_path),
                "_QPLOT_TEST_LAUNCHER_PID_PATH": os.fspath(launcher_pid_path),
                "_QPLOT_TEST_GUI_PID_PATH": os.fspath(gui_pid_path),
                "_QPLOT_TEST_HELPER_PID_PATH": os.fspath(helper_pid_path),
                "_QPLOT_TEST_SENTINEL_PID_PATH": os.fspath(sentinel_pid_path),
            },
            timeout=12.0,
        )

        assert result.returncode == 0, result.stderr
        record = _read_json(record_path)
        assert record["exception_type"] == "KeyboardInterrupt"
        assert record["immediate_running"] == {
            "launcher": False,
            "gui": False,
            "helper": False,
        }
        assert float(record["interrupt_to_catch"]) < 1.0
        assert record["protected_unchanged"] is True
        assert record["writer_alive_at_catch"] is True
        assert record["writer_committed_after_catch"] is True
        assert record["sentinel_alive_at_catch"] is True
    finally:
        for pid_path in pid_paths:
            if pid_path.exists():
                _force_cleanup_pid(int(pid_path.read_text(encoding="utf-8")))


def test_public_launcher_cleans_tree_when_api_caller_disappears(
    tmp_path: Path,
) -> None:
    launcher_pid_path = tmp_path / "vanished-caller-launcher.pid"
    gui_pid_path = tmp_path / "vanished-caller-gui.pid"
    helper_pid_path = tmp_path / "vanished-caller-helper.pid"
    sentinel_pid_path = tmp_path / "vanished-caller-sentinel.pid"
    gui_source = textwrap.dedent(
        """
        import os
        import subprocess
        import sys
        import time
        from pathlib import Path

        from qplot._shutdown_supervisor import ShutdownSupervisorClient

        ShutdownSupervisorClient.from_environment().connect()
        Path(os.environ["_QPLOT_TEST_GUI_PID_PATH"]).write_text(
            str(os.getpid()), encoding="utf-8"
        )
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import os,sys,time; from pathlib import Path; "
                    "Path(sys.argv[1]).write_text(str(os.getpid()), "
                    "encoding='utf-8'); time.sleep(30.0)"
                ),
                os.environ["_QPLOT_TEST_HELPER_PID_PATH"],
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(30.0)
        """
    )
    caller_source = textwrap.dedent(
        """
        import os
        import subprocess
        import sys
        from pathlib import Path

        import qplot
        from qplot import _shutdown_supervisor as supervisor

        sentinel = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30.0)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        Path(os.environ["_QPLOT_TEST_SENTINEL_PID_PATH"]).write_text(
            str(sentinel.pid), encoding="utf-8"
        )
        supervisor._public_api_gui_child_argv = lambda _argv: [
            sys.executable,
            "-c",
            os.environ["_QPLOT_TEST_GUI_SOURCE"],
        ]
        original_spawn = supervisor._spawn_public_api_launcher

        def capture_spawn(argv, environment):
            launcher = original_spawn(argv, environment)
            Path(os.environ["_QPLOT_TEST_LAUNCHER_PID_PATH"]).write_text(
                str(launcher.pid), encoding="utf-8"
            )
            return launcher

        supervisor._spawn_public_api_launcher = capture_spawn
        qplot.run()
        raise AssertionError("public API caller unexpectedly returned")
        """
    )
    paths = (
        launcher_pid_path,
        gui_pid_path,
        helper_pid_path,
        sentinel_pid_path,
    )
    caller = subprocess.Popen(
        [sys.executable, "-c", caller_source],
        env=_subprocess_environment(
            _QPLOT_TEST_GUI_SOURCE=gui_source,
            _QPLOT_TEST_LAUNCHER_PID_PATH=os.fspath(launcher_pid_path),
            _QPLOT_TEST_GUI_PID_PATH=os.fspath(gui_pid_path),
            _QPLOT_TEST_HELPER_PID_PATH=os.fspath(helper_pid_path),
            _QPLOT_TEST_SENTINEL_PID_PATH=os.fspath(sentinel_pid_path),
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    recorded_pids: list[int] = []
    try:
        readiness_deadline = time.monotonic() + 5.0
        while not all(path.exists() for path in paths):
            if caller.poll() is not None:
                stdout, stderr = caller.communicate()
                raise AssertionError(
                    f"caller exited before readiness: {stdout!r} {stderr!r}"
                )
            if time.monotonic() >= readiness_deadline:
                raise TimeoutError("caller-disappearance tree did not become ready")
            time.sleep(0.005)
        recorded_pids = [int(path.read_text(encoding="utf-8")) for path in paths]
        caller.kill()
        caller.wait(timeout=2.0)

        launcher_pid, gui_pid, helper_pid, sentinel_pid = recorded_pids
        cleanup_deadline = time.monotonic() + 5.0
        while any(
            _process_is_running(pid) for pid in (launcher_pid, gui_pid, helper_pid)
        ):
            if time.monotonic() >= cleanup_deadline:
                raise AssertionError("caller EOF did not clean the contained tree")
            time.sleep(0.005)
        assert _process_is_running(sentinel_pid)
    finally:
        if caller.poll() is None:
            caller.kill()
            caller.wait(timeout=1.0)
        for pid in recorded_pids:
            if _process_is_running(pid):
                _force_cleanup_pid(pid)


@pytest.mark.skipif(os.name == "nt", reason="POSIX foreign waitpid regression")
def test_public_pre_ready_foreign_reaper_never_signals_stale_pid(
    tmp_path: Path,
) -> None:
    record_path = tmp_path / "pre-ready-foreign-reaper.json"
    result = _run_public_api_driver(
        """
        import json
        import os
        import subprocess
        import sys
        import threading
        import time
        from pathlib import Path

        from qplot import _shutdown_supervisor as supervisor

        original_spawn = supervisor._spawn_public_api_launcher
        reaped = {}
        stop = threading.Event()

        class NoRawPidSignals:
            def __init__(self, child):
                self._child = child
                self.pid = child.pid
                self.returncode = None
                self.signal_attempts = []

            def __getattr__(self, name):
                return getattr(self._child, name)

            def wait(self, *args, **kwargs):
                result = self._child.wait(*args, **kwargs)
                self.returncode = self._child.returncode
                return result

            def terminate(self):
                self.signal_attempts.append("terminate")
                raise AssertionError("stale POSIX PID terminate attempted")

            def kill(self):
                self.signal_attempts.append("kill")
                raise AssertionError("stale POSIX PID kill attempted")

        captured = []

        def spawn_abrupt(_argv, environment):
            child = subprocess.Popen(
                [sys.executable, "-c", "import os; os._exit(23)"],
                env=dict(environment),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            wrapped = NoRawPidSignals(child)
            captured.append(wrapped)
            return wrapped

        supervisor._spawn_public_api_launcher = spawn_abrupt

        def reap_every_child():
            while not stop.is_set():
                try:
                    pid, status = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    time.sleep(0.001)
                    continue
                if pid == 0:
                    time.sleep(0.001)
                    continue
                reaped[pid] = status

        reaper = threading.Thread(target=reap_every_child, daemon=True)
        reaper.start()
        started = time.monotonic()
        public_result = supervisor.launch_gui_for_api(
            ["qplot-pre-ready-foreign-reaper"],
            startup_timeout=0.35,
        )
        elapsed = time.monotonic() - started
        stop.set()
        reaper.join(timeout=1.0)
        if len(captured) != 1:
            raise AssertionError("abrupt launcher was not captured")
        launcher = captured[0]
        Path(os.environ["_QPLOT_TEST_RECORD_PATH"]).write_text(
            json.dumps(
                {
                    "result": public_result,
                    "elapsed": elapsed,
                    "launcher_pid": launcher.pid,
                    "reaped": launcher.pid in reaped,
                    "signal_attempts": launcher.signal_attempts,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        """,
        environment_updates={
            "_QPLOT_TEST_RECORD_PATH": os.fspath(record_path),
        },
        timeout=4.0,
    )

    assert result.returncode == 0, result.stderr
    record = _read_json(record_path)
    assert record["result"] == supervisor._FORCED_SHUTDOWN_EXIT_CODE
    assert float(record["elapsed"]) < 2.0
    assert record["reaped"] is True
    assert record["signal_attempts"] == []
    assert not _process_is_running(int(record["launcher_pid"]))
    assert (
        "public-API launcher readiness raised TimeoutError: timed out" in result.stderr
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX signal-result mapping")
def test_public_run_maps_gui_signal_without_signalling_its_caller(
    tmp_path: Path,
) -> None:
    record_path = tmp_path / "public-signal.json"
    result = _run_public_api_driver(
        """
        import json
        import os
        import signal
        import sys
        from pathlib import Path

        import qplot
        from qplot import _shutdown_supervisor as supervisor

        child_source = '''
        import os
        import signal
        from qplot._shutdown_supervisor import ShutdownSupervisorClient
        ShutdownSupervisorClient.from_environment().connect()
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError("SIGTERM was not delivered")
        '''
        supervisor._public_api_gui_child_argv = lambda _argv: [
            sys.executable,
            "-c",
            child_source,
        ]
        public_result = qplot.run()
        Path(os.environ["_QPLOT_TEST_RECORD_PATH"]).write_text(
            json.dumps(
                {
                    "caller_survived": True,
                    "result": public_result,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        """,
        environment_updates={"_QPLOT_TEST_RECORD_PATH": os.fspath(record_path)},
    )

    assert result.returncode == 0, result.stderr
    assert _read_json(record_path) == {
        "caller_survived": True,
        "result": -signal.SIGTERM,
    }


def test_public_run_returns_70_on_authenticated_launcher_eof(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    launched: list[subprocess.Popen[str]] = []
    abrupt_launcher_source = textwrap.dedent(
        """
        import os
        from qplot import _shutdown_supervisor as supervisor

        bootstrap = supervisor._api_launcher_bootstrap_from_environment()
        supervisor._connect_public_api_result_channel(bootstrap)
        os._exit(23)
        """
    )

    def spawn_abruptly(_argv, environment):
        child = subprocess.Popen(
            [sys.executable, "-c", abrupt_launcher_source],
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        launched.append(child)
        return child

    monkeypatch.setattr(supervisor, "_spawn_public_api_launcher", spawn_abruptly)
    result = supervisor.launch_gui_for_api(
        ["qplot-public-eof"],
        startup_timeout=2.0,
    )

    assert result == supervisor._FORCED_SHUTDOWN_EXIT_CODE
    assert len(launched) == 1
    assert launched[0].returncode == 23
    assert (
        "public-API launcher result channel closed before an outcome"
        in capsys.readouterr().err
    )


def test_public_run_returns_70_on_authenticated_malformed_result(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    launched: list[subprocess.Popen[str]] = []
    malformed_launcher_source = textwrap.dedent(
        """
        import os
        from qplot import _shutdown_supervisor as supervisor

        bootstrap = supervisor._api_launcher_bootstrap_from_environment()
        channel = supervisor._connect_public_api_result_channel(bootstrap)
        channel.sendall(b"!" * supervisor._API_RESULT_HEADER.size)
        os._exit(24)
        """
    )

    def spawn_malformed(_argv, environment):
        child = subprocess.Popen(
            [sys.executable, "-c", malformed_launcher_source],
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        launched.append(child)
        return child

    monkeypatch.setattr(supervisor, "_spawn_public_api_launcher", spawn_malformed)

    assert supervisor.launch_gui_for_api(["qplot-malformed-result"]) == 70
    assert len(launched) == 1
    assert launched[0].returncode == 24
    assert not _process_is_running(launched[0].pid)
    assert (
        "public-API launcher result exceeds the bounded result size"
        in capsys.readouterr().err
    )


def test_public_run_setup_failure_returns_normally_before_popen(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    popen_called = False

    def fail_socket(*_args, **_kwargs):
        raise OSError("exact injected public launcher listener failure")

    def unexpected_spawn(*_args, **_kwargs):
        nonlocal popen_called
        popen_called = True
        raise AssertionError("launcher must not spawn after setup failure")

    monkeypatch.setattr(supervisor.socket, "socket", fail_socket)
    monkeypatch.setattr(
        supervisor,
        "_spawn_public_api_launcher",
        unexpected_spawn,
    )

    assert supervisor.launch_gui_for_api(["qplot-public-setup-failure"]) == 70
    assert not popen_called
    assert (
        "public-API launcher setup raised OSError: "
        "exact injected public launcher listener failure" in capsys.readouterr().err
    )


def test_public_run_forced_reader_shutdown_preserves_acquisition_caller(
    tmp_path: Path,
) -> None:
    """A real stuck reader tree cannot kill or contain its writer caller."""

    database_path = tmp_path / "public-api-writer.db"
    record_path = tmp_path / "public-api-writer-result.json"
    deadline_path = tmp_path / "public-api-deadline.json"
    launcher_pid_path = tmp_path / "public-api-launcher.pid"
    gui_pid_path = tmp_path / "public-api-gui.pid"
    helper_pid_path = tmp_path / "public-api-helper.pid"
    sentinel_pid_path = tmp_path / "public-api-sentinel.pid"
    gui_source = textwrap.dedent(
        """
        import ctypes
        import json
        import os
        import time
        from pathlib import Path

        from qplot._shutdown_supervisor import ShutdownSupervisorClient
        from qplot.datahandling.trusted_live_supervisor import (
            TrustedLiveReaderSupervisor,
        )

        client = ShutdownSupervisorClient.from_environment().connect()
        Path(os.environ["_QPLOT_TEST_GUI_PID_PATH"]).write_text(
            str(os.getpid()), encoding="utf-8"
        )
        reader = TrustedLiveReaderSupervisor.open(
            os.environ["_QPLOT_TEST_DATABASE_PATH"],
            reply_timeout_seconds=20.0,
            shutdown_timeout_seconds=20.0,
            terminate_timeout_seconds=20.0,
            kill_timeout_seconds=20.0,
            _test_fault="hang_before_operation",
        )
        helper_pid = reader.helper_pid
        if helper_pid is None:
            raise AssertionError("trusted live helper did not expose its PID")
        Path(os.environ["_QPLOT_TEST_HELPER_PID_PATH"]).write_text(
            str(helper_pid), encoding="utf-8"
        )
        reader.submit_query("SELECT 1", timeout=20.0)
        reader._wait_for_test_notification(b"operation_started", 10.0)
        reader._wait_for_test_notification(b"operation_hang", 10.0)
        hard_deadline = time.monotonic() + 0.45
        arm_error = client.arm(hard_deadline)
        if arm_error is not None:
            raise AssertionError(arm_error)
        Path(os.environ["_QPLOT_TEST_DEADLINE_PATH"]).write_text(
            json.dumps(
                {
                    "hard_deadline": hard_deadline,
                    "operation_hang_observed": True,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        if os.name == "nt":
            sleep = ctypes.PyDLL("kernel32", use_last_error=True).Sleep
            sleep.argtypes = (ctypes.c_ulong,)
            sleep(30_000)
        else:
            sleep = ctypes.PyDLL(None).sleep
            sleep.argtypes = (ctypes.c_uint,)
            sleep(30)
        raise AssertionError("stuck GUI returned instead of being contained")
        """
    )
    pid_paths = (
        launcher_pid_path,
        gui_pid_path,
        helper_pid_path,
        sentinel_pid_path,
    )
    try:
        result = _run_public_api_driver(
            """
            import ctypes
            import json
            import os
            import queue
            import sqlite3
            import subprocess
            import sys
            import threading
            import time
            from pathlib import Path

            import qplot
            from qplot import _shutdown_supervisor as supervisor

            database_path = Path(os.environ["_QPLOT_TEST_DATABASE_PATH"])
            writer_ready = threading.Event()
            writer_commit = threading.Event()
            writer_committed = threading.Event()
            writer_failures = queue.Queue()

            def writer():
                try:
                    connection = sqlite3.connect(
                        database_path, isolation_level=None
                    )
                    mode = connection.execute(
                        "PRAGMA journal_mode=WAL"
                    ).fetchone()
                    if mode is None or str(mode[0]).casefold() != "wal":
                        raise AssertionError(f"WAL mode unavailable: {mode!r}")
                    connection.execute("PRAGMA wal_autocheckpoint=0")
                    connection.execute(
                        "CREATE TABLE acquisition_writer ("
                        "seq INTEGER PRIMARY KEY, value TEXT NOT NULL)"
                    )
                    connection.execute(
                        "INSERT INTO acquisition_writer VALUES(1, 'before')"
                    )
                    writer_ready.set()
                    if not writer_commit.wait(10.0):
                        raise TimeoutError("post-viewer writer commit was not requested")
                    connection.execute(
                        "INSERT INTO acquisition_writer VALUES(2, 'after')"
                    )
                    count = connection.execute(
                        "SELECT COUNT(*) FROM acquisition_writer"
                    ).fetchone()[0]
                    if count != 2:
                        raise AssertionError(f"writer row count is {count}")
                    connection.close()
                    writer_committed.set()
                except BaseException as error:
                    writer_failures.put(f"{type(error).__name__}: {error}")
                    writer_ready.set()
                    writer_committed.set()

            writer_thread = threading.Thread(
                target=writer,
                name="qplot-acquisition-writer",
            )
            writer_thread.start()
            if not writer_ready.wait(5.0):
                raise TimeoutError("in-process writer did not start")
            if not writer_failures.empty():
                raise AssertionError(writer_failures.get_nowait())

            def protected_state():
                state = {}
                for suffix in ("", "-wal", "-journal"):
                    path = Path(f"{database_path}{suffix}")
                    if not path.exists():
                        state[suffix] = None
                        continue
                    metadata = path.stat()
                    state[suffix] = {
                        "bytes": path.read_bytes().hex(),
                        "metadata": [
                            metadata.st_mode,
                            metadata.st_dev,
                            metadata.st_ino,
                            metadata.st_nlink,
                            getattr(metadata, "st_uid", 0),
                            getattr(metadata, "st_gid", 0),
                            metadata.st_size,
                            metadata.st_mtime_ns,
                            metadata.st_ctime_ns,
                        ],
                    }
                return state

            before = protected_state()
            sentinel = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30.0)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            Path(os.environ["_QPLOT_TEST_SENTINEL_PID_PATH"]).write_text(
                str(sentinel.pid), encoding="utf-8"
            )
            supervisor._public_api_gui_child_argv = lambda _argv: [
                sys.executable,
                "-c",
                os.environ["_QPLOT_TEST_GUI_SOURCE"],
            ]
            original_spawn = supervisor._spawn_public_api_launcher
            launcher = []

            def capture_spawn(argv, environment):
                child = original_spawn(argv, environment)
                launcher.append(child)
                Path(os.environ["_QPLOT_TEST_LAUNCHER_PID_PATH"]).write_text(
                    str(child.pid), encoding="utf-8"
                )
                return child

            supervisor._spawn_public_api_launcher = capture_spawn

            def running(pid):
                if os.name != "nt":
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        return False
                    except PermissionError:
                        return True
                    return True
                from ctypes import wintypes

                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                open_process = kernel32.OpenProcess
                open_process.argtypes = (
                    wintypes.DWORD,
                    wintypes.BOOL,
                    wintypes.DWORD,
                )
                open_process.restype = wintypes.HANDLE
                wait_for_single_object = kernel32.WaitForSingleObject
                wait_for_single_object.argtypes = (
                    wintypes.HANDLE,
                    wintypes.DWORD,
                )
                wait_for_single_object.restype = wintypes.DWORD
                close_handle = kernel32.CloseHandle
                close_handle.argtypes = (wintypes.HANDLE,)
                close_handle.restype = wintypes.BOOL
                handle = open_process(0x00100000, False, pid)
                if not handle:
                    return False
                try:
                    return wait_for_single_object(handle, 0) == 0x102
                finally:
                    close_handle(handle)

            try:
                public_result = qplot.run(database_path=database_path)
                completed_at = time.monotonic()
                deadline_record = json.loads(
                    Path(os.environ["_QPLOT_TEST_DEADLINE_PATH"]).read_text(
                        encoding="utf-8"
                    )
                )
                gui_pid = int(
                    Path(os.environ["_QPLOT_TEST_GUI_PID_PATH"]).read_text(
                        encoding="utf-8"
                    )
                )
                helper_pid = int(
                    Path(os.environ["_QPLOT_TEST_HELPER_PID_PATH"]).read_text(
                        encoding="utf-8"
                    )
                )
                if not launcher:
                    raise AssertionError("public API launcher was not spawned")
                immediate_running = {
                    "launcher": running(launcher[0].pid),
                    "gui": running(gui_pid),
                    "helper": running(helper_pid),
                }
                protected_unchanged = protected_state() == before
                writer_alive_at_return = writer_thread.is_alive()
                sentinel_alive_at_return = sentinel.poll() is None
                writer_commit.set()
                if not writer_committed.wait(5.0):
                    raise TimeoutError("writer could not commit after qplot.run")
                writer_thread.join(timeout=1.0)
                if writer_thread.is_alive():
                    raise AssertionError("writer thread did not finish")
                if not writer_failures.empty():
                    raise AssertionError(writer_failures.get_nowait())
                Path(os.environ["_QPLOT_TEST_RECORD_PATH"]).write_text(
                    json.dumps(
                        {
                            "caller_survived": True,
                            "result": public_result,
                            "completed_at": completed_at,
                            "hard_deadline": deadline_record["hard_deadline"],
                            "operation_hang_observed": deadline_record[
                                "operation_hang_observed"
                            ],
                            "protected_unchanged": protected_unchanged,
                            "writer_alive_at_return": writer_alive_at_return,
                            "writer_committed_after_return": True,
                            "sentinel_alive_at_return": sentinel_alive_at_return,
                            "running": immediate_running,
                            "launcher_pid": launcher[0].pid,
                            "gui_pid": gui_pid,
                            "helper_pid": helper_pid,
                            "sentinel_pid": sentinel.pid,
                        },
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
            finally:
                writer_commit.set()
                sentinel.terminate()
                try:
                    sentinel.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    sentinel.kill()
                    sentinel.wait(timeout=1.0)
            """,
            environment_updates={
                "_QPLOT_TEST_DATABASE_PATH": os.fspath(database_path),
                "_QPLOT_TEST_GUI_SOURCE": gui_source,
                "_QPLOT_TEST_RECORD_PATH": os.fspath(record_path),
                "_QPLOT_TEST_DEADLINE_PATH": os.fspath(deadline_path),
                "_QPLOT_TEST_LAUNCHER_PID_PATH": os.fspath(launcher_pid_path),
                "_QPLOT_TEST_GUI_PID_PATH": os.fspath(gui_pid_path),
                "_QPLOT_TEST_HELPER_PID_PATH": os.fspath(helper_pid_path),
                "_QPLOT_TEST_SENTINEL_PID_PATH": os.fspath(sentinel_pid_path),
            },
            timeout=12.0,
        )

        assert result.returncode == 0, result.stderr
        record = _read_json(record_path)
        hard_deadline = float(record["hard_deadline"])
        assert record["caller_survived"] is True
        assert record["result"] == supervisor._FORCED_SHUTDOWN_EXIT_CODE
        assert record["operation_hang_observed"] is True
        assert record["protected_unchanged"] is True
        assert record["writer_alive_at_return"] is True
        assert record["writer_committed_after_return"] is True
        assert record["sentinel_alive_at_return"] is True
        assert record["running"] == {
            "launcher": False,
            "gui": False,
            "helper": False,
        }
        assert hard_deadline - 0.03 <= float(record["completed_at"])
        assert float(record["completed_at"]) < hard_deadline + 0.75
    finally:
        for pid_path in pid_paths:
            if pid_path.exists():
                _force_cleanup_pid(int(pid_path.read_text(encoding="utf-8")))


def test_public_run_launch_failure_returns_normally_with_exact_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_launch(_argv, _environment):
        raise OSError("exact injected public launcher Popen failure")

    monkeypatch.setattr(supervisor, "_spawn_public_api_launcher", fail_launch)

    assert (
        supervisor.launch_gui_for_api(["qplot-public-launch-failure"])
        == supervisor._FORCED_SHUTDOWN_EXIT_CODE
    )
    assert (
        "public-API launcher launch raised OSError: "
        "exact injected public launcher Popen failure" in capsys.readouterr().err
    )
