"""Regression tests for deadline-bounded partial helper reply frames."""

from __future__ import annotations

import gc
import multiprocessing
import threading
import time
import weakref
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from qplot.datahandling.trusted_live_supervisor import (
    TrustedLiveHelperForcedTerminationError,
    TrustedLiveHelperStartupError,
    TrustedLiveReaderSupervisor,
    _HelperIncarnation,
)
from tests.datahandling.test_trusted_live_supervisor import (
    _ApswWalWriter,
    _protected_artifact_contents,
)

pytestmark = pytest.mark.timeout(120)


@pytest.fixture
def partial_frame_wal_writer(tmp_path: Path) -> Iterator[_ApswWalWriter]:
    """Keep a real WAL writer open while reply framing is faulted."""

    writer = _ApswWalWriter.start(tmp_path / "partial-frame-live.db")
    try:
        assert writer.request("barrier") == 1
        yield writer
    finally:
        writer.close()


def _assert_incarnation_fully_stopped(
    supervisor: TrustedLiveReaderSupervisor,
    helper: _HelperIncarnation,
    helper_pid: int,
) -> None:
    assert helper.reply_receiver_done.wait(2.0)
    assert not helper.reply_receiver.is_alive()
    assert helper.reply_connection.closed
    assert helper.command_send.closed
    assert helper.control_send.closed
    assert not supervisor.helper_alive
    assert all(
        process.pid != helper_pid for process in multiprocessing.active_children()
    )


def _assert_writer_resumes(writer: _ApswWalWriter, value: str) -> None:
    assert writer.request("commit", value=value) >= 1
    assert writer.request("checkpoint", mode="TRUNCATE") == (0, 0, 0)


@pytest.mark.parametrize("fragment", ["header", "body"])
def test_partial_startup_reply_reaches_deadline_and_reaps_receiver(
    partial_frame_wal_writer: _ApswWalWriter,
    monkeypatch: pytest.MonkeyPatch,
    fragment: str,
) -> None:
    writer = partial_frame_wal_writer
    protected_before = _protected_artifact_contents(writer.database_path)
    captured: list[tuple[_HelperIncarnation, int]] = []
    real_wait = TrustedLiveReaderSupervisor._wait_startup_reply_locked

    def capture_helper(
        supervisor: TrustedLiveReaderSupervisor,
        helper: _HelperIncarnation,
    ) -> Any:
        helper_pid = helper.process.pid
        assert helper_pid is not None
        captured.append((helper, helper_pid))
        notification = helper.test_notify_receive
        assert notification is not None
        assert notification.poll(10.0)
        assert (
            notification.recv_bytes(256) == f"partial_startup_{fragment}_sent".encode()
        )
        return real_wait(supervisor, helper)

    monkeypatch.setattr(
        TrustedLiveReaderSupervisor,
        "_wait_startup_reply_locked",
        capture_helper,
    )
    started = time.monotonic()
    with pytest.raises(TrustedLiveHelperStartupError, match="parent deadline"):
        TrustedLiveReaderSupervisor.open(
            writer.database_path,
            startup_timeout_seconds=0.35,
            terminate_timeout_seconds=0.5,
            kill_timeout_seconds=0.5,
            _test_fault=f"partial_startup_{fragment}",
        )
    elapsed = time.monotonic() - started

    assert elapsed < 5.0
    assert len(captured) == 1
    helper, helper_pid = captured[0]
    assert helper.reply_receiver_done.wait(2.0)
    assert not helper.reply_receiver.is_alive()
    assert helper.reply_connection.closed
    assert all(
        process.pid != helper_pid for process in multiprocessing.active_children()
    )
    assert _protected_artifact_contents(writer.database_path) == protected_before
    _assert_writer_resumes(writer, f"after-partial-startup-{fragment}")


@pytest.mark.parametrize("fragment", ["header", "body"])
def test_partial_job_reply_times_out_without_reuse_or_replay(
    partial_frame_wal_writer: _ApswWalWriter,
    fragment: str,
) -> None:
    writer = partial_frame_wal_writer
    supervisor = TrustedLiveReaderSupervisor.open(
        writer.database_path,
        reply_timeout_seconds=0.25,
        cancellation_grace_seconds=0.15,
        terminate_timeout_seconds=0.5,
        kill_timeout_seconds=0.5,
        _test_fault=f"partial_job_{fragment}",
    )
    helper = supervisor._helper
    helper_pid = supervisor.helper_pid
    assert helper is not None
    assert helper_pid is not None
    protected_before = _protected_artifact_contents(writer.database_path)
    job = supervisor.submit_query("SELECT count(*) FROM supervisor_probe", timeout=5)
    supervisor._wait_for_test_notification(b"operation_started", 10.0)
    supervisor._wait_for_test_notification(
        f"partial_job_{fragment}_sent".encode(),
        10.0,
    )

    started = time.monotonic()
    with pytest.raises(TrustedLiveHelperForcedTerminationError):
        supervisor.wait(job)
    assert time.monotonic() - started < 3.0
    assert job.done
    assert job.cancellation_requested
    _assert_incarnation_fully_stopped(supervisor, helper, helper_pid)
    assert _protected_artifact_contents(writer.database_path) == protected_before
    _assert_writer_resumes(writer, f"after-partial-job-{fragment}")

    # The failed generation is never replayed.  A later explicit operation
    # gets one fresh incarnation and sees exactly the writer's two rows.
    supervisor.restart()
    assert supervisor.incarnation == helper.number + 1
    result = supervisor.query(
        "SELECT count(*) FROM supervisor_probe",
        timeout=5.0,
        wait_timeout=10.0,
    )
    assert result.rows == ((2,),)
    assert supervisor.helper_pid is not None
    assert supervisor.helper_pid != helper_pid
    assert supervisor.incarnation == helper.number + 1
    replacement_pid = supervisor.helper_pid
    supervisor.close()
    assert replacement_pid is not None
    assert all(
        process.pid != replacement_pid for process in multiprocessing.active_children()
    )


def test_explicit_cancel_of_partial_job_reply_is_bounded(
    partial_frame_wal_writer: _ApswWalWriter,
) -> None:
    writer = partial_frame_wal_writer
    supervisor = TrustedLiveReaderSupervisor.open(
        writer.database_path,
        cancellation_grace_seconds=0.15,
        terminate_timeout_seconds=0.5,
        kill_timeout_seconds=0.5,
        _test_fault="partial_job_body",
    )
    helper = supervisor._helper
    helper_pid = supervisor.helper_pid
    assert helper is not None
    assert helper_pid is not None
    protected_before = _protected_artifact_contents(writer.database_path)
    job = supervisor.submit_query("SELECT 41 + 1", timeout=5.0)
    supervisor._wait_for_test_notification(b"operation_started", 10.0)
    supervisor._wait_for_test_notification(b"partial_job_body_sent", 10.0)

    started = time.monotonic()
    assert supervisor.cancel(job, grace_timeout=0.15)
    assert time.monotonic() - started < 3.0
    with pytest.raises(TrustedLiveHelperForcedTerminationError):
        supervisor.wait(job, timeout=0.0)

    _assert_incarnation_fully_stopped(supervisor, helper, helper_pid)
    assert _protected_artifact_contents(writer.database_path) == protected_before
    _assert_writer_resumes(writer, "after-explicit-partial-cancel")
    supervisor.close()


def test_abandoned_partial_job_finalizer_reaps_process_and_receiver(
    partial_frame_wal_writer: _ApswWalWriter,
) -> None:
    writer = partial_frame_wal_writer
    protected_before = _protected_artifact_contents(writer.database_path)
    supervisor = TrustedLiveReaderSupervisor.open(
        writer.database_path,
        terminate_timeout_seconds=0.5,
        kill_timeout_seconds=0.5,
        _test_fault="partial_job_header",
    )
    helper = supervisor._helper
    helper_pid = supervisor.helper_pid
    assert helper is not None
    assert helper_pid is not None
    job = supervisor.submit_query("SELECT 42", timeout=5.0)
    supervisor._wait_for_test_notification(b"operation_started", 10.0)
    supervisor._wait_for_test_notification(b"partial_job_header_sent", 10.0)
    supervisor_reference = weakref.ref(supervisor)

    started = time.monotonic()
    del supervisor
    deadline = started + 5.0
    while time.monotonic() < deadline:
        gc.collect()
        if (
            supervisor_reference() is None
            and job.done
            and helper.reply_receiver_done.is_set()
            and all(
                process.pid != helper_pid
                for process in multiprocessing.active_children()
            )
        ):
            break
        time.sleep(0.01)

    assert time.monotonic() - started < 5.0
    assert supervisor_reference() is None
    assert job.done
    assert isinstance(job._error, TrustedLiveHelperForcedTerminationError)
    assert helper.reply_receiver_done.is_set()
    assert not helper.reply_receiver.is_alive()
    assert helper.reply_connection.closed
    assert all(
        process.pid != helper_pid for process in multiprocessing.active_children()
    )
    assert _protected_artifact_contents(writer.database_path) == protected_before
    _assert_writer_resumes(writer, "after-abandoned-partial-job")


@pytest.mark.parametrize("fragment", ["header", "body"])
def test_partial_shutdown_reply_keeps_close_bounded_and_reaps_receiver(
    partial_frame_wal_writer: _ApswWalWriter,
    fragment: str,
) -> None:
    writer = partial_frame_wal_writer
    supervisor = TrustedLiveReaderSupervisor.open(
        writer.database_path,
        shutdown_timeout_seconds=0.25,
        terminate_timeout_seconds=0.5,
        kill_timeout_seconds=0.5,
        _test_fault=f"partial_shutdown_{fragment}",
    )
    helper = supervisor._helper
    helper_pid = supervisor.helper_pid
    assert helper is not None
    assert helper_pid is not None
    assert supervisor.query("SELECT 1", timeout=5.0).rows == ((1,),)
    supervisor._wait_for_test_notification(b"operation_started", 10.0)
    protected_before = _protected_artifact_contents(writer.database_path)
    close_errors: list[BaseException] = []

    def close_supervisor() -> None:
        try:
            supervisor.close(timeout=0.25)
        except BaseException as error:
            close_errors.append(error)

    started = time.monotonic()
    closer = threading.Thread(target=close_supervisor)
    closer.start()
    notification = helper.test_notify_receive
    assert notification is not None
    assert notification.poll(10.0)
    assert notification.recv_bytes(256) == f"partial_shutdown_{fragment}_sent".encode()
    closer.join(5.0)
    elapsed = time.monotonic() - started

    assert not closer.is_alive()
    assert elapsed < 5.0
    assert len(close_errors) == 1
    assert isinstance(close_errors[0], TrustedLiveHelperForcedTerminationError)
    assert supervisor.closed
    _assert_incarnation_fully_stopped(supervisor, helper, helper_pid)
    assert _protected_artifact_contents(writer.database_path) == protected_before
    _assert_writer_resumes(writer, f"after-partial-shutdown-{fragment}")
