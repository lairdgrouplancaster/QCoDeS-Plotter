from .config import config

from jsonschema import ValidationError

import sys

class sysHandle:
    """
    Entry point for terminal/console based commands. Called through scripts, 
    which is set up in pyproject.toml.
    
    This is somewhat Jerry-Rigged, fetching string following qplot-cfg and 
    converts them to command matching functions here.
    """
    def __init__(self, command, *args):
        
        # Find valid commands by checking against class attributes which are callable
        self.valid_args = [f"-{str(method_name)}" for method_name in dir(sysHandle)
                      if callable(getattr(sysHandle, method_name))
                      and method_name[0] != "_"]

        # Convert command str to callable
        if command in self.valid_args:
            func = getattr(self, command[1:])
        else:
            key = f"Command: ({command}), not found. Valid options: {self.valid_args}"
            raise KeyError(key)
        
        # Create config to interact with
        self.config = config()
        func(*args) # Pass other arguments to func

    def dump(self):
        """
        -dump
        -----
        Prints the location of the config.json file along with its full 
        contents
        
        """
        self.config.dump()
        
    def reset(self):
        """
        -reset
        ------
        Reset all config.json values to their defaults and prints new config
        file


        """
        self.config.reset_to_defaults()
        print("Config reset:")
        self.dump()
        
    def find(self, key : str):
        """
        -find
        -----
        Returns the key and the value assiated with that key.
        key must be laid out as a dot (.) seperated path, i.e.
            qplot-cfg -find GUI.main_frame_size

        Parameters
        ----------
        key : str
            Location of value to fetch.
        
        Raises
        ------
        KeyError
            Key not found in file

        """
        print(f"{key}:\t{self.config.get(key)}")
        
    def set_value(
            self,
            key : str, 
            value : [str, list, int, float]
            ):
        """
        -set_value
        ----------
        Sets the value in the config.json file, located at key.
        
        > key must be laid out as a dot (.) seperated path, i.e.
             qplot-cfg -set_value user_preference.theme dark
          or
             qplot-cfg -set_value GUI.plot_frame_fraction 0.25
            
        > If value has spaces in it, speech marks are required around it
             qplot-cfg -set_value file.default_load_path "./file/with space"

        Parameters
        ----------
        key : str
            Location of the value to change.
        value : [str, list, int, float]
            Value to be changed to.

        Raises
        ------
        KeyError
            Key passed is not located in config.json
        ValidationError
            Value type is incorrect for corresponding key.

        """
        
        #check if key is valid
        self.config.get(key)
        
        if value[0] == "[" or value[0] == "(":
            convrt_value = value[1:-1].split(",")
            for itr in range(len(convrt_value)):
                convrt_value[itr] = try_as_num(convrt_value[itr])
        else:
            if convrt_value.lower() == "true":
                convrt_value = True
            elif convrt_value.lower() == "false":
                convrt_value = False
            else:
                convrt_value =  try_as_num(value)
        
        try:
            self.config.update(key, convrt_value)
        except ValidationError as error:
            err_key = "Value: {value}, is invalid."
            err_key += str(error)
            raise ValidationError(err_key)
        print(f"set '{key}' to '{value}'")
        
        
    def info(self, attr : str=None):
        """
        -info
        -----
        Gets infomation about callable functions.
        
        'qplot-cfg -info' lists all callable functions
        'qplot-cfg -info <attr>' produces the docstring of the function
            Note: the '-' before <attr> can be ommited

        Parameters
        ----------
        attr : callable, optional
            Produces the docstring of command attr. If none are given, prints 
            all commands

        Raises
        ------
        KeyError
            Invalid command was given.
            
        """
        if not attr:
            print(f"Valid Commands:\n\t{self.valid_args}\nUse 'qplot-cfg -info <command>' for more info")
            return
        elif attr in self.valid_args:
            func = getattr(self, attr[1:])
        elif ("-"+attr) in self.valid_args: 
            func = getattr(self, attr)
        else:
            raise KeyError(f"Command: ({attr}), not found. Valid options: {self.valid_args}, '-' may be ommited")
        
        print(func.__doc__)


def try_as_num(item):
    """
    Attempts to convert str to float or int.

    Parameters
    ----------
    item : str
        item to be converted.

    Returns
    -------
    item : int, float, str
        The item after conversions.

    """
    try:
        if "." in item: # Check for decimal point. Add log_10 check for large nums?
            item = float(item)
        else:
            item = int(item)
    except ValueError: #if cannot conver to int or float
        item = str(item)
    return item


def scripts():
    """
    The actual entry point which call sysHandle to manage commands

    """
    # Fetch str based command from command line
    args=sys.argv
    sysHandle(*args[1:])