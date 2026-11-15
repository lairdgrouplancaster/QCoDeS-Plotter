"""Shared fixtures and assertions for trusted plot."""

import hashlib
from pathlib import Path

import numpy as np
import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

import qplot.tools.worker as worker_module
from qplot.datahandling import readonly
from qplot.datahandling.qcodes_cache import update_cache_parameter_data

__all__ = [
    'make_run',
    'prohibit_snapshots',
    'protected_state',
    'work',
]


def make_run(path, *, arrays=False, dimensions=2):
    initialise_or_create_database_at(str(path), journal_mode="WAL")
    experiment = load_or_create_experiment("plots", sample_name="disposable")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("x")
    if dimensions == 2:
        measurement.register_custom_parameter("y", paramtype="array" if arrays else "numeric")
    measurement.register_custom_parameter(
        "z", setpoints=("x", "y") if dimensions == 2 else ("x",),
        paramtype="array" if arrays else "numeric",
    )
    with measurement.run() as saver:
        for x in range(3):
            if arrays:
                values = [("x", x), ("z", np.arange(4, dtype=np.int64) + 10 * x)]
                if dimensions == 2:
                    values.append(("y", np.arange(4, dtype=np.int64)))
                saver.add_result(*values)
            else:
                for y in range(4 if dimensions == 2 else 1):
                    values = [("x", x), ("z", 10 * x + y)]
                    if dimensions == 2:
                        values.append(("y", y))
                    saver.add_result(*values)
        dataset = saver.dataset
        identity = dataset.run_id, dataset.guid, dataset.table_name
    dataset.conn.close()
    experiment.conn.close()
    return identity


def prohibit_snapshots(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Trusted plotting attempted a whole-database snapshot")
    monkeypatch.setattr(readonly, "_copy_file_cooperatively", forbidden)
    monkeypatch.setattr(worker_module, "qcodes_read_only_connection", forbidden)
    monkeypatch.setattr(worker_module, "sqlite_read_only_connection", forbidden)


def work(dataset, *, limit=2_000_000):
    param = dataset.paramspecs["z"]
    axes = {"x": "y", "y": "x"} if len(param.depends_on_) == 2 else {"x": "x", "y": "z"}
    worker = worker_module.loader(dataset.cache, param, dataset.paramspecs, axes,
                    max_full_heatmap_points=limit)
    worker.setAutoDelete(False)
    errors, finished = [], []
    worker.emitter.errorOccurred.connect(errors.append)
    worker.emitter.finished.connect(finished.append)
    worker.run()
    assert not errors, errors
    assert finished == [True]
    if worker.read_data:
        assert update_cache_parameter_data(
            dataset.cache, "z", worker.updated_read_status, worker.updated_write_status,
            worker.cache_data, dataset_completed=worker.dataset_completed,
        )
    return worker


def protected_state(path):
    result = {}
    for suffix in ("", "-wal", "-journal"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            stat = candidate.stat()
            result[suffix] = (stat.st_size, stat.st_mtime_ns,
                              hashlib.sha256(candidate.read_bytes()).hexdigest())
    return result
