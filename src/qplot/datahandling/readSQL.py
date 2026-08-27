import json
import math
import os
import sqlite3
import stat
from itertools import islice
from time import monotonic
from typing import Literal, NamedTuple

from qcodes.dataset.sqlite.database import get_DB_location

from qplot.datahandling.readonly import (
    qcodes_read_only_connection,
    sqlite_read_only_connection,
)
from qplot.datahandling.trusted_presentation import (
    TRUSTED_PRESENTATION_MAX_PARAMETER_DEPENDENCIES,
    TRUSTED_PRESENTATION_MAX_PARAMETER_TEXT_BYTES,
    TRUSTED_PRESENTATION_MAX_PARAMETERS,
    bounded_presentation_text,
)

MAX_SELECTED_RUN_SETPOINT_SUMMARY_ROWS = 100_000
MAX_SELECTED_RUN_SETPOINT_SUMMARY_SCALAR_BYTES = 64 * 1024
MAX_SELECTED_RUN_SETPOINT_SUMMARY_SOURCE_BYTES = 8 * 1024 * 1024
MAX_SNAPSHOT_SELECTED_RUN_COLUMNS = 256
MAX_SNAPSHOT_SELECTED_RUN_SCALAR_BYTES = 1024 * 1024
MAX_SNAPSHOT_SELECTED_RUN_TOTAL_BYTES = 4 * 1024 * 1024
MAX_SNAPSHOT_SELECTED_OBSERVATION_BYTES = 64 * 1024
MAX_SNAPSHOT_SELECTED_LAYOUT_ROWS = 4_096
MAX_SNAPSHOT_SELECTED_PARAMETERS = 256
MAX_SNAPSHOT_SELECTED_SETPOINTS = 32
MAX_RUN_DESCRIPTION_SHAPE_DIMENSIONS = 32
_SNAPSHOT_SELECTED_LAYOUT_TEXT_BYTES = 512
_SNAPSHOT_STANDARD_RUN_COLUMNS = frozenset({
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
    })
_SNAPSHOT_OBSERVATION_FIELDS = frozenset({
    "database_modified_timestamp",
    "expected_results",
    "expected_results_source",
    "measure_parameters",
    "measurement_exception",
    "point_shape",
    "read_setpoint_count",
    "result_count",
    "setpoint_count",
    "setpoint_count_source",
    "setpoint_shape",
    "setpoint_shape_source",
    "storage_bytes",
    "storage_bytes_estimated",
    "sweep_parameters",
    })


class _StorageSize(NamedTuple):
    bytes: int | None
    accuracy: Literal["exact", "estimated", "unavailable"]


def _raise_if_read_aborted(cancelled_callback=None, deadline=None):
    if cancelled_callback is not None and cancelled_callback():
        raise InterruptedError("Database read cancelled.")
    if deadline is not None and monotonic() >= deadline:
        raise TimeoutError("Database read deadline exceeded.")


def _install_cancel_progress_handler(
        conn,
        cancelled_callback,
        deadline=None,
        ):
    if cancelled_callback is None and deadline is None:
        return
    _raise_if_read_aborted(cancelled_callback, deadline)
    set_progress_handler = getattr(conn, "set_progress_handler", None)
    if callable(set_progress_handler):
        set_progress_handler(
            lambda: int(
                bool(cancelled_callback is not None and cancelled_callback())
                or bool(deadline is not None and monotonic() >= deadline)
                ),
            1000,
            )


def _translate_interrupted_read(error, cancelled_callback=None, deadline=None):
    if _sql_was_interrupted(error):
        _raise_if_read_aborted(cancelled_callback, deadline)


def _notify_connection(connection_callback, connection):
    if connection_callback is not None:
        connection_callback(connection)


def _read_only_connection(
        database_path,
        expected_database_identity,
        cancelled_callback=None,
        deadline=None,
        ):
    """Open a QCoDeS view while retaining optional abort controls."""
    kwargs = {}
    if expected_database_identity is not None:
        kwargs["expected_database_identity"] = expected_database_identity
    if cancelled_callback is not None:
        kwargs["cancelled_callback"] = cancelled_callback
    if deadline is not None:
        kwargs["deadline"] = deadline
    return qcodes_read_only_connection(database_path, **kwargs)


def get_selected_run_setpoint_summaries(
        database_path,
        result_table_name,
        setpoint_names,
        result_count,
        *,
        setpoint_shape=None,
        expected_database_identity=None,
        cancelled_callback=None,
        connection_callback=None,
        deadline=None,
        ):
    """Return bounded, plain summaries for snapshot-mode run details.

    Result-table grouping is deliberately limited to runs whose already-known
    row count is at most ``MAX_SELECTED_RUN_SETPOINT_SUMMARY_ROWS``.  Unknown
    and larger runs retain any cheap shape-derived step counts without opening
    SQLite.  This function is part of the data layer so Qt widgets only render
    the mapping supplied by their controller.
    """
    names = tuple(dict.fromkeys(
        str(name)
        for name in setpoint_names or ()
        if str(name)
        ))
    summaries = {}
    try:
        bounded_result_count = (
            int(result_count)
            if result_count is not None
            else None
            )
    except (TypeError, ValueError, OverflowError):
        bounded_result_count = None

    if (
            database_path
            and result_table_name
            and names
            and bounded_result_count is not None
            and 0 <= bounded_result_count
            <= MAX_SELECTED_RUN_SETPOINT_SUMMARY_ROWS
            ):
        summaries.update(_read_selected_run_setpoint_summaries(
            database_path,
            result_table_name,
            names,
            expected_database_identity=expected_database_identity,
            cancelled_callback=cancelled_callback,
            connection_callback=connection_callback,
            deadline=deadline,
            ))

    for name, steps in zip(names, setpoint_shape or (), strict=False):
        if steps is None or steps == "":
            continue
        summaries.setdefault(name, {}).setdefault("steps", steps)
    return {
        name: dict(summary)
        for name, summary in summaries.items()
        }


def _read_selected_run_setpoint_summaries(
        database_path,
        result_table_name,
        setpoint_names,
        *,
        expected_database_identity=None,
        cancelled_callback=None,
        connection_callback=None,
        deadline=None,
        ):
    """Read one bounded snapshot summary batch, degrading to empty on error."""
    conn = None
    cursor = None
    try:
        conn = sqlite_read_only_connection(
            database_path,
            timeout=2,
            expected_database_identity=expected_database_identity,
            cancelled_callback=cancelled_callback,
            deadline=deadline,
            )
        _notify_connection(connection_callback, conn)
        _install_cancel_progress_handler(conn, cancelled_callback, deadline)
        cursor = conn.cursor()
        summaries = _selected_run_setpoint_summaries_from_cursor(
            cursor,
            result_table_name,
            setpoint_names,
            cancelled_callback=cancelled_callback,
            deadline=deadline,
            )
        _raise_if_read_aborted(cancelled_callback, deadline)
        return summaries
    except (InterruptedError, TimeoutError):
        raise
    except sqlite3.OperationalError as error:
        _translate_interrupted_read(error, cancelled_callback, deadline)
        return {}
    except Exception:
        return {}
    finally:
        try:
            _notify_connection(connection_callback, None)
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()


def _selected_run_setpoint_summaries_from_cursor(
        cursor,
        result_table_name,
        setpoint_names,
        *,
        cancelled_callback=None,
        deadline=None,
    ):
    """Return bounded summaries using an already guarded snapshot connection."""
    quoted_table = _sqlite_identifier(result_table_name)
    _raise_if_read_aborted(cancelled_callback, deadline)
    if not _selected_summary_source_within_byte_limit(cursor):
        return {}
    _raise_if_read_aborted(cancelled_callback, deadline)
    high_watermark = _result_table_integer_pk_high_watermark(
        cursor,
        quoted_table,
    )
    if (
            high_watermark is None
            or high_watermark > MAX_SELECTED_RUN_SETPOINT_SUMMARY_ROWS
            ):
        return {}
    columns = _result_table_columns(cursor, quoted_table)
    summaries = {}
    for name in setpoint_names:
        _raise_if_read_aborted(cancelled_callback, deadline)
        if name not in columns:
            continue
        summary = _selected_run_setpoint_summary(cursor, quoted_table, name)
        if summary:
            summaries[name] = summary
    return summaries


def _selected_summary_source_within_byte_limit(cursor):
    """Fail closed unless the whole private SQLite view is at most 8 MiB.

    A small row count does not bound the bytes sorted by ``GROUP BY``: one
    setpoint can be a multi-gigabyte BLOB, and a sparse database can have a
    tiny logical page count but a huge file length.  Check both SQLite's main
    page extent and the exact private main/sidecar file lengths before any
    result-table aggregate.  Snapshot-mode connections own stable private
    files, so these preflights remain valid for the following statement.
    """
    limit = MAX_SELECTED_RUN_SETPOINT_SUMMARY_SOURCE_BYTES
    try:
        cursor.execute("PRAGMA main.page_size")
        page_size_row = cursor.fetchone()
        cursor.execute("PRAGMA main.page_count")
        page_count_row = cursor.fetchone()
        if (
                page_size_row is None
                or page_count_row is None
                or len(page_size_row) != 1
                or len(page_count_row) != 1
                ):
            return False
        page_size = page_size_row[0]
        page_count = page_count_row[0]
        if (
                type(page_size) is not int
                or type(page_count) is not int
                or page_size <= 0
                or page_count < 0
                or page_count > limit // page_size
                ):
            return False

        cursor.execute("PRAGMA database_list")
        main_paths = [
            str(row[2])
            for row in cursor.fetchall()
            if len(row) >= 3 and str(row[1]) == "main" and row[2]
        ]
        if len(main_paths) != 1:
            return False

        total_bytes = 0
        for suffix in ("", "-wal", "-journal", "-shm"):
            path = f"{main_paths[0]}{suffix}"
            try:
                file_stat = os.stat(path)
            except FileNotFoundError:
                if not suffix:
                    return False
                continue
            if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size < 0:
                return False
            total_bytes += file_stat.st_size
            if total_bytes > limit:
                return False
        return True
    except Exception as error:
        if _sql_was_interrupted(error):
            raise
        return False


def _result_table_integer_pk_high_watermark(cursor, quoted_table):
    """Return a conservative QCoDeS result-table row bound.

    QCoDeS result tables use one ``id INTEGER PRIMARY KEY``. ``MAX(id)`` is a
    bounded index lookup and safely overestimates the number of current rows
    after deletions. Unknown or non-current schemas fail closed so an obsolete
    controller ``result_count`` can never authorize an unbounded GROUP BY.
    """
    try:
        cursor.execute(f"PRAGMA table_info({quoted_table})")
        primary_keys = [
            row
            for row in cursor.fetchall()
            if len(row) >= 6 and row[5]
        ]
        if len(primary_keys) != 1:
            return None
        primary_key = primary_keys[0]
        if (
                str(primary_key[1]) != "id"
                or str(primary_key[2] or "").strip().upper() != "INTEGER"
                or primary_key[5] != 1
                ):
            return None
        quoted_id = _sqlite_identifier(primary_key[1])
        cursor.execute(f"SELECT MAX({quoted_id}) FROM {quoted_table}")
        row = cursor.fetchone()
        if row is None or len(row) != 1:
            return None
        value = row[0]
        if value is None:
            return 0
        if type(value) is not int or value < 0:
            return None
        return value
    except Exception as error:
        if _sql_was_interrupted(error):
            raise
        return None


def get_snapshot_selected_run_detail(
        database_path,
        run_id,
        guid,
        run_metadata=None,
        *,
        expected_database_identity=None,
        cancelled_callback=None,
        connection_callback=None,
        deadline=None,
        ):
    """Return a bounded immutable detail view without constructing a DataSet.

    Every SQLite operation is finite and cancellable. Result-table values are
    never materialised; only the existing bounded setpoint aggregates may
    inspect a modest, already-counted result table.
    """
    from qplot.datahandling.trusted_live_queries import (
        TrustedRunRecord,
        TrustedSelectedRunDetail,
        TrustedSetpointSummary,
        bounded_parameter_presentation,
        bounded_parameter_views_from_run_metadata,
        bounded_setpoint_presentation,
        freeze_primitive_fields,
    )
    from qplot.datahandling.trusted_presentation import (
        bounded_presentation_names,
        build_selected_run_presentation,
    )
    from qplot.datahandling.trusted_snapshot import normalize_trusted_snapshot

    try:
        run_id = int(run_id)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("run_id must be a positive integer") from error
    guid = str(guid or "")
    if run_id <= 0 or not guid:
        raise ValueError("run_id and guid must identify one selected run")

    conn = _read_only_connection(
        database_path or get_DB_location(),
        expected_database_identity,
        cancelled_callback,
        deadline,
        )
    try:
        _notify_connection(connection_callback, conn)
        _install_cancel_progress_handler(conn, cancelled_callback, deadline)
        cursor = conn.cursor()
        columns = _snapshot_selected_run_columns(cursor)
        values, unavailable, snapshot_omission = _bounded_snapshot_selected_run_values(
            cursor,
            run_id,
            guid,
            columns,
            cancelled_callback=cancelled_callback,
            deadline=deadline,
            )
        if values is None:
            raise LookupError(
                f"Run {run_id} with GUID {guid} is no longer available."
            )
        if str(values.get("guid") or "") != guid:
            raise LookupError(
                f"Run {run_id} no longer has the selected GUID {guid}."
            )

        database_values = {
            name: value
            for name, value in values.items()
            if name in _SNAPSHOT_STANDARD_RUN_COLUMNS
        }
        database_values.pop("run_id", None)
        materialized = materialize_run_basic_fields(database_values)
        source_metadata = run_metadata or {}
        for name in _SNAPSHOT_OBSERVATION_FIELDS:
            if name not in source_metadata:
                continue
            value = source_metadata[name]
            bounded = _snapshot_plain_value(value)
            if bounded is not _SNAPSHOT_VALUE_UNAVAILABLE:
                materialized[name] = bounded
        materialized.update(
            _snapshot_selected_experiment_fields(
                cursor,
                values.get("exp_id"),
            )
        )
        materialized["guid"] = guid
        materialized["run_id"] = run_id
        try:
            materialized["database_modified_timestamp"] = os.path.getmtime(
                database_path
            )
        except OSError:
            materialized.setdefault("database_modified_timestamp", None)

        layout_rows, layout_unavailable = _snapshot_selected_layout_rows(
            cursor,
            run_id,
        )
        parameters, source_parameters_truncated = (
            bounded_parameter_views_from_run_metadata(
                materialized,
                layout_rows,
            )
        )

        setpoint_names_list: list[str] = []
        setpoint_names_seen: set[str] = set()
        setpoint_names_truncated = False
        for parameter in parameters:
            for name in parameter.depends_on:
                if not name or name in setpoint_names_seen:
                    continue
                if len(setpoint_names_list) >= MAX_SNAPSHOT_SELECTED_SETPOINTS:
                    setpoint_names_truncated = True
                    break
                setpoint_names_list.append(name)
                setpoint_names_seen.add(name)
            if setpoint_names_truncated:
                break
        setpoint_names = tuple(setpoint_names_list)
        if not setpoint_names:
            fallback_setpoints = materialized.get("sweep_parameters", ())
            if isinstance(fallback_setpoints, (list, tuple)):
                for index, name in enumerate(
                    islice(fallback_setpoints, MAX_SNAPSHOT_SELECTED_SETPOINTS + 1)
                ):
                    if index >= MAX_SNAPSHOT_SELECTED_SETPOINTS:
                        setpoint_names_truncated = True
                        break
                    if isinstance(name, str) and name:
                        setpoint_names_list.append(name)
                setpoint_names = tuple(dict.fromkeys(setpoint_names_list))
            elif fallback_setpoints:
                setpoint_names_truncated = True
        if setpoint_names_truncated:
            unavailable = (*unavailable, "setpoint_summaries")
        summary_values = _snapshot_selected_summary_values(
            cursor,
            materialized,
            setpoint_names,
            cancelled_callback=cancelled_callback,
            deadline=deadline,
        )
        summaries = tuple(
            TrustedSetpointSummary(
                name,
                summary.get("from"),
                summary.get("to"),
                _snapshot_positive_int(summary.get("steps")),
            )
            for name, summary in summary_values.items()
        )
        parameters, parameters_truncated = bounded_parameter_presentation(parameters)
        parameters_truncated = bool(
            parameters_truncated
            or source_parameters_truncated
            or materialized.get("parameters_truncated")
        )
        summaries, summaries_truncated = bounded_setpoint_presentation(summaries)
        metadata = {
            name: value
            for name, value in values.items()
            if (
                name not in _SNAPSHOT_STANDARD_RUN_COLUMNS
                and value is not None
            )
        }
        snapshot = normalize_trusted_snapshot(
            values.get("snapshot"),
            omission=snapshot_omission,
        )
        unavailable_fields = tuple(dict.fromkeys(
            (
                *unavailable,
                *layout_unavailable,
                *(("parameters.presentation",) if parameters_truncated else ()),
                *(
                    ("setpoint_summaries.presentation",)
                    if summaries_truncated
                    else ()
                ),
            )
        ))
        public_unavailable_fields, _unavailable_truncated = (
            bounded_presentation_names(unavailable_fields)
        )
        presentation = build_selected_run_presentation(
            run_fields={**materialized, "run_id": run_id},
            metadata_fields=metadata,
            parameters=tuple(
                {
                    "name": parameter.name,
                    "label": parameter.label,
                    "unit": parameter.unit,
                    "depends_on": parameter.depends_on,
                    "type": parameter.paramtype,
                }
                for parameter in parameters
            ),
            snapshot_summary={
                "Status": snapshot.status,
                "Message": snapshot.message,
                "Input bytes": snapshot.input_bytes,
                "Rendered nodes": len(snapshot.nodes),
            },
            setpoint_summaries=tuple(
                {
                    "name": summary.name,
                    "from": summary.first,
                    "to": summary.last,
                    "steps": summary.steps,
                }
                for summary in summaries
            ),
            unavailable_fields=public_unavailable_fields,
            parameters_truncated=parameters_truncated,
        )
        _raise_if_read_aborted(cancelled_callback, deadline)
        return TrustedSelectedRunDetail(
            run=TrustedRunRecord(
                run_id,
                freeze_primitive_fields(dict(presentation.run_fields)),
                public_unavailable_fields,
            ),
            parameters=parameters,
            metadata=freeze_primitive_fields(dict(presentation.metadata_fields)),
            snapshot=snapshot,
            setpoint_summaries=summaries,
            presentation=presentation,
            unavailable_fields=public_unavailable_fields,
        )
    except sqlite3.OperationalError as error:
        _translate_interrupted_read(error, cancelled_callback, deadline)
        raise
    finally:
        try:
            _notify_connection(connection_callback, None)
        finally:
            conn.close()


_SNAPSHOT_VALUE_UNAVAILABLE = object()


def _snapshot_plain_value(value):
    bounded, _size = _snapshot_plain_scalar(value)
    if bounded is not _SNAPSHOT_VALUE_UNAVAILABLE:
        return bounded
    if not isinstance(value, (list, tuple)) or len(value) > 256:
        return _SNAPSHOT_VALUE_UNAVAILABLE

    items = []
    total_bytes = 0
    for item in value:
        bounded, item_bytes = _snapshot_plain_scalar(item)
        if bounded is _SNAPSHOT_VALUE_UNAVAILABLE:
            return _SNAPSHOT_VALUE_UNAVAILABLE
        total_bytes += item_bytes
        if total_bytes > MAX_SNAPSHOT_SELECTED_OBSERVATION_BYTES:
            return _SNAPSHOT_VALUE_UNAVAILABLE
        items.append(bounded)
    return tuple(items)


def _snapshot_plain_scalar(value):
    if value is None or isinstance(value, (bool, int, float)):
        return value, 8
    if isinstance(value, str):
        value_bytes = len(value.encode("utf-8", errors="surrogatepass"))
        if value_bytes <= MAX_SNAPSHOT_SELECTED_OBSERVATION_BYTES:
            return value, value_bytes
        return _SNAPSHOT_VALUE_UNAVAILABLE, 0
    if isinstance(value, bytes):
        if len(value) <= MAX_SNAPSHOT_SELECTED_OBSERVATION_BYTES:
            return value, len(value)
        return _SNAPSHOT_VALUE_UNAVAILABLE, 0
    return _SNAPSHOT_VALUE_UNAVAILABLE, 0


def _snapshot_selected_run_columns(cursor):
    cursor.execute('SELECT * FROM "runs" WHERE 0')
    columns = tuple(description[0] for description in cursor.description or ())
    if (
            not {"run_id", "guid"}.issubset(columns)
            or len(columns) > MAX_SNAPSHOT_SELECTED_RUN_COLUMNS
            ):
        raise RuntimeError(
            "The selected QCoDeS run schema exceeds the bounded detail plan."
        )
    return columns


def _bounded_snapshot_selected_run_values(
        cursor,
        run_id,
        guid,
        columns,
        *,
        cancelled_callback=None,
        deadline=None,
        ):
    from qplot.datahandling.trusted_snapshot import TrustedSnapshotOmission

    values: dict[str, object] = {}
    unavailable = []
    accepted_bytes = 0
    snapshot_omission = None
    for name in columns:
        _raise_if_read_aborted(cancelled_callback, deadline)
        quoted = _sqlite_identifier(name)
        cursor.execute(
            f"SELECT typeof({quoted}), length(CAST({quoted} AS BLOB)) "
            'FROM "runs" WHERE "run_id" = ? AND "guid" = ? LIMIT 1',
            (run_id, guid),
        )
        preflight = cursor.fetchone()
        if preflight is None:
            return None, (), None
        value_type, value_bytes = preflight
        if value_type == "null" and value_bytes is None:
            values[name] = None
            continue
        if (
                value_type not in {"integer", "real", "text", "blob"}
                or type(value_bytes) is not int
                or value_bytes < 0
                ):
            raise RuntimeError(
                "A selected-run scalar preflight returned invalid metadata."
            )
        if (
                value_bytes > MAX_SNAPSHOT_SELECTED_RUN_SCALAR_BYTES
                or accepted_bytes + value_bytes
                > MAX_SNAPSHOT_SELECTED_RUN_TOTAL_BYTES
                ):
            values[name] = None
            unavailable.append(name)
            if name == "snapshot":
                snapshot_omission = TrustedSnapshotOmission(
                    (
                        "payload_limit"
                        if value_bytes > MAX_SNAPSHOT_SELECTED_RUN_SCALAR_BYTES
                        else "detail_budget"
                    ),
                    value_bytes,
                )
            continue
        cursor.execute(
            "SELECT CASE WHEN "
            f"typeof({quoted}) = ? AND length(CAST({quoted} AS BLOB)) = ? "
            f"THEN {quoted} ELSE NULL END "
            'FROM "runs" WHERE "run_id" = ? AND "guid" = ? LIMIT 1',
            (value_type, value_bytes, run_id, guid),
        )
        fetched = cursor.fetchone()
        if fetched is None:
            return None, (), None
        value = fetched[0]
        values[name] = value
        if value is None:
            unavailable.append(name)
            if name == "snapshot":
                snapshot_omission = TrustedSnapshotOmission(
                    "changed_during_read",
                    value_bytes,
                )
        else:
            accepted_bytes += value_bytes
    return values, tuple(unavailable), snapshot_omission


def _snapshot_selected_experiment_fields(cursor, exp_id):
    if exp_id is None:
        return {}
    limit = _SNAPSHOT_SELECTED_LAYOUT_TEXT_BYTES
    cursor.execute(
        "SELECT "
        "CASE WHEN length(CAST(name AS BLOB)) <= ? THEN name END, "
        "CASE WHEN length(CAST(sample_name AS BLOB)) <= ? THEN sample_name END "
        "FROM experiments WHERE exp_id = ? LIMIT 1",
        (limit, limit, exp_id),
    )
    row = cursor.fetchone()
    if row is None:
        return {}
    return {"exp_name": row[0], "sample_name": row[1]}


def _snapshot_selected_layout_rows(cursor, run_id):
    limit = _SNAPSHOT_SELECTED_LAYOUT_TEXT_BYTES
    cursor.execute(
        "SELECT layout_id, "
        "CASE WHEN length(CAST(parameter AS BLOB)) <= ? THEN parameter END, "
        "CASE WHEN length(CAST(label AS BLOB)) <= ? THEN label END, "
        "CASE WHEN length(CAST(unit AS BLOB)) <= ? THEN unit END, "
        "CASE WHEN length(CAST(inferred_from AS BLOB)) <= ? THEN inferred_from END "
        "FROM layouts WHERE run_id = ? ORDER BY layout_id LIMIT ?",
        (
            limit,
            limit,
            limit,
            limit,
            run_id,
            MAX_SNAPSHOT_SELECTED_LAYOUT_ROWS + 1,
        ),
    )
    rows = tuple(tuple(row) for row in cursor.fetchall())
    if len(rows) <= MAX_SNAPSHOT_SELECTED_LAYOUT_ROWS:
        return rows, ()
    return rows[:MAX_SNAPSHOT_SELECTED_LAYOUT_ROWS], ("layouts",)


def _snapshot_selected_summary_values(
        cursor,
        metadata,
        setpoint_names,
        *,
        cancelled_callback=None,
        deadline=None,
        ):
    result_count = _snapshot_positive_int(metadata.get("result_count"))
    summaries = {}
    if (
            result_count is not None
            and result_count <= MAX_SELECTED_RUN_SETPOINT_SUMMARY_ROWS
            and metadata.get("result_table_name")
            and setpoint_names
            ):
        summaries.update(_selected_run_setpoint_summaries_from_cursor(
            cursor,
            metadata["result_table_name"],
            setpoint_names,
            cancelled_callback=cancelled_callback,
            deadline=deadline,
        ))
    shape = metadata.get("setpoint_shape") or metadata.get("point_shape") or ()
    for name, steps in zip(setpoint_names, shape, strict=False):
        summaries.setdefault(name, {}).setdefault("steps", steps)
    return summaries


def _snapshot_positive_int(value):
    try:
        value = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if value > 0 else None


def _selected_run_setpoint_summary(cursor, quoted_table, parameter):
    """Return first, last, and distinct count for one setpoint column."""
    column = _sqlite_identifier(parameter)
    try:
        cursor.execute(f"""
          WITH distinct_values(value, first_rowid) AS (
              SELECT {column}, MIN(rowid)
              FROM {quoted_table}
              WHERE {column} IS NOT NULL
              GROUP BY {column}
          ), summary_values(first_value, last_value, steps) AS (
              SELECT
                  (
                      SELECT value
                      FROM distinct_values
                      ORDER BY first_rowid ASC
                      LIMIT 1
                  ),
                  (
                      SELECT value
                      FROM distinct_values
                      ORDER BY first_rowid DESC
                      LIMIT 1
                  ),
                  (SELECT COUNT(*) FROM distinct_values)
          )
          SELECT
              CASE WHEN length(CAST(first_value AS BLOB)) <=
                  {MAX_SELECTED_RUN_SETPOINT_SUMMARY_SCALAR_BYTES}
                  THEN first_value END,
              CASE WHEN length(CAST(last_value AS BLOB)) <=
                  {MAX_SELECTED_RUN_SETPOINT_SUMMARY_SCALAR_BYTES}
                  THEN last_value END,
              steps
          FROM summary_values
        """)
        first_value, last_value, count = cursor.fetchone()
        count = int(count or 0)
    except Exception as error:
        if _sql_was_interrupted(error):
            raise
        return {}
    if count <= 0 or first_value is None or last_value is None:
        return {}
    return {
        "from": first_value,
        "to": last_value,
        "steps": count,
        }


def get_runs_via_sql(
        database_path=None,
        include_details=True,
        cancelled_callback=None,
        connection_callback=None,
        expected_database_identity=None,
        deadline=None,
        ):
    """
    Read from the currently initialised QCoDeS database and fetches all data to
    be displayed in Main Window runList

    Returns
    -------
    outDict : dict{int: dict}
        A nested dictionary of requried data.
        Has layout: 
            run_id : {column_name: column_data}

    """
    conn = _read_only_connection(
        database_path or get_DB_location(),
        expected_database_identity,
        cancelled_callback,
        deadline,
        )
    try:
        _notify_connection(connection_callback, conn)
        _install_cancel_progress_handler(conn, cancelled_callback, deadline)
        cursor = conn.cursor()
        rows = _fetch_run_rows(
            cursor,
            empty_as_none=False,
            include_details=include_details,
            )
        _raise_if_read_aborted(cancelled_callback, deadline)
        return rows
    except sqlite3.OperationalError as error:
        _translate_interrupted_read(error, cancelled_callback, deadline)
        raise
    finally:
        try:
            _notify_connection(connection_callback, None)
        finally:
            conn.close()


def get_runs_basic_via_sql(
        database_path=None,
        cancelled_callback=None,
        connection_callback=None,
        expected_database_identity=None,
        deadline=None,
        ):
    """
    Read the run list without scanning result tables.

    This is the fast path for opening large databases. It includes enough
    metadata to populate the run table and measurement placeholders, while
    leaving expensive counts, setpoint-shape inference, and storage estimates
    to later targeted loads.

    """
    return get_runs_via_sql(
        database_path=database_path,
        include_details=False,
        cancelled_callback=cancelled_callback,
        connection_callback=connection_callback,
        expected_database_identity=expected_database_identity,
        deadline=deadline,
        )


def iter_run_detail_batches_via_sql(
        database_path,
        run_ids,
        batch_size=1,
        infer_missing_shapes=True,
        include_storage_bytes=True,
        include_storage_estimate=False,
        include_read_setpoint_count=True,
        cancelled_callback=None,
        connection_callback=None,
        expected_database_identity=None,
        deadline=None,
        ):
    """
    Yield detailed run metadata in small batches.

    This keeps the initial load responsive while allowing the GUI to fill in
    expensive counts, setpoint shapes, and storage sizes progressively.

    """
    run_ids = [run_id for run_id in run_ids if run_id is not None]
    _raise_if_read_aborted(cancelled_callback, deadline)
    if not run_ids:
        return

    batch_size = max(1, int(batch_size or 1))
    conn = _read_only_connection(
        database_path or get_DB_location(),
        expected_database_identity,
        cancelled_callback,
        deadline,
        )
    try:
        _notify_connection(connection_callback, conn)
        _install_cancel_progress_handler(conn, cancelled_callback, deadline)
        cursor = conn.cursor()
        for offset in range(0, len(run_ids), batch_size):
            _raise_if_read_aborted(cancelled_callback, deadline)
            batch = run_ids[offset:offset + batch_size]
            placeholders = ", ".join("?" for _ in batch)
            rows = _fetch_run_rows(
                cursor,
                f"WHERE runs.run_id IN ({placeholders})",
                tuple(batch),
                empty_as_none=False,
                include_details=True,
                infer_missing_shapes=infer_missing_shapes,
                include_storage_bytes=include_storage_bytes,
                include_storage_estimate=include_storage_estimate,
                include_read_setpoint_count=include_read_setpoint_count,
            )
            _raise_if_read_aborted(cancelled_callback, deadline)
            yield rows or {}
        _raise_if_read_aborted(cancelled_callback, deadline)
    except sqlite3.OperationalError as error:
        _translate_interrupted_read(error, cancelled_callback, deadline)
        raise
    finally:
        try:
            _notify_connection(connection_callback, None)
        finally:
            conn.close()


def iter_run_shape_batches_via_sql(
        database_path,
        run_ids,
        batch_size=1,
        cancelled_callback=None,
        connection_callback=None,
        expected_database_identity=None,
        deadline=None,
        ):
    """
    Yield setpoint-shape metadata for runs that need result-table inference.

    This can require COUNT(DISTINCT ...) scans on result columns, so it runs
    after cheap row counts are visible.

    """
    run_ids = [run_id for run_id in run_ids if run_id is not None]
    _raise_if_read_aborted(cancelled_callback, deadline)
    if not run_ids:
        return

    batch_size = max(1, int(batch_size or 1))
    conn = _read_only_connection(
        database_path or get_DB_location(),
        expected_database_identity,
        cancelled_callback,
        deadline,
        )
    try:
        _notify_connection(connection_callback, conn)
        _install_cancel_progress_handler(conn, cancelled_callback, deadline)
        cursor = conn.cursor()
        for offset in range(0, len(run_ids), batch_size):
            _raise_if_read_aborted(cancelled_callback, deadline)
            batch = run_ids[offset:offset + batch_size]
            placeholders = ", ".join("?" for _ in batch)
            rows = _fetch_run_rows(
                cursor,
                f"WHERE runs.run_id IN ({placeholders})",
                tuple(batch),
                empty_as_none=False,
                include_details=True,
                infer_missing_shapes=True,
                include_storage_bytes=False,
                include_read_setpoint_count=False,
                )
            rows = {
                run_id: metadata
                for run_id, metadata in (rows or {}).items()
                if (
                    metadata.get("setpoint_shape")
                    or metadata.get("point_shape")
                    or metadata.get("setpoint_count") is not None
                    )
            }
            if rows:
                _raise_if_read_aborted(cancelled_callback, deadline)
                yield rows
        _raise_if_read_aborted(cancelled_callback, deadline)
    except sqlite3.OperationalError as error:
        _translate_interrupted_read(error, cancelled_callback, deadline)
        raise
    finally:
        try:
            _notify_connection(connection_callback, None)
        finally:
            conn.close()


def iter_run_storage_batches_via_sql(
        database_path,
        run_ids,
        batch_size=25,
        cancelled_callback=None,
        connection_callback=None,
        expected_database_identity=None,
        deadline=None,
        ):
    """
    Yield per-run storage sizes after the cheap detail pass has completed.

    Storage lookup can require a dbstat scan over the database. Keeping this as
    a separate pass prevents size calculation from blocking result counts and
    completion metadata on very large databases.

    """
    run_ids = [run_id for run_id in run_ids if run_id is not None]
    _raise_if_read_aborted(cancelled_callback, deadline)
    if not run_ids:
        return

    batch_size = max(1, int(batch_size or 1))
    conn = _read_only_connection(
        database_path or get_DB_location(),
        expected_database_identity,
        cancelled_callback,
        deadline,
        )
    try:
        _notify_connection(connection_callback, conn)
        _install_cancel_progress_handler(conn, cancelled_callback, deadline)
        cursor = conn.cursor()
        run_tables = _run_storage_tables(cursor, run_ids)
        table_names = {
            metadata.get("result_table_name")
            for metadata in run_tables.values()
            if metadata.get("result_table_name")
            }
        sizes = _table_storage_bytes_by_name(cursor, table_names)
        if not sizes:
            _raise_if_read_aborted(cancelled_callback, deadline)
            return

        for offset in range(0, len(run_ids), batch_size):
            _raise_if_read_aborted(cancelled_callback, deadline)
            rows = {}
            for run_id in run_ids[offset:offset + batch_size]:
                metadata = run_tables.get(run_id)
                if not metadata:
                    continue

                storage_bytes = sizes.get(metadata.get("result_table_name"))
                if storage_bytes is None:
                    continue

                rows[run_id] = {
                    "guid": metadata.get("guid"),
                    "storage_bytes": storage_bytes,
                    "storage_bytes_estimated": False,
                    }
            if rows:
                _raise_if_read_aborted(cancelled_callback, deadline)
                yield rows
        _raise_if_read_aborted(cancelled_callback, deadline)
    except sqlite3.OperationalError as error:
        _translate_interrupted_read(error, cancelled_callback, deadline)
        raise
    finally:
        try:
            _notify_connection(connection_callback, None)
        finally:
            conn.close()


def find_new_runs(
        last_run_id,
        database_path=None,
        cancelled_callback=None,
        connection_callback=None,
        expected_database_identity=None,
        deadline=None,
        ):
    """
    Fetch all runs created after the last seen run ID.

    Run IDs provide a monotonic cursor even when a run has no timestamp or
    multiple runs share the same timestamp.

    Parameters
    ----------
    last_run_id : int
        Only runs with a greater run ID will be returned.

    Returns
    -------
    outDict : dict{int: dict}
        A nested dictionary of requried data.
        Has layout: 
            run_id : {column_name: column_data}
    """
    conn = _read_only_connection(
        database_path or get_DB_location(),
        expected_database_identity,
        cancelled_callback,
        deadline,
        )

    try:
        _notify_connection(connection_callback, conn)
        _install_cancel_progress_handler(conn, cancelled_callback, deadline)
        cursor = conn.cursor()
        rows = _fetch_run_rows(cursor, "WHERE runs.run_id > ?", (last_run_id, ))
        _raise_if_read_aborted(cancelled_callback, deadline)
        return rows
    except sqlite3.OperationalError as error:
        _translate_interrupted_read(error, cancelled_callback, deadline)
        raise
    finally:
        try:
            _notify_connection(connection_callback, None)
        finally:
            conn.close()


def _fetch_run_rows(
        cursor,
        where="",
        params=(),
        empty_as_none=True,
        include_details=True,
        infer_missing_shapes=True,
        include_storage_bytes=True,
        include_storage_estimate=False,
        include_read_setpoint_count=True,
        storage_bytes_by_table=None,
        ):
    optional_columns = _existing_run_columns(cursor, ["measurement_exception"])
    optional_select = "".join(
        f", runs.{_sqlite_identifier(column)} AS {_sqlite_identifier(column)}"
        for column in optional_columns
        )
    cursor.execute(f"""
       SELECT
           runs.run_id,
           runs.exp_id,
           runs.name,
           runs.run_timestamp,
           runs.completed_timestamp,
           runs.is_completed,
           runs.guid,
           runs.result_table_name,
           runs.parameters,
           runs.run_description,
           experiments.name AS exp_name,
           experiments.sample_name
           {optional_select}
       FROM runs
       LEFT JOIN experiments ON runs.exp_id = experiments.exp_id
       {where}
    """, params)
    values = cursor.fetchall()

    if len(values) == 0:
        return None if empty_as_none else {}

    column_names = [desc[0] for desc in cursor.description]

    outDict = {}
    database_modified_timestamp = _database_modified_timestamp(cursor)
    for row in values:
        metadata = dict(zip(column_names[1:], row[1:], strict=False))
        metadata["database_modified_timestamp"] = database_modified_timestamp
        metadata = materialize_run_basic_fields(metadata)
        if include_details:
            _add_run_detail_fields(
                cursor,
                metadata,
                infer_missing_shapes=infer_missing_shapes,
                include_storage_bytes=include_storage_bytes,
                include_storage_estimate=include_storage_estimate,
                include_read_setpoint_count=include_read_setpoint_count,
                storage_bytes_by_table=storage_bytes_by_table,
                )
        outDict[row[0]] = metadata

    return outDict


def _add_run_basic_fields(metadata):
    run_description = _json_dict(metadata.get("run_description"))
    measure_parameters, sweep_parameters, parameters_truncated = (
        _bounded_parameter_roles(
            run_description,
            metadata.get("parameters"),
        )
    )

    point_shape, shape_truncated = _bounded_point_shape(
        run_description,
        measure_parameters,
        )

    metadata["measure_parameters"] = measure_parameters
    metadata["sweep_parameters"] = sweep_parameters
    metadata["parameters_truncated"] = bool(
        parameters_truncated or shape_truncated
    )
    metadata["point_shape"] = point_shape
    metadata["setpoint_shape"] = metadata["point_shape"]
    shape_source = "planned" if metadata["setpoint_shape"] else None
    metadata["setpoint_shape_source"] = shape_source
    expected_results, expected_shape_truncated = (
        _bounded_expected_results_from_shapes(
            run_description,
            measure_parameters,
        )
    )
    metadata["parameters_truncated"] = bool(
        metadata["parameters_truncated"] or expected_shape_truncated
    )
    metadata["expected_results"] = expected_results
    metadata["expected_results_source"] = (
        "planned" if expected_results is not None else None
        )
    metadata["setpoint_count"] = _shape_size(metadata["setpoint_shape"])
    metadata["setpoint_count_source"] = shape_source


def materialize_run_basic_fields(metadata):
    """Return one run mapping with qPlot's shared derived basic fields.

    Snapshot readers and the trusted fixed-query adapter deliberately share
    this transformation.  Keeping it independent of cursors, connections,
    QCoDeS objects, and filesystem access prevents the two read paths from
    drifting while allowing the trusted path to materialise only primitive
    query results.
    """
    materialized = dict(metadata or {})
    _add_run_basic_fields(materialized)
    return materialized


def materialize_run_observation(
        metadata,
        *,
        result_count=None,
        setpoint_shape=None,
        setpoint_count=None,
        storage_bytes=None,
        storage_bytes_estimated=None,
        read_setpoint_count=None,
        ):
    """Return shared detail fields from already-bounded observations.

    All database work happens before this function.  The snapshot path can
    continue collecting observations with cursors, while the trusted adapter
    supplies the same values from fixed supervisor queries.
    """
    materialized = materialize_run_basic_fields(metadata)
    if result_count is not None:
        materialized["result_count"] = int(result_count)
    elif "result_count" not in materialized:
        materialized["result_count"] = None

    if setpoint_count is not None or setpoint_shape is not None:
        normalized_shape = (
            [int(size) for size in setpoint_shape]
            if setpoint_shape
            else None
        )
        materialized["setpoint_shape"] = normalized_shape
        materialized["point_shape"] = _point_shape_from_setpoint_shape(
            normalized_shape,
            materialized.get("measure_parameters"),
            materialized.get("result_count"),
        )
        materialized["setpoint_shape_source"] = (
            "observed" if normalized_shape else None
        )
        materialized["setpoint_count"] = (
            int(setpoint_count) if setpoint_count is not None else None
        )
        materialized["setpoint_count_source"] = (
            "observed" if setpoint_count is not None else None
        )
        materialized["expected_results"] = None
        materialized["expected_results_source"] = None

    if read_setpoint_count is not None:
        materialized["read_setpoint_count"] = int(read_setpoint_count)

    if storage_bytes is not None or storage_bytes_estimated is not None:
        materialized["storage_bytes"] = (
            int(storage_bytes) if storage_bytes is not None else None
        )
        materialized["storage_bytes_estimated"] = storage_bytes_estimated

    _add_completed_observed_result_count(materialized)
    return materialized


def _add_run_detail_fields(
        cursor,
        metadata,
        infer_missing_shapes=True,
        include_storage_bytes=True,
        include_storage_estimate=False,
        include_read_setpoint_count=True,
        storage_bytes_by_table=None,
        ):
    measure_parameters = metadata.get("measure_parameters") or []
    sweep_parameters = metadata.get("sweep_parameters") or []

    metadata["result_count"] = _result_count(cursor, metadata.get("result_table_name"))
    observed_setpoints = None
    if infer_missing_shapes and not metadata["point_shape"]:
        observed_setpoints = _add_observed_shape_fields(cursor, metadata)
    _add_completed_observed_result_count(metadata)
    if include_read_setpoint_count and (
            not bool(metadata.get("is_completed"))
            or _is_keyboard_interrupt(metadata.get("measurement_exception"))
            ):
        if observed_setpoints is None:
            observed_setpoints = _run_setpoint_observation(
                cursor,
                metadata.get("result_table_name"),
                _json_dict(metadata.get("run_description")),
                measure_parameters,
                sweep_parameters,
                )
        metadata["read_setpoint_count"] = observed_setpoints["count"]
    if include_storage_bytes:
        table_name = metadata.get("result_table_name")
        if storage_bytes_by_table is not None:
            storage_bytes = storage_bytes_by_table.get(table_name)
            storage_size = _StorageSize(
                storage_bytes,
                "exact" if storage_bytes is not None else "unavailable",
                )
        else:
            storage_size = _table_storage_bytes(
                cursor,
                table_name,
                result_count=metadata.get("result_count"),
                )
        _add_storage_size_fields(metadata, storage_size)
    elif include_storage_estimate:
        storage_bytes = _estimated_table_storage_bytes(
            cursor,
            metadata.get("result_table_name"),
            result_count=metadata.get("result_count"),
            )
        _add_storage_size_fields(
            metadata,
            _StorageSize(
                storage_bytes,
                "estimated" if storage_bytes is not None else "unavailable",
                ),
            )


def _add_storage_size_fields(metadata, storage_size):
    metadata["storage_bytes"] = storage_size.bytes
    metadata["storage_bytes_estimated"] = {
        "exact": False,
        "estimated": True,
        "unavailable": None,
        }[storage_size.accuracy]


def _add_observed_shape_fields(cursor, metadata):
    measure_parameters = metadata.get("measure_parameters") or []
    sweep_parameters = metadata.get("sweep_parameters") or []
    observed_setpoints = _run_setpoint_observation(
        cursor,
        metadata.get("result_table_name"),
        _json_dict(metadata.get("run_description")),
        measure_parameters,
        sweep_parameters,
        )
    setpoint_shape = observed_setpoints["shape"]
    metadata["setpoint_shape"] = setpoint_shape
    metadata["point_shape"] = _point_shape_from_setpoint_shape(
        setpoint_shape,
        measure_parameters,
        metadata.get("result_count"),
        )
    metadata["setpoint_shape_source"] = (
        "observed" if setpoint_shape else None
        )
    metadata["setpoint_count"] = observed_setpoints["count"]
    metadata["setpoint_count_source"] = (
        "observed" if observed_setpoints["count"] is not None else None
        )
    metadata["expected_results"] = None
    metadata["expected_results_source"] = None
    _add_completed_observed_result_count(metadata)
    return observed_setpoints


def _add_completed_observed_result_count(metadata):
    if (
            metadata.get("expected_results") is None
            and _run_is_complete(metadata)
            and metadata.get("result_count") is not None
            ):
        metadata["expected_results"] = metadata["result_count"]
        metadata["expected_results_source"] = "observed"


def _json_dict(value):
    if not value:
        return {}

    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError, RecursionError):
        return {}

    return decoded if isinstance(decoded, dict) else {}


def _parameter_roles(run_description, parameter_text):
    measure, sweep, _truncated = _bounded_parameter_roles(
        run_description,
        parameter_text,
    )
    return measure, sweep


def _bounded_parameter_roles(run_description, parameter_text):
    dependencies, truncated = _bounded_parameter_dependencies(run_description)
    measure_parameters: list[str] = []
    sweep_parameters: list[str] = []
    measure_seen: set[str] = set()
    sweep_seen: set[str] = set()
    all_seen: set[str] = set()

    def add(target, seen, name):
        nonlocal truncated
        if name in seen:
            return
        if (
            name not in all_seen
            and len(all_seen) >= TRUSTED_PRESENTATION_MAX_PARAMETERS
        ):
            truncated = True
            return
        target.append(name)
        seen.add(name)
        all_seen.add(name)

    for parameter, dependents in dependencies.items():
        add(measure_parameters, measure_seen, parameter)
        for name in dependents:
            add(sweep_parameters, sweep_seen, name)

    if isinstance(parameter_text, str):
        raw_parameters = parameter_text.split(",", TRUSTED_PRESENTATION_MAX_PARAMETERS)
        if len(raw_parameters) > TRUSTED_PRESENTATION_MAX_PARAMETERS:
            truncated = True
            raw_parameters.pop()
        for raw_parameter in raw_parameters:
            parameter, was_truncated = _bounded_parameter_identifier(
                raw_parameter.strip()
            )
            truncated = truncated or was_truncated
            if parameter and parameter not in sweep_seen:
                add(measure_parameters, measure_seen, parameter)
    elif parameter_text:
        truncated = True

    return measure_parameters, sweep_parameters, truncated


def _parameter_dependencies(run_description):
    dependencies, _truncated = _bounded_parameter_dependencies(run_description)
    return dependencies


def _bounded_parameter_dependencies(run_description):
    truncated = False
    interdependencies = run_description.get("interdependencies_")
    if not isinstance(interdependencies, dict):
        interdependencies = {}
    dependencies = (
        interdependencies.get("dependencies", {})
    )
    if not isinstance(dependencies, dict) or not dependencies:
        return _bounded_legacy_dependencies(run_description)

    output: dict[str, list[str]] = {}
    items = islice(
        dependencies.items(),
        TRUSTED_PRESENTATION_MAX_PARAMETERS + 1,
    )
    for index, (raw_parameter, setpoints) in enumerate(items):
        if index >= TRUSTED_PRESENTATION_MAX_PARAMETERS:
            truncated = True
            break
        parameter, name_truncated = _bounded_parameter_identifier(raw_parameter)
        truncated = truncated or name_truncated
        if not parameter or not isinstance(setpoints, (list, tuple)):
            truncated = truncated or bool(setpoints)
            continue
        bounded_setpoints = []
        seen = set()
        for dependency_index, raw_setpoint in enumerate(
            islice(
                setpoints,
                TRUSTED_PRESENTATION_MAX_PARAMETER_DEPENDENCIES + 1,
            )
        ):
            if dependency_index >= TRUSTED_PRESENTATION_MAX_PARAMETER_DEPENDENCIES:
                truncated = True
                break
            setpoint, dependency_truncated = _bounded_parameter_identifier(
                raw_setpoint
            )
            truncated = truncated or dependency_truncated
            if setpoint and setpoint not in seen:
                bounded_setpoints.append(setpoint)
                seen.add(setpoint)
        if bounded_setpoints:
            output[parameter] = bounded_setpoints
    return output, truncated


def _legacy_dependencies(run_description):
    dependencies, _truncated = _bounded_legacy_dependencies(run_description)
    return dependencies


def _bounded_legacy_dependencies(run_description):
    out: dict[str, list[str]] = {}
    truncated = False
    interdependencies = run_description.get("interdependencies")
    paramspecs = (
        interdependencies.get("paramspecs", [])
        if isinstance(interdependencies, dict)
        else []
    )
    if not isinstance(paramspecs, (list, tuple)):
        return out, bool(paramspecs)
    for index, paramspec in enumerate(
        islice(paramspecs, TRUSTED_PRESENTATION_MAX_PARAMETERS + 1)
    ):
        if index >= TRUSTED_PRESENTATION_MAX_PARAMETERS:
            truncated = True
            break
        if not isinstance(paramspec, dict):
            truncated = True
            continue
        depends_on = paramspec.get("depends_on") or []
        name, name_truncated = _bounded_parameter_identifier(paramspec.get("name"))
        truncated = truncated or name_truncated
        if not name or not isinstance(depends_on, (list, tuple)):
            truncated = truncated or bool(depends_on)
            continue
        bounded_dependencies = []
        seen = set()
        for dependency_index, raw_dependency in enumerate(
            islice(
                depends_on,
                TRUSTED_PRESENTATION_MAX_PARAMETER_DEPENDENCIES + 1,
            )
        ):
            if dependency_index >= TRUSTED_PRESENTATION_MAX_PARAMETER_DEPENDENCIES:
                truncated = True
                break
            dependency, dependency_truncated = _bounded_parameter_identifier(
                raw_dependency
            )
            truncated = truncated or dependency_truncated
            if dependency and dependency not in seen:
                bounded_dependencies.append(dependency)
                seen.add(dependency)
        if bounded_dependencies:
            out[name] = bounded_dependencies
    return out, truncated


def _bounded_parameter_identifier(value):
    if not isinstance(value, str):
        return "", value is not None
    return bounded_presentation_text(
        value,
        limit=TRUSTED_PRESENTATION_MAX_PARAMETER_TEXT_BYTES,
    )


def _point_shape(run_description, measure_parameters):
    shape, _truncated = _bounded_point_shape(run_description, measure_parameters)
    return shape


def _bounded_point_shape(run_description, measure_parameters):
    shapes = run_description.get("shapes")
    if not isinstance(shapes, dict):
        return None, False

    best_shape = None
    best_size = 0
    truncated = False
    for parameter in measure_parameters:
        shape = shapes.get(parameter)
        if isinstance(shape, list) and shape:
            dimensions = list(islice(shape, MAX_RUN_DESCRIPTION_SHAPE_DIMENSIONS + 1))
            if len(dimensions) > MAX_RUN_DESCRIPTION_SHAPE_DIMENSIONS:
                truncated = True
                continue
            try:
                point_shape = [int(size) for size in dimensions]
            except (TypeError, ValueError):
                continue

            size = _shape_size(point_shape) or 0
            if size > best_size:
                best_shape = point_shape
                best_size = size

    return best_shape, truncated


def _expected_results_from_shapes(run_description, measure_parameters):
    expected, _truncated = _bounded_expected_results_from_shapes(
        run_description,
        measure_parameters,
    )
    return expected


def _bounded_expected_results_from_shapes(run_description, measure_parameters):
    shapes = run_description.get("shapes")
    if not isinstance(shapes, dict) or not measure_parameters:
        return None, False

    sizes = []
    truncated = False
    for parameter in measure_parameters:
        shape = shapes.get(parameter)
        if not isinstance(shape, list) or not shape:
            return None, truncated

        dimensions = list(islice(shape, MAX_RUN_DESCRIPTION_SHAPE_DIMENSIONS + 1))
        if len(dimensions) > MAX_RUN_DESCRIPTION_SHAPE_DIMENSIONS:
            truncated = True
            return None, truncated

        size = _shape_size(dimensions)
        if size is None:
            return None, truncated
        sizes.append(size)

    return (sum(sizes) if sizes else None), truncated


def _shape_size(shape):
    if not shape:
        return None

    try:
        dimensions = list(islice(iter(shape), MAX_RUN_DESCRIPTION_SHAPE_DIMENSIONS + 1))
        if len(dimensions) > MAX_RUN_DESCRIPTION_SHAPE_DIMENSIONS:
            return None
        return math.prod(int(size) for size in dimensions)
    except (TypeError, ValueError):
        return None


def _database_modified_timestamp(cursor):
    try:
        cursor.execute("PRAGMA database_list")
        databases = cursor.fetchall()
    except Exception as error:
        if _sql_was_interrupted(error):
            raise
        return None

    for database in databases:
        if len(database) < 3 or database[1] != "main" or not database[2]:
            continue
        try:
            return os.path.getmtime(database[2])
        except OSError:
            return None

    return None


def _existing_run_columns(cursor, column_names):
    try:
        cursor.execute("PRAGMA table_info(runs)")
        columns = {row[1] for row in cursor.fetchall()}
    except Exception as error:
        if _sql_was_interrupted(error):
            raise
        return []

    return [column for column in column_names if column in columns]


def _is_keyboard_interrupt(value):
    return bool(value and "KeyboardInterrupt" in str(value))


def _run_is_complete(metadata):
    return bool(metadata.get("completed_timestamp") or metadata.get("is_completed"))


def _point_shape_from_result_table(
    cursor,
    table_name,
    sweep_parameters,
    measure_parameters=None,
    result_count=None,
):
    shape = _setpoint_shape_from_result_table(cursor, table_name, sweep_parameters)
    return _point_shape_from_setpoint_shape(shape, measure_parameters, result_count)


def _point_shape_from_setpoint_shape(shape, measure_parameters=None, result_count=None):
    if not shape:
        return None

    shape_size = _shape_size(shape)
    measure_count = len(measure_parameters or [])
    if shape_size and result_count and measure_count > 1:
        try:
            result_count = int(result_count)
        except (TypeError, ValueError):
            result_count = None

        if (
            result_count
            and result_count % shape_size == 0
            and result_count // shape_size == measure_count
        ):
            shape = shape + [measure_count]

    return shape


def _setpoint_shape_from_result_table(cursor, table_name, sweep_parameters):
    if not table_name or not sweep_parameters:
        return None

    quoted_table_name = _sqlite_identifier(table_name)
    columns = _result_table_columns(cursor, quoted_table_name)
    if not columns or any(parameter not in columns for parameter in sweep_parameters):
        return None

    observation = _setpoint_observation(
        cursor,
        quoted_table_name,
        sweep_parameters,
        columns,
        )
    return observation["shape"]


def _read_setpoint_count(cursor, table_name, sweep_parameters):
    if not table_name or not sweep_parameters:
        return None

    quoted_table_name = _sqlite_identifier(table_name)
    columns = _result_table_columns(cursor, quoted_table_name)
    if not columns or any(parameter not in columns for parameter in sweep_parameters):
        return None

    return _distinct_setpoint_count(
        cursor,
        quoted_table_name,
        sweep_parameters,
        )


def _run_setpoint_observation(
        cursor,
        table_name,
        run_description,
        measure_parameters,
        sweep_parameters,
        ):
    """Return a safe global shape and the largest per-dependent point count."""
    empty = {"shape": None, "count": None}
    if not table_name:
        return empty

    quoted_table_name = _sqlite_identifier(table_name)
    columns = _result_table_columns(cursor, quoted_table_name)
    if not columns:
        return empty

    dependencies = _parameter_dependencies(run_description)
    observations = []
    if dependencies:
        for parameter in measure_parameters:
            setpoints = dependencies.get(parameter)
            if (
                    not setpoints
                    or parameter not in columns
                    or any(setpoint not in columns for setpoint in setpoints)
                    ):
                continue
            observation = _setpoint_observation(
                cursor,
                quoted_table_name,
                setpoints,
                columns,
                dependent_parameter=parameter,
                )
            if observation["count"] is not None:
                observations.append((tuple(setpoints), observation))
    elif sweep_parameters and all(parameter in columns for parameter in sweep_parameters):
        observation = _setpoint_observation(
            cursor,
            quoted_table_name,
            sweep_parameters,
            columns,
            )
        if observation["count"] is not None:
            observations.append((tuple(sweep_parameters), observation))

    if not observations:
        return empty

    observed_count = max(observation["count"] for _, observation in observations)
    dependency_sets = {setpoints for setpoints, _ in observations}
    observed_shapes = [observation["shape"] for _, observation in observations]
    shared_shape = (
        observed_shapes[0]
        if (
            len(dependency_sets) == 1
            and observed_shapes[0] is not None
            and all(shape == observed_shapes[0] for shape in observed_shapes)
            )
        else None
        )
    return {"shape": shared_shape, "count": observed_count}


def _setpoint_observation(
        cursor,
        quoted_table_name,
        sweep_parameters,
        columns,
        dependent_parameter=None,
        ):
    empty = {"shape": None, "count": None}
    required_columns = list(sweep_parameters)
    if dependent_parameter is not None:
        required_columns.append(dependent_parameter)
    if not required_columns or any(column not in columns for column in required_columns):
        return empty

    observed_count = _distinct_setpoint_count(
        cursor,
        quoted_table_name,
        sweep_parameters,
        dependent_parameter=dependent_parameter,
        )
    if not observed_count:
        return empty

    conditions = _setpoint_not_null_conditions(
        sweep_parameters,
        dependent_parameter=dependent_parameter,
        )
    distinct_counts = ", ".join(
        f"COUNT(DISTINCT {_sqlite_identifier(parameter)})"
        for parameter in sweep_parameters
        )
    try:
        cursor.execute(f"""
          SELECT {distinct_counts}
          FROM {quoted_table_name}
          WHERE {conditions}
        """)
        shape = [int(count) for count in cursor.fetchone()]
    except Exception as error:
        if _sql_was_interrupted(error):
            raise
        return {"shape": None, "count": observed_count}

    if any(count <= 0 for count in shape) or _shape_size(shape) != observed_count:
        return {"shape": None, "count": observed_count}
    return {"shape": shape, "count": observed_count}


def _distinct_setpoint_count(
        cursor,
        quoted_table_name,
        sweep_parameters,
        dependent_parameter=None,
        ):
    quoted_columns = ", ".join(
        _sqlite_identifier(parameter)
        for parameter in sweep_parameters
        )
    conditions = _setpoint_not_null_conditions(
        sweep_parameters,
        dependent_parameter=dependent_parameter,
        )
    try:
        cursor.execute(f"""
          SELECT COUNT(*)
          FROM (
              SELECT DISTINCT {quoted_columns}
              FROM {quoted_table_name}
              WHERE {conditions}
          ) AS qplot_distinct_setpoints
        """)
        count = int(cursor.fetchone()[0])
    except Exception as error:
        if _sql_was_interrupted(error):
            raise
        return None
    return count if count > 0 else None


def _setpoint_not_null_conditions(sweep_parameters, dependent_parameter=None):
    parameters = list(sweep_parameters)
    if dependent_parameter is not None:
        parameters.append(dependent_parameter)
    return " AND ".join(
        f"{_sqlite_identifier(parameter)} IS NOT NULL"
        for parameter in parameters
        )


def _result_table_columns(cursor, quoted_table_name):
    try:
        cursor.execute(f"PRAGMA table_info({quoted_table_name})")
        return {row[1] for row in cursor.fetchall()}
    except Exception as error:
        if _sql_was_interrupted(error):
            raise
        return set()


def _result_count(cursor, table_name):
    if not table_name:
        return None

    try:
        cursor.execute(f"SELECT COUNT(*) FROM {_sqlite_identifier(table_name)}")
        return cursor.fetchone()[0]
    except Exception as error:
        if _sql_was_interrupted(error):
            raise
        return None


def _sqlite_identifier(name):
    return f'"{str(name).replace(chr(34), chr(34) * 2)}"'


def _table_storage_bytes(cursor, table_name, result_count=None):
    if not table_name:
        return _StorageSize(None, "unavailable")

    try:
        cursor.execute("SELECT SUM(pgsize) FROM dbstat WHERE name = ?", (table_name, ))
        row = cursor.fetchone()
        value = row[0] if row else None
        if value is not None:
            return _StorageSize(int(value), "exact")
    except Exception as err:
        if _sql_was_interrupted(err):
            raise

    estimated_bytes = _estimated_table_storage_bytes(
        cursor,
        table_name,
        result_count=result_count,
        )
    if estimated_bytes is None:
        return _StorageSize(None, "unavailable")
    return _StorageSize(estimated_bytes, "estimated")


def _sql_was_interrupted(error):
    return "interrupted" in str(error).lower()


def _run_storage_tables(cursor, run_ids):
    if not run_ids:
        return {}

    rows = {}
    chunk_size = 500
    for offset in range(0, len(run_ids), chunk_size):
        batch = run_ids[offset:offset + chunk_size]
        placeholders = ", ".join("?" for _ in batch)
        try:
            cursor.execute(f"""
              SELECT run_id, guid, result_table_name
              FROM runs
              WHERE run_id IN ({placeholders})
            """, tuple(batch))
        except Exception as error:
            if _sql_was_interrupted(error):
                raise
            continue

        for run_id, guid, table_name in cursor.fetchall():
            rows[run_id] = {
                "guid": guid,
                "result_table_name": table_name,
                }

    return rows


def _table_storage_bytes_by_name(cursor, table_names):
    table_names = sorted({name for name in table_names if name})
    if not table_names:
        return {}

    placeholders = ", ".join("?" for _ in table_names)
    try:
        cursor.execute(
            f"""
            SELECT name, SUM(pgsize)
            FROM dbstat
            WHERE name IN ({placeholders})
            GROUP BY name
            """,
            tuple(table_names),
            )
    except Exception as err:
        if _sql_was_interrupted(err):
            raise
        return {}

    sizes = {}
    for name, value in cursor.fetchall():
        if name not in table_names or value is None:
            continue
        try:
            sizes[name] = int(value)
        except (TypeError, ValueError):
            pass

    return sizes


def _estimated_table_storage_bytes(cursor, table_name, result_count=None):
    if not table_name:
        return None

    quoted_table_name = _sqlite_identifier(table_name)
    try:
        cursor.execute(f"PRAGMA table_info({quoted_table_name})")
        columns = cursor.fetchall()
    except Exception as error:
        if _sql_was_interrupted(error):
            raise
        return None

    if not columns:
        return None

    try:
        row_count = int(result_count)
    except (TypeError, ValueError):
        row_count = None

    if row_count is None:
        row_count = _result_count(cursor, table_name)

    if row_count is None:
        return None

    return max(0, row_count) * _estimated_table_row_bytes(columns)


def _estimated_table_row_bytes(columns):
    row_bytes = len(columns) + 2
    for column in columns:
        column_type = str(column[2] or "").upper()
        if any(type_name in column_type for type_name in ("INT", "REAL", "FLOA", "DOUB", "NUM")):
            row_bytes += 8
        else:
            row_bytes += 32
    return row_bytes


def get_run_status(
        guid,
        database_path=None,
        include_storage_bytes=True,
        cancelled_callback=None,
        connection_callback=None,
        expected_database_identity=None,
        deadline=None,
        ):
    """
    Returns completion and result count information for one run.

    """
    conn = _read_only_connection(
        database_path or get_DB_location(),
        expected_database_identity,
        cancelled_callback,
        deadline,
        )
    try:
        _notify_connection(connection_callback, conn)
        _install_cancel_progress_handler(conn, cancelled_callback, deadline)
        cursor = conn.cursor()
        optional_columns = _existing_run_columns(cursor, ["measurement_exception"])
        optional_select = "".join(
            f", {_sqlite_identifier(column)}"
            for column in optional_columns
            )

        cursor.execute(f"""
          SELECT
              run_timestamp,
              completed_timestamp,
              is_completed,
              result_table_name,
              run_description,
              parameters
              {optional_select}
          FROM runs
          WHERE guid=?
          LIMIT 1
        """, (guid, ))
        value = cursor.fetchone()
        if value is None:
            _raise_if_read_aborted(cancelled_callback, deadline)
            return {}

        status = {
            "run_timestamp": value[0],
            "completed_timestamp": value[1],
            "is_completed": value[2],
            "result_count": _result_count(cursor, value[3]),
            "database_modified_timestamp": _database_modified_timestamp(cursor),
            }
        if include_storage_bytes:
            _add_storage_size_fields(
                status,
                _table_storage_bytes(
                    cursor,
                    value[3],
                    result_count=status["result_count"],
                    ),
                )
        for index, column in enumerate(optional_columns, start=6):
            status[column] = value[index]

        shape_metadata = {
            "completed_timestamp": value[1],
            "is_completed": value[2],
            "result_table_name": value[3],
            "run_description": value[4],
            "parameters": value[5],
            "result_count": status["result_count"],
            }
        _add_run_basic_fields(shape_metadata)
        observed_setpoints = None
        if not shape_metadata["point_shape"]:
            observed_setpoints = _add_observed_shape_fields(cursor, shape_metadata)
        _add_completed_observed_result_count(shape_metadata)
        for field in (
                "point_shape",
                "setpoint_shape",
                "setpoint_shape_source",
                "setpoint_count",
                "setpoint_count_source",
                "expected_results",
                "expected_results_source",
                ):
            status[field] = shape_metadata.get(field)

        if (
                not bool(value[2])
                or _is_keyboard_interrupt(status.get("measurement_exception"))
                ):
            if observed_setpoints is None:
                observed_setpoints = _run_setpoint_observation(
                    cursor,
                    value[3],
                    _json_dict(value[4]),
                    shape_metadata["measure_parameters"],
                    shape_metadata["sweep_parameters"],
                    )
            status["read_setpoint_count"] = observed_setpoints["count"]

        _raise_if_read_aborted(cancelled_callback, deadline)
        return status
    except sqlite3.OperationalError as error:
        _translate_interrupted_read(error, cancelled_callback, deadline)
        raise
    finally:
        try:
            _notify_connection(connection_callback, None)
        finally:
            conn.close()


def has_finished(
        guid,
        expected_database_identity=None,
        cancelled_callback=None,
        connection_callback=None,
        deadline=None,
        ) -> float | None:
    """
    Checks if specific run (by guid) has finished running.
    If the run with guid has finished, returns the completed time. 
    Otherwise returns a NULL value which python interprets as None.

    Parameters
    ----------
    guid : str
        The unique id of the run to look up.

    Returns
    -------
    completed_timestamp : float | None
        The completion timestamp as a Unix-time float. Returns None when the
        run is present but unfinished, or when no matching run exists.

    """
    conn = _read_only_connection(
        get_DB_location(),
        expected_database_identity,
        cancelled_callback,
        deadline,
        )
    
    try:
        _notify_connection(connection_callback, conn)
        _install_cancel_progress_handler(conn, cancelled_callback, deadline)
        cursor = conn.cursor()

        cursor.execute("""
          SELECT
              completed_timestamp
          FROM runs
          WHERE guid=?
          LIMIT 1
        """, (guid, ))
        row = cursor.fetchone()
        if row is None or row[0] is None:
            _raise_if_read_aborted(cancelled_callback, deadline)
            return None
        _raise_if_read_aborted(cancelled_callback, deadline)
        return float(row[0])
    except sqlite3.OperationalError as error:
        _translate_interrupted_read(error, cancelled_callback, deadline)
        raise
    finally:
        try:
            _notify_connection(connection_callback, None)
        finally:
            conn.close()
