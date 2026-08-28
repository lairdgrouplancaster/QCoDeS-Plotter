"""Retained Windows Job Object containment for the qPlot GUI process tree.

The module is importable on every platform and loads ``kernel32`` only when a
real Windows adapter is constructed.  Production process creation is atomic
with respect to Job Object membership: ``PROC_THREAD_ATTRIBUTE_JOB_LIST``
places the suspended GUI in the retained job as part of ``CreateProcessW``.
The initial thread is resumed only after membership has been verified.

No production operation opens or terminates a process by PID.  The PID is
retained only for authenticated protocol comparison and diagnostics; all
liveness, status, and termination operations use the original process and job
handles.
"""

from __future__ import annotations

import ctypes
import math
import os
import subprocess
import time
from collections.abc import Mapping, Sequence
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, Protocol

_FORCED_SHUTDOWN_EXIT_CODE = 70
_RETRY_INTERVAL_SECONDS = 0.01
_WAIT_SLICE_MILLISECONDS = 50
_MAX_WAIT_MILLISECONDS = 0xFFFFFFFE
_FAILURE_DIAGNOSTIC_LIMIT = 64

_CREATE_SUSPENDED = 0x00000004
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_WINDOWS_CONTAINED_CREATION_FLAGS = (
    _CREATE_SUSPENDED | _CREATE_UNICODE_ENVIRONMENT | _EXTENDED_STARTUPINFO_PRESENT
)

_STARTF_USESTDHANDLES = 0x00000100
_DUPLICATE_SAME_ACCESS = 0x00000002
_FILE_TYPE_CHAR = 0x0002
_INVALID_HANDLE_VALUE = -1

_PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
_PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002000D

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS = 1
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9

_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102
_WAIT_FAILED = 0xFFFFFFFF
_ERROR_INSUFFICIENT_BUFFER = 122

_STD_INPUT_HANDLE = -10
_STD_OUTPUT_HANDLE = -11
_STD_ERROR_HANDLE = -12


class WindowsShutdownJobError(RuntimeError):
    """Windows process-tree containment could not be established or observed."""


@dataclass(frozen=True)
class _SpawnedHandles:
    process_handle: Any
    thread_handle: Any
    pid: int
    deferred_handles: tuple[Any, ...] = ()
    failures: tuple[str, ...] = ()


class _JobAdapter(Protocol):
    """Injectable boundary around the Win32 calls used by the owner."""

    def create_job(self) -> Any: ...

    def configure_kill_on_close(self, job_handle: Any) -> None: ...

    def create_process_in_job(
        self,
        job_handle: Any,
        argv: Sequence[str | os.PathLike[str]],
        env: Mapping[str, str],
    ) -> _SpawnedHandles: ...

    def process_is_in_job(self, process_handle: Any, job_handle: Any) -> bool: ...

    def resume_thread(self, thread_handle: Any) -> None: ...

    def wait_process(self, process_handle: Any, milliseconds: int) -> bool: ...

    def process_exit_code(self, process_handle: Any) -> int: ...

    def terminate_job(self, job_handle: Any, exit_code: int) -> None: ...

    def terminate_process(self, process_handle: Any, exit_code: int) -> None: ...

    def active_processes(self, job_handle: Any) -> int: ...

    def close_handle(self, handle: Any) -> None: ...


def _exact_error(context: str, error: BaseException) -> str:
    return f"{context} raised {type(error).__name__}: {error}"


def _remaining_timeout(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


def _wait_milliseconds(remaining: float | None) -> int:
    if remaining is None:
        return _WAIT_SLICE_MILLISECONDS
    if remaining <= 0.0:
        return 0
    return min(
        _MAX_WAIT_MILLISECONDS,
        max(1, int(math.ceil(remaining * 1000.0))),
        _WAIT_SLICE_MILLISECONDS,
    )


def _wait_milliseconds_before_deadline(remaining: float) -> int:
    """Floor one retained-handle wait so it cannot request deadline overrun."""

    if remaining <= 0.0:
        return 0
    return min(
        _MAX_WAIT_MILLISECONDS,
        int(math.floor(remaining * 1000.0)),
        _WAIT_SLICE_MILLISECONDS,
    )


def _handle_value(handle: Any) -> int:
    value = getattr(handle, "value", handle)
    if value is None:
        return 0
    return int(value)


def _invalid_handle_value() -> int:
    value = ctypes.c_void_p(_INVALID_HANDLE_VALUE).value
    assert value is not None
    return int(value)


class WindowsContainedProcess:
    """Own one exact GUI process and its retained Windows Job Object.

    A successful instance owns both handles until ``close_after_empty`` proves
    that the direct process is signalled and the complete contained tree is
    empty.  Retryable adapter failures are retained verbatim in ``failures``.
    Methods never release ownership merely because a fixed cleanup interval
    elapsed.
    """

    def __init__(
        self,
        *,
        adapter: _JobAdapter,
        job_handle: Any,
        process_handle: Any,
        thread_handle: Any,
        pid: int,
        argv: Sequence[str | os.PathLike[str]],
        deferred_handles: Sequence[Any] = (),
        initial_failures: Sequence[str] = (),
    ) -> None:
        self._adapter = adapter
        self._job_handle: Any | None = job_handle
        self._process_handle: Any | None = process_handle
        self._thread_handle: Any | None = thread_handle
        self._deferred_handles = list(deferred_handles)
        self.pid = int(pid)
        self.args = [os.fsdecode(argument) for argument in argv]
        self.returncode: int | None = None
        self._failures = list(initial_failures)
        self._termination_requested = False
        self._direct_exit_preceded_termination = False
        self._direct_exit_observed_before_termination = False
        self._termination_exit_code: int | None = None
        self._closed = False

    @property
    def failures(self) -> tuple[str, ...]:
        """Exact retained adapter failures, in observation order."""

        return tuple(self._failures)

    @property
    def termination_requested(self) -> bool:
        """Whether ``TerminateJobObject`` succeeded before tree observation."""

        return self._termination_requested

    @property
    def direct_exit_preceded_termination(self) -> bool:
        """Whether direct exit was observed before the first kill attempt.

        When false and ``termination_requested`` is true, the owner issued Job
        Object termination before it observed a direct-child exit.  That is the
        operational distinction needed at the deadline; Windows cannot prove
        physical causality if a natural exit races the kernel termination call.
        """

        return self._direct_exit_preceded_termination

    @property
    def termination_exit_code(self) -> int | None:
        """The code supplied to the successful Job Object termination call."""

        return self._termination_exit_code

    @property
    def closed(self) -> bool:
        return self._closed

    def _record_failure(self, context: str, error: BaseException) -> None:
        try:
            diagnostic = _exact_error(context, error)
            if diagnostic in self._failures:
                return
            if len(self._failures) < _FAILURE_DIAGNOSTIC_LIMIT - 1:
                self._failures.append(diagnostic)
                return
            marker = (
                "additional distinct Windows containment diagnostics omitted "
                f"after {_FAILURE_DIAGNOSTIC_LIMIT - 1} entries"
            )
            if marker not in self._failures:
                self._failures.append(marker)
        except BaseException:
            # Retaining handles and retrying is more important than allocating
            # a secondary diagnostic under severe memory pressure.
            pass

    def _require_process_handle(self) -> Any:
        handle = self._process_handle
        if self._closed or handle is None:
            raise WindowsShutdownJobError("retained Windows process handle is closed")
        return handle

    def _require_job_handle(self) -> Any:
        handle = self._job_handle
        if self._closed or handle is None:
            raise WindowsShutdownJobError(
                "retained Windows Job Object handle is closed"
            )
        return handle

    def _close_thread_handle(self, *, retry: bool = True) -> None:
        while self._thread_handle is not None:
            handle = self._thread_handle
            try:
                self._adapter.close_handle(handle)
            except BaseException as error:
                self._record_failure("Windows initial-thread handle close", error)
                if not retry:
                    return
                time.sleep(_RETRY_INTERVAL_SECONDS)
                continue
            self._thread_handle = None

    def poll(self) -> int | None:
        """Return the exact direct-process status without reopening its PID."""

        if self.returncode is not None:
            return self.returncode
        process_handle = self._require_process_handle()
        while True:
            try:
                signalled = self._adapter.wait_process(process_handle, 0)
            except BaseException as error:
                self._record_failure("Windows direct-process poll", error)
                time.sleep(_RETRY_INTERVAL_SECONDS)
                continue
            if not signalled:
                return None
            try:
                self.returncode = int(self._adapter.process_exit_code(process_handle))
            except BaseException as error:
                self._record_failure("Windows direct-process exit-status read", error)
                time.sleep(_RETRY_INTERVAL_SECONDS)
                continue
            return self.returncode

    def observe_before_deadline(self, hard_deadline: float) -> bool | None:
        """Observe the retained direct handle without crossing an absolute deadline.

        ``True`` means the direct process was observed signalled before
        ``hard_deadline``. ``False`` means it was still live at that observation,
        and ``None`` means the deadline had been reached by the time the adapter
        returned.  The deadline result performs no zero-time follow-up poll,
        status query, diagnostic allocation, or relative-timeout exception.

        A successful pre-deadline observation is retained separately from the
        exit code.  The owner can therefore terminate residual Job members first
        and read the exact direct-process status afterwards, including a natural
        status equal to the Job termination code.
        """

        # The authenticated ARM decoder has already validated this immutable
        # float.  Do not rebase or allocate a replacement in the deadline loop.
        deadline = hard_deadline
        process_handle = self._require_process_handle()
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            return None
        wait_milliseconds = _wait_milliseconds_before_deadline(remaining)
        if time.monotonic() >= deadline:
            return None
        # Do not format or retain adapter failures here.  The supervisor first
        # terminates the Job and only then materialises the exact exception as
        # a diagnostic, even if this call crossed the absolute deadline.
        signalled = self._adapter.wait_process(
            process_handle,
            wait_milliseconds,
        )
        if time.monotonic() >= deadline:
            return None
        if not signalled:
            return False
        self._direct_exit_observed_before_termination = True
        return True

    def wait(self, timeout: float | None = None) -> int:
        """Wait for the retained direct process without extending ``timeout``."""

        if timeout is not None:
            timeout = float(timeout)
            if not math.isfinite(timeout) or timeout < 0.0:
                raise ValueError("timeout must be finite and non-negative")
        if self.returncode is not None:
            return self.returncode
        deadline = None if timeout is None else time.monotonic() + timeout
        process_handle = self._require_process_handle()
        while True:
            remaining = _remaining_timeout(deadline)
            if remaining is not None and remaining <= 0.0:
                try:
                    signalled = self._adapter.wait_process(process_handle, 0)
                except BaseException as error:
                    self._record_failure("Windows direct-process wait", error)
                    signalled = False
                if not signalled:
                    assert timeout is not None
                    raise subprocess.TimeoutExpired(self.args, timeout)
                try:
                    self.returncode = int(
                        self._adapter.process_exit_code(process_handle)
                    )
                except BaseException as error:
                    self._record_failure(
                        "Windows direct-process exit-status read",
                        error,
                    )
                    assert timeout is not None
                    raise subprocess.TimeoutExpired(self.args, timeout) from error
                return self.returncode
            try:
                signalled = self._adapter.wait_process(
                    process_handle,
                    _wait_milliseconds(remaining),
                )
            except BaseException as error:
                self._record_failure("Windows direct-process wait", error)
                after_failure = _remaining_timeout(deadline)
                if after_failure is not None and after_failure <= 0.0:
                    assert timeout is not None
                    raise subprocess.TimeoutExpired(self.args, timeout) from error
                time.sleep(
                    min(
                        _RETRY_INTERVAL_SECONDS,
                        after_failure
                        if after_failure is not None
                        else _RETRY_INTERVAL_SECONDS,
                    )
                )
                continue
            if not signalled:
                continue
            try:
                self.returncode = int(self._adapter.process_exit_code(process_handle))
            except BaseException as error:
                self._record_failure("Windows direct-process exit-status read", error)
                after_failure = _remaining_timeout(deadline)
                if after_failure is not None and after_failure <= 0.0:
                    assert timeout is not None
                    raise subprocess.TimeoutExpired(self.args, timeout) from error
                continue
            return self.returncode

    def terminate_tree(self, exit_code: int = _FORCED_SHUTDOWN_EXIT_CODE) -> None:
        """Terminate the whole retained job, retrying without releasing it.

        The first adapter call is always ``TerminateJobObject``.  In particular,
        no liveness query is performed before the first termination attempt.
        """

        job_handle = self._require_job_handle()
        requested_code = int(exit_code)
        self._direct_exit_preceded_termination = bool(
            self.returncode is not None or self._direct_exit_observed_before_termination
        )
        while True:
            try:
                self._adapter.terminate_job(job_handle, requested_code)
            except BaseException as error:
                self._record_failure("Windows Job Object termination", error)
                # After the first kill attempt it is safe to distinguish an
                # already-empty job from a genuine termination failure.
                try:
                    active = int(self._adapter.active_processes(job_handle)) != 0
                except BaseException as observation_error:
                    self._record_failure(
                        "Windows Job Object post-termination observation",
                        observation_error,
                    )
                else:
                    if not active:
                        return
                time.sleep(_RETRY_INTERVAL_SECONDS)
                continue
            self._termination_requested = True
            self._termination_exit_code = requested_code
            return

    def tree_active(self) -> bool:
        """Report whether any process in the retained nested job tree is active."""

        job_handle = self._require_job_handle()
        while True:
            try:
                return int(self._adapter.active_processes(job_handle)) != 0
            except BaseException as error:
                self._record_failure("Windows Job Object liveness query", error)
                time.sleep(_RETRY_INTERVAL_SECONDS)

    def _tree_active_until(
        self,
        deadline: float | None,
        timeout: float | None,
    ) -> bool:
        job_handle = self._require_job_handle()
        while True:
            try:
                return int(self._adapter.active_processes(job_handle)) != 0
            except BaseException as error:
                self._record_failure("Windows Job Object liveness query", error)
                remaining = _remaining_timeout(deadline)
                if remaining is not None and remaining <= 0.0:
                    assert timeout is not None
                    raise subprocess.TimeoutExpired(self.args, timeout) from error
                time.sleep(
                    min(
                        _RETRY_INTERVAL_SECONDS,
                        remaining if remaining is not None else _RETRY_INTERVAL_SECONDS,
                    )
                )

    def wait_tree_empty(self, timeout: float | None = None) -> None:
        """Wait for complete tree disappearance while retaining job ownership."""

        if timeout is not None:
            timeout = float(timeout)
            if not math.isfinite(timeout) or timeout < 0.0:
                raise ValueError("timeout must be finite and non-negative")
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if not self._tree_active_until(deadline, timeout):
                return
            remaining = _remaining_timeout(deadline)
            if remaining is not None and remaining <= 0.0:
                assert timeout is not None
                raise subprocess.TimeoutExpired(self.args, timeout)
            time.sleep(
                min(
                    _RETRY_INTERVAL_SECONDS,
                    remaining if remaining is not None else _RETRY_INTERVAL_SECONDS,
                )
            )

    def close_after_empty(self) -> None:
        """Release handles only after the direct process and tree are quiescent."""

        if self._closed:
            return
        if self.poll() is None:
            raise WindowsShutdownJobError(
                "refusing to close a live retained Windows direct process"
            )
        if self.tree_active():
            raise WindowsShutdownJobError(
                "refusing to close a retained Windows Job Object with active processes"
            )
        self._close_thread_handle()
        while self._deferred_handles:
            handle = self._deferred_handles[0]
            try:
                self._adapter.close_handle(handle)
            except BaseException as error:
                self._record_failure("Windows inherited-handle close", error)
                time.sleep(_RETRY_INTERVAL_SECONDS)
                continue
            self._deferred_handles.pop(0)
        while self._process_handle is not None:
            handle = self._process_handle
            try:
                self._adapter.close_handle(handle)
            except BaseException as error:
                self._record_failure("Windows direct-process handle close", error)
                time.sleep(_RETRY_INTERVAL_SECONDS)
                continue
            self._process_handle = None
        while self._job_handle is not None:
            handle = self._job_handle
            try:
                self._adapter.close_handle(handle)
            except BaseException as error:
                self._record_failure("Windows Job Object handle close", error)
                time.sleep(_RETRY_INTERVAL_SECONDS)
                continue
            self._job_handle = None
        self._closed = True

    def _terminate_exact_suspended_process(self, exit_code: int) -> None:
        """Abort a process that failed containment verification before resume."""

        process_handle = self._require_process_handle()
        while self.poll() is None:
            try:
                self._adapter.terminate_process(process_handle, int(exit_code))
            except BaseException as error:
                self._record_failure(
                    "Windows suspended direct-process termination",
                    error,
                )
                time.sleep(_RETRY_INTERVAL_SECONDS)
                continue
            break


def _close_empty_job(adapter: _JobAdapter, job_handle: Any) -> str | None:
    """Try once to close a proven-empty job and return an exact failure."""

    try:
        adapter.close_handle(job_handle)
    except BaseException as error:
        # No process has been created/assigned, so returning lets launcher exit
        # close this empty kernel object without risking a live-tree release or
        # extending the absolute startup interval indefinitely.
        return _exact_error("empty Windows Job Object handle close", error)
    return None


def _abort_failed_spawn(
    child: WindowsContainedProcess,
    *,
    membership_verified: bool,
) -> None:
    """Drain a contained/suspended process before propagating spawn failure."""

    if membership_verified:
        child.terminate_tree()
    else:
        # The initial thread has not been resumed, so exact retained-handle
        # termination cannot miss a descendant and does not rely on uncertain
        # Job membership. No PID is opened or targeted.
        child._terminate_exact_suspended_process(_FORCED_SHUTDOWN_EXIT_CODE)
    child.wait()
    child.wait_tree_empty()
    child.close_after_empty()


def spawn_contained(
    argv: Sequence[str | os.PathLike[str]],
    env: Mapping[str, str],
    *,
    adapter: _JobAdapter | None = None,
) -> WindowsContainedProcess:
    """Create a suspended GUI atomically inside one retained Windows job.

    ``adapter`` is an intentional non-Windows regression-test seam.  Production
    callers omit it and receive the lazy ctypes-backed Win32 implementation.
    """

    preserved_argv = list(argv)
    if not preserved_argv:
        raise ValueError("contained Windows process argv must not be empty")
    preserved_environment = dict(env)
    active_adapter = _CtypesWindowsJobAdapter() if adapter is None else adapter

    try:
        job_handle = active_adapter.create_job()
    except BaseException as error:
        raise WindowsShutdownJobError(
            _exact_error("Windows Job Object creation", error)
        ) from error
    try:
        active_adapter.configure_kill_on_close(job_handle)
    except BaseException as error:
        cleanup_failure = _close_empty_job(active_adapter, job_handle)
        diagnostic = _exact_error(
            "Windows Job Object kill-on-close configuration",
            error,
        )
        if cleanup_failure is not None:
            diagnostic = "; ".join((diagnostic, cleanup_failure))
        raise WindowsShutdownJobError(diagnostic) from error
    try:
        spawned = active_adapter.create_process_in_job(
            job_handle,
            preserved_argv,
            preserved_environment,
        )
    except BaseException as error:
        cleanup_failure = _close_empty_job(active_adapter, job_handle)
        diagnostic = _exact_error("contained Windows GUI process creation", error)
        if cleanup_failure is not None:
            diagnostic = "; ".join((diagnostic, cleanup_failure))
        raise WindowsShutdownJobError(diagnostic) from error

    child = WindowsContainedProcess(
        adapter=active_adapter,
        job_handle=job_handle,
        process_handle=spawned.process_handle,
        thread_handle=spawned.thread_handle,
        pid=spawned.pid,
        argv=preserved_argv,
        deferred_handles=spawned.deferred_handles,
        initial_failures=spawned.failures,
    )
    membership_verified = False
    try:
        membership_verified = bool(
            active_adapter.process_is_in_job(
                spawned.process_handle,
                job_handle,
            )
        )
        if not membership_verified:
            raise WindowsShutdownJobError(
                "Windows created the suspended GUI outside its retained Job Object"
            )
        active_adapter.resume_thread(spawned.thread_handle)
        # A failed CloseHandle must not hold startup past its absolute
        # authentication deadline after the GUI has been resumed.  Retain the
        # exact thread handle and retry its close during final tree teardown.
        child._close_thread_handle(retry=False)
    except BaseException as error:
        _abort_failed_spawn(child, membership_verified=membership_verified)
        retained_failures = child.failures
        if isinstance(error, WindowsShutdownJobError):
            diagnostic = str(error)
        else:
            diagnostic = _exact_error("contained Windows GUI process activation", error)
        if retained_failures:
            diagnostic = "; ".join((diagnostic, *retained_failures))
        if isinstance(error, WindowsShutdownJobError):
            raise WindowsShutdownJobError(diagnostic) from error
        raise WindowsShutdownJobError(diagnostic) from error
    return child


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [
        ("StartupInfo", _STARTUPINFOW),
        ("lpAttributeList", ctypes.c_void_p),
    ]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


def _environment_block(env: Mapping[str, str]) -> str:
    entries: list[tuple[str, str]] = []
    for raw_key, raw_value in env.items():
        key = os.fsdecode(raw_key)
        value = os.fsdecode(raw_value)
        if "\0" in key or "\0" in value:
            raise ValueError("Windows environment entries must not contain NUL")
        if not key or "=" in key[1:]:
            raise ValueError(f"invalid Windows environment name {key!r}")
        entries.append((key, value))
    entries.sort(key=lambda item: item[0].upper())
    return "\0".join(f"{key}={value}" for key, value in entries) + "\0\0"


class _CtypesWindowsJobAdapter:
    """Lazy Win32 implementation; construction is rejected off Windows."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows Job Object containment is only available on Windows")
        win_dll = ctypes.WinDLL  # type: ignore[attr-defined]
        self._kernel32 = win_dll("kernel32", use_last_error=True)
        self._bind_functions()

    @staticmethod
    def _last_error() -> OSError:
        get_last_error = ctypes.get_last_error  # type: ignore[attr-defined]
        win_error = ctypes.WinError  # type: ignore[attr-defined]
        return win_error(get_last_error())

    def _bind_functions(self) -> None:
        kernel32 = self._kernel32
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.QueryInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        )
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        kernel32.InitializeProcThreadAttributeList.argtypes = (
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_size_t),
        )
        kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
        kernel32.UpdateProcThreadAttribute.argtypes = (
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )
        kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
        kernel32.DeleteProcThreadAttributeList.argtypes = (ctypes.c_void_p,)
        kernel32.DeleteProcThreadAttributeList.restype = None
        kernel32.CreateProcessW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.LPCWSTR,
            ctypes.c_void_p,
            ctypes.POINTER(_PROCESS_INFORMATION),
        )
        kernel32.CreateProcessW.restype = wintypes.BOOL
        kernel32.IsProcessInJob.argtypes = (
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.BOOL),
        )
        kernel32.IsProcessInJob.restype = wintypes.BOOL
        kernel32.ResumeThread.argtypes = (wintypes.HANDLE,)
        kernel32.ResumeThread.restype = wintypes.DWORD
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.GetExitCodeProcess.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        )
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.GetStdHandle.argtypes = (wintypes.DWORD,)
        kernel32.GetStdHandle.restype = wintypes.HANDLE
        kernel32.GetCurrentProcess.argtypes = ()
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.DuplicateHandle.argtypes = (
            wintypes.HANDLE,
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        )
        kernel32.DuplicateHandle.restype = wintypes.BOOL
        kernel32.GetFileType.argtypes = (wintypes.HANDLE,)
        kernel32.GetFileType.restype = wintypes.DWORD

    def create_job(self) -> Any:
        handle = self._kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise self._last_error()
        return handle

    def configure_kill_on_close(self, job_handle: Any) -> None:
        information = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        information.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        if not self._kernel32.SetInformationJobObject(
            job_handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            raise self._last_error()

    def _duplicate_standard_handle(self, selector: int) -> Any:
        source = self._kernel32.GetStdHandle(selector)
        if not source or _handle_value(source) == _invalid_handle_value():
            return wintypes.HANDLE(_INVALID_HANDLE_VALUE)
        duplicate = wintypes.HANDLE()
        current_process = self._kernel32.GetCurrentProcess()
        if not self._kernel32.DuplicateHandle(
            current_process,
            source,
            current_process,
            ctypes.byref(duplicate),
            0,
            True,
            _DUPLICATE_SAME_ACCESS,
        ):
            raise self._last_error()
        return duplicate

    def _close_duplicated_standard_handles(
        self,
        handles: Sequence[Any],
    ) -> tuple[tuple[Any, ...], tuple[str, ...]]:
        deferred: list[Any] = []
        failures: list[str] = []
        for handle in handles:
            if _handle_value(handle) == _invalid_handle_value():
                continue
            try:
                self.close_handle(handle)
            except BaseException as error:
                failures.append(
                    _exact_error("Windows duplicated standard-handle close", error)
                )
                deferred.append(handle)
        return tuple(deferred), tuple(failures)

    def _attribute_list(
        self,
        job_handle: Any,
        inherited_handles: Sequence[Any],
    ) -> tuple[Any, Any, Any | None, Any]:
        attribute_count = 1 + bool(inherited_handles)
        required_size = ctypes.c_size_t()
        initialize = self._kernel32.InitializeProcThreadAttributeList
        ctypes.set_last_error(0)  # type: ignore[attr-defined]
        initial_result = initialize(
            None,
            int(attribute_count),
            0,
            ctypes.byref(required_size),
        )
        get_last_error = ctypes.get_last_error  # type: ignore[attr-defined]
        if (
            initial_result
            or get_last_error() != _ERROR_INSUFFICIENT_BUFFER
            or required_size.value == 0
        ):
            raise self._last_error()
        buffer = ctypes.create_string_buffer(required_size.value)
        attribute_list = ctypes.cast(buffer, ctypes.c_void_p)
        if not initialize(
            attribute_list,
            int(attribute_count),
            0,
            ctypes.byref(required_size),
        ):
            raise self._last_error()

        job_array = (wintypes.HANDLE * 1)(job_handle)
        if not self._kernel32.UpdateProcThreadAttribute(
            attribute_list,
            0,
            _PROC_THREAD_ATTRIBUTE_JOB_LIST,
            ctypes.cast(job_array, ctypes.c_void_p),
            ctypes.sizeof(job_array),
            None,
            None,
        ):
            error = self._last_error()
            self._kernel32.DeleteProcThreadAttributeList(attribute_list)
            raise error

        handle_array = None
        if inherited_handles:
            handle_array = (wintypes.HANDLE * len(inherited_handles))(
                *inherited_handles
            )
            if not self._kernel32.UpdateProcThreadAttribute(
                attribute_list,
                0,
                _PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                ctypes.cast(handle_array, ctypes.c_void_p),
                ctypes.sizeof(handle_array),
                None,
                None,
            ):
                error = self._last_error()
                self._kernel32.DeleteProcThreadAttributeList(attribute_list)
                raise error
        return buffer, attribute_list, handle_array, job_array

    def create_process_in_job(
        self,
        job_handle: Any,
        argv: Sequence[str | os.PathLike[str]],
        env: Mapping[str, str],
    ) -> _SpawnedHandles:
        decoded_argv = [os.fsdecode(argument) for argument in argv]
        command_line = ctypes.create_unicode_buffer(
            subprocess.list2cmdline(decoded_argv)
        )
        environment_text = _environment_block(env)
        environment = ctypes.create_unicode_buffer(
            environment_text,
            len(environment_text),
        )

        standard_handles: list[Any] = []
        try:
            for selector in (
                _STD_INPUT_HANDLE,
                _STD_OUTPUT_HANDLE,
                _STD_ERROR_HANDLE,
            ):
                standard_handles.append(self._duplicate_standard_handle(selector))
        except BaseException as error:
            _deferred, cleanup_failures = self._close_duplicated_standard_handles(
                standard_handles
            )
            diagnostic = _exact_error("Windows standard-handle duplication", error)
            if cleanup_failures:
                diagnostic = "; ".join((diagnostic, *cleanup_failures))
            raise WindowsShutdownJobError(diagnostic) from error
        inheritable_standard_handles = [
            handle
            for handle in standard_handles
            if _handle_value(handle) != _invalid_handle_value()
            and not (
                _handle_value(handle) & 0x3 == 0x3
                and self._kernel32.GetFileType(handle) == _FILE_TYPE_CHAR
            )
        ]
        # The HANDLE_LIST route is valid only when all three STARTUPINFO
        # standard handles can be listed.  Windows console handles with their
        # low bits set are explicitly forbidden from HANDLE_LIST; an absent
        # standard handle is invalid too.  In either case, use CreateProcess's
        # ordinary inherited-standard-stream behavior without
        # STARTF_USESTDHANDLES, matching subprocess.Popen(..., stdio=None).
        explicit_standard_handles = len(inheritable_standard_handles) == 3
        inherited_handles = (
            inheritable_standard_handles if explicit_standard_handles else []
        )
        attribute_list: Any | None = None
        spawned: _SpawnedHandles | None = None
        creation_error: BaseException | None = None
        try:
            _buffer, attribute_list, _handle_array, _job_array = self._attribute_list(
                job_handle, inherited_handles
            )
            startup = _STARTUPINFOEXW()
            startup.StartupInfo.cb = ctypes.sizeof(_STARTUPINFOEXW)
            if explicit_standard_handles:
                startup.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
                (
                    startup.StartupInfo.hStdInput,
                    startup.StartupInfo.hStdOutput,
                    startup.StartupInfo.hStdError,
                ) = standard_handles
            startup.lpAttributeList = attribute_list
            process_information = _PROCESS_INFORMATION()
            if not self._kernel32.CreateProcessW(
                None,
                ctypes.cast(command_line, wintypes.LPWSTR),
                None,
                None,
                bool(inherited_handles),
                _WINDOWS_CONTAINED_CREATION_FLAGS,
                ctypes.cast(environment, ctypes.c_void_p),
                None,
                ctypes.byref(startup),
                ctypes.byref(process_information),
            ):
                raise self._last_error()
            spawned = _SpawnedHandles(
                process_handle=process_information.hProcess,
                thread_handle=process_information.hThread,
                pid=int(process_information.dwProcessId),
            )
        except BaseException as error:
            creation_error = error
        finally:
            if attribute_list is not None:
                self._kernel32.DeleteProcThreadAttributeList(attribute_list)
            deferred_handles, cleanup_failures = (
                self._close_duplicated_standard_handles(standard_handles)
            )
        if creation_error is not None:
            diagnostic = _exact_error(
                "Windows contained-process creation", creation_error
            )
            if cleanup_failures:
                diagnostic = "; ".join((diagnostic, *cleanup_failures))
            raise WindowsShutdownJobError(diagnostic) from creation_error
        assert spawned is not None
        return _SpawnedHandles(
            process_handle=spawned.process_handle,
            thread_handle=spawned.thread_handle,
            pid=spawned.pid,
            deferred_handles=deferred_handles,
            failures=cleanup_failures,
        )

    def process_is_in_job(self, process_handle: Any, job_handle: Any) -> bool:
        result = wintypes.BOOL()
        if not self._kernel32.IsProcessInJob(
            process_handle,
            job_handle,
            ctypes.byref(result),
        ):
            raise self._last_error()
        return bool(result.value)

    def resume_thread(self, thread_handle: Any) -> None:
        if self._kernel32.ResumeThread(thread_handle) == _WAIT_FAILED:
            raise self._last_error()

    def wait_process(self, process_handle: Any, milliseconds: int) -> bool:
        result = int(
            self._kernel32.WaitForSingleObject(process_handle, int(milliseconds))
        )
        if result == _WAIT_OBJECT_0:
            return True
        if result == _WAIT_TIMEOUT:
            return False
        if result == _WAIT_FAILED:
            raise self._last_error()
        raise OSError(f"unexpected WaitForSingleObject result {result}")

    def process_exit_code(self, process_handle: Any) -> int:
        result = wintypes.DWORD()
        if not self._kernel32.GetExitCodeProcess(
            process_handle,
            ctypes.byref(result),
        ):
            raise self._last_error()
        return int(result.value)

    def terminate_job(self, job_handle: Any, exit_code: int) -> None:
        if not self._kernel32.TerminateJobObject(job_handle, int(exit_code)):
            raise self._last_error()

    def terminate_process(self, process_handle: Any, exit_code: int) -> None:
        if not self._kernel32.TerminateProcess(process_handle, int(exit_code)):
            raise self._last_error()

    def active_processes(self, job_handle: Any) -> int:
        information = _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        if not self._kernel32.QueryInformationJobObject(
            job_handle,
            _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS,
            ctypes.byref(information),
            ctypes.sizeof(information),
            None,
        ):
            raise self._last_error()
        return int(information.ActiveProcesses)

    def close_handle(self, handle: Any) -> None:
        if not self._kernel32.CloseHandle(handle):
            raise self._last_error()
