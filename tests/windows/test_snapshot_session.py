"""Snapshot work ownership without an event loop or widget test doubles."""

from __future__ import annotations

import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from qplot.datahandling.file_identity import database_instance
from qplot.datahandling.trusted_query_types import TrustedSourceRevision
from qplot.windows._snapshot_session import (
    SnapshotRequestFence,
    SnapshotSession,
    safe_equal,
)


class _Source:
    session_token = "snapshot-session"

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls: list[object] = []

    def page(self, parent, continuation, *, cancel_check):
        self.calls.append(parent)
        self.started.set()
        # Deliberately do not poll cancellation while blocked. Shutdown must
        # keep accounting for this work until it actually finishes.
        assert self.release.wait(5.0)
        cancel_check()
        return parent

    def full_value(self, handle, *, cancel_check):
        return self.page(handle, None, cancel_check=cancel_check)


@pytest.fixture
def browsing(tmp_path):
    source = _Source()
    ready = queue.Queue()
    rejected = []
    session = SnapshotSession(
        ready=lambda: ready.put(threading.get_ident()),
        is_current=lambda _fence: True,
        reject=lambda request: rejected.append((request, threading.get_ident())),
    )
    session.bind(source, source.session_token)

    def request(index=0):
        identity = SimpleNamespace(
            session_token=source.session_token,
            parent_handle=f"parent-{index}",
            continuation=None,
        )
        return SnapshotRequestFence(
            database_instance=database_instance(tmp_path / "snapshot.db"),
            binding_serial=1,
            coordinator_generation=1,
            helper_incarnation=1,
            source_revision=TrustedSourceRevision(b"snapshot-revision"),
            selection_generation=1,
            run_index=0,
            run_id=1,
            run_guid="guid-1",
            # Database/service identity is opaque to the session; the bridge's
            # is_current callback validates it in the Qt integration tests.
            service=SimpleNamespace(session_generation=1),
            service_session_generation=1,
            snapshot_token=identity.session_token,
            session_serial=session.serial,
            parent_handle=identity.parent_handle,
            continuation=identity.continuation,
            request=identity,
            kind="page",
        )

    try:
        yield session, source, ready, rejected, request
    finally:
        source.release.set()
        # Only test teardown waits; production shutdown is always nonblocking.
        executor = session._executor
        session.shutdown()
        if executor is not None:
            executor.shutdown(wait=True)


def test_completed_results_keep_slots_until_the_owner_drains_them(browsing):
    session, source, ready, _rejected, request = browsing
    first, second, third = (request(index) for index in range(3))
    assert session.submit(first)
    assert source.started.wait(1.0)
    assert session.submit(second)
    assert not session.submit(third)
    assert source.calls == [first.parent_handle]

    source.release.set()
    assert ready.get(timeout=2.0) != threading.get_ident()
    ready.get(timeout=2.0)
    assert session.pending_counts() == (2, 0)
    assert not session.submit(third)

    outcomes = session.take_results()
    assert [outcome.value for outcome in outcomes] == [
        first.parent_handle,
        second.parent_handle,
    ]
    assert all(session.accepts(outcome) for outcome in outcomes)
    assert session.pending_counts() == (0, 0)
    assert session.submit(third)
    ready.get(timeout=2.0)
    assert session.accepts(session.take_results()[0])


def test_drained_result_is_stale_after_rebinding_the_same_source_and_token(browsing):
    session, source, ready, _rejected, request = browsing
    source.release.set()
    assert session.submit(request())
    ready.get(timeout=2.0)
    outcome = session.take_results()[0]
    assert session.accepts(outcome)

    session.bind(source, source.session_token)

    assert not session.accepts(outcome)
    assert session.submit(request())
    ready.get(timeout=2.0)
    assert session.accepts(session.take_results()[0])


def test_shutdown_releases_source_but_tracks_a_worker_that_has_not_stopped(browsing):
    session, source, ready, rejected, request = browsing
    fence = request()
    assert session.submit(fence)
    assert source.started.wait(1.0)
    executor = session._executor
    assert executor is not None

    session.shutdown()
    session.shutdown()

    assert session.source is None
    assert session.pending_counts() == (1, 1)
    assert rejected == [(fence.request, threading.get_ident())]
    assert not session.submit(fence)
    with pytest.raises(RuntimeError, match="closed Snapshot session"):
        session.bind(source, source.session_token)
    source.release.set()
    executor.shutdown(wait=True)
    assert session.pending_counts() == (0, 0)
    assert not session.take_results()
    assert ready.empty()


@pytest.mark.parametrize("operation", ["bind", "cancel", "submit", "take_results", "shutdown"])
def test_session_mutations_require_the_owner_thread(browsing, operation):
    session, source, _ready, _rejected, request = browsing
    arguments = {
        "bind": (source, source.session_token),
        "submit": (request(),),
    }.get(operation, ())
    with ThreadPoolExecutor(max_workers=1) as worker:
        future = worker.submit(getattr(session, operation), *arguments)
        with pytest.raises(RuntimeError, match="owner thread"):
            future.result(timeout=1.0)
    assert session.source is source
    assert session.pending_counts() == (0, 0)


@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_snapshot_identity_comparison_errors_are_rejected(error_type) -> None:
    class RaisingIdentity:
        def __eq__(self, other):
            raise error_type("malformed snapshot identity")

    assert not safe_equal(RaisingIdentity(), object())


def test_snapshot_identity_non_boolean_comparison_is_rejected() -> None:
    class NonBooleanIdentity:
        def __eq__(self, other):
            return object()

    assert not safe_equal(NonBooleanIdentity(), object())


@pytest.mark.parametrize("left,right,expected", [(1, 1, True), (1, 2, False), (None, None, True)])
def test_snapshot_identity_boolean_comparison(left, right, expected) -> None:
    assert safe_equal(left, right) is expected
