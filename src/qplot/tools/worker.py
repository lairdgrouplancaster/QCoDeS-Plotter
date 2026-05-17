from typing import TYPE_CHECKING

from PyQt5 import QtCore

import numpy as np

from . import data2matrix

from qcodes.dataset.sqlite.database import connect

from qplot.datahandling import load_param_data_from_db
from qplot.datahandling.qcodes_cache import (
    cache_data,
    cache_database_path,
    cache_parameter_data,
    cache_read_status,
    cache_rundescriber,
    cache_table_name,
    cache_write_status,
    )
from qplot.diagnostics import log_exception


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
                 operations : list = None
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
        operations: list
            A list containing functions to perform on the refreshed data
            before returning

        """
        super().__init__()
        self.running = True
        self.emitter = _emitter() # For signals
        
        # Required working data
        self.cache = cache
        self.table_name = cache_table_name(cache)
        self.param = param
        self.param_dict = param_dict
        
        self.axes_dict = axes
        self.read_data = read_data
        self.operations = [] if operations is None else operations
        
    
    def run(self):
        try:
            cache = self.cache
            
            if self.read_data:
                conn = connect(cache_database_path(cache))
                (
                    self.updated_read_status,
                    self.updated_write_status,
                    self.cache_data
                ) = load_param_data_from_db (
                    conn,
                    self.table_name,
                    cache_rundescriber(cache),
                    self.param.name,
                    cache_write_status(cache),
                    cache_read_status(cache),
                    cache_data(cache)
                )
                conn.close()
                
                data = self.cache_data[self.param.name]
                
            else:
                data = cache_parameter_data(cache, self.param.name)
                
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
            
            else:
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
                # for >2d plots/unshaped 2d
                else:
                    (
                        axis_data,
                        axis_param,
                        dataGrid
                    ) = self.for_unshaped_2d(
                        data,
                        valid_rows,
                        depvarData
                        )

        except Exception as err: # Raise error in main thread
            log_exception("Plot worker failed", err, __name__)
            self.emitter.errorOccurred.emit(err)
            self.emitter.finished.emit(False) # False: Failed
            return

        # Allow main to fetch data
        self.axis_data = axis_data
        self.axis_param = axis_param
        if len(self.param.depends_on_) != 1:
            self.dataGrid = dataGrid

        # Run additional operations
        results = self.do_operations()

        # Update based on operations
        if results is not None:
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
        axis_dimension = {}
        valid = {}
        depvarData = np.asarray(depvarData, dtype=float)
        
        # Find correct data for each axis
        for axis in ["x", "y"]:
            name = self.axes_dict[axis]
            param = self.param_dict[name]

            param_data = np.asarray(data[name], dtype=float)
            dimension = self._shaped_axis_dimension(name, param_data, depvarData)
            param_data = self._shaped_axis_values(param_data, dimension)

            valid[axis] = np.isfinite(param_data)
            axis_data[axis] = param_data[valid[axis]]
            axis_param[axis] = param
            axis_dimension[axis] = dimension

        dataGrid = self._shaped_data_grid(
            data,
            depvarData,
            axis_dimension,
            valid,
            )
        
        return axis_data, axis_param, dataGrid


    def _shaped_axis_dimension(self, name, param_data, depvarData):
        depends_on = list(getattr(self.param, "depends_on_", ()))
        if (
                param_data.shape == depvarData.shape
                and len(depends_on) == depvarData.ndim
                and name in depends_on
                ):
            return depends_on.index(name)

        residuals = [
            self._shaped_axis_residual(param_data, dimension)
            for dimension in range(depvarData.ndim)
            ]
        return int(np.nanargmin(residuals))


    def _shaped_axis_values(self, param_data, dimension):
        moved = np.moveaxis(param_data, dimension, 0)
        rows = moved.reshape(moved.shape[0], -1)
        values = np.full(rows.shape[0], np.nan, dtype=float)

        for index, row in enumerate(rows):
            finite = np.flatnonzero(np.isfinite(row))
            if finite.size:
                values[index] = row[finite[0]]

        return values


    def _shaped_axis_residual(self, param_data, dimension):
        values = self._shaped_axis_values(param_data, dimension)
        shape = [1] * param_data.ndim
        shape[dimension] = values.size
        expected = np.broadcast_to(values.reshape(shape), param_data.shape)
        valid = np.isfinite(param_data) & np.isfinite(expected)
        if not np.any(valid):
            return np.inf

        return float(np.nanmax(np.abs(param_data[valid] - expected[valid])))


    def _shaped_data_grid(self, data, depvarData, axis_dimension, valid):
        x_dimension = axis_dimension["x"]
        y_dimension = axis_dimension["y"]

        if x_dimension == 1 and y_dimension == 0:
            return depvarData[np.ix_(valid["y"], valid["x"])]

        if x_dimension == 0 and y_dimension == 1:
            return depvarData[np.ix_(valid["x"], valid["y"])].transpose()

        valid_rows = np.isfinite(depvarData)
        for axis in ["x", "y"]:
            name = self.axes_dict[axis]
            valid_rows = valid_rows & np.isfinite(np.asarray(data[name], dtype=float))

        return self.for_unshaped_2d(data, valid_rows, depvarData)[2]
    
    
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
                axis_data["y"], 
                axis_data["x"], 
                depvarData[valid_rows]
            )
        
        # remove duplicates
        axis_data["y"] = dataGrid.index.to_numpy(float)
        axis_data["x"] = dataGrid.columns.to_numpy(float)
        
        dataGrid = dataGrid.to_numpy(float)
        
        return axis_data, axis_param, dataGrid
        
    
    def do_operations(self):
        """
        Runs through all functions in self.operations and performs those on the
        data.
        Copies data to allow a fall

        Returns
        -------
        data_dict["x"], data_dict["y"], data_dict["z"] : np.ndarray
            The updated data after all operations have been performed
        None : NoneType
            No operations to be perform or all failed.
    
        """
        operations = self.operations
        if len(operations) == 0:
            return None
        
        one_succeeded = False
        
        data_dict = {
            "x" : self.axis_data["x"].copy(),
            "y" : self.axis_data["y"].copy(),
            "z" : self.dataGrid.copy() if hasattr(self, "dataGrid") else None # Only give dataGrid if it exists
            }
        
        for func in operations:
            try:
                results = func(data_dict)
                for key in results.keys():
                    data_dict[key] = results[key]
                one_succeeded = True
                
            except Exception as err:
                log_exception("Plot operation failed", err, __name__)
                self.emitter.errorOccurred.emit(err)
                
        if one_succeeded:
            return data_dict["x"], data_dict["y"], data_dict["z"]
        else: # If all failed, go back to before operations data
            return None

        
class _emitter(QtCore.QObject):
    """
    QRunnable cannot emit signals, use of QObject can
    """
    printer = QtCore.pyqtSignal([str]) # FOR USE IN PLACE OF PRINT()
    finished = QtCore.pyqtSignal([bool]) # Callback to main to say fetch data
    errorOccurred = QtCore.pyqtSignal([Exception]) # Errors do not display in threads
    
