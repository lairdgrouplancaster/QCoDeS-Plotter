"""Benchmark large-heatmap spatial aggregation with 2,000,000 rows."""

import sqlite3
import tempfile
from pathlib import Path
from time import perf_counter

import numpy as np

from qplot.tools.worker import MAX_SQL_HEATMAP_GRID_CELLS, loader
from qplot.windows._widgets.preview import generate_run_previews

BENCHMARK_COLUMNS = 2_000
BENCHMARK_ROWS = 2_000_000


class _Parameter:
    def __init__(self, name):
        self.name = name
        self.depends_on_ = ("y", "x") if name == "signal" else ()
        self._complete = False


class _Dataset:
    def __init__(self, database_path):
        self.path_to_db = str(database_path)
        self.table_name = "results"


class _Rundescriber:
    shapes = {"signal": (BENCHMARK_ROWS // BENCHMARK_COLUMNS, BENCHMARK_COLUMNS)}


class _Cache:
    def __init__(self, database_path):
        self._dataset = _Dataset(database_path)
        self.rundescriber = _Rundescriber()


def _create_database(database_path):
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute(
            "CREATE TABLE results (x REAL, y REAL, signal REAL)"
            )
        connection.executemany(
            "INSERT INTO results (x, y, signal) VALUES (?, ?, ?)",
            (
                (
                    float(index % BENCHMARK_COLUMNS),
                    float(index // BENCHMARK_COLUMNS),
                    float(index % BENCHMARK_COLUMNS)
                    + float(index // BENCHMARK_COLUMNS) / 100,
                    )
                for index in range(BENCHMARK_ROWS)
                ),
            )
        connection.commit()
    finally:
        connection.close()


def _benchmark(database_path):
    signal = _Parameter("signal")
    parameters = {
        "x": _Parameter("x"),
        "y": _Parameter("y"),
        "signal": signal,
        }
    worker = loader(
        _Cache(database_path),
        signal,
        parameters,
        {"x": "x", "y": "y"},
        force_sql_heatmap=True,
        )

    started = perf_counter()
    worker._load_large_heatmap_from_sql()
    elapsed = perf_counter() - started

    assert worker.aggregated_heatmap_source
    assert worker.heatmap_downsample_info["aggregated_source_row_count"] == (
        BENCHMARK_ROWS
        )
    assert worker.dataGrid.size <= MAX_SQL_HEATMAP_GRID_CELLS
    assert np.isfinite(worker.dataGrid).all()
    return worker, elapsed


def _benchmark_preview(database_path):
    metadata = {
        "result_table_name": "results",
        "result_count": BENCHMARK_ROWS,
        "setpoint_shape": [
            BENCHMARK_ROWS // BENCHMARK_COLUMNS,
            BENCHMARK_COLUMNS,
            ],
        "measure_parameters": ["signal"],
        "sweep_parameters": ["y", "x"],
        }
    started = perf_counter()
    previews = generate_run_previews(str(database_path), metadata, size=200)
    elapsed = perf_counter() - started

    assert len(previews) == 1
    assert previews[0]["downsample_strategy"] == "spatial mean"
    assert previews[0]["image"].width() == 200
    assert previews[0]["image"].height() == 200
    return elapsed


def main():
    with tempfile.TemporaryDirectory(prefix="qplot-heatmap-benchmark-") as tmpdir:
        database_path = Path(tmpdir) / "heatmap.db"
        setup_started = perf_counter()
        _create_database(database_path)
        setup_elapsed = perf_counter() - setup_started

        worker, aggregation_elapsed = _benchmark(database_path)
        preview_elapsed = _benchmark_preview(database_path)
        info = worker.heatmap_downsample_info
        print(f"Database rows: {BENCHMARK_ROWS:,}")
        print(f"Database creation: {setup_elapsed:.3f} s")
        print(
            "Aggregated grid: "
            f"{info['grid_columns']:,} x {info['grid_rows']:,} "
            f"= {info['grid_cell_count']:,} cells"
        )
        print(f"Spatial aggregation load: {aggregation_elapsed:.3f} s")
        print(f"Spatial-mean preview generation: {preview_elapsed:.3f} s")


if __name__ == "__main__":
    main()
