from .plotWin import plotWidget

class plot1d(plotWidget):
    def __init__(self, 
                 *args,
                 **kargs
                 ):
        super().__init__(*args, **kargs)
        
    def initFrame(self):
        if self.df.empty:
            return
        
        self.line = self.plot.plot()
        
        self.refreshPlot()
        
        self.plot.setLabel(axis="bottom", text=f"{self.xaxis_param.label} ({self.xaxis_param.unit})")
        self.plot.setLabel(axis="left", text=f"{self.yaxis_param.label} ({self.yaxis_param.unit})")
        
        self.initalised = True
        print("Graph produced \n")
        
        
    def refreshPlot(self):
        self.line.setData(
            x=self.xaxis_data, 
            y=self.yaxis_data,
            )
        # self.vb.enableAutoRange(bool(self.rescale_refresh.isChecked())) #currently redundant
        