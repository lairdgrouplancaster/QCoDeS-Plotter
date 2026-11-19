"""Exercise a real window, depth editor and exported volume planes on each backend."""
import csv
import inspect
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

import numpy as np
from PyQt6 import QtCore, QtTest, QtWidgets, sip
from pyqtgraph.exporters import CSVExporter
from qcodes.dataset import Measurement, initialise_or_create_database_at, load_or_create_experiment

from qplot.windows.main import MainWindow
from tests.support.windows.plot3d import main as main
from tests.support.windows.plot_integration import (
    assert_database_artifacts_unchanged_except_trusted_shm, trusted_database_artifact_state, wait_for,
)
from tests.support.windows.svg_export_integration import select_exporter


def test_typed_depth_and_home_publish_the_selected_plane(main, qapplication, monkeypatch, tmp_path):
    out = Path(os.environ["HUNT_OUTPUT"])
    out.mkdir(parents=True, exist_ok=True)
    source = Path.cwd().resolve()
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    assert head == os.environ["HUNT_SHA"]
    backend = qapplication.platformName()
    assert backend == os.environ["QT_QPA_PLATFORM"]
    if backend == "cocoa":
        assert platform.machine().lower() == "arm64"
    assert Path(inspect.getfile(MainWindow)).resolve().is_relative_to(source / "src")
    window, errors = main
    window.show()
    window.raise_()
    assert QtTest.QTest.qWaitForWindowExposed(window, 30_000)
    path = (tmp_path / "typed_depth.db").resolve()
    initialise_or_create_database_at(str(path), journal_mode="WAL")
    experiment = load_or_create_experiment("Irregular planes", "native edit")
    measurement = Measurement(exp=experiment)
    for axis, unit in (("depth", "T"), ("bias", "mV"), ("delay", "ns")):
        measurement.register_custom_parameter(axis, unit=unit)
    measurement.register_custom_parameter("signal", unit="nA", setpoints=("depth", "bias", "delay"))
    depths, biases, delays = (11, -4, 3), (2, -1), (8, -5, 0)
    inputs = {}
    with measurement.run(write_in_background=False) as saver:
        for i, depth in enumerate(depths):
            for j, bias in enumerate(biases):
                for delay in (delays if (i+j) % 2 else reversed(delays)):
                    value = 37 * depth - 19 * bias + delay**2 + 2 * delay
                    inputs[(depth, bias, delay)] = value
                    saver.add_result(("depth", depth), ("bias", bias), ("delay", delay), ("signal", value))
        guid = saver.dataset.guid
    saver.dataset.conn.close()
    experiment.conn.close()
    initial = trusted_database_artifact_state(path)
    assert window.load_file(str(path))
    wait_for(lambda: (window._selected_run_guid == guid and window.plotRunButton.isEnabled()) or errors, 100)
    assert not errors
    QtTest.QTest.mouseClick(window.plotRunButton, QtCore.Qt.MouseButton.LeftButton)
    wait_for(lambda: (window.windows and window.windows[-1]._qplot_display_synchronized) or errors, 100)
    assert not errors
    plot = window.windows[-1]
    worker = Path(inspect.getfile(type(plot.worker))).resolve()
    assert worker.is_relative_to(source / "src"), worker
    assert QtTest.QTest.qWaitForWindowExposed(plot, 30_000)
    receipt = dict(head=head, platform=backend, machine=platform.machine(), interpreter=sys.executable,
                   main=inspect.getfile(MainWindow), worker=str(worker), guid=guid, planes=[])

    def check(depth):
        wait_for(lambda: plot._qplot_display_synchronized and plot.slice_frame.depth == depth, 90)
        assert plot.depth_entry.text() == str(depth)
        expected = [[inputs[depth, b, d] for d in sorted(delays)] for b in sorted(biases)]
        np.testing.assert_array_equal(plot.dataGrid, expected)
        np.testing.assert_array_equal(plot.axis_data["x"], sorted(delays))
        np.testing.assert_array_equal(plot.axis_data["y"], sorted(biases))
        export = select_exporter(plot, CSVExporter)
        assert QtTest.QTest.qWaitForWindowExposed(export, 30_000)
        dest = out / f"plane-{depth}.csv"
        monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName", lambda *a, **k: (str(dest), ""))
        QtTest.QTest.mouseClick(export.ui.exportBtn, QtCore.Qt.MouseButton.LeftButton)
        wait_for(lambda: dest.is_file() and not plot._export_tasks, 60)
        export.close()
        with dest.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == len(biases)*len(delays)
        assert [(float(r["bias"]), float(r["delay"]), float(r["signal"])) for r in rows] == [
            (b, d, inputs[depth, b, d]) for b in sorted(biases) for d in sorted(delays)
        ]
        for r in rows:
            assert (r["run_guid"], float(r["depth (T)"]), r["x_unit"], r["y_unit"], r["value_unit"]) == (
                guid, depth, "ns", "mV", "nA")
            assert Path(r["database"]).resolve() == path
        assert plot.grab().save(str(out / f"plane-{depth}.png"))
        receipt["planes"].append(dict(depth=depth, grid=expected, rows=len(rows)))

    check(-4)
    QtTest.QTest.mouseClick(plot.depth_entry, QtCore.Qt.MouseButton.LeftButton)
    plot.depth_entry.selectAll()
    QtTest.QTest.keyClicks(plot.depth_entry, "9.8")
    QtTest.QTest.keyClick(plot.depth_entry, QtCore.Qt.Key.Key_Return)
    check(11)
    plot.raise_()
    plot.activateWindow()
    plot.widget.setFocus()
    wait_for(lambda: plot.isActiveWindow() and plot.widget.hasFocus(), 25)
    QtTest.QTest.keyClick(plot.widget, QtCore.Qt.Key.Key_Home)
    wait_for(lambda: plot._qplot_display_synchronized and plot.slice_frame.depth == -4, 90)
    np.testing.assert_array_equal(plot.dataGrid, receipt["planes"][0]["grid"])
    plot.close()
    def retired():
        qapplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        return sip.isdeleted(plot)
    wait_for(retired, 60)
    window.close_database(status=False)
    wait_for(lambda: not window._shutdown_background_work_active(), 60)
    assert not errors
    assert_database_artifacts_unchanged_except_trusted_shm(path, initial)
    receipt["returned_home"] = receipt["retired"] = True
    (out / "native.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(json.dumps(receipt), flush=True)
