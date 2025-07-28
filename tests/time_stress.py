from qplot import config

from qplot.windows import (
    plot1d,
    plot2d,
    MainWindow
    )

from time import time
import numpy as np

from PyQt5 import (
    QtWidgets as qtw,
    QtCore
    )

from qcodes.dataset import load_by_guid

import sys
from os.path import join

import csv

class test2d(plot2d):
    
    timer = QtCore.pyqtSignal([object, float, int])
    
    def refreshWindow(self, force : bool = False):
        
        start_time = time()
        
        super().refreshWindow()
        
        refresh_time = time() - start_time
        
        current_length = len(self.depvarData)
        
        if current_length != self.last_df_len:
            self.timer.emit(self, refresh_time, current_length)
    
        
class testMain(MainWindow):
    
    @QtCore.pyqtSlot(str)
    def openPlot(self, guid : str=None):
        if not self.ds:
            ds = load_by_guid(guid)
        elif guid and self.ds.guid != guid:
            ds = load_by_guid(guid)
        else:
            ds = self.ds
            
        for param in ds.get_parameters():
            if param.depends_on != "":
                depends_on = param.depends_on_
                if len(depends_on) == 1:
                    self.openWin(plot1d, ds, param, self.config, refrate = self.spinBox.value())
                else:
                    self.openWin(test2d, ds, param, self.config, refrate = self.spinBox.value())
                    
                self.windows[-1].timer.connect(save_time_log)
                
                
                
                
@QtCore.pyqtSlot(object, float, int)
def save_time_log(win, run_time, data_length):
    global conf
    print("writing")
    with open(join(conf.default_path, f"{win.ds.run_id} {win.ds.name}.csv"), 'a', newline='') as file:
        writer = csv.writer(file)
        writer.writerow([data_length, run_time])
        
        
if __name__=="__main__":
    conf = config()
    app = qtw.QApplication(sys.argv)
    w = testMain()
    app.exec()
