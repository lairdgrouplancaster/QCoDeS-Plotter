"""Specification records and CSV validation for synthetic QCoDeS data.

This reader has no generator, QCoDeS, or GUI dependencies. Its only SQL work is
an in-memory syntax check for names that QCoDeS emits as unquoted identifiers;
physical instruction files are opened only for reading.
"""

from __future__ import annotations

import csv
import io
import math
import re
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path

CSV_COLUMNS = (
    "run",
    "kind",
    "name",
    "label",
    "unit",
    "setpoints",
    "start",
    "stop",
    "points",
)

# Setpoints are ordered from the outermost to the innermost sweep.
EXAMPLE_ROWS = (
    ("line", "axis", "V_SD", "Source-drain voltage", "V", "", "-0.1", "0.1", "501"),
    ("line", "measurement", "current", "Current", "nA", "V_SD", "", "", ""),
    ("map", "axis", "V_SD", "Source-drain voltage", "V", "", "-0.1", "0.1", "121"),
    ("map", "axis", "V_G", "Gate voltage", "V", "", "-1", "1", "81"),
    ("map", "measurement", "current", "Current", "nA", "V_SD;V_G", "", "", ""),
    ("mixed", "axis", "V_SD", "Source-drain voltage", "V", "", "-0.1", "0.1", "21"),
    ("mixed", "axis", "V_G", "Gate voltage", "V", "", "-1", "1", "11"),
    ("mixed", "axis", "field", "Magnetic field", "T", "", "0", "1", "3"),
    ("mixed", "axis", "temperature", "Temperature", "K", "", "0.1", "1", "2"),
    ("mixed", "measurement", "current", "Current", "nA", "V_SD", "", "", ""),
    (
        "mixed",
        "measurement",
        "conductance",
        "Conductance",
        "uS",
        "V_SD;V_G",
        "",
        "",
        "",
    ),
    (
        "mixed",
        "measurement",
        "volume",
        "Volume signal",
        "a.u.",
        "field;V_G;V_SD",
        "",
        "",
        "",
    ),
    (
        "mixed",
        "measurement",
        "hypervolume",
        "4D signal",
        "a.u.",
        "temperature;field;V_G;V_SD",
        "",
        "",
        "",
    ),
)


_PARAMETER_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SpecificationError(ValueError):
    """Raised when a test-database CSV specification is invalid."""


def _result_column_name_is_usable(name):
    """Return whether SQLite accepts ``name`` as QCoDeS will emit it."""
    connection = sqlite3.connect(":memory:")
    try:
        # Parameter names have already passed _PARAMETER_NAME_PATTERN, so this
        # parser probe cannot introduce additional SQL tokens. Keeping the
        # identifier unquoted deliberately mirrors QCoDeS result-table SQL.
        connection.execute(
            f"CREATE TABLE qplot_results (id INTEGER PRIMARY KEY, {name} numeric)"
        )
    except sqlite3.DatabaseError:
        return False
    finally:
        connection.close()
    return True


@dataclass(frozen=True)
class AxisSpecification:
    """One shared, independent QCoDeS setpoint parameter."""

    name: str
    label: str
    unit: str
    start: float
    stop: float
    points: int


@dataclass(frozen=True)
class MeasurementSpecification:
    """A measured quantity and its ordered independent axes."""

    name: str
    label: str
    unit: str
    axes: tuple[AxisSpecification, ...]

    @property
    def dimensions(self):
        return len(self.axes)

    @property
    def point_count(self):
        return math.prod(axis.points for axis in self.axes)


@dataclass(frozen=True)
class RunSpecification:
    """Measured quantities acquired together in one QCoDeS run."""

    measurements: tuple[MeasurementSpecification, ...]
    name: str = ""

    @property
    def axes(self):
        return tuple(
            {
                axis.name: axis
                for measured in self.measurements
                for axis in measured.axes
            }.values()
        )

    @property
    def point_count(self):
        """Number of stored measurement samples, including all quantities."""
        return sum(measured.point_count for measured in self.measurements)

    @property
    def dimension_summary(self):
        return "/".join(
            f"{value}D" for value in sorted({m.dimensions for m in self.measurements})
        )


@dataclass(frozen=True)
class _MeasurementDeclaration:
    """A measurement whose setpoint names await per-run resolution."""

    name: str
    label: str
    unit: str
    setpoint_names: tuple[str, ...]


@dataclass(frozen=True)
class _Declaration:
    """A parsed value and its original CSV row for resolution errors."""

    line_number: int
    value: AxisSpecification | _MeasurementDeclaration


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


def _parse_runs(rows):
    """Resolve per-run declarations, allowing forward setpoint references."""
    runs: dict[str, dict[str, _Declaration]] = {}
    for line_number, row in rows:
        run = _required(row, "run", line_number)
        kind = _required(row, "kind", line_number)
        if kind not in {"axis", "measurement"}:
            raise _row_error(line_number, "kind must be axis or measurement")
        name = _required(row, "name", line_number)
        if not _PARAMETER_NAME_PATTERN.fullmatch(name):
            raise _row_error(
                line_number,
                "name must start with a letter or underscore and contain only letters, numbers, and underscores",
            )
        if not _result_column_name_is_usable(name):
            raise _row_error(
                line_number, "name cannot be used as a QCoDeS result column"
            )
        declarations = runs.setdefault(run, {})
        if name.lower() in {existing.lower() for existing in declarations}:
            raise _row_error(
                line_number, f"duplicate parameter name {name!r} in run {run!r}"
            )
        label = _required(row, "label", line_number)
        unit = (row.get("unit") or "").strip()
        value: AxisSpecification | _MeasurementDeclaration
        if kind == "axis":
            if (row.get("setpoints") or "").strip():
                raise _row_error(line_number, "axis setpoints must be blank")
            start = _finite_float(row, "start", line_number)
            stop = _finite_float(row, "stop", line_number)
            if start == stop:
                raise _row_error(line_number, "start and stop must differ")
            value = AxisSpecification(
                name, label, unit, start, stop, _point_count(row, "points", line_number)
            )
        else:
            if any(
                (row.get(column) or "").strip()
                for column in ("start", "stop", "points")
            ):
                raise _row_error(
                    line_number, "measurement start, stop, and points must be blank"
                )
            names = tuple(
                part.strip()
                for part in _required(row, "setpoints", line_number).split(";")
            )
            if not all(names) or len(set(names)) != len(names):
                raise _row_error(
                    line_number,
                    "setpoints must be distinct axis names separated by semicolons",
                )
            value = _MeasurementDeclaration(name, label, unit, names)
        declarations[name] = _Declaration(line_number, value)

    specifications = []
    for run, declarations in runs.items():
        measurements = []
        used_axes = set()
        for declaration in declarations.values():
            value = declaration.value
            if isinstance(value, AxisSpecification):
                continue
            axes = []
            for axis_name in value.setpoint_names:
                declared = declarations.get(axis_name)
                if declared is None or not isinstance(declared.value, AxisSpecification):
                    raise _row_error(
                        declaration.line_number,
                        f"setpoint {axis_name!r} must name an axis in run {run!r}",
                    )
                axes.append(declared.value)
                used_axes.add(axis_name)
            measurements.append(
                MeasurementSpecification(value.name, value.label, value.unit, tuple(axes))
            )
        if not measurements:
            raise SpecificationError(f"Run {run!r} contains no measurements")
        for name, declaration in declarations.items():
            if isinstance(declaration.value, AxisSpecification) and name not in used_axes:
                raise _row_error(
                    declaration.line_number, f"axis {name!r} is not used by a measurement"
                )
        specifications.append(RunSpecification(tuple(measurements), name=run))
    return specifications


def read_specifications(csv_path):
    """Read and validate all nonblank rows in a generator CSV file."""
    csv_path = Path(csv_path)
    try:
        # The GUI validates this input before starting its generation worker.
        # A FIFO must not block that synchronous read waiting for a writer.
        if not stat.S_ISREG(csv_path.stat().st_mode):
            raise OSError("The instruction CSV source is not a regular file.")
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

        rows = []
        for line_number, row in enumerate(reader, start=2):
            if None in row and any(str(value).strip() for value in row[None]):
                raise _row_error(line_number, "contains more values than the header")
            if not any((row.get(column) or "").strip() for column in CSV_COLUMNS):
                continue
            rows.append((line_number, row))

    if not rows:
        raise SpecificationError("CSV file contains no run specifications")
    return _parse_runs(rows)


def example_csv_contents():
    """Return example CSV bytes for either CLI or guarded GUI publication."""
    content = io.StringIO(newline="")
    writer = csv.writer(content)
    writer.writerow(CSV_COLUMNS)
    writer.writerows(EXAMPLE_ROWS)
    return content.getvalue().encode("utf-8-sig")
