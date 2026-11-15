"""Shared fixtures and assertions for worker coordinate precision."""

from contextlib import contextmanager

import numpy as np
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

import qplot.tools.worker as worker_module
from qplot.datahandling.readonly import load_by_id_read_only
from tests.support.windows.complex_line_data import database_state

__all__ = [
    'coordinate_dataset',
    'coordinate_worker',
    'run_coordinate_worker',
]


@contextmanager
def coordinate_dataset(tmp_path, *, coordinates, shaped=False, split=False, record_dtypes=None):
    path = tmp_path / "coordinate-precision.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("coordinate precision", "test")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("slow", paramtype="array")
    measurement.register_custom_parameter("fast", paramtype="array")
    measurement.register_custom_parameter("signal", paramtype="array", setpoints=("slow", "fast"))
    if shaped:
        measurement.set_shapes({"signal": (2, 3)})
    try:
        with measurement.run(write_in_background=False, in_memory_cache=False) as saver:
            for row in range(2):
                fast = np.asarray(coordinates)
                slow = np.full(3, row)
                signal = np.array([1, 3, 10]) + 20 * row
                for index, selection in enumerate([slice(0, 1), slice(1, 3)] if split else [slice(None)]):
                    part = fast[selection]
                    if record_dtypes is not None:
                        part = part.astype(record_dtypes[index])
                    saver.add_result(("fast", part), ("slow", slow[selection]),
                                     ("signal", signal[selection]))
            run_id = saver.dataset.run_id
    finally:
        saver.dataset.conn.close()
        experiment.conn.close()
    protected = database_state(path)
    dataset = load_by_id_read_only(run_id, str(path))
    try:
        yield dataset
    finally:
        dataset.conn.close()
        assert database_state(path) == protected


def coordinate_worker(dataset, *, swapped=False, **kwargs):
    parameters = {parameter.name: parameter for parameter in dataset.get_parameters()}
    return worker_module.loader(dataset.cache, parameters["signal"], parameters,
                  {"x": "slow" if swapped else "fast", "y": "fast" if swapped else "slow"},
                  **kwargs)


def run_coordinate_worker(worker):
    finished, errors = [], []
    worker.emitter.finished.connect(finished.append)
    worker.emitter.errorOccurred.connect(errors.append)
    worker.run()
    return finished, errors
