"""Mixed QCoDeS runs retain every quantity's axes and recorded samples."""

import csv
import itertools
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from PyQt6 import QtCore, QtTest
from PyQt6 import QtWidgets as qtw
from qcodes.dataset.measurements import DataSaver

from qplot.datahandling.dimensions import (
    UnsupportedPlotDimensionError,
    ensure_supported_plot_dimensions,
)
from qplot.datahandling.parameter_data import parameter_data_for_export
from qplot.datahandling.readonly import (
    load_by_id_read_only,
    sqlite_read_only_connection,
)
from qplot.datahandling.trusted_live_queries import TrustedParameterView
from qplot.testdata import (
    CSV_COLUMNS,
    INSTRUCTION_FILE_NAMES,
    AxisSpecification,
    GenerationCancelled,
    MeasurementSpecification,
    RunSpecification,
    SpecificationError,
    generate_database,
    generate_database_from_csv,
    read_specifications,
)
from qplot.windows import _plot_marquee
from qplot.windows._dataset_handle import DatasetKey, dataset_key_matches_current_source
from qplot.windows._plot_actions import PlotActionsMixin
from qplot.windows._plotWin import plotWidget
from qplot.windows._trusted_derived_qt import TrustedDerivedQtBridge
from qplot.windows._widgets import preview as preview_module
from qplot.windows._widgets.preview import (
    DraggablePreviewImageLabel,
    PreviewCard,
    PreviewImageLabel,
    generate_run_previews,
)
from qplot.windows._widgets.run_list_items import RunPreviewCell


def _write_csv(path, rows):
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        writer.writerows(rows)


@pytest.fixture
def mixed_database(tmp_path):
    source = tmp_path / "mixed.csv"
    # Forward references and a non-adjacent run declaration are intentional.
    rows = [
        ("mixed", "measurement", "line", "Line", "A", "x", "", "", ""),
        ("other", "axis", "x", "Different X", "V", "", "10", "20", "2"),
        ("mixed", "axis", "x", "X", "V", "", "-1", "1", "4"),
        ("mixed", "axis", "y", "Y", "V", "", "0", "2", "3"),
        ("mixed", "axis", "z", "Z", "T", "", "1", "2", "2"),
        ("mixed", "axis", "t", "T", "K", "", "3", "4", "2"),
        ("mixed", "measurement", "map", "Map", "A", "y;x", "", "", ""),
        ("mixed", "measurement", "volume", "Volume", "A", "z;y;x", "", "", ""),
        (
            "mixed",
            "measurement",
            "hypervolume",
            "Hypervolume",
            "A",
            "t;z;y;x",
            "",
            "",
            "",
        ),
        ("mixed", "measurement", "second_line", "Second line", "A", "x", "", "", ""),
        ("other", "measurement", "line", "Other line", "A", "x", "", "", ""),
    ]
    _write_csv(source, rows)
    output, specifications = generate_database_from_csv(
        source, tmp_path / "mixed.db", rng=np.random.default_rng(123)
    )
    return output, specifications


def _metadata(path):
    connection = sqlite_read_only_connection(path)
    try:
        connection.row_factory = None
        row = connection.execute(
            "SELECT guid, result_table_name, run_description FROM runs ORDER BY run_id LIMIT 1"
        ).fetchone()
        return dict(
            zip(("guid", "result_table_name", "run_description"), row, strict=True)
        )
    finally:
        connection.close()


def _artifact_state(path):
    state = {}
    for suffix in ("", "-wal", "-journal", "-shm"):
        artifact = Path(f"{path}{suffix}")
        if artifact.exists():
            status = artifact.stat()
            state[suffix] = (artifact.read_bytes(), status.st_ino, status.st_mtime_ns)
    return state


def test_mixed_run_preserves_qcodes_axes_grids_and_independent_sample_counts(
    mixed_database,
):
    path, specifications = mixed_database
    assert [run.name for run in specifications] == ["mixed", "other"]
    assert [m.dimensions for m in specifications[0].measurements] == [1, 2, 3, 4, 1]
    before = _artifact_state(path)
    dataset = load_by_id_read_only(1, path)
    try:
        assert dataset.name == "mixed"
        assert dataset.completed
        assert dataset.number_of_results == 4 + 12 + 24 + 48 + 4
        trees = {}
        for measured in specifications[0].measurements:
            axes = [axis.name for axis in measured.axes]
            assert dataset.paramspecs[measured.name].depends_on_ == axes
            data = parameter_data_for_export(dataset, measured.name)
            assert set(data) == {measured.name, *axes}
            assert data[measured.name].size == measured.point_count
            expected = np.array(
                list(
                    itertools.product(
                        *(
                            np.linspace(axis.start, axis.stop, axis.points)
                            for axis in measured.axes
                        )
                    )
                )
            )
            for index, name in enumerate(axes):
                np.testing.assert_allclose(data[name].ravel(), expected[:, index])
            assert np.isfinite(data[measured.name]).all()
            trees[measured.name] = data
        assert not np.array_equal(
            trees["line"]["line"], trees["second_line"]["second_line"]
        )
    finally:
        dataset.conn.close()
    assert _artifact_state(path) == before


def test_generator_accepts_dimensions_beyond_four_and_bounds_every_batch(
    tmp_path, monkeypatch
):
    axes = tuple(
        AxisSpecification(f"a{i}", f"Axis {i}", "", 0.0, 1.0, 2) for i in range(7)
    )
    measured = MeasurementSpecification("signal", "Signal", "", axes)
    sizes = []
    original = DataSaver.add_result

    def record(saver, *results):
        sizes.append(np.size(results[-1][1]))
        return original(saver, *results)

    monkeypatch.setattr("qplot.testdata._RESULT_CHUNK_POINTS", 9)
    monkeypatch.setattr(DataSaver, "add_result", record)
    # Exercise the public CSV parser as well as the writer for >4 axes.
    rows = [
        ("seven", "axis", a.name, a.label, a.unit, "", a.start, a.stop, a.points)
        for a in axes
    ]
    rows.append(
        (
            "seven",
            "measurement",
            "signal",
            "Signal",
            "",
            ";".join(a.name for a in axes),
            "",
            "",
            "",
        )
    )
    source = tmp_path / "seven.csv"
    _write_csv(source, rows)
    path, specs = generate_database_from_csv(source, tmp_path / "seven.db")
    assert specs[0].measurements == (measured,)
    assert max(sizes) <= 9
    assert sum(sizes) == 128
    dataset = load_by_id_read_only(1, path)
    try:
        data = parameter_data_for_export(dataset, "signal")
        assert data["signal"].size == 128
        assert dataset.paramspecs["signal"].depends_on_ == [a.name for a in axes]
    finally:
        dataset.conn.close()


def test_higher_dimensional_generation_cancels_between_chunks(tmp_path, monkeypatch):
    axes = tuple(AxisSpecification(f"a{i}", f"A{i}", "", 0.0, 1.0, 4) for i in range(4))
    specification = RunSpecification(
        (MeasurementSpecification("signal", "Signal", "", axes),)
    )
    batches = []
    original = DataSaver.add_result

    def record(saver, *results):
        batches.append(True)
        return original(saver, *results)

    monkeypatch.setattr("qplot.testdata._RESULT_CHUNK_POINTS", 5)
    monkeypatch.setattr(DataSaver, "add_result", record)
    with pytest.raises(GenerationCancelled):
        generate_database(
            [specification],
            tmp_path / "cancel.db",
            cancelled_callback=lambda: len(batches) >= 2,
        )
    assert len(batches) == 2
    assert list(tmp_path.iterdir()) == []


def test_programmatic_singleton_axis_keeps_finite_measurements(tmp_path):
    axis = AxisSpecification("x", "X", "", 2.0, 3.0, 1)
    specification = RunSpecification(
        (MeasurementSpecification("signal", "Signal", "", (axis,)),)
    )
    path = generate_database([specification], tmp_path / "singleton.db")
    dataset = load_by_id_read_only(1, path)
    try:
        data = parameter_data_for_export(dataset, "signal")
        np.testing.assert_array_equal(data["x"], [2.0])
        assert data["signal"].size == 1
        assert np.isfinite(data["signal"]).all()
    finally:
        dataset.conn.close()


@pytest.mark.parametrize(
    "extra, message",
    [
        (("r", "axis", "X", "Collision", "", "", "0", "1", "3"), "duplicate parameter"),
        (
            ("r", "measurement", "x", "Collision", "", "x", "", "", ""),
            "duplicate parameter",
        ),
        (
            ("r", "measurement", "bad", "Bad", "", "s", "", "", ""),
            "setpoint 's' must name an axis",
        ),
        (("r", "axis", "unused", "Unused", "", "", "0", "1", "3"), "is not used"),
        (
            ("elsewhere", "measurement", "bad", "Bad", "", "x", "", "", ""),
            "setpoint 'x' must name an axis",
        ),
    ],
)
def test_invalid_shared_axis_declarations_fail_before_generation(
    tmp_path, extra, message
):
    source = tmp_path / "bad.csv"
    _write_csv(
        source,
        [
            ("r", "axis", "x", "X", "", "", "0", "1", "3"),
            ("r", "measurement", "s", "Signal", "", "x", "", "", ""),
            extra,
        ],
    )
    with pytest.raises(SpecificationError, match=message):
        read_specifications(source)


@pytest.mark.parametrize("compact_metadata", [False, True])
def test_higher_dimensional_previews_do_not_read_measurement_samples(
    mixed_database, monkeypatch, compact_metadata
):
    path, _ = mixed_database
    metadata = _metadata(path)
    if compact_metadata:
        metadata.pop("run_description")
        metadata.update(
            measure_parameters=["line", "map", "volume", "hypervolume", "second_line"],
            sweep_parameters=["x", "y", "z", "t"],
        )
    before = _artifact_state(path)
    selected = []
    original = preview_module._select_arrays

    def record(cursor, table, columns, *args, **kwargs):
        selected.extend(columns)
        return original(cursor, table, columns, *args, **kwargs)

    monkeypatch.setattr(preview_module, "_select_arrays", record)
    previews = generate_run_previews(path, metadata, size=22)
    by_name = {preview["parameter"]: preview for preview in previews}
    assert {"line", "map", "volume", "hypervolume", "second_line"} == set(by_name)
    assert "image" in by_name["line"]
    assert by_name["line"]["axes"] == ["x"]
    assert by_name["map"]["axes"] == ["y", "x"]
    assert by_name["volume"]["axes"] == ["z", "y", "x"]
    assert by_name["hypervolume"]["axes"] == ["t", "z", "y", "x"]
    for name, dimensions in [("volume", 3), ("hypervolume", 4)]:
        assert by_name[name]["unsupported"] is True
        assert by_name[name]["dimension_count"] == dimensions
        assert "image" not in by_name[name]
        assert name not in selected
        with pytest.raises(UnsupportedPlotDimensionError):
            ensure_supported_plot_dimensions(name, by_name[name]["axes"])
    assert _artifact_state(path) == before


@pytest.mark.parametrize("dimension", [3, 4])
@pytest.mark.parametrize("surface", ["thumbnail", "preview", "pending-thumbnail"])
def test_placeholders_offer_export_and_stats_without_plot_or_drag(
    monkeypatch, dimension, surface
):
    preview = {
        "parameter": "signal",
        "axes": [f"a{i}" for i in range(dimension)],
        "dimension_count": dimension,
        "unsupported": True,
    }
    if surface == "preview":
        host = PreviewCard(preview, 200, "guid")
    else:
        host = RunPreviewCell("guid", 1)
        host.update_placeholder_metadata(
            {"measure_parameters": ["signal"], "preview_dimensions": [dimension]}
        )
        if surface == "thumbnail":
            host.show_previews([preview])
    signals = []
    host.plotRequested.connect(lambda *args: signals.append(("plot", args)))
    host.exportRequested.connect(lambda *args: signals.append(("export", args)))
    host.statsRequested.connect(lambda *args: signals.append(("stats", args)))
    try:
        label = host.findChild(PreviewImageLabel)
        assert label.text() == f"{dimension}D"
        assert not isinstance(label, DraggablePreviewImageLabel)
        QtTest.QTest.mouseDClick(label, QtCore.Qt.MouseButton.LeftButton)
        QtTest.QTest.keyClick(label, QtCore.Qt.Key.Key_Return)
        QtTest.QTest.keyClick(label, QtCore.Qt.Key.Key_Space)
        assert signals == []

        def choose(menu, _position):
            plot, export, stats = menu.actions()
            assert not plot.isEnabled()
            assert export.isEnabled() and stats.isEnabled()
            export.trigger()
            stats.trigger()

        monkeypatch.setattr(qtw.QMenu, "exec", choose)
        label._show_context_menu(QtCore.QPoint())
        assert [kind for kind, _ in signals] == ["export", "stats"]
        assert all(args[-1] == "signal" for _, args in signals)
    finally:
        host.close()
        host.deleteLater()


@pytest.mark.parametrize("only_high_dimensions", [False, True])
def test_trusted_previews_keep_all_higher_dimensional_targets(only_high_dimensions):
    parameters = tuple(
        TrustedParameterView(
            name, name, "", tuple(f"a{i}" for i in range(dim)), "numeric"
        )
        for name, dim in [("line", 1), ("volume", 3), ("hypervolume", 4)]
    )
    if only_high_dimensions:
        parameters = parameters[1:]
    bridge = SimpleNamespace(_parameters_by_guid={"guid": parameters})
    previews, error = TrustedDerivedQtBridge._decode_images(
        bridge, "guid", {"images": ()}
    )
    assert error is None
    assert [p["parameter"] for p in previews] == ["volume", "hypervolume"]
    assert [p["dimension_count"] for p in previews] == [3, 4]
    assert all(p["unsupported"] for p in previews)


def test_high_dimension_csv_and_stats_actions_read_every_sample_and_axis(
    mixed_database, tmp_path, monkeypatch
):
    path, specs = mixed_database
    key = DatasetKey(str(path), _metadata(path)["guid"])
    before = _artifact_state(path)
    dialogs, errors = [], []

    class Host(PlotActionsMixin, qtw.QWidget):
        destination = tmp_path / "export.csv"

        def _bound_plot_dataset_key_is_current(self, requested):
            return dataset_key_matches_current_source(requested)

        def _default_preview_export_filename(self, *args):
            return "measurement.csv"

        def _default_run_csv_export_filename(self, *args, **kwargs):
            return "run.csv"

        def _choose_csv_export_filename(self, default):
            return self.destination

        def show_status(self, *args):
            pass

        def show_error(self, *args):
            errors.append(args)

    original_dialog = _plot_marquee.new_statistics_dialog

    def capture_dialog(parent, text, title):
        dialog = original_dialog(parent, text, title)
        dialogs.append((dialog, text))
        return dialog

    monkeypatch.setattr(_plot_marquee, "new_statistics_dialog", capture_dialog)
    host = Host()
    try:
        for measured in specs[0].measurements:
            if measured.dimensions <= 2:
                continue
            host.destination = tmp_path / f"{measured.name}.csv"
            host._export_preview_csv(key, measured.name)
            assert not errors
            frame = pd.read_csv(host.destination)
            assert len(frame) == measured.point_count
            assert set(frame.columns) == {
                measured.name,
                *(a.name for a in measured.axes),
            }
            host._show_preview_stats(key, measured.name)
            assert not errors
            text = dialogs[-1][1]
            assert text.startswith(f"{measured.point_count} points\n")
            assert (
                f"Average: {plotWidget.formatNum(frame[measured.name].mean())}" in text
            )
            for axis in measured.axes:
                assert (
                    f"{axis.name} range: {plotWidget.formatNum(axis.start)} to {plotWidget.formatNum(axis.stop)}"
                    in text
                )
        host.destination = tmp_path / "whole-run.csv"
        host._export_measurement_csv(
            key, tuple(m.name for m in specs[0].measurements), run_id=1
        )
        assert not errors
        frame = pd.read_csv(host.destination)
        assert len(frame) == 48
        for measured in specs[0].measurements:
            assert (
                frame[f"{measured.name}.{measured.name}"].notna().sum()
                == measured.point_count
            )
    finally:
        for dialog, _ in dialogs:
            dialog.close()
        host.close()
    assert _artifact_state(path) == before


def test_smallest_collection_database_matches_nominal_size(tmp_path):
    source = (
        Path(__file__).parents[1]
        / "src/qplot/resources/testdata"
        / INSTRUCTION_FILE_NAMES[0]
    )
    output, _ = generate_database_from_csv(source, tmp_path / "ten-mb.db")
    assert 8_000_000 <= output.stat().st_size <= 12_000_000
