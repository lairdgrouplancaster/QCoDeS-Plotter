from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from qplot.datahandling.file_identity import DatabaseInstance
from qplot.datahandling.trusted_live import TrustedQuery, TrustedQueryResult
from qplot.datahandling.trusted_live_queries import (
    TRUSTED_DERIVED_MAX_SAMPLE_ROWS,
    TrustedMetadataQueryAdapter,
    TrustedSourceRevisionNamespace,
    trusted_derived_source_revision,
)


def _description() -> str:
    return json.dumps(
        {
            "interdependencies_": {
                "parameters": {
                    "x": {"name": "x", "type": "numeric", "label": "X"},
                    "signal": {
                        "name": "signal",
                        "type": "numeric",
                        "label": "Signal",
                    },
                },
                "dependencies": {"signal": ["x"]},
                "inferences": {},
                "standalones": [],
            },
            "shapes": {"signal": [1000000000]},
        }
    )


class _Executor:
    incarnation = 7

    def __init__(self) -> None:
        self.batches: list[tuple[TrustedQuery, ...]] = []
        self.on_batch: Any = None

    def data_version(self, **_kwargs: Any) -> int:
        return 13

    def query(self, *_args: Any, **_kwargs: Any) -> TrustedQueryResult:
        raise AssertionError("Derived extraction must use one bounded query batch")

    def query_batch(
        self,
        queries: tuple[TrustedQuery, ...],
        **_kwargs: Any,
    ) -> tuple[TrustedQueryResult, ...]:
        self.batches.append(queries)
        if self.on_batch is not None:
            callback = self.on_batch
            self.on_batch = None
            callback()
        identity = TrustedQueryResult(
            ("run_id", "guid", "result_table_name", "parameters", "run_description"),
            ((1, "guid-1", "results", "x,signal", _description()),),
        )
        schema = TrustedQueryResult(("id", "x", "signal"), ())
        schema_sql = TrustedQueryResult(
            ("sql", "sql_bytes"),
            (
                (
                    'CREATE TABLE "results" ("id" INTEGER PRIMARY KEY, '
                    '"x" numeric, "signal" numeric)',
                    94,
                ),
            ),
        )
        watermark = TrustedQueryResult(("result_watermark",), ((1_000_000_000,),))
        windows = []
        for index, query in enumerate(queries[4:]):
            row_id = (
                1_000_000_000
                if "DESC" in query.sql.upper()
                else min(1_000_000_000, index * 62_500_000 + 1)
            )
            windows.append(
                TrustedQueryResult(
                    ("id", "x", "signal"),
                    ((row_id, float(row_id), float(row_id * 2)),),
                )
            )
        return (identity, schema, schema_sql, watermark, *windows)


class _DerivedAdapter(TrustedMetadataQueryAdapter):
    """Keep fixed-query unit probes focused on the repeatable-read batch.

    Real integration tests exercise the actual cheap/expensive metadata plans.
    """

    def cheap_run(self, run_id: int):  # type: ignore[no-untyped-def]
        return None

    def expensive_run(self, run_id: int):  # type: ignore[no-untyped-def]
        metadata = self._runs[run_id]  # type: ignore[attr-defined]
        metadata.setdefault("point_shape", (1_000_000_000,))
        metadata.setdefault("setpoint_shape", (1_000_000_000,))
        metadata.setdefault("setpoint_count", 1_000_000_000)
        metadata.setdefault("read_setpoint_count", 1_000_000_000)
        return None


def test_derived_extraction_is_indexed_bounded_and_prefix_consistent(
    tmp_path: Path,
) -> None:
    executor = _Executor()
    adapter = _DerivedAdapter(executor, tmp_path / "large.db")
    adapter._runs[1] = {  # type: ignore[attr-defined]
        "guid": "guid-1",
        "result_table_name": "results",
        "result_count": 1_000_000_000,
        "measure_parameters": ("signal",),
        "sweep_parameters": ("x",),
    }
    adapter._result_columns["results"] = (  # type: ignore[attr-defined]
        "id",
        "x",
        "signal",
    )
    instance = DatabaseInstance(
        str(tmp_path / "large.db"), str(tmp_path / "large.db"), (7, 11)
    )
    namespace = TrustedSourceRevisionNamespace(b"test-service")

    observation = adapter.derived_source_observation(
        1,
        database_instance=instance,
        namespace=namespace,
    )

    assert observation.result_watermark == 1_000_000_000
    assert len(observation.sample_rows) <= TRUSTED_DERIVED_MAX_SAMPLE_ROWS
    assert observation.sample_rows[-1][0] == observation.result_watermark
    assert all(
        0 < row[0] <= observation.result_watermark for row in observation.sample_rows
    )
    assert observation.dependent_parameters == ("signal",)
    assert observation.parameters[-1].depends_on == ("x",)
    assert trusted_derived_source_revision(
        observation
    ) == trusted_derived_source_revision(observation)
    batch = executor.batches[-1]
    sample_sql = tuple(query.sql.upper() for query in batch[4:])
    assert all(
        ('"ID" > MAX(0,' in sql and "* ?)" in sql) or 'ORDER BY "ID" DESC' in sql
        for sql in sample_sql
    )
    assert all("LIMIT 256" in sql for sql in sample_sql)
    assert all("OFFSET" not in sql for sql in sample_sql)
    assert all("DISTINCT" not in sql and "GROUP BY" not in sql for sql in sample_sql)
    assert all("COUNT(" not in sql for sql in sample_sql)


def test_revision_separates_database_prefix_and_helper_incarnation(
    tmp_path: Path,
) -> None:
    executor = _Executor()
    adapter = _DerivedAdapter(executor, tmp_path / "source.db")
    adapter._runs[1] = {  # type: ignore[attr-defined]
        "guid": "guid-1",
        "result_table_name": "results",
        "result_count": 1,
    }
    adapter._result_columns["results"] = ("id", "x", "signal")  # type: ignore[attr-defined]
    instance = DatabaseInstance("/data/source.db", "/data/source.db", (1, 2))
    observation = adapter.derived_source_observation(
        1,
        database_instance=instance,
        namespace=TrustedSourceRevisionNamespace(b"namespace-a"),
    )
    first = trusted_derived_source_revision(observation)

    executor.incarnation = 8
    changed = adapter.derived_source_observation(
        1,
        database_instance=instance,
        namespace=TrustedSourceRevisionNamespace(b"namespace-a"),
    )

    assert trusted_derived_source_revision(changed) != first


def test_sampling_windows_do_not_depend_on_cached_expensive_result_count(
    tmp_path: Path,
) -> None:
    executor = _Executor()
    adapter = _DerivedAdapter(executor, tmp_path / "large.db")
    adapter._runs[1] = {  # type: ignore[attr-defined]
        "guid": "guid-1",
        "result_table_name": "results",
        "measure_parameters": ("signal",),
        "sweep_parameters": ("x",),
    }
    adapter._result_columns["results"] = (  # type: ignore[attr-defined]
        "id",
        "x",
        "signal",
    )
    instance = DatabaseInstance(
        str(tmp_path / "large.db"), str(tmp_path / "large.db"), (7, 11)
    )

    observation = adapter.derived_source_observation(
        1,
        database_instance=instance,
        namespace=TrustedSourceRevisionNamespace(b"self-contained"),
    )

    sample_queries = executor.batches[-1][4:]
    assert len(sample_queries) == 16
    assert observation.sample_rows[0][0] == 1
    assert observation.sample_rows[-1][0] == observation.result_watermark
    assert all('MAX("id")' in query.sql for query in sample_queries)
    assert all("OFFSET" not in query.sql.upper() for query in sample_queries)
    assert observation.run_fields
    assert dict(observation.run_fields)["result_count"] == 1_000_000_000


def test_derived_extraction_never_regresses_newer_adapter_metadata(
    tmp_path: Path,
) -> None:
    executor = _Executor()
    adapter = _DerivedAdapter(executor, tmp_path / "ordering.db")
    adapter._runs[1] = {  # type: ignore[attr-defined]
        "guid": "guid-1",
        "result_table_name": "results",
        "name": "old-name",
        "is_completed": False,
        "completed_timestamp": None,
        "point_shape": (8,),
        "measure_parameters": ("signal",),
        "sweep_parameters": ("x",),
    }
    adapter._result_columns["results"] = ("id", "x", "signal")  # type: ignore[attr-defined]

    def publish_newer_metadata() -> None:
        adapter._runs[1] = {  # type: ignore[attr-defined]
            **adapter._runs[1],  # type: ignore[attr-defined]
            "name": "new-name",
            "is_completed": True,
            "completed_timestamp": 1234.5,
            "point_shape": (10,),
            "setpoint_shape": (10,),
            "setpoint_count": 10,
            "read_setpoint_count": 10,
        }

    executor.on_batch = publish_newer_metadata
    instance = DatabaseInstance(
        str(tmp_path / "ordering.db"), str(tmp_path / "ordering.db"), (7, 13)
    )
    observation = adapter.derived_source_observation(
        1,
        database_instance=instance,
        namespace=TrustedSourceRevisionNamespace(b"ordering"),
    )
    fields = dict(observation.run_fields)

    assert fields["name"] == "new-name"
    assert fields["is_completed"] is True
    assert fields["completed_timestamp"] == 1234.5
    assert fields["point_shape"] == (10,)
    assert fields["setpoint_count"] == 10


def test_sqlite_plans_use_primary_key_searches_for_every_sample_window(
    tmp_path: Path,
) -> None:
    executor = _Executor()
    adapter = _DerivedAdapter(executor, tmp_path / "plan.db")
    adapter._runs[1] = {  # type: ignore[attr-defined]
        "guid": "guid-1",
        "result_table_name": "results",
    }
    adapter._result_columns["results"] = ("id", "x", "signal")  # type: ignore[attr-defined]
    instance = DatabaseInstance("/data/plan.db", "/data/plan.db", (7, 12))
    adapter.derived_source_observation(
        1,
        database_instance=instance,
        namespace=TrustedSourceRevisionNamespace(b"query-plan"),
    )

    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            'CREATE TABLE "results" ('
            '"id" INTEGER PRIMARY KEY, "x" numeric, "signal" numeric)'
        )
        for query in executor.batches[-1][4:]:
            details = tuple(
                row[3]
                for row in connection.execute(
                    f"EXPLAIN QUERY PLAN {query.sql}",
                    query.bindings or (),
                )
            )
            assert details[0].startswith("SEARCH results USING INTEGER PRIMARY KEY")
            assert all(not detail.startswith("SCAN results") for detail in details)
    finally:
        connection.close()
