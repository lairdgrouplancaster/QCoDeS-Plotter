from pyqtgraph import PlotDataItem


class subplot1d(PlotDataItem):
    def __init__(self, parent, window, *args, **kargs):
        super().__init__(*args, **kargs)
        
        self.init_dynamic(parent, window)
        
        
    def init_dynamic(self, parent, window):
        self.label = window.label
        self.param_dict = window.param_dict
        self.df = window.df
        
        self.parent = parent
        self.window = window
        
        self.refresh()
        
        parent.plot.addItem(self)
            
            
    def refresh(self):
        
        parent = self.parent
        window = self.window
        
        data = {}
        
        
        if self.df.empty:
            data["x"] = []
            data["y"] = []
        
        else:
            indepDataNames = self.df.index.names

            for axis in ["x", "y"]:
                name = parent.axis_dropdown[axis].currentText()
                
                
                if self.param_dict.get(name, 0):
                    param = self.param_dict.get(name)
                else:
                    param = parent.param_dict.get(name)
                    
                if not param.depends_on:
                    data[axis] = window.valid_data[indepDataNames.index(name)]
                    
                else:
                    data[axis] = window.depvarData #ignore error, is used in exec below
                
        self.setData(
            x=data["x"], 
            y=data["y"],
            )
        # parent.vb.enableAutoRange(bool(self.rescale_refresh.isChecked())) #currently redundant