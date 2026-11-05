"""Specification resolution preserves declaration order and source errors."""

import csv
import subprocess
import sys

import pytest

from qplot import _testdata_specifications as specifications


def _write_csv(tmp_path, rows):
    path = tmp_path / "instructions.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(specifications.CSV_COLUMNS)
        writer.writerows(rows)
    return path


def test_specification_module_import_does_not_load_generation_or_gui():
    subprocess.run(
        [sys.executable, "-c", "\n".join((
            "import sys",
            "from qplot import _testdata_specifications",
            "for module in ('qcodes', 'numpy', 'PyQt6', 'qplot.testdata', 'qplot.windows'):",
            "    assert module not in sys.modules, module",
        ))],
        check=True,
        timeout=10,
    )


def test_public_generator_specification_api_remains_available():
    from qplot import testdata

    for name in (
        "CSV_COLUMNS", "EXAMPLE_ROWS", "SpecificationError",
        "AxisSpecification", "MeasurementSpecification", "RunSpecification",
        "read_specifications", "example_csv_contents",
    ):
        assert getattr(testdata, name) is getattr(specifications, name)


def test_forward_references_preserve_run_measurement_and_axis_order(tmp_path):
    path = _write_csv(tmp_path, [
        ("first", "measurement", "signal", "Signal", "A", "y;x", "", "", ""),
        ("second", "measurement", "signal", "Second signal", "V", "x", "", "", ""),
        ("first", "axis", "y", "Outer", "V", "", "0", "1", "3"),
        ("first", "axis", "x", "Inner", "V", "", "2", "4", "5"),
        ("second", "axis", "x", "Other run", "s", "", "10", "20", "2"),
        ("first", "measurement", "reference", "Reference", "A", "x", "", "", ""),
    ])

    first, second = specifications.read_specifications(path)

    assert (first.name, second.name) == ("first", "second")
    assert [value.name for value in first.measurements] == ["signal", "reference"]
    assert [axis.name for axis in first.axes] == ["y", "x"]
    assert first.measurements[0].axes[1] is first.measurements[1].axes[0]
    assert first.measurements[1].axes[0] is not second.axes[0]
    assert (first.point_count, first.dimension_summary) == (20, "1D/2D")
    assert (second.axes[0].start, second.axes[0].unit) == (10.0, "s")


@pytest.mark.parametrize(("rows", "expected_error"), [
    (
        [
            ("",) * 9,
            ("run", "measurement", "signal", "Signal", "A", "missing", "", "", ""),
            ("run", "axis", "x", "X", "V", "", "0", "1", "2"),
        ],
        "CSV row 3: setpoint 'missing' must name an axis in run 'run'",
    ),
    (
        [
            ("run", "axis", "x", "X", "V", "", "0", "1", "2"),
            ("run", "axis", "X", "X again", "V", "", "0", "1", "2"),
        ],
        "CSV row 3: duplicate parameter name 'X' in run 'run'",
    ),
    (
        [
            ("run", "measurement", "signal", "Signal", "A", "x", "", "", ""),
            ("run", "axis", "unused", "Unused", "V", "", "0", "1", "2"),
            ("run", "axis", "x", "X", "V", "", "0", "1", "2"),
        ],
        "CSV row 3: axis 'unused' is not used by a measurement",
    ),
    (
        [
            ("first", "measurement", "signal", "Signal", "A", "x", "", "", ""),
            ("second", "axis", "x", "X", "V", "", "0", "1", "2"),
        ],
        "CSV row 2: setpoint 'x' must name an axis in run 'first'",
    ),
])
def test_resolution_errors_keep_original_row_and_run(tmp_path, rows, expected_error):
    with pytest.raises(specifications.SpecificationError) as caught:
        specifications.read_specifications(_write_csv(tmp_path, rows))

    assert str(caught.value) == expected_error


def test_non_regular_source_is_rejected_before_reading(tmp_path):
    with pytest.raises(specifications.SpecificationError, match="not a regular file"):
        specifications.read_specifications(tmp_path)
