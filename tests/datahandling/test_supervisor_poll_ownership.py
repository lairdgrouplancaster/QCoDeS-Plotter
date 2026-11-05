"""Polling outcomes cannot act on a helper or job installed during external work."""

from types import SimpleNamespace

import pytest

from qplot.datahandling import trusted_live_supervisor as supervisor_module
from qplot.datahandling._trusted_live_protocol import (
    ProtocolEnvelope,
    TrustedLiveProtocolValidationError,
)
from qplot.datahandling.trusted_live_supervisor import (
    TrustedLiveHelperExitedError,
    TrustedLiveJob,
    TrustedLiveReaderSupervisor,
)


@pytest.mark.parametrize("transition", ["helper_replaced", "job_replaced", "completed"])
@pytest.mark.parametrize(
    ("boundary", "message"),
    [
        ("receive_error", "The job's helper was retired while polling its reply."),
        ("receiver_stopped", "The job's helper was retired while its sole reply receiver was stopping."),
        ("invalid_frame", "The job's helper was retired before its reply could be validated."),
        ("frame_eof", "The job's helper was retired while receiving its reply."),
        ("frame_error", "The job's helper was retired while receiving its reply frame."),
        ("valid_frame", "The job's helper was retired before its reply could be accepted."),
        ("process_error", "The job's helper was retired while its process state was inspected."),
        ("process_exited", "The job's helper was retired before process-exit handling completed."),
    ],
)
def test_polling_rechecks_ownership_after_external_work(
    tmp_path, monkeypatch, boundary, message, transition,
):
    supervisor = TrustedLiveReaderSupervisor(tmp_path / "not-opened.db")
    job = TrustedLiveJob(1, "old-session", 1, "query", supervisor._owner_nonce)
    replacement_job = TrustedLiveJob(2, "new-session", 2, "query", supervisor._owner_nonce)
    replacement_helper = SimpleNamespace(session="new-session")
    transitions = []

    def change_owner():
        # A real concurrent owner can acquire this lock while the poller is
        # receiving, decoding or inspecting the process outside its lock.
        assert not supervisor._lock._is_owned()
        with supervisor._lock:
            transitions.append(transition)
            if transition == "helper_replaced":
                supervisor._helper = replacement_helper
            elif transition == "job_replaced":
                supervisor._active_job = replacement_job
            else:
                supervisor._complete_job(job, value="already accepted")

    def process_alive():
        change_owner()
        if boundary == "process_error":
            raise ValueError("process handle changed")
        assert boundary == "process_exited"
        return False

    helper = SimpleNamespace(
        session=job.session,
        process=SimpleNamespace(is_alive=process_alive, exitcode=1),
    )
    supervisor._helper = helper
    supervisor._active_job = job

    def take_reply_item(actual_helper, _timeout):
        assert actual_helper is helper
        if boundary.startswith("process_"):
            return None
        if boundary in {"receive_error", "receiver_stopped"}:
            change_owner()
            if boundary == "receive_error":
                raise OSError("reply pipe failed")
            return supervisor_module._ReplyReceiveFailure("eof", "receiver stopped")
        return b"reply-frame"

    def decode_reply_frame(frame):
        assert frame == b"reply-frame"
        change_owner()
        if boundary == "invalid_frame":
            raise TrustedLiveProtocolValidationError("invalid frame")
        if boundary == "frame_eof":
            raise EOFError("frame ended early")
        if boundary == "frame_error":
            raise OSError("frame failed")
        assert boundary == "valid_frame"
        return ProtocolEnvelope(job.session, job.generation, job.operation, {})

    def forbidden_handler(*_args, **_kwargs):
        pytest.fail("An obsolete polling outcome reached helper handling")

    monkeypatch.setattr(supervisor, "_take_reply_item", take_reply_item)
    monkeypatch.setattr(supervisor_module, "decode_reply_frame", decode_reply_frame)
    monkeypatch.setattr(supervisor, "_protocol_failure_locked", forbidden_handler)
    monkeypatch.setattr(supervisor, "_unexpected_exit_locked", forbidden_handler)
    monkeypatch.setattr(supervisor, "_handle_job_reply_locked", forbidden_handler)
    try:
        supervisor._poll_job_once(job, 0.0)

        assert transitions == [transition]
        assert supervisor._helper is (
            replacement_helper if transition == "helper_replaced" else helper
        )
        assert supervisor._active_job is (
            replacement_job if transition == "job_replaced" else job
        )
        assert not replacement_job.done
        assert job.done
        if transition == "completed":
            assert job._error is None
            assert job._value == "already accepted"
        else:
            assert isinstance(job._error, TrustedLiveHelperExitedError)
            assert str(job._error) == message
    finally:
        # These are inert stand-ins; no spawned process or pipe is owned.
        supervisor._helper = None
        supervisor._active_job = None
        supervisor.close()
