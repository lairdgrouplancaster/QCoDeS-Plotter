"""Real process harnesses shared by shutdown contract tests."""

from __future__ import annotations

import ctypes
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path

from qplot import _shutdown_supervisor as supervisor


def _process_is_running(pid: int) -> bool:
    if os.name == "nt":
        import ctypes.wintypes as wintypes

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


def _force_cleanup_pid(pid: int) -> None:
    if not _process_is_running(pid):
        return
    if os.name == "nt":
        import ctypes.wintypes as wintypes

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
        # The test process is already gone, which satisfies forced fixture cleanup.
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
