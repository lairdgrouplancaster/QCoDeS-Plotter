"""Shared fixtures and assertions for qcodes measurement layouts."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Mapping
from pathlib import Path

from qplot.datahandling.trusted_live import TrustedQuery, TrustedQueryResult
from qplot.datahandling.trusted_live_queries import (
    TrustedMetadataQueryAdapter,
)

__all__ = [
    '_ReadOnlySqliteExecutor',
    '_load_basic_runs',
    '_protected_artifact_state',
]


class _ReadOnlySqliteExecutor:
    """Small DB-API executor for an explicitly generated diagnostic source."""

    def __init__(self, path: Path) -> None:
        self.incarnation = 1
        self.queries: list[TrustedQuery] = []
        self._connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        self._connection.execute("PRAGMA query_only=ON")

    def close(self) -> None:
        self._connection.close()

    def query(
        self,
        sql: str,
        bindings=None,
        *,
        timeout: float | None = None,
        wait_timeout: float | None = None,
    ) -> TrustedQueryResult:
        del timeout, wait_timeout
        return self._execute(TrustedQuery(sql, bindings))

    def query_batch(
        self,
        queries: tuple[TrustedQuery, ...],
        *,
        timeout: float | None = None,
        wait_timeout: float | None = None,
    ) -> tuple[TrustedQueryResult, ...]:
        del timeout, wait_timeout
        self._connection.execute("BEGIN")
        try:
            return tuple(self._execute(query) for query in queries)
        finally:
            self._connection.rollback()

    def data_version(
        self,
        *,
        timeout: float | None = None,
        wait_timeout: float | None = None,
    ) -> int:
        del timeout, wait_timeout
        row = self._connection.execute("PRAGMA data_version").fetchone()
        assert row is not None and type(row[0]) is int
        return row[0]

    def _execute(self, query: TrustedQuery) -> TrustedQueryResult:
        self.queries.append(query)
        bindings = query.bindings
        if isinstance(bindings, Mapping):
            bindings = dict(bindings)
        cursor = self._connection.execute(query.sql, bindings or ())
        columns = tuple(item[0] for item in (cursor.description or ()))
        return TrustedQueryResult(
            columns, tuple(tuple(row) for row in cursor.fetchall())
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _protected_artifact_state(database_path: Path) -> tuple[tuple[object, ...], ...]:
    state = []
    for suffix in ("", "-wal", "-journal"):
        path = Path(f"{database_path}{suffix}")
        if not path.exists():
            state.append((suffix, None))
            continue
        status = os.stat(path, follow_symlinks=False)
        state.append(
            (
                suffix,
                status.st_dev,
                status.st_ino,
                status.st_size,
                status.st_mtime_ns,
                _sha256(path),
            )
        )
    return tuple(state)


def _load_basic_runs(
    adapter: TrustedMetadataQueryAdapter,
) -> tuple[object, ...]:
    bootstrap = adapter.bootstrap()
    cursor = 0
    runs: list[object] = []
    while True:
        page = adapter.basic_run_page(cursor, bootstrap.run_id_watermark)
        runs.extend(page.runs)
        if page.complete:
            return tuple(runs)
        cursor = page.next_run_id
