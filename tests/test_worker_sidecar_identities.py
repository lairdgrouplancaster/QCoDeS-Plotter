import os
from pathlib import Path

from qplot.datahandling.database import (
    DatabaseDetailWorker,
    DatabaseExpensiveDetailWorker,
    DatabaseLoadWorker,
    DatabaseRefreshWorker,
)
from qplot.datahandling.file_identity import database_sidecar_identities
from qplot.tools.worker import loader
from qplot.windows._widgets.preview import PreviewWorker

_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def _database_with_sidecars(tmp_path: Path) -> Path:
    database_path = tmp_path / "input.db"
    database_path.write_bytes(b"database")
    for suffix in _SIDECAR_SUFFIXES:
        Path(f"{database_path}{suffix}").write_bytes(suffix.encode())
    return database_path


def _replace_sidecars(database_path: Path) -> None:
    for suffix in _SIDECAR_SUFFIXES:
        sidecar_path = Path(f"{database_path}{suffix}")
        replacement_path = Path(f"{database_path}{suffix}.replacement")
        replacement_path.write_bytes(f"replacement {suffix}".encode())
        os.replace(replacement_path, sidecar_path)


def _artifact_state(database_path: Path) -> dict[str, tuple[object, ...]]:
    state = {}
    for suffix in ("", *_SIDECAR_SUFFIXES):
        artifact_path = Path(f"{database_path}{suffix}")
        artifact_stat = artifact_path.stat()
        state[suffix] = (
            artifact_path.read_bytes(),
            artifact_stat.st_dev,
            artifact_stat.st_ino,
            artifact_stat.st_nlink,
            artifact_stat.st_size,
            artifact_stat.st_mtime_ns,
        )
    return state


def test_database_workers_retain_sidecar_identities(tmp_path):
    database_path = _database_with_sidecars(tmp_path)
    expected = database_sidecar_identities(database_path)
    original_state = _artifact_state(database_path)

    workers = (
        DatabaseLoadWorker(1, str(database_path)),
        DatabaseRefreshWorker(1, str(database_path), 0, []),
        DatabaseDetailWorker(1, str(database_path), [1]),
        DatabaseExpensiveDetailWorker(1, str(database_path), [1]),
    )

    assert _artifact_state(database_path) == original_state
    _replace_sidecars(database_path)

    assert expected
    assert database_sidecar_identities(database_path).isdisjoint(expected)
    assert all(worker.sidecar_identities == expected for worker in workers)


def test_preview_and_plot_workers_retain_sidecar_identities(tmp_path):
    database_path = _database_with_sidecars(tmp_path)
    expected = database_sidecar_identities(database_path)
    original_state = _artifact_state(database_path)

    class Dataset:
        path_to_db = str(database_path)
        table_name = "results"

    class Cache:
        _dataset = Dataset()

    class Parameter:
        name = "signal"

    preview_worker = PreviewWorker(
        1,
        str(database_path),
        "run-guid",
        {},
        100,
    )
    plot_worker = loader(Cache(), Parameter(), {}, {})

    assert _artifact_state(database_path) == original_state
    _replace_sidecars(database_path)

    assert expected
    assert database_sidecar_identities(database_path).isdisjoint(expected)
    assert preview_worker.sidecar_identities == expected
    assert plot_worker.sidecar_identities == expected
