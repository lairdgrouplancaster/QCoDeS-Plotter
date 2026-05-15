# Architecture

This document is a working map of the current codebase. It is intentionally
short: update it when module responsibilities move.

## Entry Points

`src/qplot/__main__.py` defines the `qplot` command and `qplot.run()`. It creates
the `QApplication`, opens `MainWindow`, and starts the Qt event loop.

`src/qplot/__init__.py` exposes the public package imports used by scripts and
interactive users.

## Main Window

`src/qplot/windows/main.py` owns the top-level application window. It handles:

* loading and remembering QCoDeS database paths
* refreshing the run list
* opening 1D and 2D plot windows
* exporting measurement data
* application menus, shortcuts, themes, and global status messages

When adding a new top-level command, menu action, or workflow that coordinates
multiple windows, start here.

## Plot Windows

`src/qplot/windows/_plotWin.py` is the shared base for plot windows. It owns
common plotting behavior such as refresh timers, worker loading, axis controls,
context menus, export handling, operation panels, marquee selection, and status
or error reporting.

`src/qplot/windows/plot1d.py` extends the shared plot window for line plots. It
owns 1D-specific trace handling, secondary axes, snap-to-trace behavior, and
line-plot marquee statistics.

`src/qplot/windows/plot2d.py` extends the shared plot window for heatmaps. It
owns 2D-specific colorbar controls, color map filtering, hover pixel display,
marquee color scaling, and 1D cut extraction.

Use the shared base only for behavior that should apply to both line plots and
heatmaps. Keep plot-type-specific interaction details in `plot1d.py` or
`plot2d.py`.

## Main Window Widgets

`src/qplot/windows/_widgets/treeWidgets.py` contains the run table, run details
tabs, copyable metadata tables, formatting helpers, and delegates used by the
main window.

`src/qplot/windows/_widgets/preview.py` creates and renders run preview
thumbnails. It also handles preview selection, drag payloads, and background
preview generation.

`src/qplot/windows/_widgets/operations.py` defines the operation panel widgets
that collect user-selected data operations before refresh processing.

`src/qplot/windows/_widgets/dropbox.py` and `toolbar.py` contain smaller
reusable UI controls used inside plot windows.

## Data Loading

`src/qplot/datahandling/readSQL.py` reads run metadata directly from the current
QCoDeS SQLite database. It also computes summary fields used by the run table,
including status, point counts, and storage size estimates.

`src/qplot/datahandling/database.py` contains database-file access helpers,
cloud-storage hydration, background main-window load workers, and database
diagnostic report generation.

`src/qplot/datahandling/LoadFromDB.py` adapts QCoDeS database loading for
threaded refreshes.

`src/qplot/datahandling/qcodes_cache.py` is the compatibility boundary for
QCoDeS cache internals used by per-parameter refreshes. Prefer adding cache
private-attribute access there instead of spreading it through GUI modules.

`src/qplot/tools/worker.py` defines the background loader used by plot windows.
It loads data, reshapes it for the plot type, applies selected operations, and
emits results back to the GUI thread.

`src/qplot/tools/general.py` and `plot_tools.py` contain small data helpers and
plot operation functions. `src/qplot/tools/operation_registry.py` maps those
operation functions to the plot-window surfaces and input controls that expose
them.

## Configuration

`src/qplot/configuration/config.py` loads, validates, updates, and resets
`~/.qplot/config.json` using `config_schema.json`.

`src/qplot/configuration/scripts.py` backs the `qplot-cfg` command-line helper.

Theme files live in `src/qplot/configuration/themes`.

The user-facing key reference and contributor checklist for config changes live
in `docs/configuration.md`.

## Tests

Tests are grouped by area:

* `tests/datahandling` covers database metadata helpers.
* `tests/widgets` covers main-window widgets and preview behavior.
* `tests/windows` covers main-window and plot-window behavior.
* `tests/test_config.py` and `tests/test_tools.py` cover configuration and
  general helper behavior.

Shared pytest and Qt setup lives in `tests/conftest.py`.
