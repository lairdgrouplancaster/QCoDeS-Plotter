import pyqtgraph as pg

from ._base import PlotTheme, color_list


class blank(PlotTheme):
    main = ""
    colors = color_list(["red", "green", "blue", "black", "darkcyan", "darkorange"])
    plot_background = "w"
    plot_foreground = "k"
    plot_grid = "darkgray"
    
    @classmethod
    def style_plotItem(cls, plot_win):
        pg.setConfigOption('background', "w")
        pg.setConfigOption('foreground', "k")
        super().style_plotItem(plot_win)
