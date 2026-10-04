"""Current QCoDeS array records must not become scalar metadata counts."""

import builtins
import hashlib
import io
import os
import sqlite3
from pathlib import Path
from urllib.parse import unquote, urlparse

import numpy as np
import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.datahandling import readSQL
from qplot.windows._widgets._run_formatting import format_point_count
from qplot.windows._widgets.treeWidgets import moreInfo


def _protect_source(monkeypatch, path):
    """Reject source writes before the viewer can issue them."""
    protected = {os.path.normcase(str(path.resolve()) + suffix)
                 for suffix in ("", "-wal", "-shm", "-journal")}

    def is_protected(value):
        if isinstance(value, int):
            return False
        return os.path.normcase(os.path.abspath(os.fsdecode(value))) in protected

    def guard_open(original):
        def guarded(file, mode="r", *args, **kwargs):
            assert not (is_protected(file) and any(flag in mode for flag in "wax+"))
            return original(file, mode, *args, **kwargs)
        return guarded

    monkeypatch.setattr(builtins, "open", guard_open(builtins.open))
    monkeypatch.setattr(io, "open", guard_open(io.open))
    original_open = os.open

    def guarded_os_open(file, flags, *args, **kwargs):
        assert not (is_protected(file) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
        return original_open(file, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded_os_open)
    for name in ("remove", "unlink", "rename", "replace", "truncate"):
        original = getattr(os, name)

        def guarded_mutation(path, *args, _original=original, _name=name, **kwargs):
            assert not is_protected(path), f"{_name} attempted on source"
            if _name in ("rename", "replace"):
                assert not is_protected(args[0]), f"{_name} attempted onto source"
            return _original(path, *args, **kwargs)

        monkeypatch.setattr(os, name, guarded_mutation)
    original_connect = sqlite3.connect

    def guarded_connect(database, *args, **kwargs):
        spelling = os.fspath(database)
        if spelling.startswith("file:"):
            spelling = unquote(urlparse(spelling).path).lstrip("/") if os.name == "nt" else unquote(urlparse(spelling).path)
        assert not is_protected(spelling), "SQLite must read only a private snapshot"
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", guarded_connect)


def _artifact_state(path, *, include_digest=True):
    result = {}
    for suffix in ("", "-wal", "-shm", "-journal"):
        artifact = Path(str(path) + suffix)
        if artifact.exists():
            stat = artifact.stat()
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest() if include_digest else None
            result[suffix] = (digest, stat.st_size, stat.st_mtime_ns)
    return result


@pytest.mark.parametrize("array_setpoint", [False, True])
@pytest.mark.parametrize("planned", [False, True])
def test_array_record_metadata_does_not_invent_sample_counts(tmp_path, monkeypatch, array_setpoint, planned):
    path = tmp_path / "arrays.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("arrays", sample_name="metadata")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("x", paramtype="array" if array_setpoint else "numeric")
    measurement.register_custom_parameter("z", paramtype="array", setpoints=("x",))
    if planned:
        measurement.set_shapes({"z": (6,)})
    with measurement.run(write_in_background=False) as saver:
        for offset in (0, 3):
            x = np.arange(offset, offset + 3, dtype=float) if array_setpoint else float(offset)
            saver.add_result(("x", x), ("z", np.arange(3.) + offset))
        run_id, guid = saver.run_id, saver.dataset.guid
    saver.dataset.conn.close()
    experiment.conn.close()
    before = _artifact_state(path)
    _protect_source(monkeypatch, path)

    metadata = readSQL.get_runs_via_sql(str(path))[run_id]
    status = readSQL.get_run_status(guid, str(path))
    for fields in (metadata, status):
        assert fields["result_count"] == 2
        assert fields["setpoint_count"] == (6 if planned else None)
        assert fields["setpoint_shape"] == ([6] if planned else None)
        assert fields["point_shape"] == ([6] if planned else None)
        assert fields["setpoint_count_source"] == ("planned" if planned else None)

    detail = readSQL.get_snapshot_selected_run_detail(str(path), run_id, guid, metadata)
    # Exercise the actual snapshot presentation, including its completed-run
    # physical result-count fallback, rather than only inspecting metadata.
    widget = moreInfo()
    try:
        widget.set_snapshot_run_detail(detail)
        overview = {
            widget.overview.item(row, 0).text(): widget.overview.item(row, 1).text()
            for row in range(widget.overview.rowCount())
        }
        assert "Data points" not in overview
        assert format_point_count(metadata) == ("6" if planned else "unknown")
    finally:
        widget.preview.shutdown()
        widget.close()
        widget.deleteLater()
    if array_setpoint:
        summaries = {summary.name: summary for summary in detail.setpoint_summaries}
        if planned:
            assert summaries["x"].steps == 6
            assert summaries["x"].first is summaries["x"].last is None
        else:
            assert "x" not in summaries
    else:
        # Scalar sweep coordinates remain useful even for array dependents.
        summary = detail.setpoint_summaries[0]
        assert (summary.first, summary.last, summary.steps) == (0., 3., 2)
    assert _artifact_state(path) == before


@pytest.mark.parametrize("kind", ["numeric", "complex"])
def test_scalar_metadata_inference_still_counts_points(tmp_path, monkeypatch, kind):
    path = tmp_path / "scalars.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("scalars", sample_name="metadata")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("x", paramtype="numeric")
    measurement.register_custom_parameter("z", paramtype=kind, setpoints=("x",))
    with measurement.run(write_in_background=False) as saver:
        for x in range(3):
            saver.add_result(("x", x), ("z", x + 2j if kind == "complex" else x))
        run_id = saver.run_id
    saver.dataset.conn.close()
    experiment.conn.close()
    before = _artifact_state(path)
    _protect_source(monkeypatch, path)
    metadata = readSQL.get_runs_via_sql(str(path))[run_id]
    assert metadata["result_count"] == metadata["setpoint_count"] == 3
    assert metadata["point_shape"] == [3]
    assert _artifact_state(path) == before


@pytest.mark.parametrize("planned", [False, True])
def test_live_mixed_array_and_scalar_trees_have_no_global_observed_count(
    tmp_path, monkeypatch, planned,
):
    path = tmp_path / "live-arrays.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("live_arrays", sample_name="metadata")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("x", paramtype="numeric")
    measurement.register_custom_parameter("scalar", setpoints=("x",))
    measurement.register_custom_parameter("array", paramtype="array", setpoints=("x",))
    if planned:
        measurement.set_shapes({"scalar": (2,), "array": (6,)})
    try:
        with measurement.run(write_in_background=False) as saver:
            for x in (0., 1.):
                saver.add_result(("x", x), ("scalar", x), ("array", np.arange(3.) + x))
            saver.flush_data_to_database(block=True)
            # Opening and closing a second file descriptor can release POSIX
            # process locks, so inspect only stat metadata while the writer is
            # alive. Completed-run tests above also compare full digests.
            before = _artifact_state(path, include_digest=False)
            # Release test guards before the actual QCoDeS writer marks its
            # own run complete; only qPlot viewing is protected here.
            with monkeypatch.context() as guard:
                _protect_source(guard, path)
                status = readSQL.get_run_status(saver.dataset.guid, str(path))
                assert not status["is_completed"]
                assert status["read_setpoint_count"] is None
                assert status["setpoint_count"] == (6 if planned else None)
                assert status["setpoint_shape"] == ([6] if planned else None)
                assert _artifact_state(path, include_digest=False) == before
    finally:
        saver.dataset.conn.close()
        experiment.conn.close()
