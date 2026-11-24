"""Exports from live volume cuts describe the measurements in the CSV."""

import csv

import numpy as np
from PyQt6 import QtCore, QtWidgets
from pyqtgraph.exporters import CSVExporter
from qcodes.dataset import Measurement, load_or_create_experiment

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


def export_csv(cut, path, monkeypatch, *, transformed=False, selected_guid=None):
    dialog = select_exporter(cut, CSVExporter)
    if selected_guid is not None:
        choices = dialog.currentExporter._qplot_csv_measurements
        token = next(token for token, entry in choices.items() if entry.source._dataset_key.guid == selected_guid)
        combo = dialog.controls.fields["measurements"]
        combo.setCurrentIndex(combo.findData(token))
    elif len(cut.lines) > 1:
        combo = dialog.controls.fields["measurements"]
        combo.setCurrentIndex(combo.findData("all_traces"))
    if transformed:
        dialog.currentExporter.params["values"] = CSV_TRANSFORMED_VALUES
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                        lambda *_args, **_kwargs: (str(path), ""))
    dialog.ui.exportBtn.click()
    wait_for(lambda: path.exists() and not cut._export_tasks)
    dialog.close()
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def open_volume(window, path, guid):
    assert window.load_file(str(path))
    wait_for(lambda: not window._database_load_active)
    window.openPlot(guid)
    wait_for(lambda: bool(window.windows) and window.windows[0]._qplot_display_synchronized)
    return window.windows[0]


def test_phase_map_cut_csv_units_follow_the_chosen_values(main, tmp_path, monkeypatch):
    window, errors = main
    path = tmp_path / "volume.db"
    guid = build_volume(path)
    protected = trusted_database_artifact_state(path)
    volume = open_volume(window, path, guid)
    volume.show_hover_pixel_outline(1, 0)
    next(action for action in volume.vbMenu.actions() if action.text() == "Horizontal cut").trigger()
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


def test_mean_cut_csv_provenance_applies_only_to_its_own_trace(main, tmp_path, monkeypatch):
    window, errors = main
    path = tmp_path / "comparison.db"
    guid = build_volume(path)
    depths, reference = [1, 2.5, 4], [-13, 490, -64]
    experiment = load_or_create_experiment("comparison", "reference trace")
    try:
        measurement = Measurement(exp=experiment)
        measurement.register_custom_parameter("field", unit="V")
        measurement.register_custom_parameter("comparison", unit="A", setpoints=("field",))
        with measurement.run(write_in_background=False) as saver:
            for x, y in zip(depths, reference, strict=True):
                saver.add_result(("field", x), ("comparison", y))
        comparison_guid = saver.dataset.guid
        saver.dataset.conn.close()
    finally:
        experiment.conn.close()

    protected = trusted_database_artifact_state(path)
    volume = open_volume(window, path, guid)
    volume.set_marquee_rect(QtCore.QRectF(-3, -1, 4, 2))
    menu = volume._new_marquee_context_menu()
    next(action for action in menu.actions() if action.text() == "Mean within selection versus depth").trigger()
    menu.deleteLater()
    wait_for(lambda: len(window.windows) == 2 and window.windows[-1]._qplot_display_synchronized)
    cut = window.windows[-1]
    cut.appearance_action.trigger()
    appearance = cut._plot_appearance_dialog
    combo = appearance.add_item_combo
    index = next(i for i in range(combo.count()) if (entry := combo.itemData(i)) is not None
                 and entry[0] == "trace" and entry[1].dataset_key.guid == comparison_guid)
    combo.setCurrentIndex(index)
    appearance.add_item_button.click()
    wait_for(lambda: len(cut.lines) == 2 and all(line.getData()[0] is not None for line in cut.lines.values()))
    appearance.appearance_buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).click()

    # The two measured fields coincide. The region covers gate=-2 and 0 at
    # bias=0, giving mean(100*field + gate**2) = 100*field + 2 A.
    primary = np.column_stack((depths, [102, 252, 402]))
    secondary = np.column_stack((depths, reference))
    for selected, expected in [(guid, primary), (comparison_guid, secondary), (None, np.column_stack((primary, secondary)))]:
        for transformed in (False, True):
            rows = export_csv(cut, tmp_path / f"{selected}-{transformed}.csv", monkeypatch,
                              transformed=transformed, selected_guid=selected)
            columns = list(rows[0])[:expected.shape[1]]
            np.testing.assert_allclose([[float(row[col]) for col in columns] for row in rows], expected)
            if selected == guid:
                assert {row["run_guid"] for row in rows} == {guid}
                if not transformed:
                    assert [int(row["finite_cells"]) for row in rows] == [2, 2, 2]
            else:
                assert {"run_guid", "cut", "finite_cells", "x_unit", "value_unit"}.isdisjoint(rows[0])
    assert_database_artifacts_unchanged_except_trusted_shm(path, protected)
    assert not errors
