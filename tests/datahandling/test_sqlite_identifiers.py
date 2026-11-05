"""Identifiers must stay literal SQL names even when they contain SQL syntax."""

import sqlite3

import pytest

from qplot.datahandling._sqlite_identifiers import (
    quote_identifier,
    quote_identifier_text,
)


@pytest.mark.parametrize(
    "name",
    ["runs", 'result "one"', "main.runs", 'a"; DROP TABLE runs;--', "Δ current"],
)
def test_quoted_identifier_selects_only_the_literal_name(name):
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE sentinel (value)")
        connection.execute("INSERT INTO sentinel VALUES (42)")
        quoted = quote_identifier(name)
        connection.execute(f"CREATE TABLE {quoted} ({quoted})")
        connection.execute(f"INSERT INTO {quoted} VALUES (?)", (17,))

        assert connection.execute(f"SELECT {quoted} FROM {quoted}").fetchall() == [(17,)]
        assert connection.execute("SELECT * FROM sentinel").fetchall() == [(42,)]
    finally:
        connection.close()


def test_text_conversion_remains_explicit_for_snapshot_callers():
    assert quote_identifier_text(123) == '"123"'
    assert quote_identifier_text(None) == '"None"'
    with pytest.raises(AttributeError):
        quote_identifier(123)  # type: ignore[arg-type]
