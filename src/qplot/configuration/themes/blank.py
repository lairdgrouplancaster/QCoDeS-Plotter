import pyqtgraph as pg

class blank:
    main = ""
    
    
    @staticmethod
    def style_plotItem(plot_item):
        pg.setConfigOption('background', "default")  
        pg.setConfigOption('foreground', "default")
        
        # penCol = pg.mkPen(color="default")
        # plot_item.getAxis('bottom').setPen(penCol)
        # plot_item.getAxis('left').setPen(penCol)
        
        # plot_item.getAxis('bottom').setTextPen(penCol)
        # plot_item.getAxis('left').setTextPen(penCol)
        
        # plot_item.vb.gridPen = penCol
        
        # for item in plot_item.listDataItems():
        #     item.setPen(penCol)