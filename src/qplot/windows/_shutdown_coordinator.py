"""GUI shutdown policy, separate from the process-level termination backstop."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from time import monotonic
from typing import TYPE_CHECKING, Protocol

from PyQt6 import QtGui
from PyQt6 import QtWidgets as qtw

from ._database_detail_state import detail_channel
from ._shutdown_resources import ShutdownResources

if TYPE_CHECKING:
    from .main import MainWindow


class DiagnosticPersistence(Protocol):
    def __call__(
        self,
        *,
        started_at: float,
        total_timeout: float,
        diagnostics: tuple[str, ...],
    ) -> None: ...


class ShutdownPhase(Enum):
    RUNNING = auto()
    WAITING = auto()
    READY = auto()


@dataclass
class ShutdownState:
    phase: ShutdownPhase = ShutdownPhase.RUNNING
    started_at: float | None = None
    diagnostic_deadline: float | None = None
    hard_deadline: float | None = None
    cleanup_escalated: bool = False
    escalation_diagnostics: tuple[str, ...] = ()
    liveness_diagnostics: tuple[str, ...] = ()
    final_diagnostics: tuple[str, ...] = ()
    deadline_exhausted: bool = False

    @property
    def diagnostics(self) -> tuple[str, ...]:
        return (*self.escalation_diagnostics, *self.liveness_diagnostics)


class ShutdownCoordinator:
    """Cancel in the established order and poll against one fixed deadline."""

    def __init__(
        self,
        window: MainWindow,
        *,
        timeout_seconds: float,
        diagnostic_grace_seconds: float,
        persist_diagnostics: DiagnosticPersistence,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.window = window
        self.resources = ShutdownResources(window)
        self.state = ShutdownState()
        self.timeout_seconds = timeout_seconds
        self.diagnostic_grace_seconds = diagnostic_grace_seconds
        self.persist_diagnostics = persist_diagnostics
        self.clock = clock

    @property
    def waiting(self) -> bool:
        return self.state.phase is ShutdownPhase.WAITING

    @property
    def ready(self) -> bool:
        return self.state.phase is ShutdownPhase.READY

    def begin(self, event: QtGui.QCloseEvent) -> None:
        """Start only after the main window has confirmed the user's request."""
        window = self.window
        self._arm_deadline()
        self._cancel_background_work()
        self._invalidate_database_publications()
        stop_refresh_timer = getattr(window, "_stop_automatic_refresh_timer", None)
        if callable(stop_refresh_timer):
            stop_refresh_timer()
        else:
            window.monitor.stop()
        window.close_plot_windows(confirm=False, status=False)
        window.close_database(status=False)

        if self.background_work_active():
            self.state.phase = ShutdownPhase.WAITING
            event.ignore()
            window.hide()
            window._shutdown_timer.start()
            return

        self.state.phase = ShutdownPhase.READY
        event.accept()
        qtw.QApplication.closeAllWindows()

    def _arm_deadline(self) -> None:
        # Diagnostic persistence gets a grace inside the total bound, never
        # after it. The independent process fail-safe retains its own deadline.
        started_at = self.clock()
        timeout = max(0.0, self.timeout_seconds)
        grace = min(self.diagnostic_grace_seconds, timeout * 0.25)
        hard_deadline = started_at + timeout
        self.state = ShutdownState(
            started_at=started_at,
            diagnostic_deadline=hard_deadline - grace,
            hard_deadline=hard_deadline,
        )
        process_fail_safe = getattr(self.window, "_shutdown_process_fail_safe", None)
        if process_fail_safe is not None:
            diagnostic = process_fail_safe.arm(
                started_at=started_at,
                diagnostic_deadline=self.state.diagnostic_deadline,
                hard_deadline=hard_deadline,
            )
            if diagnostic:
                self.state.escalation_diagnostics = (diagnostic,)
                self._publish_diagnostics()

    def _cancel_background_work(self) -> None:
        window = self.window
        # Fence queued refresh timeouts before asking any worker to cancel.
        window._automatic_refresh_shutdown = True
        stop_refresh_timer = getattr(window, "_stop_automatic_refresh_timer", None)
        if callable(stop_refresh_timer):
            stop_refresh_timer()
        derived_bridge = getattr(window, "_trusted_derived_bridge", None)
        if derived_bridge is not None:
            derived_bridge.shutdown()
        preview = getattr(getattr(window, "infoBox", None), "preview", None)
        if preview is not None:
            preview.shutdown()
        window.startupDatabaseTimer.stop()
        self.resources.cancel_database_workers()
        self.resources.cancel_plot_work()

    def _invalidate_database_publications(self) -> None:
        window = self.window
        window._database_load_generation += 1
        window._database_load_active = False
        window._database_load_state = None
        window._database_load_worker = None
        detail_channel(window).invalidate()
        detail_channel(window, True).invalidate()
        window._database_refresh_generation = (
            getattr(window, "_database_refresh_generation", 0) + 1
        )
        window._database_refresh_active = False
        window._database_refresh_pending = False
        window._database_refresh_worker = None
        window._database_refresh_identity = None
        window._database_refresh_instance = None
        window._database_selected_run_generation = (
            getattr(window, "_database_selected_run_generation", 0) + 1
        )
        window._restoring_selected_run_publication = False
        window._selected_run_restore_pending = False
        window._test_database_generation_active = False
        window._test_database_generation_worker = None
        window._test_database_replacement_state = None
        window._database_view_released_for_generation = False

    def _publish_diagnostics(self) -> None:
        process_fail_safe = getattr(self.window, "_shutdown_process_fail_safe", None)
        if process_fail_safe is not None:
            process_fail_safe.update_diagnostics(self.state.diagnostics)

    def background_work_active(self) -> bool:
        status = self.resources.inspect()
        self.state.liveness_diagnostics = status.diagnostics
        self._publish_diagnostics()
        return status.active

    def _escalate_cleanup(self) -> None:
        if self.state.cleanup_escalated:
            return
        self.state.cleanup_escalated = True
        diagnostics = self.resources.escalate_cleanup()
        if diagnostics:
            self.state.escalation_diagnostics += diagnostics
            self._publish_diagnostics()

    def poll(self) -> None:
        if not self.waiting:
            return
        active = self.background_work_active()
        if active and not self.state.cleanup_escalated:
            self._escalate_cleanup()
            active = self.background_work_active()
        deadline = self.state.diagnostic_deadline
        assert deadline is not None, "Shutdown must be armed before polling"
        if active and self.clock() < deadline:
            return
        self._complete(deadline_exhausted=active)

    def _complete(self, *, deadline_exhausted: bool) -> None:
        """Stop GUI polling; the process entry point still verifies quiescence."""
        window = self.window
        window._shutdown_timer.stop()
        reaper_timer = getattr(window, "_retired_service_reaper_timer", None)
        if reaper_timer is not None:
            reaper_timer.stop()
        if deadline_exhausted:
            diagnostics = self.state.diagnostics or (
                "background work remained active without diagnostics",
            )
            self.state.final_diagnostics = diagnostics
            process_fail_safe = getattr(window, "_shutdown_process_fail_safe", None)
            if process_fail_safe is not None:
                process_fail_safe.update_diagnostics(diagnostics)
                process_fail_safe.persist_async()
            else:
                started_at = self.state.started_at
                assert started_at is not None, (
                    "Shutdown must be armed before completion"
                )
                self.persist_diagnostics(
                    started_at=started_at,
                    total_timeout=self.timeout_seconds,
                    diagnostics=diagnostics,
                )
        self.state.deadline_exhausted = deadline_exhausted
        self.state.phase = ShutdownPhase.READY
        qtw.QApplication.closeAllWindows()
        application = qtw.QApplication.instance()
        if application is not None and isinstance(window, qtw.QWidget):
            application.quit()
