"""Shared fixtures and assertions for trusted live queries."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from qplot.datahandling._trusted_query_metadata import (
    quote_sqlite_identifier,
)
from qplot.datahandling.trusted_live import (
    TrustedQuery,
    TrustedQueryResult,
)
from qplot.datahandling.trusted_live_queries import (
    TrustedMetadataQueryAdapter,
)

__all__ = [
    '_FakeExecutor',
    '_adapter',
    '_load_basic_pages',
]


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
        self.result_shapes = {
            row["result_table_name"]: (2, 3) for row in self.runs.values()
        }
        self.result_fast_axis_indexes = {
            row["result_table_name"]: 1 for row in self.runs.values()
        }
        self.result_dependents = {
            row["result_table_name"]: ("signal",) for row in self.runs.values()
        }
        self.result_storage_patterns = {
            row["result_table_name"]: "dense" for row in self.runs.values()
        }
        self.result_serpentine = {
            row["result_table_name"]: False for row in self.runs.values()
        }
        self.result_row_overrides: dict[
            str,
            dict[int, tuple[float, float]],
        ] = {row["result_table_name"]: {} for row in self.runs.values()}
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
        self.result_shapes[table_name] = (2, 3)
        self.result_fast_axis_indexes[table_name] = 1
        self.result_dependents[table_name] = ("signal",)
        self.result_storage_patterns[table_name] = "dense"
        self.result_serpentine[table_name] = False
        self.result_row_overrides[table_name] = {}
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

        if (
            sql.startswith('SELECT "id", CASE WHEN typeof(')
            and 'ORDER BY "id" LIMIT ?' in sql
            and 'AS "qplot_axis_0"' not in sql
        ):
            table_name = self._result_table_for_sql(sql)
            lower, upper, limit = self._bindings(query)
            columns = self.result_columns[table_name]
            axes = tuple(
                name
                for name in columns
                if name not in ("id", *self.result_dependents[table_name])
            )
            rows = []
            for row_id in range(lower + 1, min(upper, lower + limit) + 1):
                regular = self._regular_result_row(table_name, row_id)
                values = dict(zip(axes, regular[1 : 1 + len(axes)], strict=True))
                values.update(
                    {
                        name: float(row_id) if present else None
                        for name, present in zip(
                            self.result_dependents[table_name],
                            regular[1 + len(axes) :],
                            strict=True,
                        )
                    }
                )
                rows.append((row_id, *(values[name] for name in columns[1:])))
            return TrustedQueryResult(columns, tuple(rows))

        if ' AS "qplot_axis_0"' in sql and ' AS "qplot_present_0"' in sql:
            table_name = self._result_table_for_sql(sql)
            bindings = self._bindings(query)
            if 'WHERE "id" > ? AND "id" <= ?' in sql:
                lower, upper, limit = bindings
                # Apply SQL LIMIT before materializing the virtual result set.
                row_ids = tuple(
                    range(
                        lower + 1,
                        min(upper, self.result_counts[table_name]) + 1,
                    )[:limit]
                )
            elif 'WHERE "id" IN (' in sql:
                *requested, through = bindings
                row_ids = tuple(
                    sorted(
                        {
                            row_id
                            for row_id in requested
                            if 0 < row_id <= through <= self.result_counts[table_name]
                        }
                    )
                )
            else:  # pragma: no cover - every new layout plan is explicit
                raise AssertionError(f"unexpected regular-layout query: {sql}")
            shape = self.result_shapes[table_name]
            dependents = self.result_dependents[table_name]
            columns = (
                "id",
                *(f"qplot_axis_{index}" for index in range(len(shape))),
                *(f"qplot_present_{index}" for index in range(len(dependents))),
            )
            return TrustedQueryResult(
                columns,
                tuple(
                    self._regular_result_row(table_name, row_id) for row_id in row_ids
                ),
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
                elif alias == "run_description":
                    value = run[alias]
                    if (
                        "json_extract" in sql
                        and 2048 < len(value.encode("utf-8")) <= 16 * 1024
                    ):
                        try:
                            description = json.loads(value)
                        except ValueError:
                            value = ""
                        else:
                            interdependencies = (
                                description.get("interdependencies_", {})
                                if isinstance(description, dict) else {}
                            )
                            value = json.dumps({"interdependencies_": {
                                "dependencies": interdependencies.get("dependencies")
                                if isinstance(interdependencies, dict) else None,
                            }}, separators=(",", ":"))
                    projected.append(value if len(value.encode("utf-8")) <= 2048 else None)
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

    def _regular_result_row(
        self,
        table_name: str,
        row_id: int,
    ) -> tuple[int | float, ...]:
        shape = self.result_shapes[table_name]
        dependents = self.result_dependents[table_name]
        dependent_count = len(dependents)
        pattern = self.result_storage_patterns[table_name]
        fast_axis_index = self.result_fast_axis_indexes[table_name]
        if len(shape) == 1:
            if pattern == "dense":
                logical_index = row_id - 1
                active = tuple(True for _dependent in dependents)
            elif pattern == "interleaved":
                logical_index = (row_id - 1) // dependent_count
                phase = (row_id - 1) % dependent_count
                active = tuple(index == phase for index in range(dependent_count))
            else:
                logical_index = (row_id - 1) % shape[0]
                phase = (row_id - 1) // shape[0]
                active = tuple(index == phase for index in range(dependent_count))
            axes = (float(logical_index),)
        else:
            fast_count = shape[fast_axis_index]
            if pattern == "dense":
                logical_index = row_id - 1
                slow_index, fast_index = divmod(logical_index, fast_count)
                active = tuple(True for _dependent in dependents)
            elif pattern == "interleaved":
                logical_index = (row_id - 1) // dependent_count
                slow_index, fast_index = divmod(logical_index, fast_count)
                phase = (row_id - 1) % dependent_count
                active = tuple(index == phase for index in range(dependent_count))
            else:
                slow_stride = dependent_count * fast_count
                slow_index, within_slow = divmod(row_id - 1, slow_stride)
                phase, fast_index = divmod(within_slow, fast_count)
                active = tuple(index == phase for index in range(dependent_count))
            coordinate_fast_index = (
                fast_count - 1 - fast_index
                if self.result_serpentine[table_name] and slow_index % 2
                else fast_index
            )
            axis_indexes = [0, 0]
            axis_indexes[fast_axis_index] = coordinate_fast_index
            axis_indexes[1 - fast_axis_index] = slow_index
            axes = (float(axis_indexes[0]), float(axis_indexes[1] * 10 + 10))
        axes = self.result_row_overrides[table_name].get(row_id, axes)
        return (row_id, *axes, *(int(present) for present in active))

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
