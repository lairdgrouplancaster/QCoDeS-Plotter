from ._base import (
    PlotTheme,
    ThemePalette,
    build_stylesheet,
    color_list,
)

_PALETTE = ThemePalette(
    window_bg="#f0f0f0",
    panel_bg="#eceef1",
    panel_alt_bg="#f6f8fb",
    field_bg="#ffffff",
    field_alt_bg="#f8f9fb",
    button_bg="#f7f8fa",
    button_hover_bg="#ffffff",
    button_pressed_bg="#e6f1ff",
    text="#111827",
    text_strong="#000000",
    muted_text="#526173",
    disabled_text="#a0a0a0",
    selection_bg="#0a84ff",
    selection_text="#ffffff",
    accent="#0a84ff",
    accent_hover="#075eb8",
    accent_soft="#dcecff",
    accent_pressed="#e6f1ff",
    border="#d4d9e2",
    border_strong="#b8bec7",
    menu_hover_bg="#e0e0e0",
    tab_bg="#f7f8fa",
    tab_selected_bg="#dcecff",
    tab_hover_bg="#eef6ff",
    table_bg="#ffffff",
    table_alt_bg="#f3f5f7",
    table_hover_bg="#dcecff",
    header_bg="#eef0f3",
    status_text="#01664d",
    progress_text="rgb(40, 40, 40)",
    splitter="#d7dbe1",
    scrollbar_bg="#eef0f3",
    scrollbar_handle="#8b929b",
    scrollbar_handle_hover="#66707c",
    danger="#c2410c",
    danger_bg="#fff7ed",
    danger_pressed_bg="#ffedd5",
)


class light(PlotTheme):
    main = build_stylesheet(_PALETTE)
    colors = color_list(["red", "green", "blue", "black", "darkcyan", "darkorange"])
    plot_background = "w"
    plot_foreground = "k"
    plot_grid = "darkgray"
