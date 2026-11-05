"""Filename resolution keeps extension choices stable before approval."""

import os

import pytest

from qplot.windows._export_names import ExportSuffixPolicy, normalize_export_path


@pytest.mark.parametrize(("filename", "selected_filter", "enforce_filter", "expected"), [
    ("measurement", "", False, "measurement.csv"),
    ("measurement", "Tab-separated (*.tsv)", False, "measurement.tsv"),
    ("measurement.CSV", "Tab-separated (*.tsv)", False, "measurement.CSV"),
    ("measurement.CSV", "Tab-separated (*.tsv)", True, "measurement.tsv"),
    ("measurement.TSV", "Tab-separated (*.tsv)", True, "measurement.TSV"),
    ("measurement.tsv", "Comma-separated (*.csv)", True, "measurement.csv"),
    ("measurement.raw", "Tab-separated (*.TSV)", False, "measurement.raw.tsv"),
    ("measurement.raw", "Tab-separated (*.TSV)", True, "measurement.raw.tsv"),
    ("measurement.tsv", "Unsupported (*.bin)", True, "measurement.tsv"),
    ("measurement.raw", "Unsupported (*.bin)", True, "measurement.raw.csv"),
    ("measurement", "All files (*)", True, "measurement.csv"),
    ("measurement", "Data (*.tsv *.csv)", True, "measurement.tsv"),
    ("measurement", "Data (*.bin *.tsv)", True, "measurement.csv"),
    ("measurement", "Data (*.csv.gz)", True, "measurement.csv"),
    (".measurement", "Tab-separated (*.tsv)", True, ".measurement.tsv"),
    ("measurement.", "Tab-separated (*.tsv)", True, "measurement..tsv"),
    ("folder.csv/measurement", "", True, "folder.csv/measurement.csv"),
])
def test_resolve_export_name(filename, selected_filter, enforce_filter, expected):
    policy = ExportSuffixPolicy(("CSV", ".TSV"), "Csv")
    assert policy.resolve(
        filename, selected_filter, match_selected_filter=enforce_filter,
    ) == os.path.abspath(expected)


@pytest.mark.parametrize(("suffixes", "default", "message"), [
    ((), ".csv", "At least one export suffix"),
    ((".csv",), ".tsv", "default export suffix must also be allowed"),
])
def test_invalid_format_policy(suffixes, default, message):
    with pytest.raises(ValueError, match=message):
        ExportSuffixPolicy(suffixes, default)


@pytest.mark.parametrize(("filename", "suffix", "expected"), [
    ("plot", "pdf", "plot.pdf"),
    ("plot.PDF", ".pdf", "plot.PDF"),
    ("plot.csv", ".pdf", "plot.csv.pdf"),
    ("plot.pdf.pdf", ".pdf", "plot.pdf.pdf"),
])
def test_required_suffix_preserves_existing_spelling(filename, suffix, expected):
    assert normalize_export_path(filename, suffix) == os.path.abspath(expected)
