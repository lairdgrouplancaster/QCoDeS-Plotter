"""Ownership and deadline contracts of the GUI shutdown coordinator."""

from types import SimpleNamespace

import pytest
from PyQt6 import QtWidgets as qtw

from qplot.windows._shutdown_coordinator import ShutdownCoordinator
from qplot.windows._shutdown_resources import ShutdownResources
from qplot.windows.main import MainWindow


class Pool:
    def __init__(self, active=1):
        self.active = active
        self.clears = 0

    def activeThreadCount(self):
        return self.active

    def clear(self):
        self.clears += 1


class Timer:
    def __init__(self):
        self.running = False

    def start(self):
        self.running = True

    def stop(self):
        self.running = False


class CloseEvent:
    accepted = False
    ignored = False

    def accept(self):
        self.accepted = True

    def ignore(self):
        self.ignored = True


class Reader:
    def __init__(self):
        self.probes = 0
        self.escalations = 0

    def liveness(self):
        self.probes += 1
        return SimpleNamespace(closed=False, resource_cleanup_pending=True)

    def escalate_cleanup_async(self):
        self.escalations += 1


@pytest.fixture
def shutdown(monkeypatch):
    clock = [100.0]
    persisted = []
    closed = []
    window = SimpleNamespace(
        config=SimpleNamespace(get=lambda _key: False),
        _database_load_generation=7,
        _shutdown_timer=Timer(),
        startupDatabaseTimer=Timer(),
        monitor=Timer(),
        hide=lambda: None,
        close_plot_windows=lambda **_kwargs: None,
        close_database=lambda **_kwargs: None,
    )
    window._shutdown = ShutdownCoordinator(
        window,
        timeout_seconds=15.0,
        diagnostic_grace_seconds=0.25,
        persist_diagnostics=lambda **kwargs: persisted.append(kwargs),
        clock=lambda: clock[0],
    )
    monkeypatch.setattr(qtw.QApplication, "closeAllWindows", lambda: closed.append(True))
    return window, clock, persisted, closed


def test_registered_pool_keeps_close_pending_and_is_escalated_once(shutdown):
    window, clock, persisted, closed = shutdown
    pool = Pool()
    coordinator = window._shutdown
    coordinator.resources.register_pool("additionalPool", pool)
    event = CloseEvent()
    MainWindow.closeEvent(window, event)

    assert event.ignored
    assert coordinator.waiting
    assert window._shutdown_timer.running
    assert coordinator.state.diagnostic_deadline == 114.75
    assert coordinator.state.hard_deadline == 115.0
    assert coordinator.state.diagnostics == ("pool additionalPool: active_threads=1",)

    coordinator.poll()
    assert pool.clears == 1
    clock[0] = 102.0
    repeated_close = CloseEvent()
    MainWindow.closeEvent(window, repeated_close)
    coordinator.poll()
    assert repeated_close.ignored
    assert coordinator.state.hard_deadline == 115.0
    assert coordinator.state.diagnostic_deadline == 114.75
    assert pool.clears == 1

    pool.active = 0
    coordinator.poll()
    assert coordinator.ready
    assert not window._shutdown_timer.running
    assert not coordinator.state.deadline_exhausted
    assert persisted == []
    assert closed == [True]


def test_escalation_error_survives_later_probes_at_original_deadline(shutdown):
    window, clock, persisted, closed = shutdown

    class FailingReader(Reader):
        def escalate_cleanup_async(self):
            super().escalate_cleanup_async()
            raise RuntimeError("reader cleanup failed")

    reader = FailingReader()
    window._retired_trusted_read_services = {reader}
    coordinator = window._shutdown
    MainWindow.closeEvent(window, CloseEvent())
    coordinator.poll()
    assert reader.escalations == 1
    assert coordinator.waiting

    clock[0] = 114.75
    coordinator.poll()
    assert coordinator.ready
    assert coordinator.state.deadline_exhausted
    assert coordinator.state.hard_deadline == 115.0
    assert reader.escalations == 1
    assert closed == [True]
    assert len(persisted) == 1
    assert persisted[0]["started_at"] == 100.0
    assert persisted[0]["total_timeout"] == 15.0
    assert persisted[0]["diagnostics"] == coordinator.state.final_diagnostics
    diagnostics = "\n".join(coordinator.state.final_diagnostics)
    assert "RuntimeError: reader cleanup failed" in diagnostics
    assert "resource_cleanup_pending=True" in diagnostics


def test_cancellation_resolves_current_workers_in_registration_order():
    calls = []
    window = SimpleNamespace(load=SimpleNamespace(cancel=lambda: calls.append("old")))
    resources = ShutdownResources(window)
    resources.watch_worker("load")
    resources.watch_worker("info", cancel=lambda: calls.append("info-hook"))
    resources.watch_worker("generation")
    window.load = SimpleNamespace(cancel=lambda: calls.append("current"))
    window.generation = SimpleNamespace(cancel=lambda: calls.append("generation"))

    resources.cancel_database_workers()

    assert calls == ["current", "info-hook", "generation"]


def test_reader_membership_is_live_and_each_service_is_probed_once():
    first, second, later = Reader(), Reader(), Reader()
    window = SimpleNamespace(
        _trusted_read_service=first,
        _pending_trusted_read_services={1: first, 2: second},
        _retired_trusted_read_services={second},
    )
    resources = ShutdownResources(window)
    assert resources.inspect().active
    assert (first.probes, second.probes, later.probes) == (1, 1, 0)

    window._trusted_read_service = None
    window._pending_trusted_read_services = {3: later}
    window._retired_trusted_read_services = {first, second}
    assert resources.inspect().active
    assert (first.probes, second.probes, later.probes) == (2, 2, 1)

    window._retired_trusted_read_services.clear()
    resources.escalate_cleanup()
    assert (first.escalations, second.escalations, later.escalations) == (0, 0, 1)


@pytest.mark.parametrize("exception", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_failed_pool_probe_never_reports_quiescence(exception):
    class UnavailablePool(Pool):
        def activeThreadCount(self):
            raise exception("probe unavailable")

    resources = ShutdownResources(SimpleNamespace())
    resources.register_pool("unavailablePool", UnavailablePool())
    status = resources.inspect()
    assert status.active
    assert status.diagnostics == (
        f"pool unavailablePool: liveness raised {exception.__name__}: probe unavailable",
    )
