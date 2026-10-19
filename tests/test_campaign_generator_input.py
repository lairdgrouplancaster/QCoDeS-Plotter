"""Invalid instruction files should fail through the generator CLI's error path."""

import csv

import pytest

from qplot.testdata import CSV_COLUMNS, main


@pytest.mark.parametrize("invalid_input", ["encoding", "oversized_field"])
def test_generator_cli_reports_unreadable_csv_without_creating_output(
    tmp_path, capsys, invalid_input,
):
    source = tmp_path / "instructions.csv"
    destination = tmp_path / "measurement.db"
    if invalid_input == "encoding":
        source.write_bytes(b"\xff\xfe\x00invalid encoding")
    else:
        with source.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(CSV_COLUMNS)
            writer.writerow([
                "1", "current", "x" * (csv.field_size_limit() + 1), "A",
                "0", "1", "2", "", "", "",
            ])
    original = source.read_bytes()

    with pytest.raises(SystemExit) as exit_info:
        main([str(source), str(destination)])

    assert exit_info.value.code == 2
    output = capsys.readouterr()
    assert "qplot-generate-db: error:" in output.err
    assert "Traceback" not in output.err
    assert source.read_bytes() == original
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == [source]
