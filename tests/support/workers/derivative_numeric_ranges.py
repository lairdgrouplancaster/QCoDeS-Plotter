"""Shared fixtures and assertions for derivative numeric ranges."""

import hashlib

import numpy as np
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

__all__ = [
    '_measurement',
    '_source_state',
]


def _measurement(tmp_path, x, signal, *, heatmap=False, mixed=False, integer_first=None):
    path = tmp_path / "d.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    exp = load_or_create_experiment("numeric ranges", sample_name="test")
    measurement = Measurement(exp=exp)
    measurement.register_custom_parameter("x", paramtype="array")
    if heatmap:
        measurement.register_custom_parameter("fixed", paramtype="array")
    measurement.register_custom_parameter(
        "signal", setpoints=("fixed", "x") if heatmap else ("x",), paramtype="array",
    )
    writer = None
    try:
        with measurement.run() as saver:
            writer = saver.dataset
            for row in range(2 if heatmap else 1):
                for index, (coordinate, value) in enumerate(zip(x, signal, strict=True)):
                    # Individual array records intentionally retain mixed dtypes.
                    x_dtype = np.int64 if mixed and coordinate == 0 else np.float64
                    y_dtype = np.int64 if mixed and value == 0 else np.float64
                    if integer_first == "x":
                        x_dtype = np.int64 if index == 0 else np.float64
                    elif integer_first == "signal":
                        y_dtype = np.int64 if index == 0 else np.float64
                    values = [("x", np.array([coordinate], dtype=x_dtype)),
                              ("signal", np.array([value], dtype=y_dtype))]
                    if heatmap:
                        values.append(("fixed", np.array([row], dtype=np.int64)))
                    saver.add_result(*values)
            run_id = saver.dataset.run_id
    finally:
        if writer is not None:
            writer.conn.close()
        exp.conn.close()
    return path, run_id


def _source_state(path):
    files = [path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-journal")]
    return [(item.name, item.stat().st_size, item.stat().st_mtime_ns,
             hashlib.sha256(item.read_bytes()).digest()) if item.exists() else (item.name, None)
            for item in files]
