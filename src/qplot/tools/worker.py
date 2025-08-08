from typing import TYPE_CHECKING

from PyQt5 import QtCore

import numpy as np

from . import data2matrix

if TYPE_CHECKING:
    import qcodes

class loader(QtCore.QRunnable):
    def __init__(self,
                 cache_data : "qcodes.dataset.cache._data",
                 param : "qcodes.dataset.descriptions.param_spec.ParamSpec", 
                 param_dict : dict,
                 axes : dict
                 ):
        super().__init__()
        self.running = True
        self.emitter = _emitter()
        
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

               
            # for 2d plots
            if len(depvarData.shape) == 2:
                
                valid_rows = ~np.isnan(depvarData).any(axis=1)
                
                depvarData = depvarData[valid_rows]
                
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
                    
                self.dataGrid = depvarData
                self.axis_data = axis_data
                self.axis_param = axis_param
            
                self.emitter.finished.emit(True)
                return
           
            #Remove nan values
            valid_rows = ~np.isnan(depvarData)
            
            # for 1d plots
            if len(self.param.depends_on_) == 1:
                
                x_name =  self.axes_dict["x"]
                axis_data["x"] = self.data[x_name][valid_rows]
                axis_param["x"] = self.param_dict[x_name]
                
                index = 1 if dict_labels[0] == x_name else 0
                axis_data["y"] = self.data[dict_labels[index]][valid_rows]
                axis_param["y"] = self.param_dict[dict_labels[index]]
                
                self.axis_data = axis_data
                self.axis_param = axis_param
                
                self.emitter.finished.emit(True)
                return
            
            
            # for >2d plots
            for axis in ["x", "y"]:
                name = self.axes_dict[axis]
                param = self.param_dict[name]
                
                axis_data[axis] = self.data[name][valid_rows]
                axis_param[axis] = param
                
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
            self.emitter.finished.emit(False) # False - Failed
    
    
# class loader_2d(loader_1d):
#     def __init__(self, *args, **kargs):
#         super().__init__(*args, **kargs)
#         self.dataGrid = []
        
#     @QtCore.pyqtSlot()
#     def run(self):
#         super().run()
#         try:
#             self.dataGrid = data2matrix(
#                     self.axis_data["x"], 
#                     self.axis_data["y"], 
#                     self.depvarData
#                 ).to_numpy(float)
            
#             self.running = False
#             self.emitter.finished.emit(True)
            
#         except Exception as err:
#             self.emitter.errorOccurred.emit(err)
#             self.emitter.finished.emit(False)
      
        
class _emitter(QtCore.QObject):
    """
    QRunnable cannot emit signals, use of QObject can
    """
    printer = QtCore.pyqtSignal([str]) # FOR USE IN PLACE OF PRINT()
    finished = QtCore.pyqtSignal([bool]) # Callback to main to say fetch data
    errorOccurred = QtCore.pyqtSignal([Exception]) # Errors do not display in threads
    
    