"""Shared fixtures and assertions for bounded heatmap means."""

from contextlib import contextmanager

import numpy as np
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.datahandling.readonly import load_by_id_read_only
from tests.support.windows.complex_line_data import database_state

__all__ = [
    'finite_surface',
]


@contextmanager
def finite_surface(tmp_path, values, arrays):
    path = tmp_path / "finite-means.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("finite spatial means", "test")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("slow")
    measurement.register_custom_parameter("fast", paramtype="array" if arrays else "numeric")
    measurement.register_custom_parameter("signal", paramtype="array" if arrays else "numeric",
                                          setpoints=("slow", "fast"))
    try:
        with measurement.run(write_in_background=False) as saver:
            for slow, row in ((0, values), (1, np.full(len(values), 1e-308))):
                if arrays:
                    saver.add_result(("slow", slow), ("fast", np.arange(len(row))), ("signal", row))
                else:
                    for fast, value in enumerate(row):
                        saver.add_result(("slow", slow), ("fast", fast), ("signal", value))
            run_id = saver.dataset.run_id
    finally:
        experiment.conn.close()
    protected = database_state(path)
    dataset = load_by_id_read_only(run_id, str(path))
    try:
        yield dataset
    finally:
        dataset.conn.close()
        assert database_state(path) == protected
