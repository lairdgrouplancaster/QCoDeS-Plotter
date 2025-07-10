
from PyQt5 import QtWidgets as qtw
from PyQt5 import QtCore

from qplot.windows import plot1d, plot2d
from qplot.datahandling import dataset
# from qplot.windows.setup import closeEvent
# from pyqtgraph import GraphicsLayoutWidget
import os


class MainWindow(qtw.QMainWindow):
    
    def __init__(self):
        super().__init__()
       
        #prevent auto delete of windows
        self.windows = []
        
        layout = qtw.QVBoxLayout()
        
        layout.addWidget(qtw.QLabel("File Directory:"))
        
        self.fileTextbox = qtw.QLineEdit("C:/Users/Benjamin Wordsworth/.qcodes/code/WN7C_first cooldown.db")
        self.fileTextbox.setReadOnly(True)
        layout.addWidget(self.fileTextbox)
        
        fileButton = qtw.QPushButton("Choose File Location")
        fileButton.clicked.connect(self.getfile)
        layout.addWidget(fileButton)
        
        
        layout.addWidget(qtw.QLabel("Run id:"))
        
        self.pltTextbox = qtw.QLineEdit()
        layout.addWidget(self.pltTextbox)
        
        pltbutton = qtw.QPushButton("Open Plots")
        pltbutton.clicked.connect(self.openPlot)
        layout.addWidget(pltbutton)
        
        w = qtw.QWidget()
        w.setLayout(layout)
        self.setCentralWidget(w)
       
        #bring window to top
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowStaysOnTopHint)
        self.show() 
        self.setWindowFlags(self.windowFlags() & ~QtCore.Qt.WindowStaysOnTopHint) 
        self.show()
        
    def closeEvent(self, event):
        qtw.QApplication.closeAllWindows()
    
    
    def openWin(self, widget, *args, **kargs):
        win = widget(*args, **kargs)
        self.windows.append(win)
        win.sig.connect(self.onClose)
        win.show()

    @QtCore.pyqtSlot(object)
    def onClose(self, win):
        self.windows.remove(win)
        # print(f"Closed {str(win)}, remaining: {self.windows}")
        
    
    def openPlot(self):
        ds = self.openDataset()
        
        for param in ds.get_parameters():
            if param.depends_on != "":
                depends_on = param.depends_on.split(", ")
                if len(depends_on) == 1:
                    self.openWin(plot1d, ds, param)
                elif len(depends_on) == 2:
                    self.openWin(plot2d, ds, param)
                else:
                    raise IndexError(
                        f"Parameter: {param.name}, depends on too many variables ({depends_on}, {len(depends_on)=})"
                        )
        
    def getfile(self):
      filename = qtw.QFileDialog.getOpenFileName(self, 'Open file', 
         os.getcwd(),"Data Base File (*.db)")[0]
      print(filename)
      self.fileTextbox.setText(filename)
    
    def openDataset(self):
        run_id = int(self.pltTextbox.text())
        ds = dataset.init_and_load_by_spec(
            self.fileTextbox.text(),
            captured_run_id=run_id
            )
        return ds
