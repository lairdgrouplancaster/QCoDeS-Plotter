"""Process fail-safe regressions without Qt window construction."""

from threading import Event as ThreadEvent
from time import monotonic

from qplot._process_shutdown import (
    FORCED_SHUTDOWN_EXIT_CODE,
    ProcessShutdownFailSafe,
)


def test_unavailable_supervisor_uses_original_deadline_local_fallback() -> None:
    forced = ThreadEvent()
    persistence_recorded = ThreadEvent()
    forced_codes = []
    persisted = []

    def record_persistence(**kwargs):
        persisted.append(kwargs)
        persistence_recorded.set()

    process_fail_safe = ProcessShutdownFailSafe(
        persist_diagnostics=record_persistence,
        startup_diagnostic="exact shutdown launcher startup failure",
        force_exit=lambda code: (forced_codes.append(code), forced.set()),
    )
    started_at = monotonic()

    startup_diagnostic = process_fail_safe.arm(
        started_at=started_at,
        diagnostic_deadline=started_at + 0.5,
        hard_deadline=started_at + 1.0,
    )
    process_fail_safe.update_diagnostics(("final resource_cleanup_pending=True",))

    assert startup_diagnostic == "exact shutdown launcher startup failure"
    assert not forced.wait(0.1)
    assert forced.wait(1.5)
    assert forced_codes == [FORCED_SHUTDOWN_EXIT_CODE]
    assert persistence_recorded.wait(1.5)
    assert "exact shutdown launcher startup failure" in persisted[-1]["diagnostics"]
    assert "final resource_cleanup_pending=True" in persisted[-1]["diagnostics"]
    process_fail_safe.disarm()


def test_supervisor_arm_failure_is_exact_and_keeps_local_fallback() -> None:
    forced = ThreadEvent()
    persistence_recorded = ThreadEvent()
    persisted = []

    def record_persistence(**kwargs):
        persisted.append(kwargs)
        persistence_recorded.set()

    class FailingClient:
        def arm(self, _hard_deadline):
            raise OSError("exact ARM transport failure")

    process_fail_safe = ProcessShutdownFailSafe(
        FailingClient(),
        persist_diagnostics=record_persistence,
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
    assert persistence_recorded.wait(0.3)
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

    process_fail_safe = ProcessShutdownFailSafe(
        AcknowledgingClient(),
        persist_diagnostics=lambda **_kwargs: None,
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

    process_fail_safe = ProcessShutdownFailSafe(
        AcknowledgingClient(),
        persist_diagnostics=lambda **_kwargs: None,
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


def test_diagnostic_publication_is_memory_only_until_persisted() -> None:
    persistence_calls = []
    process_fail_safe = ProcessShutdownFailSafe(
        persist_diagnostics=lambda **kwargs: persistence_calls.append(kwargs),
        force_exit=lambda _code: None,
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


def test_async_diagnostic_persistence_never_blocks_its_caller() -> None:
    persistence_entered = ThreadEvent()
    release_persistence = ThreadEvent()

    def blocked_persistence(**_kwargs):
        persistence_entered.set()
        release_persistence.wait(1.0)

    process_fail_safe = ProcessShutdownFailSafe(
        persist_diagnostics=blocked_persistence,
        force_exit=lambda _code: None,
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
