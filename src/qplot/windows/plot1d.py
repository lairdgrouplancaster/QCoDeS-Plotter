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
            print("df empty")
            return
        print("Working")
        
        indepParam = unpack_param(self.ds, self.param.depends_on)
        
        indepData = self.indepData[0]
        
        self.line = self.plot.plot()
        
        self.line.setData(x=indepData, y=self.depvarData)
        
        self.plot.setLabel(axis="bottom", text=f"{indepParam.label} ({indepParam.unit})")
        self.plot.setLabel(axis="left", text=f"{self.param.label} ({self.param.unit})")
        
        self.initLabels()
        self.initContextMenu()
        
        self.initalised = True
        print("Graph produced \n")
        
        
    def refreshPlot(self):
        indepData = self.indepData[0]
        self.line.setData(
            x=indepData, 
            y=self.depvarData,
            )
        # self.plot.disableAutoRange
        