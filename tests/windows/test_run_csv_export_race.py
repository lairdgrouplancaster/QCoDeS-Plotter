"""Real-data CSV exports and database-instance races."""

import csv
import os
import sqlite3
from pathlib import Path

import numpy as np
import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.parameters import ManualParameter

from qplot.datahandling import csv_export as csv_export_module
from qplot.datahandling.export_data import exportable_measurement_params
from qplot.datahandling.file_identity import (
    database_file_identity,
    logical_database_path,
)
from qplot.datahandling.trusted_live_service import TRUSTED_LIVE_MODE
from qplot.windows._database_detail_state import detail_channel
from qplot.windows._dataset_handle import close_dataset_connection
from qplot.windows._export_paths import StagedExport, prepare_export_destination
from qplot.windows.main import MainWindow
from tests._window_lifecycle import close_main_window
from tests.windows.test_plot_integration import configure_temp_qplot, wait_for


class _RunCsvWindow(MainWindow):
    """Exercise the real Qt owner, trusted reader and asynchronous task pool."""

    instances = []

    def __init__(self, database_path, destination, run_id, guid):
        self.destination = destination
        self.destination_callback = None
        self.status_messages = []
        self.errors = []
        self.reload_requests = []
        self.loaded_datasets = []
        super().__init__()
        self.instances.append(self)
        self.startupDatabaseTimer.stop()
        self.monitor.stop()
        self.config.config["user_preference"]["confirm_close"] = False
        self.config.config["user_preference"]["confirm_close_all"] = False
        self.close_database(status=False)
        assert self.load_file(str(database_path))
        wait_for(lambda: (
            not self._database_load_active
            and not detail_channel(self).active
            and not detail_channel(self, True).active
        ))
        self.monitor.stop()
        assert self.selected_run_id == run_id
        assert self.RunList.run_id_for_guid(guid) == run_id
        assert self._database_access_mode == TRUSTED_LIVE_MODE
        self.measurementBox.setText("*")

    def _choose_csv_export_filename(self, default_name):
        if self.destination_callback is not None:
            destination = self.destination_callback(default_name)
            if destination is None:
                self.show_status("CSV export cancelled.", 3000)
            return destination
        return prepare_export_destination(
            self, str(self.destination),
            replacement_confirmed=self.destination.exists(),
        )

    def _load_run_csv_dataset(self, dataset_key):
        dataset = super()._load_run_csv_dataset(dataset_key)
        self.loaded_datasets.append(dataset)
        return dataset

    def _reload_replaced_database(self, database_path, **_kwargs):
        # Observe recovery requests without racing a second UI load against
        # assertions about the replacement's untouched on-disk contents.
        self.reload_requests.append(str(database_path))

    def show_status(self, message, timeout=5000):
        self.status_messages.append((message, timeout))

    def show_error(self, title, message, details=None):
        self.errors.append((title, message, details))

    def wait_export(self):
        manager = getattr(self, "_export_task_manager", None)
        assert manager is not None, (self.errors, self.status_messages)
        wait_for(lambda: not manager.tasks)
        return manager.history[-1]


@pytest.fixture(autouse=True)
def isolated_export_windows(monkeypatch, tmp_path):
    configure_temp_qplot(monkeypatch, tmp_path)
    _RunCsvWindow.instances = []
    yield
    for window in _RunCsvWindow.instances:
        close_main_window(window)
    _RunCsvWindow.instances = []


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
    harness = _RunCsvWindow(database_path, destination, run_id, guid)
    harness.RunList._item_for_guid(guid).run_metadata["measure_parameters"] = names
    harness.measurementBox.setText(selection)
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
        assert request[1] == (None if selection == "*" else int(selection))
        harness.exportRunCsv()
        assert harness.wait_export().error is None
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


@pytest.mark.parametrize("selection", ("*", "2"))
def test_trusted_csv_export_recovers_standalone_before_metadata_finishes(
        tmp_path, monkeypatch, selection,
        ):
    database_path = tmp_path / "progressive.db"
    run_id, guid = _create_measurement_run(database_path, "mixed")
    destination = tmp_path / "progressive.csv"
    harness = _RunCsvWindow(database_path, destination, run_id, guid)
    harness.RunList._item_for_guid(guid).run_metadata["measure_parameters"] = ["signal"]
    harness.measurementBox.setText(selection)
    captures = []
    original_capture = harness._trusted_read_service.submit_plot_dataset

    def record_capture(*args, **kwargs):
        captures.append(True)
        return original_capture(*args, **kwargs)

    monkeypatch.setattr(harness._trusted_read_service, "submit_plot_dataset", record_capture)

    def choose_before_capture(_default_name):
        assert captures == []
        assert harness.loaded_datasets == []
        return prepare_export_destination(harness, str(destination))

    harness.destination_callback = choose_before_capture
    harness.exportRunCsv()
    assert harness.wait_export().error is None
    assert captures
    assert harness.loaded_datasets == []
    assert harness.errors == []
    with destination.open(newline="", encoding="utf-8") as csv_file:
        rows = list(csv.reader(csv_file))
    if selection == "2":
        assert rows == [["temperature"], ["20.0"], ["21.0"]]
    else:
        assert rows == [
            ["signal.signal", "signal.x", "temperature.temperature"],
            ["10.0", "0.0", "20.0"],
            ["11.0", "1.0", "21.0"],
            ["12.0", "2.0", ""],
        ]


def _artifact_state(database_path: Path):
    state = {}
    for suffix in ("", "-wal", "-journal"):
        path = Path(f"{database_path}{suffix}")
        if not path.exists():
            state[suffix] = None
            continue
        state[suffix] = {
            "identity": database_file_identity(path),
            "bytes": path.read_bytes(),
        }
    return state


@pytest.mark.parametrize(
    "dtype,values",
    (
        (np.uint64, [9007199254740993, 9007199254740995]),
        (np.int64, [-9007199254740993, -9007199254740995]),
        (np.uint64, [2**64 - 2, 2**64 - 1]),
        (np.int64, [-(2**63), 2**63 - 1]),
    ),
    ids=("uint64-above-float-range", "negative-int64", "uint64-max", "int64-limits"),
)
@pytest.mark.parametrize("lengths", ("z1-shorter", "equal", "z2-shorter"))
@pytest.mark.parametrize("selection", ("*", "1"))
def test_run_csv_preserves_exact_array_integers(
        tmp_path, dtype, values, lengths, selection
        ):
    source = tmp_path / "integers.db"
    initialise_or_create_database_at(str(source), journal_mode="DELETE")
    experiment = load_or_create_experiment("integer_csv", sample_name="sample")
    measurement = Measurement(exp=experiment, name="integer_csv")
    z1 = ManualParameter("z1")
    z2 = ManualParameter("z2")
    measurement.register_parameter(z1, paramtype="array")
    measurement.register_parameter(z2, paramtype="array")
    z2_values = [11, 12, 13] if lengths == "z1-shorter" else [11, 12]
    z1_values = values + [7] if lengths == "z2-shorter" else values
    dataset = None
    try:
        with measurement.run(write_in_background=False) as datasaver:
            # Keep a multidimensional raw array to exercise flattening too.
            datasaver.add_result((z1, np.array([z1_values], dtype=dtype)))
            datasaver.add_result((z2, np.array(z2_values, dtype=np.int64)))
            dataset = datasaver.dataset
            run_id, guid = dataset.run_id, dataset.guid
    finally:
        if dataset is not None:
            dataset.conn.close()
        experiment.conn.close()

    protected = _artifact_state(source)
    timestamps = {
        suffix: Path(f"{source}{suffix}").stat().st_mtime_ns
        for suffix, state in protected.items() if state is not None
    }
    destination = tmp_path / "integers.csv"
    harness = _RunCsvWindow(source, destination, run_id, guid)
    descriptor = harness._trusted_read_service.submit_plot_dataset(guid).wait(5)
    # QCoDeS serializes standalone ordering from a frozenset. Match the
    # persisted descriptor, rather than assuming registration order survives.
    names = [parameter.name for parameter in exportable_measurement_params(descriptor)]
    harness.measurementBox.setText(
        "*" if selection == "*" else str(names.index("z1") + 1)
    )

    harness.exportRunCsv()
    task = harness.wait_export()

    assert task.error is None
    assert harness.errors == []
    assert str(destination) in harness.status_messages[-1][0]
    assert harness.loaded_datasets == []
    assert _artifact_state(source) == protected
    assert {
        suffix: Path(f"{source}{suffix}").stat().st_mtime_ns
        for suffix in timestamps
    } == timestamps
    _assert_no_staging_artifacts(destination)
    with destination.open(newline="", encoding="utf-8") as csv_file:
        rows = list(csv.reader(csv_file))
    if selection == "1":
        assert rows == [["z1"], *[[str(value)] for value in z1_values]]
    else:
        expected = {"z1": z1_values, "z2": z2_values}
        assert rows == [
            [f"{name}.{name}" for name in names],
            *[
                [str(expected[name][index]) if index < len(expected[name]) else ""
                 for name in names]
                for index in range(max(len(z1_values), len(z2_values)))
            ],
        ]



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
    harness = _RunCsvWindow(source, destination, run_id, guid)
    harness._selected_run_guid = guid
    if mixed:
        harness.RunList._item_for_guid(guid).run_metadata["measure_parameters"].append("temperature")
    source_before = _artifact_state(source)
    if export_path == "run":
        harness.exportRunCsv()
    elif export_path == "selected_preview":
        harness.export_preview_csv("signal")
    else:
        harness.export_run_preview_csv(guid, "signal")

    assert harness.wait_export().error is None
    assert harness.errors == []
    assert "exported run" in harness.status_messages[-1][0].lower()
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
    harness = _RunCsvWindow(source, destination, run_id, guid)
    replacement_states = []

    def replace_source():
        os.replace(replacement, source)
        replacement_states.append(_artifact_state(source))

    return harness, source, destination, replace_source, replacement_states


def _assert_replacement_was_rejected(
        harness,
        source,
        destination,
        replacement_states,
        ):
    assert replacement_states
    assert _artifact_state(source) == replacement_states[-1]
    assert harness.reload_requests
    assert set(harness.reload_requests) == {logical_database_path(source)}
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
    _assert_replacement_was_rejected(
        harness,
        source,
        destination,
        replacement_states,
    )


def test_run_csv_replacement_during_serialization_rejects_stale_capture(
        tmp_path, monkeypatch,
        ):
    harness, source, destination, replace_source, states = _replacement_fixture(tmp_path)
    original_write = csv_export_module._write_rows

    def write_after_replacement(*args, **kwargs):
        replace_source()
        return original_write(*args, **kwargs)

    monkeypatch.setattr(csv_export_module, "_write_rows", write_after_replacement)
    harness.exportRunCsv()
    assert harness.wait_export().error is not None
    assert not destination.exists()
    assert harness.loaded_datasets == []
    _assert_replacement_was_rejected(harness, source, destination, states)


def test_run_csv_replacement_immediately_before_publication_preserves_sentinel(
        tmp_path, monkeypatch,
        ):
    harness, source, destination, replace_source, states = _replacement_fixture(tmp_path)
    sentinel = b"existing destination sentinel"
    destination.write_bytes(sentinel)
    original_publish = StagedExport.publish

    def replace_before_publish(stage, before_publish=None):
        replace_source()
        return original_publish(stage, before_publish)

    monkeypatch.setattr(StagedExport, "publish", replace_before_publish)
    harness.exportRunCsv()
    assert harness.wait_export().error is not None
    assert destination.read_bytes() == sentinel
    assert harness.loaded_datasets == []
    _assert_replacement_was_rejected(harness, source, destination, states)


def test_run_csv_unchanged_source_exports_without_a_dataset_snapshot(
        tmp_path,
        monkeypatch,
        ):
    source = tmp_path / "source.db"
    run_id, guid = _create_run(source, 2)
    destination = tmp_path / "export.csv"
    harness = _RunCsvWindow(source, destination, run_id, guid)
    source_before = _artifact_state(source)

    harness.exportRunCsv()
    assert harness.wait_export().error is None

    assert destination.read_text().splitlines() == [
        "signal,x",
        "10.0,0.0",
        "11.0,1.0",
    ]
    assert harness.loaded_datasets == []
    assert harness.reload_requests == []
    assert harness.errors == []
    assert "exported run" in harness.status_messages[-1][0].lower()
    assert _artifact_state(source) == source_before
    _assert_no_staging_artifacts(destination)


def test_run_csv_dialog_cancellation_opens_no_dataset_and_changes_nothing(
        tmp_path,
        monkeypatch,
        ):
    source = tmp_path / "source.db"
    run_id, guid = _create_run(source, 1)
    destination = tmp_path / "export.csv"
    harness = _RunCsvWindow(source, destination, run_id, guid)
    source_before = _artifact_state(source)
    harness.destination_callback = lambda _default_name: None

    harness.exportRunCsv()

    assert not destination.exists()
    assert harness.loaded_datasets == []
    assert harness.reload_requests == []
    assert harness.errors == []
    assert "cancelled" in harness.status_messages[-1][0].lower()
    assert _artifact_state(source) == source_before
    _assert_no_staging_artifacts(destination)


def test_run_csv_write_failure_preserves_existing_destination(tmp_path, monkeypatch):
    source = tmp_path / "source.db"
    run_id, guid = _create_run(source, 2)
    destination = tmp_path / "export.csv"
    destination.write_text("sentinel", encoding="utf-8")
    harness = _RunCsvWindow(source, destination, run_id, guid)
    protected = _artifact_state(source)

    def fail_write(filename, *_args, **_kwargs):
        Path(filename).write_text("partial", encoding="utf-8")
        raise OSError("disk full")

    monkeypatch.setattr(csv_export_module, "_write_rows", fail_write)
    harness.exportRunCsv()
    task = harness.wait_export()
    assert isinstance(task.error, OSError)
    assert "disk full" in harness.status_messages[-1][0]
    assert destination.read_text(encoding="utf-8") == "sentinel"
    assert _artifact_state(source) == protected
    _assert_no_staging_artifacts(destination)
