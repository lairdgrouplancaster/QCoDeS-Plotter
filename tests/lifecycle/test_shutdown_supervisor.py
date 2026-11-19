"""Launcher supervision preserves process identity, exit status and bounded cleanup."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from qplot import _shutdown_supervisor as supervisor
from tests.support.lifecycle.shutdown_supervisor import (
    _assert_supervised_processes_gone,
    _force_cleanup_pid,
    _read_json,
    _run_supervised_child,
)
from tests.support.lifecycle.shutdown_supervisor import (
    _process_is_running as _process_is_running,
)

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


def test_windows_venv_child_authenticates_its_retained_redirector_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(supervisor.os, "name", "nt")
    monkeypatch.setattr(supervisor.sys, "prefix", "C:/venv")
    monkeypatch.setattr(supervisor.sys, "base_prefix", "C:/Python")
    monkeypatch.setattr(supervisor.sys, "executable", "C:/venv/Scripts/python.exe")
    monkeypatch.setattr(
        supervisor.sys,
        "_base_executable",
        "C:/Python/python.exe",
        raising=False,
    )
    monkeypatch.setattr(supervisor.os, "getppid", lambda: 4312)
    monkeypatch.setattr(supervisor.os, "getpid", lambda: 8765)

    assert supervisor._supervised_child_claimed_pid() == 4312


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
            deadline = time.monotonic() + 0.75
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
            writer.communicate()
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
        # The launcher can kill this child as soon as the first deadline expires.
        Path(os.environ["_QPLOT_TEST_CHILD_PID_PATH"]).write_text(
            str(os.getpid()), encoding="utf-8"
        )
        first_deadline = time.monotonic() + 0.25
        later_deadline = first_deadline + 1.0
        Path(os.environ["_QPLOT_TEST_RECORD_PATH"]).write_text(
            json.dumps(
                {
                    "first_deadline": first_deadline,
                    "later_deadline": later_deadline,
                }
            ),
            encoding="utf-8",
        )
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
            # Leave enough scheduling margin for loaded hosted macOS runners;
            # waking after the immutable deadline must correctly force exit.
            time.sleep(max(0.0, deadline - time.monotonic() - 0.25))
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


@pytest.mark.skipif(os.name == "nt", reason="POSIX child-observation regression")
def test_posix_child_observation_uses_kqueue_when_waitid_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = []

    class Child:
        pid = 4312

    class Event:
        ident = Child.pid
        fflags = 0x80000000

    class Queue:
        def control(self, changes, max_events, timeout):
            events.append((changes, max_events, timeout))
            return [Event()]

        def close(self):
            events.append("closed")

    def kevent(ident, **kwargs):
        return (ident, kwargs)

    monkeypatch.delattr(supervisor.os, "waitid", raising=False)
    monkeypatch.setattr(supervisor.select, "kqueue", Queue, raising=False)
    monkeypatch.setattr(supervisor.select, "kevent", kevent, raising=False)
    monkeypatch.setattr(supervisor.select, "KQ_FILTER_PROC", -5, raising=False)
    monkeypatch.setattr(supervisor.select, "KQ_EV_ADD", 1, raising=False)
    monkeypatch.setattr(supervisor.select, "KQ_EV_ENABLE", 4, raising=False)
    monkeypatch.setattr(supervisor.select, "KQ_EV_ONESHOT", 16, raising=False)
    monkeypatch.setattr(
        supervisor.select,
        "KQ_NOTE_EXIT",
        Event.fflags,
        raising=False,
    )

    assert supervisor._posix_child_exit_observed(
        Child(),
        supervisor._LauncherSignalState(),
    )
    assert events[-1] == "closed"
    changes, max_events, timeout = events[0]
    assert changes == [
        (
            Child.pid,
            {
                "filter": -5,
                "flags": 21,
                "fflags": Event.fflags,
            },
        )
    ]
    assert max_events == 1
    assert timeout == 0.0


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
    monkeypatch.setattr(supervisor.os, "waitid", failing_waitid, raising=False)
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
    monkeypatch.setattr(supervisor.os, "waitid", delayed_live_waitid, raising=False)
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
