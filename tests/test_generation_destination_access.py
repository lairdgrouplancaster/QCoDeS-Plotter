"""Cloud-placeholder failures must not be mistaken for SQLite activity."""

import errno
from contextlib import contextmanager
from pathlib import Path

import pytest

from qplot import testdata
from tests.test_testdata import complete_artifact_state, small_specification


@pytest.mark.parametrize("failure", ["open-timeout", "read-timeout", "permission"])
def test_unreadable_destination_reports_cause_before_generation(
    tmp_path, monkeypatch, failure
):
    destination = testdata.generate_database(
        [small_specification()], tmp_path / "existing.db"
    )
    before = complete_artifact_state(destination)
    original_open = Path.open
    reads, generated = [], []
    error = (
        PermissionError(errno.EACCES, "Permission denied")
        if failure == "permission"
        else TimeoutError(errno.ETIMEDOUT, "Operation timed out")
    )

    @contextmanager
    def unavailable_read():
        class Reader:
            def read(self, count):
                reads.append(count)
                raise error

        yield Reader()

    def open_destination(path, *args, **kwargs):
        if path == destination:
            if failure == "read-timeout":
                return unavailable_read()
            raise error
        return original_open(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", open_destination)
        patch.setattr(
            testdata, "_write_run", lambda *args, **kwargs: generated.append(True)
        )
        with pytest.raises(testdata.SpecificationError) as caught:
            testdata.generate_database(
                [small_specification()], destination, overwrite=True
            )
    message = str(caught.value)
    assert "database active or SQLite sidecars present" not in message
    assert str(error) in message
    assert "qPlot did not change" in message
    assert "new output filename" in message
    if failure == "permission":
        assert "permission" in message
    else:
        assert "Timed out" in message and "available offline" in message
    assert reads == ([20] if failure == "read-timeout" else [])
    assert generated == []
    assert complete_artifact_state(destination) == before
    assert list(tmp_path.iterdir()) == [destination]


def test_destination_stat_timeout_reports_access_failure(tmp_path, monkeypatch):
    destination = tmp_path / "cloud.db"
    original_lstat = Path.lstat
    error = TimeoutError(errno.ETIMEDOUT, "Operation timed out")

    def timed_out_stat(path, *args, **kwargs):
        if path == destination:
            raise error
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", timed_out_stat)
    with pytest.raises(
        testdata.SpecificationError, match="Timed out trying to inspect"
    ) as caught:
        testdata.generate_database([small_specification()], destination, overwrite=True)
    assert caught.value.__cause__ is error
    assert list(tmp_path.iterdir()) == []


def test_destination_read_failure_at_publication_preserves_old_database(
    tmp_path, monkeypatch
):
    destination = testdata.generate_database(
        [small_specification()], tmp_path / "existing.db"
    )
    before = complete_artifact_state(destination)
    original_open = Path.open
    original_write = testdata._write_run
    staged = False

    def write_then_lose_access(*args, **kwargs):
        nonlocal staged
        result = original_write(*args, **kwargs)
        staged = True
        return result

    def fail_final_read(path, *args, **kwargs):
        if staged and path == destination:
            raise TimeoutError(errno.ETIMEDOUT, "Operation timed out")
        return original_open(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(testdata, "_write_run", write_then_lose_access)
        patch.setattr(Path, "open", fail_final_read)
        with pytest.raises(
            testdata.SpecificationError, match="Timed out trying to read"
        ):
            testdata.generate_database(
                [small_specification()], destination, overwrite=True
            )
    assert complete_artifact_state(destination) == before
    assert list(tmp_path.iterdir()) == [destination]


@pytest.mark.parametrize("outcome", ["ready", "timeout", "cancelled", "changed"])
def test_gui_worker_downloads_cloud_target_before_generating(
    tmp_path, monkeypatch, outcome
):
    from qplot.windows import _database_actions as actions

    destination = testdata.generate_database(
        [small_specification()], tmp_path / "cloud.db"
    )
    before = complete_artifact_state(destination)
    events, results, statuses = [], [], []
    worker = actions.TestDatabaseGenerationWorker(
        [small_specification()],
        destination,
        overwrite=True,
        cloud_sync_timeout=17,
    )
    worker.signals.status.connect(statuses.append)
    worker.signals.finished.connect(lambda *args: results.append(args))
    monkeypatch.setattr(
        actions, "database_is_likely_cloud_placeholder", lambda path: True
    )

    def download(path, *, timeout, status_callback, cancelled_callback):
        assert path == str(destination)
        assert timeout == 17
        assert not cancelled_callback()
        events.append("download")
        status_callback("Waiting for OneDrive sync... 50% available")
        if outcome == "timeout":
            raise TimeoutError("OneDrive download timed out")
        if outcome == "cancelled":
            worker.cancel()
            raise InterruptedError("Download cancelled")
        if outcome == "changed":
            destination.write_bytes(b"concurrent replacement")

    def generate(*args, **kwargs):
        events.append("generate")
        assert kwargs["overwrite"] is True
        return destination

    monkeypatch.setattr(actions, "prefetch_database_file_with_timeout", download)
    monkeypatch.setattr(actions, "generate_database", generate)
    worker.run()
    assert len(results) == 1
    error = results[0][2]
    assert statuses[0] == "Waiting for OneDrive sync... 50% available"
    assert events == (["download", "generate"] if outcome == "ready" else ["download"])
    if outcome == "ready":
        assert error is None
        assert "Generating test database" in statuses[-1]
    elif outcome == "cancelled":
        assert isinstance(error, testdata.GenerationCancelled)
    elif outcome == "changed":
        assert "changed while downloading" in str(error)
        assert destination.read_bytes() == b"concurrent replacement"
    else:
        assert isinstance(error, TimeoutError)
    if outcome != "changed":
        assert complete_artifact_state(destination) == before


def test_gui_worker_skips_cloud_download_for_local_or_new_output(tmp_path, monkeypatch):
    from qplot.windows import _database_actions as actions

    def no_download(*args, **kwargs):
        pytest.fail("Local/new output must not be downloaded")

    monkeypatch.setattr(actions, "prefetch_database_file_with_timeout", no_download)
    monkeypatch.setattr(
        actions, "database_is_likely_cloud_placeholder", lambda path: False
    )
    destination = testdata.generate_database(
        [small_specification()], tmp_path / "existing.db"
    )
    for path, overwrite in ((destination, True), (tmp_path / "new.db", False)):
        worker = actions.TestDatabaseGenerationWorker(
            [small_specification()], path, overwrite=overwrite
        )
        results = []
        worker.signals.finished.connect(lambda *args, captured=results: captured.append(args))
        worker.run()
        assert results[0][2] is None
