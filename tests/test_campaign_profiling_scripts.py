"""Profiling CLIs accept supported QCoDeS data and close real Qt workers."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def measured_database(tmp_path):
    path = (tmp_path / "profiling source.db").resolve()
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("profiling scripts", sample_name="test")
    try:
        for kind in ("line", "heatmap", "array", "mixed"):
            measurement = Measurement(exp=experiment, name=kind)
            measurement.register_custom_parameter("x", paramtype="array" if kind == "array" else "numeric")
            if kind == "heatmap":
                measurement.register_custom_parameter("slow")
            measurement.register_custom_parameter(
                "signal", paramtype="array" if kind in ("array", "mixed") else "numeric",
                setpoints=("slow", "x") if kind == "heatmap" else ("x",),
            )
            with measurement.run(write_in_background=False) as saver:
                if kind in ("array", "mixed"):
                    saver.add_result(("x", np.arange(3.) if kind == "array" else 2.),
                                     ("signal", np.arange(3.) ** 2))
                else:
                    for slow in range(2 if kind == "heatmap" else 1):
                        for x in range(3):
                            values = [("x", float(x)), ("signal", float(slow * 10 + x * x))]
                            if kind == "heatmap":
                                values.append(("slow", float(slow)))
                            saver.add_result(*values)
    finally:
        experiment.conn.close()
    yield path


def _state(path):
    return {
        suffix: (item.stat().st_size, item.stat().st_mtime_ns, hashlib.sha256(item.read_bytes()).digest())
        for suffix in ("", "-wal", "-journal")
        if (item := Path(str(path) + suffix)).exists()
    }


def _run(script, database, *arguments):
    before = _state(database)
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script), str(database), *map(str, arguments)],
        cwd=ROOT, env=dict(os.environ, QT_QPA_PLATFORM="offscreen"),
        capture_output=True, text=True, timeout=45,
    )
    assert _state(database) == before
    return result


@pytest.mark.parametrize("run_id", [1, 2])
def test_actual_plot_benchmark_reports_lines_and_heatmaps(measured_database, run_id):
    result = _run("benchmark_plot_launch.py", measured_database, run_id, "--repeats", 2)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["protected_file_stats_unchanged"]
    assert len(report["results"]) == 2
    for iteration in report["results"]:
        assert iteration["grids"] == ([[2, 3]] if run_id == 2 else [])
        assert iteration["line_samples"] == ([3] if run_id == 1 else [])
        assert iteration["paint_ms"]


def test_actual_plot_benchmark_failure_shuts_down_without_crashing(measured_database):
    result = _run("benchmark_plot_launch.py", measured_database, 999, "--repeats", 1)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "QThread: Destroyed" not in result.stderr
    assert "Fatal Python error" not in result.stderr


@pytest.mark.parametrize("run_id,columns,expected", [
    (1, ["x", "signal"], [3, 0., 2., 0., 4.]),
    (4, ["x"], [1, 2., 2.]),
])
def test_actual_numeric_read_profiler_uses_declared_numeric_columns(measured_database, run_id, columns, expected):
    result = _run("profile_trusted_reads.py", measured_database, run_id, "--pages", 1, "--repeats", 2)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["numeric_parameters"] == columns
    assert report["protected_file_stats_unchanged"]
    assert len(report["results"]) == 2
    for iteration in report["results"]:
        assert iteration["first_page"] == [expected]


def test_actual_array_only_read_profiler_explains_numeric_limit(measured_database):
    result = _run("profile_trusted_reads.py", measured_database, 3, "--pages", 1, "--repeats", 1)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "no scalar numeric parameters" in result.stderr
    assert "Traceback" not in result.stderr
