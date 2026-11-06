"""Independent publication checks across the GUI/worker export boundary."""

import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from qplot.datahandling.file_identity import database_instance
from qplot.windows import _export_paths
from qplot.windows._database_load_state import DatabaseLoadSession


def _remaining_stages(target):
    return list(target.parent.glob(f".{target.name}.*"))


def test_staged_export_writes_off_thread_but_validates_and_publishes_on_gui(
    tmp_path, monkeypatch,
):
    main_thread = threading.get_ident()
    observed_threads = []
    original_collect = _export_paths.collect_protected_database_files

    def collect_on_gui(owner):
        observed_threads.append(threading.get_ident())
        assert threading.get_ident() == main_thread
        return original_collect(owner)

    monkeypatch.setattr(_export_paths, "collect_protected_database_files", collect_on_gui)
    target = tmp_path / "signal.csv"
    destination = _export_paths.prepare_export_destination(None, str(target))
    stage = _export_paths.StagedExport(destination)
    paths, errors, receipts = [], [], []

    def writer(staging_path):
        assert threading.get_ident() != main_thread
        paths.append(Path(staging_path))
        Path(staging_path).write_text("gate,signal\n1,7\n", encoding="utf-8")
        return {"rows": 1}

    def run():
        try:
            receipts.append(stage.write(writer))
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=run)
    try:
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert not errors
        assert receipts == [{"rows": 1}]
        assert not target.exists()
        assert paths[0].is_file()
        assert stage.publish()
        assert target.read_text(encoding="utf-8") == "gate,signal\n1,7\n"
        assert observed_threads
        assert not paths[0].exists()
    finally:
        thread.join(timeout=5)
        stage.close()
        stage.close()
    assert not _remaining_stages(target)


@pytest.mark.parametrize("existing", [False, True])
def test_staged_export_target_changed_before_publication_is_preserved(tmp_path, existing):
    target = tmp_path / "signal.csv"
    if existing:
        target.write_bytes(b"approved old output")
    destination = _export_paths.prepare_export_destination(
        None, str(target), replacement_confirmed=existing,
    )
    stage = _export_paths.StagedExport(destination)
    try:
        stage.write(lambda name: Path(name).write_bytes(b"new export"))
        other = tmp_path / "concurrent.csv"
        other.write_bytes(b"concurrently installed output")
        os.replace(other, target)
        with pytest.raises(_export_paths.UnsafeExportDestinationError, match="changed"):
            stage.publish()
        assert target.read_bytes() == b"concurrently installed output"
    finally:
        stage.close()
    assert not _remaining_stages(target)


def test_staged_export_checks_newly_protected_database_path_before_publish(tmp_path):
    target = tmp_path / "new-measurement.csv"
    owner = SimpleNamespace()
    destination = _export_paths.prepare_export_destination(owner, str(target))
    stage = _export_paths.StagedExport(destination)
    try:
        stage.write(lambda name: Path(name).write_bytes(b"new export"))
        # The application starts opening this path while an export is writing.
        # Even an absent source/sidecar is protected by the pending load.
        owner._database_load_state = DatabaseLoadSession(
            abspath=str(target), load_instance=database_instance(target),
        )
        with pytest.raises(_export_paths.UnsafeExportDestinationError, match="database"):
            stage.publish()
        assert not target.exists()
    finally:
        stage.close()
    assert not _remaining_stages(target)


def test_staged_export_source_validation_failure_preserves_approved_output(tmp_path):
    target = tmp_path / "signal.csv"
    target.write_bytes(b"previous export")
    destination = _export_paths.prepare_export_destination(
        None, str(target), replacement_confirmed=True,
    )
    stage = _export_paths.StagedExport(destination)

    def source_replaced():
        raise RuntimeError("source instance changed")

    try:
        stage.write(lambda name: Path(name).write_bytes(b"stale source data"))
        with pytest.raises(RuntimeError, match="source instance changed"):
            stage.publish(before_publish=source_replaced)
        assert target.read_bytes() == b"previous export"
    finally:
        stage.close()
    assert not _remaining_stages(target)
