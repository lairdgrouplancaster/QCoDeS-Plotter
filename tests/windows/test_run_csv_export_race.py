"""Real-data CSV exports and database-instance races."""

import csv
import os
import sqlite3
from collections import Counter
from pathlib import Path

import numpy as np
import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.parameters import ManualParameter

from qplot.datahandling.file_identity import (
    SQLITE_SIDECAR_SUFFIXES,
    database_file_identity,
    logical_database_path,
)
from qplot.datahandling.trusted_live_service import TRUSTED_LIVE_MODE
from qplot.windows import _plot_actions as plot_actions_module
from qplot.windows._dataset_handle import close_dataset_connection
from qplot.windows._export_paths import prepare_export_destination
from qplot.windows._plot_actions import PlotActionsMixin


class _Field:
    def __init__(self, value):
        self.value = str(value)

    def text(self):
        return self.value


class _RunList:
    def __init__(self, run_id, guid):
        self._runs = {
            run_id: {
                "guid": guid,
                "measure_parameters": ["signal"],
                "sweep_parameters": ["x"],
            }
        }

    def all_run_metadata(self):
        return self._runs

    def run_id_for_guid(self, guid):
        for run_id, metadata in self._runs.items():
            if metadata["guid"] == guid:
                return run_id
        return None


class _RunCsvHarness(PlotActionsMixin):
    def __init__(self, database_path, destination, run_id, guid):
        self.fileTextbox = _Field(database_path)
        self.measurementBox = _Field("*")
        self.selected_run_id = run_id
        self.ds = None
        self._selected_dataset_key = None
        self.dataset_holder = {}
        self.RunList = _RunList(run_id, guid)
        self.destination = destination
        self.destination_callback = None
        self._loaded_database_identity = database_file_identity(database_path)
        self.status_messages = []
        self.errors = []
        self.reload_requests = []
        self.loaded_datasets = []

    def _choose_csv_export_filename(self, default_name):
        if self.destination_callback is not None:
            destination = self.destination_callback(default_name)
            if destination is None:
                self.show_status("CSV export cancelled.", 3000)
            return destination
        return prepare_export_destination(
            self,
            str(self.destination),
            replacement_confirmed=self.destination.exists(),
        )

    def _load_run_csv_dataset(self, dataset_key):
        dataset = super()._load_run_csv_dataset(dataset_key)
        self.loaded_datasets.append(dataset)
        return dataset

    def _reload_if_database_instance_changed(self, database_path):
        changed = (
            database_file_identity(database_path)
            != self._loaded_database_identity
        )
        if changed:
            self.reload_requests.append(str(database_path))
        return changed

    def show_status(self, message, timeout=5000):
        self.status_messages.append((message, timeout))

    def show_error(self, title, message, details=None):
        self.errors.append((title, message, details))


def _create_run(database_path: Path, row_count: int):
    initialise_or_create_database_at(
        str(database_path),
        journal_mode="DELETE",
    )
    experiment = load_or_create_experiment(
        f"csv_export_{database_path.stem}",
        sample_name="sample",
    )
    x = ManualParameter("x")
    signal = ManualParameter("signal")
    measurement = Measurement(exp=experiment, name="csv_export")
    measurement.register_parameter(x)
    measurement.register_parameter(signal, setpoints=(x,))
    with measurement.run(write_in_background=False) as datasaver:
        for index in range(row_count):
            datasaver.add_result((x, index), (signal, 10 + index))
        dataset = datasaver.dataset
        run_id = dataset.run_id
        guid = dataset.guid
    dataset.conn.close()
    # Measurement retains the experiment's independent connection.  Closing
    # only the dataset connection leaves the source database locked on
    # Windows, preventing the replacement race this test is meant to model.
    experiment.conn.close()
    return run_id, guid


def _create_measurement_run(database_path, kind):
    initialise_or_create_database_at(str(database_path), journal_mode="DELETE")
    experiment = load_or_create_experiment(
        f"csv_measurements_{database_path.stem}", sample_name="sample"
    )
    x = ManualParameter("x")
    signal = ManualParameter("signal")
    temperature = ManualParameter("temperature")
    measurement = Measurement(exp=experiment, name="csv_measurements")
    if kind == "mixed":
        measurement.register_parameter(temperature)
    if kind in ("mixed", "dependent"):
        measurement.register_parameter(x)
        measurement.register_parameter(signal, setpoints=(x,))
    if kind == "standalone":
        measurement.register_parameter(temperature)
    with measurement.run(write_in_background=False) as datasaver:
        if kind in ("mixed", "dependent"):
            for index in range(3):
                datasaver.add_result((x, index), (signal, 10 + index))
        if kind in ("mixed", "standalone"):
            for value in (20, 21):
                datasaver.add_result((temperature, value))
        dataset = datasaver.dataset
        run_id, guid = dataset.run_id, dataset.guid
    dataset.conn.close()
    experiment.conn.close()
    return run_id, guid


@pytest.mark.parametrize(
    "kind,selection",
    (
        ("mixed", "*"), ("mixed", "1"), ("mixed", "2"),
        ("standalone", "*"), ("standalone", "1"),
        ("dependent", "*"), ("dependent", "1"),
    ),
)
@pytest.mark.parametrize("loaded", (False, True))
def test_run_csv_exports_real_measurements_consistently(
        tmp_path, kind, selection, loaded
        ):
    names = {
        "mixed": ["signal", "temperature"],
        "standalone": ["temperature"],
        "dependent": ["signal"],
    }[kind]
    database_path = tmp_path / f"{kind}.db"
    run_id, guid = _create_measurement_run(database_path, kind)
    destination = tmp_path / "export.csv"
    harness = _RunCsvHarness(database_path, destination, run_id, guid)
    harness.RunList._runs[run_id]["measure_parameters"] = names
    harness.measurementBox.value = selection
    selected = None
    try:
        if loaded:
            selected = harness._load_run_csv_dataset(
                harness._current_dataset_key(guid)
            )
            harness.ds = selected
            harness._selected_dataset_key = harness._current_dataset_key(guid)

        request = harness._run_csv_export_request()
        expected_names = names if selection == "*" else [names[int(selection) - 1]]
        assert request[1] == tuple(expected_names)
        harness.exportRunCsv()
        assert harness.errors == []
        with destination.open(newline="", encoding="utf-8") as csv_file:
            rows = list(csv.reader(csv_file))

        if expected_names == ["signal"]:
            assert rows == [
                ["signal", "x"], ["10.0", "0.0"],
                ["11.0", "1.0"], ["12.0", "2.0"],
            ]
        elif expected_names == ["temperature"]:
            assert rows == [["temperature"], ["20.0"], ["21.0"]]
        else:
            assert rows == [
                ["signal.signal", "signal.x", "temperature.temperature"],
                ["10.0", "0.0", "20.0"],
                ["11.0", "1.0", "21.0"],
                ["12.0", "2.0", ""],
            ]
    finally:
        if selected is not None:
            close_dataset_connection(selected)


def test_trusted_csv_export_recovers_standalone_before_metadata_finishes(
        tmp_path
        ):
    database_path = tmp_path / "progressive.db"
    run_id, guid = _create_measurement_run(database_path, "mixed")
    destination = tmp_path / "progressive.csv"
    harness = _RunCsvHarness(database_path, destination, run_id, guid)
    harness._database_access_mode = TRUSTED_LIVE_MODE
    harness.RunList._runs[run_id]["measure_parameters"] = ["signal"]

    def choose_after_preflight(_default_name):
        assert len(harness.loaded_datasets) == 1
        with pytest.raises((sqlite3.ProgrammingError, RuntimeError)):
            harness.loaded_datasets[0].conn.cursor()
        return str(destination)

    harness.destination_callback = choose_after_preflight
    harness.exportRunCsv()

    assert harness.errors == []
    with destination.open(newline="", encoding="utf-8") as csv_file:
        rows = list(csv.reader(csv_file))
    assert rows == [
        ["signal.signal", "signal.x", "temperature.temperature"],
        ["10.0", "0.0", "20.0"],
        ["11.0", "1.0", "21.0"],
        ["12.0", "2.0", ""],
    ]
    assert len(harness.loaded_datasets) == 2
    with pytest.raises((sqlite3.ProgrammingError, RuntimeError)):
        harness.loaded_datasets[1].conn.cursor()


def _artifact_state(database_path: Path):
    state = {}
    for suffix in ("", *SQLITE_SIDECAR_SUFFIXES):
        path = Path(f"{database_path}{suffix}")
        if not path.exists():
            state[suffix] = None
            continue
        state[suffix] = {
            "identity": database_file_identity(path),
            "bytes": path.read_bytes(),
        }
    return state


@pytest.mark.parametrize("export_path", ("run", "selected_preview", "run_preview"))
@pytest.mark.parametrize("mixed", (False, True))
@pytest.mark.parametrize("kind", ("variable", "fixed", "scalar_setpoint", "multidimensional"))
def test_array_csv_exports_every_sample(tmp_path, export_path, mixed, kind):
    source = tmp_path / "arrays.db"
    initialise_or_create_database_at(str(source), journal_mode="DELETE")
    experiment = load_or_create_experiment("array_csv", sample_name="sample")
    measurement = Measurement(exp=experiment, name="array_csv")
    x = ManualParameter("x")
    signal = ManualParameter("signal")
    temperature = ManualParameter("temperature")
    measurement.register_parameter(
        x, paramtype="numeric" if kind == "scalar_setpoint" else "array",
    )
    measurement.register_parameter(signal, setpoints=(x,), paramtype="array")
    if mixed:
        measurement.register_parameter(temperature)
    expected_x = []
    expected_signal = []
    with measurement.run(write_in_background=False) as datasaver:
        for index, count in enumerate((1001, 1001 if kind == "fixed" else 1002)):
            x_values = np.arange(count)
            signal_values = 10000 * (index + 1) + np.arange(count)
            if kind == "scalar_setpoint":
                x_values = index + 7
            elif kind == "multidimensional":
                shape = (7, 143) if index == 0 else (3, 334)
                x_values = x_values.reshape(shape)
                signal_values = signal_values.reshape(shape)
            datasaver.add_result((x, x_values), (signal, signal_values))
            expected_x.extend(
                [x_values] * count if kind == "scalar_setpoint"
                else np.asarray(x_values).ravel().tolist()
            )
            expected_signal.extend(signal_values.ravel().tolist())
        if mixed:
            for value in (20, 21, 22):
                datasaver.add_result((temperature, value))
        dataset = datasaver.dataset
        run_id, guid = dataset.run_id, dataset.guid
    dataset.conn.close()
    experiment.conn.close()

    destination = tmp_path / "export.csv"
    harness = _RunCsvHarness(source, destination, run_id, guid)
    harness._selected_run_guid = guid
    if mixed:
        harness.RunList._runs[run_id]["measure_parameters"].append("temperature")
    source_before = _artifact_state(source)
    if export_path == "run":
        harness.exportRunCsv()
    elif export_path == "selected_preview":
        harness.export_preview_csv("signal")
    else:
        harness.export_run_preview_csv(guid, "signal")

    assert harness.errors == []
    assert "exported csv" in harness.status_messages[-1][0].lower()
    with destination.open(newline="", encoding="utf-8") as csv_file:
        reader = csv.DictReader(csv_file)
        rows = list(reader)
        prefix = "signal." if mixed and export_path == "run" else ""
        expected_columns = [f"{prefix}signal", f"{prefix}x"]
        if prefix:
            expected_columns.append("temperature.temperature")
        assert reader.fieldnames == expected_columns
    assert len(rows) == (2002 if kind == "fixed" else 2003)
    assert [float(row[f"{prefix}signal"]) for row in rows] == expected_signal
    assert [float(row[f"{prefix}x"]) for row in rows] == expected_x
    assert float(rows[500][f"{prefix}signal"]) == 10500
    if prefix:
        assert [row["temperature.temperature"] for row in rows] == (
            ["20.0", "21.0", "22.0"] + [""] * (len(rows) - 3)
        )
    assert _artifact_state(source) == source_before
    _assert_no_staging_artifacts(destination)
    for loaded in harness.loaded_datasets:
        with pytest.raises((sqlite3.ProgrammingError, RuntimeError)):
            loaded.conn.cursor()


def _assert_no_staging_artifacts(destination: Path):
    assert not any(
        entry.name.startswith(f".{destination.name}.")
        for entry in destination.parent.iterdir()
    )


def _replacement_fixture(tmp_path):
    source = tmp_path / "source.db"
    replacement = tmp_path / "replacement.db"
    run_id, guid = _create_run(source, 1)
    _create_run(replacement, 2)
    destination = tmp_path / "export.csv"
    harness = _RunCsvHarness(source, destination, run_id, guid)
    replacement_states = []

    def replace_source():
        os.replace(replacement, source)
        replacement_states.append(_artifact_state(source))

    return harness, source, destination, replace_source, replacement_states


def _track_dataset_closes(monkeypatch):
    close_calls = Counter()
    original_close = plot_actions_module.close_dataset_connection

    def counted_close(dataset):
        close_calls[id(dataset)] += 1
        return original_close(dataset)

    monkeypatch.setattr(
        plot_actions_module,
        "close_dataset_connection",
        counted_close,
    )
    return close_calls


def _assert_replacement_was_rejected(
        harness,
        source,
        destination,
        replacement_states,
        ):
    assert replacement_states
    assert _artifact_state(source) == replacement_states[-1]
    assert harness.reload_requests == [logical_database_path(source)]
    assert harness.errors == []
    assert "database was replaced" in harness.status_messages[-1][0].lower()
    assert "no csv was written" in harness.status_messages[-1][0].lower()
    _assert_no_staging_artifacts(destination)


def test_run_csv_replacement_while_save_dialog_is_open_publishes_nothing(
        tmp_path,
        monkeypatch,
        ):
    (
        harness,
        source,
        destination,
        replace_source,
        replacement_states,
    ) = _replacement_fixture(tmp_path)
    sentinel = b"existing destination sentinel"
    destination.write_bytes(sentinel)
    close_calls = _track_dataset_closes(monkeypatch)

    def choose_after_replacement(_default_name):
        replace_source()
        return prepare_export_destination(
            harness,
            str(destination),
            replacement_confirmed=True,
        )

    harness.destination_callback = choose_after_replacement
    harness.exportRunCsv()

    assert destination.read_bytes() == sentinel
    assert harness.loaded_datasets == []
    assert close_calls == Counter()
    _assert_replacement_was_rejected(
        harness,
        source,
        destination,
        replacement_states,
    )


def test_run_csv_replacement_during_dataframe_extraction_rejects_stale_frame(
        tmp_path,
        monkeypatch,
        ):
    (
        harness,
        source,
        destination,
        replace_source,
        replacement_states,
    ) = _replacement_fixture(tmp_path)
    close_calls = _track_dataset_closes(monkeypatch)
    original_dataframe = harness._measurement_dataframe

    def dataframe_after_replacement(dataset, params):
        replace_source()
        return original_dataframe(dataset, params)

    monkeypatch.setattr(
        harness,
        "_measurement_dataframe",
        dataframe_after_replacement,
    )
    harness.exportRunCsv()

    assert not destination.exists()
    assert len(harness.loaded_datasets) == 1
    assert close_calls == Counter({id(harness.loaded_datasets[0]): 1})
    _assert_replacement_was_rejected(
        harness,
        source,
        destination,
        replacement_states,
    )


def test_run_csv_replacement_immediately_before_publication_preserves_sentinel(
        tmp_path,
        monkeypatch,
        ):
    (
        harness,
        source,
        destination,
        replace_source,
        replacement_states,
    ) = _replacement_fixture(tmp_path)
    sentinel = b"existing destination sentinel"
    destination.write_bytes(sentinel)
    close_calls = _track_dataset_closes(monkeypatch)
    original_atomic_write = plot_actions_module.write_export_atomically

    def replace_in_before_publish(destination_transaction, writer, *, before_publish):
        def validate_replacement():
            replace_source()
            before_publish()

        return original_atomic_write(
            destination_transaction,
            writer,
            before_publish=validate_replacement,
        )

    monkeypatch.setattr(
        plot_actions_module,
        "write_export_atomically",
        replace_in_before_publish,
    )
    harness.exportRunCsv()

    assert destination.read_bytes() == sentinel
    assert len(harness.loaded_datasets) == 1
    assert close_calls == Counter({id(harness.loaded_datasets[0]): 1})
    _assert_replacement_was_rejected(
        harness,
        source,
        destination,
        replacement_states,
    )


def test_run_csv_unchanged_source_exports_and_closes_dataset_once(
        tmp_path,
        monkeypatch,
        ):
    source = tmp_path / "source.db"
    run_id, guid = _create_run(source, 2)
    destination = tmp_path / "export.csv"
    harness = _RunCsvHarness(source, destination, run_id, guid)
    source_before = _artifact_state(source)
    close_calls = _track_dataset_closes(monkeypatch)

    harness.exportRunCsv()

    assert destination.read_text().splitlines() == [
        "signal,x",
        "10.0,0.0",
        "11.0,1.0",
    ]
    assert len(harness.loaded_datasets) == 1
    assert close_calls == Counter({id(harness.loaded_datasets[0]): 1})
    assert harness.reload_requests == []
    assert harness.errors == []
    assert "exported csv" in harness.status_messages[-1][0].lower()
    assert _artifact_state(source) == source_before
    _assert_no_staging_artifacts(destination)


def test_run_csv_dialog_cancellation_opens_no_dataset_and_changes_nothing(
        tmp_path,
        monkeypatch,
        ):
    source = tmp_path / "source.db"
    run_id, guid = _create_run(source, 1)
    destination = tmp_path / "export.csv"
    harness = _RunCsvHarness(source, destination, run_id, guid)
    source_before = _artifact_state(source)
    close_calls = _track_dataset_closes(monkeypatch)
    harness.destination_callback = lambda _default_name: None

    harness.exportRunCsv()

    assert not destination.exists()
    assert harness.loaded_datasets == []
    assert close_calls == Counter()
    assert harness.reload_requests == []
    assert harness.errors == []
    assert "cancelled" in harness.status_messages[-1][0].lower()
    assert _artifact_state(source) == source_before
    _assert_no_staging_artifacts(destination)
