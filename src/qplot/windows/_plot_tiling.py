"""Shared preferences and geometry for newly opened plot windows."""

PLOT_TILE_ROWS_KEY = "user_preference.plot_tile_rows"
PLOT_TILE_COLUMNS_KEY = "user_preference.plot_tile_columns"


def plot_tile_grid(config):
    """Return the configured (rows, columns) grid."""
    return config.get(PLOT_TILE_ROWS_KEY), config.get(PLOT_TILE_COLUMNS_KEY)


def plot_tile_size(config, screenrect):
    """Size plots to their grid cells, retaining the configured frame margin."""
    rows, columns = plot_tile_grid(config)
    fraction = min(1.0, 2 * config.get("GUI.plot_frame_fraction"))
    return (
        max(1, int(fraction * screenrect.width() / columns)),
        max(1, int(fraction * screenrect.height() / rows)),
    )


def place_plot_window(owner, window):
    """Fill grid cells in row order and wrap; restart when the grid changes."""
    grid = plot_tile_grid(owner.config)
    if getattr(owner, "_plot_tile_grid", None) != grid:
        owner._plot_tile_grid = grid
        owner._next_plot_tile = 0
    rows, columns = grid
    row, column = divmod(owner._next_plot_tile, columns)
    rect = owner.screenrect
    window.move(
        rect.left() + column * rect.width() // columns,
        rect.top() + row * rect.height() // rows,
    )
    owner._next_plot_tile = (owner._next_plot_tile + 1) % (rows * columns)
