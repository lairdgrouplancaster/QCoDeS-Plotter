QCoDeS-Plotter
==============

QCoDeS-Plotter is a PyQt-based data viewer for QCoDeS databases. It is designed
to inspect both completed and running experiments, with live refresh, line plots,
heatmaps, 1D cut extraction, and simple data operations.

> [!IMPORTANT]
> Plot windows may appear before their data has finished loading. Check the
> status bar at the bottom of the window; unless the app stops responding or an
> error appears, wait for loading to complete.

Requirements
------------

QCoDeS-Plotter requires Python 3.11 or newer.

The core runtime dependencies are installed automatically:

* QCoDeS
* PyQt5
* pyqtgraph
* numpy
* pandas
* jsonschema

Installation
------------

Using a virtual environment is recommended. Choose one of the two installation
methods below.

### Option 1: For users

Use this if you want to install the current GitHub version without editing the source code. This requires Git to be installed.

#### Windows

```console
py -3.11 -m venv .venv
.venv\Scripts\activate
python -m pip install -U pip
python -m pip install git+https://github.com/lairdgrouplancaster/QCoDeS-Plotter.git@main
```

#### Mac

```console
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install git+https://github.com/lairdgrouplancaster/QCoDeS-Plotter.git@main
```

### Option 2: For editors

Use this if you want to clone this repository and edit the source code in `src/qplot/` in a way that will be reflected when you restart the app.

#### Windows

```console
git clone https://github.com/lairdgrouplancaster/QCoDeS-Plotter.git
cd QCoDeS-Plotter
py -3.11 -m venv .venv
.venv\Scripts\activate
python -m pip install -U pip
python -m pip install -e .
```

#### Mac

```console
git clone https://github.com/lairdgrouplancaster/QCoDeS-Plotter.git
cd QCoDeS-Plotter
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e .
```

Running The App
---------------

After installation, run:

```console
qplot
```

You can also start it from Python:

```python
import qplot

qplot.run()
```

Opening A Database
------------------

1. Open the app.
2. Drag a QCoDeS `.db` file onto the database path field, or select
   `File -> Load Database...`.
3. You will see controls at the top, a run table in the middle, and information tabs on the bottom. The run table gives basic information about each run. The information tabs give more detail, including the full nested key-value structure created by QCoDeS.

Refresh And Live Data
---------------------
You can plot live runs from QCoDeS. The refresh interval in the top toolbar controls how often the main window checks the current database for new runs. Setting it to `0.0` disables automatic
checks.

Use `File -> Refresh` or press `R` to refresh manually.

`Auto-plot` opens newly detected runs automatically.

Plotting A Measurement
----------------------
There are three ways to do this:
1. Double-click on its preview in the run table.
2. Right-click on the run table, and use the pop-up.
3. Set ID and measurement at the top of the window and press the plot button.

You can also use this window to export measurements as CSV, either using the save button or by right-clicking on a preview.

Plot Windows
------------

The plot window depends on what kind of data you have in your measurement:

* Parameters with one independent variable open as line plots.
* Parameters with two or more independent variables open as heatmaps.

Each plot window has toolbars or dock panels that can be shown or hidden from
`View -> Toolbars`, by right-clicking a toolbar/panel, or with keyboard
shortcuts.

Common plot controls:

* Mouse wheel over the plot: zoom.
* Left-click drag: pan.
* Right-click: open the plot context menu, including `Autoscale` and `Autoscale color`
* `Alt`/`Option` + left-click drag: draw a marquee selection. Drag its handles to resize it, right-click inside it for zoom and statistics actions, and press `Esc` or double-click the plot to clear it.
* Double-click an X or Y axis, or the colour scale bar: open its scaling dialog.
* To fast scale the X or Y axis:
  - Right-drag on the plot
  - Scroll over the axis
* The bottom toolbar shows cursor coordinates.
* The left panel controls assigned axes and plot-specific options.
* The right `Operations` panel applies data operations during refresh, after data is loaded from the database.

Line Plots
----------
Line plots support multiple compatible traces in one window. To add a trace, drag the preview thumbnail from the run table onto an existing line plot. You can also use the left panel. Compatible plots are matched by independent variable name.

The source window for an added line can be closed after the line is added. Live updates continue at the same refresh rate.

When multiple lines use different y axes:

* Zooming or dragging in the central plot controls both axes.
* Interacting with a side axis controls that axis only.
* Secondary lines attached to the right axis cannot be rotated.

Heatmaps
--------

* To fast scale the color bar:
  - Drag one handle to adjust its limits
  - Drag between handles to slide the range
  - Drag outside the handles to widen/narrow the range.

Heatmaps support 1D cut extraction:

* Right-click the heatmap and select `Plot Horizontal Cut` or
  `Plot Vertical Cut`.
* A cut window opens, with a cursor shown on the heatmap.
* Move the cut position with the cut window slider or by dragging the cursor
  on the heatmap.
* Switch the cut and fixed parameters with the `x axis` and `fixed parameter` dropdowns.
* Cut plots are live-data compatible.
* Cut plots can be added to compatible 1D plots when their x axis matches the 1D plot's independent variable.

Keyboard Shortcuts
------------------

General shortcuts:

| Shortcut | Action |
| --- | --- |
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

Dynamic context menu entries are numbered or underlined. Once the menu is open,
press the shown number or letter to trigger that entry.

Configuration
-------------

On first run, QCoDeS-Plotter creates a configuration file at:

```text
~/.qplot/config.json
```

Show available config commands:

```console
qplot-cfg -info
```

Show help for a specific command:

```console
qplot-cfg -info dump
```

Print the current config:

```python
from qplot import config

config().dump()
```

```console
qplot-cfg -dump
```

Update a config value:

```python
from qplot import config

config().update("file.default_load_path", r"C:\Users\<user>\Desktop")
```

```console
qplot-cfg -set_value file.default_load_path "C:\Users\<user>\Desktop"
```

Use quotes around terminal values that contain spaces.

Reset config to defaults:

```python
from qplot import config

config().reset_to_defaults()
```

```console
qplot-cfg -reset
```

Development
-----------

For local development, use the project virtual environment and install in
editable mode.

Windows:

```console
.venv-win\Scripts\python.exe -m pip install -e .
```

macOS/Linux:

```console
.venv/bin/python -m pip install -e .
```

Run the current non-GUI tests:

Windows:

```console
.venv-win\Scripts\python.exe -m pytest
```

macOS/Linux:

```console
.venv/bin/python -m pytest
```

Run the manual GUI smoke script:

Windows:

```console
.venv-win\Scripts\python.exe tests/test.py
```

macOS/Linux:

```console
.venv/bin/python tests/test.py
```
