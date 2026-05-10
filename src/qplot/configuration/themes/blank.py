import pyqtgraph as pg

class blank:
    main = ""
    colors = [pg.mkColor(col) for col in ["red", "green", "blue", "black", "darkcyan", "darkorange"]]
    
    @classmethod
    def style_plotItem(cls, plot_win):
        pg.setConfigOption('background', "w")
        pg.setConfigOption('foreground', "k")
        
        plot_item = plot_win.plot
        plot_win.widget.setBackground("w")

        pen = pg.mkPen("k")
        
        for side in ['left', 'bottom', 'right', 'top']:
            axis = plot_item.getAxis(side)
            axis.setPen(pen)
            axis.setTextPen(pen)
        plot_item.vb.gridPen = pg.mkPen(color='darkgray')
        
        cls.set_line_colours(plot_item)
        
    @classmethod
    def set_line_colours(cls, plot_item):
        for itr, line in enumerate(plot_item.listDataItems()):
            index = itr % len(cls.colors)
            line.setPen(pg.mkPen(color=cls.colors[index]))
