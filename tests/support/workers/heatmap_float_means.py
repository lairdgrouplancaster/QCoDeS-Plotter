"""Shared fixtures and assertions for heatmap float means."""

from contextlib import contextmanager

import numpy as np
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.datahandling.readonly import load_by_id_read_only

__all__ = [
    'repeated_dataset',
]


@contextmanager
def repeated_dataset(tmp_path, values, *, shaped):
    path = tmp_path / "repeated-means.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("repeated means", sample_name="test")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("slow")
    measurement.register_custom_parameter("fast", paramtype="array")
    measurement.register_custom_parameter(
        "signal", paramtype="array", setpoints=("slow", "fast")
    )
    if shaped:
        measurement.set_shapes({"signal": (2, 4)})
    try:
        with measurement.run(write_in_background=False) as saver:
            for slow in (0, 1):
                saver.add_result(
                    ("slow", slow), ("fast", np.zeros(len(values))), ("signal", values)
                )
        run_id = saver.dataset.run_id
    finally:
        experiment.conn.close()
    protected = {
        suffix: (
            (
                path.with_name(path.name + suffix).read_bytes(),
                path.with_name(path.name + suffix).stat().st_mtime_ns,
            )
            if path.with_name(path.name + suffix).exists()
            else None
        )
        for suffix in ("", "-wal", "-journal")
    }
    dataset = load_by_id_read_only(run_id, str(path))
    try:
        yield dataset
    finally:
        dataset.conn.close()
        for suffix, before in protected.items():
            artifact = path.with_name(path.name + suffix)
            after = (
                (artifact.read_bytes(), artifact.stat().st_mtime_ns)
                if artifact.exists()
                else None
            )
            assert after == before
