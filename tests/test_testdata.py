import csv
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import numpy as np
import pytest
import qcodes as qc
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_by_id,
    load_or_create_experiment,
)
from qcodes.dataset.measurements import DataSaver
from qcodes.dataset.sqlite.connection import AtomicConnection, atomic
from qcodes.parameters import ManualParameter

from qplot import testdata as testdata_module
from qplot.datahandling import readonly as readonly_module
from qplot.datahandling.file_identity import (
    DATABASE_PUBLICATION_GUARD_SUFFIX,
    QPLOT_GENERATION_LINEAGE_RING_TABLE,
    QPLOT_GENERATION_LINEAGE_STATE_TABLE,
    database_file_identity,
    database_has_qplot_generation_marker,
)
from qplot.datahandling.readonly import (
    DatabaseInstanceChangedError,
    ReadOnlyDatabaseAccessError,
    UnverifiableDatabaseWalError,
    qcodes_read_only_connection,
)
from qplot.testdata import (
    CSV_COLUMNS,
    INSTRUCTION_FILE_NAMES,
    GenerationCancelled,
    SpecificationError,
    copy_instruction_collection,
    generate_database_from_csv,
    main,
    read_specifications,
    write_example_csv,
)


def write_specification(path, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        writer.writerows(rows)


def small_specification():
    return testdata_module.RunSpecification(
        1,
        "current",
        "Current",
        "nA",
        -1.0,
        1.0,
        5,
    )


def assert_generated_database(database_path):
    assert database_path.is_file()
    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute("SELECT name FROM runs").fetchall() == [("run_1",)]
    finally:
        connection.close()


def owned_temporary_artifacts(directory):
    return sorted(
        path
        for path in directory.iterdir()
        if (
            path.name.startswith(testdata_module._TEMPORARY_DATABASE_PREFIX)
            or path.name.endswith(DATABASE_PUBLICATION_GUARD_SUFFIX)
        )
    )


def create_all_temporary_sidecars(temporary_path):
    for suffix in testdata_module._SQLITE_SIDECAR_SUFFIXES:
        Path(f"{temporary_path}{suffix}").write_bytes(suffix.encode("ascii"))


def artifact_bytes_and_mtimes(database_path):
    artifacts = {}
    for suffix in ("", *testdata_module._SQLITE_SIDECAR_SUFFIXES):
        artifact_path = Path(f"{database_path}{suffix}")
        if artifact_path.exists():
            contents = artifact_path.read_bytes()
            artifacts[suffix] = (contents, artifact_path.stat().st_mtime_ns)
    return artifacts


def complete_artifact_state(database_path):
    artifacts = {}
    for suffix in ("", *testdata_module._SQLITE_SIDECAR_SUFFIXES):
        artifact_path = Path(f"{database_path}{suffix}")
        try:
            contents = artifact_path.read_bytes()
            status = artifact_path.stat()
        except FileNotFoundError:
            artifacts[suffix] = None
            continue
        artifacts[suffix] = (
            status.st_dev,
            status.st_ino,
            status.st_mode,
            status.st_size,
            status.st_atime_ns,
            status.st_mtime_ns,
            status.st_ctime_ns,
            contents,
        )
    return artifacts


def immutable_run_name(database_path):
    connection = sqlite3.connect(
        f"{Path(database_path).resolve().as_uri()}?mode=ro&immutable=1",
        uri=True,
    )
    try:
        return connection.execute("SELECT name FROM runs").fetchone()[0]
    finally:
        connection.close()


def qplot_run_name(database_path):
    connection = qcodes_read_only_connection(database_path)
    try:
        return connection.execute("SELECT name FROM runs").fetchone()[0]
    finally:
        connection.close()


def qplot_result_values(database_path, table_name, parameter_name):
    quoted_table = testdata_module._quote_sqlite_identifier(table_name)
    quoted_parameter = testdata_module._quote_sqlite_identifier(parameter_name)
    connection = qcodes_read_only_connection(database_path)
    try:
        return [
            row[0]
            for row in connection.execute(
                f"SELECT {quoted_parameter} FROM {quoted_table} ORDER BY id"
            ).fetchall()
        ]
    finally:
        connection.close()


def child_process_qplot_result_values(database_path, table_name, parameter_name):
    child_script = "\n".join(
        (
            "import json",
            "import sys",
            "from qplot.datahandling.readonly import qcodes_read_only_connection",
            "database_path, table_name, parameter_name = sys.argv[1:]",
            "quote = lambda value: '\"' + value.replace('\"', '\"\"') + '\"'",
            "connection = qcodes_read_only_connection(database_path)",
            "try:",
            "    rows = connection.execute(",
            "        f'SELECT {quote(parameter_name)} FROM {quote(table_name)} '",
            "        'ORDER BY id'",
            "    ).fetchall()",
            "    print(json.dumps([row[0] for row in rows]))",
            "finally:",
            "    connection.close()",
        )
    )
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            child_script,
            str(database_path),
            table_name,
            parameter_name,
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(child.stdout)


def future_qcodes_measurement(experiment, name):
    setpoint = ManualParameter(f"{name}_setpoint")
    signal = ManualParameter(f"{name}_signal")
    measurement = Measurement(exp=experiment, name=name)
    measurement.register_parameter(setpoint)
    measurement.register_parameter(signal, setpoints=(setpoint,))
    return measurement, setpoint, signal


def add_and_flush_result(datasaver, setpoint, signal, value):
    datasaver.add_result((setpoint, value), (signal, value * 10.0))
    datasaver.flush_data_to_database(block=True)


def replace_generation_triggers_with_legacy_numeric_format(connection):
    trigger_names = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_schema "
            "WHERE type = 'trigger' AND name LIKE 'qplot_provenance_%'"
        ).fetchall()
    ]
    for trigger_name in trigger_names:
        connection.execute(
            f"DROP TRIGGER {testdata_module._quote_sqlite_identifier(trigger_name)}"
        )

    for lineage_table in (
        QPLOT_GENERATION_LINEAGE_STATE_TABLE,
        QPLOT_GENERATION_LINEAGE_RING_TABLE,
    ):
        connection.execute(
            f"DROP TABLE {testdata_module._quote_sqlite_identifier(lineage_table)}"
        )

    table_names = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_schema "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "AND name != 'qplot_generation_provenance' ORDER BY name"
        ).fetchall()
    ]
    legacy_names = []
    provenance_table = testdata_module._quote_sqlite_identifier(
        "qplot_generation_provenance"
    )
    for table_number, table_name in enumerate(table_names):
        quoted_table = testdata_module._quote_sqlite_identifier(table_name)
        for operation in ("INSERT", "UPDATE", "DELETE"):
            trigger_name = (
                f"qplot_provenance_{table_number}_{operation.lower()}"
            )
            legacy_names.append(trigger_name)
            quoted_trigger = testdata_module._quote_sqlite_identifier(trigger_name)
            connection.execute(
                f"CREATE TRIGGER {quoted_trigger} AFTER {operation} "
                f"ON {quoted_table} BEGIN UPDATE {provenance_table} "
                "SET write_epoch = write_epoch + 1 WHERE singleton = 1; END"
            )
    connection.commit()
    return tuple(legacy_names)


def create_hot_rollback_journal(database_path):
    """Crash a venv subprocess after spilling an uncommitted transaction."""
    crash_script = "\n".join(
        (
            "import os",
            "import sqlite3",
            "import sys",
            "connection = sqlite3.connect(sys.argv[1])",
            "connection.execute('PRAGMA journal_mode = DELETE')",
            "connection.execute('PRAGMA synchronous = FULL')",
            "connection.execute('PRAGMA cache_size = 1')",
            "connection.execute('BEGIN IMMEDIATE')",
            "connection.execute(\"UPDATE runs SET name = 'UNCOMMITTED'\")",
            "connection.execute('CREATE TABLE hot_journal_filler(i, payload)')",
            "for index in range(100):",
            "    connection.execute(",
            "        'INSERT INTO hot_journal_filler VALUES (?, ?)',",
            "        (index, 'x' * 10000),",
            "    )",
            "os._exit(0)",
        )
    )
    subprocess.run(
        [sys.executable, "-c", crash_script, str(database_path)],
        check=True,
        timeout=30,
    )
    journal_path = Path(f"{database_path}-journal")
    journal_contents = journal_path.read_bytes()
    assert journal_contents.startswith(b"\xd9\xd5\x05\xf9 \xa1c\xd7")
    return journal_contents


def test_example_csv_is_ready_to_generate(tmp_path):
    csv_path = tmp_path / "example.csv"

    assert write_example_csv(csv_path) == csv_path
    specifications = read_specifications(csv_path)

    assert csv_path.read_bytes().startswith(b"\xef\xbb\xbf")
    dimensions = [
        specification.dimensions for specification in specifications
    ]
    assert dimensions == [
        1,
        1,
        1,
        1,
        1,
        2,
        2,
    ]
    assert [specification.point_count for specification in specifications] == [
        501,
        501,
        501,
        501,
        501,
        121 * 81,
        121 * 81,
    ]
    with pytest.raises(SpecificationError, match="already exists"):
        write_example_csv(csv_path)


def test_example_cli_reports_written_path(tmp_path, capsys):
    csv_path = tmp_path / "example.csv"

    assert main(["--write-example", str(csv_path)]) == 0

    assert csv_path.is_file()
    assert f"Wrote example CSV: {csv_path}" in capsys.readouterr().out


def test_instruction_collection_is_cumulative_and_spans_10mb_to_30gb(tmp_path):
    output_paths = copy_instruction_collection(tmp_path)

    assert tuple(path.name for path in output_paths) == INSTRUCTION_FILE_NAMES
    specification_sets = [read_specifications(path) for path in output_paths]
    assert [len(specifications) for specifications in specification_sets] == list(
        range(10, 38, 3)
    )

    one_dimensional = [
        specification
        for specification in specification_sets[0]
        if specification.dimensions == 1
    ]
    assert [specification.measured_name for specification in one_dimensional] == [
        "current",
        "conductance",
        "resistance",
        "transconductance",
        "current",
    ]
    assert {
        (
            specification.v_sd_start,
            specification.v_sd_stop,
            specification.v_sd_points,
        )
        for specification in one_dimensional
    } == {(-0.1, 0.1, 1001)}

    two_dimensional = [
        specification
        for specification in specification_sets[0]
        if specification.dimensions == 2
    ]
    assert two_dimensional[:2] == [
        two_dimensional[0],
        two_dimensional[0],
    ]

    for predecessor, successor in zip(
        specification_sets[:-1],
        specification_sets[1:],
        strict=True,
    ):
        assert successor[: len(predecessor)] == predecessor

    largest_runs = [
        max(specifications, key=lambda specification: specification.point_count)
        for specifications in specification_sets
    ]
    assert (largest_runs[0].v_sd_points, largest_runs[0].v_g_points) == (201, 301)
    assert (largest_runs[-1].v_sd_points, largest_runs[-1].v_g_points) == (
        11001,
        17001,
    )

    # Calibrated on a generated QCoDeS database using the first collection file.
    estimated_bytes_per_point = 35.132
    estimated_sizes = [
        sum(specification.point_count for specification in specifications)
        * estimated_bytes_per_point
        for specifications in specification_sets
    ]
    assert 8_000_000 <= estimated_sizes[0] <= 12_000_000
    assert 27_000_000_000 <= estimated_sizes[-1] <= 33_000_000_000
    assert all(
        successor > predecessor
        for predecessor, successor in zip(
            estimated_sizes[:-1],
            estimated_sizes[1:],
            strict=True,
        )
    )


def test_instruction_collection_cli_exports_all_files(tmp_path, capsys):
    output_directory = tmp_path / "collection"

    assert main(["--write-collection", str(output_directory)]) == 0

    assert tuple(path.name for path in sorted(output_directory.iterdir())) == (
        INSTRUCTION_FILE_NAMES
    )
    assert "Wrote 10 instruction CSV files" in capsys.readouterr().out

    with pytest.raises(SystemExit) as error:
        main(["--write-collection", str(output_directory)])
    assert error.value.code == 2

    assert (
        main(["--write-collection", str(output_directory), "--overwrite"])
        == 0
    )


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (
            ("3", "current", "Current", "nA", "-1", "1", "11", "", "", ""),
            "dimensions must be 1 or 2",
        ),
        (
            ("1", "V_SD", "Current", "nA", "-1", "1", "11", "", "", ""),
            "measured_name cannot be V_SD or V_G",
        ),
        (
            ("1", "id", "Current", "nA", "-1", "1", "11", "", "", ""),
            "measured_name cannot be used as a QCoDeS result column",
        ),
        (
            ("1", "select", "Current", "nA", "-1", "1", "11", "", "", ""),
            "measured_name cannot be used as a QCoDeS result column",
        ),
        (
            ("1", "current", "Current", "nA", "-1", "1", "11", "-2", "2", "5"),
            "v_g_start, v_g_stop, and v_g_points must be blank for a 1D run",
        ),
        (
            ("2", "current", "Current", "nA", "-1", "1", "11", "", "2", "5"),
            "v_g_start is required",
        ),
    ],
)
def test_invalid_rows_report_the_csv_row(tmp_path, row, message):
    csv_path = tmp_path / "invalid.csv"
    write_specification(csv_path, [row])

    with pytest.raises(SpecificationError, match=f"CSV row 2: {message}"):
        read_specifications(csv_path)


def test_generate_database_creates_named_two_sinusoid_runs(tmp_path):
    csv_path = tmp_path / "runs.csv"
    database_path = tmp_path / "runs.db"
    write_specification(
        csv_path,
        [
            ("1", "current", "Current", "nA", "-2", "2", "5", "", "", ""),
            (
                "2",
                "conductance",
                "Conductance",
                "uS",
                "-0.1",
                "0.1",
                "3",
                "-1",
                "1",
                "4",
            ),
        ],
    )

    random_seed = 1234
    generated_path, specifications = generate_database_from_csv(
        csv_path,
        database_path,
        rng=np.random.default_rng(random_seed),
    )

    assert generated_path == database_path
    assert database_path.is_file()
    assert [specification.point_count for specification in specifications] == [5, 12]

    previous_database_path = qc.config["core"]["db_location"]
    try:
        initialise_or_create_database_at(database_path, journal_mode="DELETE")
        line_run = load_by_id(1)
        map_run = load_by_id(2)

        assert line_run.name == "run_1"
        assert map_run.name == "run_2"
        assert line_run.completed
        assert map_run.completed

        line_parameters = {parameter.name: parameter for parameter in line_run.get_parameters()}
        assert set(line_parameters) == {"V_SD", "current"}
        assert line_parameters["current"].label == "Current"
        assert line_parameters["current"].unit == "nA"
        assert line_parameters["current"].depends_on_ == ["V_SD"]

        line_data = line_run.get_parameter_data("current")["current"]
        np.testing.assert_allclose(line_data["V_SD"], np.linspace(-2.0, 2.0, 5))
        expected_generator = np.random.default_rng(random_seed)
        line_components = [
            (
                expected_generator.uniform(0.5, 1.5),
                expected_generator.uniform(0.5, 4.0),
                expected_generator.uniform(0.0, 2.0 * np.pi),
            )
            for _ in range(2)
        ]
        line_normalized = np.linspace(0.0, 1.0, 5)
        expected_line_values = sum(
            amplitude
            * np.sin(2.0 * np.pi * frequency * line_normalized + phase)
            for amplitude, frequency, phase in line_components
        )
        np.testing.assert_allclose(
            line_data["current"],
            expected_line_values,
            atol=1e-12,
        )

        map_parameters = {parameter.name: parameter for parameter in map_run.get_parameters()}
        assert set(map_parameters) == {"V_SD", "V_G", "conductance"}
        assert map_parameters["conductance"].depends_on_ == ["V_SD", "V_G"]
        map_data = map_run.get_parameter_data("conductance")["conductance"]
        assert map_data["conductance"].size == 12
        np.testing.assert_allclose(
            map_data["V_SD"],
            np.repeat(np.linspace(-0.1, 0.1, 3), 4),
        )
        np.testing.assert_allclose(
            map_data["V_G"],
            np.tile(np.linspace(-1.0, 1.0, 4), 3),
        )
        map_components = [
            (
                expected_generator.uniform(0.5, 1.5),
                expected_generator.uniform(0.5, 4.0),
                expected_generator.uniform(0.5, 4.0),
                expected_generator.uniform(0.0, 2.0 * np.pi),
            )
            for _ in range(2)
        ]
        normalized_v_sd = np.repeat(np.linspace(0.0, 1.0, 3), 4)
        normalized_v_g = np.tile(np.linspace(0.0, 1.0, 4), 3)
        expected_map_values = sum(
            amplitude
            * np.sin(
                2.0
                * np.pi
                * (v_sd_frequency * normalized_v_sd + v_g_frequency * normalized_v_g)
                + phase
            )
            for amplitude, v_sd_frequency, v_g_frequency, phase in map_components
        )
        np.testing.assert_allclose(
            map_data["conductance"],
            expected_map_values,
            atol=1e-12,
        )
        assert np.isfinite(map_data["conductance"]).all()
        assert np.min(map_data["conductance"]) >= -3.0
        assert np.max(map_data["conductance"]) <= 3.0
    finally:
        qc.config["core"]["db_location"] = previous_database_path


def test_generate_database_writes_bounded_result_chunks(
    tmp_path,
    monkeypatch,
    capsys,
):
    csv_path = tmp_path / "runs.csv"
    database_path = tmp_path / "runs.db"
    write_specification(
        csv_path,
        [
            ("1", "current", "Current", "nA", "-1", "1", "5", "", "", ""),
            (
                "2",
                "conductance",
                "Conductance",
                "uS",
                "-0.1",
                "0.1",
                "2",
                "-1",
                "1",
                "4",
            ),
        ],
    )
    original_add_result = DataSaver.add_result
    result_sizes = []

    def record_add_result(datasaver, *result_tuples):
        sizes = tuple(np.asarray(value).size for _, value in result_tuples)
        assert len(set(sizes)) == 1
        result_sizes.append(sizes[0])
        return original_add_result(datasaver, *result_tuples)

    monkeypatch.setattr("qplot.testdata._RESULT_CHUNK_POINTS", 3)
    monkeypatch.setattr(DataSaver, "add_result", record_add_result)

    generate_database_from_csv(csv_path, database_path)

    assert result_sizes == [3, 2, 3, 1, 3, 1]
    output_lines = [line.strip() for line in capsys.readouterr().out.splitlines()]
    assert "Starting experimental run with id: 1." in output_lines
    assert "Starting experimental run with id: 2." in output_lines
    timestamped_lines = [line for line in output_lines if line.startswith("[")]
    assert len(timestamped_lines) == 6
    assert all(
        re.match(
            r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}\] ",
            line,
        )
        for line in timestamped_lines
    )
    messages = [line.split("] ", maxsplit=1)[1] for line in timestamped_lines]
    assert messages[0].startswith("Test database generation started:")
    assert messages[1].startswith("Run started: run_1")
    assert messages[2].startswith("Run stopped (completed): run_1")
    assert messages[3].startswith("Run started: run_2")
    assert messages[4].startswith("Run stopped (completed): run_2")
    assert messages[5].startswith("Test database generation stopped (completed)")


def test_cancellation_during_batched_run_removes_temporary_database(
    tmp_path,
    monkeypatch,
    capsys,
):
    csv_path = tmp_path / "runs.csv"
    database_path = tmp_path / "runs.db"
    write_specification(
        csv_path,
        [("1", "current", "Current", "nA", "-1", "1", "10", "", "", "")],
    )
    cancellation_checks = 0

    def cancelled():
        nonlocal cancellation_checks
        cancellation_checks += 1
        return cancellation_checks == 3

    monkeypatch.setattr("qplot.testdata._RESULT_CHUNK_POINTS", 3)

    with pytest.raises(GenerationCancelled):
        generate_database_from_csv(
            csv_path,
            database_path,
            cancelled_callback=cancelled,
        )

    assert not database_path.exists()
    assert owned_temporary_artifacts(tmp_path) == []
    output = capsys.readouterr().out
    assert "Run stopped (cancelled): run_1" in output
    assert "Test database generation stopped (cancelled)" in output


def test_generation_failure_reports_timestamped_stop_and_removes_temporary_database(
    tmp_path,
    monkeypatch,
    capsys,
):
    csv_path = tmp_path / "runs.csv"
    database_path = tmp_path / "runs.db"
    write_specification(
        csv_path,
        [("1", "current", "Current", "nA", "-1", "1", "5", "", "", "")],
    )

    def fail_run(*args, **kwargs):
        raise RuntimeError("deliberate failure")

    monkeypatch.setattr("qplot.testdata._write_run", fail_run)

    with pytest.raises(RuntimeError, match="deliberate failure"):
        generate_database_from_csv(csv_path, database_path)

    assert not database_path.exists()
    assert owned_temporary_artifacts(tmp_path) == []
    output = capsys.readouterr().out
    assert "Run stopped (failed): run_1" in output
    assert "RuntimeError: deliberate failure" in output
    assert "Test database generation stopped (failed)" in output


def test_generation_rejects_silently_missing_result_rows(
    tmp_path,
    monkeypatch,
):
    csv_path = tmp_path / "runs.csv"
    database_path = tmp_path / "runs.db"
    write_specification(
        csv_path,
        [("1", "current", "Current", "nA", "-1", "1", "5", "", "", "")],
    )

    monkeypatch.setattr(DataSaver, "add_result", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="persisted 0 of 5 expected result rows"):
        generate_database_from_csv(csv_path, database_path)

    assert not database_path.exists()
    assert owned_temporary_artifacts(tmp_path) == []


def test_generate_database_requires_explicit_overwrite(tmp_path):
    csv_path = tmp_path / "runs.csv"
    database_path = tmp_path / "runs.db"
    write_specification(
        csv_path,
        [("1", "current", "Current", "nA", "-1", "1", "5", "", "", "")],
    )
    generate_database_from_csv(csv_path, database_path)

    with pytest.raises(SpecificationError, match="use --overwrite"):
        generate_database_from_csv(csv_path, database_path)

    generated_path, _ = generate_database_from_csv(
        csv_path, database_path, overwrite=True
    )
    assert generated_path == database_path


def test_no_overwrite_publish_does_not_clobber_concurrently_created_file(
    tmp_path,
    monkeypatch,
):
    csv_path = tmp_path / "runs.csv"
    database_path = tmp_path / "runs.db"
    write_specification(
        csv_path,
        [("1", "current", "Current", "nA", "-1", "1", "5", "", "", "")],
    )
    original_write_run = testdata_module._write_run

    def write_run_after_competitor_arrives(*args, **kwargs):
        points_written = original_write_run(*args, **kwargs)
        database_path.write_bytes(b"concurrent owner")
        return points_written

    monkeypatch.setattr(testdata_module, "_write_run", write_run_after_competitor_arrives)

    with pytest.raises(SpecificationError, match="already exists"):
        generate_database_from_csv(csv_path, database_path)

    assert database_path.read_bytes() == b"concurrent owner"
    assert owned_temporary_artifacts(tmp_path) == []


_RESERVED_PATH_COMPONENTS = (
    pytest.param("hash#scan", id="hash"),
    pytest.param("question?scan", id="question"),
    pytest.param("percent%scan", id="literal-percent"),
    pytest.param("escape%3fscan", id="literal-percent-3f"),
    pytest.param("escape%23scan", id="literal-percent-23"),
    pytest.param("space scan", id="space"),
    pytest.param("unicode-測定", id="unicode"),
)


@pytest.mark.parametrize("component", _RESERVED_PATH_COMPONENTS)
@pytest.mark.parametrize("location", ("filename", "parent"))
def test_generate_database_publishes_exact_reserved_path(
    tmp_path,
    component,
    location,
):
    if os.name == "nt" and "?" in component:
        pytest.skip("Windows does not support '?' in filesystem path components")

    if location == "filename":
        database_path = tmp_path / f"{component}.db"
    else:
        output_directory = tmp_path / component
        output_directory.mkdir()
        database_path = output_directory / "runs.db"

    generated_path = testdata_module.generate_database(
        [small_specification()],
        database_path,
    )

    assert generated_path == database_path
    assert_generated_database(database_path)
    assert owned_temporary_artifacts(database_path.parent) == []


@pytest.mark.parametrize("overwrite", [False, True], ids=["new", "replacement"])
def test_publication_callback_observes_committed_database(tmp_path, overwrite):
    database_path = tmp_path / "publication-callback.db"
    if overwrite:
        testdata_module.generate_database([small_specification()], database_path)
    original_identity = database_file_identity(database_path)
    published_identities = []

    generated_path = testdata_module.generate_database(
        [small_specification()],
        database_path,
        overwrite=overwrite,
        publication_callback=lambda: published_identities.append(
            database_file_identity(database_path)
        ),
    )

    assert generated_path == database_path
    assert published_identities == [database_file_identity(database_path)]
    assert published_identities[0] is not None
    assert published_identities[0] != original_identity
    assert_generated_database(database_path)


def test_qcodes_uri_name_has_exactly_one_encoding_layer(tmp_path):
    database_path = (
        tmp_path
        / "parent # ? % %3f %23 space 測定"
        / "out # ? % %3f %23 space Δ.db"
    )

    uri_name = testdata_module._qcodes_uri_name(database_path)

    assert f"file:{uri_name}" == database_path.absolute().as_uri()
    assert "%23" in uri_name
    assert "%3F" in uri_name
    assert "%25" in uri_name
    assert "%253f" in uri_name
    assert "%2523" in uri_name
    assert "%20" in uri_name
    assert "%E6%B8%AC%E5%AE%9A" in uri_name


@pytest.mark.parametrize(
    ("exact_name", "historical_name"),
    (
        pytest.param("exact#scan.db", "exact", id="fragment-truncation"),
        pytest.param("exact?scan.db", "exact", id="query-truncation"),
        pytest.param("exact%3fscan.db", "exact?scan.db", id="decoded-question"),
        pytest.param("exact%23scan.db", "exact#scan.db", id="decoded-hash"),
    ),
)
def test_exact_writable_connection_preserves_historical_sentinel(
    tmp_path,
    exact_name,
    historical_name,
):
    if os.name == "nt" and "?" in (exact_name + historical_name):
        pytest.skip("Windows does not support '?' in filesystem path components")

    exact_path = tmp_path / exact_name
    historical_path = tmp_path / historical_name
    sentinel = sqlite3.connect(historical_path)
    try:
        sentinel.execute("CREATE TABLE sentinel(value TEXT)")
        sentinel.execute("INSERT INTO sentinel VALUES ('unchanged')")
        sentinel.commit()
    finally:
        sentinel.close()
    before = historical_path.read_bytes()

    connection = testdata_module._connect_writable_exact_path(exact_path)
    try:
        assert isinstance(connection, AtomicConnection)
        assert connection.path_to_dbfile == str(exact_path.resolve())
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'runs'"
        ).fetchone() == ("runs",)
        assert connection.execute("PRAGMA user_version").fetchone()[0] > 0
        expected_array = np.array([1.25, 2.5])
        connection.execute("CREATE TABLE converter_probe(payload array)")
        connection.execute("INSERT INTO converter_probe VALUES (?)", (expected_array,))
        converted_array = connection.execute(
            "SELECT payload FROM converter_probe"
        ).fetchone()[0]
        np.testing.assert_array_equal(converted_array, expected_array)
    finally:
        connection.close()

    assert exact_path.is_file()
    assert historical_path.read_bytes() == before


def test_generator_does_not_modify_historically_truncated_sentinel(tmp_path):
    database_path = tmp_path / "out#scan.db"
    historical_path = tmp_path / ".out"
    sentinel = sqlite3.connect(historical_path)
    try:
        sentinel.execute("CREATE TABLE sentinel(value TEXT)")
        sentinel.execute("INSERT INTO sentinel VALUES ('unchanged')")
        sentinel.commit()
    finally:
        sentinel.close()
    before = historical_path.read_bytes()

    testdata_module.generate_database([small_specification()], database_path)

    assert_generated_database(database_path)
    assert historical_path.read_bytes() == before


def test_connection_failure_removes_all_owned_temporary_sidecars(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "parent#scan" / "out%23scan.db"
    temporary_paths = []

    def fail_connection(temporary_path):
        temporary_path = Path(temporary_path)
        temporary_paths.append(temporary_path)
        create_all_temporary_sidecars(temporary_path)
        raise RuntimeError("injected connection failure")

    monkeypatch.setattr(
        testdata_module,
        "_connect_writable_exact_path",
        fail_connection,
    )

    with pytest.raises(RuntimeError, match="injected connection failure"):
        testdata_module.generate_database([small_specification()], database_path)

    assert len(temporary_paths) == 1
    assert temporary_paths[0].name.startswith(
        testdata_module._TEMPORARY_DATABASE_PREFIX
    )
    assert "out" not in temporary_paths[0].name
    assert not database_path.exists()
    assert owned_temporary_artifacts(database_path.parent) == []


def test_generation_failure_removes_all_owned_temporary_sidecars(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "runs.db"
    temporary_paths = []
    original_connect = testdata_module._connect_writable_exact_path

    def record_connection(temporary_path):
        temporary_paths.append(Path(temporary_path))
        return original_connect(temporary_path)

    def fail_generation(*_args, **_kwargs):
        create_all_temporary_sidecars(temporary_paths[0])
        raise RuntimeError("injected generation failure")

    monkeypatch.setattr(
        testdata_module,
        "_connect_writable_exact_path",
        record_connection,
    )
    monkeypatch.setattr(testdata_module, "_write_run", fail_generation)

    with pytest.raises(RuntimeError, match="injected generation failure"):
        testdata_module.generate_database([small_specification()], database_path)

    assert not database_path.exists()
    assert owned_temporary_artifacts(tmp_path) == []


def test_prepublication_failure_does_not_call_publication_callback(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "prepublication-failure.db"
    publication_callbacks = []

    def fail_generation(*_args, **_kwargs):
        raise RuntimeError("injected prepublication failure")

    monkeypatch.setattr(testdata_module, "_write_run", fail_generation)

    with pytest.raises(RuntimeError, match="injected prepublication failure"):
        testdata_module.generate_database(
            [small_specification()],
            database_path,
            publication_callback=lambda: publication_callbacks.append(True),
        )

    assert publication_callbacks == []
    assert not database_path.exists()
    assert owned_temporary_artifacts(tmp_path) == []


def test_publication_callback_precedes_error_after_real_publisher_returns(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "post-publication-error.db"
    testdata_module.generate_database([small_specification()], database_path)
    original_identity = database_file_identity(database_path)
    original_publish = testdata_module._publish_database
    events = []

    def publish_then_fail(*args, **kwargs):
        original_publish(*args, **kwargs)
        events.append("publisher returned")
        raise RuntimeError("injected post-publication failure")

    monkeypatch.setattr(testdata_module, "_publish_database", publish_then_fail)

    with pytest.raises(RuntimeError, match="injected post-publication failure"):
        testdata_module.generate_database(
            [small_specification()],
            database_path,
            overwrite=True,
            publication_callback=lambda: events.append("published"),
        )

    assert events == ["published", "publisher returned"]
    assert database_file_identity(database_path) != original_identity
    assert_generated_database(database_path)
    assert owned_temporary_artifacts(tmp_path) == []


def test_publication_failure_removes_all_owned_temporary_sidecars(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "runs.db"

    def fail_publication(
        temporary_path,
        _database_path,
        _overwrite,
        _expected_destination_state,
    ):
        create_all_temporary_sidecars(temporary_path)
        raise RuntimeError("injected publication failure")

    monkeypatch.setattr(testdata_module, "_publish_database", fail_publication)

    with pytest.raises(RuntimeError, match="injected publication failure"):
        testdata_module.generate_database([small_specification()], database_path)

    assert not database_path.exists()
    assert owned_temporary_artifacts(tmp_path) == []


def test_publication_guard_close_failure_removes_created_guard(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "guard-close-failure.db"
    guard_path = Path(f"{database_path}{DATABASE_PUBLICATION_GUARD_SUFFIX}")
    original_close = os.close

    def close_then_report_failure(descriptor):
        original_close(descriptor)
        raise OSError("injected close failure")

    monkeypatch.setattr(testdata_module.os, "close", close_then_report_failure)

    with pytest.raises(
        SpecificationError,
        match="database active or SQLite sidecars present",
    ):
        testdata_module._create_publication_guard(database_path)

    assert not guard_path.exists()


def test_cancellation_removes_all_owned_temporary_sidecars(tmp_path):
    database_path = tmp_path / "runs.db"

    def cancel_with_sidecars():
        temporary_path = owned_temporary_artifacts(tmp_path)[0]
        create_all_temporary_sidecars(temporary_path)
        return True

    with pytest.raises(GenerationCancelled):
        testdata_module.generate_database(
            [small_specification()],
            database_path,
            cancelled_callback=cancel_with_sidecars,
        )

    assert not database_path.exists()
    assert owned_temporary_artifacts(tmp_path) == []


def test_overwrite_publish_rejects_concurrently_created_file(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "runs.db"
    original_write_run = testdata_module._write_run

    def write_run_after_competitor_arrives(*args, **kwargs):
        points_written = original_write_run(*args, **kwargs)
        database_path.write_bytes(b"concurrent owner")
        return points_written

    monkeypatch.setattr(
        testdata_module,
        "_write_run",
        write_run_after_competitor_arrives,
    )

    with pytest.raises(
        SpecificationError,
        match="database active or SQLite sidecars present",
    ):
        testdata_module.generate_database(
            [small_specification()],
            database_path,
            overwrite=True,
        )

    assert database_path.read_bytes() == b"concurrent owner"
    assert owned_temporary_artifacts(tmp_path) == []


def test_overwrite_rejects_active_wal_without_changing_destination(tmp_path):
    database_path = tmp_path / "active-wal.db"
    testdata_module.generate_database([small_specification()], database_path)

    writer = testdata_module._connect_writable_exact_path(database_path)
    try:
        assert isinstance(writer, AtomicConnection)
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("UPDATE runs SET name = 'OLD_WAL'")
        writer.commit()

        assert immutable_run_name(database_path) == "run_1"
        before = artifact_bytes_and_mtimes(database_path)
        assert set(before) == {"", "-wal", "-shm"}

        with pytest.raises(
            SpecificationError,
            match="database active or SQLite sidecars present",
        ):
            testdata_module.generate_database(
                [small_specification()],
                database_path,
                overwrite=True,
            )

        assert artifact_bytes_and_mtimes(database_path) == before
        assert owned_temporary_artifacts(tmp_path) == []

        # A private ordinary SQLite copy proves the preserved WAL carries the
        # later commit. Its generation token and advanced write epoch let qPlot
        # safely expose that same committed value.
        probe_directory = tmp_path / "active-wal-probe"
        probe_directory.mkdir()
        probe_path = probe_directory / "probe.db"
        shutil.copyfile(database_path, probe_path)
        shutil.copyfile(Path(f"{database_path}-wal"), Path(f"{probe_path}-wal"))
        probe = sqlite3.connect(probe_path)
        try:
            assert probe.execute("SELECT name FROM runs").fetchone()[0] == "OLD_WAL"
        finally:
            probe.close()
        assert qplot_run_name(database_path) == "OLD_WAL"
        assert artifact_bytes_and_mtimes(database_path) == before
    finally:
        writer.close()


def test_overwrite_rejects_rollback_journal_without_changing_destination(tmp_path):
    database_path = tmp_path / "active-journal.db"
    testdata_module.generate_database([small_specification()], database_path)

    writer = sqlite3.connect(database_path)
    try:
        assert writer.execute("PRAGMA journal_mode = DELETE").fetchone()[0] == "delete"
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE runs SET name = 'UNCOMMITTED'")
        journal_path = Path(f"{database_path}-journal")
        assert journal_path.is_file()
        before = artifact_bytes_and_mtimes(database_path)
        assert set(before) == {"", "-journal"}

        with pytest.raises(
            SpecificationError,
            match="database active or SQLite sidecars present",
        ):
            testdata_module.generate_database(
                [small_specification()],
                database_path,
                overwrite=True,
            )

        assert artifact_bytes_and_mtimes(database_path) == before
        assert owned_temporary_artifacts(tmp_path) == []
    finally:
        writer.rollback()
        writer.close()


def test_overwrite_does_not_recover_hot_journal_racing_during_lock(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "hot-journal-race.db"
    journal_source_path = tmp_path / "hot-journal-source.db"
    journal_path = Path(f"{database_path}-journal")
    testdata_module.generate_database([small_specification()], database_path)
    shutil.copyfile(database_path, journal_source_path)
    parked_journal = create_hot_rollback_journal(journal_source_path)
    main_before = artifact_bytes_and_mtimes(database_path)[""]
    original_connect = sqlite3.connect
    injected_journal = []

    def connect_after_hot_journal_arrives(database, *args, **kwargs):
        if ".backup?mode=rw" in str(database) and not injected_journal:
            journal_path.write_bytes(parked_journal)
            controlled_mtime_ns = 1_700_000_000_000_000_000
            os.utime(
                journal_path,
                ns=(controlled_mtime_ns, controlled_mtime_ns),
            )
            injected_journal.append(
                (journal_path.read_bytes(), journal_path.stat().st_mtime_ns)
            )
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(
        testdata_module.sqlite3,
        "connect",
        connect_after_hot_journal_arrives,
    )

    with pytest.raises(
        SpecificationError,
        match="database active or SQLite sidecars present",
    ):
        testdata_module.generate_database(
            [small_specification()],
            database_path,
            overwrite=True,
        )

    assert len(injected_journal) == 1
    after = artifact_bytes_and_mtimes(database_path)
    assert after[""] == main_before
    assert after["-journal"] == injected_journal[0]
    assert owned_temporary_artifacts(tmp_path) == []


def test_overwrite_rejects_writer_before_rollback_journal_exists(tmp_path):
    database_path = tmp_path / "reserved-writer.db"
    testdata_module.generate_database([small_specification()], database_path)

    writer = sqlite3.connect(database_path)
    try:
        writer.execute("BEGIN IMMEDIATE")
        assert not Path(f"{database_path}-journal").exists()
        before = artifact_bytes_and_mtimes(database_path)

        with pytest.raises(
            SpecificationError,
            match="database active or SQLite sidecars present",
        ):
            testdata_module.generate_database(
                [small_specification()],
                database_path,
                overwrite=True,
            )

        assert artifact_bytes_and_mtimes(database_path) == before
        assert owned_temporary_artifacts(tmp_path) == []
    finally:
        writer.rollback()
        writer.close()


def test_overwrite_rejects_quiescent_wal_mode_without_creating_sidecars(tmp_path):
    database_path = tmp_path / "quiescent-wal.db"
    testdata_module.generate_database([small_specification()], database_path)
    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
    finally:
        connection.close()
    before = artifact_bytes_and_mtimes(database_path)
    assert set(before) == {""}

    with pytest.raises(
        SpecificationError,
        match="database active or SQLite sidecars present",
    ):
        testdata_module.generate_database(
            [small_specification()],
            database_path,
            overwrite=True,
        )

    assert artifact_bytes_and_mtimes(database_path) == before
    assert owned_temporary_artifacts(tmp_path) == []


@pytest.mark.parametrize("suffix", testdata_module._SQLITE_SIDECAR_SUFFIXES)
def test_new_database_rejects_orphaned_destination_sidecar(tmp_path, suffix):
    database_path = tmp_path / "orphaned.db"
    sidecar_path = Path(f"{database_path}{suffix}")
    sidecar_path.write_bytes(f"orphaned {suffix}".encode("ascii"))
    controlled_mtime_ns = 1_700_000_000_000_000_000
    os.utime(sidecar_path, ns=(controlled_mtime_ns, controlled_mtime_ns))
    before = (sidecar_path.read_bytes(), sidecar_path.stat().st_mtime_ns)

    with pytest.raises(
        SpecificationError,
        match="database active or SQLite sidecars present",
    ):
        testdata_module.generate_database(
            [small_specification()],
            database_path,
        )

    assert not database_path.exists()
    assert (sidecar_path.read_bytes(), sidecar_path.stat().st_mtime_ns) == before
    assert set(tmp_path.iterdir()) == {sidecar_path}
    assert owned_temporary_artifacts(tmp_path) == []


def test_new_database_rolls_back_sidecar_racing_at_atomic_link(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "link-race.db"
    sidecar_path = Path(f"{database_path}-wal")
    original_link = os.link
    raced_sidecar = []

    def link_after_sidecar_arrives(source_path, destination_path):
        if Path(destination_path) == database_path and not raced_sidecar:
            sidecar_path.write_bytes(b"orphan copied at link boundary")
            controlled_mtime_ns = 1_700_000_000_000_000_000
            os.utime(sidecar_path, ns=(controlled_mtime_ns, controlled_mtime_ns))
            raced_sidecar.append(
                (sidecar_path.read_bytes(), sidecar_path.stat().st_mtime_ns)
            )
        return original_link(source_path, destination_path)

    monkeypatch.setattr(testdata_module.os, "link", link_after_sidecar_arrives)

    with pytest.raises(
        SpecificationError,
        match="database active or SQLite sidecars present",
    ):
        testdata_module.generate_database(
            [small_specification()],
            database_path,
        )

    assert not database_path.exists()
    assert (sidecar_path.read_bytes(), sidecar_path.stat().st_mtime_ns) == (
        raced_sidecar[0]
    )
    assert set(tmp_path.iterdir()) == {sidecar_path}
    assert owned_temporary_artifacts(tmp_path) == []


def test_cli_reports_sidecar_rejection_without_changing_destination(
    tmp_path,
    capsys,
):
    csv_path = tmp_path / "runs.csv"
    database_path = tmp_path / "cli-active.db"
    write_specification(
        csv_path,
        [("1", "current", "Current", "nA", "-1", "1", "5", "", "", "")],
    )
    testdata_module.generate_database([small_specification()], database_path)
    journal_path = Path(f"{database_path}-journal")
    journal_path.write_bytes(b"owned by another SQLite process")
    before = artifact_bytes_and_mtimes(database_path)

    with pytest.raises(SystemExit) as exit_info:
        main([str(csv_path), str(database_path), "--overwrite"])

    assert exit_info.value.code == 2
    assert "database active or SQLite sidecars present" in capsys.readouterr().err
    assert artifact_bytes_and_mtimes(database_path) == before
    assert owned_temporary_artifacts(tmp_path) == []


def test_overwrite_rejects_when_sidecar_appears_at_final_replace(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "racing-sidecar.db"
    sidecar_path = Path(f"{database_path}-wal")
    testdata_module.generate_database([small_specification()], database_path)
    old_main = artifact_bytes_and_mtimes(database_path)[""]
    original_replace = os.replace
    raced_sidecar = []
    blocked_qplot_reads = []

    def replace_after_sidecar_arrives(source_path, destination_path):
        if Path(destination_path) == database_path and not raced_sidecar:
            sidecar_path.write_bytes(b"sidecar created at final replace boundary")
            controlled_mtime_ns = 1_700_000_000_000_000_000
            os.utime(
                sidecar_path,
                ns=(controlled_mtime_ns, controlled_mtime_ns),
            )
            raced_sidecar.append(artifact_bytes_and_mtimes(database_path)["-wal"])
            result = original_replace(source_path, destination_path)
            assert database_path.read_bytes() != old_main[0]
            with pytest.raises(ReadOnlyDatabaseAccessError):
                qplot_run_name(database_path)
            blocked_qplot_reads.append(True)
            return result
        return original_replace(source_path, destination_path)

    monkeypatch.setattr(
        testdata_module.os,
        "replace",
        replace_after_sidecar_arrives,
    )

    with pytest.raises(
        SpecificationError,
        match="database active or SQLite sidecars present",
    ):
        testdata_module.generate_database(
            [small_specification()],
            database_path,
            overwrite=True,
        )

    assert len(raced_sidecar) == 1
    assert blocked_qplot_reads == [True]
    assert artifact_bytes_and_mtimes(database_path) == {
        "": old_main,
        "-wal": raced_sidecar[0],
    }
    guard_path = Path(f"{database_path}{DATABASE_PUBLICATION_GUARD_SUFFIX}")
    assert guard_path.is_file()
    with pytest.raises(ReadOnlyDatabaseAccessError):
        qplot_run_name(database_path)
    assert set(tmp_path.iterdir()) == {database_path, sidecar_path, guard_path}
    assert not any(
        path.name.startswith(testdata_module._TEMPORARY_DATABASE_PREFIX)
        for path in tmp_path.iterdir()
    )


def test_post_replace_wal_restores_old_main_and_retains_safety_guard(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "post-replace-wal.db"
    wal_path = Path(f"{database_path}-wal")
    testdata_module.generate_database([small_specification()], database_path)
    old_main = artifact_bytes_and_mtimes(database_path)[""]
    original_replace = os.replace
    replacement_wals = []

    def replace_then_commit_new_wal(source_path, destination_path):
        result = original_replace(source_path, destination_path)
        if Path(destination_path) == database_path and not replacement_wals:
            writer = sqlite3.connect(database_path)
            try:
                assert writer.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
                writer.execute("PRAGMA wal_autocheckpoint = 0")
                writer.execute("UPDATE runs SET name = 'NEW_WAL'")
                writer.commit()
                wal_contents = wal_path.read_bytes()
            finally:
                writer.close()

            # Windows cannot atomically restore the old main while this writer
            # holds the replacement open. Re-create its captured WAL only after
            # closing it: the sidecar is still genuinely from the replacement,
            # and pairing it with the restored old main remains ambiguous.
            wal_path.write_bytes(wal_contents)
            controlled_mtime_ns = 1_700_000_000_000_000_000
            os.utime(wal_path, ns=(controlled_mtime_ns, controlled_mtime_ns))
            replacement_wals.append(
                (wal_path.read_bytes(), wal_path.stat().st_mtime_ns)
            )
        return result

    monkeypatch.setattr(
        testdata_module.os,
        "replace",
        replace_then_commit_new_wal,
    )

    with pytest.raises(
        SpecificationError,
        match="database active or SQLite sidecars present",
    ) as error_info:
        testdata_module.generate_database(
            [small_specification()],
            database_path,
            overwrite=True,
        )

    guard_path = Path(f"{database_path}{DATABASE_PUBLICATION_GUARD_SUFFIX}")
    assert "cannot prove which main they belong to" in str(error_info.value)
    assert len(replacement_wals) == 1
    artifacts = artifact_bytes_and_mtimes(database_path)
    assert artifacts[""] == old_main
    assert artifacts["-wal"] == replacement_wals[0]
    assert immutable_run_name(database_path) == "run_1"
    assert guard_path.is_file()
    with pytest.raises(ReadOnlyDatabaseAccessError):
        qplot_run_name(database_path)
    assert not any(
        path.name.startswith(testdata_module._TEMPORARY_DATABASE_PREFIX)
        for path in tmp_path.iterdir()
    )


def test_post_replace_sidecar_restore_failure_retains_recovery_artifacts(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "failed-restore.db"
    sidecar_path = Path(f"{database_path}-wal")
    guard_path = Path(f"{database_path}{DATABASE_PUBLICATION_GUARD_SUFFIX}")
    testdata_module.generate_database([small_specification()], database_path)
    old_main = artifact_bytes_and_mtimes(database_path)[""]
    original_replace = os.replace
    replacement_main = []
    injected_sidecar = []
    backup_paths = []
    backup_states = []
    restore_attempts = []

    def install_then_fail_backup_restore(source_path, destination_path):
        source_path = Path(source_path)
        destination_path = Path(destination_path)
        if destination_path == database_path and source_path.suffix == ".backup":
            if not backup_paths:
                backup_paths.append(source_path)
                backup_states.append(
                    (source_path.read_bytes(), source_path.stat().st_mtime_ns)
                )
            restore_attempts.append(source_path)
            raise OSError("injected backup restore failure")

        result = original_replace(source_path, destination_path)
        if destination_path == database_path and not replacement_main:
            replacement_main.append(artifact_bytes_and_mtimes(database_path)[""])
            sidecar_path.write_bytes(b"ambiguous WAL from replacement main")
            controlled_mtime_ns = 1_700_000_000_000_000_000
            os.utime(sidecar_path, ns=(controlled_mtime_ns, controlled_mtime_ns))
            injected_sidecar.append(
                (sidecar_path.read_bytes(), sidecar_path.stat().st_mtime_ns)
            )
        return result

    monkeypatch.setattr(
        testdata_module.os,
        "replace",
        install_then_fail_backup_restore,
    )

    with pytest.raises(OSError) as error_info:
        testdata_module.generate_database(
            [small_specification()],
            database_path,
            overwrite=True,
        )

    assert len(replacement_main) == 1
    assert replacement_main[0] != old_main
    assert len(injected_sidecar) == 1
    assert len(backup_paths) == 1
    assert restore_attempts
    backup_path = backup_paths[0]
    message = str(error_info.value)
    normalized_message = os.path.normcase(message)
    assert "database active or SQLite sidecars present" in message
    assert os.path.normcase(str(database_path)) in normalized_message
    assert os.path.normcase(str(guard_path)) in normalized_message
    assert os.path.normcase(str(backup_path)) in normalized_message
    assert "selected path may contain the replacement" in message.lower()

    artifacts = artifact_bytes_and_mtimes(database_path)
    assert artifacts[""] == replacement_main[0]
    assert artifacts["-wal"] == injected_sidecar[0]
    assert guard_path.is_file()
    assert backup_path.is_file()
    assert (backup_path.read_bytes(), backup_path.stat().st_mtime_ns) == (
        backup_states[0]
    )
    assert backup_states[0] == old_main
    with pytest.raises(ReadOnlyDatabaseAccessError):
        qplot_run_name(database_path)
    assert set(tmp_path.iterdir()) == {
        database_path,
        sidecar_path,
        guard_path,
        backup_path,
    }


def test_overwrite_succeeds_for_quiescent_database(tmp_path):
    database_path = tmp_path / "quiescent.db"
    testdata_module.generate_database([small_specification()], database_path)
    original_contents = database_path.read_bytes()
    original_connection = sqlite3.connect(database_path)
    try:
        original_token = original_connection.execute(
            "SELECT generation_token FROM qplot_generation_provenance"
        ).fetchone()[0]
    finally:
        original_connection.close()
    assert artifact_bytes_and_mtimes(database_path).keys() == {""}
    replacement = testdata_module.RunSpecification(
        1,
        "replacement",
        "Replacement",
        "V",
        -2.0,
        2.0,
        7,
    )

    generated_path = testdata_module.generate_database(
        [replacement],
        database_path,
        overwrite=True,
    )

    assert generated_path == database_path
    assert database_path.read_bytes() != original_contents
    assert database_has_qplot_generation_marker(database_path)
    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute(
            "SELECT parameter FROM layouts ORDER BY layout_id"
        ).fetchall() == [("V_SD",), ("replacement",)]
        replacement_token = connection.execute(
            "SELECT generation_token FROM qplot_generation_provenance"
        ).fetchone()[0]
    finally:
        connection.close()
    assert replacement_token != original_token
    assert artifact_bytes_and_mtimes(database_path).keys() == {""}
    assert owned_temporary_artifacts(tmp_path) == []


@pytest.mark.parametrize("overwrite_before_write", [False, True])
def test_later_legitimate_wal_commit_is_visible_without_source_changes(
    tmp_path,
    overwrite_before_write,
):
    database_path = tmp_path / "later-legitimate-wal.db"
    testdata_module.generate_database([small_specification()], database_path)
    if overwrite_before_write:
        testdata_module.generate_database(
            [small_specification()],
            database_path,
            overwrite=True,
        )

    assert qplot_run_name(database_path) == "run_1"
    writer = testdata_module._connect_writable_exact_path(database_path)
    try:
        assert isinstance(writer, AtomicConnection)
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("UPDATE runs SET name = 'LEGITIMATE_LATER_COMMIT'")
        writer.commit()
        assert writer.execute("SELECT name FROM runs").fetchone()[0] == (
            "LEGITIMATE_LATER_COMMIT"
        )

        source_before = complete_artifact_state(database_path)
        assert source_before[""] is not None
        assert source_before["-wal"] is not None
        assert source_before["-shm"] is not None
        assert source_before["-journal"] is None
        assert qplot_run_name(database_path) == "LEGITIMATE_LATER_COMMIT"
        assert complete_artifact_state(database_path) == source_before

        child_script = "\n".join(
            (
                "import sys",
                "from qplot.datahandling.readonly import qcodes_read_only_connection",
                "connection = qcodes_read_only_connection(sys.argv[1])",
                "try:",
                "    print(connection.execute(",
                "        'SELECT name FROM runs'",
                "    ).fetchone()[0])",
                "finally:",
                "    connection.close()",
            )
        )
        child = subprocess.run(
            [sys.executable, "-c", child_script, str(database_path)],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        assert child.stdout.strip() == "LEGITIMATE_LATER_COMMIT"
        assert complete_artifact_state(database_path) == source_before
    finally:
        writer.close()


def test_enabled_writer_proves_future_qcodes_result_after_checkpoint_and_restart(
    tmp_path,
):
    database_path = tmp_path / "future-result.db"
    testdata_module.generate_database([small_specification()], database_path)
    writer = testdata_module._connect_writable_exact_path(database_path)
    try:
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        assert (
            testdata_module.enable_generation_provenance_for_writer(writer)
            is writer
        )
        experiment = load_or_create_experiment(
            "future_result",
            sample_name="writer_provenance",
            conn=writer,
        )
        assert experiment.conn is writer
        measurement, setpoint, signal = future_qcodes_measurement(
            experiment,
            "future_result_run",
        )

        with measurement.run(write_in_background=False) as datasaver:
            table_name = datasaver.dataset.table_name
            add_and_flush_result(datasaver, setpoint, signal, 1.0)
            assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (
                0,
                0,
                0,
            )
            add_and_flush_result(datasaver, setpoint, signal, 2.0)

            source_before = complete_artifact_state(database_path)
            assert source_before[""] is not None
            assert source_before["-wal"] is not None
            assert source_before["-wal"][3] > 0
            assert source_before["-shm"] is not None
            assert source_before["-journal"] is None
            assert qplot_result_values(
                database_path,
                table_name,
                signal.name,
            ) == [10.0, 20.0]
            assert complete_artifact_state(database_path) == source_before
            assert child_process_qplot_result_values(
                database_path,
                table_name,
                signal.name,
            ) == [10.0, 20.0]
            assert complete_artifact_state(database_path) == source_before
    finally:
        writer.close()


def test_enabled_writer_keeps_qcodes_atomic_reads_read_only(tmp_path):
    database_path = tmp_path / "atomic-read.db"
    testdata_module.generate_database([small_specification()], database_path)
    writer = testdata_module._connect_writable_exact_path(database_path)
    try:
        testdata_module.enable_generation_provenance_for_writer(writer)
        lineage_before = testdata_module._generation_lineage_state(writer)

        with atomic(writer):
            assert writer.execute("SELECT COUNT(*) FROM runs").fetchone() == (1,)

        assert testdata_module._generation_lineage_state(writer) == lineage_before
    finally:
        writer.close()


def test_enabled_writer_covers_background_future_table_after_checkpoint(
    tmp_path,
):
    database_path = tmp_path / "background-future-result.db"
    testdata_module.generate_database([small_specification()], database_path)
    writer = testdata_module._connect_writable_exact_path(database_path)
    try:
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        testdata_module.enable_generation_provenance_for_writer(writer)
        experiment = load_or_create_experiment(
            "background_future_result",
            sample_name="writer_provenance",
            conn=writer,
        )
        measurement, setpoint, signal = future_qcodes_measurement(
            experiment,
            "background_future_result_run",
        )

        with measurement.run(write_in_background=True) as datasaver:
            table_name = datasaver.dataset.table_name
            add_and_flush_result(datasaver, setpoint, signal, 1.0)
            assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (
                0,
                0,
                0,
            )
            add_and_flush_result(datasaver, setpoint, signal, 2.0)

            source_before = complete_artifact_state(database_path)
            assert source_before[""] is not None
            assert source_before["-wal"] is not None
            assert source_before["-wal"][3] > 0
            assert source_before["-shm"] is not None
            assert source_before["-journal"] is None
            assert qplot_result_values(
                database_path,
                table_name,
                signal.name,
            ) == [10.0, 20.0]
            assert complete_artifact_state(database_path) == source_before
    finally:
        writer.close()


def test_enabled_writer_tracks_multiple_future_tables_and_checkpoint_cycles(
    tmp_path,
):
    database_path = tmp_path / "multiple-future-results.db"
    testdata_module.generate_database([small_specification()], database_path)
    writer = testdata_module._connect_writable_exact_path(database_path)
    try:
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        testdata_module.enable_generation_provenance_for_writer(writer)
        experiment = load_or_create_experiment(
            "multiple_future_results",
            sample_name="writer_provenance",
            conn=writer,
        )
        result_tables = []

        for table_number in range(3):
            measurement, setpoint, signal = future_qcodes_measurement(
                experiment,
                f"future_result_run_{table_number}",
            )
            with measurement.run(write_in_background=False) as datasaver:
                table_name = datasaver.dataset.table_name
                result_tables.append((table_name, signal.name))
                add_and_flush_result(datasaver, setpoint, signal, 1.0)
                for value in (2.0, 3.0):
                    assert writer.execute(
                        "PRAGMA wal_checkpoint(TRUNCATE)"
                    ).fetchone() == (0, 0, 0)
                    add_and_flush_result(datasaver, setpoint, signal, value)
                    source_before = complete_artifact_state(database_path)
                    assert qplot_result_values(
                        database_path,
                        table_name,
                        signal.name,
                    ) == [
                        result * 10.0
                        for result in range(1, int(value) + 1)
                    ]
                    assert complete_artifact_state(database_path) == source_before

        source_before = complete_artifact_state(database_path)
        assert source_before["-wal"] is not None
        assert source_before["-wal"][3] > 0
        assert source_before["-shm"] is not None
        assert source_before["-journal"] is None
        for table_name, parameter_name in result_tables:
            assert qplot_result_values(
                database_path,
                table_name,
                parameter_name,
            ) == [10.0, 20.0, 30.0]
        assert complete_artifact_state(database_path) == source_before
    finally:
        writer.close()


def test_uninstrumented_future_qcodes_result_wal_fails_closed_with_guidance(
    tmp_path,
):
    database_path = tmp_path / "uninstrumented-future-result.db"
    testdata_module.generate_database([small_specification()], database_path)
    writer = testdata_module._connect_writable_exact_path(database_path)
    try:
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        experiment = load_or_create_experiment(
            "uninstrumented_future_result",
            sample_name="writer_provenance",
            conn=writer,
        )
        measurement, setpoint, signal = future_qcodes_measurement(
            experiment,
            "uninstrumented_future_result_run",
        )

        with measurement.run(write_in_background=False) as datasaver:
            table_name = datasaver.dataset.table_name
            add_and_flush_result(datasaver, setpoint, signal, 1.0)
            assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (
                0,
                0,
                0,
            )
            add_and_flush_result(datasaver, setpoint, signal, 2.0)

            immutable = sqlite3.connect(
                f"{database_path.resolve().as_uri()}?mode=ro&immutable=1",
                uri=True,
            )
            try:
                main_epoch = immutable.execute(
                    "SELECT write_epoch FROM qplot_generation_provenance"
                ).fetchone()[0]
            finally:
                immutable.close()
            wal_epoch = writer.execute(
                "SELECT write_epoch FROM qplot_generation_provenance"
            ).fetchone()[0]
            assert wal_epoch == main_epoch

            source_before = complete_artifact_state(database_path)
            with pytest.raises(
                RuntimeError,
                match="refuses to bless writes already present in a WAL",
            ):
                testdata_module.enable_generation_provenance_for_writer(writer)
            assert complete_artifact_state(database_path) == source_before
            with pytest.raises(UnverifiableDatabaseWalError) as error_info:
                qplot_result_values(
                    database_path,
                    table_name,
                    signal.name,
                )
            message = str(error_info.value)
            assert "enable_generation_provenance_for_writer" in message
            assert "checkpoint" in message.lower()
            assert complete_artifact_state(database_path) == source_before
    finally:
        writer.close()


def test_later_provenance_commit_does_not_bless_earlier_uninstrumented_wal(
    tmp_path,
):
    database_path = tmp_path / "uninstrumented-then-provenance.db"
    testdata_module.generate_database([small_specification()], database_path)
    writer = testdata_module._connect_writable_exact_path(database_path)
    try:
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        testdata_module.enable_generation_provenance_for_writer(writer)
        assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (
            0,
            0,
            0,
        )

        uninstrumented = sqlite3.connect(database_path)
        try:
            uninstrumented.execute(
                "CREATE TABLE uninstrumented_results "
                "(id INTEGER PRIMARY KEY, value TEXT)"
            )
            uninstrumented.execute(
                "INSERT INTO uninstrumented_results(value) VALUES ('UNPROVEN')"
            )
            uninstrumented.commit()
        finally:
            uninstrumented.close()

        before_later_commit = complete_artifact_state(database_path)
        with pytest.raises(UnverifiableDatabaseWalError):
            qplot_run_name(database_path)
        assert complete_artifact_state(database_path) == before_later_commit

        # The enabled owner now installs the missing table triggers and records
        # a genuine lineage event.  That later event must not retroactively
        # authenticate the preceding uninstrumented WAL transaction.
        writer.execute("UPDATE runs SET name = name")
        writer.commit()
        assert writer.execute(
            "SELECT value FROM uninstrumented_results"
        ).fetchall() == [("UNPROVEN",)]

        source_before = complete_artifact_state(database_path)
        with pytest.raises(UnverifiableDatabaseWalError):
            qplot_run_name(database_path)
        assert complete_artifact_state(database_path) == source_before
    finally:
        writer.close()


def test_writer_enablement_locks_before_wal_quiescence_check(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "enablement-lock-order.db"
    wal_path = Path(f"{database_path}-wal")
    testdata_module.generate_database([small_specification()], database_path)
    writer = testdata_module._connect_writable_exact_path(database_path)
    race_started = threading.Event()
    race_finished = threading.Event()
    competitor_outcome = []

    def competing_writer():
        if not race_started.wait(timeout=5):
            competitor_outcome.append(("timeout", "enablement WAL check not reached"))
            race_finished.set()
            return
        connection = sqlite3.connect(database_path, timeout=0.0)
        try:
            connection.execute(
                "CREATE TABLE racing_uninstrumented "
                "(id INTEGER PRIMARY KEY, value TEXT)"
            )
            connection.execute(
                "INSERT INTO racing_uninstrumented(value) VALUES ('RACED')"
            )
            connection.commit()
            competitor_outcome.append(("committed", ""))
        except sqlite3.OperationalError as error:
            connection.rollback()
            competitor_outcome.append(("error", str(error)))
        finally:
            connection.close()
            race_finished.set()

    competitor = threading.Thread(target=competing_writer, daemon=True)
    try:
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (
            0,
            0,
            0,
        )

        original_stat = Path.stat
        coordinated = False

        def stat_after_competing_write_attempt(path, *args, **kwargs):
            nonlocal coordinated
            result = original_stat(path, *args, **kwargs)
            if path == wal_path and not coordinated:
                coordinated = True
                race_started.set()
                assert race_finished.wait(timeout=5)
            return result

        monkeypatch.setattr(Path, "stat", stat_after_competing_write_attempt)
        competitor.start()
        testdata_module.enable_generation_provenance_for_writer(writer)
        competitor.join(timeout=5)

        assert coordinated
        assert not competitor.is_alive()
        assert len(competitor_outcome) == 1
        outcome, detail = competitor_outcome[0]
        assert outcome == "error"
        assert "locked" in detail.lower()
        assert writer.execute(
            "SELECT name FROM sqlite_schema WHERE name = 'racing_uninstrumented'"
        ).fetchone() is None

        # A raw writer can still bypass the opt-in hook after enablement, but
        # its uninstrumented new table must make the resulting WAL unverifiable.
        uninstrumented = sqlite3.connect(database_path)
        try:
            uninstrumented.execute(
                "CREATE TABLE post_enable_uninstrumented "
                "(id INTEGER PRIMARY KEY, value TEXT)"
            )
            uninstrumented.execute(
                "INSERT INTO post_enable_uninstrumented(value) VALUES ('RAW')"
            )
            uninstrumented.commit()
        finally:
            uninstrumented.close()

        source_before = complete_artifact_state(database_path)
        with pytest.raises(UnverifiableDatabaseWalError):
            qplot_run_name(database_path)
        assert complete_artifact_state(database_path) == source_before
    finally:
        race_started.set()
        competitor.join(timeout=5)
        writer.close()


def test_writer_enablement_upgrades_legacy_numeric_provenance_triggers(tmp_path):
    database_path = tmp_path / "legacy-writer-provenance.db"
    testdata_module.generate_database([small_specification()], database_path)
    writer = testdata_module._connect_writable_exact_path(database_path)
    try:
        original_token = writer.execute(
            "SELECT generation_token FROM qplot_generation_provenance"
        ).fetchone()[0]
        legacy_names = replace_generation_triggers_with_legacy_numeric_format(
            writer
        )
        assert legacy_names
        assert writer.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert writer.execute(
            "PRAGMA table_info(qplot_generation_provenance)"
        ).fetchall() == [
            (0, "singleton", "INTEGER", 0, None, 1),
            (1, "generation_token", "TEXT", 1, None, 0),
            (2, "write_epoch", "INTEGER", 1, None, 0),
        ]
        assert {
            row[0]
            for row in writer.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            ).fetchall()
        }.isdisjoint(
            {
                QPLOT_GENERATION_LINEAGE_STATE_TABLE,
                QPLOT_GENERATION_LINEAGE_RING_TABLE,
            }
        )
        assert {
            row[0]
            for row in writer.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'trigger'"
            ).fetchall()
        }.issuperset(legacy_names)

        testdata_module.enable_generation_provenance_for_writer(writer)
        migrated_token = writer.execute(
            "SELECT generation_token FROM qplot_generation_provenance"
        ).fetchone()[0]
        assert migrated_token != original_token
        experiment = load_or_create_experiment(
            "legacy_future_result",
            sample_name="writer_provenance",
            conn=writer,
        )
        upgraded_names = {
            row[0]
            for row in writer.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'trigger'"
            ).fetchall()
        }
        assert upgraded_names.isdisjoint(legacy_names)
        assert any(
            name.startswith(testdata_module._PROVENANCE_TRIGGER_PREFIX)
            for name in upgraded_names
        )
        assert {
            row[0]
            for row in writer.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            ).fetchall()
        }.issuperset(
            {
                QPLOT_GENERATION_LINEAGE_STATE_TABLE,
                QPLOT_GENERATION_LINEAGE_RING_TABLE,
            }
        )

        assert writer.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        measurement, setpoint, signal = future_qcodes_measurement(
            experiment,
            "legacy_future_result_run",
        )
        with measurement.run(write_in_background=False) as datasaver:
            table_name = datasaver.dataset.table_name
            add_and_flush_result(datasaver, setpoint, signal, 1.0)
            assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (
                0,
                0,
                0,
            )
            add_and_flush_result(datasaver, setpoint, signal, 2.0)
            source_before = complete_artifact_state(database_path)
            assert qplot_result_values(
                database_path,
                table_name,
                signal.name,
            ) == [10.0, 20.0]
            assert complete_artifact_state(database_path) == source_before
    finally:
        writer.close()


def test_legacy_main_only_is_readable_but_live_higher_epoch_wal_fails_closed(
    tmp_path,
):
    database_path = tmp_path / "legacy-live-wal.db"
    testdata_module.generate_database([small_specification()], database_path)
    fixture_writer = testdata_module._connect_writable_exact_path(database_path)
    try:
        replace_generation_triggers_with_legacy_numeric_format(fixture_writer)
    finally:
        fixture_writer.close()

    main_only_state = complete_artifact_state(database_path)
    assert main_only_state[""] is not None
    assert main_only_state["-wal"] is None
    assert main_only_state["-shm"] is None
    assert main_only_state["-journal"] is None
    assert qplot_run_name(database_path) == "run_1"
    assert complete_artifact_state(database_path) == main_only_state

    writer = testdata_module._connect_writable_exact_path(database_path)
    try:
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("UPDATE runs SET name = 'LEGACY_HIGHER_EPOCH_WAL'")
        writer.commit()

        immutable = sqlite3.connect(
            f"{database_path.resolve().as_uri()}?mode=ro&immutable=1",
            uri=True,
        )
        try:
            assert immutable.execute("SELECT name FROM runs").fetchone()[0] == (
                "run_1"
            )
            main_epoch = immutable.execute(
                "SELECT write_epoch FROM qplot_generation_provenance"
            ).fetchone()[0]
        finally:
            immutable.close()
        assert writer.execute("SELECT name FROM runs").fetchone()[0] == (
            "LEGACY_HIGHER_EPOCH_WAL"
        )
        wal_epoch = writer.execute(
            "SELECT write_epoch FROM qplot_generation_provenance"
        ).fetchone()[0]
        assert wal_epoch > main_epoch

        source_before = complete_artifact_state(database_path)
        with pytest.raises(UnverifiableDatabaseWalError) as error_info:
            qplot_run_name(database_path)
        message = str(error_info.value)
        assert "older qPlot" in message
        assert "enable_generation_provenance_for_writer" in message
        assert complete_artifact_state(database_path) == source_before
    finally:
        writer.close()


def test_generated_wal_provenance_survives_rename_copy_and_recreation(tmp_path):
    original_path = tmp_path / "original-generated.db"
    renamed_path = tmp_path / "renamed-generated.db"
    copied_path = tmp_path / "copied-generated.db"
    testdata_module.generate_database([small_specification()], original_path)
    original_path.rename(renamed_path)

    writer = sqlite3.connect(renamed_path)
    try:
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("UPDATE runs SET name = 'RENAMED_WAL'")
        writer.commit()
        renamed_state = complete_artifact_state(renamed_path)
        assert qplot_run_name(renamed_path) == "RENAMED_WAL"
        assert complete_artifact_state(renamed_path) == renamed_state

        shutil.copyfile(renamed_path, copied_path)
        shutil.copyfile(f"{renamed_path}-wal", f"{copied_path}-wal")
        shutil.copyfile(f"{renamed_path}-shm", f"{copied_path}-shm")
        copied_state = complete_artifact_state(copied_path)
        assert qplot_run_name(copied_path) == "RENAMED_WAL"
        assert complete_artifact_state(copied_path) == copied_state
    finally:
        writer.close()

    assert not Path(f"{renamed_path}-wal").exists()
    checkpointed_state = complete_artifact_state(renamed_path)
    assert qplot_run_name(renamed_path) == "RENAMED_WAL"
    assert complete_artifact_state(renamed_path) == checkpointed_state

    recreated_writer = sqlite3.connect(renamed_path)
    try:
        recreated_writer.execute("PRAGMA wal_autocheckpoint = 0")
        recreated_writer.execute("UPDATE runs SET name = 'RECREATED_WAL'")
        recreated_writer.commit()
        recreated_state = complete_artifact_state(renamed_path)
        assert recreated_state["-wal"] is not None
        assert qplot_run_name(renamed_path) == "RECREATED_WAL"
        assert complete_artifact_state(renamed_path) == recreated_state
    finally:
        recreated_writer.close()


def test_first_load_rejects_valid_wal_created_after_final_check(
    tmp_path,
    monkeypatch,
):
    wal_source_path = tmp_path / "wal-source.db"
    testdata_module.generate_database([small_specification()], wal_source_path)
    wal_source_writer = sqlite3.connect(wal_source_path)
    try:
        assert wal_source_writer.execute(
            "PRAGMA journal_mode = WAL"
        ).fetchone()[0] == "wal"
        wal_source_writer.execute("PRAGMA wal_autocheckpoint = 0")
        wal_source_writer.execute("UPDATE runs SET name = 'OLD_WAL'")
        wal_source_writer.commit()
        parked_wal = Path(f"{wal_source_path}-wal").read_bytes()
        parked_shm = Path(f"{wal_source_path}-shm").read_bytes()
    finally:
        wal_source_writer.close()

    database_path = tmp_path / "last-gap-sidecar.db"
    sidecar_path = Path(f"{database_path}-wal")
    shm_path = Path(f"{database_path}-shm")
    guard_path = Path(f"{database_path}{DATABASE_PUBLICATION_GUARD_SUFFIX}")
    testdata_module.generate_database([small_specification()], database_path)
    original_unlink = Path.unlink
    injected = []

    def unlink_guard_after_sidecar_appears(path, *args, **kwargs):
        if path == guard_path and not injected:
            sidecar_path.write_bytes(parked_wal)
            shm_path.write_bytes(parked_shm)
            injected.append(True)
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink_guard_after_sidecar_appears)

    generated_path = testdata_module.generate_database(
        [small_specification()],
        database_path,
        overwrite=True,
    )

    assert generated_path == database_path
    assert injected == [True]
    assert database_has_qplot_generation_marker(database_path)
    assert not guard_path.exists()
    sidecars_before = complete_artifact_state(database_path)
    probe_directory = tmp_path / "unsafe-probe"
    probe_directory.mkdir()
    probe_path = probe_directory / "probe.db"
    shutil.copyfile(database_path, probe_path)
    shutil.copyfile(sidecar_path, Path(f"{probe_path}-wal"))
    probe = sqlite3.connect(probe_path)
    try:
        assert probe.execute("SELECT name FROM runs").fetchone()[0] == "OLD_WAL"
    finally:
        probe.close()

    assert immutable_run_name(database_path) == "run_1"
    with pytest.raises(
        UnverifiableDatabaseWalError,
        match="different generation token",
    ):
        qplot_run_name(database_path)
    assert complete_artifact_state(database_path) == sidecars_before


def test_new_output_first_load_rejects_wal_after_final_link_check(
    tmp_path,
    monkeypatch,
):
    wal_source_path = tmp_path / "new-output-wal-source.db"
    testdata_module.generate_database([small_specification()], wal_source_path)
    wal_source_writer = sqlite3.connect(wal_source_path)
    try:
        assert wal_source_writer.execute(
            "PRAGMA journal_mode = WAL"
        ).fetchone()[0] == "wal"
        wal_source_writer.execute("PRAGMA wal_autocheckpoint = 0")
        wal_source_writer.execute("UPDATE runs SET name = 'OLD_WAL'")
        wal_source_writer.commit()
        parked_wal = Path(f"{wal_source_path}-wal").read_bytes()
        parked_shm = Path(f"{wal_source_path}-shm").read_bytes()
    finally:
        wal_source_writer.close()

    database_path = tmp_path / "new-output-last-gap.db"
    wal_path = Path(f"{database_path}-wal")
    shm_path = Path(f"{database_path}-shm")
    guard_path = Path(f"{database_path}{DATABASE_PUBLICATION_GUARD_SUFFIX}")
    original_unlink = Path.unlink
    injected = []

    def unlink_guard_after_sidecars_appear(path, *args, **kwargs):
        if path == guard_path and not injected:
            wal_path.write_bytes(parked_wal)
            shm_path.write_bytes(parked_shm)
            injected.append(True)
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink_guard_after_sidecars_appear)

    generated_path = testdata_module.generate_database(
        [small_specification()],
        database_path,
    )

    assert generated_path == database_path
    assert injected == [True]
    assert database_has_qplot_generation_marker(database_path)
    assert immutable_run_name(database_path) == "run_1"
    sidecars_before = complete_artifact_state(database_path)
    with pytest.raises(
        UnverifiableDatabaseWalError,
        match="different generation token",
    ):
        qplot_run_name(database_path)
    assert complete_artifact_state(database_path) == sidecars_before


def test_first_load_rejects_identity_changed_during_marker_read(
    tmp_path,
    monkeypatch,
):
    wal_source_path = tmp_path / "identity-race-wal-source.db"
    testdata_module.generate_database([small_specification()], wal_source_path)
    wal_source_writer = sqlite3.connect(wal_source_path)
    try:
        assert wal_source_writer.execute(
            "PRAGMA journal_mode = WAL"
        ).fetchone()[0] == "wal"
        wal_source_writer.execute("PRAGMA wal_autocheckpoint = 0")
        wal_source_writer.execute("UPDATE runs SET name = 'OLD_WAL'")
        wal_source_writer.commit()
        parked_wal = Path(f"{wal_source_path}-wal").read_bytes()
    finally:
        wal_source_writer.close()

    replacement_path = tmp_path / "marked-replacement.db"
    testdata_module.generate_database([small_specification()], replacement_path)
    testdata_module.generate_database(
        [small_specification()],
        replacement_path,
        overwrite=True,
    )
    assert database_has_qplot_generation_marker(replacement_path)

    database_path = tmp_path / "marker-identity-race.db"
    wal_path = Path(f"{database_path}-wal")
    testdata_module.generate_database([small_specification()], database_path)
    original_marker_check = readonly_module.database_has_qplot_generation_marker
    replacement_installed = []

    def replace_during_marker_read(path):
        if Path(path) == database_path and not replacement_installed:
            os.replace(replacement_path, database_path)
            wal_path.write_bytes(parked_wal)
            replacement_installed.append(True)
            return False
        return original_marker_check(path)

    monkeypatch.setattr(
        readonly_module,
        "database_has_qplot_generation_marker",
        replace_during_marker_read,
    )

    with pytest.raises(DatabaseInstanceChangedError):
        qplot_run_name(database_path)

    assert replacement_installed == [True]
    assert immutable_run_name(database_path) == "run_1"
    assert qplot_run_name(database_path) == "run_1"


def test_post_commit_cleanup_error_does_not_report_rejected_publication(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "post-commit-cleanup.db"
    testdata_module.generate_database([small_specification()], database_path)
    original_cleanup = testdata_module._remove_owned_temporary_artifacts
    post_commit_failures = []

    def fail_only_after_publication(temporary_path, *, include_database=True):
        original_cleanup(
            temporary_path,
            include_database=include_database,
        )
        if (
            include_database
            and database_path.exists()
            and not post_commit_failures
        ):
            post_commit_failures.append(Path(temporary_path))
            raise OSError("injected post-commit cleanup failure")

    monkeypatch.setattr(
        testdata_module,
        "_remove_owned_temporary_artifacts",
        fail_only_after_publication,
    )

    generated_path = testdata_module.generate_database(
        [small_specification()],
        database_path,
        overwrite=True,
    )

    assert generated_path == database_path
    assert len(post_commit_failures) == 1
    assert_generated_database(database_path)
    assert owned_temporary_artifacts(tmp_path) == []


def test_successful_overwrite_retries_transient_backup_cleanup_failure(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "backup-cleanup-retry.db"
    testdata_module.generate_database([small_specification()], database_path)
    original_unlink = Path.unlink
    backup_unlink_attempts = []

    def fail_first_committed_backup_unlink(path, *args, **kwargs):
        if path.name.endswith(".backup"):
            backup_unlink_attempts.append(path)
            if len(backup_unlink_attempts) == 2:
                raise OSError("injected transient backup cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_first_committed_backup_unlink)

    generated_path = testdata_module.generate_database(
        [small_specification()],
        database_path,
        overwrite=True,
    )

    assert generated_path == database_path
    assert len(backup_unlink_attempts) == 3
    assert owned_temporary_artifacts(tmp_path) == []


def test_successful_new_output_retries_transient_temporary_cleanup_failure(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "temporary-cleanup-retry.db"
    original_unlink = Path.unlink
    temporary_unlink_attempts = []

    def fail_first_published_temporary_unlink(path, *args, **kwargs):
        if (
            path.name.startswith(testdata_module._TEMPORARY_DATABASE_PREFIX)
            and path.suffix == ".db"
            and path.exists()
        ):
            temporary_unlink_attempts.append(path)
            if len(temporary_unlink_attempts) == 1:
                raise OSError("injected transient temporary cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_first_published_temporary_unlink)

    generated_path = testdata_module.generate_database(
        [small_specification()],
        database_path,
    )

    assert generated_path == database_path
    assert len(temporary_unlink_attempts) == 2
    assert owned_temporary_artifacts(tmp_path) == []


def test_cli_generates_exact_reserved_output_path(tmp_path):
    csv_path = tmp_path / "runs.csv"
    output_directory = tmp_path / "CLI # %3f space 測定"
    output_directory.mkdir()
    database_path = output_directory / "out#%23 space 測定.db"
    write_specification(
        csv_path,
        [("1", "current", "Current", "nA", "-1", "1", "5", "", "", "")],
    )

    assert main([str(csv_path), str(database_path)]) == 0

    assert_generated_database(database_path)
    assert owned_temporary_artifacts(output_directory) == []
