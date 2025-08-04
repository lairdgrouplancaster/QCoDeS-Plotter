# -*- coding: utf-8 -*-
"""
Created on Thu Jul 10 10:57:06 2025

@author: Benjamin Wordsworth
"""
from qcodes.dataset.sqlite.database import connect, get_DB_location
# import time

def get_runs_via_sql():
    conn = connect(get_DB_location())
    
    cursor = conn.cursor()
    
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
    
    outDict = {}
    for row in cursor.fetchall():
        outDict[row[0]] = dict(zip(column_names[1:], row[1:]))
        

    conn.close()

    return outDict


def find_new_runs(last_time):
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

    if len(values) == 0:
        return None
    
    column_names = [desc[0] for desc in cursor.description]
    
    outDict = {}
    for row in values:
        outDict[row[0]] = dict(zip(column_names[1:], row[1:]))

    conn.close()
    return outDict

def has_finished(guid):
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
