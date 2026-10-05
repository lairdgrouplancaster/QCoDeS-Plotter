"""The documented live-data helper creates only a new database."""

import builtins
import hashlib
import math
import runpy
from pathlib import Path
from unittest.mock import Mock

import pytest
import qcodes as qc
import qcodes.dataset as dataset_module
from qcodes.dataset import Measurement, load_or_create_experiment
from qcodes.parameters import ManualParameter


@pytest.fixture
def liveplot_script(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[1]
    monkeypatch.chdir(tmp_path)
    return source / "scripts" / "liveplot.py"


def _database_path(tmp_path):
    folder = tmp_path / "tests" / "data"
    folder.mkdir(parents=True)
    return folder / "qplot-demo.db"


def _state(path):
    return {
        artifact.name: (
            artifact.stat().st_size,
            artifact.stat().st_mtime_ns,
            hashlib.sha256(artifact.read_bytes()).hexdigest(),
        )
        for artifact in path.parent.iterdir()
    }


def _guard_initializer(monkeypatch):
    initializer = Mock(
        side_effect=AssertionError("Existing data reached writable SQLite")
    )
    monkeypatch.setattr(dataset_module, "initialise_or_create_database_at", initializer)
    return initializer


def test_existing_real_measurement_is_refused_without_writable_open(
    liveplot_script, tmp_path, monkeypatch
):
    path = _database_path(tmp_path)
    dataset_module.initialise_or_create_database_at(str(path))
    experiment = load_or_create_experiment("existing_liveplot", sample_name="owned")
    signal = ManualParameter("signal")
    measurement = Measurement(exp=experiment)
    measurement.register_parameter(signal)
    try:
        with measurement.run(write_in_background=False) as datasaver:
            datasaver.add_result((signal, 987.654))
        original = _state(path)
        initializer = _guard_initializer(monkeypatch)
        with pytest.raises(FileExistsError, match="existing database artifact"):
            runpy.run_path(str(liveplot_script), run_name="__main__")
        initializer.assert_not_called()
        assert _state(path) == original
    finally:
        experiment.conn.close()


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_orphan_sidecar_is_preserved_and_prevents_creation(
    liveplot_script, tmp_path, monkeypatch, suffix
):
    path = _database_path(tmp_path)
    Path(f"{path}{suffix}").write_bytes(b"owned orphan sidecar")
    original = _state(path)
    initializer = _guard_initializer(monkeypatch)
    with pytest.raises(FileExistsError, match="existing database artifact"):
        runpy.run_path(str(liveplot_script), run_name="__main__")
    initializer.assert_not_called()
    assert not path.exists()
    assert _state(path) == original


def test_exclusive_creation_preserves_file_created_after_artifact_checks(
    liveplot_script, tmp_path, monkeypatch
):
    path = _database_path(tmp_path)
    initializer = _guard_initializer(monkeypatch)
    original_open = builtins.open
    created = []

    def competing_create(filename, mode="r", *args, **kwargs):
        if Path(filename) == path and mode == "xb":
            with original_open(path, "xb") as stream:
                stream.write(b"owned competing database")
            created.append(_state(path))
        return original_open(filename, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", competing_create)
    with pytest.raises(FileExistsError):
        runpy.run_path(str(liveplot_script), run_name="__main__")
    initializer.assert_not_called()
    assert len(created) == 1
    assert _state(path) == created[0]


def test_fresh_path_runs_real_qcodes_dond_with_reduced_test_sweeps(
    liveplot_script, tmp_path, monkeypatch
):
    original_sweep = dataset_module.LinSweep
    original_dond = dataset_module.dond
    connections = {}
    observed = []

    def small_sweep(parameter, start, stop, _points, _delay):
        return original_sweep(parameter, start, stop, 2, 0)

    def real_measurement(*args, **kwargs):
        connection = kwargs["exp"].conn
        connections[id(connection)] = connection
        kwargs.update(do_plot=False, show_progress=False)
        result = original_dond(*args, **kwargs)
        observed.append(result[0])
        connections[id(result[0].conn)] = result[0].conn
        return result

    monkeypatch.setattr(dataset_module, "LinSweep", small_sweep)
    monkeypatch.setattr(dataset_module, "dond", real_measurement)
    try:
        runpy.run_path(str(liveplot_script), run_name="__main__")
        assert len(observed) == 1
        data = observed[0]
        assert Path(data.path_to_db) == tmp_path / "tests" / "data" / "qplot-demo.db"
        for parameter in ("dmm_v1", "dmm_v2"):
            rows = data.conn.execute(
                f'SELECT dac_ch1, dac_ch2, "{parameter}" FROM "{data.table_name}" '
                f'WHERE "{parameter}" IS NOT NULL ORDER BY id'
            ).fetchall()
            assert len(rows) == 4
            assert [(row[0], row[1]) for row in rows] == [
                (-1.0, -1.0),
                (-1.0, 1.0),
                (1.0, -1.0),
                (1.0, 1.0),
            ]
            assert all(math.isfinite(row[2]) for row in rows)
    finally:
        for connection in connections.values():
            connection.close()
        qc.Instrument.close_all()
