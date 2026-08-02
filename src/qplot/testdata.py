"""Generate synthetic QCoDeS databases from CSV specifications."""

from __future__ import annotations

import argparse
import csv
import math
import os
import re
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from pathlib import Path
from time import perf_counter

import numpy as np
from qcodes.dataset import Measurement, new_experiment
from qcodes.dataset.sqlite.database import connect
from qcodes.parameters import ManualParameter

CSV_COLUMNS = (
    "dimensions",
    "measured_name",
    "measured_label",
    "measured_unit",
    "v_sd_start",
    "v_sd_stop",
    "v_sd_points",
    "v_g_start",
    "v_g_stop",
    "v_g_points",
)

EXAMPLE_ROWS = (
    ("1", "current", "Current", "nA", "-0.01", "0.01", "101", "", "", ""),
    (
        "1",
        "conductance",
        "Conductance",
        "uS",
        "-0.1",
        "0.1",
        "501",
        "",
        "",
        "",
    ),
    (
        "2",
        "current",
        "Current",
        "nA",
        "-0.01",
        "0.01",
        "121",
        "-1",
        "1",
        "81",
    ),
    (
        "2",
        "conductance",
        "Conductance",
        "uS",
        "-0.1",
        "0.1",
        "201",
        "-3",
        "3",
        "101",
    ),
)

INSTRUCTION_FILE_NAMES = (
    "qplot_test_db_01_10mb.csv",
    "qplot_test_db_02_25mb.csv",
    "qplot_test_db_03_50mb.csv",
    "qplot_test_db_04_100mb.csv",
    "qplot_test_db_05_250mb.csv",
    "qplot_test_db_06_500mb.csv",
    "qplot_test_db_07_1gb.csv",
    "qplot_test_db_08_5gb.csv",
    "qplot_test_db_09_10gb.csv",
    "qplot_test_db_10_30gb.csv",
)

_PARAMETER_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MINIMUM_AMPLITUDE = 0.5
_MAXIMUM_AMPLITUDE = 1.5
_RESULT_CHUNK_POINTS = 10_000


class SpecificationError(ValueError):
    """Raised when a test-database CSV specification is invalid."""


class GenerationCancelled(RuntimeError):
    """Raised internally when test-database generation is cancelled."""


@dataclass(frozen=True)
class RunSpecification:
    """Validated settings for one generated QCoDeS run."""

    dimensions: int
    measured_name: str
    measured_label: str
    measured_unit: str
    v_sd_start: float
    v_sd_stop: float
    v_sd_points: int
    v_g_start: float | None = None
    v_g_stop: float | None = None
    v_g_points: int | None = None

    @property
    def point_count(self):
        """Return the number of measured points in the run."""
        if self.dimensions == 1:
            return self.v_sd_points
        assert self.v_g_points is not None
        return self.v_sd_points * self.v_g_points


def _row_error(line_number, message):
    return SpecificationError(f"CSV row {line_number}: {message}")


def _required(row, column, line_number):
    value = (row.get(column) or "").strip()
    if not value:
        raise _row_error(line_number, f"{column} is required")
    return value


def _finite_float(row, column, line_number):
    value = _required(row, column, line_number)
    try:
        number = float(value)
    except ValueError as error:
        raise _row_error(line_number, f"{column} must be a number") from error
    if not math.isfinite(number):
        raise _row_error(line_number, f"{column} must be finite")
    return number


def _point_count(row, column, line_number):
    value = _required(row, column, line_number)
    try:
        points = int(value)
    except ValueError as error:
        raise _row_error(line_number, f"{column} must be an integer") from error
    if points < 2:
        raise _row_error(line_number, f"{column} must be at least 2")
    return points


def _parse_row(row, line_number):
    dimensions_value = _required(row, "dimensions", line_number)
    try:
        dimensions = int(dimensions_value)
    except ValueError as error:
        raise _row_error(line_number, "dimensions must be 1 or 2") from error
    if dimensions not in (1, 2):
        raise _row_error(line_number, "dimensions must be 1 or 2")

    measured_name = _required(row, "measured_name", line_number)
    if not _PARAMETER_NAME_PATTERN.fullmatch(measured_name):
        raise _row_error(
            line_number,
            "measured_name must start with a letter or underscore and contain "
            "only letters, numbers, and underscores",
        )
    if measured_name.lower() in {"v_sd", "v_g"}:
        raise _row_error(
            line_number,
            "measured_name cannot be V_SD or V_G because those names are swept",
        )

    measured_label = _required(row, "measured_label", line_number)
    measured_unit = (row.get("measured_unit") or "").strip()
    v_sd_start = _finite_float(row, "v_sd_start", line_number)
    v_sd_stop = _finite_float(row, "v_sd_stop", line_number)
    if v_sd_start == v_sd_stop:
        raise _row_error(line_number, "v_sd_start and v_sd_stop must differ")
    v_sd_points = _point_count(row, "v_sd_points", line_number)

    gate_values = tuple((row.get(column) or "").strip() for column in CSV_COLUMNS[7:])
    if dimensions == 1:
        if any(gate_values):
            raise _row_error(
                line_number,
                "v_g_start, v_g_stop, and v_g_points must be blank for a 1D run",
            )
        return RunSpecification(
            dimensions=dimensions,
            measured_name=measured_name,
            measured_label=measured_label,
            measured_unit=measured_unit,
            v_sd_start=v_sd_start,
            v_sd_stop=v_sd_stop,
            v_sd_points=v_sd_points,
        )

    v_g_start = _finite_float(row, "v_g_start", line_number)
    v_g_stop = _finite_float(row, "v_g_stop", line_number)
    if v_g_start == v_g_stop:
        raise _row_error(line_number, "v_g_start and v_g_stop must differ")
    v_g_points = _point_count(row, "v_g_points", line_number)
    return RunSpecification(
        dimensions=dimensions,
        measured_name=measured_name,
        measured_label=measured_label,
        measured_unit=measured_unit,
        v_sd_start=v_sd_start,
        v_sd_stop=v_sd_stop,
        v_sd_points=v_sd_points,
        v_g_start=v_g_start,
        v_g_stop=v_g_stop,
        v_g_points=v_g_points,
    )


def read_specifications(csv_path):
    """Read and validate all nonblank rows in a generator CSV file."""
    csv_path = Path(csv_path)
    try:
        handle = csv_path.open("r", encoding="utf-8-sig", newline="")
    except OSError as error:
        raise SpecificationError(f"Could not open {csv_path}: {error}") from error

    with handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames
        if fieldnames is None:
            raise SpecificationError("CSV file is empty")
        fieldnames = [name.strip() for name in fieldnames]
        reader.fieldnames = fieldnames
        if len(fieldnames) != len(set(fieldnames)):
            raise SpecificationError("CSV header contains duplicate columns")
        missing = [column for column in CSV_COLUMNS if column not in fieldnames]
        extra = [column for column in fieldnames if column not in CSV_COLUMNS]
        if missing or extra:
            details = []
            if missing:
                details.append(f"missing columns: {', '.join(missing)}")
            if extra:
                details.append(f"unexpected columns: {', '.join(extra)}")
            raise SpecificationError("Invalid CSV header (" + "; ".join(details) + ")")

        specifications = []
        for line_number, row in enumerate(reader, start=2):
            if None in row and any(str(value).strip() for value in row[None]):
                raise _row_error(line_number, "contains more values than the header")
            if not any((row.get(column) or "").strip() for column in CSV_COLUMNS):
                continue
            specifications.append(_parse_row(row, line_number))

    if not specifications:
        raise SpecificationError("CSV file contains no run specifications")
    return specifications


def write_example_csv(csv_path, overwrite=False):
    """Write a ready-to-use example generator CSV and return its path."""
    csv_path = Path(csv_path)
    if csv_path.suffix.lower() != ".csv":
        raise SpecificationError("Example output path must end in .csv")
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    mode = "w" if overwrite else "x"
    try:
        with csv_path.open(mode, encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(CSV_COLUMNS)
            writer.writerows(EXAMPLE_ROWS)
    except FileExistsError as error:
        raise SpecificationError(
            f"{csv_path} already exists; use --overwrite to replace it"
        ) from error
    except OSError as error:
        raise SpecificationError(f"Could not write {csv_path}: {error}") from error
    return csv_path


def copy_instruction_collection(directory, overwrite=False):
    """Copy the installed cumulative instruction CSV collection to a directory."""
    directory = Path(directory)
    if directory.exists() and not directory.is_dir():
        raise SpecificationError(f"Collection destination is not a directory: {directory}")

    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise SpecificationError(f"Could not create {directory}: {error}") from error

    output_paths = tuple(directory / name for name in INSTRUCTION_FILE_NAMES)
    if not overwrite:
        existing = [path.name for path in output_paths if path.exists()]
        if existing:
            raise SpecificationError(
                f"{directory} already contains collection files; use --overwrite "
                f"to replace them: {', '.join(existing)}"
            )

    resource_directory = resources.files("qplot").joinpath("resources", "testdata")
    try:
        contents = tuple(
            resource_directory.joinpath(name).read_bytes()
            for name in INSTRUCTION_FILE_NAMES
        )
        for output_path, content in zip(output_paths, contents, strict=True):
            mode = "wb" if overwrite else "xb"
            with output_path.open(mode) as handle:
                handle.write(content)
    except OSError as error:
        raise SpecificationError(
            f"Could not copy the instruction CSV collection to {directory}: {error}"
        ) from error

    return output_paths


def _raise_if_cancelled(cancelled_callback):
    if cancelled_callback is not None and cancelled_callback():
        raise GenerationCancelled("Test-database generation was cancelled")


def _result_chunks(point_count):
    for start in range(0, point_count, _RESULT_CHUNK_POINTS):
        yield slice(start, min(start + _RESULT_CHUNK_POINTS, point_count))


def _timestamped_message(message):
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    print(f"[{timestamp}] {message}", flush=True)


def _write_run(
        experiment,
        run_number,
        specification,
        random_generator,
        cancelled_callback=None,
        ):
    _raise_if_cancelled(cancelled_callback)
    v_sd = ManualParameter("V_SD", label="Source-drain voltage", unit="V")
    measured = ManualParameter(
        specification.measured_name,
        label=specification.measured_label,
        unit=specification.measured_unit,
    )
    measurement = Measurement(exp=experiment, name=f"run_{run_number}")
    measurement.register_parameter(v_sd)

    v_sd_values = np.linspace(
        specification.v_sd_start,
        specification.v_sd_stop,
        specification.v_sd_points,
    )
    v_sd_normalized = np.linspace(0.0, 1.0, specification.v_sd_points)
    amplitude = random_generator.uniform(_MINIMUM_AMPLITUDE, _MAXIMUM_AMPLITUDE)
    v_sd_phase = random_generator.uniform(0.0, 2.0 * np.pi)

    if specification.dimensions == 1:
        measurement.register_parameter(measured, setpoints=(v_sd,))
        measured_values = amplitude * np.sin(
            4.0 * np.pi * v_sd_normalized + v_sd_phase
        )
        with measurement.run() as datasaver:
            for result_slice in _result_chunks(specification.v_sd_points):
                _raise_if_cancelled(cancelled_callback)
                datasaver.add_result(
                    (v_sd, v_sd_values[result_slice]),
                    (measured, measured_values[result_slice]),
                )
        return

    assert specification.v_g_start is not None
    assert specification.v_g_stop is not None
    assert specification.v_g_points is not None
    v_g = ManualParameter("V_G", label="Gate voltage", unit="V")
    measurement.register_parameter(v_g)
    measurement.register_parameter(measured, setpoints=(v_sd, v_g))
    v_g_values = np.linspace(
        specification.v_g_start,
        specification.v_g_stop,
        specification.v_g_points,
    )
    v_g_normalized = np.linspace(0.0, 1.0, specification.v_g_points)
    v_g_phase = random_generator.uniform(0.0, 2.0 * np.pi)
    v_g_component = amplitude * np.cos(
        4.0 * np.pi * v_g_normalized + v_g_phase
    )

    with measurement.run() as datasaver:
        for v_sd_index, v_sd_value in enumerate(v_sd_values):
            v_sd_component = amplitude * np.sin(
                4.0 * np.pi * v_sd_normalized[v_sd_index] + v_sd_phase
            )
            measured_values = 0.5 * (v_sd_component + v_g_component)
            for result_slice in _result_chunks(specification.v_g_points):
                _raise_if_cancelled(cancelled_callback)
                result_count = result_slice.stop - result_slice.start
                datasaver.add_result(
                    (v_sd, np.full(result_count, float(v_sd_value))),
                    (v_g, v_g_values[result_slice]),
                    (measured, measured_values[result_slice]),
                )


def generate_database(
        specifications,
        database_path,
        overwrite=False,
        rng=None,
        cancelled_callback=None,
        ):
    """Generate a complete QCoDeS database from validated specifications."""
    specifications = list(specifications)
    if not specifications:
        raise SpecificationError("At least one run specification is required")

    database_path = Path(database_path)
    if database_path.suffix.lower() != ".db":
        raise SpecificationError("Database output path must end in .db")
    if database_path.exists() and not overwrite:
        raise SpecificationError(
            f"{database_path} already exists; use --overwrite to replace it"
        )

    database_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_handle = tempfile.NamedTemporaryFile(
        prefix=f".{database_path.stem}-",
        suffix=".db",
        dir=database_path.parent,
        delete=False,
    )
    temporary_path = Path(temporary_handle.name)
    temporary_handle.close()
    temporary_path.unlink()

    random_generator = rng if rng is not None else np.random.default_rng()
    total_runs = len(specifications)
    total_points = sum(specification.point_count for specification in specifications)
    generation_started = perf_counter()
    _timestamped_message(
        f"Test database generation started: {database_path} "
        f"({total_runs} runs, {total_points} points)."
    )
    try:
        connection = connect(temporary_path)
        try:
            experiment = new_experiment(
                "qplot_test_database",
                sample_name="synthetic",
                conn=connection,
            )
            for run_number, specification in enumerate(specifications, start=1):
                run_started = perf_counter()
                _timestamped_message(
                    f"Run started: run_{run_number} ({run_number}/{total_runs}, "
                    f"{specification.dimensions}D, {specification.point_count} points)."
                )
                try:
                    _write_run(
                        experiment,
                        run_number,
                        specification,
                        random_generator,
                        cancelled_callback=cancelled_callback,
                    )
                except GenerationCancelled:
                    _timestamped_message(
                        f"Run stopped (cancelled): run_{run_number} "
                        f"after {perf_counter() - run_started:.2f} s."
                    )
                    raise
                except Exception as error:
                    _timestamped_message(
                        f"Run stopped (failed): run_{run_number} after "
                        f"{perf_counter() - run_started:.2f} s "
                        f"({type(error).__name__}: {error})."
                    )
                    raise
                _timestamped_message(
                    f"Run stopped (completed): run_{run_number} in "
                    f"{perf_counter() - run_started:.2f} s."
                )
            _raise_if_cancelled(cancelled_callback)
        finally:
            connection.close()
        os.replace(temporary_path, database_path)
    except GenerationCancelled:
        temporary_path.unlink(missing_ok=True)
        _timestamped_message(
            "Test database generation stopped (cancelled) after "
            f"{perf_counter() - generation_started:.2f} s: {database_path}."
        )
        raise
    except Exception as error:
        temporary_path.unlink(missing_ok=True)
        _timestamped_message(
            "Test database generation stopped (failed) after "
            f"{perf_counter() - generation_started:.2f} s: {database_path} "
            f"({type(error).__name__}: {error})."
        )
        raise

    _timestamped_message(
        "Test database generation stopped (completed) in "
        f"{perf_counter() - generation_started:.2f} s: {database_path}."
    )
    return database_path


def generate_database_from_csv(
        csv_path,
        database_path,
        overwrite=False,
        rng=None,
        cancelled_callback=None,
        ):
    """Validate a CSV and generate its QCoDeS database."""
    specifications = read_specifications(csv_path)
    path = generate_database(
        specifications,
        database_path,
        overwrite=overwrite,
        rng=rng,
        cancelled_callback=cancelled_callback,
    )
    return path, specifications


def _argument_parser():
    parser = argparse.ArgumentParser(
        prog="qplot-generate-db",
        description="Generate sinusoidal test runs in a QCoDeS database.",
    )
    parser.add_argument("specification", nargs="?", help="input CSV specification")
    parser.add_argument("database", nargs="?", help="output QCoDeS .db file")
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument(
        "--write-example",
        metavar="CSV",
        help="write a ready-to-use example CSV instead of generating a database",
    )
    output_group.add_argument(
        "--write-collection",
        metavar="DIRECTORY",
        help="copy the installed cumulative instruction CSV collection",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing output file",
    )
    return parser


def main(argv: Sequence[str] | None = None):
    """Run the ``qplot-generate-db`` command-line interface."""
    parser = _argument_parser()
    args = parser.parse_args(argv)

    try:
        if args.write_example:
            if args.specification or args.database:
                parser.error("positional arguments cannot be used with --write-example")
            output_path = write_example_csv(args.write_example, overwrite=args.overwrite)
            print(f"Wrote example CSV: {output_path}")
            return 0

        if args.write_collection:
            if args.specification or args.database:
                parser.error("positional arguments cannot be used with --write-collection")
            output_paths = copy_instruction_collection(
                args.write_collection,
                overwrite=args.overwrite,
            )
            print(
                f"Wrote {len(output_paths)} instruction CSV files to "
                f"{Path(args.write_collection)}"
            )
            return 0

        if not args.specification or not args.database:
            parser.error(
                "specification and database are required unless --write-example or "
                "--write-collection is used"
            )
        output_path, specifications = generate_database_from_csv(
            args.specification,
            args.database,
            overwrite=args.overwrite,
        )
    except (OSError, SpecificationError) as error:
        parser.exit(2, f"qplot-generate-db: error: {error}\n")

    total_points = sum(specification.point_count for specification in specifications)
    _timestamped_message(
        f"Generated {output_path} with {len(specifications)} runs "
        f"and {total_points} points."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
