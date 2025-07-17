# -*- coding: utf-8 -*-
"""
Created on Thu Jul 10 10:57:06 2025

@author: Benjamin Wordsworth
"""
from qcodes.dataset.experiment_container import experiments
from qcodes.dataset.sqlite.queries import get_runs
from qcodes.dataset.sqlite.database import connect, get_DB_location
# import time

def get_runs_via_sql():
    # startTime = time.time()
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
    # print(f"method: get_runs_via_sql, took {time.time() - startTime}s" )
    column_names = [desc[0] for desc in cursor.description]
    
    outDict = {}
    for row in cursor.fetchall():
        outDict[row[0]] = dict(zip(column_names[1:], row[1:]))
        

    conn.close()

    return outDict


def find_new_runs(last_time): #Work in progress
    # startTime = time.time()    
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
       WHERE runs.run_timestamp > ?        
    """, (last_time, ))
    # print(f"method: find_new_runs, took {time.time() - startTime}s" )
    values = cursor.fetchall()

    if len(values) == 0:
        return None
    
    column_names = [desc[0] for desc in cursor.description]
    
    outDict = {}
    for row in values:
        outDict[row[0]] = dict(zip(column_names[1:], row[1:]))

    conn.close()
    return outDict


def read_UpdatedData(guid, last_index):
    pass


#depricated
def get_runs_from_db(start: int = 0,
                     stop: int = None,
                     ) -> list:
    conn = connect(get_DB_location())
    runs = {}
    
    for exp in experiments():
        
        #Get all attributes that are not private nor contain datasets
        lst = {}
        for attr in dir(exp):
            if not ("data_set" in str(attr) or attr[0]=="_"):
                lst[attr] = eval(f"exp.{attr}")
        
        for run_id in get_runs(conn, exp_id=exp.exp_id):
            runs[int(run_id)] = lst
    
    return dict(sorted(runs.items()))
