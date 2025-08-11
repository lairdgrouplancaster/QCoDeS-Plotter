from typing import TYPE_CHECKING

from PyQt5 import QtCore

import numpy as np

from . import data2matrix

if TYPE_CHECKING:
    import qcodes

class loader(QtCore.QRunnable):
    """
    A Worker to be placed inside a QThreadPool.
    It handles fetched data from the dataset cache and performs necessary work
    before rerending data
    
    """
    def __init__(self,
                 cache_data : "qcodes.dataset.cache._data",
                 param : "qcodes.dataset.descriptions.param_spec.ParamSpec", 
                 param_dict : dict,
                 axes : dict
                 ):
        """
        Sets up worker with required data for run()

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
        self.data = cache_data[param.name]
        self.param = param
        self.param_dict = param_dict
        
        self.axes_dict = axes
        
    
    def run(self):
        try:    
            depvarData = self.data[self.param.name]
            
            axis_data = {}
            axis_param = {}
            dict_labels = list(self.data.keys())
            
            #Remove nan values
            valid_rows = ~np.isnan(depvarData)

            # for 2d plots
            if len(depvarData.shape) == 2:
                
                # convert valid_rows into form for 2d numpy arrays
                valid_rows = valid_rows.any(axis=1)
                
                depvarData = depvarData[valid_rows]
                
                # Find correct data for each axis
                for axis in ["x", "y"]:
                    name = self.axes_dict[axis]
                    param = self.param_dict[name]
                    
                    data = self.data[name]
                    
                    # indep data in self.data is in either identical rows or 
                    # columns to match size of depvar Data, so find either col 
                    # or row as need.
                    if data[0, 0] == data[0, 1]: # identical columns
                        data = data[:, 0][valid_rows]
                    else: # identical rows
                        data = data[0, :]
                        if axis == "x": #rotate data to match axis data
                            depvarData = depvarData.transpose()
                    
                    axis_data[axis] = data
                    axis_param[axis] = param
                  
                # Allow main to fetch data
                self.dataGrid = depvarData
                self.axis_data = axis_data
                self.axis_param = axis_param
            
                self.emitter.finished.emit(True)
                return
            
            # for 1d plots
            if len(self.param.depends_on_) == 1:
                
                x_name =  self.axes_dict["x"]
                axis_data["x"] = self.data[x_name][valid_rows]
                axis_param["x"] = self.param_dict[x_name]
                
                # get other value
                index = 1 if dict_labels[0] == x_name else 0
                axis_data["y"] = self.data[dict_labels[index]][valid_rows]
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
                axis_data[axis] = self.data[name][valid_rows]
                axis_param[axis] = param
                
            # Allow main to fetch data
            self.axis_data = axis_data
            self.axis_param = axis_param
            
            self.dataGrid = data2matrix(
                    self.axis_data["x"], 
                    self.axis_data["y"], 
                    depvarData[valid_rows]
                ).to_numpy(float)
            
            self.emitter.finished.emit(True)
            return
            
        except Exception as err: # Raise error in main thread
            self.emitter.errorOccurred.emit(err)
            self.emitter.finished.emit(False) # False: Failed
            
        
class _emitter(QtCore.QObject):
    """
    QRunnable cannot emit signals, use of QObject can
    """
    printer = QtCore.pyqtSignal([str]) # FOR USE IN PLACE OF PRINT()
    finished = QtCore.pyqtSignal([bool]) # Callback to main to say fetch data
    errorOccurred = QtCore.pyqtSignal([Exception]) # Errors do not display in threads
    
    