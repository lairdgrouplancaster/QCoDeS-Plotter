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
    # Connect to SQL database and create cursor to access it.
    conn = connect(get_DB_location())
    cursor = conn.cursor()
    
    #Fetch data
    cursor.execute("""
       SELECT
           runs.run_id,
           runs.exp_id,
           runs.name,
           runs.run_timestamp,
           runs.completed_timestamp,
           runs.is_completed,
           runs.guid,
           experiments.name AS exp_name,
           experiments.sample_name
       FROM runs
       LEFT JOIN experiments ON runs.exp_id = experiments.exp_id
    """)
    column_names = [desc[0] for desc in cursor.description]
    
    # Convert fetched data to dict
    outDict = {}
    for row in cursor.fetchall():
        outDict[row[0]] = dict(zip(column_names[1:], row[1:]))
        
    # Close to prevent SQL locks
    conn.close()

    return outDict


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
    
    cursor = conn.cursor()
    
    cursor.execute("""
       SELECT
           runs.run_id,
           runs.exp_id,
           runs.name,
           runs.run_timestamp,
           runs.completed_timestamp,
           runs.guid,
           experiments.name AS exp_name,
           experiments.sample_name
       FROM runs
       LEFT JOIN experiments ON runs.exp_id = experiments.exp_id
       WHERE runs.run_timestamp > ?        
    """, (last_time, ))
    values = cursor.fetchall()

    # Confim data is found
    if len(values) == 0:
        return None
    
    column_names = [desc[0] for desc in cursor.description]
    
    outDict = {}
    for row in values:
        outDict[row[0]] = dict(zip(column_names[1:], row[1:]))

    conn.close()
    return outDict

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
    
    cursor = conn.cursor()
    
    cursor.execute("""
      SELECT 
          completed_timestamp
      FROM runs
      WHERE guid=?
      LIMIT 1
    """, (guid, ))
    value = cursor.fetchall()
    
    conn.close()
    return value[0]
