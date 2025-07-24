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

Currently changes to config file can only be done in file and IDE. Terminal commands will be added shortly.
* Use:

      from qplot import config
      config().dump()
  to see current config file.

* To manually change a config value, use config().update(key, value), i.e.

      config().update("file.default_load_path", "C:\Users\<user>\Desktop")

* To reset config to defaults

      config().reset_to_defaults()
