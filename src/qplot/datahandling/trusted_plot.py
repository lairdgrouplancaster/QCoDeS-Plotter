"""Explicit plot data captured through the application's trusted broker.

Only a run's result table is staged, never database pages or unrelated runs.
The private spool lets the existing precision-preserving QCoDeS decoder and
bounded heatmap aggregator operate on one immutable acquisition prefix. No
source transaction survives a page, a BLOB chunk, or time spent processing it.
"""

import sqlite3
import tempfile
from pathlib import Path
from typing import Any, cast

from qcodes.dataset.data_set_cache import DataSetCacheWithDBBackend
from qcodes.dataset.descriptions.versioning.converters import new_to_old
from qcodes.dataset.descriptions.versioning.serialization import from_json_to_current
from qcodes.dataset.sqlite.connection import AtomicConnection

from .trusted_live import TrustedLiveQueryError, TrustedQuery

PAGE_ROWS = 8192
BLOB_CHUNK_BYTES = 256 * 1024
MAX_PARAMETERS = 256


def identifier(name):
    return '"' + str(name).replace('"', '""') + '"'


class TrustedPlotDataset:
    """Connection-free run description and viewer-owned QCoDeS cache."""

    def __init__(self, service, row):
        (self.run_id, self.guid, self.table_name, description,
         completed, self.name, self.exp_name, self.sample_name) = row
        self.service = service
        self.path_to_db = service.database_instance.logical_path
        self.description_json = description
        self.description = from_json_to_current(description)
        self._parameters = list(new_to_old(self.description.interdeps).paramspecs)
        if not 0 < len(self._parameters) <= MAX_PARAMETERS:
            raise ValueError("Plot parameter count exceeds the supported bound.")
        self._completed = bool(completed)
        self.number_of_results = 0
        # Only qPlot's cache accessors are used; no DataSet database methods.
        self.cache = DataSetCacheWithDBBackend(cast(Any, self))
        self.cache.prepare()

    @property
    def completed(self):
        return self._completed

    @property
    def running(self):
        return not self._completed

    @property
    def paramspecs(self):
        return {parameter.name: parameter for parameter in self._parameters}

    def get_parameters(self):
        return list(self._parameters)


def plot_dataset(executor, service, guid):
    rows = executor.query(
        "SELECT r.run_id, r.guid, r.result_table_name, r.run_description, "
        "r.is_completed, r.name, e.name, e.sample_name "
        "FROM runs r JOIN experiments e ON r.exp_id=e.exp_id "
        "WHERE r.guid=? LIMIT 2", (guid,),
    ).rows
    if len(rows) != 1:
        raise ValueError("The requested plot GUID does not identify one run.")
    return TrustedPlotDataset(service, rows[0])


class PlotPrefix:
    """Owned private storage, shared only after its writer has closed."""

    def __init__(self):
        self.directory = tempfile.TemporaryDirectory(prefix="qplot-plot-")
        self.path = Path(self.directory.name) / "results.db"
        self.row_count = 0
        self.decode_bytes = 0
        self.watermark = 0
        self.disk_bytes = 0

    def connect(self, *, decode_arrays=False, cancelled=lambda: False):
        if decode_arrays:
            from qcodes.dataset.sqlite import database as qcodes_database

            from .readonly import _register_qcodes_sqlite_types

            _register_qcodes_sqlite_types(qcodes_database)
        conn = sqlite3.connect(
            self.path.as_uri() + "?mode=ro&immutable=1", uri=True,
            factory=AtomicConnection,
            detect_types=sqlite3.PARSE_DECLTYPES if decode_arrays else 0,
        )
        conn.execute("PRAGMA query_only=ON")
        conn.set_progress_handler(lambda: int(cancelled()), 1000)
        return conn


def plot_prefix(executor, dataset, heatmap_plan=None):
    """Capture a run in resumable steps, returning its immutable private spool.

    Yields occur with no source transaction open, after each numeric page or
    array chunk. Only the broker dispatcher may advance or close this iterator.
    """
    table = identifier(dataset.table_name)
    executor.report_progress("Checking plot data")
    metadata, extent = executor.query_batch((
        TrustedQuery(
            "SELECT guid, result_table_name, run_description, is_completed "
            "FROM runs WHERE run_id=?", (dataset.run_id,),
        ),
        TrustedQuery(f"SELECT MAX(id) FROM {table}"),
    ))
    expected = (dataset.guid, dataset.table_name, dataset.description_json)
    if len(metadata.rows) != 1 or metadata.rows[0][:3] != expected:
        raise TrustedLiveQueryError("The plot run identity changed.")
    completed = bool(metadata.rows[0][3])
    watermark = extent.rows[0][0] or 0
    cache_key = (dataset.run_id, dataset.guid, dataset.description_json, heatmap_plan)
    retained = dataset.service._plot_prefix_cache.get(cache_key)
    if completed and retained is not None and retained.watermark == watermark:
        return retained
    specs = dataset.get_parameters()
    names = [identifier(spec.name) for spec in specs]
    if any(spec.type not in {"numeric", "array", "complex", "text"} for spec in specs):
        raise ValueError("Unsupported plot storage type.")
    if heatmap_plan is not None and watermark > heatmap_plan.full_limit:
        plan = heatmap_plan
        if any(dataset.paramspecs[name].type != "numeric" for name in (plan.x, plan.y, plan.z)):
            raise ValueError("The numeric heatmap plan requires numeric parameters.")
        count = 0
        executor.report_progress("Checking plot size")
        for start in range(0, watermark, 65_536):
            count += executor.query(
                f"SELECT COUNT(*) FROM {table} WHERE id>? AND id<=? "
                f"AND {identifier(plan.z)} IS NOT NULL",
                (start, min(start + 65_536, watermark)),
            ).rows[0][0]
            yield
            if count > plan.full_limit:
                break
        if count > plan.full_limit:
            from .trusted_heatmap import PlotOperationLimitError, numeric_heatmap
            if plan.operations:
                raise PlotOperationLimitError(
                    "This heatmap exceeds max_full_heatmap_points. "
                    "Disable operations or increase max_full_heatmap_points.")
            heatmap = yield from numeric_heatmap(executor, dataset, plan, watermark, completed)
            final = executor.query(
                "SELECT guid, result_table_name, run_description FROM runs WHERE run_id=?",
                (dataset.run_id,),
            ).rows
            if final != (expected,):
                raise TrustedLiveQueryError("The plot run identity changed during capture.")
            heatmap.watermark = watermark
            if completed and heatmap.disk_bytes <= 512 * 1024 * 1024:
                _cache_prefix(dataset.service, cache_key, heatmap)
            return heatmap
    prefix = PlotPrefix()
    prefix.watermark = watermark
    executor.report_progress("Reading plot data", 0, watermark)
    # This is a newly created, private database. It contains no source metadata
    # besides the completion observation needed by the existing cache loader.
    try:
        with sqlite3.connect(prefix.path) as target:
            def interrupted():
                try:
                    executor.check_cancelled()
                    return 0
                except (InterruptedError, TimeoutError):
                    return 1
            target.set_progress_handler(interrupted, 1000)
            target.execute("PRAGMA cache_size=-2048")
            target.execute("CREATE TABLE runs(run_id INTEGER PRIMARY KEY, is_completed INTEGER)")
            target.execute("INSERT INTO runs VALUES (?, ?)", (dataset.run_id, completed))
            columns = ", ".join(f"{name} {spec.type}" for name, spec in zip(names, specs, strict=True))
            target.execute(f"CREATE TABLE {table}(id INTEGER PRIMARY KEY, {columns})")
            # BLOB lengths travel separately; even a multi-gigabyte array never
            # becomes a scalar in the reader, IPC, or this process.
            values_sql = ", ".join(
                f"CASE WHEN typeof({name})='blob' THEN NULL ELSE {name} END, "
                f"CASE WHEN typeof({name})='blob' THEN length({name}) ELSE NULL END"
                for name in names
            )
            last_id = 0
            page_size = min(PAGE_ROWS, max(1, 65536 // (2 * len(names))))
            while last_id < watermark:
                rows = executor.query(
                    f"SELECT id, {values_sql} FROM {table} "
                    "WHERE id>? AND id<=? ORDER BY id LIMIT ?",
                    (last_id, watermark, page_size),
                ).rows
                if not rows:
                    break
                for row in rows:
                    executor.check_cancelled()
                    row_id = row[0]
                    expressions = ["?"]
                    bindings = [row_id]
                    for index in range(len(names)):
                        value, size = row[1 + 2 * index:3 + 2 * index]
                        expressions.append("?" if size is None else "zeroblob(?)")
                        bindings.append(value if size is None else size)
                        prefix.decode_bytes += 128 + (size or 0) + (
                            4 * len(value) if isinstance(value, str) else 0)
                    target.execute(
                        f"INSERT INTO {table} VALUES ({','.join(expressions)})", bindings,
                    )
                    for index in range(len(names)):
                        size = row[2 + 2 * index]
                        if size is None:
                            continue
                        with target.blobopen(dataset.table_name, specs[index].name, row_id) as blob:
                            array_phase = f"Reading array {specs[index].name} (record {prefix.row_count + 1:,})"
                            executor.report_progress(array_phase, 0, size, unit="array")
                            for offset in range(0, size, BLOB_CHUNK_BYTES):
                                chunk = executor.query(
                                    "SELECT qplot_read_blob(?, ?, ?, ?, ?)",
                                    (dataset.table_name, specs[index].name, row_id,
                                     offset, min(BLOB_CHUNK_BYTES, size - offset)),
                                ).rows
                                if len(chunk) != 1 or len(chunk[0][0]) != min(BLOB_CHUNK_BYTES, size - offset):
                                    raise TrustedLiveQueryError("A captured array record changed.")
                                blob.write(chunk[0][0])
                                executor.report_progress(array_phase, offset + len(chunk[0][0]), size, unit="array")
                                yield
                    prefix.row_count += 1
                last_id = rows[-1][0]
                target.commit()
                executor.report_progress("Reading plot data", last_id, watermark)
                if last_id < watermark:
                    yield
            executor.report_progress("Validating plot data")
            final = executor.query(
                "SELECT guid, result_table_name, run_description FROM runs WHERE run_id=?",
                (dataset.run_id,),
            ).rows
            if final != (expected,):
                raise TrustedLiveQueryError("The plot run identity changed during capture.")
        # Context managers commit but do not close sqlite3 connections.
        target.close()
        prefix.disk_bytes = prefix.path.stat().st_size
        if completed and prefix.disk_bytes <= 512 * 1024 * 1024:
            _cache_prefix(dataset.service, cache_key, prefix)
        return prefix
    except BaseException:
        if "target" in locals():
            target.close()
        prefix.directory.cleanup()
        raise


def _cache_prefix(service, key, prefix):
    cache = service._plot_prefix_cache
    cache.pop(key, None)
    while cache and (len(cache) >= 8 or
            sum(item.disk_bytes for item in cache.values()) + prefix.disk_bytes > 512 * 1024 * 1024):
        cache.pop(next(iter(cache)))
    cache[key] = prefix
