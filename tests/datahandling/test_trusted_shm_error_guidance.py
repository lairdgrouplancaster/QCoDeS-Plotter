"""SHM access failures identify the file and retain native diagnostics."""

from types import SimpleNamespace

import pytest

from qplot.datahandling import trusted_live
from qplot.datahandling.file_identity import DatabaseInstance


@pytest.mark.parametrize("operation", ["shm_proof_open", "shm_proof_post_open"])
@pytest.mark.parametrize("platform", ["darwin", "win32", "linux"])
def test_shm_open_failure_has_actionable_guidance(monkeypatch, operation, platform):
    monkeypatch.setattr(trusted_live.sys, "platform", platform)
    reader = SimpleNamespace(_source=SimpleNamespace(database_instance=DatabaseInstance(
        "/selected.db", "/resolved/measurement.db", (1, 2),
    )))
    status = trusted_live._TrustedNativeStatus(1, "io", "shm", operation, 14)
    error = trusted_live.TrustedLiveReader._error_from_native_status(reader, status)
    assert isinstance(error, trusted_live.TrustedLiveSourceIOError)
    message = str(error)
    assert "/resolved/measurement.db-shm" in message
    assert "read and write access" in message
    assert f"shm/{operation}, SQLite code 14" in message
    assert ("Files and Folders" in message) == (platform == "darwin")


def test_other_io_failure_keeps_its_diagnostic():
    status = trusted_live._TrustedNativeStatus(1, "io", "main", "read", 10)
    error = trusted_live.TrustedLiveReader._error_from_native_status(SimpleNamespace(), status)
    assert isinstance(error, trusted_live.TrustedLiveSourceIOError)
    assert "main/read, SQLite code 10" in str(error)
    assert "coordination file" not in str(error)
