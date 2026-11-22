import pyqtgraph as pg

from ._base import AXIS_PANEL_STYLESHEET, PlotTheme, color_list


class blank(PlotTheme):
    main = AXIS_PANEL_STYLESHEET.substitute(
        min_height="0px",
        padding_y="1px",
        window_bg="palette(window)",
        panel_bg="palette(button)",
        muted_text="palette(button-text)",
        tab_hover_bg="palette(light)",
        text_strong="palette(window-text)",
        accent="palette(highlight)",
        border="palette(midlight)",
        border_strong="palette(mid)",
        disabled_text="palette(mid)",
    )
    colors = color_list(["red", "green", "blue", "black", "darkcyan", "darkorange"])
    plot_background = "w"
    plot_foreground = "k"
    plot_grid = "darkgray"
    
    @classmethod
    def style_plotItem(cls, plot_win):
        pg.setConfigOption('background', "w")
        pg.setConfigOption('foreground', "k")
        super().style_plotItem(plot_win)
