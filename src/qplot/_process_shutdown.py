"""Process shutdown deadlines and diagnostics, independent of Qt windows.

The GUI composes this fail-safe with its resource coordinator. The fail-safe
owns only process supervision and daemon watchdogs; the coordinator owns Qt
resource retirement and decides when to arm or disarm the local backstop.
"""

import os
import sys
import threading
from time import monotonic

from qplot.diagnostics import get_logger, log_bounded_shutdown

FORCED_SHUTDOWN_EXIT_CODE = 70


def _flush_shutdown_diagnostics():
    """Flush qPlot's persistent diagnostics and process text streams."""

    for handler in tuple(get_logger().handlers):
        try:
            handler.flush()
            stream = getattr(handler, "stream", None)
            if stream is not None:
                os.fsync(stream.fileno())
        except BaseException:
            # Diagnostic flushing must not prevent the armed process-shutdown fallback
            # from completing.
            pass
    for stream in (sys.stderr, sys.stdout):
        try:
            stream.flush()
        except BaseException:
            # Unavailable diagnostic streams must not prevent the armed process-shutdown
            # fallback from completing.
            pass


def persist_shutdown_diagnostics(*, started_at, total_timeout, diagnostics):
    """Persist an exact bounded-shutdown snapshot before a hard process exit."""

    elapsed = max(0.0, monotonic() - started_at)
    if not diagnostics:
        diagnostics = ("background work remained active without diagnostics",)
    details = f"elapsed={elapsed:.3f}s\n" + "\n".join(diagnostics)
    try:
        log_bounded_shutdown(
            "Bounded Application Shutdown: "
            f"the {total_timeout:g}-second monotonic shutdown deadline was "
            "exhausted; qPlot will terminate at the process boundary if "
            "Qt-owned work still cannot be destroyed.",
            details,
            logger_name=__name__,
        )
    finally:
        _flush_shutdown_diagnostics()


class ProcessShutdownFailSafe:
    """Pair a direct-parent supervisor with a deadline-only local backstop.

    The caller supplies diagnostic persistence so file I/O remains independent
    of deadline enforcement. No Qt object is retained or accessed here.
    """

    def __init__(
        self,
        supervisor_client=None,
        *,
        persist_diagnostics,
        startup_diagnostic=None,
        force_exit=os._exit,
    ):
        self._persist_diagnostics = persist_diagnostics
        self._supervisor_client = supervisor_client
        self._startup_diagnostic = (
            None if startup_diagnostic is None else str(startup_diagnostic)
        )
        self._force_exit = force_exit
        self._lock = threading.Lock()
        self._persistence_io_lock = threading.Lock()
        self._cancelled = threading.Event()
        self._fallback_diagnostic_started = threading.Event()
        self._fallback_diagnostic_completed = threading.Event()
        self._armed = False
        self._started_at = 0.0
        self._diagnostic_deadline = 0.0
        self._hard_deadline = 0.0
        self._total_timeout = 0.0
        self._diagnostics: tuple[str, ...] = ()
        self._supervisor_diagnostics: tuple[str, ...] = ()
        self._diagnostic_version = 0
        self._persisted_version = -1
        self._supervisor_acknowledged = False
        self._fallback_active = False
        self._persistence_active = False

    @property
    def armed(self):
        with self._lock:
            return self._armed

    def arm(self, *, started_at, diagnostic_deadline, hard_deadline):
        """Arm once after shutdown confirmation; later calls never extend it."""

        with self._lock:
            if self._armed:
                return None
            self._armed = True
            self._started_at = started_at
            self._diagnostic_deadline = diagnostic_deadline
            self._hard_deadline = hard_deadline
            self._total_timeout = max(0.0, hard_deadline - started_at)
            self._diagnostics = ()
            self._supervisor_diagnostics = ()
            self._diagnostic_version = 0
            self._persisted_version = -1
            self._supervisor_acknowledged = False
            self._fallback_active = False
            self._persistence_active = False
            self._cancelled.clear()
            self._fallback_diagnostic_started.clear()
            self._fallback_diagnostic_completed.clear()

        if self._startup_diagnostic is not None:
            self._append_supervisor_diagnostic(self._startup_diagnostic)
        client = self._supervisor_client
        if client is None:
            self._activate_fallback()
            diagnostic = self._startup_diagnostic
            if diagnostic is None:
                diagnostic = (
                    "process shutdown supervisor is unavailable; "
                    "only the in-process deadline fallback is armed"
                )
                self._append_supervisor_diagnostic(diagnostic)
            return diagnostic

        try:
            arm_diagnostic = client.arm(hard_deadline)
        except BaseException as error:
            arm_diagnostic = (
                "process shutdown supervisor ARM raised "
                f"{type(error).__name__}: {error}"
            )
        # The direct parent has either installed its immutable deadline or
        # returned an exact failure by this point.  Install the independent
        # GUI-local backstop before doing any diagnostic persistence.
        self._activate_fallback()
        if arm_diagnostic:
            exact = str(arm_diagnostic)
            self._append_supervisor_diagnostic(exact)
            return exact
        with self._lock:
            self._supervisor_acknowledged = True
        return self._latest_supervisor_diagnostic()

    def update_diagnostics(self, diagnostics):
        """Publish an in-memory liveness snapshot without doing diagnostic I/O."""

        exact = tuple(str(item) for item in diagnostics)
        with self._lock:
            if exact == self._diagnostics:
                return
            self._diagnostics = exact
            self._diagnostic_version += 1

    def persist_now(self):
        """Persist the newest exact snapshot, once per diagnostic version."""

        with self._persistence_io_lock:
            with self._lock:
                if not self._armed:
                    return
                version = self._diagnostic_version
                if version <= self._persisted_version:
                    return
                started_at = self._started_at
                total_timeout = self._total_timeout
                diagnostics = self._effective_diagnostics_locked()
            self._persist_diagnostics(
                started_at=started_at,
                total_timeout=total_timeout,
                diagnostics=diagnostics,
            )
            with self._lock:
                self._persisted_version = max(self._persisted_version, version)

    def persist_async(self):
        """Persist on a daemon thread so diagnostic I/O cannot delay exit."""

        with self._lock:
            if not self._armed or self._persistence_active:
                return
            self._persistence_active = True
        try:
            threading.Thread(
                target=self._persist_async_worker,
                name="qplot-shutdown-diagnostic-persistence",
                daemon=True,
            ).start()
        except BaseException as error:
            with self._lock:
                self._persistence_active = False
            self._append_supervisor_diagnostic(
                "process shutdown diagnostic thread setup raised "
                f"{type(error).__name__}: {error}"
            )

    def _persist_async_worker(self):
        try:
            while True:
                self.persist_now()
                with self._lock:
                    if (
                        not self._armed
                        or self._persisted_version >= self._diagnostic_version
                    ):
                        self._persistence_active = False
                        return
        finally:
            with self._lock:
                self._persistence_active = False

    def disarm(self):
        """Cancel only the GUI-local fallback after quiescent Qt teardown."""

        # There is deliberately no supervisor DISARM message.  Once ARM was
        # accepted, the direct parent remains armed until it reaps this process.
        with self._lock:
            self._armed = False
        self._cancelled.set()

    def wait_for_forced_exit(self):
        """Reach the process boundary before any possibly blocking destructor."""

        with self._lock:
            hard_deadline = self._hard_deadline
            armed = self._armed
        if not armed:
            return
        if self._cancelled.wait(max(0.0, hard_deadline - monotonic())):
            return
        self._force_exit(FORCED_SHUTDOWN_EXIT_CODE)

    def watchdog_operational(self):
        """Return whether the direct parent acknowledged immutable ARM."""

        with self._lock:
            return self._armed and self._supervisor_acknowledged

    def _fallback_diagnostic_watchdog(self):
        self._fallback_diagnostic_started.set()
        if self._cancelled.wait(max(0.0, self._diagnostic_deadline - monotonic())):
            return
        # This watchdog already runs away from the GUI thread.  Persist here
        # instead of scheduling a second daemon so the hard-deadline watchdog
        # cannot overtake diagnostic publication merely because that extra
        # thread has not yet been scheduled.
        try:
            self.persist_now()
        finally:
            self._fallback_diagnostic_completed.set()

    def _fallback_termination_watchdog(self):
        # Prefer a completed diagnostic snapshot before termination without
        # ever allowing slow or blocked diagnostic I/O to extend the immutable
        # hard deadline. This also prevents the two fallback threads racing at
        # the deadline after ordinary, fast persistence has already started.
        self._fallback_diagnostic_completed.wait(
            max(0.0, self._hard_deadline - monotonic())
        )
        if self._cancelled.wait(max(0.0, self._hard_deadline - monotonic())):
            return
        self._force_exit(FORCED_SHUTDOWN_EXIT_CODE)

    def _activate_fallback(self):
        with self._lock:
            if self._fallback_active or not self._armed:
                return
            self._fallback_active = True
        for name, target in (
            (
                "qplot-shutdown-diagnostic-fallback",
                self._fallback_diagnostic_watchdog,
            ),
            (
                "qplot-shutdown-process-fallback",
                self._fallback_termination_watchdog,
            ),
        ):
            try:
                threading.Thread(target=target, name=name, daemon=True).start()
                if target == self._fallback_diagnostic_watchdog:
                    readiness_deadline = min(
                        self._diagnostic_deadline,
                        self._hard_deadline,
                    )
                    self._fallback_diagnostic_started.wait(
                        max(0.0, readiness_deadline - monotonic())
                    )
            except BaseException as error:
                self._append_supervisor_diagnostic(
                    "process shutdown local fallback setup raised "
                    f"{type(error).__name__}: {error}"
                )

    def _append_supervisor_diagnostic(self, diagnostic):
        exact = str(diagnostic)
        with self._lock:
            if exact in self._supervisor_diagnostics:
                return
            self._supervisor_diagnostics = (*self._supervisor_diagnostics, exact)
            self._diagnostic_version += 1

    def _latest_supervisor_diagnostic(self):
        with self._lock:
            if not self._supervisor_diagnostics:
                return None
            return self._supervisor_diagnostics[-1]

    def _effective_diagnostics_locked(self):
        return tuple(dict.fromkeys((*self._supervisor_diagnostics, *self._diagnostics)))
