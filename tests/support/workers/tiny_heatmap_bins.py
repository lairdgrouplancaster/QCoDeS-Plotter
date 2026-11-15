"""Shared fixtures and assertions for tiny heatmap bins."""

from types import SimpleNamespace

__all__ = [
    '_SqlExecutor',
]


class _SqlExecutor:
    def __init__(self, connection):
        self.connection = connection

    def check_cancelled(self):
        pass

    def report_progress(self, *_args, **_kwargs):
        pass

    def query(self, sql, bindings=()):
        return SimpleNamespace(rows=tuple(self.connection.execute(sql, bindings).fetchall()))

    def query_batch(self, queries):
        return tuple(self.query(query.sql, query.bindings) for query in queries)
