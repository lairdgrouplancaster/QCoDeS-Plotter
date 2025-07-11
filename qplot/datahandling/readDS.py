# -*- coding: utf-8 -*-
"""
Created on Thu Jul 10 10:57:06 2025

@author: Benjamin Wordsworth
"""
from qcodes.dataset.experiment_container import experiments
from qcodes.dataset.sqlite.queries import get_runs
from qcodes.dataset.sqlite.database import connect, get_DB_location

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
