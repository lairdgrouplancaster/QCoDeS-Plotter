from __future__ import annotations

import os
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from types import SimpleNamespace

import pytest

from qplot import _shutdown_supervisor as supervisor
from qplot import _windows_shutdown_job as jobs


class _FakeAdapter:
    def __init__(self) -> None:
        self.calls: list[object] = []
        self.signalled = False
        self.active = 0
        self.exit_code = 0
        self.in_job = True
        self.configure_failure = False
        self.create_failure = False
        self.resume_failure = False
        self.terminate_failures = 0
        self.wait_failures = 0
        self.exit_code_failures = 0
        self.query_failures = 0
        self.close_failures = 0
        self.created_argv: list[str] | None = None
        self.created_env: dict[str, str] | None = None

    def create_job(self):
        self.calls.append("create_job")
        return "job"

    def configure_kill_on_close(self, job_handle):
        self.calls.append(("configure_kill_on_close", job_handle))
        if self.configure_failure:
            raise OSError("exact Job configuration failure")

    def create_process_in_job(
        self,
        job_handle,
        argv: Sequence[str | os.PathLike[str]],
        env: Mapping[str, str],
    ):
        self.calls.append(("create_process_in_job", job_handle))
        if self.create_failure:
            raise OSError("exact contained process creation failure")
        self.created_argv = [os.fsdecode(argument) for argument in argv]
        self.created_env = dict(env)
        self.active = 1
        return jobs._SpawnedHandles("process", "thread", 4312)

    def process_is_in_job(self, process_handle, job_handle):
        self.calls.append(("process_is_in_job", process_handle, job_handle))
        if not self.in_job:
            self.active = 0
        return self.in_job

    def resume_thread(self, thread_handle):
        self.calls.append(("resume_thread", thread_handle))
        if self.resume_failure:
            raise OSError("exact resume failure")

    def wait_process(self, process_handle, milliseconds):
        self.calls.append(("wait_process", process_handle, milliseconds))
        if self.wait_failures:
            self.wait_failures -= 1
            raise OSError("exact first wait failure")
        return self.signalled

    def process_exit_code(self, process_handle):
        self.calls.append(("process_exit_code", process_handle))
        if self.exit_code_failures:
            self.exit_code_failures -= 1
            raise OSError("exact first status failure")
        return self.exit_code

    def terminate_job(self, job_handle, exit_code):
        self.calls.append(("terminate_job", job_handle, exit_code))
        if self.terminate_failures:
            self.terminate_failures -= 1
            raise OSError("exact first Job termination failure")
        if not self.signalled:
            self.exit_code = exit_code
        self.signalled = True
        self.active = 0

    def terminate_process(self, process_handle, exit_code):
        self.calls.append(("terminate_process", process_handle, exit_code))
        self.signalled = True
        self.exit_code = exit_code

    def active_processes(self, job_handle):
        self.calls.append(("active_processes", job_handle))
        if self.query_failures:
            self.query_failures -= 1
            raise OSError("exact first Job query failure")
        return self.active

    def close_handle(self, handle):
        self.calls.append(("close_handle", handle))
        if self.close_failures:
            self.close_failures -= 1
            raise OSError("exact first handle close failure")

    def complete(self, exit_code: int) -> None:
        self.exit_code = exit_code
        self.signalled = True
        self.active = 0


class _FakeClock:
    def __init__(self, now: float) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now


class _AdvancingWaitAdapter(_FakeAdapter):
    def __init__(
        self,
        clock: _FakeClock,
        *,
        advance_seconds: float,
        wait_result: bool = False,
        wait_error: BaseException | None = None,
        observed_exit_code: int = 70,
    ) -> None:
        super().__init__()
        self.clock = clock
        self.advance_seconds = advance_seconds
        self.wait_result = wait_result
        self.wait_error = wait_error
        self.observed_exit_code = observed_exit_code

    def wait_process(self, process_handle, milliseconds):
        self.calls.append(("wait_process", process_handle, milliseconds))
        self.clock.now += self.advance_seconds
        error = self.wait_error
        self.wait_error = None
        if error is not None:
            raise error
        if self.wait_result:
            self.complete(self.observed_exit_code)
            return True
        return self.signalled


def _spawn(adapter: _FakeAdapter) -> jobs.WindowsContainedProcess:
    return jobs.spawn_contained(
        ["python.exe", "argument with spaces", "雪"],
        {"QPLOT_UNICODE": "π", "PATH": "preserved"},
        adapter=adapter,
    )


def test_module_is_importable_without_loading_windows_dlls() -> None:
    assert jobs._WINDOWS_CONTAINED_CREATION_FLAGS & jobs._CREATE_SUSPENDED
    assert jobs._WINDOWS_CONTAINED_CREATION_FLAGS & (jobs._EXTENDED_STARTUPINFO_PRESENT)
    assert jobs._WINDOWS_CONTAINED_CREATION_FLAGS & (jobs._CREATE_UNICODE_ENVIRONMENT)
    if os.name != "nt":
        with pytest.raises(OSError, match="only available on Windows"):
            jobs._CtypesWindowsJobAdapter()


def test_handle_value_accepts_ctypes_handles_and_handle_arrays() -> None:
    ordinary = jobs.wintypes.HANDLE(3)
    invalid = jobs.wintypes.HANDLE(-1)
    handles = (jobs.wintypes.HANDLE * 2)(ordinary, invalid)

    assert jobs._handle_value(ordinary) == 3
    assert jobs._handle_value(handles[0]) == 3
    assert jobs._handle_value(invalid) == jobs._invalid_handle_value()
    assert jobs._handle_value(handles[1]) == jobs._invalid_handle_value()


def test_duplicated_standard_handle_close_failure_is_exact_and_retained() -> None:
    retained = jobs.wintypes.HANDLE(7)
    invalid = jobs.wintypes.HANDLE(-1)

    def fail_close(handle) -> None:
        assert handle is retained
        raise OSError("exact inherited-handle close failure")

    adapter = SimpleNamespace(close_handle=fail_close)
    deferred, failures = (
        jobs._CtypesWindowsJobAdapter._close_duplicated_standard_handles(
            adapter,
            (retained, invalid),
        )
    )

    assert deferred == (retained,)
    assert failures == (
        "Windows duplicated standard-handle close raised OSError: "
        "exact inherited-handle close failure",
    )


def test_unicode_environment_block_is_sorted_and_double_terminated() -> None:
    block = jobs._environment_block(
        {
            "z-last": "雪",
            "Alpha": "π",
            "=C:": "C:\\working directory",
        }
    )

    assert block == ("=C:=C:\\working directory\0Alpha=π\0z-last=雪\0\0")


def test_spawn_configures_atomic_job_before_resuming_and_preserves_inputs() -> None:
    adapter = _FakeAdapter()

    child = _spawn(adapter)

    assert child.pid == 4312
    assert child.returncode is None
    assert child.args == ["python.exe", "argument with spaces", "雪"]
    assert adapter.created_argv == child.args
    assert adapter.created_env == {"QPLOT_UNICODE": "π", "PATH": "preserved"}
    assert adapter.calls[:6] == [
        "create_job",
        ("configure_kill_on_close", "job"),
        ("create_process_in_job", "job"),
        ("process_is_in_job", "process", "job"),
        ("resume_thread", "thread"),
        ("close_handle", "thread"),
    ]

    adapter.complete(17)
    assert child.wait(timeout=0.1) == 17
    child.wait_tree_empty(timeout=0.1)
    child.close_after_empty()
    assert child.closed
    assert adapter.calls[-2:] == [
        ("close_handle", "process"),
        ("close_handle", "job"),
    ]


def test_empty_job_close_failure_is_exact_without_extending_startup() -> None:
    adapter = _FakeAdapter()
    adapter.configure_failure = True
    adapter.close_failures = 1
    started_at = time.monotonic()

    with pytest.raises(jobs.WindowsShutdownJobError) as failure:
        _spawn(adapter)

    assert time.monotonic() - started_at < 0.1
    assert "exact Job configuration failure" in str(failure.value)
    assert "empty Windows Job Object handle close" in str(failure.value)
    assert "exact first handle close failure" in str(failure.value)
    assert adapter.calls.count(("close_handle", "job")) == 1


def test_process_creation_and_empty_job_close_failures_are_both_exact() -> None:
    adapter = _FakeAdapter()
    adapter.create_failure = True
    adapter.close_failures = 1

    with pytest.raises(jobs.WindowsShutdownJobError) as failure:
        _spawn(adapter)

    assert "exact contained process creation failure" in str(failure.value)
    assert "empty Windows Job Object handle close" in str(failure.value)
    assert "exact first handle close failure" in str(failure.value)
    assert adapter.calls.count(("close_handle", "job")) == 1


def test_initial_thread_close_failure_is_retained_without_blocking_startup() -> None:
    adapter = _FakeAdapter()
    adapter.close_failures = 1

    child = _spawn(adapter)

    assert child.failures == (
        "Windows initial-thread handle close raised OSError: "
        "exact first handle close failure",
    )
    assert not child.closed
    adapter.complete(17)
    assert child.wait(timeout=0.1) == 17
    child.wait_tree_empty(timeout=0.1)
    child.close_after_empty()
    assert child.closed
    assert adapter.calls.count(("close_handle", "thread")) == 2


def test_first_termination_failure_is_retried_after_kill_first() -> None:
    adapter = _FakeAdapter()
    adapter.terminate_failures = 1
    child = _spawn(adapter)
    calls_before_termination = len(adapter.calls)

    child.terminate_tree(70)

    termination_calls = adapter.calls[calls_before_termination:]
    assert termination_calls[0] == ("terminate_job", "job", 70)
    assert termination_calls == [
        ("terminate_job", "job", 70),
        ("active_processes", "job"),
        ("terminate_job", "job", 70),
    ]
    assert child.termination_requested
    assert child.failures == (
        "Windows Job Object termination raised OSError: "
        "exact first Job termination failure",
    )
    assert child.wait(timeout=0.1) == 70
    child.wait_tree_empty(timeout=0.1)
    child.close_after_empty()


def test_first_wait_status_query_and_close_failures_retry_without_release() -> None:
    adapter = _FakeAdapter()
    adapter.wait_failures = 1
    adapter.exit_code_failures = 1
    adapter.query_failures = 1
    child = _spawn(adapter)
    adapter.close_failures = 1
    adapter.complete(23)

    assert child.wait(timeout=0.2) == 23
    child.wait_tree_empty(timeout=0.2)
    child.close_after_empty()

    assert child.closed
    assert child.failures == (
        "Windows direct-process wait raised OSError: exact first wait failure",
        "Windows direct-process exit-status read raised OSError: "
        "exact first status failure",
        "Windows Job Object liveness query raised OSError: "
        "exact first Job query failure",
        "Windows direct-process handle close raised OSError: "
        "exact first handle close failure",
    )
    assert adapter.calls.count(("close_handle", "process")) == 2
    assert adapter.calls.index(("close_handle", "job")) > adapter.calls.index(
        ("close_handle", "process")
    )


def test_close_refuses_live_tree_and_preserves_both_owner_handles() -> None:
    adapter = _FakeAdapter()
    child = _spawn(adapter)

    with pytest.raises(jobs.WindowsShutdownJobError, match="live retained"):
        child.close_after_empty()

    assert not child.closed
    assert ("close_handle", "process") not in adapter.calls
    assert ("close_handle", "job") not in adapter.calls
    child.terminate_tree()
    child.wait()
    child.wait_tree_empty()
    child.close_after_empty()


def test_failed_membership_verification_terminates_suspended_exact_handle() -> None:
    adapter = _FakeAdapter()
    adapter.in_job = False

    with pytest.raises(
        jobs.WindowsShutdownJobError,
        match="outside its retained Job Object",
    ):
        _spawn(adapter)

    assert ("resume_thread", "thread") not in adapter.calls
    assert ("terminate_process", "process", 70) in adapter.calls
    assert ("close_handle", "process") in adapter.calls
    assert ("close_handle", "job") in adapter.calls


def test_activation_abort_retains_original_and_retry_diagnostics() -> None:
    adapter = _FakeAdapter()
    adapter.resume_failure = True
    adapter.terminate_failures = 1

    with pytest.raises(jobs.WindowsShutdownJobError) as failure:
        _spawn(adapter)

    diagnostic = str(failure.value)
    assert (
        "contained Windows GUI process activation raised OSError: exact resume failure"
    ) in diagnostic
    assert (
        "Windows Job Object termination raised OSError: "
        "exact first Job termination failure"
    ) in diagnostic
    assert ("close_handle", "process") in adapter.calls
    assert ("close_handle", "job") in adapter.calls


def test_tree_wait_timeout_never_closes_or_extends_ownership() -> None:
    adapter = _FakeAdapter()
    child = _spawn(adapter)
    started_at = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired):
        child.wait_tree_empty(timeout=0.025)

    assert time.monotonic() - started_at < 0.15
    assert not child.closed
    assert ("close_handle", "process") not in adapter.calls
    assert ("close_handle", "job") not in adapter.calls
    child.terminate_tree()
    child.wait()
    child.wait_tree_empty()
    child.close_after_empty()


def test_absolute_deadline_observation_floors_wait_and_retains_natural_70_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(100.0)
    adapter = _AdvancingWaitAdapter(
        clock,
        advance_seconds=0.00025,
        wait_result=True,
        observed_exit_code=70,
    )
    monkeypatch.setattr(jobs.time, "monotonic", clock.monotonic)
    child = _spawn(adapter)
    calls_before_observation = len(adapter.calls)

    observed = child.observe_before_deadline(100.0029)

    observation_calls = adapter.calls[calls_before_observation:]
    assert observed is True
    assert observation_calls == [("wait_process", "process", 2)]
    assert ("process_exit_code", "process") not in observation_calls
    assert child.returncode is None

    outcome = supervisor._terminate_and_reap_windows_child(child)

    assert outcome.return_code == 70
    assert not outcome.forced
    assert child.direct_exit_preceded_termination
    assert child.closed


def test_wait_crossing_absolute_deadline_returns_without_status_before_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(200.0)
    adapter = _AdvancingWaitAdapter(
        clock,
        advance_seconds=0.003,
        wait_result=True,
        observed_exit_code=70,
    )
    monkeypatch.setattr(jobs.time, "monotonic", clock.monotonic)
    child = _spawn(adapter)
    calls_before_observation = len(adapter.calls)

    outcome = supervisor._wait_for_armed_windows_child(
        child,
        200.0029,
        supervisor._LauncherSignalState(),
    )

    calls_after_observation = adapter.calls[calls_before_observation:]
    assert calls_after_observation[:2] == [
        ("wait_process", "process", 2),
        ("terminate_job", "job", 70),
    ]
    assert outcome == supervisor._SupervisionOutcome(70, forced=True)
    assert not child.direct_exit_preceded_termination
    assert child.closed


def test_wait_failure_crossing_deadline_adds_no_diagnostic_before_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(300.0)
    adapter = _AdvancingWaitAdapter(
        clock,
        advance_seconds=0.003,
        wait_error=OSError("deadline-crossing wait failure"),
    )
    monkeypatch.setattr(jobs.time, "monotonic", clock.monotonic)
    child = _spawn(adapter)
    calls_before_observation = len(adapter.calls)

    outcome = supervisor._wait_for_armed_windows_child(
        child,
        300.0029,
        supervisor._LauncherSignalState(),
    )

    assert adapter.calls[calls_before_observation:][:2] == [
        ("wait_process", "process", 2),
        ("terminate_job", "job", 70),
    ]
    assert outcome.forced
    assert outcome.diagnostics == (
        "shutdown launcher Windows child observation raised OSError: "
        "deadline-crossing wait failure",
    )
    assert child.closed


def test_permanent_termination_failure_holds_ownership_until_retry_succeeds() -> None:
    adapter = _FakeAdapter()
    adapter.terminate_failures = 10_000
    child = _spawn(adapter)
    completed = threading.Event()

    thread = threading.Thread(
        target=lambda: (child.terminate_tree(), completed.set()),
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 0.2
    while not child.failures and time.monotonic() < deadline:
        time.sleep(0.005)

    assert child.failures
    assert len(child.failures) == 1
    assert not completed.is_set()
    assert not child.closed
    assert ("close_handle", "job") not in adapter.calls

    adapter.terminate_failures = 0
    assert completed.wait(0.3)
    child.wait()
    child.wait_tree_empty()
    child.close_after_empty()
    thread.join(timeout=0.2)


def test_supervisor_deadline_kills_job_first_and_retains_exact_failures() -> None:
    adapter = _FakeAdapter()
    adapter.terminate_failures = 1
    child = _spawn(adapter)
    calls_before_deadline = len(adapter.calls)

    outcome = supervisor._wait_for_armed_windows_child(
        child,
        time.monotonic() - 1.0,
        supervisor._LauncherSignalState(),
    )

    deadline_calls = adapter.calls[calls_before_deadline:]
    assert deadline_calls[0] == ("terminate_job", "job", 70)
    assert outcome.return_code == 70
    assert outcome.forced
    assert outcome.diagnostics == (
        "Windows Job Object termination raised OSError: "
        "exact first Job termination failure",
    )
    assert child.closed


def test_supervisor_preserves_status_known_before_residual_tree_cleanup() -> None:
    adapter = _FakeAdapter()
    child = _spawn(adapter)
    adapter.complete(17)
    assert child.wait(timeout=0.1) == 17

    outcome = supervisor._terminate_and_reap_windows_child(child)

    assert outcome.return_code == 17
    assert not outcome.forced
    assert child.direct_exit_preceded_termination
    assert child.closed


def test_supervisor_deadline_race_preserves_uncached_natural_status() -> None:
    adapter = _FakeAdapter()
    child = _spawn(adapter)
    # The kernel handle is signalled, but the launcher has not yet cached the
    # status when its deadline branch performs the required kill-first call.
    adapter.complete(17)
    assert child.returncode is None

    outcome = supervisor._wait_for_armed_windows_child(
        child,
        time.monotonic() - 1.0,
        supervisor._LauncherSignalState(),
    )

    assert outcome.return_code == 17
    assert not outcome.forced
    assert child.termination_requested
    assert not child.direct_exit_preceded_termination
    assert child.closed
