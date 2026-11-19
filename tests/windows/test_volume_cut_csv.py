"""Exports from live volume cuts describe the measurements in the CSV."""

import csv

import numpy as np
from PyQt6 import QtWidgets
from pyqtgraph.exporters import CSVExporter

from qplot.windows._plot_export import CSV_TRANSFORMED_VALUES
from tests.support.windows.native_transform_labels import click_control
from tests.support.windows.plot3d import build_volume
from tests.support.windows.plot3d import main as main
from tests.support.windows.plot_integration import (
    assert_database_artifacts_unchanged_except_trusted_shm,
    trusted_database_artifact_state,
    wait_for,
)
from tests.support.windows.svg_export_integration import select_exporter


def export_csv(cut, path, monkeypatch, *, transformed):
    dialog = select_exporter(cut, CSVExporter)
    if transformed:
        dialog.currentExporter.params["values"] = CSV_TRANSFORMED_VALUES
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                        lambda *_args, **_kwargs: (str(path), ""))
    dialog.ui.exportBtn.click()
    wait_for(lambda: path.exists() and not cut._export_tasks)
    dialog.close()
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def test_phase_map_cut_csv_units_follow_the_chosen_values(main, tmp_path, monkeypatch):
    window, errors = main
    path = tmp_path / "volume.db"
    guid = build_volume(path)
    protected = trusted_database_artifact_state(path)
    assert window.load_file(str(path))
    wait_for(lambda: not window._database_load_active)
    window.openPlot(guid)
    wait_for(lambda: bool(window.windows) and window.windows[0]._qplot_display_synchronized)
    volume = window.windows[0]
    volume.show_hover_pixel_outline(1, 0)
    next(action for action in volume.vbMenu.actions() if action.text() == "Horizontal Cut").trigger()
    wait_for(lambda: len(window.windows) == 2 and window.windows[-1]._qplot_display_synchronized)
    cut = window.windows[-1]
    click_control(cut, "phasemapCheck")
    assert cut.line.opts["phasemapMode"]

    # At field=1 and bias=0 the writer measured 100 + gate**2 A.
    # The phase map plots current against its slope between successive gates.
    for mode, expected, units in [
        ("underlying", [(-2, 104), (0, 100), (5, 125)], ("V", "A")),
        ("transformed", [(104, -2), (100, 5)], ("A", "A/V")),
    ]:
        rows = export_csv(cut, tmp_path / f"{mode}.csv", monkeypatch, transformed=mode == "transformed")
        x, y = tuple(rows[0])[:2]
        np.testing.assert_allclose([(float(row[x]), float(row[y])) for row in rows], expected)
        assert {row["run_guid"] for row in rows} == {guid}
        assert {(row["x_unit"], row["value_unit"]) for row in rows} == {units}
        if mode == "transformed":
            assert x.endswith("[A]") and y.endswith("[A/V]")
    assert_database_artifacts_unchanged_except_trusted_shm(path, protected)
    assert not errors
