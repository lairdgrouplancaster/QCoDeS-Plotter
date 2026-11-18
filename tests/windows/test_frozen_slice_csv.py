"""Frozen slice and cut exports retain their physical coordinates and units."""

import csv
from itertools import product

import numpy as np
import pytest
from PyQt6 import QtWidgets
from pyqtgraph.exporters import CSVExporter
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.windows._dataset_handle import TraceKey
from tests.support.windows.plot3d import main as main
from tests.support.windows.plot_integration import wait_for
from tests.support.windows.svg_export_integration import select_exporter

DELAYS = (-3., 0., 3.)
BIASES = (1., 4., 9.)


def signal(field, bias, delay):
    return 100 * field + bias * (2 + delay**2)


def next_plot(window, action):
    count = len(window.windows)
    action()
    wait_for(lambda: len(window.windows) == count + 1 and window.windows[-1]._qplot_display_synchronized)
    return window.windows[-1]


def menu_action(plot, label):
    return next(action for action in plot.vbMenu.actions() if action.text() == label)


@pytest.fixture
def slices(main, tmp_path):
    path = tmp_path / "measurement.db"
    initialise_or_create_database_at(str(path), journal_mode="WAL")
    experiment = load_or_create_experiment("slice CSV", "sample")
    try:
        measurement = Measurement(exp=experiment)
        for name, unit in (("field", "T"), ("bias", "mV"), ("delay", "s")):
            measurement.register_custom_parameter(name, unit=unit)
        measurement.register_custom_parameter("signal", unit="nA", setpoints=("field", "bias", "delay"))
        with measurement.run(write_in_background=False) as saver:
            for field, bias, delay in product((2., 7.), BIASES, DELAYS):
                saver.add_result(("field", field), ("bias", bias), ("delay", delay),
                                 ("signal", signal(field, bias, delay)))
        guid = saver.dataset.guid
        saver.dataset.conn.close()
    finally:
        experiment.conn.close()
    window, errors = main
    assert window.load_file(str(path))
    wait_for(lambda: not window._database_load_active and window._selected_run_guid == guid)
    live = next_plot(window, window.plotRunButton.click)
    frozen = next_plot(window, menu_action(live, "Freeze Slice as 2D Plot").trigger)
    yield window, live, frozen, guid
    assert not errors


def export_csv(plot, path, monkeypatch):
    dialog = select_exporter(plot, CSVExporter)
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName", lambda *args, **kwargs: (str(path), ""))
    dialog.ui.exportBtn.click()
    wait_for(lambda: path.exists() and not plot._export_tasks)
    dialog.close()
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def swap_axes(plot):
    plot.appearance_action.trigger()
    dialog = plot._plot_appearance_dialog
    dialog.swap_axes.click()
    dialog.appearance_buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).click()
    wait_for(lambda: plot._qplot_display_synchronized and plot.axis_param["x"].name == "bias")


def test_swapped_frozen_slice_derivative_csv_matches_display(slices, tmp_path, monkeypatch):
    _window, live, frozen, guid = slices
    control = export_csv(live, tmp_path / "live.csv", monkeypatch)
    np.testing.assert_allclose(
        [(float(row["delay"]), float(row["bias"]), float(row["signal"])) for row in control],
        [(delay, bias, signal(2., bias, delay)) for bias, delay in product(BIASES, DELAYS)],
    )
    assert {(r["x_unit"], r["y_unit"], r["value_unit"]) for r in control} == {("s", "mV", "nA")}

    swap_axes(frozen)
    options = frozen.oper_widget.list_options
    derivative = next(options.item(i) for i in range(options.count())
                      if options.item(i).operation_row.spec.key == "derivative_x")
    derivative.input.click()
    frozen.oper_widget.apply_but.click()
    wait_for(lambda: frozen._qplot_display_synchronized and frozen.display_param.unit == "nA/mV")

    rows = export_csv(frozen, tmp_path / "derivative.csv", monkeypatch)
    expected = [(bias, delay, 2 + delay**2) for delay, bias in product(DELAYS, BIASES)]
    assert tuple(rows[0])[:3] == ("bias", "delay", "signal")
    np.testing.assert_array_equal(frozen.axis_data["x"], BIASES)
    np.testing.assert_array_equal(frozen.axis_data["y"], DELAYS)
    np.testing.assert_allclose([(float(r["bias"]), float(r["delay"]), float(r["signal"])) for r in rows], expected)
    np.testing.assert_allclose(frozen.dataGrid, np.asarray(expected)[:, 2].reshape(len(DELAYS), len(BIASES)))
    units = (frozen.axis_param["x"].unit, frozen.axis_param["y"].unit, frozen.display_param.unit)
    assert units == ("mV", "s", "nA/mV")
    assert {(r["x_unit"], r["y_unit"], r["value_unit"]) for r in rows} == {units}
    assert {(float(r["field (T)"]), r["run_guid"], r["source_revision"]) for r in rows} == {
        (2., guid, control[0]["source_revision"]),
    }


@pytest.mark.parametrize("swapped", [False, True], ids=["vertical", "horizontal-after-swap"])
def test_equal_frozen_cuts_export_both_fixed_coordinates(slices, tmp_path, monkeypatch, swapped):
    window, live, frozen, guid = slices
    if swapped:
        swap_axes(frozen)
    exports = []
    for parent in (live, frozen):
        horizontal = parent is frozen and swapped
        action = menu_action(parent, "Horizontal Cut" if horizontal else "Vertical Cut")
        for index in (0, 2):
            pixel = (1, index) if horizontal else (index, 1)
            parent.show_hover_pixel_outline(*pixel)
            cut = next_plot(window, action.trigger)
            path = tmp_path / f"{len(exports)}.csv"
            rows = export_csv(cut, path, monkeypatch)
            x, y = tuple(rows[0])[:2]
            np.testing.assert_allclose([(float(r[x]), float(r[y])) for r in rows],
                                       [(bias, signal(2., bias, DELAYS[index])) for bias in BIASES])
            assert all(r["run_guid"] == guid for r in rows)
            exports.append(rows)

    for delay, live_rows, frozen_rows in zip((DELAYS[0], DELAYS[2]), exports[:2], exports[2:], strict=True):
        assert all(f"delay={delay} s" in r["cut"] and "field=2.0 T" in r["cut"] for r in live_rows)
        assert {(float(r["delay (s)"]), float(r["field (T)"])) for r in frozen_rows} == {(delay, 2.)}
        assert {(r["x_unit"], r["value_unit"]) for r in frozen_rows} == {("mV", "nA")}


def test_combined_csv_does_not_label_other_samples_with_frozen_cut_coordinates(slices, tmp_path, monkeypatch):
    window, _live, frozen, _guid = slices
    frozen.show_hover_pixel_outline(0, 1)
    cut = next_plot(window, menu_action(frozen, "Vertical Cut").trigger)
    reference = cut.plot.plot(BIASES, [30., 35., 50.], name="reference")
    monkeypatch.setitem(cut.lines, TraceKey(cut._dataset_key, "reference"), reference)

    rows = export_csv(cut, tmp_path / "both-traces.csv", monkeypatch)
    columns = list(rows[0])
    assert {"field (T)", "delay (s)", "run_guid", "value_unit"}.isdisjoint(columns)
    np.testing.assert_allclose([[float(r[key]) for key in columns] for r in rows], [
        [bias, signal(2., bias, -3.), bias, ref] for bias, ref in zip(BIASES, (30., 35., 50.), strict=True)
    ])
