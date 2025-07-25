QCodes-Plotter
==============

QCodes-Plotter is an alternative live data plotter aimed to provide fast data display of running and finished QCoDeS experiments.

Installation
------------

Install with, requires Git:

    pip install git+https://github.com/lairdgrouplancaster/QCoDeS-Plotter.git@main

and while repo is private, you will need to sign in. (Usually twice)

<br/>

How to Run
----------

After installing with pip into a virtual environment
To run, either:
* In its own IDE consol
  
      import qplot
      qplot.run()
  
* open a powershell, activate the virtual environment and enter,
  
      qplot

<br/>
<br/>

Config 
------
To see all config options, in terminal run:
```console
qplot-cfg -info
```
Currently changes to config file can be done in file, IDE, or terminal.  (Top box is IDE, bottom is terminal)
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
> You will need to use "" if the value has spaces.
  
* To reset config to defaults
```python
    config().reset_to_defaults()
```
```console
    qplot-cfg -reset
```
