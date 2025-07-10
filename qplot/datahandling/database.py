# -*- coding: utf-8 -*-
"""
Created on Sun Jul  6 19:40:32 2025

@author: Benjamin Wordsworth
"""
# from typing import TYPE_CHECKING
import qcodes
from os.path import abspath

from qcodes.dataset import initialise_or_create_database_at
from qcodes.dataset.data_set import (
    DataSet,
    get_guids_by_run_spec,
    generate_dataset_table,
    )

from qcodes.dataset.sqlite.queries import (
    get_parameter_data,
    get_runid_from_guid,
    _get_result_table_name_by_guid,
    _check_if_table_found,
    )
from qcodes.dataset.sqlite.database import (
    connect,
    get_DB_location,
    )



# if TYPE_CHECKING: #prevents circular import for checking types
from qcodes.dataset.data_set_protocol import ParameterData
from qcodes.dataset.descriptions.param_spec import ParamSpec
from qcodes.parameters import ParameterBase
from collections.abc import Callable

class DataSet4Plt(DataSet):
    def __init__(self, *args, **kargs):
        super().__init__(*args, **kargs)
        
    
    @classmethod
    def init_and_load_by_spec(cls, path, **kargs):
        
        if abspath(get_DB_location()) != abspath(path):
            initialise_or_create_database_at(path, journal_mode = None)
        
        internal_conn = connect(get_DB_location())
        
        
        guids = get_guids_by_run_spec(**kargs, conn=internal_conn)
        if len(guids) != 1:
            print(generate_dataset_table(guids, conn=internal_conn))
            raise NameError(
                "More than one matching dataset found. "
                "Please supply more information to uniquely"
                "identify a dataset"
            )
        
        guid = guids[0]
        run_id = get_runid_from_guid(internal_conn, guid)
        
        
        # if qcodes.config.dataset.load_from_exported_file: print("first")

        
        d =  cls(conn=internal_conn, run_id=run_id)
        

        return d

