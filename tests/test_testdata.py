import csv

import numpy as np
import pytest
import qcodes as qc
from qcodes.dataset import initialise_or_create_database_at, load_by_id
from qcodes.dataset.measurements import DataSaver

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


def test_generate_database_creates_named_sinusoidal_runs(tmp_path):
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
        expected_amplitude = expected_generator.uniform(0.5, 1.5)
        expected_phase = expected_generator.uniform(0.0, 2.0 * np.pi)
        np.testing.assert_allclose(
            line_data["current"],
            expected_amplitude
            * np.sin(
                4.0 * np.pi * np.linspace(0.0, 1.0, 5) + expected_phase
            ),
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
        expected_amplitude = expected_generator.uniform(0.5, 1.5)
        expected_v_sd_phase = expected_generator.uniform(0.0, 2.0 * np.pi)
        expected_v_g_phase = expected_generator.uniform(0.0, 2.0 * np.pi)
        expected_v_sd_component = expected_amplitude * np.sin(
            4.0 * np.pi * np.linspace(0.0, 1.0, 3) + expected_v_sd_phase
        )
        expected_v_g_component = expected_amplitude * np.cos(
            4.0 * np.pi * np.linspace(0.0, 1.0, 4) + expected_v_g_phase
        )
        np.testing.assert_allclose(
            map_data["conductance"],
            0.5
            * (
                np.repeat(expected_v_sd_component, 4)
                + np.tile(expected_v_g_component, 3)
            ),
            atol=1e-12,
        )
        assert np.isfinite(map_data["conductance"]).all()
        assert np.min(map_data["conductance"]) >= -1.5
        assert np.max(map_data["conductance"]) <= 1.5
    finally:
        qc.config["core"]["db_location"] = previous_database_path


def test_generate_database_writes_bounded_result_chunks(tmp_path, monkeypatch):
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


def test_cancellation_during_batched_run_removes_temporary_database(
    tmp_path,
    monkeypatch,
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
    assert list(tmp_path.glob(".runs-*.db")) == []


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
