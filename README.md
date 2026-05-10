QCoDeS-Plotter
==============

QCoDeS-Plotter is a PyQt-based data viewer for QCoDeS databases. It is designed
to inspect both completed and running experiments, with live refresh, line plots,
heatmaps, sweep extraction, and simple data operations.

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

On Windows, replace:

```console
source .venv/bin/activate
```

with:

```console
.venv\Scripts\activate
```

### Option 1: Install From GitHub

Use this if you want to install the current GitHub version without editing the
source code.

```console
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install git+https://github.com/lairdgrouplancaster/QCoDeS-Plotter.git@main
```

This requires Git to be installed.

### Option 2: Install A Local Checkout

Use this if you have cloned this repository and plan to edit the source code in `src/qplot/` in a way that will be reflected when you restart the app.

```console
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
2. Select `File -> Load`.
3. Choose a QCoDeS `.db` database file.
4. Select a run from the central table.
5. Open plots with `Open Plots`, by double-clicking the run, or from the run
   table context menu.

The run table can be sorted by clicking a column header. Selecting a run shows
more detail in the lower table, including data structure, metadata, and snapshot
information where available.

Refresh And Live Data
---------------------

The refresh interval in the top toolbar controls how often the main window
checks the current database for new runs. Setting it to `0.0` disables automatic
checks.

Use `File -> Refresh` or press `R` to refresh manually.

`Toggle Auto-plot` opens newly detected runs automatically.

Plot Windows
------------

Plot windows are created for dependent parameters:

* Parameters with one independent variable open as line plots.
* Parameters with two or more independent variables open as heatmaps.

Each plot window has toolbars or dock panels that can be shown or hidden from
`View -> Toolbars`, by right-clicking a toolbar/panel, or with keyboard
shortcuts.

Common plot controls:

* Mouse wheel over the plot: zoom.
* Left-click drag: pan.
* Right-click drag: scale the view.
* Right-click the plot: open the plot context menu.
* `Autoscale`: reset the plot view.
* The bottom toolbar shows cursor coordinates.
* The left panel controls assigned axes and plot-specific options.
* The right `Operations` panel applies data operations during refresh, after
  data is loaded from the database.

Line Plots
----------

Line plots support multiple compatible traces in one window.

Use the left panel or the run table `Add _ to _` context menu to add another
line to an existing plot. Compatible plots are matched by independent variable
name.

The source window for an added line can be closed after the line is added. Live
updates continue at the same refresh rate.

When multiple lines use different y axes:

* Zooming or dragging in the central plot controls both axes.
* Interacting with a side axis controls that axis only.
* Secondary lines attached to the right axis cannot be rotated.

Heatmaps
--------

Heatmaps support 1D sweep extraction:

* Right-click the heatmap and select `Plot Horizontal Sweep` or
  `Plot Vertical Sweep`.
* A sweep window opens, with a cursor shown on the heatmap.
* Move the sweep position with the sweep window slider or by dragging the cursor
  on the heatmap.
* Switch the sweep and fixed parameters with the `x axis` and `fixed parameter`
  dropdowns.
* Sweep plots are live-data compatible.
* Sweep plots can be added to compatible 1D plots when their x axis matches the
  1D plot's independent variable.

Keyboard Shortcuts
------------------

General shortcuts:

| Shortcut | Action |
| --- | --- |
| `R` | Refresh the current window |
| `Ctrl+W` / `Cmd+W` | Close the current qPlot window |
| `Ctrl+Q` / `Cmd+Q` | Quit qPlot |
| `Cmd+M` / `Alt+Space, N` | Minimize the current window |
| `Alt+Space, X` / `Alt+Space, R` | Maximize or restore the current window on Windows |
| `Ctrl+Cmd+F` / `F11` | Enter or leave full screen |
| `Shift+F10` | Open the focused widget's context menu |
| `Ctrl+Shift+D` | Open the current database folder |
| `Ctrl+Return` | Open all plots for the selected run |
| `Ctrl+1` to `Ctrl+9` | Open dependent parameters 1 to 9 for the selected run |

Plot-window shortcuts:

| Shortcut | Action |
| --- | --- |
| `Ctrl+0` | Autoscale the plot view |
| `Ctrl+Shift+O` | Show or hide the operations panel |
| `Ctrl+Alt+R` | Show or hide the refresh toolbar |
| `Ctrl+Alt+C` | Show or hide the coordinate toolbar |
| `Ctrl+Alt+A` | Show or hide the axis control panel |
| `Ctrl+Alt+O` | Show or hide the operations dock |

Heatmap shortcuts:

| Shortcut | Action |
| --- | --- |
| `Ctrl+Shift+C` | Autoscale the colour range |
| `Ctrl+Shift+H` | Open a horizontal sweep |
| `Ctrl+Shift+V` | Open a vertical sweep |

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

For local development, install in editable mode:

```console
python -m pip install -e .
```

Run the current non-GUI tests:

```console
python -m unittest discover -s tests -p "test*.py"
```

Run the manual GUI smoke script:

```console
python tests/test.py
```
