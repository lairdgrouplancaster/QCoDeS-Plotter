"""Deterministic fixed-query tests for trusted QCoDeS metadata plans."""

from __future__ import annotations

import builtins
import io
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from qplot.datahandling import trusted_live_queries as trusted_queries_module
from qplot.datahandling.trusted_live import (
    TRUSTED_LIVE_MAX_ROWS_PER_RESULT,
    TRUSTED_LIVE_MAX_SCALAR_BYTES,
    TrustedQuery,
    TrustedQueryResult,
)
from qplot.datahandling.trusted_live_queries import (
    TRUSTED_RUN_PAGE_SIZE,
    TRUSTED_RUN_PAGE_SIZE_MAX,
    TrustedMetadataQueryAdapter,
    TrustedMetadataQueryError,
    TrustedParameterView,
    TrustedSetpointSummary,
    quote_sqlite_identifier,
)
from qplot.datahandling.trusted_presentation import (
    TRUSTED_PRESENTATION_MAX_FIELD_VALUE_BYTES,
    TRUSTED_PRESENTATION_MAX_PARAMETER_DEPENDENCIES,
    TRUSTED_PRESENTATION_MAX_PARAMETER_TEXT_BYTES,
    TRUSTED_PRESENTATION_MAX_PARAMETER_TOTAL_TEXT_BYTES,
    TRUSTED_PRESENTATION_MAX_PARAMETERS,
    TRUSTED_PRESENTATION_MAX_RENDERED_NODES,
    TRUSTED_PRESENTATION_MAX_TOOLTIP_BYTES,
    TRUSTED_PRESENTATION_MAX_UNAVAILABLE_FIELDS,
)

_RUN_COLUMNS = (
    "run_id",
    "exp_id",
    "name",
    "result_table_name",
    "result_counter",
    "run_timestamp",
    "completed_timestamp",
    "is_completed",
    "parameters",
    "guid",
    "run_description",
    "snapshot",
    "parent_datasets",
    "captured_run_id",
    "captured_counter",
    "measurement_exception",
    "operator",
)
_EXPERIMENT_COLUMNS = ("exp_id", "name", "sample_name")
_LAYOUT_COLUMNS = (
    "layout_id",
    "run_id",
    "parameter",
    "label",
    "unit",
    "inferred_from",
)


def _description() -> str:
    return json.dumps(
        {
            "interdependencies_": {
                "parameters": {
                    "x": {"label": "X gate", "unit": "V", "type": "numeric"},
                    "y": {"label": "Y gate", "unit": "V", "type": "numeric"},
                    "signal": {
                        "label": "Signal",
                        "unit": "A",
                        "type": "numeric",
                    },
                },
                "dependencies": {"signal": ["x", "y"]},
            }
        },
        separators=(",", ":"),
    )


def _run(run_id: int) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "exp_id": 1,
        "name": f"run-{run_id}",
        "result_table_name": f'results "{run_id}"',
        "result_counter": 6,
        "run_timestamp": 1000.0 + run_id,
        "completed_timestamp": 2000.0 + run_id,
        "is_completed": 1,
        "parameters": "x,y,signal",
        "guid": f"00000000-0000-0000-0000-{run_id:012d}",
        "run_description": _description(),
        "snapshot": json.dumps({"station": {"run_id": run_id}}),
        "parent_datasets": "[]",
        "captured_run_id": run_id,
        "captured_counter": run_id,
        "measurement_exception": None,
        "operator": f"operator-{run_id}",
    }


class _FakeExecutor:
    """Execute only the adapter's bounded fixed statements, without SQLite."""

    def __init__(self, run_ids: tuple[int, ...], *, data_version: int = 17):
        self.incarnation = 41
        self.current_data_version = data_version
        self.result_id_primary_key = True
        self.run_columns = list(_RUN_COLUMNS)
        self.runs = {run_id: _run(run_id) for run_id in run_ids}
        self.result_columns = {
            row["result_table_name"]: ("id", "x", "y", "signal")
            for row in self.runs.values()
        }
        self.result_counts = {row["result_table_name"]: 6 for row in self.runs.values()}
        self.layouts: dict[int, tuple[tuple[Any, ...], ...]] = {
            run_id: (
                (1, "x", "layout x", "layout V", None),
                (2, "y", "layout y", "layout V", None),
                (3, "signal", "layout signal", "layout A", None),
            )
            for run_id in self.runs
        }
        self.events: list[tuple[Any, ...]] = []
        self.queries: list[TrustedQuery] = []
        self.batches: list[tuple[TrustedQuery, ...]] = []
        self.layout_page_ids: list[tuple[int, ...]] = []

    def add_run(self, run_id: int) -> None:
        row = _run(run_id)
        self.runs[run_id] = row
        table_name = row["result_table_name"]
        self.result_columns[table_name] = ("id", "x", "y", "signal")
        self.result_counts[table_name] = 6
        self.layouts[run_id] = (
            (1, "x", "layout x", "layout V", None),
            (2, "y", "layout y", "layout V", None),
            (3, "signal", "layout signal", "layout A", None),
        )

    def clear_history(self) -> None:
        self.events.clear()
        self.queries.clear()
        self.batches.clear()
        self.layout_page_ids.clear()

    def data_version(
        self,
        *,
        timeout: float | None = None,
        wait_timeout: float | None = None,
    ) -> int:
        del timeout, wait_timeout
        self.events.append(("data_version", self.current_data_version))
        return self.current_data_version

    def query(
        self,
        sql: str,
        bindings: object = None,
        *,
        timeout: float | None = None,
        wait_timeout: float | None = None,
    ) -> TrustedQueryResult:
        del timeout, wait_timeout
        query = TrustedQuery(sql, bindings)
        self.events.append(("query", query.sql, query.bindings))
        self.queries.append(query)
        return self._execute(query)

    def query_batch(
        self,
        queries: tuple[TrustedQuery, ...],
        *,
        timeout: float | None = None,
        wait_timeout: float | None = None,
    ) -> tuple[TrustedQueryResult, ...]:
        del timeout, wait_timeout
        specifications = tuple(queries)
        self.events.append(("batch", tuple(query.sql for query in specifications)))
        self.batches.append(specifications)
        self.queries.extend(specifications)
        return tuple(self._execute(query) for query in specifications)

    def _execute(self, query: TrustedQuery) -> TrustedQueryResult:
        sql = query.sql
        assert "PRAGMA" not in sql.upper(), f"trusted plan issued a PRAGMA: {sql}"

        schema_results = {
            'SELECT * FROM "runs" WHERE 0': tuple(self.run_columns),
            'SELECT * FROM "experiments" WHERE 0': _EXPERIMENT_COLUMNS,
            'SELECT * FROM "layouts" WHERE 0': _LAYOUT_COLUMNS,
        }
        if sql in schema_results:
            return TrustedQueryResult(schema_results[sql], ())

        if sql == 'SELECT COALESCE(MAX("run_id"), 0) FROM "runs"':
            maximum = max(self.runs, default=0)
            return TrustedQueryResult(("maximum",), ((maximum,),))

        if sql.startswith("SELECT typeof(") and (
            ' AS "value_type", octet_length(' in sql
        ):
            match = re.match(r'^SELECT typeof\("((?:[^"]|"")*)"\)', sql)
            assert match is not None
            column = match.group(1).replace('""', '"')
            (run_id,) = self._bindings(query)
            run = self.runs.get(run_id)
            if run is None:
                return TrustedQueryResult(("value_type", "value_bytes"), ())
            value_type, value_bytes = self._sqlite_type_and_size(run[column])
            return TrustedQueryResult(
                ("value_type", "value_bytes"),
                ((value_type, value_bytes),),
            )

        if sql.startswith("SELECT CASE WHEN ") and (
            ' FROM "runs" WHERE "run_id" = ?' in sql
        ):
            return self._guarded_run_query(sql, query.bindings)

        if sql.endswith(' FROM "runs" WHERE "run_id" = ?'):
            return self._selected_run_query(sql, query.bindings)

        if 'FROM "runs" AS runs' in sql:
            return self._run_query(sql, query.bindings)

        if sql.startswith("SELECT * FROM ") and sql.endswith(" WHERE 0"):
            table_name = self._result_table_for_sql(sql)
            return TrustedQueryResult(self.result_columns[table_name], ())

        if sql.startswith(
            "SELECT CASE WHEN octet_length(sql) <= ? THEN sql ELSE NULL END"
        ):
            maximum, table_name = self._bindings(query)
            assert table_name in self.result_columns
            columns = ", ".join(
                (
                    "id INTEGER PRIMARY KEY"
                    if self.result_id_primary_key
                    else "id INTEGER"
                )
                if name == "id"
                else f'"{name}" numeric'
                for name in self.result_columns[table_name]
            )
            schema_sql = f'CREATE TABLE "{table_name}" ({columns})'
            schema_bytes = len(schema_sql.encode("utf-8"))
            return TrustedQueryResult(
                ("sql", "sql_bytes"),
                (((schema_sql if schema_bytes <= maximum else None), schema_bytes),),
            )

        if sql.startswith('SELECT COALESCE(MAX("id"), 0) AS result_count FROM '):
            table_name = self._result_table_for_sql(sql)
            return TrustedQueryResult(
                ("result_count",),
                ((self.result_counts[table_name],),),
            )

        if sql.startswith("SELECT (SELECT COUNT(*) FROM (SELECT DISTINCT "):
            table_name = self._result_table_for_sql(sql)
            assert self.result_columns[table_name] == ("id", "x", "y", "signal")
            assert self._bindings(query) == (
                self.result_counts[table_name],
                self.result_counts[table_name],
            )
            return TrustedQueryResult(
                ("tuple_count", "axis_0", "axis_1"),
                ((6, 2, 3),),
            )

        if sql.startswith("WITH distinct_values(value, first_rowid) AS ("):
            table_name = self._result_table_for_sql(sql)
            assert self._bindings(query) == (self.result_counts[table_name],)
            if quote_sqlite_identifier("x") in sql:
                row = (0.0, 1.0, 2)
            elif quote_sqlite_identifier("y") in sql:
                row = (10.0, 30.0, 3)
            else:  # pragma: no cover - any new query is a test failure below
                raise AssertionError(f"unexpected summary query: {sql}")
            assert table_name in self.result_columns
            return TrustedQueryResult(("first", "last", "steps"), (row,))

        if sql.startswith("SELECT (SELECT ") and 'ORDER BY "id" ' in sql:
            table_name = self._result_table_for_sql(sql)
            bindings = self._bindings(query)
            assert len(bindings) % 2 == 0
            bounds = tuple(zip(bindings[::2], bindings[1::2], strict=True))
            assert all(
                0 <= lower < upper <= self.result_counts[table_name]
                for lower, upper in bounds
            )
            ascending = 'ORDER BY "id" ASC' in sql
            parameters = tuple(
                name.replace('""', '"')
                for name in re.findall(
                    r'\(SELECT "((?:[^"]|"")*)" FROM ',
                    sql,
                )
            )
            values = []
            for parameter in parameters:
                if parameter == "x":
                    values.append(0.0 if ascending else 1.0)
                elif parameter == "y":
                    values.append(10.0 if ascending else 30.0)
                else:  # pragma: no cover - any new query is a test failure below
                    raise AssertionError(f"unexpected edge query: {sql}")
            columns = tuple(f"value_{index}" for index in range(len(values)))
            return TrustedQueryResult(columns, (tuple(values),))

        if sql == "SELECT SUM(pgsize) AS storage_bytes FROM dbstat WHERE name = ?":
            table_name = self._bindings(query)[0]
            assert table_name in self.result_columns
            return TrustedQueryResult(("storage_bytes",), ((8192,),))

        if sql == (
            'SELECT COALESCE(MAX("layout_id"), 0) FROM "layouts" WHERE "run_id" = ?'
        ):
            run_id = self._bindings(query)[0]
            assert run_id in self.runs
            maximum = max((row[0] for row in self.layouts[run_id]), default=0)
            return TrustedQueryResult(("maximum",), ((maximum,),))

        if sql.startswith("SELECT layout_id, CASE WHEN octet_length(parameter)") and (
            "FROM layouts WHERE run_id = ? AND layout_id > ?" in sql
        ):
            run_id, after, through, limit = self._bindings(query)
            assert run_id in self.runs
            source_rows = tuple(
                row for row in sorted(self.layouts[run_id]) if after < row[0] <= through
            )[:limit]
            self.layout_page_ids.append(tuple(row[0] for row in source_rows))
            rows = tuple(
                (
                    row[0],
                    *(
                        value
                        if self._sqlite_type_and_size(value)[1] in {None}
                        or self._sqlite_type_and_size(value)[1] <= 512
                        else None
                        for value in row[1:]
                    ),
                    *(self._sqlite_type_and_size(value)[1] for value in row[1:]),
                )
                for row in source_rows
            )
            return TrustedQueryResult(
                (
                    "layout_id",
                    "parameter",
                    "label",
                    "unit",
                    "inferred_from",
                    "octet_length(parameter)",
                    "octet_length(label)",
                    "octet_length(unit)",
                    "octet_length(inferred_from)",
                ),
                rows,
            )

        raise AssertionError(f"unexpected trusted query: {sql}")

    def _run_query(
        self,
        sql: str,
        bindings: object,
    ) -> TrustedQueryResult:
        aliases = tuple(
            alias.replace('""', '"')
            for alias in re.findall(r' AS "((?:[^"]|"")*)"', sql.split(" FROM ", 1)[0])
        )
        values = tuple(bindings or ())
        if 'ORDER BY runs."run_id" LIMIT ?' in sql:
            after, through, limit = values
            selected_ids = [
                run_id for run_id in sorted(self.runs) if after < run_id <= through
            ][:limit]
        else:
            (run_id,) = values
            selected_ids = [run_id] if run_id in self.runs else []

        rows = []
        for run_id in selected_ids:
            run = self.runs[run_id]
            projected = []
            for alias in aliases:
                if alias == "exp_name":
                    projected.append("experiment one")
                elif alias == "sample_name":
                    projected.append("sample one")
                else:
                    projected.append(run[alias])
            rows.append(tuple(projected))
        return TrustedQueryResult(aliases, tuple(rows))

    def _selected_run_query(
        self,
        sql: str,
        bindings: object,
    ) -> TrustedQueryResult:
        select_clause = sql.removeprefix("SELECT ").split(' FROM "runs"', 1)[0]
        columns = tuple(
            column.replace('""', '"')
            for column in re.findall(r'"((?:[^"]|"")*)"', select_clause)
        )
        values = tuple(bindings or ())
        (run_id,) = values
        run = self.runs.get(run_id)
        rows = () if run is None else (tuple(run[column] for column in columns),)
        return TrustedQueryResult(columns, rows)

    def _guarded_run_query(
        self,
        sql: str,
        bindings: object,
    ) -> TrustedQueryResult:
        columns = tuple(
            alias.replace('""', '"')
            for alias in re.findall(r' AS "((?:[^"]|"")*)"', sql)
        )
        values = tuple(bindings or ())
        run_id = values[-1]
        run = self.runs.get(run_id)
        if run is None:
            return TrustedQueryResult(columns, ())
        guards = values[:-1]
        assert len(guards) == 2 * len(columns)
        row = []
        for index, column in enumerate(columns):
            observed_type, observed_size = guards[index * 2 : index * 2 + 2]
            actual_type, actual_size = self._sqlite_type_and_size(run[column])
            row.append(
                run[column]
                if (actual_type, actual_size) == (observed_type, observed_size)
                else None
            )
        return TrustedQueryResult(columns, (tuple(row),))

    @staticmethod
    def _sqlite_type_and_size(value: object) -> tuple[str, int | None]:
        if value is None:
            return "null", None
        if isinstance(value, bytes):
            return "blob", len(value)
        if isinstance(value, str):
            return "text", len(value.encode("utf-8"))
        if isinstance(value, float):
            return "real", len(str(value).encode("utf-8"))
        if isinstance(value, (bool, int)):
            return "integer", len(str(int(value)).encode("utf-8"))
        raise AssertionError(f"unsupported fake SQLite value: {value!r}")

    def _result_table_for_sql(self, sql: str) -> str:
        matches = [
            table_name
            for table_name in self.result_columns
            if quote_sqlite_identifier(table_name) in sql
        ]
        assert len(matches) == 1, f"could not resolve result table in: {sql}"
        return matches[0]

    @staticmethod
    def _bindings(query: TrustedQuery) -> tuple[Any, ...]:
        bindings = query.bindings
        assert isinstance(bindings, tuple)
        return bindings


def _adapter(
    tmp_path: Path,
    run_ids: tuple[int, ...],
    *,
    page_size: int = 2,
    data_version: int = 17,
) -> tuple[TrustedMetadataQueryAdapter, _FakeExecutor]:
    executor = _FakeExecutor(run_ids, data_version=data_version)
    database_path = tmp_path / "accepted.db"
    database_path.touch()
    adapter = TrustedMetadataQueryAdapter(
        executor,
        database_path,
        page_size=page_size,
    )
    return adapter, executor


def _assert_frozen_primitive(value: object) -> None:
    if value is None or isinstance(value, (bool, int, float, str, bytes)):
        return
    assert isinstance(value, tuple)
    for item in value:
        _assert_frozen_primitive(item)


def _load_basic_pages(
    adapter: TrustedMetadataQueryAdapter,
    after_run_id: int,
    through_run_id: int,
):
    records = []
    cursor = after_run_id
    while True:
        page = adapter.basic_run_page(cursor, through_run_id)
        records.extend(page.runs)
        if page.complete:
            return tuple(records)
        assert page.next_run_id > cursor
        cursor = page.next_run_id


def test_quote_sqlite_identifier_doubles_quotes_and_rejects_invalid_values():
    assert quote_sqlite_identifier("runs") == '"runs"'
    assert quote_sqlite_identifier('result "one"') == '"result ""one"""'

    for invalid in (None, 1, "", "nul\x00inside"):
        with pytest.raises(TrustedMetadataQueryError, match="identifier is invalid"):
            quote_sqlite_identifier(invalid)


def test_bootstrap_uses_zero_row_schema_and_captures_version_before_watermark(
    tmp_path,
):
    adapter, executor = _adapter(tmp_path, (1, 2, 3), data_version=23)

    result = adapter.bootstrap()

    assert result.run_id_watermark == 3
    assert result.data_version == 23
    assert result.helper_incarnation == 41
    assert not hasattr(result, "runs")
    assert executor.batches[0] == tuple(
        TrustedQuery(sql)
        for sql in (
            'SELECT * FROM "runs" WHERE 0',
            'SELECT * FROM "experiments" WHERE 0',
            'SELECT * FROM "layouts" WHERE 0',
        )
    )
    assert executor.events[1] == ("data_version", 23)
    assert executor.events[2][0:2] == (
        "query",
        'SELECT COALESCE(MAX("run_id"), 0) FROM "runs"',
    )
    assert not any('FROM "runs" AS runs' in query.sql for query in executor.queries)
    assert all("PRAGMA" not in query.sql.upper() for query in executor.queries)


def test_basic_run_list_is_paged_without_accessing_any_result_table(tmp_path):
    adapter, executor = _adapter(tmp_path, (1, 2, 3, 4, 5), page_size=2)

    header = adapter.bootstrap()
    records = _load_basic_pages(adapter, 0, header.run_id_watermark)

    assert [record.run_id for record in records] == [1, 2, 3, 4, 5]
    page_queries = [
        query
        for query in executor.queries
        if 'ORDER BY runs."run_id" LIMIT ?' in query.sql
    ]
    assert [query.bindings for query in page_queries] == [
        (0, 5, 2),
        (2, 5, 2),
        (4, 5, 2),
    ]
    assert all(
        quote_sqlite_identifier(run["result_table_name"]) not in query.sql
        for query in executor.queries
        for run in executor.runs.values()
    )
    assert records[0].as_dict()["measure_parameters"] == []
    assert records[0].as_dict()["sweep_parameters"] == []
    assert all("run_description" not in query.sql for query in page_queries)
    assert all('runs."parameters"' not in query.sql for query in page_queries)


def test_refresh_reconciles_same_version_when_data_version_respawns_helper(tmp_path):
    class RespawnDuringVersionExecutor(_FakeExecutor):
        respawn_on_next_version = False

        def data_version(self, **options: object) -> int:
            del options
            if self.respawn_on_next_version:
                self.incarnation += 1
                self.respawn_on_next_version = False
            return super().data_version()

    executor = RespawnDuringVersionExecutor((1,), data_version=17)
    source = tmp_path / "accepted.db"
    source.touch()
    adapter = TrustedMetadataQueryAdapter(executor, source)
    bootstrap = adapter.bootstrap()
    _load_basic_pages(adapter, 0, bootstrap.run_id_watermark)
    adapter.refresh_new_runs(accepted_run_id=1)
    executor.clear_history()

    executor.respawn_on_next_version = True
    refresh = adapter.refresh_new_runs(accepted_run_id=1)

    assert refresh.data_version == bootstrap.data_version == 17
    assert refresh.helper_incarnation == bootstrap.helper_incarnation + 1
    assert refresh.data_version_changed
    assert (refresh.prior_run_id_watermark, refresh.run_id_watermark) == (1, 1)
    assert any(
        batch[0].sql == 'SELECT * FROM "runs" WHERE 0' for batch in executor.batches
    )
    assert any('MAX("run_id")' in query.sql for query in executor.queries)


def test_changed_data_version_revalidates_dynamic_run_schema(tmp_path):
    adapter, executor = _adapter(tmp_path, (1,), data_version=17)
    header = adapter.bootstrap()
    _load_basic_pages(adapter, 0, header.run_id_watermark)

    dynamic_name = 'operator "note"'
    executor.run_columns.append(dynamic_name)
    executor.runs[1][dynamic_name] = "added after bootstrap"
    executor.current_data_version = 18

    refresh = adapter.refresh_new_runs()
    detail = adapter.selected_run_detail(1)

    assert refresh.data_version_changed
    assert dict(detail.metadata)[dynamic_name] == "added after bootstrap"
    schema_batches = [
        batch
        for batch in executor.batches
        if batch[0].sql == 'SELECT * FROM "runs" WHERE 0'
    ]
    assert len(schema_batches) == 2


def test_commit_during_multi_page_bootstrap_is_discovered_once_on_first_refresh(
    tmp_path,
):
    class CommitAfterFirstPageExecutor(_FakeExecutor):
        def __init__(self):
            super().__init__((1, 2, 3, 4, 5), data_version=31)
            self.commit_injected = False
            self.page_sizes = []

        def query(
            self,
            sql: str,
            bindings: object = None,
            *,
            timeout: float | None = None,
            wait_timeout: float | None = None,
        ) -> TrustedQueryResult:
            result = super().query(
                sql,
                bindings,
                timeout=timeout,
                wait_timeout=wait_timeout,
            )
            if 'ORDER BY runs."run_id" LIMIT ?' in sql:
                self.page_sizes.append(len(result.rows))
                if not self.commit_injected:
                    self.add_run(6)
                    self.current_data_version += 1
                    self.commit_injected = True
            return result

    executor = CommitAfterFirstPageExecutor()
    adapter = TrustedMetadataQueryAdapter(
        executor,
        tmp_path / "accepted.db",
        page_size=2,
    )

    bootstrap = adapter.bootstrap()
    bootstrap_records = _load_basic_pages(
        adapter,
        0,
        bootstrap.run_id_watermark,
    )

    assert executor.commit_injected
    assert bootstrap.run_id_watermark == 5
    assert [record.run_id for record in bootstrap_records] == [1, 2, 3, 4, 5]
    assert executor.page_sizes == [2, 2, 1]

    first_refresh = adapter.refresh_new_runs()
    first_refresh_records = _load_basic_pages(
        adapter,
        first_refresh.prior_run_id_watermark,
        first_refresh.run_id_watermark,
    )
    assert first_refresh.data_version_changed
    assert (
        first_refresh.prior_run_id_watermark,
        first_refresh.run_id_watermark,
    ) == (5, 6)
    assert [record.run_id for record in first_refresh_records] == [6]

    executor.clear_history()
    second_refresh = adapter.refresh_new_runs()
    assert not second_refresh.data_version_changed
    assert (
        second_refresh.prior_run_id_watermark,
        second_refresh.run_id_watermark,
    ) == (6, 6)
    assert executor.queries == []
    all_run_ids = [
        record.run_id for record in (*bootstrap_records, *first_refresh_records)
    ]
    assert all_run_ids == [1, 2, 3, 4, 5, 6]
    assert all_run_ids.count(6) == 1


def test_high_run_count_drains_bounded_pages_without_omission_or_duplication(
    tmp_path,
):
    run_count = TRUSTED_RUN_PAGE_SIZE * 12 + 137
    expected_run_ids = tuple(range(1, run_count + 1))
    executor = _FakeExecutor(expected_run_ids)
    adapter = TrustedMetadataQueryAdapter(executor, tmp_path / "accepted.db")
    bootstrap = adapter.bootstrap()
    cursor = 0
    observed_run_ids = []
    page_sizes = []

    while True:
        page = adapter.basic_run_page(cursor, bootstrap.run_id_watermark)
        page_run_ids = [record.run_id for record in page.runs]
        assert len(page_run_ids) <= TRUSTED_RUN_PAGE_SIZE
        assert page_run_ids == sorted(set(page_run_ids))
        observed_run_ids.extend(page_run_ids)
        page_sizes.append(len(page_run_ids))
        if page.complete:
            break
        assert page.next_run_id > cursor
        cursor = page.next_run_id

    assert bootstrap.run_id_watermark == run_count
    assert tuple(observed_run_ids) == expected_run_ids
    assert len(observed_run_ids) == len(set(observed_run_ids)) == run_count
    assert page_sizes == [TRUSTED_RUN_PAGE_SIZE] * 12 + [137]
    page_queries = [
        query
        for query in executor.queries
        if 'ORDER BY runs."run_id" LIMIT ?' in query.sql
    ]
    assert len(page_queries) == len(page_sizes)
    assert all(
        query.bindings[-1] == TRUSTED_RUN_PAGE_SIZE  # type: ignore[index]
        for query in page_queries
    )
    assert (
        TRUSTED_RUN_PAGE_SIZE
        <= TRUSTED_RUN_PAGE_SIZE_MAX
        <= TRUSTED_LIVE_MAX_ROWS_PER_RESULT
    )


class _LogicalResultPayloadProxy:
    """Represent an enormous result payload and fail on any materialisation."""

    def __init__(self, logical_size: int):
        self.logical_size = logical_size
        self.accesses = []

    def reject(self, operation: object) -> None:
        self.accesses.append(operation)
        raise AssertionError(f"Trusted startup touched result payload: {operation}")

    def __bytes__(self) -> bytes:
        self.reject("bytes")

    def __getitem__(self, key: object) -> object:
        self.reject(("getitem", key))

    def __iter__(self):
        self.reject("iter")

    def __len__(self) -> int:
        self.reject("len")


_LOGICAL_LARGE_SOURCE_BYTES = 32 * 1024**3


def _install_logical_artifact_sizes(monkeypatch, sizes: dict[Path, int]):
    """Report logical sizes for tiny fixtures without extending their files."""

    real_stat = os.stat
    artifacts = {
        os.path.abspath(os.fsdecode(os.fspath(path))): (
            real_stat(path, follow_symlinks=False),
            size,
        )
        for path, size in sizes.items()
    }

    def logical_stat(path: object, *args: Any, **kwargs: Any):
        try:
            normalized = os.path.abspath(os.fsdecode(os.fspath(path)))
        except TypeError:
            return real_stat(path, *args, **kwargs)
        artifact = artifacts.get(normalized)
        if artifact is None:
            return real_stat(path, *args, **kwargs)
        physical, logical_size = artifact
        stat_fields = {
            name: getattr(physical, name)
            for name in dir(physical)
            if name.startswith("st_")
        }
        stat_fields["st_size"] = logical_size
        return SimpleNamespace(**stat_fields)

    monkeypatch.setattr(os, "stat", logical_stat)
    return real_stat


def _guard_logical_artifact_io(monkeypatch, *paths: Path) -> None:
    """Reject attempts to allocate, open, or materialise a logical fixture."""

    targets = {os.path.abspath(os.fsdecode(os.fspath(path))) for path in paths}

    def targets_artifact(file: object) -> bool:
        try:
            candidate = os.fsdecode(os.fspath(file))
        except TypeError:
            return False
        return os.path.abspath(candidate) in targets

    real_builtin_open = builtins.open
    real_io_open = io.open
    real_os_open = os.open
    real_path_open = Path.open
    real_truncate = os.truncate

    def guarded_builtin_open(file: object, *args: Any, **kwargs: Any):
        if targets_artifact(file):
            raise AssertionError("Trusted metadata opened a logical source in Python")
        return real_builtin_open(file, *args, **kwargs)

    def guarded_io_open(file: object, *args: Any, **kwargs: Any):
        if targets_artifact(file):
            raise AssertionError("Trusted metadata opened a logical source in Python")
        return real_io_open(file, *args, **kwargs)

    def guarded_os_open(file: object, *args: Any, **kwargs: Any):
        if targets_artifact(file):
            raise AssertionError("Trusted metadata opened a logical source in Python")
        return real_os_open(file, *args, **kwargs)

    def guarded_path_open(path: Path, *args: Any, **kwargs: Any):
        if targets_artifact(path):
            raise AssertionError("Trusted metadata opened a logical source via Path")
        return real_path_open(path, *args, **kwargs)

    def guarded_truncate(file: object, length: int) -> None:
        if targets_artifact(file):
            raise AssertionError("A logical source must never be physically extended")
        real_truncate(file, length)

    def reject_fd_allocation(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError(
            "A logical source test must never allocate through a file fd"
        )

    monkeypatch.setattr(builtins, "open", guarded_builtin_open)
    monkeypatch.setattr(io, "open", guarded_io_open)
    monkeypatch.setattr(os, "open", guarded_os_open)
    monkeypatch.setattr(Path, "open", guarded_path_open)
    monkeypatch.setattr(os, "truncate", guarded_truncate)
    monkeypatch.setattr(os, "ftruncate", reject_fd_allocation)
    if hasattr(os, "posix_fallocate"):
        monkeypatch.setattr(os, "posix_fallocate", reject_fd_allocation)


def test_logical_32_gib_proxy_startup_never_opens_or_scans_result_payload(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "logical-32-gib.db"
    source.write_bytes(b"small physical fixture")
    real_stat = os.stat
    before = real_stat(source, follow_symlinks=False)

    class PayloadTripwireExecutor(_FakeExecutor):
        def __init__(self):
            super().__init__((1,))
            self.payload = _LogicalResultPayloadProxy(_LOGICAL_LARGE_SOURCE_BYTES)

        def _execute(self, query: TrustedQuery) -> TrustedQueryResult:
            table_name = self.runs[1]["result_table_name"]
            if quote_sqlite_identifier(table_name) in query.sql:
                self.payload.reject(query.sql)
            return super()._execute(query)

    executor = PayloadTripwireExecutor()
    _guard_logical_artifact_io(monkeypatch, source)

    adapter = TrustedMetadataQueryAdapter(executor, source)
    bootstrap = adapter.bootstrap()
    records = _load_basic_pages(adapter, 0, bootstrap.run_id_watermark)

    after = real_stat(source, follow_symlinks=False)
    assert [record.run_id for record in records] == [1]
    assert executor.payload.logical_size == _LOGICAL_LARGE_SOURCE_BYTES
    assert executor.payload.accesses == []
    assert all(
        quote_sqlite_identifier(executor.runs[1]["result_table_name"]) not in query.sql
        for query in executor.queries
    )
    assert (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) == (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    assert after.st_size == len(b"small physical fixture")
    assert list(tmp_path.iterdir()) == [source]


def test_refresh_skips_unchanged_version_and_never_duplicates_new_runs(tmp_path):
    adapter, executor = _adapter(tmp_path, (1, 2), data_version=7)
    bootstrap = adapter.bootstrap()
    _load_basic_pages(adapter, 0, bootstrap.run_id_watermark)

    # The mandatory first reconciliation catches a commit concurrent with
    # bootstrap paging, even when SQLite reports the same numeric version.
    reconciliation = adapter.refresh_new_runs()
    assert reconciliation.data_version_changed
    assert reconciliation.prior_run_id_watermark == 2
    assert reconciliation.run_id_watermark == 2
    executor.clear_history()

    unchanged = adapter.refresh_new_runs()

    assert not unchanged.data_version_changed
    assert unchanged.prior_run_id_watermark == 2
    assert unchanged.run_id_watermark == 2
    assert not hasattr(unchanged, "runs")
    assert executor.queries == []

    executor.add_run(3)
    executor.current_data_version = 8
    changed = adapter.refresh_new_runs()
    assert changed.data_version_changed
    assert changed.prior_run_id_watermark == 2
    assert changed.run_id_watermark == 3
    assert not hasattr(changed, "runs")
    changed_records = _load_basic_pages(
        adapter,
        changed.prior_run_id_watermark,
        changed.run_id_watermark,
    )
    assert [record.run_id for record in changed_records] == [3]

    executor.clear_history()
    unchanged_again = adapter.refresh_new_runs()
    assert unchanged_again.prior_run_id_watermark == 3
    assert unchanged_again.run_id_watermark == 3
    assert executor.queries == []

    executor.add_run(4)
    executor.current_data_version = 9
    changed_again = adapter.refresh_new_runs()
    changed_again_records = _load_basic_pages(
        adapter,
        changed_again.prior_run_id_watermark,
        changed_again.run_id_watermark,
    )
    assert [record.run_id for record in changed_again_records] == [4]
    assert {record.run_id for record in (*changed_records, *changed_again_records)} == {
        3,
        4,
    }
    assert adapter.last_run_id == 4


def test_refresh_reconciles_a_page_not_yet_accepted_by_the_gui(tmp_path):
    adapter, executor = _adapter(tmp_path, (1, 2), data_version=7)
    bootstrap = adapter.bootstrap()
    _load_basic_pages(adapter, 0, bootstrap.run_id_watermark)
    adapter.refresh_new_runs(accepted_run_id=2)

    executor.add_run(3)
    executor.current_data_version = 8
    delivered = adapter.refresh_new_runs(accepted_run_id=2)
    delivered_records = _load_basic_pages(
        adapter,
        delivered.prior_run_id_watermark,
        delivered.run_id_watermark,
    )
    assert [record.run_id for record in delivered_records] == [3]

    # Model cancellation between the service page result and GUI publication.
    # Even with unchanged data_version, the application-owned cursor forces the
    # exact page to be offered again instead of trusting the adapter's cursor.
    executor.clear_history()
    reconciled = adapter.refresh_new_runs(accepted_run_id=2)
    reconciled_records = _load_basic_pages(
        adapter,
        reconciled.prior_run_id_watermark,
        reconciled.run_id_watermark,
    )

    assert reconciled.data_version_changed
    assert (reconciled.prior_run_id_watermark, reconciled.run_id_watermark) == (2, 3)
    assert [record.run_id for record in reconciled_records] == [3]


def test_reconciled_basic_page_preserves_enriched_fields(tmp_path):
    adapter, executor = _adapter(tmp_path, (1, 2), data_version=7)
    bootstrap = adapter.bootstrap()
    _load_basic_pages(adapter, 0, bootstrap.run_id_watermark)
    adapter.refresh_new_runs(accepted_run_id=2)

    executor.add_run(3)
    executor.current_data_version = 8
    delivered = adapter.refresh_new_runs(accepted_run_id=2)
    _load_basic_pages(
        adapter,
        delivered.prior_run_id_watermark,
        delivered.run_id_watermark,
    )
    enriched = adapter.expensive_run(3).as_dict()
    assert enriched["result_count"] == 6
    assert enriched["storage_bytes"] > 0

    # Model cancellation after the service produced run 3 but before the GUI
    # advanced its accepted cursor.  The repeated basic page may refresh
    # authoritative run-table fields, but must not erase prior observations.
    executor.runs[3]["name"] = "fresh replayed name"
    reconciled = adapter.refresh_new_runs(accepted_run_id=2)
    replayed = _load_basic_pages(
        adapter,
        reconciled.prior_run_id_watermark,
        reconciled.run_id_watermark,
    )[0].as_dict()

    assert replayed["name"] == "fresh replayed name"
    assert replayed["result_count"] == enriched["result_count"]
    assert replayed["setpoint_count"] == enriched["setpoint_count"]
    assert replayed["read_setpoint_count"] == enriched["read_setpoint_count"]
    assert replayed["storage_bytes"] == enriched["storage_bytes"]


def test_cheap_and_expensive_runs_use_aggregates_and_return_frozen_primitives(
    tmp_path,
):
    adapter, executor = _adapter(tmp_path, (1,))
    bootstrap = adapter.bootstrap()
    _load_basic_pages(adapter, 0, bootstrap.run_id_watermark)
    executor.clear_history()

    cheap = adapter.cheap_run(1)

    table = quote_sqlite_identifier(executor.runs[1]["result_table_name"])
    result_queries = [query.sql for query in executor.queries if table in query.sql]
    assert result_queries == []
    cheap_fields = cheap.as_dict()
    assert "result_count" not in cheap_fields
    assert cheap_fields["expected_results"] is None
    assert cheap_fields["expected_results_source"] is None
    assert cheap_fields["measure_parameters"] == ["signal"]
    assert cheap_fields["sweep_parameters"] == ["x", "y"]
    assert cheap.unavailable_fields == ()
    _assert_frozen_primitive(cheap.fields)

    executor.clear_history()
    expensive = adapter.expensive_run(1)

    expensive_fields = expensive.as_dict()
    assert expensive_fields["result_count"] == 6
    assert expensive_fields["setpoint_shape"] == [2, 3]
    assert expensive_fields["point_shape"] == [2, 3]
    assert expensive_fields["setpoint_count"] == 6
    assert expensive_fields["read_setpoint_count"] == 6
    assert expensive_fields["storage_bytes"] > 0
    assert expensive_fields["storage_bytes_estimated"] is True
    _assert_frozen_primitive(expensive.fields)

    result_queries = [query.sql for query in executor.queries if table in query.sql]
    assert result_queries[0] == f"SELECT * FROM {table} WHERE 0"
    assert all("SELECT *" not in sql for sql in result_queries[1:])
    assert any('COALESCE(MAX("id"), 0)' in sql for sql in result_queries)
    assert any("SELECT DISTINCT" in sql for sql in result_queries)
    assert all("dbstat" not in sql for sql in result_queries)
    assert all(len(batch) <= 4 for batch in executor.batches)
    assert all("PRAGMA" not in sql.upper() for sql in result_queries)


def test_nested_cheap_refresh_cannot_be_regressed_by_resumed_expensive_run(
    tmp_path,
):
    adapter, executor = _adapter(tmp_path, (1,))
    executor.runs[1].update(
        name="stale-running-name",
        completed_timestamp=None,
        is_completed=0,
    )
    bootstrap = adapter.bootstrap()
    _load_basic_pages(adapter, 0, bootstrap.run_id_watermark)

    real_query_batch = executor.query_batch
    nested_records = []
    nested_once = False

    def query_batch_with_nested_cheap(queries, **kwargs):
        nonlocal nested_once
        if not nested_once:
            nested_once = True
            executor.runs[1].update(
                name="fresh-completed-name",
                completed_timestamp=9001.0,
                is_completed=1,
                run_timestamp=8001.0,
            )
            nested_records.append(adapter.cheap_run(1))
        return real_query_batch(queries, **kwargs)

    executor.query_batch = query_batch_with_nested_cheap  # type: ignore[method-assign]

    expensive = adapter.expensive_run(1)

    assert nested_once
    assert nested_records[0].as_dict()["name"] == "fresh-completed-name"
    expensive_fields = expensive.as_dict()
    assert expensive_fields["name"] == "fresh-completed-name"
    assert expensive_fields["is_completed"] == 1
    assert expensive_fields["completed_timestamp"] == 9001.0
    assert expensive_fields["run_timestamp"] == 8001.0
    assert expensive_fields["result_count"] == 6


def test_nested_expensive_refresh_cannot_be_regressed_by_resumed_cheap_run(
    tmp_path,
):
    adapter, executor = _adapter(tmp_path, (1,))
    bootstrap = adapter.bootstrap()
    _load_basic_pages(adapter, 0, bootstrap.run_id_watermark)

    real_query_batch = executor.query_batch
    nested_records = []
    nested_once = False

    def query_batch_with_nested_expensive(queries, **kwargs):
        nonlocal nested_once
        if not nested_once:
            nested_once = True
            nested_records.append(adapter.expensive_run(1))
        return real_query_batch(queries, **kwargs)

    executor.query_batch = query_batch_with_nested_expensive  # type: ignore[method-assign]

    cheap = adapter.cheap_run(1)

    assert nested_once
    assert nested_records[0].as_dict()["result_count"] == 6
    cheap_fields = cheap.as_dict()
    assert cheap_fields["result_count"] == 6
    assert cheap_fields["setpoint_count"] == 6
    assert cheap_fields["read_setpoint_count"] == 6
    assert cheap_fields["storage_bytes"] > 0


def test_nested_cheap_refresh_cannot_be_regressed_by_resumed_selected_detail(
    tmp_path,
):
    adapter, executor = _adapter(tmp_path, (1,))
    executor.runs[1].update(
        name="stale-selected-name",
        completed_timestamp=None,
        is_completed=0,
    )
    bootstrap = adapter.bootstrap()
    _load_basic_pages(adapter, 0, bootstrap.run_id_watermark)

    real_query = executor.query
    nested_records = []
    nested_once = False

    def query_with_nested_cheap(sql, bindings=None, **kwargs):
        nonlocal nested_once
        if 'MAX("layout_id")' in sql and not nested_once:
            nested_once = True
            executor.runs[1].update(
                name="fresh-selected-name",
                completed_timestamp=9101.0,
                is_completed=1,
                run_timestamp=8101.0,
            )
            nested_records.append(adapter.cheap_run(1))
        return real_query(sql, bindings, **kwargs)

    executor.query = query_with_nested_cheap  # type: ignore[method-assign]

    selected = adapter.selected_run_detail(1)

    assert nested_once
    assert nested_records[0].as_dict()["name"] == "fresh-selected-name"
    selected_fields = selected.run.as_dict()
    assert selected_fields["name"] == "fresh-selected-name"
    assert selected_fields["is_completed"] == 1
    assert selected_fields["completed_timestamp"] == 9101.0
    assert selected_fields["run_timestamp"] == 8101.0
    assert selected.snapshot.status == "available"
    assert [(node.key, node.value) for node in selected.snapshot.nodes] == [
        ("station", ""),
        ("run_id", "1"),
    ]


def test_selected_detail_preserves_prior_expensive_observations(tmp_path):
    adapter, executor = _adapter(tmp_path, (1,))
    bootstrap = adapter.bootstrap()
    _load_basic_pages(adapter, 0, bootstrap.run_id_watermark)

    expensive = adapter.expensive_run(1).as_dict()
    selected = adapter.selected_run_detail(1).run.as_dict()

    assert selected["result_count"] == expensive["result_count"] == 6
    assert selected["setpoint_count"] == expensive["setpoint_count"] == 6
    assert selected["read_setpoint_count"] == expensive["read_setpoint_count"] == 6
    assert selected["storage_bytes"] == expensive["storage_bytes"]


@pytest.mark.parametrize("aggregate_prefix", (True, False))
def test_expensive_setpoint_edges_are_bounded_before_adapter_cache(
    tmp_path,
    aggregate_prefix,
):
    adapter, executor = _adapter(tmp_path, (1,))
    bootstrap = adapter.bootstrap()
    _load_basic_pages(adapter, 0, bootstrap.run_id_watermark)
    adapter._bounded_aggregate_prefix = lambda _watermark: aggregate_prefix
    real_execute = executor._execute
    raw_tail = "RAW_SETPOINT_TAIL"
    raw_first = "f" * (64 * 1024 - len(raw_tail)) + raw_tail
    raw_last = "l" * (64 * 1024 - len(raw_tail)) + raw_tail

    def execute_with_large_edges(query):
        if query.sql.startswith("WITH distinct_values(value, first_rowid) AS ("):
            return TrustedQueryResult(
                ("first", "last", "steps"),
                ((raw_first, raw_last, 2),),
            )
        if query.sql.startswith("SELECT (SELECT ") and 'ORDER BY "id" ' in query.sql:
            parameter_count = query.sql.count('ORDER BY "id"')
            value = raw_first if 'ORDER BY "id" ASC' in query.sql else raw_last
            return TrustedQueryResult(
                tuple(f"value_{index}" for index in range(parameter_count)),
                ((value,) * parameter_count,),
            )
        return real_execute(query)

    executor._execute = execute_with_large_edges

    adapter.expensive_run(1)
    cached = adapter._setpoint_summaries[1]
    detail = adapter.selected_run_detail(1)

    assert cached == detail.setpoint_summaries
    assert adapter._setpoint_summaries_truncated[1] is True
    assert "setpoint_summaries.presentation" in detail.unavailable_fields
    assert raw_tail not in repr(cached)
    assert all(
        len(str(value).encode("utf-8")) <= TRUSTED_PRESENTATION_MAX_FIELD_VALUE_BYTES
        for summary in cached
        for value in (summary.first, summary.last)
    )


def test_expensive_run_rejects_result_table_without_qcodes_integer_primary_key(
    tmp_path,
):
    adapter, executor = _adapter(tmp_path, (1,))
    bootstrap = adapter.bootstrap()
    _load_basic_pages(adapter, 0, bootstrap.run_id_watermark)
    executor.result_id_primary_key = False
    executor.clear_history()

    with pytest.raises(TrustedMetadataQueryError, match="primary-key id"):
        adapter.expensive_run(1)

    assert not any('MAX("id")' in query.sql for query in executor.queries)


def test_logical_32_gib_source_uses_only_pk_watermark_and_bounded_edge_windows(
    tmp_path,
    monkeypatch,
):
    adapter, executor = _adapter(tmp_path, (1,))
    executor.runs[1]["run_description"] = json.dumps(
        {
            "interdependencies_": {
                "parameters": {
                    "x": {"type": "numeric"},
                    "y": {"type": "numeric"},
                    "signal": {"type": "numeric"},
                },
                "dependencies": {"signal": ["x", "y"]},
            },
            "shapes": {"signal": [2, 3]},
        },
        separators=(",", ":"),
    )
    database_path = tmp_path / "accepted.db"
    database_path.write_bytes(b"small physical fixture")
    real_stat = _install_logical_artifact_sizes(
        monkeypatch,
        {database_path: _LOGICAL_LARGE_SOURCE_BYTES},
    )
    _guard_logical_artifact_io(monkeypatch, database_path)

    bootstrap = adapter.bootstrap()
    _load_basic_pages(adapter, 0, bootstrap.run_id_watermark)
    executor.clear_history()

    expensive = adapter.expensive_run(1).as_dict()

    table = quote_sqlite_identifier(executor.runs[1]["result_table_name"])
    result_queries = [query for query in executor.queries if table in query.sql]
    assert expensive["result_count"] == 6
    assert expensive["setpoint_shape"] == [2, 3]
    assert expensive["storage_bytes_estimated"] is True
    assert all("dbstat" not in query.sql for query in result_queries)
    assert all("DISTINCT" not in query.sql for query in result_queries)
    assert all("GROUP BY" not in query.sql for query in result_queries)
    assert all("COUNT(" not in query.sql for query in result_queries)
    edge_queries = [query for query in result_queries if 'ORDER BY "id"' in query.sql]
    assert edge_queries
    assert all('WHERE "id" > ? AND "id" <= ?' in query.sql for query in edge_queries)
    assert all("LIMIT 1)" in query.sql for query in edge_queries)
    assert len(edge_queries) == 2
    assert all(len(batch) <= 4 for batch in executor.batches)
    assert any(
        len(batch) == 2 and "sqlite_schema" in batch[-1].sql
        for batch in executor.batches
    )
    assert real_stat(database_path, follow_symlinks=False).st_size == len(
        b"small physical fixture"
    )


def test_logical_large_wal_size_disables_whole_prefix_aggregates(
    tmp_path,
    monkeypatch,
):
    adapter, executor = _adapter(tmp_path, (1,))
    wal_path = tmp_path / "accepted.db-wal"
    wal_path.write_bytes(b"tiny WAL fixture")
    real_stat = _install_logical_artifact_sizes(
        monkeypatch,
        {wal_path: _LOGICAL_LARGE_SOURCE_BYTES},
    )
    _guard_logical_artifact_io(monkeypatch, wal_path)

    bootstrap = adapter.bootstrap()
    _load_basic_pages(adapter, 0, bootstrap.run_id_watermark)
    executor.clear_history()

    adapter.expensive_run(1)

    table = quote_sqlite_identifier(executor.runs[1]["result_table_name"])
    result_sql = "\n".join(
        query.sql for query in executor.queries if table in query.sql
    )
    assert "DISTINCT" not in result_sql
    assert "GROUP BY" not in result_sql
    assert "dbstat" not in result_sql
    assert real_stat(wal_path, follow_symlinks=False).st_size == len(
        b"tiny WAL fixture"
    )


def test_torn_main_wal_size_observation_cannot_enable_aggregate_scan(
    tmp_path,
    monkeypatch,
):
    adapter, _executor = _adapter(tmp_path, (1,))
    database_path = os.fspath(tmp_path / "accepted.db")
    wal_path = f"{database_path}-wal"
    main_sizes = iter((1, _LOGICAL_LARGE_SOURCE_BYTES))

    def observed_stat(path, *, follow_symlinks):
        assert follow_symlinks is False
        normalized = os.fspath(path)
        if normalized == database_path:
            size = next(main_sizes)
        elif normalized == wal_path:
            size = 1
        else:
            raise FileNotFoundError(normalized)
        return SimpleNamespace(
            st_mode=0o100400,
            st_dev=1,
            st_ino=2 if normalized == database_path else 3,
            st_size=size,
            st_mtime_ns=size,
            st_ctime_ns=size,
        )

    monkeypatch.setattr(os, "stat", observed_stat)

    assert not adapter._bounded_aggregate_prefix(6)


def test_selected_detail_contains_plain_parameters_metadata_snapshot_and_summaries(
    tmp_path,
):
    adapter, executor = _adapter(tmp_path, (1,))
    bootstrap = adapter.bootstrap()
    _load_basic_pages(adapter, 0, bootstrap.run_id_watermark)
    adapter.expensive_run(1)
    executor.clear_history()

    detail = adapter.selected_run_detail(1)

    assert detail.run.run_id == 1
    assert detail.run.as_dict()["run_id"] == 1
    assert dict(detail.presentation.run_fields)["run_id"] == 1
    _assert_frozen_primitive(detail.run.fields)
    assert detail.parameters == (
        TrustedParameterView("x", "X gate", "V", (), "numeric"),
        TrustedParameterView("y", "Y gate", "V", (), "numeric"),
        TrustedParameterView("signal", "Signal", "A", ("x", "y"), "numeric"),
    )
    assert dict(detail.metadata) == {"operator": "operator-1"}
    assert detail.snapshot.status == "available"
    assert [(node.key, node.value) for node in detail.snapshot.nodes] == [
        ("station", ""),
        ("run_id", "1"),
    ]
    assert not hasattr(detail, "snapshot_json")
    assert detail.setpoint_summaries == (
        TrustedSetpointSummary("x", 0.0, 1.0, 2),
        TrustedSetpointSummary("y", 10.0, 30.0, 3),
    )
    assert not hasattr(detail, "dataset")

    assert all(len(batch) <= 4 for batch in executor.batches)
    assert any('octet_length("snapshot")' in query.sql for query in executor.queries)
    assert any('AS "operator"' in query.sql for query in executor.queries)
    layout_watermark_query = next(
        query for query in executor.queries if 'MAX("layout_id")' in query.sql
    )
    assert layout_watermark_query.bindings == (1,)
    layout_queries = [
        query
        for query in executor.queries
        if "FROM layouts WHERE run_id = ? AND layout_id > ?" in query.sql
    ]
    assert [query.bindings for query in layout_queries] == [
        (1, 0, 3, 2),
        (1, 2, 3, 2),
    ]
    assert all("PRAGMA" not in query.sql.upper() for query in executor.queries)


def test_selected_detail_discards_near_limit_raw_presentation_values(tmp_path):
    adapter, executor = _adapter(tmp_path, (1,))
    huge_label = "run-description-label-" * 80_000
    huge_metadata = "dynamic-metadata-value-" * 80_000
    assert len(huge_label.encode("utf-8")) > 1024 * 1024
    assert len(huge_metadata.encode("utf-8")) > 1024 * 1024
    executor.runs[1]["run_description"] = json.dumps(
        {
            "interdependencies_": {
                "parameters": {
                    "x": {
                        "label": huge_label,
                        "unit": "V",
                        "type": "numeric",
                    }
                },
                "dependencies": {},
            }
        },
        separators=(",", ":"),
    )
    executor.runs[1]["operator"] = huge_metadata
    bootstrap = adapter.bootstrap()
    _load_basic_pages(adapter, 0, bootstrap.run_id_watermark)

    detail = adapter.selected_run_detail(1)

    run_fields = detail.run.as_dict()
    metadata = dict(detail.metadata)
    assert "run_description" not in run_fields
    assert len(detail.parameters[0].label.encode("utf-8")) <= (
        TRUSTED_PRESENTATION_MAX_PARAMETER_TEXT_BYTES
    )
    assert (
        sum(
            len(value.encode("utf-8"))
            for parameter in detail.parameters
            for value in (
                parameter.name,
                parameter.label,
                parameter.unit,
                parameter.paramtype,
                *parameter.depends_on,
            )
        )
        <= TRUSTED_PRESENTATION_MAX_PARAMETER_TOTAL_TEXT_BYTES
    )
    assert len(metadata["operator"].encode("utf-8")) <= (
        TRUSTED_PRESENTATION_MAX_FIELD_VALUE_BYTES
    )
    assert huge_label not in repr(detail)
    assert huge_metadata not in repr(detail)
    assert detail.presentation.metadata.status == "truncated"
    assert detail.presentation.raw.status == "truncated"
    for view in (detail.presentation.metadata, detail.presentation.raw):
        assert len(view.nodes) <= TRUSTED_PRESENTATION_MAX_RENDERED_NODES
        assert all(
            len(node.tooltip.encode("utf-8")) <= TRUSTED_PRESENTATION_MAX_TOOLTIP_BYTES
            for node in view.nodes
        )

    raw_fields = {"parameters", "run_description", "snapshot", "parent_datasets"}
    assert raw_fields.isdisjoint(adapter._runs[1])
    assert huge_label not in repr(adapter._runs[1])
    for record in (adapter.cheap_run(1), adapter.expensive_run(1)):
        assert raw_fields.isdisjoint(record.as_dict())
        assert huge_label not in repr(record)
        assert huge_metadata not in repr(record)
        assert raw_fields.isdisjoint(adapter._runs[1])
        assert huge_label not in repr(adapter._runs[1])


def test_many_tiny_run_description_parameters_are_capped_before_views(tmp_path):
    adapter, executor = _adapter(tmp_path, (1,))
    names = tuple(f"parameter-{index}" for index in range(5_000))
    axes = tuple(f"axis-{index}" for index in range(5_000))
    nested_secret = "nested-parameter-private-value-" * 20_000
    specifications = {
        name: {"label": "label", "unit": "V", "type": "numeric"} for name in names
    }
    specifications[names[0]]["label"] = {"private": nested_secret}
    executor.runs[1]["parameters"] = ",".join(names)
    executor.runs[1]["run_description"] = json.dumps(
        {
            "interdependencies_": {
                "parameters": specifications,
                "dependencies": {names[0]: axes},
            }
        },
        separators=(",", ":"),
    )
    bootstrap = adapter.bootstrap()
    _load_basic_pages(adapter, 0, bootstrap.run_id_watermark)

    adapter.cheap_run(1)
    adapter.expensive_run(1)
    detail = adapter.selected_run_detail(1)

    assert len(detail.parameters) <= TRUSTED_PRESENTATION_MAX_PARAMETERS
    assert max(len(parameter.depends_on) for parameter in detail.parameters) <= (
        TRUSTED_PRESENTATION_MAX_PARAMETER_DEPENDENCIES
    )
    assert detail.presentation.parameters_truncated
    assert "parameters.presentation" in detail.unavailable_fields
    assert detail.run.as_dict()["parameters_truncated"] is True
    assert names[-1] not in repr(detail)
    assert axes[-1] not in repr(detail)
    assert nested_secret not in repr(detail)
    assert {"parameters", "run_description"}.isdisjoint(adapter._runs[1])


def test_parameter_view_source_iterators_stop_after_limit_probe(monkeypatch):
    class GuardedParameters(dict):
        yielded = 0

        def items(self):
            for index in range(TRUSTED_PRESENTATION_MAX_PARAMETERS + 1):
                self.yielded += 1
                name = "signal" if index == 0 else f"parameter-{index}"
                yield name, {"label": "label", "type": "numeric"}
            raise AssertionError("parameter mapping traversed past MAX + 1")

    class GuardedDependencies(list):
        yielded = 0

        def __iter__(self):
            for index in range(TRUSTED_PRESENTATION_MAX_PARAMETER_DEPENDENCIES + 1):
                self.yielded += 1
                yield f"axis-{index}"
            raise AssertionError("parameter dependencies traversed past MAX + 1")

    parameters = GuardedParameters({"present": {}})
    dependencies = GuardedDependencies(["present"])
    monkeypatch.setattr(
        trusted_queries_module,
        "_json_object",
        lambda _value: {
            "interdependencies_": {
                "parameters": parameters,
                "dependencies": {"signal": dependencies},
            }
        },
    )

    views, truncated = TrustedMetadataQueryAdapter._parameter_views_bounded(
        {"run_description": "guarded"},
        TrustedQueryResult(
            ("layout_id", "parameter", "label", "unit", "inferred_from"),
            (),
        ),
    )

    assert truncated
    assert len(views) == TRUSTED_PRESENTATION_MAX_PARAMETERS
    assert parameters.yielded == TRUSTED_PRESENTATION_MAX_PARAMETERS + 1
    assert dependencies.yielded == (TRUSTED_PRESENTATION_MAX_PARAMETER_DEPENDENCIES + 1)
    assert len(views[0].depends_on) == (TRUSTED_PRESENTATION_MAX_PARAMETER_DEPENDENCIES)


@pytest.mark.parametrize(
    ("stored_snapshot", "expected_status", "message_fragment"),
    (
        (None, "empty", "No snapshot was stored"),
        ('{"station":{"valid":true}}', "available", "decoded within all limits"),
        ('{"station":', "malformed", "Malformed snapshot JSON"),
        (
            '"' + "x" * TRUSTED_LIVE_MAX_SCALAR_BYTES + '"',
            "unavailable",
            "was stored",
        ),
    ),
    ids=("empty", "available", "malformed", "oversized"),
)
def test_selected_detail_preserves_snapshot_storage_state_through_preflight(
    tmp_path,
    stored_snapshot,
    expected_status,
    message_fragment,
):
    adapter, executor = _adapter(tmp_path, (1,))
    executor.runs[1]["snapshot"] = stored_snapshot
    bootstrap = adapter.bootstrap()
    _load_basic_pages(adapter, 0, bootstrap.run_id_watermark)
    executor.clear_history()

    detail = adapter.selected_run_detail(1)

    assert detail.snapshot.status == expected_status
    assert message_fragment in detail.snapshot.message
    if expected_status == "unavailable":
        assert "snapshot" in detail.unavailable_fields
        assert "exceeds" in detail.snapshot.message
        assert "No snapshot was stored" not in detail.snapshot.message
        assert detail.snapshot.input_bytes == len(stored_snapshot.encode("utf-8"))
        guarded_sql = "\n".join(
            query.sql
            for query in executor.queries
            if query.sql.startswith("SELECT CASE WHEN")
        )
        assert 'THEN "snapshot"' not in guarded_sql


def test_oversized_single_run_scalars_are_explicitly_unavailable_without_fetch(
    tmp_path,
):
    adapter, executor = _adapter(tmp_path, (1,))
    executor.runs[1]["run_description"] = "d" * (TRUSTED_LIVE_MAX_SCALAR_BYTES + 1)
    executor.runs[1]["operator"] = "o" * (TRUSTED_LIVE_MAX_SCALAR_BYTES + 1)
    bootstrap = adapter.bootstrap()
    _load_basic_pages(adapter, 0, bootstrap.run_id_watermark)
    executor.clear_history()

    cheap = adapter.cheap_run(1)
    selected = adapter.selected_run_detail(1)

    assert "run_description" not in cheap.as_dict()
    assert cheap.unavailable_fields == ("run_description",)
    assert selected.snapshot.status == "available"
    assert selected.unavailable_fields == ("run_description", "operator")
    assert selected.run.unavailable_fields == selected.unavailable_fields
    guarded_sql = "\n".join(
        query.sql
        for query in executor.queries
        if query.sql.startswith("SELECT CASE WHEN")
    )
    assert 'THEN "run_description"' not in guarded_sql
    assert 'THEN "operator"' not in guarded_sql


def test_selected_detail_has_one_cumulative_public_scalar_budget(tmp_path):
    adapter, executor = _adapter(tmp_path, (1,))
    dynamic_names = tuple(f"large_metadata_{index}" for index in range(5))
    executor.run_columns.extend(dynamic_names)
    for name in dynamic_names:
        executor.runs[1][name] = name[-1] * (1024 * 1024)
    bootstrap = adapter.bootstrap()
    _load_basic_pages(adapter, 0, bootstrap.run_id_watermark)
    executor.clear_history()

    detail = adapter.selected_run_detail(1)

    metadata = dict(detail.metadata)
    retained = tuple(name for name in dynamic_names if name in metadata)
    omitted = tuple(name for name in dynamic_names if name in detail.unavailable_fields)
    assert retained
    assert omitted
    assert len(retained) + len(omitted) == len(dynamic_names)
    assert (
        sum(len(metadata[name]) for name in retained) <= TRUSTED_LIVE_MAX_SCALAR_BYTES
    )
    guarded_sql = "\n".join(
        query.sql
        for query in executor.queries
        if query.sql.startswith("SELECT CASE WHEN")
    )
    assert all(f'THEN "{name}"' not in guarded_sql for name in omitted)
    large_fetches = [
        query.sql
        for query in executor.queries
        if query.sql.startswith("SELECT CASE WHEN")
        and any(f'THEN "{name}"' in query.sql for name in retained)
    ]
    assert all(sql.count("CASE WHEN") == 1 for sql in large_fetches)


def test_selected_layout_caps_text_and_surfaces_omission(tmp_path):
    adapter, executor = _adapter(tmp_path, (1,))
    executor.layouts[1] = ((1, "x", "L" * 513, "V", None),)
    bootstrap = adapter.bootstrap()
    _load_basic_pages(adapter, 0, bootstrap.run_id_watermark)

    detail = adapter.selected_run_detail(1)

    assert "layouts.1.label" in detail.unavailable_fields
    assert detail.run.unavailable_fields == detail.unavailable_fields


def test_selected_layout_omissions_are_bounded_before_adapter_cache(tmp_path):
    adapter, executor = _adapter(tmp_path, (1,))
    layout_count = TRUSTED_PRESENTATION_MAX_UNAVAILABLE_FIELDS + 32
    parameter_names = tuple(
        f"parameter_{index}" for index in range(1, layout_count + 1)
    )
    executor.runs[1]["parameters"] = ",".join(parameter_names)
    executor.layouts[1] = tuple(
        (index, name, "L" * 513, "V", None)
        for index, name in enumerate(parameter_names, start=1)
    )
    bootstrap = adapter.bootstrap()
    _load_basic_pages(adapter, 0, bootstrap.run_id_watermark)

    first = adapter.selected_run_detail(1)
    second = adapter.selected_run_detail(1)
    cached = adapter._unavailable_run_fields[1]

    assert cached == first.unavailable_fields == second.unavailable_fields
    assert len(cached) == TRUSTED_PRESENTATION_MAX_UNAVAILABLE_FIELDS
    assert cached[-1] == "[additional unavailable fields omitted]"
    assert f"layouts.{layout_count}.label" not in cached

    deferred_groups = iter(
        (
            tuple(f"cheap.{index}" for index in range(64)),
            tuple(f"expensive.{index}" for index in range(64)),
        )
    )

    def loaded_with_more_unavailable(_run_id, _selected):
        return trusted_queries_module._SingleRunValues(
            {"run_id": 1},
            next(deferred_groups),
            0,
        )

    adapter._single_run_columns = loaded_with_more_unavailable
    cheap = adapter.cheap_run(1)
    expensive = adapter.expensive_run(1)

    assert adapter._unavailable_run_fields[1] == cached
    assert cheap.unavailable_fields == cached
    assert expensive.unavailable_fields == cached


def test_oversized_result_schema_is_rejected_before_schema_text_crosses_wire(
    tmp_path,
):
    adapter, executor = _adapter(tmp_path, (1,))
    table_name = executor.runs[1]["result_table_name"]
    executor.result_columns[table_name] = (
        "id",
        *(f"column_{index}_" + "x" * 1000 for index in range(70)),
    )
    bootstrap = adapter.bootstrap()
    _load_basic_pages(adapter, 0, bootstrap.run_id_watermark)

    with pytest.raises(TrustedMetadataQueryError, match="schema is too large"):
        adapter.expensive_run(1)


def test_selected_layouts_drain_bounded_monotonic_pages_without_loss(tmp_path):
    page_size = 3
    layout_count = page_size * 2 + 2
    adapter, executor = _adapter(tmp_path, (1,), page_size=page_size)
    parameter_names = tuple(
        f"parameter_{index}" for index in range(1, layout_count + 1)
    )
    executor.runs[1]["parameters"] = ",".join(parameter_names)
    executor.runs[1]["run_description"] = json.dumps(
        {
            "interdependencies_": {
                "parameters": {
                    name: {"label": "", "unit": "", "type": "numeric"}
                    for name in parameter_names
                },
                "dependencies": {},
            }
        },
        separators=(",", ":"),
    )
    executor.layouts[1] = tuple(
        (
            layout_id,
            parameter_names[layout_id - 1],
            f"Layout {layout_id}",
            f"unit-{layout_id}",
            None,
        )
        for layout_id in range(1, layout_count + 1)
    )

    bootstrap = adapter.bootstrap()
    _load_basic_pages(adapter, 0, bootstrap.run_id_watermark)
    executor.clear_history()

    detail = adapter.selected_run_detail(1)

    assert [parameter.name for parameter in detail.parameters] == list(parameter_names)
    assert [parameter.label for parameter in detail.parameters] == [
        f"Layout {layout_id}" for layout_id in range(1, layout_count + 1)
    ]
    layout_queries = [
        query
        for query in executor.queries
        if "FROM layouts WHERE run_id = ? AND layout_id > ?" in query.sql
    ]
    assert [query.bindings for query in layout_queries] == [
        (1, 0, layout_count, page_size),
        (1, 3, layout_count, page_size),
        (1, 6, layout_count, page_size),
    ]
    assert all(
        query.sql.endswith("ORDER BY layout_id LIMIT ?") for query in layout_queries
    )
    assert executor.layout_page_ids == [(1, 2, 3), (4, 5, 6), (7, 8)]
    drained_ids = [layout_id for page in executor.layout_page_ids for layout_id in page]
    assert drained_ids == list(range(1, layout_count + 1))
    assert len(drained_ids) == len(set(drained_ids)) == layout_count
