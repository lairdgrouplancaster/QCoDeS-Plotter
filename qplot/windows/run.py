
from PyQt5 import QtWidgets as qtw
from qplot.windows import plot1d
from qplot.datahandling import dataset
# from pyqtgraph import GraphicsLayoutWidget
import sys

class MainWindow(qtw.QMainWindow):
    
    def __init__(self):
        super().__init__()
        
        layout = qtw.QVBoxLayout()
        
        self.textbox = qtw.QLineEdit()
        layout.addWidget(self.textbox)
        
        button1d = qtw.QPushButton("test push plot1d")
        button1d.clicked.connect(lambda checked: self.openPlot(plot1d))
        layout.addWidget(button1d)
        
        button2d = qtw.QPushButton("test push plot2d")
        # button2d.clicked.connect()
        layout.addWidget(button2d)
        
        
        
        w = qtw.QWidget()
        w.setLayout(layout)
        self.setCentralWidget(w)
        
    
    def openWin(self, widget, *args, **kargs):
        w = widget(*args, **kargs)
        w.show()
        
    
    
    def openPlot(self, widget):
        ds = self.openDataset()
        
        for param in ds.get_parameters():
            if param.depends_on != "":
                self.openWin(widget, ds, param)
        
    
    
    def openDataset(self):
        # run_id = self.textbox.text()
        run_id = 2
        ds = dataset.init_and_load_by_spec(
            "C:/Users/Benjamin Wordsworth/.qcodes/code/WN7C_first cooldown.db",
            captured_run_id=run_id
            )
        return ds


app = qtw.QApplication(sys.argv)
w = MainWindow()
w.show()
app.exec()