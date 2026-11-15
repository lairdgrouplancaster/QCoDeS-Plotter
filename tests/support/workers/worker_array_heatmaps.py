"""Shared fixtures and assertions for worker array heatmaps."""

import hashlib
from contextlib import contextmanager

import numpy as np
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

import qplot.tools.worker as worker_module
from qplot.datahandling.readonly import load_by_id_read_only

__all__ = [
    'heatmap_dataset',
    'make_worker',
    'run_worker',
]


@contextmanager
def heatmap_dataset(
    tmp_path,
    *,
    arrays=True,
    shaped=True,
    records=None,
    scalar_fast=False,
    planned_shape=None,
):
    path = tmp_path / "array_heatmap.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("array_heatmap", sample_name="test")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("slow")
    measurement.register_custom_parameter(
        "fast", paramtype="array" if arrays and not scalar_fast else "numeric",
    )
    measurement.register_custom_parameter(
        "signal", setpoints=("slow", "fast"),
        paramtype="array" if arrays else "numeric",
    )
    if shaped:
        measurement.set_shapes({"signal": planned_shape or (2, 3)})
    if records is None:
        # Reverse the second sweep so independently sorting an array is wrong.
        records = [(0, [0, 1, 2], [0, 1, 2]), (1, [2, 1, 0], [12, 11, 10])]
    with measurement.run() as datasaver:
        for slow, fast, signal in records:
            if arrays:
                datasaver.add_result(
                    ("slow", slow), ("fast", np.asarray(fast)),
                    ("signal", np.asarray(signal)),
                )
            else:
                for x, z in zip(fast, signal, strict=True):
                    datasaver.add_result(("slow", slow), ("fast", x), ("signal", z))
        run_id = datasaver.dataset.run_id
    before = hashlib.sha256(path.read_bytes()).digest()
    dataset = load_by_id_read_only(run_id, str(path))
    try:
        yield dataset
    finally:
        dataset.conn.close()
        assert hashlib.sha256(path.read_bytes()).digest() == before
        assert not path.with_name(path.name + "-journal").exists()
        assert not path.with_name(path.name + "-wal").exists()


def make_worker(dataset, **kwargs):
    params = {param.name: param for param in dataset.get_parameters()}
    return worker_module.loader(
        dataset.cache, params["signal"], params, {"x": "fast", "y": "slow"},
        **kwargs,
    )


def run_worker(worker):
    finished, errors = [], []
    worker.emitter.finished.connect(finished.append)
    worker.emitter.errorOccurred.connect(errors.append)
    worker.run()
    assert errors == []
    assert finished == [True]
