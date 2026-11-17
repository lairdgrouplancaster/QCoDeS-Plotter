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

ROOT = Path(__file__).resolve().parents[2]


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
        cwd=ROOT, env=dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONFAULTHANDLER="1"),
        capture_output=True, text=True, timeout=45,
    )
    assert _state(database) == before
    return result


@pytest.mark.parametrize("run_id", [1, 2])
def test_actual_plot_benchmark_reports_lines_and_heatmaps(measured_database, run_id):
    result = _run("benchmark_plot_launch.py", measured_database, run_id, "--repeats", 2)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["protected_files_unchanged"]
    assert len(report["results"]) == 2
    for iteration in report["results"]:
        assert iteration["grids"] == ([[2, 3]] if run_id == 2 else [])
        assert iteration["line_samples"] == ([3] if run_id == 1 else [])
        assert iteration["paint_ms"]


@pytest.mark.parametrize("action", ["guid", "double-click", "run"])
def test_plot_benchmark_repeated_preceding_run_accepts_cached_completion(
    measured_database, action,
):
    result = _run(
        "benchmark_plot_launch.py", measured_database, 1,
        "--repeats", 2, "--preceding-run", 2, "--action", action,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["protected_files_unchanged"]
    assert len(report["results"]) == 2
    for iteration in report["results"]:
        assert iteration["line_samples"] == [3]
        assert iteration["paint_ms"]
        assert any(capture["run_id"] == 2 for capture in iteration["capture"])
    # The second completed-run capture legitimately reuses its immutable
    # prefix without yielding a page; that must not strand the launch gate.
    assert report["results"][1]["preceding_capture_steps"] == [0, 0]


@pytest.mark.parametrize("stage", ["aggregate", "large-summary"])
def test_plot_benchmark_explains_unreachable_preceding_stage(
    measured_database, stage,
):
    result = _run(
        "benchmark_plot_launch.py", measured_database, 1,
        "--repeats", 1, "--preceding-run", 2, "--preceding-stage", stage,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"Preceding run 2 completed without reaching the {stage} stage" in result.stderr
    assert "QThread: Destroyed" not in result.stderr
    assert "Fatal Python error" not in result.stderr


def test_actual_plot_benchmark_failure_shuts_down_without_crashing(measured_database):
    result = _run("benchmark_plot_launch.py", measured_database, 999, "--repeats", 1)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "QThread: Destroyed" not in result.stderr
    assert "Fatal Python error" not in result.stderr


@pytest.mark.parametrize("run_id,fail_paint", [(1, False), (2, False), (999, False), (1, True)])
def test_plot_benchmark_retires_owned_qt_objects_before_return(
    measured_database, tmp_path, run_id, fail_paint,
):
    """Destroy held plot owners before QApplication's unordered wrapper sweep."""
    probe = tmp_path / "observe_cleanup.py"
    state_path = tmp_path / "cleanup.json"
    probe.write_text("""
import json
import runpy
import sys
from pathlib import Path
from PyQt6 import QtWidgets, sip
from qplot.windows.main import MainWindow
from qplot.windows._plotWin import plotWidget

owned = []
applications = []
before_application_delete = []
for kind in (MainWindow, plotWidget):
    original = kind.__init__
    def observed_init(self, *args, original=original, **kwargs):
        original(self, *args, **kwargs)
        owned.append(self)
        applications.append(QtWidgets.QApplication.instance())
    kind.__init__ = observed_init

original_delete = sip.delete
def observed_delete(item):
    if isinstance(item, QtWidgets.QApplication):
        before_application_delete.append({
            "objects_deleted": [sip.isdeleted(owner) for owner in owned],
            "plot_windows": sum(isinstance(widget, plotWidget) for widget in item.topLevelWidgets()),
        })
    original_delete(item)
sip.delete = observed_delete

if FAIL_PAINT:
    def failed_grab(self, *args, **kwargs):
        raise RuntimeError("Deliberate plot paint failure")
    plotWidget.grab = failed_grab

try:
    sys.path.insert(0, str(Path(SCRIPT).parent))
    runpy.run_path(SCRIPT, run_name="__main__")
finally:
    Path(STATE).write_text(json.dumps({
        "owned_types": [type(item).__name__ for item in owned],
        "objects_deleted": [sip.isdeleted(item) for item in owned],
        "applications_deleted": [sip.isdeleted(item) for item in applications],
        "application_absent": QtWidgets.QApplication.instance() is None,
        "before_application_delete": before_application_delete,
    }))
""".replace("SCRIPT", repr(str(ROOT / "scripts/benchmark_plot_launch.py")))
        .replace("STATE", repr(str(state_path)))
        .replace("FAIL_PAINT", repr(fail_paint)))
    result = _run(probe, measured_database, run_id, "--repeats", 2)
    assert result.returncode == (1 if run_id == 999 or fail_paint else 0), result.stdout + result.stderr
    if fail_paint:
        assert "Deliberate plot paint failure" in result.stderr
    cleanup = json.loads(state_path.read_text())
    assert cleanup["owned_types"].count("MainWindow") == 1
    assert cleanup["owned_types"].count("plot1d" if run_id == 1 else "plot2d") == (
        0 if run_id == 999 else 1 if fail_paint else 2
    )
    assert all(cleanup["objects_deleted"]), cleanup
    assert all(cleanup["applications_deleted"]), cleanup
    assert cleanup["application_absent"], cleanup
    assert len(cleanup["before_application_delete"]) == 1, cleanup
    before_delete = cleanup["before_application_delete"][0]
    assert all(before_delete["objects_deleted"]), cleanup
    assert before_delete["plot_windows"] == 0, cleanup


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



def test_workflow_benchmark_refreshes_selects_and_records_resource_samples(measured_database, tmp_path):
    output = tmp_path / "workflow.json"
    result = _run("benchmark_plot_launch.py", measured_database, 2,
                  "--repeats", 3, "--refreshes", 2, "--selection-rounds", 2,
                  "--output", output)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(output.read_text())
    assert report == json.loads(result.stdout)
    assert report["protected_files_unchanged"]
    assert report["source_before"][""]["sha256"] == hashlib.sha256(measured_database.read_bytes()).hexdigest()
    assert report["refresh_summary"]["count"] == 6
    assert report["event_loop_delay"]["count"] > 0
    for cycle in report["results"]:
        assert len(cycle["refresh_ms"]) == 2
        assert cycle["active_plot_workers"] == 0
        assert cycle["memory_after_close"]["rss_bytes"] > 0
        assert cycle["grids"] == [[2, 3]]


def test_workflow_report_does_not_replace_existing_file(measured_database, tmp_path):
    output = tmp_path / "existing.json"
    output.write_bytes(b"original")
    result = _run("benchmark_plot_launch.py", measured_database, 1, "--output", output)
    assert result.returncode == 2
    assert "output already exists" in result.stderr
    assert output.read_bytes() == b"original"


def test_benchmark_fixture_is_qcodes_and_refuses_replacement(tmp_path):
    path = tmp_path / "workload.db"
    command = [sys.executable, str(ROOT / "scripts/create_benchmark_database.py"),
               str(path), "--heatmap-side", "4", "--runs", "2"]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    before = _state(path)
    from qplot.datahandling.readonly import sqlite_read_only_connection
    with sqlite_read_only_connection(path) as connection:
        assert connection.execute("SELECT name FROM runs ORDER BY run_id").fetchall() == [
            ("line",), ("heatmap",),
        ]
    connection.close()
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=45)
    assert result.returncode != 0
    assert "already exists" in result.stderr
    assert _state(path) == before
