"""Prove descriptor ownership at the source-header read boundary."""

import errno
import os

import pytest

from qplot.datahandling import trusted_live


@pytest.mark.parametrize("failure", [None, OSError, KeyboardInterrupt, SystemExit])
def test_header_descriptor_is_readonly_and_closed_after_reads_or_failures(
    tmp_path, monkeypatch, failure,
):
    path = tmp_path / "header.bin"
    path.write_bytes(b"SQLite format 3\x00\x10\x00\x01\x01".ljust(100, b"\x00"))
    real_open, real_read, real_close = os.open, os.read, os.close
    opened, closed, read_sizes = [], [], []

    def record_open(path, flags):
        assert flags & (os.O_WRONLY | os.O_RDWR) == os.O_RDONLY
        descriptor = real_open(path, flags)
        opened.append(descriptor)
        return descriptor

    def read_fragment(descriptor, size):
        read_sizes.append(size)
        if failure is not None:
            raise failure("injected header read failure")
        return real_read(descriptor, min(size, 3))

    def record_close(descriptor):
        closed.append(descriptor)
        real_close(descriptor)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", record_open)
        patch.setattr(os, "read", read_fragment)
        patch.setattr(os, "close", record_close)
        if failure is None:
            mode = trusted_live._database_header_journal_mode(path)
            assert mode == "rollback"
            assert len(read_sizes) > 1
        else:
            expected = trusted_live.TrustedLiveSourceIOError if failure is OSError else failure
            with pytest.raises(expected, match="injected header read failure"):
                trusted_live._database_header_journal_mode(path)

    assert len(opened) == 1
    assert closed == opened
    with pytest.raises(OSError):
        os.fstat(opened[0])


@pytest.mark.parametrize("close_errno", [errno.EINTR, errno.EIO])
def test_uncertain_header_close_is_reported_without_retry(
    tmp_path, monkeypatch, close_errno,
):
    path = tmp_path / "header.bin"
    path.write_bytes(b"SQLite format 3\x00\x10\x00\x01\x01".ljust(100, b"\x00"))
    real_close = os.close
    attempts = []

    def uncertain_close(descriptor):
        attempts.append(descriptor)
        # Model a kernel that consumed the descriptor before reporting failure.
        real_close(descriptor)
        raise OSError(close_errno, "injected uncertain close")

    with monkeypatch.context() as patch:
        patch.setattr(os, "close", uncertain_close)
        with pytest.raises(trusted_live._PreflightFileDescriptorCloseError) as caught:
            trusted_live._database_header_journal_mode(path)

    assert caught.value.errno == close_errno
    assert isinstance(caught.value.__cause__, OSError)
    assert len(attempts) == 1
    with pytest.raises(OSError):
        os.fstat(attempts[0])
