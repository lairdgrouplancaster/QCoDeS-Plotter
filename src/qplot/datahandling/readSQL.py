import json
import math
import os

from qcodes.dataset.sqlite.database import get_DB_location

from qplot.datahandling.readonly import qcodes_read_only_connection


def get_runs_via_sql(database_path=None, include_details=True):
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
        cursor = conn.cursor()
        return _fetch_run_rows(
            cursor,
            empty_as_none=False,
            include_details=include_details,
            )
    finally:
        conn.close()


def get_runs_basic_via_sql(database_path=None):
    """
    Read the run list without scanning result tables.

    This is the fast path for opening large databases. It includes enough
    metadata to populate the run table and measurement placeholders, while
    leaving expensive counts, setpoint-shape inference, and storage estimates
    to later targeted loads.

    """
    return get_runs_via_sql(database_path=database_path, include_details=False)


def iter_run_detail_batches_via_sql(
        database_path,
        run_ids,
        batch_size=1,
        infer_missing_shapes=True,
        include_storage_bytes=True,
        include_storage_estimate=False,
        include_read_setpoint_count=True,
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
        cursor = conn.cursor()
        for offset in range(0, len(run_ids), batch_size):
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
        conn.close()


def iter_run_shape_batches_via_sql(database_path, run_ids, batch_size=1):
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
        cursor = conn.cursor()
        for offset in range(0, len(run_ids), batch_size):
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
                if metadata.get("setpoint_shape") or metadata.get("point_shape")
                }
            if rows:
                yield rows
    finally:
        conn.close()


def iter_run_storage_batches_via_sql(database_path, run_ids, batch_size=25):
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
                    }
            if rows:
                yield rows
    finally:
        conn.close()


def find_new_runs(last_time):
    """
    Fetches all runs produced after the last_time. Otherwise functions the same
    as get_runs_via_sql()

    Parameters
    ----------
    last_time : float
        Only data after produced last_time will be returned.
        last_time is in unix time.

    Returns
    -------
    outDict : dict{int: dict}
        A nested dictionary of requried data.
        Has layout: 
            run_id : {column_name: column_data}
    """
    conn = qcodes_read_only_connection(get_DB_location())

    try:
        cursor = conn.cursor()
        return _fetch_run_rows(cursor, "WHERE runs.run_timestamp > ?", (last_time, ))
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
    expected_results = _expected_results_from_shapes(
        run_description,
        measure_parameters,
        )
    metadata["expected_results"] = (
        expected_results
        if expected_results is not None
        else _shape_size(metadata["point_shape"])
        )
    metadata["setpoint_count"] = _shape_size(metadata["setpoint_shape"])


def _add_run_detail_fields(
        cursor,
        metadata,
        infer_missing_shapes=True,
        include_storage_bytes=True,
        include_storage_estimate=False,
        include_read_setpoint_count=True,
        storage_bytes_by_table=None,
        ):
    run_description = _json_dict(metadata.get("run_description"))
    measure_parameters = metadata.get("measure_parameters") or []
    sweep_parameters = metadata.get("sweep_parameters") or []

    metadata["result_count"] = _result_count(cursor, metadata.get("result_table_name"))
    expected_results = metadata.get("expected_results")
    if infer_missing_shapes and not metadata["point_shape"]:
        setpoint_shape = _setpoint_shape_from_result_table(
            cursor,
            metadata.get("result_table_name"),
            sweep_parameters,
            )
        metadata["setpoint_shape"] = setpoint_shape
        metadata["point_shape"] = _point_shape_from_setpoint_shape(
            setpoint_shape,
            measure_parameters,
            metadata["result_count"],
            )
        expected_results = _shape_size(metadata["point_shape"])
    metadata["expected_results"] = (
        expected_results
        if expected_results is not None
        else _shape_size(metadata["point_shape"])
        )
    metadata["setpoint_count"] = _shape_size(metadata["setpoint_shape"])
    if (
            include_read_setpoint_count
            and _is_keyboard_interrupt(metadata.get("measurement_exception"))
            ):
        metadata["read_setpoint_count"] = _read_setpoint_count(
            cursor,
            metadata.get("result_table_name"),
            sweep_parameters,
            )
    if include_storage_bytes:
        table_name = metadata.get("result_table_name")
        if storage_bytes_by_table is not None:
            metadata["storage_bytes"] = storage_bytes_by_table.get(table_name)
        else:
            metadata["storage_bytes"] = _table_storage_bytes(
                cursor,
                table_name,
                result_count=metadata.get("result_count"),
                )
    elif include_storage_estimate:
        metadata["storage_bytes"] = _estimated_table_storage_bytes(
            cursor,
            metadata.get("result_table_name"),
            result_count=metadata.get("result_count"),
            )


def _json_dict(value):
    if not value:
        return {}

    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}

    return decoded if isinstance(decoded, dict) else {}


def _parameter_roles(run_description, parameter_text):
    dependencies = (
        run_description
        .get("interdependencies_", {})
        .get("dependencies", {})
        )
    if not dependencies:
        dependencies = _legacy_dependencies(run_description)

    measure_parameters = list(dependencies.keys())
    sweep_parameters = []
    for dependents in dependencies.values():
        for name in dependents:
            if name not in sweep_parameters:
                sweep_parameters.append(name)

    if not measure_parameters:
        parameters = [
            parameter.strip()
            for parameter in (parameter_text or "").split(",")
            if parameter.strip()
            ]
        measure_parameters = [
            parameter for parameter in parameters
            if parameter not in sweep_parameters
            ]

    return measure_parameters, sweep_parameters


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
    try:
        cursor.execute(f"PRAGMA table_info({quoted_table_name})")
        columns = {row[1] for row in cursor.fetchall()}
    except Exception:
        return None

    if not columns or any(parameter not in columns for parameter in sweep_parameters):
        return None

    shape = []
    for parameter in sweep_parameters:
        quoted_parameter = _sqlite_identifier(parameter)
        try:
            cursor.execute(f"""
              SELECT COUNT(DISTINCT {quoted_parameter})
              FROM {quoted_table_name}
              WHERE {quoted_parameter} IS NOT NULL
            """)
            count = int(cursor.fetchone()[0])
        except Exception:
            return None

        if count <= 0:
            return None
        shape.append(count)

    return shape


def _read_setpoint_count(cursor, table_name, sweep_parameters):
    if not table_name or not sweep_parameters:
        return None

    quoted_table_name = _sqlite_identifier(table_name)
    try:
        cursor.execute(f"PRAGMA table_info({quoted_table_name})")
        columns = {row[1] for row in cursor.fetchall()}
        if any(parameter not in columns for parameter in sweep_parameters):
            return None

        quoted_columns = ", ".join(
            _sqlite_identifier(parameter)
            for parameter in sweep_parameters
            )
        not_null = " AND ".join(
            f"{_sqlite_identifier(parameter)} IS NOT NULL"
            for parameter in sweep_parameters
            )
        cursor.execute(f"""
          SELECT DISTINCT {quoted_columns}
          FROM {quoted_table_name}
          WHERE {not_null}
        """)
        return len(cursor.fetchall())
    except Exception:
        return None


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
        return None

    try:
        cursor.execute("SELECT SUM(pgsize) FROM dbstat WHERE name = ?", (table_name, ))
        value = cursor.fetchone()[0]
    except Exception:
        return _estimated_table_storage_bytes(
            cursor,
            table_name,
            result_count=result_count,
            )

    if value is None:
        return _estimated_table_storage_bytes(
            cursor,
            table_name,
            result_count=result_count,
            )
    return int(value)


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
    except Exception:
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


def get_run_status(guid):
    """
    Returns completion and result count information for one run.

    """
    conn = qcodes_read_only_connection(get_DB_location())
    try:
        cursor = conn.cursor()
        optional_columns = _existing_run_columns(cursor, ["measurement_exception"])
        optional_select = "".join(
            f", {_sqlite_identifier(column)}"
            for column in optional_columns
            )

        cursor.execute(f"""
          SELECT
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
            "completed_timestamp": value[0],
            "is_completed": value[1],
            "result_count": _result_count(cursor, value[2]),
            "storage_bytes": _table_storage_bytes(cursor, value[2]),
            "database_modified_timestamp": _database_modified_timestamp(cursor),
            }
        for index, column in enumerate(optional_columns, start=5):
            status[column] = value[index]

        if _is_keyboard_interrupt(status.get("measurement_exception")):
            run_description = _json_dict(value[3])
            _, sweep_parameters = _parameter_roles(run_description, value[4])
            status["read_setpoint_count"] = _read_setpoint_count(
                cursor,
                value[2],
                sweep_parameters,
                )

        return status
    finally:
        conn.close()


def has_finished(guid):
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
    completed_timestamp : list[float, None]
        Result of the SQL query. Either completed_timestamp as a unix time float
        or None if no entry is found.

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
        value = cursor.fetchall()

        return value[0]
    finally:
        conn.close()
