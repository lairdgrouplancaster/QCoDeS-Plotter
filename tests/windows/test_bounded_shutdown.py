"""Qt regressions for bounded Stage 4 retirement and application shutdown."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from threading import Event as ThreadEvent
from time import monotonic
from types import SimpleNamespace

import pytest
from PyQt6 import QtWidgets as qtw

from qplot.windows import main as main_window


def _offscreen_subprocess_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment.setdefault(
        "MPLCONFIGDIR",
        os.fspath(Path(tempfile.gettempdir()) / "qplot-matplotlib-cache"),
    )
    return environment


_REAL_QTHREADPOOL_SHUTDOWN_PROBE = textwrap.dedent(
    """
    import os
    import sys
    import threading
    import time
    from pathlib import Path
    from types import SimpleNamespace

    from PyQt6 import QtCore
    from PyQt6 import QtWidgets as qtw
    from qplot.__main__ import _finalize_confirmed_process_shutdown
    from qplot._shutdown_supervisor import ShutdownSupervisorClient
    from qplot.diagnostics import configure_logging
    from qplot.windows import main as main_window

    mode = sys.argv[1]
    log_path = Path(sys.argv[2])
    configure_logging(log_file=log_path, force=True)
    main_window._APPLICATION_SHUTDOWN_TIMEOUT_SECONDS = (
        1.5 if mode == "graceful" else 0.45
    )
    main_window._APPLICATION_SHUTDOWN_DIAGNOSTIC_GRACE_SECONDS = 0.10

    if mode == "blocked-diagnostics":
        diagnostic_gate = threading.Event()

        def block_diagnostic_io(**_kwargs):
            print("DIAGNOSTIC_IO_BLOCKED", flush=True)
            diagnostic_gate.wait(10.0)

        main_window._persist_shutdown_diagnostics = block_diagnostic_io

    class Config:
        def get(self, _key):
            return True

    class ProbeRunnable(QtCore.QRunnable):
        def run(self):
            print("RUNNABLE_STARTED", flush=True)
            if mode != "graceful":
                time.sleep(5.0)
                return
            time.sleep(0.06)
            print("RUNNABLE_FINISHED", flush=True)

    class StuckService:
        def escalate_cleanup_async(self):
            raise RuntimeError("exact escalation failure")

        def liveness(self):
            return SimpleNamespace(
                dispatcher_alive=False,
                control_alive=False,
                helper_alive=False,
                helper_pid=None,
                receiver_alive=True,
                open_supervisor_endpoints=1,
                unreaped_incarnations=1,
                resource_cleanup_pending=True,
                outstanding_requests=0,
                closing=True,
                closed=False,
            )

    class Window(qtw.QMainWindow):
        _finish_deferred_shutdown = (
            main_window.MainWindow._finish_deferred_shutdown
        )

        def __init__(self, process_fail_safe):
            super().__init__()
            self.config = Config()
            self.windows = []
            self._plot_workers = set()
            self.threadPool = QtCore.QThreadPool(self)
            self.threadPool.setMaxThreadCount(1)
            self.monitor = SimpleNamespace(stop=lambda: None)
            self.startupDatabaseTimer = SimpleNamespace(stop=lambda: None)
            self._database_load_generation = 0
            self._shutdown_started = False
            self._shutdown_ready = False
            self._shutdown_started_at = None
            self._shutdown_deadline = None
            self._shutdown_hard_deadline = None
            self._shutdown_cleanup_escalated = False
            self._shutdown_escalation_diagnostics = ()
            self._shutdown_liveness_diagnostics = ()
            self._shutdown_last_diagnostics = ()
            self._shutdown_diagnostics = ()
            self._shutdown_deadline_exhausted = False
            self._shutdown_process_fail_safe = process_fail_safe
            self._retired_trusted_read_services = (
                {StuckService()} if mode != "graceful" else set()
            )
            self._pending_trusted_read_services = {}
            self._trusted_read_service = None
            self.infoBox = SimpleNamespace(
                preview=SimpleNamespace(_workers={}, shutdown=lambda: None)
            )
            self._shutdown_timer = QtCore.QTimer(self)
            self._shutdown_timer.setInterval(5)
            self._shutdown_timer.timeout.connect(
                self._finish_deferred_shutdown
            )
            self.armed_hard_deadline = None

        def closeEvent(self, event):
            main_window.MainWindow.closeEvent(self, event)
            if (
                self._shutdown_hard_deadline is not None
                and self.armed_hard_deadline is None
            ):
                self.armed_hard_deadline = self._shutdown_hard_deadline
                print(
                    "SHUTDOWN_ARMED "
                    f"started={self._shutdown_started_at:.9f} "
                    f"diagnostic={self._shutdown_deadline:.9f} "
                    f"hard={self._shutdown_hard_deadline:.9f} "
                    f"failsafe_hard={self._shutdown_process_fail_safe._hard_deadline:.9f}",
                    flush=True,
                )

        def close_plot_windows(self, *, confirm, status):
            return True

        def close_database(self, *, status):
            return None

    application = qtw.QApplication.instance() or qtw.QApplication([])
    main_window.ask_confirmation_with_dont_ask_again = (
        lambda *args, **kwargs: qtw.QMessageBox.StandardButton.Yes
    )
    supervisor_client = ShutdownSupervisorClient.from_environment().connect()
    print(f"GUI_PID={os.getpid()}", flush=True)

    def suppress_local_force_exit(code):
        print(f"LOCAL_FORCE_EXIT_SUPPRESSED code={code}", flush=True)

    # A successful forced regression can therefore be satisfied only by the
    # independent direct-parent launcher, never by an in-GUI fallback thread.
    process_fail_safe = main_window._ProcessShutdownFailSafe(
        supervisor_client,
        force_exit=suppress_local_force_exit,
    )
    window = Window(process_fail_safe)
    runnable = ProbeRunnable()
    window.threadPool.start(runnable)
    window.show()
    print(f"SHUTDOWN_REQUEST_AT={time.monotonic():.9f}", flush=True)
    QtCore.QTimer.singleShot(0, window.close)
    QtCore.QTimer.singleShot(3000, lambda: application.exit(91))
    exit_code = application.exec()
    print(
        f"EVENT_LOOP_RETURNED exit={exit_code} "
        f"deadline_exhausted={window._shutdown_deadline_exhausted} "
        f"at={time.monotonic():.9f} "
        f"deadline_unchanged="
        f"{window._shutdown_hard_deadline == window.armed_hard_deadline} "
        f"failsafe_deadline_unchanged="
        f"{process_fail_safe._hard_deadline == window.armed_hard_deadline}",
        flush=True,
    )
    if exit_code != 0:
        raise SystemExit(exit_code)
    _finalize_confirmed_process_shutdown(
        application,
        window,
        process_fail_safe,
        retain_objects=False,
    )
    if mode == "graceful":
        supervisor_client.close()
        print(
            f"PROCESS_TEARDOWN_COMPLETED at={time.monotonic():.9f}",
            flush=True,
        )
        raise SystemExit(0)

    # If the injected no-op fallback returns at the deadline, remain alive so
    # that this probe still cannot pass without the external launcher kill.
    print("LOCAL_FALLBACK_RETURNED_WITHOUT_PROCESS_EXIT", flush=True)
    time.sleep(5.0)
    print("CHILD_EXITING_WITHOUT_LAUNCHER", flush=True)
    """
)


_SUPERVISOR_DRIVER = textwrap.dedent(
    """
    import os
    import sys

    from qplot._shutdown_supervisor import _supervise_child

    child_argv = [sys.executable, "-c", sys.argv[1], *sys.argv[2:]]
    raise SystemExit(
        _supervise_child(
            child_argv,
            env=os.environ,
            # This probe imports PyQt before it can authenticate.  Give cold
            # CI imports their own bounded setup budget; the asserted 0.45 s
            # shutdown deadline below is unchanged and starts only after
            # SHUTDOWN_REQUEST_AT is published.
            startup_timeout=30.0,
        )
    )
    """
)


_CONFIRMATION_REJECTION_PROBE = textwrap.dedent(
    """
    import os
    import time
    from types import SimpleNamespace

    from PyQt6 import QtCore
    from PyQt6 import QtWidgets as qtw
    from qplot._shutdown_supervisor import ShutdownSupervisorClient
    from qplot.windows import main as main_window

    main_window._APPLICATION_SHUTDOWN_TIMEOUT_SECONDS = 0.12

    class Window(qtw.QMainWindow):
        closeEvent = main_window.MainWindow.closeEvent

        def __init__(self, process_fail_safe):
            super().__init__()
            self.config = SimpleNamespace(get=lambda _key: True)
            self._shutdown_process_fail_safe = process_fail_safe

    application = qtw.QApplication.instance() or qtw.QApplication([])
    supervisor_client = ShutdownSupervisorClient.from_environment().connect()
    process_fail_safe = main_window._ProcessShutdownFailSafe(
        supervisor_client,
        force_exit=lambda code: print(
            f"UNEXPECTED_LOCAL_FORCE_EXIT code={code}", flush=True
        ),
    )
    main_window.ask_confirmation_with_dont_ask_again = (
        lambda *args, **kwargs: qtw.QMessageBox.StandardButton.No
    )
    window = Window(process_fail_safe)
    window.show()
    started_at = time.monotonic()
    print(f"GUI_PID={os.getpid()}", flush=True)
    QtCore.QTimer.singleShot(0, window.close)

    def verify_rejection():
        print(
            "CONFIRMATION_REJECTED "
            f"visible={window.isVisible()} "
            f"armed={process_fail_safe.armed} "
            f"elapsed={time.monotonic() - started_at:.6f}",
            flush=True,
        )
        application.exit(0)

    QtCore.QTimer.singleShot(260, verify_rejection)
    exit_code = application.exec()
    supervisor_client.close()
    print("REJECTED_CHILD_EXITING_NORMALLY", flush=True)
    # Stay alive briefly so the launcher deterministically observes pre-ARM
    # EOF, then reaps this child without ever signalling it.
    time.sleep(0.08)
    raise SystemExit(exit_code)
    """
)


def test_normal_zero_resource_shutdown_finishes_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed = []
    logged = []

    class Timer:
        stopped = False

        def stop(self) -> None:
            self.stopped = True

    harness = SimpleNamespace(
        _shutdown_started=True,
        _shutdown_ready=False,
        _shutdown_cleanup_escalated=False,
        _shutdown_deadline=None,
        _shutdown_timer=Timer(),
        _retired_trusted_read_services=set(),
        _pending_trusted_read_services={},
        _trusted_read_service=None,
        infoBox=SimpleNamespace(preview=SimpleNamespace(_workers={})),
    )
    monkeypatch.setattr(
        qtw.QApplication, "closeAllWindows", lambda: closed.append(True)
    )
    monkeypatch.setattr(
        main_window,
        "log_user_error",
        lambda *args, **kwargs: logged.append((args, kwargs)),
    )

    main_window.MainWindow._finish_deferred_shutdown(harness)

    assert harness._shutdown_timer.stopped
    assert not harness._shutdown_started
    assert harness._shutdown_ready
    assert closed == [True]
    assert logged == []


def test_cancelled_confirmation_never_arms_process_fail_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    armed = []

    class Event:
        accepted = False
        ignored = False

        def accept(self) -> None:
            self.accepted = True

        def ignore(self) -> None:
            self.ignored = True

    harness = SimpleNamespace(
        config=SimpleNamespace(get=lambda _key: True),
        _shutdown_process_fail_safe=SimpleNamespace(
            arm=lambda **kwargs: armed.append(kwargs)
        ),
    )
    monkeypatch.setattr(
        main_window,
        "ask_confirmation_with_dont_ask_again",
        lambda *args, **kwargs: qtw.QMessageBox.StandardButton.No,
    )
    event = Event()

    main_window.MainWindow.closeEvent(harness, event)

    assert event.ignored
    assert not event.accepted
    assert armed == []


def test_unavailable_supervisor_uses_original_deadline_local_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forced = ThreadEvent()
    forced_codes = []
    persisted = []
    monkeypatch.setattr(
        main_window,
        "_persist_shutdown_diagnostics",
        lambda **kwargs: persisted.append(kwargs),
    )
    process_fail_safe = main_window._ProcessShutdownFailSafe(
        startup_diagnostic="exact shutdown launcher startup failure",
        force_exit=lambda code: (forced_codes.append(code), forced.set()),
    )
    started_at = monotonic()

    startup_diagnostic = process_fail_safe.arm(
        started_at=started_at,
        diagnostic_deadline=started_at + 0.08,
        hard_deadline=started_at + 0.14,
    )
    process_fail_safe.update_diagnostics(("final resource_cleanup_pending=True",))

    assert startup_diagnostic == "exact shutdown launcher startup failure"
    assert not forced.wait(0.03)
    assert forced.wait(0.3)
    assert forced_codes == [main_window._APPLICATION_FORCED_SHUTDOWN_EXIT_CODE]
    assert persisted
    assert "exact shutdown launcher startup failure" in persisted[-1]["diagnostics"]
    assert "final resource_cleanup_pending=True" in persisted[-1]["diagnostics"]
    process_fail_safe.disarm()


def test_supervisor_arm_failure_is_exact_and_keeps_local_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forced = ThreadEvent()
    persisted = []

    class FailingClient:
        def arm(self, _hard_deadline):
            raise OSError("exact ARM transport failure")

    monkeypatch.setattr(
        main_window,
        "_persist_shutdown_diagnostics",
        lambda **kwargs: persisted.append(kwargs),
    )
    process_fail_safe = main_window._ProcessShutdownFailSafe(
        FailingClient(),
        force_exit=lambda _code: forced.set(),
    )
    started_at = monotonic()

    arm_diagnostic = process_fail_safe.arm(
        started_at=started_at,
        diagnostic_deadline=started_at + 0.05,
        hard_deadline=started_at + 0.10,
    )
    process_fail_safe.update_diagnostics(("final receiver_alive=True",))

    assert arm_diagnostic == (
        "process shutdown supervisor ARM raised OSError: exact ARM transport failure"
    )
    assert forced.wait(0.25)
    assert persisted
    assert arm_diagnostic in persisted[-1]["diagnostics"]
    assert "final receiver_alive=True" in persisted[-1]["diagnostics"]
    process_fail_safe.disarm()


def test_acknowledged_supervisor_can_disarm_only_the_local_fallback() -> None:
    forced = ThreadEvent()
    armed_deadlines = []

    class AcknowledgingClient:
        def arm(self, hard_deadline):
            armed_deadlines.append(hard_deadline)
            return None

    process_fail_safe = main_window._ProcessShutdownFailSafe(
        AcknowledgingClient(),
        force_exit=lambda _code: forced.set(),
    )
    started_at = monotonic()
    hard_deadline = started_at + 0.10

    assert (
        process_fail_safe.arm(
            started_at=started_at,
            diagnostic_deadline=started_at + 0.05,
            hard_deadline=hard_deadline,
        )
        is None
    )
    assert process_fail_safe.watchdog_operational()
    process_fail_safe.disarm()

    assert armed_deadlines == [hard_deadline]
    assert not process_fail_safe.armed
    assert not process_fail_safe.watchdog_operational()
    assert not forced.wait(0.15)


def test_repeated_arm_cannot_extend_the_first_deadline() -> None:
    armed_deadlines = []

    class AcknowledgingClient:
        def arm(self, hard_deadline):
            armed_deadlines.append(hard_deadline)
            return None

    process_fail_safe = main_window._ProcessShutdownFailSafe(
        AcknowledgingClient(),
        force_exit=lambda _code: None,
    )
    started_at = monotonic()
    first_deadline = started_at + 1.0

    process_fail_safe.arm(
        started_at=started_at,
        diagnostic_deadline=started_at + 0.8,
        hard_deadline=first_deadline,
    )
    process_fail_safe.arm(
        started_at=started_at,
        diagnostic_deadline=started_at + 8.0,
        hard_deadline=started_at + 10.0,
    )

    assert armed_deadlines == [first_deadline]
    assert process_fail_safe._hard_deadline == first_deadline
    process_fail_safe.disarm()


def test_diagnostic_publication_is_memory_only_until_persisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persistence_calls = []
    monkeypatch.setattr(
        main_window,
        "_persist_shutdown_diagnostics",
        lambda **kwargs: persistence_calls.append(kwargs),
    )
    process_fail_safe = main_window._ProcessShutdownFailSafe(
        force_exit=lambda _code: None
    )
    started_at = monotonic()
    process_fail_safe.arm(
        started_at=started_at,
        diagnostic_deadline=started_at + 1.0,
        hard_deadline=started_at + 2.0,
    )

    process_fail_safe.update_diagnostics(("exact in-memory liveness",))

    assert persistence_calls == []
    process_fail_safe.disarm()


def test_async_diagnostic_persistence_never_blocks_its_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persistence_entered = ThreadEvent()
    release_persistence = ThreadEvent()

    def blocked_persistence(**_kwargs):
        persistence_entered.set()
        release_persistence.wait(1.0)

    monkeypatch.setattr(
        main_window,
        "_persist_shutdown_diagnostics",
        blocked_persistence,
    )
    process_fail_safe = main_window._ProcessShutdownFailSafe(
        force_exit=lambda _code: None
    )
    started_at = monotonic()
    process_fail_safe.arm(
        started_at=started_at,
        diagnostic_deadline=started_at + 1.0,
        hard_deadline=started_at + 2.0,
    )
    process_fail_safe.update_diagnostics(("exact blocked persistence",))

    call_started_at = monotonic()
    process_fail_safe.persist_async()
    call_elapsed = monotonic() - call_started_at

    assert call_elapsed < 0.10
    assert persistence_entered.wait(0.3)
    process_fail_safe.disarm()
    release_persistence.set()


def _run_supervised_probe(script: str, *arguments: str, timeout: float = 40.0):
    environment = _offscreen_subprocess_environment()
    launcher = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _SUPERVISOR_DRIVER,
            script,
            *arguments,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    stdout, stderr = launcher.communicate(timeout=timeout)
    completed_at = monotonic()
    completed = subprocess.CompletedProcess(
        launcher.args,
        launcher.returncode,
        stdout,
        stderr,
    )
    return completed, completed_at, launcher.pid


def _run_real_qthreadpool_shutdown_probe(mode: str, log_path: Path):
    return _run_supervised_probe(
        _REAL_QTHREADPOOL_SHUTDOWN_PROBE,
        mode,
        os.fspath(log_path),
    )


def _probe_value(output: str, prefix: str, key: str) -> str:
    line = next(line for line in output.splitlines() if line.startswith(prefix))
    fields = dict(field.split("=", 1) for field in line.split()[1:])
    return fields[key]


def test_real_qthreadpool_stall_forces_os_process_exit_after_diagnostics(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "stuck-qthreadpool.log"

    completed, completed_at, launcher_pid = _run_real_qthreadpool_shutdown_probe(
        "stuck", log_path
    )

    output = completed.stdout + completed.stderr
    assert completed.returncode == main_window._APPLICATION_FORCED_SHUTDOWN_EXIT_CODE, (
        output
    )
    assert launcher_pid > 0
    request_line = next(
        line
        for line in completed.stdout.splitlines()
        if line.startswith("SHUTDOWN_REQUEST_AT=")
    )
    shutdown_elapsed = completed_at - float(request_line.partition("=")[2])
    assert 0.25 <= shutdown_elapsed < 1.5
    shutdown_started_at = float(
        _probe_value(completed.stdout, "SHUTDOWN_ARMED", "started")
    )
    diagnostic_deadline = float(
        _probe_value(completed.stdout, "SHUTDOWN_ARMED", "diagnostic")
    )
    hard_deadline = float(_probe_value(completed.stdout, "SHUTDOWN_ARMED", "hard"))
    failsafe_deadline = float(
        _probe_value(completed.stdout, "SHUTDOWN_ARMED", "failsafe_hard")
    )
    assert 0.42 <= hard_deadline - shutdown_started_at <= 0.48
    assert 0.07 <= hard_deadline - diagnostic_deadline <= 0.13
    assert failsafe_deadline == hard_deadline
    assert hard_deadline - 0.04 <= completed_at <= hard_deadline + 0.20
    assert "RUNNABLE_STARTED" in completed.stdout
    assert "GUI_PID=" in completed.stdout
    assert "EVENT_LOOP_RETURNED exit=0 deadline_exhausted=True" in completed.stdout
    assert "deadline_unchanged=True" in completed.stdout
    assert "failsafe_deadline_unchanged=True" in completed.stdout
    assert "PROCESS_TEARDOWN_COMPLETED" not in completed.stdout
    assert "CHILD_EXITING_WITHOUT_LAUNCHER" not in completed.stdout
    event_loop_returned_at = float(
        _probe_value(completed.stdout, "EVENT_LOOP_RETURNED", "at")
    )
    assert completed_at - event_loop_returned_at >= 0.04
    persisted = log_path.read_text(encoding="utf-8")
    assert "Bounded Application Shutdown" in persisted
    assert "pool threadPool: active_threads=1" in persisted
    assert "RuntimeError: exact escalation failure" in persisted
    assert "receiver_alive=True" in persisted
    assert "open_supervisor_endpoints=1" in persisted
    assert "unreaped_incarnations=1" in persisted
    assert "resource_cleanup_pending=True" in persisted
    assert "closed=False" in persisted


def test_real_qthreadpool_completion_disarms_after_graceful_destruction(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "graceful-qthreadpool.log"

    completed, completed_at, launcher_pid = _run_real_qthreadpool_shutdown_probe(
        "graceful", log_path
    )

    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output
    assert launcher_pid > 0
    assert "GUI_PID=" in completed.stdout
    assert "RUNNABLE_STARTED" in completed.stdout
    assert "RUNNABLE_FINISHED" in completed.stdout
    assert "EVENT_LOOP_RETURNED exit=0 deadline_exhausted=False" in completed.stdout
    assert "deadline_unchanged=True" in completed.stdout
    assert "failsafe_deadline_unchanged=True" in completed.stdout
    assert "PROCESS_TEARDOWN_COMPLETED" in completed.stdout
    assert "LOCAL_FORCE_EXIT_SUPPRESSED" not in completed.stdout
    assert completed.stdout.index("EVENT_LOOP_RETURNED") < completed.stdout.index(
        "PROCESS_TEARDOWN_COMPLETED"
    )
    request_at = float(
        next(
            line.partition("=")[2]
            for line in completed.stdout.splitlines()
            if line.startswith("SHUTDOWN_REQUEST_AT=")
        )
    )
    shutdown_started_at = float(
        _probe_value(completed.stdout, "SHUTDOWN_ARMED", "started")
    )
    hard_deadline = float(_probe_value(completed.stdout, "SHUTDOWN_ARMED", "hard"))
    teardown_completed_at = float(
        _probe_value(completed.stdout, "PROCESS_TEARDOWN_COMPLETED", "at")
    )
    assert 1.45 <= hard_deadline - shutdown_started_at <= 1.55
    assert teardown_completed_at - request_at >= 0.04
    assert teardown_completed_at < hard_deadline
    assert "Bounded Application Shutdown" not in log_path.read_text(encoding="utf-8")


def test_blocked_diagnostic_io_cannot_delay_external_launcher_kill(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "blocked-diagnostics.log"

    completed, completed_at, launcher_pid = _run_real_qthreadpool_shutdown_probe(
        "blocked-diagnostics", log_path
    )

    output = completed.stdout + completed.stderr
    assert completed.returncode == main_window._APPLICATION_FORCED_SHUTDOWN_EXIT_CODE, (
        output
    )
    assert launcher_pid > 0
    shutdown_started_at = float(
        _probe_value(completed.stdout, "SHUTDOWN_ARMED", "started")
    )
    hard_deadline = float(_probe_value(completed.stdout, "SHUTDOWN_ARMED", "hard"))
    failsafe_deadline = float(
        _probe_value(completed.stdout, "SHUTDOWN_ARMED", "failsafe_hard")
    )

    assert 0.42 <= hard_deadline - shutdown_started_at <= 0.48
    assert failsafe_deadline == hard_deadline
    assert hard_deadline - 0.04 <= completed_at <= hard_deadline + 0.20
    assert "DIAGNOSTIC_IO_BLOCKED" in completed.stdout
    assert "CHILD_EXITING_WITHOUT_LAUNCHER" not in completed.stdout


def test_rejected_confirmation_leaves_supervisor_unarmed_and_gui_open() -> None:
    completed, _completed_at, launcher_pid = _run_supervised_probe(
        _CONFIRMATION_REJECTION_PROBE
    )

    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output
    assert launcher_pid > 0
    assert "GUI_PID=" in completed.stdout
    assert "CONFIRMATION_REJECTED visible=True armed=False" in completed.stdout
    elapsed = float(_probe_value(completed.stdout, "CONFIRMATION_REJECTED", "elapsed"))
    assert elapsed >= 0.22
    assert "REJECTED_CHILD_EXITING_NORMALLY" in completed.stdout
    assert "UNEXPECTED_LOCAL_FORCE_EXIT" not in completed.stdout
    assert "shutdown launcher control channel closed before ARM" in completed.stderr


@pytest.mark.parametrize(
    ("mode", "expected_diagnostic"),
    [
        ("owned", "resource_cleanup_pending=True"),
        ("liveness-error", "RuntimeError: injected liveness failure"),
        ("escalation-error", "RuntimeError: exact escalation failure"),
    ],
)
def test_qt_subprocess_exits_at_one_deadline_with_exact_diagnostics(
    mode: str,
    expected_diagnostic: str,
) -> None:
    script = (
        textwrap.dedent(
            """
        import sys
        from time import monotonic
        from types import SimpleNamespace

        from PyQt6 import QtCore
        from PyQt6 import QtWidgets as qtw
        from qplot.windows import main as main_window

        mode = __MODE__
        main_window._APPLICATION_SHUTDOWN_TIMEOUT_SECONDS = 0.12
        records = []
        main_window.log_user_error = (
            lambda *args, **kwargs: records.append((args, kwargs))
        )

        class Service:
            def __init__(self):
                self.escalations = 0
                self.close_async_calls = 0

            def close_async(self):
                self.close_async_calls += 1

            def escalate_cleanup_async(self):
                self.escalations += 1
                if mode == "escalation-error":
                    raise RuntimeError("exact escalation failure")

            def liveness(self):
                if mode == "liveness-error":
                    raise RuntimeError("injected liveness failure")
                return SimpleNamespace(
                    dispatcher_alive=False,
                    control_alive=False,
                    helper_alive=False,
                    helper_pid=None,
                    receiver_alive=True,
                    open_supervisor_endpoints=1,
                    unreaped_incarnations=1,
                    resource_cleanup_pending=True,
                    outstanding_requests=0,
                    closing=False,
                    closed=True,
                )

        class Config:
            def get(self, _key):
                return False

        class Pool:
            def activeThreadCount(self):
                return 0

            def clear(self):
                return None

        class Window(qtw.QMainWindow):
            closeEvent = main_window.MainWindow.closeEvent
            _shutdown_background_work_active = (
                main_window.MainWindow._shutdown_background_work_active
            )
            _escalate_shutdown_cleanup = (
                main_window.MainWindow._escalate_shutdown_cleanup
            )
            _complete_deferred_shutdown = (
                main_window.MainWindow._complete_deferred_shutdown
            )
            _finish_deferred_shutdown = (
                main_window.MainWindow._finish_deferred_shutdown
            )

            def __init__(self, service):
                super().__init__()
                self.config = Config()
                self.windows = []
                self._plot_workers = set()
                self.threadPool = Pool()
                self.monitor = SimpleNamespace(stop=lambda: None)
                self.startupDatabaseTimer = SimpleNamespace(stop=lambda: None)
                self._database_load_generation = 0
                self._shutdown_started = False
                self._shutdown_ready = False
                self._shutdown_cleanup_escalated = False
                self._shutdown_started_at = None
                self._shutdown_deadline = None
                self._shutdown_last_diagnostics = ()
                self._shutdown_diagnostics = ()
                self._retired_trusted_read_services = set()
                self._pending_trusted_read_services = {}
                self._trusted_read_service = service
                self.infoBox = SimpleNamespace(
                    preview=SimpleNamespace(_workers={}, shutdown=lambda: None)
                )
                self._shutdown_timer = QtCore.QTimer(self)
                self._shutdown_timer.setInterval(5)
                self._shutdown_timer.timeout.connect(
                    self._finish_deferred_shutdown
                )

            def close_plot_windows(self, *, confirm, status):
                return True

            def close_database(self, *, status):
                service = self._trusted_read_service
                self._trusted_read_service = None
                self._retired_trusted_read_services.add(service)
                service.close_async()

        application = qtw.QApplication.instance() or qtw.QApplication([])
        service = Service()
        window = Window(service)
        window.show()
        started = monotonic()
        QtCore.QTimer.singleShot(0, window.close)
        QtCore.QTimer.singleShot(2000, lambda: application.exit(91))
        exit_code = application.exec()
        elapsed = monotonic() - started
        diagnostics = "\\n".join(window._shutdown_diagnostics)
        print(
            f"exit={exit_code} elapsed={elapsed:.3f} "
            f"escalations={service.escalations} diagnostics={diagnostics}",
            flush=True,
        )
        if exit_code != 0:
            sys.exit(exit_code)
        if elapsed >= 1.0:
            raise AssertionError(f"bounded shutdown took {elapsed:.3f}s")
        if service.escalations != 1:
            raise AssertionError(f"cleanup escalations={service.escalations}")
        if service.close_async_calls != 1:
            raise AssertionError(f"close_async calls={service.close_async_calls}")
        if window._shutdown_deadline is None:
            raise AssertionError("closeEvent did not install a shutdown deadline")
        if not records:
            raise AssertionError("deadline exhaustion was not logged")
        if __EXPECTED__ not in diagnostics:
            raise AssertionError(diagnostics)
        if (
            mode == "escalation-error"
            and "resource_cleanup_pending=True" not in diagnostics
        ):
            raise AssertionError(diagnostics)
        """
        )
        .replace("__MODE__", repr(mode))
        .replace(
            "__EXPECTED__",
            repr(expected_diagnostic),
        )
    )
    environment = _offscreen_subprocess_environment()

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=6.0,
        env=environment,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "exit=0" in completed.stdout
    assert "escalations=1" in completed.stdout
