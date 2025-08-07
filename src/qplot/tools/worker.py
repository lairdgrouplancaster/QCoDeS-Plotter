from typing import TYPE_CHECKING

from PyQt5 import QtCore

from . import data2matrix

import numpy as np

if TYPE_CHECKING:
    import qcodes

class loader_1d(QtCore.QObject):
    """
    Currently in:
        ds
        param
        axis_dropdown (pass in start signal)
        
        
    Retruns:
        df (potentially removeable by only holding in worker)
        depvarData (potentially removable again - used in refresh to prevent unneeded rerun - replay with SQL query?)
        valid_data (could contain within data dicts + rework subplot to use axis_data, axis_dropdown)
        axis_data
        axis_param
        data_grid   (2d only)
        
    
    """
    start = QtCore.pyqtSignal([dict])
    _start_thread = QtCore.pyqtSignal()
    finished = QtCore.pyqtSignal()
    errorOccurred = QtCore.pyqtSignal([Exception])
    
    def __init__(self,
                 thread : QtCore.QThread,
                 ds : "qcodes.data_set.dataset",
                 param : "qcodes.dataset.descriptions.param_spec.ParamSpec", 
                 param_dict : dict
                 ):
        super().__init__()
        
        self.ds = ds
        self.param = param
        self.param_dict = param_dict        
        self.df = self.ds.cache.to_pandas_dataframe_dict()[self.param.name]
        
        self.moveToThread(thread)
        self.start.connect(self.start_load)
        
        self._start_thread.connect(thread.start)
        thread.started.connect(self.load)
        
        # self.finished.connect(thread.quit)
        
        
        
    @QtCore.pyqtSlot(dict)
    def start_load(self, axes : dict):
        try:
            self.reset_load()
        
            self.axes_dict = axes
        
            self._start_thread.emit()
        except Exception as err:
            self.errorOccurred.emit(err)
            self.reset_load()
            # self.cancel()
    
    @QtCore.pyqtSlot()
    def load(self):
        try:
            self.df = self.ds.cache.to_pandas_dataframe_dict()[self.param.name]
            self.depvarData = self.df.iloc[:,0].to_numpy(float)
            
            #get non np.nan values
            valid_rows = ~np.isnan(self.depvarData)
            indepData = self.df.index.to_frame()
            
            valid_data = []
            for itr in range(len(indepData.columns)):
                valid_data.append(indepData.iloc[:,itr].loc[valid_rows].to_numpy(float))
            
            self.depvarData = self.depvarData[valid_rows]
            
            axis_data = {}
            axis_param = {}
            
            for axis in ["x", "y"]:
                name = self.axes_dict[axis]
                param = self.param_dict[name]
                
                if not param.depends_on:
                    data = valid_data[indepData.columns.get_loc(name)]
                else:
                    data = self.depvarData
                
                axis_data[axis] = data
                axis_param[axis] = param
                
                
            self.axis_data = axis_data
            self.axis_param = axis_param
        
            #2d inherits and does more so only emit for 1d
            if self.__class__.__name__ == "loader_1d":
                self.finished.emit()
        except Exception as err:
            self.errorOccurred.emit(err)
            self.reset_load()
            # self.cancel()
    
    
    def reset_load(self):
        try: # If case of trying to fetch aborted data
            self.axes_dict = None
            
            self.axis_data = None
            self.axis_param = None
            
        except Exception as err:
            self.errorOccurred.emit(err)
            # self.cancel()
    
    
class loader_2d(loader_1d):
    def __init__(self, *args, **kargs):
        super().__init__(*args, **kargs)
        self.dataGrid = []
        
    @QtCore.pyqtSlot()
    def load(self):
        super().load()
        try:
            self.dataGrid = data2matrix(
                    self.axis_data["x"], 
                    self.axis_data["y"], 
                    self.depvarData
                ).to_numpy(float)
            
            self.finished.emit()
        except Exception as err:
            self.errorOccurred.emit(err)
            self.reset_load()
      
    @QtCore.pyqtSlot()
    def reset_load(self):
        super().reset_load()
        self.dataGrid = []