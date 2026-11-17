"""Synthetic preview source and exact protected-file observations."""

from pathlib import Path

import numpy as np

from qplot.datahandling.readonly import sqlite_read_only_connection
from qplot.testdata import (
    AxisSpecification,
    MeasurementSpecification,
    RunSpecification,
    generate_database,
)

_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def _preview_database(path: Path, seed: int):
    specification = RunSpecification(
        (
            MeasurementSpecification(
                "current",
                "Current",
                "nA",
                (AxisSpecification("V_SD", "Source-drain voltage", "V", -1.0, 1.0, 5),),
            ),
        )
    )
    generate_database(
        [specification],
        path,
        rng=np.random.default_rng(seed),
    )

    connection = sqlite_read_only_connection(path)
    try:
        run_id, guid, table_name, run_description = connection.execute(
            "SELECT run_id, guid, result_table_name, run_description FROM runs"
        ).fetchone()
        columns = tuple(
            row[1]
            for row in connection.execute(
                f'PRAGMA table_info("{table_name}")'
            )
        )
        axis = next(column for column in columns if column.lower() == "v_sd")
        values = tuple(
            row[0]
            for row in connection.execute(
                f'SELECT current FROM "{table_name}" ORDER BY id'
            )
        )
    finally:
        connection.close()

    metadata = {
        "run_id": run_id,
        "guid": guid,
        "result_table_name": table_name,
        "result_count": len(values),
        "run_description": run_description,
        "measure_parameters": ["current"],
        "sweep_parameters": [axis],
    }
    return metadata, columns, values


def _artifact_state(database_path: Path):
    state = {}
    for suffix in ("", *_SQLITE_SIDECAR_SUFFIXES):
        artifact_path = Path(f"{database_path}{suffix}")
        if not artifact_path.exists():
            state[suffix] = None
            continue
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


