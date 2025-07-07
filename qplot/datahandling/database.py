# -*- coding: utf-8 -*-
"""
Created on Sun Jul  6 19:40:32 2025

@author: Benjamin Wordsworth
"""
# from typing import TYPE_CHECKING
import qcodes

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
        
    
    
    # #Replace initial fucktion as its not very functional
    # def get_parameter_data(
    #     self,
    #     *params: str | ParamSpec | ParameterBase,
    #     start: int | None = None,
    #     end: int | None = None,
    #     callback: Callable[[float], None] | None = None,
    # ) -> ParameterData:
    #     if len(params) == 0:
    #         valid_param_names = [
    #             ps.name for ps in self._rundescriber.interdeps.non_dependencies
    #         ]
    #     else:
    #         valid_param_names = self._validate_parameters(*params)
        
    #     paramData = get_parameter_data(
    #         self.conn, self.table_name, valid_param_names, start, end, callback
    #     )
        
    #     return {para: paramData.get(para) for para in paramData}
    
    @classmethod
    def init_and_load_by_spec(cls, path, **kargs):
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
    
    # def _get_datasetprotocol_from_guid(
    #         guid: str, conn: AtomicConnection
    #         ) -> DataSetProtocol:
    #     run_id = get_runid_from_guid(conn, guid)
    #     if run_id is None:
    #         raise NameError(
    #             "No run with GUID: %s found in database: %s", guid, conn.path_to_dbfile
    #         )
    
    #     if qcodes.config.dataset.load_from_exported_file:
    #         export_info = _get_datasetprotocol_export_info(run_id=run_id, conn=conn)
    
    #         export_file_path = export_info.export_paths.get(
    #             DataExportType.NETCDF.value, None
    #         )
    
    #         if export_file_path is not None:
    #             try:
    #                 d: DataSetProtocol = load_from_file(export_file_path)
    
    #             except (ValueError, FileNotFoundError) as e:
    #                 log.warning("Cannot load data from file: %s", e)
    
    #             else:
    #                 return d
    
    #     result_table_name = _get_result_table_name_by_guid(conn, guid)
    #     if _check_if_table_found(conn, result_table_name):
    #         d = DataSet(conn=conn, run_id=run_id)
    #     else:
    #         d = DataSetInMem._load_from_db(conn=conn, guid=guid)
    
    #     return d