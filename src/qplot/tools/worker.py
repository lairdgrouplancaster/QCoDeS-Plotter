from typing import TYPE_CHECKING

from PyQt5 import QtCore

import numpy as np

from . import data2matrix

from qcodes.dataset.sqlite.database import connect, get_DB_location

from qplot.datahandling import load_param_data_from_db


if TYPE_CHECKING:
    import qcodes

class loader(QtCore.QRunnable):
    """
    A Worker to be placed inside a QThreadPool.
    It handles fetched data from the dataset cache and performs necessary work
    before rerending data
    
    """
    def __init__(self,
                 cache : "qcodes.dataset.data_set_cache.DataSetCacheWithDBBackend",
                 param : "qcodes.dataset.descriptions.param_spec.ParamSpec", 
                 param_dict : dict,
                 axes : dict
                 ):
        """
        Sets up worker with required data for run()
        
        Please note that self.__init__ is run in main thread, self.run() is ran
        in the worker thread.

        Parameters
        ----------
        cache_data : qcodes.dataset.cache._data
            The refreshed data from the dataset.
        param : qcodes.dataset.descriptions.param_spec.ParamSpec
            The parameter being updated.
        param_dict : dict{str: ParamSpec}
            List of all parameter data inside the dataset.
        axes : dict{str: str}
            The selected parameter for the axes.

        """
        super().__init__()
        self.running = True
        self.emitter = _emitter() # For signals
        
        # Required working data
        self.cache = cache
        self.table_name = cache._dataset.table_name # This property is an SQL check
        self.param = param
        self.param_dict = param_dict
        
        self.axes_dict = axes
        
    
    def run(self):
        try:  
            conn = connect(get_DB_location())
            cache = self.cache
            
            (
                self.updated_read_status,
                self.updated_write_status,
                self.cache_data
            ) = load_param_data_from_db (
                conn,
                self.table_name,
                cache.rundescriber,
                self.param.name,
                cache._write_status,
                cache._read_status,
                cache._data
            )
            data = self.cache_data[self.param.name]
            depvarData = data[self.param.name]
            
            axis_data = {}
            axis_param = {}
            dict_labels = list(data.keys())
            
            # for 2d plots
            if len(depvarData.shape) == 2:
                valid = {}
                
                # Find correct data for each axis
                for axis in ["x", "y"]:
                    name = self.axes_dict[axis]
                    param = self.param_dict[name]
                    
                    param_data = data[name]
                    
                    # indep data in data is in either identical rows or 
                    # columns to match size of depvar Data, so find either col 
                    # or row as need.
                    if param_data[0, 0] == param_data[0, 1] and param_data[1, 0] == param_data[1, 1]: # identical columns (double check for safety)
                        param_data = param_data[:, 0]
                        
                        # Find non nan index values
                        valid[axis] = ~np.isnan(param_data)
                        param_data = param_data[valid[axis]]
                        
                    else: # identical rows, set using column
                        param_data = param_data[0, :]
                        
                        # Find non nan index values
                        valid[axis] = ~np.isnan(param_data)
                        param_data = param_data[valid[axis]]
                        
                        if axis == "x": #rotate data to match axis data
                            depvarData = depvarData.transpose()
                    
                    axis_data[axis] = param_data
                    axis_param[axis] = param
                  
                # Allow main to fetch data
                
                # Access non nan indexed values
                self.dataGrid = depvarData[valid["x"]][:, valid["y"]]
                
                self.axis_data = axis_data
                self.axis_param = axis_param
            
                self.emitter.finished.emit(True)
                return

            #Remove nan values
            valid_rows = ~np.isnan(depvarData)

            # for 1d plots
            if len(self.param.depends_on_) == 1:
                
                x_name =  self.axes_dict["x"]
                axis_data["x"] = data[x_name][valid_rows]
                axis_param["x"] = self.param_dict[x_name]
                
                # get other value
                index = 1 if dict_labels[0] == x_name else 0
                axis_data["y"] = data[dict_labels[index]][valid_rows]
                axis_param["y"] = self.param_dict[dict_labels[index]]
                
                # Allow main to fetch data
                self.axis_data = axis_data
                self.axis_param = axis_param
                
                self.emitter.finished.emit(True)
                return
            
            # for >2d plots
            for axis in ["x", "y"]:
                # Get specific parameter
                name = self.axes_dict[axis]
                param = self.param_dict[name]
                
                # Update data
                axis_data[axis] = data[name][valid_rows]
                axis_param[axis] = param
                
            # Allow main to fetch data
            self.dataGrid = data2matrix(
                    axis_data["x"], 
                    axis_data["y"], 
                    depvarData[valid_rows]
                ).to_numpy(float)
            self.axis_data = axis_data
            self.axis_param = axis_param
            
            
            self.emitter.finished.emit(True)
            return
            
        except Exception as err: # Raise error in main thread
            self.emitter.errorOccurred.emit(err)
            self.emitter.finished.emit(False) # False: Failed
            conn.close()
            
        
class _emitter(QtCore.QObject):
    """
    QRunnable cannot emit signals, use of QObject can
    """
    printer = QtCore.pyqtSignal([str]) # FOR USE IN PLACE OF PRINT()
    finished = QtCore.pyqtSignal([bool]) # Callback to main to say fetch data
    errorOccurred = QtCore.pyqtSignal([Exception]) # Errors do not display in threads
    
    