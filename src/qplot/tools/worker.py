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
                 axes : dict,
                 read_data : bool = True,
                 operations : dict = {}
                 ):
        """
        Sets up worker with required data for run()
        
        Please note that self.__init__ is run in main thread, self.run() is ran
        in the worker thread.

        Parameters
        ----------
        cache : qcodes.dataset.data_set_cache.DataSetCacheWithDBBackend
            The cache for the dataset that is being refreshed.
        param : qcodes.dataset.descriptions.param_spec.ParamSpec
            The parameter being updated.
        param_dict : dict{str: ParamSpec}
            List of all parameter data inside the dataset.
        axes : dict{str: str}
            The selected parameter for the axes.
        read_data : bool
            Whether to read the database for new data or use current data.
            The default is True.
        operations: dict{str, callable}
            A dictionary containing functions to perform on the refreshed data
            before returning

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
        self.read_data = read_data
        self.operations = operations
        
    
    def run(self):
        try:
            did_error = False
            cache = self.cache
            
            if self.read_data:
                conn = connect(get_DB_location())
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
                conn.close()
                
                data = self.cache_data[self.param.name]
                
            else:
                data = cache._data[self.param.name]
                
            depvarData = data[self.param.name]
            
            # for shaped 2d plots
            if len(depvarData.shape) == 2:
                (
                    axis_data,
                    axis_param,
                    dataGrid
                ) = self.for_shaped_2d(
                    data,
                    depvarData
                    )
                return

            #Remove nan values
            valid_rows = ~np.isnan(depvarData)

            # for 1d plots
            if len(self.param.depends_on_) == 1:
                (
                    axis_data,
                    axis_param
                ) = self.for_1d(
                    data,
                    valid_rows
                    )
                return
            
            # for >2d plots/unshaped 2d
            (
                axis_data,
                axis_param,
                dataGrid
            ) = self.for_unshaped_2d(
                data,
                valid_rows,
                depvarData
                )
            return
            
        
        except Exception as err: # Raise error in main thread
            did_error = True
            self.emitter.errorOccurred.emit(err)
            self.emitter.finished.emit(False) # False: Failed
            
        finally:
            if did_error: # errored out
                return
            
            # Allow main to fetch data
            self.axis_data = axis_data
            self.axis_param = axis_param
            if len(self.param.depends_on_) != 1:
                self.dataGrid = dataGrid
              
            # Run additional operations
            results = do_operations(
                self.operations,
                self.axis_data["x"],
                self.axis_data["y"],
                self.dataGrid if hasattr(self, "dataGrid") else None # Only give dataGrid if it exists
                )
            
            # If an operation failed, raise error, data should be complete to 
            # refresh without operations, so finished stays true
            if isinstance(results, Exception):
                self.emitter.errorOccurred.emit(results)
                
            # Update based on operations
            elif results is not None:
                (
                    self.axis_data["x"],
                    self.axis_data["y"]
                ) = results[:2]
                if hasattr(self, "dataGrid"):
                    self.dataGrid = results[2]
            
            # Callback
            self.emitter.finished.emit(True)
            
   
            
    def for_1d(self, data, valid_rows):
        axis_data = {}
        axis_param = {}
        dict_labels = list(data.keys())
        
        x_name =  self.axes_dict["x"]
        axis_data["x"] = data[x_name][valid_rows]
        axis_param["x"] = self.param_dict[x_name]
        
        # get other value
        index = 1 if dict_labels[0] == x_name else 0
        axis_data["y"] = data[dict_labels[index]][valid_rows]
        axis_param["y"] = self.param_dict[dict_labels[index]]
        
        return axis_data, axis_param
        
    
    def for_shaped_2d(self, data, depvarData):
        axis_data = {}
        axis_param = {}
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
          
        # Access non nan indexed values
        dataGrid = depvarData[valid["x"]][:, valid["y"]]
        
        return axis_data, axis_param, dataGrid
    
    
    def for_unshaped_2d(self, data, valid_rows, depvarData):
        axis_data = {}
        axis_param = {}
        for axis in ["x", "y"]:
            # Get specific parameter
            name = self.axes_dict[axis]
            param = self.param_dict[name]
            
            # Update data
            axis_data[axis] = data[name][valid_rows]
            axis_param[axis] = param
            
        dataGrid = data2matrix(
                axis_data["x"], 
                axis_data["y"], 
                depvarData[valid_rows]
            )
        
        # remove duplicates
        axis_data["x"] = dataGrid.index.to_numpy(float)
        axis_data["y"] = dataGrid.columns.to_numpy(float)
        
        dataGrid = dataGrid.to_numpy(float)
        
        return axis_data, axis_param, dataGrid
        
        
        
class _emitter(QtCore.QObject):
    """
    QRunnable cannot emit signals, use of QObject can
    """
    printer = QtCore.pyqtSignal([str]) # FOR USE IN PLACE OF PRINT()
    finished = QtCore.pyqtSignal([bool]) # Callback to main to say fetch data
    errorOccurred = QtCore.pyqtSignal([Exception]) # Errors do not display in threads
    

def do_operations(operations : dict, x, y, z):
    """
    Runs through all functions in operations and performs those on the data.

    Parameters
    ----------
    operations : dict{str: callable}
        Contains function to perform on the data. Will perform them in order added.
    x : np.array
        x data array.
    y : np.array
        y data array.
    z : np.ndarray | None
        z data array. None if 1d.

    Returns
    -------
    data_dict["x"], data_dict["y"], data_dict["z"] : np.ndarray
        The updated data after all operations have been performed
    None : NoneType
        No operations to be perform
    err : Exception
        An error occured in operation, return to worker to emit error.

    """
    try:
        if len(operations) == 0:
            return None
        
        data_dict = {
            "x" : x.copy(),
            "y" : y.copy(),
            "z" : z.copy() if z is not None else None
            }
        
        for func in operations.values():
            results = func(data_dict)
            for key in results.keys():
                data_dict[key] = results[key]
        
        return data_dict["x"], data_dict["y"], data_dict["z"]

    except Exception as err:
        return err