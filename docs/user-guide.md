# User Guide

This guide covers the main qPlot workflows after installation. For setup
problems, see [troubleshooting.md](troubleshooting.md).

## Opening a Database

1. Start qPlot with `qplot` or `python -m qplot`.
2. Drag a QCoDeS `.db` file onto the database path field, or select
   `File -> Load Database...`.
3. The main window shows database controls at the top, the run table in the
   middle, and selected-run details at the bottom.

On first launch, qPlot shows a small empty-database prompt with direct load and
quick-start actions. It disappears once a database is loaded or a load is in
progress.

The run table gives a compact view of each run, including measurements,
setpoints, start time, completion state, duration, and estimated size. The
details pane shows the selected run's overview, parameters, preview images, and
raw metadata.

## Main Window

The main window is the database and run-selection hub.

Common actions:

* `File -> Load Database...` loads a QCoDeS database.
* `File -> Load Recent Database` reopens a recently used database.
* `File -> Refresh` checks the current database for new runs.
* `File -> Open Database Folder` opens the folder containing the loaded
  database.
* `Help -> Quick Start` shows the core workflow inside qPlot.
* `Help -> Keyboard Shortcuts` shows the shortcut reference inside qPlot.
* `Help -> Copy Diagnostic Log Path` copies the log file location for support
  or troubleshooting.
* The refresh interval controls how often qPlot checks for new runs. Set it to
  `0.0 s` to disable automatic checks.
* `Auto-plot` opens newly detected runs automatically.

The selected-run preview tab can plot or export individual measurements through
double-click and context-menu actions.

## Plotting a Measurement

There are several ways to open plots:

* Double-click a preview image in the run table or selected-run preview tab.
* Right-click a run in the run table and choose a plot action.
* Enter a run ID and measurement number at the top of the main window, then
  press the plot button.
* Enter `*` as the measurement to open all plottable measurements for the
  selected run.

Parameters with one independent variable open as line plots. Parameters with
two or more independent variables open as heatmaps.

Plot windows may appear before their data has finished loading. Check the plot
window status bar; unless qPlot stops responding or shows an error, wait for the
load to complete.

## Plot Windows

Each plot window has plot controls, a status bar, optional toolbars, and dock
panels. Toolbars and dock panels can be shown or hidden from `View -> Toolbars`,
by right-clicking a toolbar or panel, or with keyboard shortcuts.

Common plot controls:

* Mouse wheel over the plot: zoom.
* Left-click drag: pan.
* Right-click: open the plot context menu.
* `Alt`/`Option` + left-click drag: draw a marquee selection.
* Drag marquee handles to resize the selection.
* Right-click inside a marquee selection for zoom and statistics actions.
* Press `Esc` or double-click the plot to clear a marquee selection.
* Double-click an X or Y axis to open its scaling dialog.
* Right-drag on the plot, or scroll over an axis, to fast scale an axis.
* The bottom toolbar shows cursor coordinates.
* The left panel controls assigned axes and plot-specific options.
* The right `Operations` panel applies data operations during refresh, after
  data is loaded from the database.

### Line Plots

Line plots support multiple compatible traces in one window. To add a trace,
drag a preview thumbnail from the run table onto an existing line plot. You can
also use the left panel.

Compatible plots are matched by independent variable name. The source plot
window for an added trace can be closed after the trace is added. Live updates
continue at the same refresh rate.

When multiple traces use different Y axes:

* Zooming or dragging in the central plot controls both axes.
* Interacting with a side axis controls that axis only.
* Secondary traces attached to the right axis cannot be rotated.

### Heatmaps

Heatmaps add color-scale controls and 1D cut extraction.

Color-scale controls:

* Right-click the plot and choose `Autoscale Color`, or use the color autoscale
  button.
* Double-click the color scale bar to open the color scaling dialog.
* Drag one color-scale handle to adjust a limit.
* Drag between handles to slide the range.
* Drag outside the handles to widen or narrow the range.

1D cut extraction:

* Right-click the heatmap and select `Horizontal Cut` or
  `Vertical Cut`.
* A cut window opens, with a cursor shown on the heatmap.
* Move the cut position with the cut window slider or by dragging the cursor on
  the heatmap.
* Hold Shift while dragging a cut cursor to move all cuts with the same
  orientation together.
* Switch the cut and fixed parameters with the `x axis` and `fixed parameter`
  dropdowns.
* Cut plots are live-data compatible.
* Cut plots can be added to compatible 1D plots when their X axis matches the
  1D plot's independent variable.

### Data Operations

Plot windows can apply operations during refresh from the `Operations` panel.
The available operations depend on the plot type.

Common operations:

* Limit maximum values.
* Limit minimum values.

Line-plot operations:

* Differentiate `dy/dx`.

Heatmap operations:

* Subtract row mean.
* Subtract column mean.
* Differentiate `dz/dx`.
* Differentiate `dz/dy`.
* Fill below.
* Fill right.

Cut-plot operations:

* Subtract cut mean.
* Subtract fixed mean.
* Differentiate cut.
* Differentiate fixed.

Select operations in the panel, drag active operations to control order, then
choose `Apply/Refresh`.

## Export

The main window can export measurement data as CSV:

* Select a run and use the CSV button.
* Right-click a preview and choose the export action.

Plot windows can export plot images and data through `File -> Export Plot...` or
`Ctrl+E`, using pyqtgraph's export dialog.

## Live Data

qPlot can display running QCoDeS experiments. The main-window refresh interval
checks for newly added runs. Each plot window has its own refresh timer for
loading new data from the database.

Use `File -> Refresh` or `R` to refresh manually. Set a refresh interval to
`0.0 s` when you want manual refresh only.

## Keyboard Shortcuts

General shortcuts:

| Shortcut | Action |
| --- | --- |
| `F1` | Show quick start help |
| `Ctrl+L` | Load a database |
| `R` | Refresh the current window |
| `Ctrl+W` / `Cmd+W` | Close the current qPlot window |
| `Ctrl+Q` / `Cmd+Q` | Quit qPlot |
| `Ctrl+M` / `Alt+Space, N` | Minimize the current window |
| `Alt+Space, X` / `Alt+Space, R` | Maximize or restore the current window on Windows |
| `Ctrl+Cmd+F` / `F11` | Enter or leave full screen |
| `Ctrl+C` / `Cmd+C` | Copy selected cells or rows in the run details pane |
| `Ctrl+Shift+C` / `Cmd+Shift+C` | Copy the current cell or value in the run details pane |
| `Shift+F10` | Open the focused widget's context menu |
| `Ctrl+Shift+D` | Open the current database folder |
| `Ctrl+Shift+M` | Bring the main window to front, or behind the graph windows |
| `Ctrl+Return` | Plot the requested run and measurement |
| `Ctrl+Shift+Return` | Plot all measurements in the selected run |
| `Ctrl+1` to `Ctrl+9` | Plot measurements 1 to 9 in the selected run |
| `Ctrl+Shift+W` | Close all plot windows |

Plot-window shortcuts:

| Shortcut | Action |
| --- | --- |
| `Ctrl+0` | Autoscale the plot view |
| `Ctrl+E` | Export the plot |
| `Ctrl+Shift+O` | Show or hide the operations panel |
| `Ctrl+Alt+R` | Show or hide the refresh toolbar |
| `Ctrl+Alt+C` | Show or hide the coordinate toolbar |
| `Ctrl+Alt+A` | Show or hide the axis control panel |
| `Ctrl+Alt+O` | Show or hide the operations dock |
| `Ctrl+Alt+S` | Snap the 1D coordinate readout to the nearest trace point |

Heatmap shortcuts:

| Shortcut | Action |
| --- | --- |
| `Ctrl+Shift+C` | Autoscale the colour range |
| `Ctrl+Shift+H` | Open a horizontal cut |
| `Ctrl+Shift+V` | Open a vertical cut |
| Arrow keys | Move the selected cut cursor by one pixel |

Dynamic context-menu entries are numbered or underlined. Once a menu is open,
press the shown number or letter to trigger that entry.
