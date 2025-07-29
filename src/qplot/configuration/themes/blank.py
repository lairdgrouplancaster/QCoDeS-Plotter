import pyqtgraph as pg

class blank:
    main = ""
    colors = [pg.mkColor(col) for col in ["white", "red", "green", "blue", "cyan", "yellow"]]
    
    @classmethod
    def style_plotItem(cls, plot_win):
        pg.setConfigOption('background', "k")  
        pg.setConfigOption('foreground', "d")
        
        plot_item = plot_win.plot
        plot_win.widget.setBackground("k")
        
        for side in ['left', 'bottom', 'right', 'top']:
            axis = plot_item.getAxis(side)
            axis.setPen()
            axis.setTextPen()
        plot_item.vb.gridPen = None
        
        cls.set_line_colours(plot_item)
        
    @staticmethod
    def set_line_colours(plot_item):
        for line in plot_item.listDataItems():
            line.setPen()