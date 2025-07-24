import pyqtgraph as pg

class blank:
    main = ""
    
    
    @staticmethod
    def style_plotItem(plot_win):
        pg.setConfigOption('background', "k")  
        pg.setConfigOption('foreground', "d")
        
        plot_item = plot_win.plot
        plot_win.widget.setBackground("k")
        
        for side in ['left', 'bottom', 'right', 'top']:
            axis = plot_item.getAxis(side)
            axis.setPen()
            axis.setTextPen()
        plot_item.vb.gridPen = None
        
        for line in plot_item.listDataItems():
            line.setPen()