import json
import math
import os
from typing import Literal, NamedTuple

from qcodes.dataset.sqlite.database import get_DB_location

from qplot.datahandling.readonly import qcodes_read_only_connection


class _StorageSize(NamedTuple):
    bytes: int | None
    accuracy: Literal["exact", "estimated", "unavailable"]


def _install_cancel_progress_handler(conn, cancelled_callback):
    if cancelled_callback is None:
        return
    if cancelled_callback():
        raise InterruptedError("Database read cancelled.")
    set_progress_handler = getattr(conn, "set_progress_handler", None)
    if callable(set_progress_handler):
        set_progress_handler(lambda: int(bool(cancelled_callback())), 1000)


def _notify_connection(connection_callback, connection):
    if connection_callback is not None:
        connection_callback(connection)


def get_runs_via_sql(
        database_path=None,
        include_details=True,
        cancelled_callback=None,
        connection_callback=None,
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
    conn = qcodes_read_only_connection(database_path or get_DB_location())
    try:
        _notify_connection(connection_callback, conn)
        _install_cancel_progress_handler(conn, cancelled_callback)
        cursor = conn.cursor()
        return _fetch_run_rows(
            cursor,
            empty_as_none=False,
            include_details=include_details,
            )
    finally:
        try:
            _notify_connection(connection_callback, None)
        finally:
            conn.close()


def get_runs_basic_via_sql(
        database_path=None,
        cancelled_callback=None,
        connection_callback=None,
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
        ):
    """
    Yield detailed run metadata in small batches.

    This keeps the initial load responsive while allowing the GUI to fill in
    expensive counts, setpoint shapes, and storage sizes progressively.

    """
    run_ids = [run_id for run_id in run_ids if run_id is not None]
    if not run_ids:
        return

    batch_size = max(1, int(batch_size or 1))
    conn = qcodes_read_only_connection(database_path or get_DB_location())
    try:
        _notify_connection(connection_callback, conn)
        _install_cancel_progress_handler(conn, cancelled_callback)
        cursor = conn.cursor()
        for offset in range(0, len(run_ids), batch_size):
            if cancelled_callback is not None and cancelled_callback():
                raise InterruptedError("Database detail read cancelled.")
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
            yield rows or {}
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
        ):
    """
    Yield setpoint-shape metadata for runs that need result-table inference.

    This can require COUNT(DISTINCT ...) scans on result columns, so it runs
    after cheap row counts are visible.

    """
    run_ids = [run_id for run_id in run_ids if run_id is not None]
    if not run_ids:
        return

    batch_size = max(1, int(batch_size or 1))
    conn = qcodes_read_only_connection(database_path or get_DB_location())
    try:
        _notify_connection(connection_callback, conn)
        _install_cancel_progress_handler(conn, cancelled_callback)
        cursor = conn.cursor()
        for offset in range(0, len(run_ids), batch_size):
            if cancelled_callback is not None and cancelled_callback():
                raise InterruptedError("Database shape read cancelled.")
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
                yield rows
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
        ):
    """
    Yield per-run storage sizes after the cheap detail pass has completed.

    Storage lookup can require a dbstat scan over the database. Keeping this as
    a separate pass prevents size calculation from blocking result counts and
    completion metadata on very large databases.

    """
    run_ids = [run_id for run_id in run_ids if run_id is not None]
    if not run_ids:
        return

    batch_size = max(1, int(batch_size or 1))
    conn = qcodes_read_only_connection(database_path or get_DB_location())
    try:
        _notify_connection(connection_callback, conn)
        _install_cancel_progress_handler(conn, cancelled_callback)
        cursor = conn.cursor()
        run_tables = _run_storage_tables(cursor, run_ids)
        table_names = {
            metadata.get("result_table_name")
            for metadata in run_tables.values()
            if metadata.get("result_table_name")
            }
        sizes = _table_storage_bytes_by_name(cursor, table_names)
        if not sizes:
            return

        for offset in range(0, len(run_ids), batch_size):
            if cancelled_callback is not None and cancelled_callback():
                raise InterruptedError("Database storage read cancelled.")
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
                yield rows
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
    conn = qcodes_read_only_connection(database_path or get_DB_location())

    try:
        _notify_connection(connection_callback, conn)
        _install_cancel_progress_handler(conn, cancelled_callback)
        cursor = conn.cursor()
        return _fetch_run_rows(cursor, "WHERE runs.run_id > ?", (last_run_id, ))
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
        _add_run_basic_fields(metadata)
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
    measure_parameters, sweep_parameters = _parameter_roles(
        run_description,
        metadata.get("parameters")
        )

    metadata["measure_parameters"] = measure_parameters
    metadata["sweep_parameters"] = sweep_parameters
    metadata["point_shape"] = _point_shape(run_description, measure_parameters)
    metadata["setpoint_shape"] = metadata["point_shape"]
    shape_source = "planned" if metadata["setpoint_shape"] else None
    metadata["setpoint_shape_source"] = shape_source
    expected_results = _expected_results_from_shapes(
        run_description,
        measure_parameters,
        )
    metadata["expected_results"] = expected_results
    metadata["expected_results_source"] = (
        "planned" if expected_results is not None else None
        )
    metadata["setpoint_count"] = _shape_size(metadata["setpoint_shape"])
    metadata["setpoint_count_source"] = shape_source


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
    if (
            include_read_setpoint_count
            and _is_keyboard_interrupt(metadata.get("measurement_exception"))
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
    except (TypeError, json.JSONDecodeError):
        return {}

    return decoded if isinstance(decoded, dict) else {}


def _parameter_roles(run_description, parameter_text):
    dependencies = _parameter_dependencies(run_description)

    measure_parameters = list(dependencies.keys())
    sweep_parameters = []
    for dependents in dependencies.values():
        for name in dependents:
            if name not in sweep_parameters:
                sweep_parameters.append(name)

    parameters = [
        parameter.strip()
        for parameter in (parameter_text or "").split(",")
        if parameter.strip()
        ]
    for parameter in parameters:
        if parameter not in sweep_parameters and parameter not in measure_parameters:
            measure_parameters.append(parameter)

    return measure_parameters, sweep_parameters


def _parameter_dependencies(run_description):
    dependencies = (
        run_description
        .get("interdependencies_", {})
        .get("dependencies", {})
        )
    if not isinstance(dependencies, dict) or not dependencies:
        dependencies = _legacy_dependencies(run_description)

    if not isinstance(dependencies, dict):
        return {}

    return {
        parameter: list(setpoints)
        for parameter, setpoints in dependencies.items()
        if isinstance(setpoints, (list, tuple)) and setpoints
        }


def _legacy_dependencies(run_description):
    out = {}
    paramspecs = run_description.get("interdependencies", {}).get("paramspecs", [])
    for paramspec in paramspecs:
        if not isinstance(paramspec, dict):
            continue
        depends_on = paramspec.get("depends_on") or []
        name = paramspec.get("name")
        if name and depends_on:
            out[name] = depends_on
    return out


def _point_shape(run_description, measure_parameters):
    shapes = run_description.get("shapes")
    if not isinstance(shapes, dict):
        return None

    best_shape = None
    best_size = 0
    for parameter in measure_parameters:
        shape = shapes.get(parameter)
        if isinstance(shape, list) and shape:
            try:
                point_shape = [int(size) for size in shape]
            except (TypeError, ValueError):
                continue

            size = _shape_size(point_shape) or 0
            if size > best_size:
                best_shape = point_shape
                best_size = size

    return best_shape


def _expected_results_from_shapes(run_description, measure_parameters):
    shapes = run_description.get("shapes")
    if not isinstance(shapes, dict) or not measure_parameters:
        return None

    sizes = []
    for parameter in measure_parameters:
        shape = shapes.get(parameter)
        if not isinstance(shape, list) or not shape:
            return None

        size = _shape_size(shape)
        if size is None:
            return None
        sizes.append(size)

    return sum(sizes) if sizes else None


def _shape_size(shape):
    if not shape:
        return None

    try:
        return math.prod(int(size) for size in shape)
    except (TypeError, ValueError):
        return None


def _database_modified_timestamp(cursor):
    try:
        cursor.execute("PRAGMA database_list")
        databases = cursor.fetchall()
    except Exception:
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
    except Exception:
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
    except Exception:
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
    except Exception:
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
    except Exception:
        return set()


def _result_count(cursor, table_name):
    if not table_name:
        return None

    try:
        cursor.execute(f"SELECT COUNT(*) FROM {_sqlite_identifier(table_name)}")
        return cursor.fetchone()[0]
    except Exception:
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
        except Exception:
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
    except Exception:
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
        ):
    """
    Returns completion and result count information for one run.

    """
    conn = qcodes_read_only_connection(database_path or get_DB_location())
    try:
        _notify_connection(connection_callback, conn)
        _install_cancel_progress_handler(conn, cancelled_callback)
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

        if _is_keyboard_interrupt(status.get("measurement_exception")):
            if observed_setpoints is None:
                observed_setpoints = _run_setpoint_observation(
                    cursor,
                    value[2],
                    _json_dict(value[3]),
                    shape_metadata["measure_parameters"],
                    shape_metadata["sweep_parameters"],
                    )
            status["read_setpoint_count"] = observed_setpoints["count"]

        return status
    finally:
        try:
            _notify_connection(connection_callback, None)
        finally:
            conn.close()


def has_finished(guid) -> float | None:
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
    conn = qcodes_read_only_connection(get_DB_location())
    
    try:
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
            return None
        return float(row[0])
    finally:
        conn.close()
