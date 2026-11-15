"""Shared fixtures and assertions for high dimensional setpoints."""

import itertools
import sqlite3
from dataclasses import dataclass

import numpy as np
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.datahandling.trusted_live import TrustedQueryResult

__all__ = [
    '_ReadOnlySqliteExecutor',
    '_database',
    '_load_basic_runs',
]


class _ReadOnlySqliteExecutor:
    incarnation = 1

    def __init__(self, path):
        self.connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        self.queries = []

    def query(self, sql, bindings=None, **kwargs):
        self.queries.append(sql)
        cursor = self.connection.execute(sql, bindings or ())
        return TrustedQueryResult(
            tuple(c[0] for c in cursor.description), tuple(cursor.fetchall()),
        )

    def query_batch(self, statements, **kwargs):
        self.connection.execute("BEGIN")
        try:
            return tuple(self.query(q.sql, q.bindings) for q in statements)
        finally:
            self.connection.rollback()

    def data_version(self, **kwargs):
        return self.connection.execute("PRAGMA data_version").fetchone()[0]

    def close(self):
        self.connection.close()


def _load_basic_runs(adapter):
    bootstrap = adapter.bootstrap()
    adapter.basic_run_page(0, bootstrap.run_id_watermark)


@dataclass
class _Axis:
    name: str
    points: int


def _database(tmp_path, dimensions, mixed=False, descending=False, planned=False,
              acquired=None):
    axes = tuple(_Axis(f"a{i}", i + 2) for i in range(dimensions))
    path = tmp_path / "nd.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("nD counts", "test")
    measurement = Measurement(exp=experiment)
    for axis in axes:
        measurement.register_custom_parameter(axis.name)
    sizes = range(1, dimensions + 1) if mixed else (dimensions,)
    for n in sizes:
        measurement.register_custom_parameter(
            f"signal_{n}", setpoints=tuple(a.name for a in axes[:n]),
        )
    if planned:
        measurement.set_shapes({f"signal_{n}": tuple(a.points for a in axes[:n])
                                for n in sizes})
    try:
        with measurement.run(write_in_background=False) as saver:
            for n in sizes:
                grid = np.array(list(itertools.product(*(
                    np.linspace(1 if descending else 0, 0 if descending else 1, a.points)
                    for a in axes[:n]
                ))))
                if acquired is not None:
                    grid = grid[:acquired]
                saver.add_result(
                    *((axis.name, grid[:, i]) for i, axis in enumerate(axes[:n])),
                    (f"signal_{n}", np.arange(len(grid), dtype=float)),
                )
    finally:
        saver.dataset.conn.close()
        experiment.conn.close()
    return path, axes
