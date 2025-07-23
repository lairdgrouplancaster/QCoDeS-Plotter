from qplot.tools import unpack_param

from .plotWin import plotWidget

class plot1d(plotWidget):
    def __init__(self, 
                 *args,
                 refrate = None,
                 **kargs
                 ):
        super().__init__(*args, **kargs)
        
        self.initFrame()
        self.initRefresh(refrate)
        
    def initFrame(self):
        if self.df.empty:
            return
        print("Working")
        
        self.initLabels()
        self.initContextMenu()
        
        # indepParam = unpack_param(self.ds, self.param.depends_on)
        
        # indepData = self.indepData[0]
        
        self.line = self.plot.plot()
        
        # self.line.setData(x=self.xaxis_data, y=self.yaxis_data)
        self.refreshPlot()
        
        self.plot.setLabel(axis="bottom", text=f"{self.xaxis_param.label} ({self.xaxis_param.unit})")
        self.plot.setLabel(axis="left", text=f"{self.yaxis_param.label} ({self.yaxis_param.unit})")
        
        
        self.initalised = True
        print("Graph produced \n")
        
        
    def refreshPlot(self):
        # indepData = self.indepData[0]
        self.line.setData(
            x=self.xaxis_data, 
            y=self.yaxis_data,
            )
        