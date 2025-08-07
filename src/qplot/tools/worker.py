from typing import TYPE_CHECKING

from PyQt5 import QtCore

from qcodes.dataset import load_by_guid
from qcodes.dataset.sqlite.database import connect, get_DB_location

from . import data2matrix

import numpy as np

if TYPE_CHECKING:
    import qcodes

class loader_1d(QtCore.QRunnable):
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
    
    def __init__(self,
                 guid : str,
                 param : "qcodes.dataset.descriptions.param_spec.ParamSpec", 
                 param_dict : dict,
                 axes : dict
                 ):
        super().__init__()
        self.running = True
        self.emitter = _emitter()
        
        self.guid = guid
        self.param = param
        self.param_dict = param_dict
        
        self.axes_dict = axes
        
    
    def run(self):
        try:    
            # SQLite is not thread safe, so requires new conn. Settled for making 
            # ds object to ensure safety at the cost of some speed. Responsiveness 
            # should not be lost.
            self.ds = load_by_guid(self.guid)
            
            
            self.emitter.printer.emit("Running")
            self.df = self.ds.to_pandas_dataframe_dict()[self.param.name]
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
                self.running = False
                self.emitter.finished.emit(True)
                
        except Exception as err:
            self.emitter.errorOccurred.emit(err)
            self.emitter.finished.emit(False)
    
    
class loader_2d(loader_1d):
    def __init__(self, *args, **kargs):
        super().__init__(*args, **kargs)
        self.dataGrid = []
        
    @QtCore.pyqtSlot()
    def run(self):
        super().run()
        try:
            self.dataGrid = data2matrix(
                    self.axis_data["x"], 
                    self.axis_data["y"], 
                    self.depvarData
                ).to_numpy(float)
            
            self.running = False
            self.emitter.finished.emit(True)
            
        except Exception as err:
            self.emitter.errorOccurred.emit(err)
            self.emitter.finished.emit(False)
      
        
class _emitter(QtCore.QObject):
    """
    QRunnable cannot emit signals, use of QObject can
    """
    printer = QtCore.pyqtSignal([str])
    finished = QtCore.pyqtSignal([bool])
    errorOccurred = QtCore.pyqtSignal([Exception])
    
    