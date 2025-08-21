QCodes-Plotter
==============

QCodes-Plotter is an alternative data plotter aimed to provide fast data display of running and completed expermients using QCoDeS databases.

<br/>

> [!IMPORTANT]
> Please note that plots will open empty while loading. Unless the app stops responding or errors appear in console, please wait for loading to finish.

Installation
------------

Install with, requires Git:
```console
    pip install git+https://github.com/lairdgrouplancaster/QCoDeS-Plotter.git@main
```
Recommended to create and activate a virtual environment to avoid conflicts.
<br/>

How to Run
----------

After installing with pip into a virtual environment
To run, either:
* In its own IDE consol
```python  
      import qplot
      qplot.run()
```
* open a powershell, activate the virtual environment and enter, (**Recomended**)
```console
      qplot
```
<br/>

How to Use
----------
After opening a QCoDeS database (.db) with File -> Load, all runs within the database will be displayed in the central table. The table is sortable by clicking on any column header.
Clicking on a run displays a more detailed view in the table below. <br/>
After selecting a run, it can be opened with the "Open Plots" button or by double clicking the row in the central table. Alternatively you may enter a run ID into the text box above the central table, then use the "Open Plots" Button. <br/>
Both double clicking and using the button will open a window with a plot display for each dependent parameter. To open a specific parameter, right click on the row and use the "Open" context menu. <br/> 
The "Add _ to _" context menu is used to add additional lines to a plot window. It displays the parameters in the selected row (Add _) and then which plots that parameter can be added to (to _) by matching independant variable names. This can also be done within a line plot window and is mentioned later.
<br/>
You can alter how frequently the app checks for new runs by changing the interval box in the top toolbar. Setting the value to 0.0, will stop any checks. You can also manually refresh using File -> Refresh or by pressing R. The "Toggle Auto-plot" tickbox will open any new runs once ticked.
<br/>
The "Options" menu in the menu bar allows for: changing between light and dark mode and the default PyQt display, and allows for changing the default directory for the File -> Load dialog box. Both of these can also be changed using configuration options, see below. 
<br/>
<br/>
Plot windows are separated into 2 types, line graphs and heatmaps. They both have toolbars which can be displayed or hidden using View -> Toolbars or by right-clicking on a toolbar. <br/>
* The top refresh toolbar is only shown on runs which are live, but can be displayed on static runs. The refresh timer takes the same value as the main window if it is set to update, otherwise it tries to refresh every 5.0s.
* The bottom toolbar displays the current coordinates of the mouse.
* The left toolbar is used to change to assigned axes, this is mainly used for heatmaps with more than 2 independent variables but can be used to flip axes.
* The left toolbar also contains specialist functionality.
* The right toolbar controls operations, which are ran during the refresh process but after the data is collected from the database. After selecting from the bottom table, they appear in the top table. You can then enter inputs as needed and drag to sort the order in which the operations are run. (top to bottom)
  The right toolbar is hidden by default but can be opened through any method or the plot context menu.


### Line Graphs
* For Line graphs, you can change the colour of the line in the left toolbar. You may also add other lines from other open line graphs using the dropdown menu, control which y axis these are attached to, and change the color. The "X" button can be used to remove a line.
* When multiple lines are connected to different sides, using scroll or drag on the central area of the plot controls both axes, while placing your cursor on a side axis will control only that axis.
* Once a line is added, the source window can be closed. Live updates will continue with the same refresh rate.
* Please note that secondary lines cannot be rotated when attached to the right axis.

### Heatmaps
* 1d sweeps can be performed on heatmaps by right clicking on the row/column that you wish to sweep and using "Plot Horizontal/Vertical Sweep".
* This will create a new window with the sweep, and a cursor of the sweep location on the heatmap. The sweep is <ins>live data compatible</ins>.
* Within the sweep plot, you may move the sweep location with the slider on the left, this can also be done by dragging the cursor on the heatmap.
* You may also switch which is the sweep parameter and the fixed parameter using the "x axis" or "fixed parameter" dropdown boxes.
* Any changes in the sweep plot will be reflected in the cursor, including parameter and <ins>colour change</ins>.
* Once a sweep is open, the heatmap can be closed and the sweep will remain open and live.
* Sweeps can be added to 1d plots if the sweep's x axis matches the 1d plots independant variable. Manual refresh may be needed to pick the option and update any changes.

<br/>

Configuration Options
---------------------
On first run, a config.json file is created in your user directory under C:/Users/\<User\>/.qplot/config.json. Values can maually be changed from there or using commands.
To see all config options, in terminal run:
```console
qplot-cfg -info
```
You may also get more infomation on a desired command using `-info <command>`, i.e.
```console
qplot-cfg -info dump
```

Changes to config file can be done in file, IDE, or terminal. (Top box is python IDE, bottom is terminal)
* To see the current config file, use:
```python
    from qplot import config
    config().dump()
```
```console
    qplot-cfg -dump
```
* To manually change a config value, use: (both take `key, value`, as such).
```python
    config().update("file.default_load_path", "C:\Users\<user>\Desktop")
```
```console
    qplot-cfg set_value file.default_load_path C:\Users\<user>\Desktop
```
> You will need to use "" if the value has spaces, this will not affect object typing.
  
* To reset config to defaults
```python
    config().reset_to_defaults()
```
```console
    qplot-cfg -reset
```
