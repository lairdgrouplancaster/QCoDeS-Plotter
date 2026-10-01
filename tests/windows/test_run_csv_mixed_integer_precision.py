"""Mixed array dtypes retain the stored integers through the real Run CSV UI."""

import csv
import sqlite3
from contextlib import closing
from io import BytesIO

import numpy as np
import pytest
from PyQt6 import QtWidgets as qtw
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.windows import _plot_actions as actions
from qplot.windows import main as main_window
from tests._window_lifecycle import close_main_window
from tests.windows.test_complex_line_data import database_state
from tests.windows.test_plot_integration import configure_temp_qplot, wait_for


def _create_mixed_run(path, layout, lengths, second_dtype):
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("mixed_integer_csv", sample_name="test")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("x", paramtype="array")
    measurement.register_custom_parameter("signal", paramtype="array", setpoints=("x",))
    measurement.register_custom_parameter("other", paramtype="array")
    unsigned = np.array([9223372036854775809, 9223372036854775811], dtype=np.uint64)
    signed = np.array([-1, -3, -5] if "ragged" in layout else [-1, -3], dtype=np.int64)
    if second_dtype == "float":
        signed = signed.astype(np.float64) - 0.5
    elif second_dtype == "complex":
        signed = signed.astype(np.complex128) + 2j
    count = unsigned.size + signed.size
    other_count = count + {"shorter": -1, "equal": 0, "longer": 1}[lengths]
    dataset = None
    try:
        # The writer's independent cache must not combine the mixed dtypes.
        with measurement.run(write_in_background=False, in_memory_cache=False) as saver:
            for index, signal in enumerate((unsigned, signed)):
                # Also test mixed integer coordinate records, in reverse dtype order.
                x = (np.arange(signal.size, dtype=np.int64) - 10 if index == 0
                     else np.arange(signal.size, dtype=np.uint64) + np.uint64(2**63 + 17))
                if "matrix" in layout:
                    signal, x = signal.reshape(1, -1), x.reshape(1, -1)
                saver.add_result(("signal", signal), ("x", x))
            saver.add_result(("other", np.arange(other_count, dtype=np.uint64) + np.uint64(2**63 + 31)))
            dataset = saver.dataset
            run_id, table_name = dataset.run_id, dataset.table_name
    finally:
        if dataset is not None:
            dataset.conn.close()
        experiment.conn.close()

    # Decode the stored NumPy blobs directly. Never promote/concatenate the
    # oracle with NumPy or obtain it from QCoDeS' parameter-data conversion.
    expected = {name: [] for name in ("signal", "x", "other")}
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        for row in conn.execute(f'SELECT signal, x, other FROM "{table_name}" ORDER BY id'):
            for name, blob in zip(expected, row, strict=True):
                if blob is not None:
                    array = np.load(BytesIO(blob), allow_pickle=False)
                    assert array.dtype.kind in "iufc"
                    expected[name].extend(array.ravel().tolist())
    assert expected["signal"][:2] == [9223372036854775809, 9223372036854775811]
    if second_dtype == "int":
        assert expected["signal"][2:4] == [-1, -3]
    return run_id, expected


@pytest.mark.parametrize("layout", ["equal", "ragged", "matrix_equal", "matrix_ragged"])
@pytest.mark.parametrize("lengths", ["shorter", "equal", "longer"])
@pytest.mark.parametrize("second_dtype", ["int", "float", "complex"])
def test_actual_run_csv_preserves_mixed_integers(tmp_path, monkeypatch, layout, lengths, second_dtype):
    configure_temp_qplot(monkeypatch, tmp_path)
    path = tmp_path / "mixed.db"
    run_id, expected = _create_mixed_run(path, layout, lengths, second_dtype)
    protected = database_state(path)
    extracted = []
    original_extract = actions.parameter_data_for_export

    def track_extract(dataset, name):
        # Run CSV uses a fresh reader and must leave its raw cache untouched.
        assert dataset.cache._data == {}
        data = original_extract(dataset, name)
        assert dataset.cache._data == {}
        copies = {key: [np.asarray(record).copy() for record in column]
                  for key, column in data.items()}
        extracted.append((data, copies))
        return data

    monkeypatch.setattr(actions, "parameter_data_for_export", track_extract)
    errors = []
    window = main_window.MainWindow()
    try:
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        monkeypatch.setattr(window, "show_error", lambda *args: errors.append(args))
        window.close_database(status=False)
        assert window.load_file(str(path))
        wait_for(lambda: not window._database_load_active and not window._database_detail_active)
        window.monitor.stop()
        assert window.selected_run_id == run_id

        for selection in ("1", "*"):
            target = tmp_path / f"mixed-{selection.replace('*', 'all')}.csv"
            monkeypatch.setattr(
                qtw.QFileDialog, "getSaveFileName", lambda *a, target=target, **k: (str(target), ""),
            )
            window.measurementBox.setText(selection)
            window.exportRunCsv()
            assert errors == []
            with target.open(newline="", encoding="utf-8") as csv_file:
                reader = csv.DictReader(csv_file)
                rows = list(reader)
            names = {"signal": "signal", "x": "x"} if selection == "1" else {
                "signal.signal": "signal", "signal.x": "x", "other.other": "other",
            }
            assert reader.fieldnames == list(names)
            count = max(len(expected[name]) for name in names.values())
            assert len(rows) == count
            for column, name in names.items():
                values = [str(value) for value in expected[name]]
                assert [row[column] for row in rows] == values + [""] * (count - len(values))

        # Neither flattening nor CSV alignment may alter the extracted arrays.
        for data, copies in extracted:
            for name, records in copies.items():
                for actual, saved in zip(data[name], records, strict=True):
                    assert np.asarray(actual).dtype == saved.dtype
                    np.testing.assert_array_equal(actual, saved)
    finally:
        close_main_window(window)
    assert database_state(path) == protected
