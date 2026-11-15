"""Public launch calls preserve caller ownership, diagnostics and process outcomes."""

from __future__ import annotations

import os
import secrets
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from qplot import _shutdown_supervisor as supervisor
from tests.support.lifecycle.shutdown_supervisor import (
    _force_cleanup_pid,
    _read_json,
    _run_public_api_driver,
    _run_repeated_interrupt_boundary_probe,
    _subprocess_environment,
)
from tests.support.lifecycle.shutdown_supervisor import (
    _process_is_running as _process_is_running,
)


def test_public_api_launcher_authenticates_retained_child_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind((supervisor._LOOPBACK_HOST, 0))
    listener.listen(1)
    authentication_key = secrets.token_bytes(supervisor._AUTHENTICATION_BYTES)
    session_nonce = secrets.token_bytes(supervisor._NONCE_BYTES)
    claimed_pid = 4312
    observed_pids: list[int] = []

    def acknowledge_launcher() -> None:
        channel, _address = listener.accept()
        try:
            hello = supervisor._receive_exact(channel, supervisor._FRAME_SIZE)
            frame_type, hello_nonce, payload = supervisor._decode_frame(
                hello,
                authentication_key=authentication_key,
                session_nonce=session_nonce,
            )
            assert frame_type == supervisor._API_LAUNCHER_HELLO
            observed_pids.append(supervisor._PID_PAYLOAD.unpack(payload)[0])
            channel.sendall(
                supervisor._encode_frame(
                    supervisor._API_LAUNCHER_READY,
                    authentication_key=authentication_key,
                    session_nonce=session_nonce,
                    message_nonce=hello_nonce,
                    payload=supervisor._PID_PAYLOAD.pack(claimed_pid),
                )
            )
        finally:
            channel.close()

    bootstrap = supervisor._ApiLauncherBootstrap(
        host=supervisor._LOOPBACK_HOST,
        port=listener.getsockname()[1],
        authentication_key=authentication_key,
        session_nonce=session_nonce,
        startup_deadline=time.monotonic() + 2.0,
        caller_pid=os.getpid(),
        child_argv=(sys.executable,),
        database_path=None,
    )
    monkeypatch.setattr(
        supervisor,
        "_supervised_child_claimed_pid",
        lambda: claimed_pid,
    )
    server = threading.Thread(target=acknowledge_launcher)
    server.start()
    try:
        channel = supervisor._connect_public_api_result_channel(bootstrap)
        channel.close()
        server.join(timeout=2.0)
        assert not server.is_alive()
        assert observed_pids == [claimed_pid]
    finally:
        listener.close()


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
        # The publisher itself has a 25 ms join budget. Allow enough outer
        # scheduling tolerance for heavily loaded CI runners while still
        # proving that the injected one-second sink cannot block shutdown.
        assert elapsed < 0.25
    finally:
        release.set()


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

    import qplot.__main__ as qplot_entrypoint
    from qplot import run as run_qplot

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

    result = run_qplot(
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
        import socket
        from qplot import _shutdown_supervisor as supervisor

        bootstrap = supervisor._api_launcher_bootstrap_from_environment()
        channel = supervisor._connect_public_api_result_channel(bootstrap)
        channel.sendall(b"!" * supervisor._API_RESULT_HEADER.size)
        # Match production publication: commit the queued header and FIN before
        # ExitProcess can reset Winsock and discard the malformed test frame.
        channel.shutdown(socket.SHUT_WR)
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
