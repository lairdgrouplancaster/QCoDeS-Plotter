"""Documentation screenshot generation owns only its fresh temporary database."""

import hashlib
import importlib.util
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.parameters import ManualParameter


@pytest.fixture
def screenshot_script(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("QPLOT_DEMO_WORKDIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location(
        "owned_screenshot_script", source / "scripts" / "capture_demo_screenshots.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.WORK_DIR == tmp_path
    return module


def _artifact_state(path):
    return {
        suffix: (
            artifact.stat().st_size,
            artifact.stat().st_mtime_ns,
            hashlib.sha256(artifact.read_bytes()).hexdigest(),
        )
        for suffix in ("", "-wal", "-shm", "-journal")
        for artifact in [Path(f"{path}{suffix}")]
    }


def _assert_demo_values(database, line_guid, heatmap_guid):
    with closing(
        sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
    ) as connection:
        runs = connection.execute(
            "SELECT guid, result_table_name FROM runs ORDER BY run_id"
        ).fetchall()
        assert [guid for guid, _table in runs] == [line_guid, heatmap_guid]
        line = connection.execute(
            f'SELECT gate, current FROM "{runs[0][1]}" ORDER BY id'
        ).fetchall()
        heatmap = connection.execute(
            f'SELECT gate, bias, conductance FROM "{runs[1][1]}" ORDER BY id'
        ).fetchall()
    assert len(line) == 81
    assert line[40] == (0.0, 0.0)
    assert len(heatmap) == 45 * 35
    assert heatmap[0][:2] == (-2.0, -1.0)
    assert heatmap[-1][:2] == (2.0, 1.0)


def test_repeated_generation_preserves_existing_qcodes_database_and_sidecars(
    screenshot_script, tmp_path
):
    existing = tmp_path / "qplot-demo.db"
    initialise_or_create_database_at(str(existing))
    experiment = load_or_create_experiment("existing_measurement", sample_name="owned")
    experiment.conn.execute("PRAGMA journal_mode=WAL")
    signal = ManualParameter("signal")
    measurement = Measurement(exp=experiment)
    measurement.register_parameter(signal)
    try:
        with measurement.run(write_in_background=False) as datasaver:
            datasaver.add_result((signal, 123.456))
        # A rollback-journal sentinel also belongs to this test. The live WAL
        # and SHM above are SQLite-created artifacts of the existing dataset.
        Path(f"{existing}-journal").write_bytes(b"owned rollback-journal sentinel")
        original = _artifact_state(existing)
        directories = []
        for _ in range(2):
            with screenshot_script.build_demo_database() as database:
                path, line_guid, heatmap_guid = database
                assert path.parent.parent == tmp_path
                assert path.parent not in directories
                directories.append(path.parent)
                _assert_demo_values(path, line_guid, heatmap_guid)
                assert _artifact_state(existing) == original
            assert not path.parent.exists()
            assert _artifact_state(existing) == original
    finally:
        experiment.conn.close()


def test_main_keeps_private_database_alive_until_capture_failure_cleanup(
    screenshot_script, tmp_path, monkeypatch
):
    observed = []
    monkeypatch.setattr(screenshot_script, "configure_environment", lambda: None)

    def capture(database_path, line_guid, heatmap_guid):
        _assert_demo_values(database_path, line_guid, heatmap_guid)
        observed.append(database_path)
        raise RuntimeError("owned screenshot failure")

    monkeypatch.setattr(screenshot_script, "capture_screenshots", capture)
    with pytest.raises(RuntimeError, match="owned screenshot failure"):
        screenshot_script.main()
    assert len(observed) == 1
    assert not observed[0].parent.exists()
    assert list(tmp_path.iterdir()) == []
