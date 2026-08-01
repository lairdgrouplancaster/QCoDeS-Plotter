import json
import sys

from jsonschema import ValidationError

from qplot._metadata import package_version

from .config import config


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
            value: str,
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

        try:
            convrt_value = _config_value(value, self.config.schema_for(key))
            self.config.update(key, convrt_value)
        except (TypeError, ValueError, json.JSONDecodeError, ValidationError) as error:
            err_key = f"Value: {value}, is invalid."
            err_key += str(error)
            raise ValidationError(err_key) from error
        print(f"set '{key}' to '{value}'")
        
        
    def info(self, attr: str | None = None):
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


    def version(self):
        """
        -version
        --------
        Prints the installed qPlot package version.

        """
        print(package_version())


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
        item = item.strip()
        if "." in item or "e" in item.lower():
            item = float(item)
        else:
            item = int(item)
    except ValueError: #if cannot conver to int or float
        item = str(item)
    return item


def _config_value(value, schema):
    """Parse a command-line value according to its target config schema."""

    value_type = schema.get("type")
    stripped = value.strip()

    if value_type == "string":
        return value
    if value_type == "boolean":
        if stripped.lower() == "true":
            return True
        if stripped.lower() == "false":
            return False
        raise ValueError("expected true or false")
    if value_type == "integer":
        return int(stripped, 10)
    if value_type == "number":
        return float(stripped)
    if value_type == "array":
        if not (
                (stripped.startswith("[") and stripped.endswith("]"))
                or (stripped.startswith("(") and stripped.endswith(")"))
                ):
            raise ValueError("expected a bracketed list")
        json_list = f"[{stripped[1:-1]}]"
        try:
            return json.loads(json_list)
        except json.JSONDecodeError:
            inner = stripped[1:-1].strip()
            if not inner:
                return []
            item_schema = schema.get("items", {})
            return [
                _config_value(item.strip(), item_schema)
                for item in inner.split(",")
                ]

    return try_as_num(value)


def scripts():
    """
    The actual entry point which call sysHandle to manage commands

    """
    # Fetch str based command from command line
    args=sys.argv
    if len(args) == 1:
        sysHandle("-info")
    else:
        sysHandle(*args[1:])
