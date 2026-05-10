import json
import math

from qcodes.dataset.sqlite.database import connect, get_DB_location


def get_runs_via_sql():
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
    conn = connect(get_DB_location())
    try:
        cursor = conn.cursor()
        return _fetch_run_rows(cursor, empty_as_none=False)
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
    conn = connect(get_DB_location())

    try:
        cursor = conn.cursor()
        return _fetch_run_rows(cursor, "WHERE runs.run_timestamp > ?", (last_time, ))
    finally:
        conn.close()


def _fetch_run_rows(cursor, where="", params=(), empty_as_none=True):
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
       FROM runs
       LEFT JOIN experiments ON runs.exp_id = experiments.exp_id
       {where}
    """, params)
    values = cursor.fetchall()

    if len(values) == 0:
        return None if empty_as_none else {}

    column_names = [desc[0] for desc in cursor.description]

    outDict = {}
    for row in values:
        metadata = dict(zip(column_names[1:], row[1:]))
        _add_run_summary_fields(cursor, metadata)
        outDict[row[0]] = metadata

    return outDict


def _add_run_summary_fields(cursor, metadata):
    run_description = _json_dict(metadata.get("run_description"))
    measure_parameters, sweep_parameters = _parameter_roles(
        run_description,
        metadata.get("parameters")
        )

    metadata["measure_parameters"] = measure_parameters
    metadata["sweep_parameters"] = sweep_parameters
    metadata["point_shape"] = _point_shape(run_description, measure_parameters)
    metadata["expected_results"] = _shape_size(metadata["point_shape"])
    metadata["result_count"] = _result_count(cursor, metadata.get("result_table_name"))
    metadata["storage_bytes"] = _table_storage_bytes(cursor, metadata.get("result_table_name"))


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


def _shape_size(shape):
    if not shape:
        return None

    try:
        return math.prod(int(size) for size in shape)
    except (TypeError, ValueError):
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


def _table_storage_bytes(cursor, table_name):
    if not table_name:
        return None

    try:
        cursor.execute("SELECT SUM(pgsize) FROM dbstat WHERE name = ?", (table_name, ))
        value = cursor.fetchone()[0]
    except Exception:
        return _estimated_table_storage_bytes(cursor, table_name)

    return int(value) if value is not None else None


def _estimated_table_storage_bytes(cursor, table_name):
    quoted_table_name = _sqlite_identifier(table_name)
    try:
        cursor.execute(f"PRAGMA table_info({quoted_table_name})")
        columns = cursor.fetchall()
    except Exception:
        return None

    if not columns:
        return None

    numeric_bytes_per_row = 0
    variable_columns = []
    for column in columns:
        column_name = column[1]
        column_type = str(column[2] or "").upper()
        if any(type_name in column_type for type_name in ("INT", "REAL", "FLOA", "DOUB", "NUM")):
            numeric_bytes_per_row += 8
        else:
            variable_columns.append(column_name)

    variable_terms = [
        f"COALESCE(length(CAST({_sqlite_identifier(column)} AS BLOB)), 0)"
        for column in variable_columns
        ]
    variable_expression = " + ".join(variable_terms) if variable_terms else "0"

    try:
        cursor.execute(f"""
          SELECT
              COUNT(*) * ? + COALESCE(SUM({variable_expression}), 0)
          FROM {quoted_table_name}
        """, (numeric_bytes_per_row + len(columns) + 2, ))
        value = cursor.fetchone()[0]
    except Exception:
        return None

    return int(value) if value is not None else None


def get_run_status(guid):
    """
    Returns completion and result count information for one run.

    """
    conn = connect(get_DB_location())
    try:
        cursor = conn.cursor()

        cursor.execute("""
          SELECT
              completed_timestamp,
              is_completed,
              result_table_name
          FROM runs
          WHERE guid=?
          LIMIT 1
        """, (guid, ))
        value = cursor.fetchone()
        if value is None:
            return {}

        return {
            "completed_timestamp": value[0],
            "is_completed": value[1],
            "result_count": _result_count(cursor, value[2]),
            "storage_bytes": _table_storage_bytes(cursor, value[2]),
            }
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
    conn = connect(get_DB_location())
    
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
