"""Construct the real shutdown coordinator around focused window doubles."""

from time import monotonic

from qplot.windows import main as main_window
from qplot.windows._shutdown_coordinator import (
    ShutdownCoordinator,
    ShutdownPhase,
    ShutdownState,
)


def attach_shutdown(window, *, waiting=False):
    """Register only the pools/workers supplied by this test's window double."""
    if not hasattr(window, "_shutdown"):
        window._shutdown = ShutdownCoordinator(
            window,
            timeout_seconds=main_window._APPLICATION_SHUTDOWN_TIMEOUT_SECONDS,
            diagnostic_grace_seconds=main_window._APPLICATION_SHUTDOWN_DIAGNOSTIC_GRACE_SECONDS,
            persist_diagnostics=main_window._persist_shutdown_diagnostics,
        )
        resources = window._shutdown.resources
        for name in dir(window):
            if name == "threadPool" or name.endswith("ThreadPool"):
                resources.register_pool(name, getattr(window, name))
            if name.startswith(("_database_", "_test_database_")) and name.endswith("_worker"):
                resources.watch_worker(name)
    if waiting:
        now = monotonic()
        window._shutdown.state = ShutdownState(
            phase=ShutdownPhase.WAITING,
            started_at=now,
            diagnostic_deadline=now + 1.0,
            hard_deadline=now + 1.25,
        )
    return window._shutdown
