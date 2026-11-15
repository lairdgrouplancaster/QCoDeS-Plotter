"""Cancellation and repeated interrupts retain one cleanup owner until children exit."""

from __future__ import annotations

import os
import signal
import socket
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from qplot import _shutdown_supervisor as supervisor
from tests.support.lifecycle.shutdown_supervisor import (
    _assert_supervised_processes_gone,
    _force_cleanup_pid,
    _read_json,
    _run_public_api_driver,
    _run_repeated_interrupt_boundary_probe,
    _run_supervised_child,
)
from tests.support.lifecycle.shutdown_supervisor import (
    _process_is_running as _process_is_running,
)


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


@pytest.mark.parametrize(
    "worker_state",
    [
        supervisor._CancellationWorkerState.RUNNING,
        supervisor._CancellationWorkerState.EOF_FALLBACK,
    ],
)
def test_cancellation_completion_publishes_final_worker_state(
    worker_state: supervisor._CancellationWorkerState,
) -> None:
    caller_channel, launcher_channel = socket.socketpair()
    release_worker = threading.Event()

    class PausedCompletion(threading.Event):
        def set(self) -> None:
            super().set()
            assert release_worker.wait(2.0)

    sender = supervisor._ApiLauncherCancellationSender(
        caller_channel,
        b"",
        _completed=PausedCompletion(),
        _worker_state=worker_state,
    )
    worker = threading.Thread(target=sender._finish, daemon=True)
    worker.start()
    try:
        assert sender.completed.wait(2.0)
        # Hold the sender at event publication so this cannot pass merely
        # because the scheduler lets it finish before the waiter resumes.
        assert sender._worker_state is supervisor._CancellationWorkerState.COMPLETE
    finally:
        release_worker.set()
        worker.join(timeout=2.0)
        caller_channel.close()
        launcher_channel.close()
    assert not worker.is_alive()


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
                # The injected sender interruption may already have closed this channel;
                # preserve the primary test outcome.
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


def test_public_run_real_interrupt_preserves_writer_and_cleans_trusted_tree(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "interrupted-public-writer.db"
    record_path = tmp_path / "interrupted-public-result.json"
    launcher_pid_path = tmp_path / "interrupted-public-launcher.pid"
    gui_pid_path = tmp_path / "interrupted-public-gui.pid"
    helper_pid_path = tmp_path / "interrupted-public-helper.pid"
    helper_ready_path = tmp_path / "interrupted-public-helper.ready"
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
        Path(os.environ["_QPLOT_TEST_HELPER_READY_PATH"]).write_text(
            "operation_hang", encoding="utf-8"
        )
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
            cancellation_stages = {}
            original_send = supervisor._send_public_api_cancellation_record
            original_wait = supervisor._wait_for_public_api_launcher_exit
            original_cleanup = supervisor._public_api_caller_cleanup_boundary

            def observe_stage(name):
                if interrupt_delivered_at:
                    cancellation_stages.setdefault(name, time.monotonic())

            def observed_send(sender):
                observe_stage("cancellation_send_started")
                try:
                    return original_send(sender)
                finally:
                    observe_stage("cancellation_send_finished")

            def observed_wait(launcher):
                observe_stage("launcher_exit_wait_started")
                try:
                    return original_wait(launcher)
                finally:
                    observe_stage("launcher_exit_wait_finished")

            def observed_cleanup(name):
                observe_stage(name)
                return original_cleanup(name)

            supervisor._send_public_api_cancellation_record = observed_send
            supervisor._wait_for_public_api_launcher_exit = observed_wait
            supervisor._public_api_caller_cleanup_boundary = observed_cleanup

            def interrupt_when_trusted_helper_is_stuck():
                ready_path = Path(os.environ["_QPLOT_TEST_HELPER_READY_PATH"])
                deadline = time.monotonic() + 6.0
                while True:
                    try:
                        ready = ready_path.read_text(encoding="utf-8")
                    except FileNotFoundError:
                        ready = ""
                    if ready == "operation_hang":
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError("trusted helper did not confirm its operation hang")
                    time.sleep(0.005)
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
                            "operation_hang_observed": True,
                            "cancellation_stages": {
                                name: observed_at - interrupt_delivered_at[0]
                                for name, observed_at in cancellation_stages.items()
                            },
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
                "_QPLOT_TEST_HELPER_READY_PATH": os.fspath(helper_ready_path),
                "_QPLOT_TEST_SENTINEL_PID_PATH": os.fspath(sentinel_pid_path),
            },
            timeout=12.0,
        )

        assert result.returncode == 0, result.stderr
        record = _read_json(record_path)
        assert record["exception_type"] == "KeyboardInterrupt"
        assert helper_ready_path.read_text(encoding="utf-8") == "operation_hang"
        assert record["immediate_running"] == {
            "launcher": False,
            "gui": False,
            "helper": False,
        }
        assert record["operation_hang_observed"] is True
        assert float(record["interrupt_to_catch"]) < 1.0, record
        assert record["protected_unchanged"] is True
        assert record["writer_alive_at_catch"] is True
        assert record["writer_committed_after_catch"] is True
        assert record["sentinel_alive_at_catch"] is True
    finally:
        for pid_path in pid_paths:
            if pid_path.exists():
                _force_cleanup_pid(int(pid_path.read_text(encoding="utf-8")))
