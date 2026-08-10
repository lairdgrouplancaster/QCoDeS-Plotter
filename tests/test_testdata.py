import csv
import os
import re
import sqlite3
from pathlib import Path

import numpy as np
import pytest
import qcodes as qc
from qcodes.dataset import initialise_or_create_database_at, load_by_id
from qcodes.dataset.measurements import DataSaver
from qcodes.dataset.sqlite.connection import AtomicConnection

from qplot import testdata as testdata_module
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
        if path.name.startswith(testdata_module._TEMPORARY_DATABASE_PREFIX)
    )


def create_all_temporary_sidecars(temporary_path):
    for suffix in testdata_module._SQLITE_SIDECAR_SUFFIXES:
        Path(f"{temporary_path}{suffix}").write_bytes(suffix.encode("ascii"))


def test_example_csv_is_ready_to_generate(tmp_path):
    csv_path = tmp_path / "example.csv"

    assert write_example_csv(csv_path) == csv_path
    specifications = read_specifications(csv_path)

    assert csv_path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert [specification.dimensions for specification in specifications] == [1, 1, 2, 2]
    assert [specification.point_count for specification in specifications] == [
        101,
        501,
        121 * 81,
        201 * 101,
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
        range(7, 35, 3)
    )

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


def test_publication_failure_removes_all_owned_temporary_sidecars(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "runs.db"

    def fail_publication(temporary_path, _database_path, _overwrite):
        create_all_temporary_sidecars(temporary_path)
        raise RuntimeError("injected publication failure")

    monkeypatch.setattr(testdata_module, "_publish_database", fail_publication)

    with pytest.raises(RuntimeError, match="injected publication failure"):
        testdata_module.generate_database([small_specification()], database_path)

    assert not database_path.exists()
    assert owned_temporary_artifacts(tmp_path) == []


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


def test_overwrite_publish_replaces_concurrently_created_file(
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

    testdata_module.generate_database(
        [small_specification()],
        database_path,
        overwrite=True,
    )

    assert_generated_database(database_path)
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
