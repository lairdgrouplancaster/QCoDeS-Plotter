"""Cancellation accepted during result cleanup must prevent publication."""

import multiprocessing
import threading

import pytest
from qcodes.dataset import initialise_or_create_database_at

from qplot.datahandling._trusted_live_helper import trusted_live_helper_main
from qplot.datahandling._trusted_live_protocol import encode_cancel
from qplot.datahandling.trusted_live import (
    TrustedLiveCancelledError,
    TrustedLiveReader,
)
from qplot.datahandling.trusted_live_supervisor import (
    TrustedLiveProtocolError,
    TrustedLiveReaderSupervisor,
)


def test_interrupt_accepted_after_rows_are_read_prevents_publication(tmp_path, monkeypatch):
    database = tmp_path / "source.db"
    initialise_or_create_database_at(str(database), journal_mode="DELETE")
    before = database.read_bytes()
    before_mtime = database.stat().st_mtime_ns
    reached_cleanup = threading.Event()
    interrupted = threading.Event()
    accepted = []

    with TrustedLiveReader.open(database) as reader:
        restore = reader._restore_result_length_limit

        def pause_after_restore(*args):
            result = restore(*args)
            reached_cleanup.set()
            assert interrupted.wait(5), "Interrupt thread did not complete"
            assert accepted == [True]
            return result

        def interrupt_during_cleanup():
            if reached_cleanup.wait(5):
                accepted.append(reader.interrupt())
            interrupted.set()

        with monkeypatch.context() as patch:
            patch.setattr(reader, "_restore_result_length_limit", pause_after_restore)
            worker = threading.Thread(target=interrupt_during_cleanup)
            worker.start()
            try:
                with pytest.raises(TrustedLiveCancelledError):
                    reader.query("SELECT count(*) FROM runs")
            finally:
                worker.join(5)
                assert not worker.is_alive()

        assert accepted == [True]
        assert reader.query("SELECT count(*) FROM runs").rows == ((0,),)
        assert not reader.interrupt()

    assert database.read_bytes() == before
    assert database.stat().st_mtime_ns == before_mtime


@pytest.mark.parametrize("invalid_generation", [1, 100])
def test_completed_generation_window_rejects_prior_incarnation_and_future(
    tmp_path, invalid_generation,
):
    database = tmp_path / "source.db"
    initialise_or_create_database_at(str(database), journal_mode="DELETE")
    supervisor = TrustedLiveReaderSupervisor(database)
    supervisor._next_generation = 17
    try:
        assert supervisor.query("SELECT 1").rows == ((1,),)
        current = supervisor.submit_query(
            "WITH RECURSIVE x(n) AS (SELECT 0 UNION ALL SELECT n + 1 FROM x "
            "WHERE n < 100000000) SELECT sum(n) FROM x"
        )
        supervisor._send_test_control_frame(
            encode_cancel(current.session, invalid_generation)
        )
        with pytest.raises(TrustedLiveProtocolError):
            supervisor.wait(current)
    finally:
        supervisor.close()


class _DelayedControl:
    """Pause only the child's control-thread scheduling, preserving real IPC."""

    def __init__(self, connection, allow, consumed):
        self.connection, self.allow, self.consumed = connection, allow, consumed

    def poll(self, timeout):
        return self.allow.wait(timeout) and self.connection.poll(timeout)

    def recv_bytes(self, limit):
        frame = self.connection.recv_bytes(limit)
        self.consumed.set()
        return frame

    def close(self):
        self.connection.close()


def _delayed_helper(args, allow, consumed):
    args = list(args)
    args[2] = _DelayedControl(args[2], allow, consumed)
    trusted_live_helper_main(*args)


class _DelayedControlContext:
    def __init__(self, context, allow, consumed):
        self.context, self.allow, self.consumed = context, allow, consumed

    def Pipe(self, **kwargs):
        return self.context.Pipe(**kwargs)

    def Process(self, *, target, args, name):
        assert target is trusted_live_helper_main
        return self.context.Process(
            target=_delayed_helper,
            args=(args, self.allow, self.consumed),
            name=name,
        )


@pytest.mark.parametrize("first_generation", [1, 17])
def test_valid_cancel_delayed_past_two_completions_keeps_helper_usable(
    tmp_path, first_generation,
):
    database = tmp_path / "source.db"
    initialise_or_create_database_at(str(database), journal_mode="DELETE")
    before = database.read_bytes()
    before_mtime = database.stat().st_mtime_ns
    context = multiprocessing.get_context("spawn")
    allow, consumed = context.Event(), context.Event()
    supervisor = TrustedLiveReaderSupervisor(database)
    supervisor._context = _DelayedControlContext(context, allow, consumed)
    supervisor._next_generation = first_generation
    try:
        first = supervisor.submit_query("SELECT 1")
        # The cancel frame is sent by the public API into the real control
        # pipe. The child can complete this job before its control thread runs.
        assert supervisor.cancel(first)
        assert supervisor.wait(first).rows == ((1,),)
        assert supervisor.query("SELECT 2").rows == ((2,),)
        helper_pid = supervisor.helper_pid
        allow.set()
        assert consumed.wait(5)
        # Give the delayed controller a chance to run during this query. Its
        # older, already completed job must never interrupt this generation.
        assert supervisor.query(
            "WITH RECURSIVE x(n) AS (SELECT 0 UNION ALL SELECT n + 1 FROM x "
            "WHERE n < 1000000) SELECT sum(n) FROM x"
        ).rows == ((500000500000,),)
        assert supervisor.query("SELECT 4").rows == ((4,),)
        assert supervisor.helper_pid == helper_pid
    finally:
        allow.set()
        supervisor.close()
    assert database.read_bytes() == before
    assert database.stat().st_mtime_ns == before_mtime
