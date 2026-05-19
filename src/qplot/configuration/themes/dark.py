from ._base import (
    PlotTheme,
    ThemePalette,
    build_stylesheet,
    color_list,
)

_PALETTE = ThemePalette(
    window_bg="#1f2026",
    panel_bg="#202127",
    panel_alt_bg="#20242a",
    field_bg="#24262d",
    field_alt_bg="#202127",
    button_bg="#24262d",
    button_hover_bg="#292d34",
    button_pressed_bg="#123b36",
    text="#c7d2e1",
    text_strong="#ffffff",
    muted_text="#aebacc",
    disabled_text="#808086",
    selection_bg="#0f766e",
    selection_text="#ffffff",
    accent="#16a085",
    accent_hover="#37efba",
    accent_soft="#183f3b",
    accent_pressed="#123b36",
    border="#343842",
    border_strong="#474c57",
    menu_hover_bg="#292d34",
    tab_bg="#24262d",
    tab_selected_bg="#0c625b",
    tab_hover_bg="#183f3b",
    table_bg="#1f2026",
    table_alt_bg="#252730",
    table_hover_bg="#24443f",
    header_bg="#2a2d35",
    status_text="#16a085",
    progress_text="rgb(240, 240, 240)",
    splitter="#343842",
    scrollbar_bg="#252730",
    scrollbar_handle="#6b7280",
    scrollbar_handle_hover="#8d96a5",
    danger="#f97316",
    danger_bg="#3a2a22",
    danger_pressed_bg="#4a2d1c",
)


class dark(PlotTheme):
    main = build_stylesheet(_PALETTE)
    colors = color_list(["red", "green", "blue", "white", "cyan", "yellow"])
    plot_background = "k"
    plot_foreground = "w"
    plot_grid = "lightgray"
