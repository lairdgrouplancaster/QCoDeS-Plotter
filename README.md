QCodes-Plotter
==============

QCodes-Plotter is an alternative live data plotter aimed to provide fast data display of running and finished QCoDeS experiments.

<br/>
<br/>

Installation
------------

Install with, requires Git:
```console
    pip install git+https://github.com/lairdgrouplancaster/QCoDeS-Plotter.git@main
```
<br/>

How to Run
----------

After installing with pip into a virtual environment
To run, either:
* In its own IDE consol
```console  
      import qplot
      qplot.run()
```
* open a powershell, activate the virtual environment and enter, (**Recomended**)
```console
      qplot
```
<br/>
<br/>

How to Use
----------
After opening a QCoDeS database (.db) with File -> Load, all runs within the database will be displayed in the central table. The table is sortable by clicking on any column header.
Clicking on a run displays a more detailed view in the table below. <br/>
After selecting a run, it can be openned with the "Open Plots" button or by double clicking the row in the central table. Alternatively you may enter a run ID into the text box above the central table then use the "Open Plots" Button. <br/>
Both double clicking and using the button will open a window with a plot display for each dependant parameter. To open a specific parameter, right click on the row and use the "Open" context menu. <br/> 
The "Add _ to _" conext menu is used to add other line to a plot window. It displays the parameters in the selected row (Add _) and then which plots that parameter can be added to (to _) by matching independant varaible names. This can also be done within a line plot window and is mentioned later.
<br/>
You can alter how frequently the app check for new runs by changing the box in the top toolbar. Setting the value to 0.0, will stop any checks. You can also manually refresh using File -> Refresh or by pressing R. The "Toggle Auto-plt" tickbox will open any new runs once ticked.
<br/>
The "Options" menu in the menu bar allows for: changing between light and dark mode and the default PyQt display, and allows for changing the default directory for the File -> Load dialog box. Both of these can also be changed using configuration options, see below. 
<br/>
<br/>
Plot windows are seperated into 2 types, line graphs and heatmaps. They both have toolbars which can displayed or hidden using View -> Toolbars or by right-clicking on a toolbar. <br/>
* The top refresh toolbar is only shown on runs which are live, but can be displayed on static runs. The refresh timer takes the same value as the main window if it refreshing, otherwise it trys to refresh every 5.0s.
* The bottom toolbar displays the current co-ordinates of the mouse.
* The left toolbar is used to change to assigned axes, this is mainly used for heatmaps with more than 2 independant variables but can be used to flip axes.
* For Line graphs, you can change the color of the line in the left toolbar. You may also add other lines from other open line graphs, control which y axis these are attached to, and change the color. The "X" button can be used to remove a line.  

<br/>
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

Changes to config file can be done in file, IDE, or terminal.  (Top box is python IDE, bottom is terminal)
* To see current config file, use:
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
