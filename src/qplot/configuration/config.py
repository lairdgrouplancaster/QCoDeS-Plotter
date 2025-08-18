import json
import jsonschema

from copy import deepcopy

from os import makedirs
from os import path

from importlib.resources import files

from .themes import * # ignore error, used in self.theme()

class config:
    """
    Class for reading and writing to the config.json file.
    
    **NOTE, config search functions are only set up for 1 level of nested
            dictionaries and will require edits or work arounds after that.
    
    see self.default_file for config location
    
    .config_schema.json control defaults and restrictions on what can be changed
    """
    config_file_name = "config.json"
    schema_file_name = "config_schema.json"
    
    default_path = path.expanduser(
        path.join("~", ".qplot")
        )
    default_file = path.join(default_path, config_file_name)
    default__schema_file = str(files("qplot.configuration") / schema_file_name)
    
    def __init__(self):
        self.schema = self.load_config(self.default__schema_file)
        
        # Make config file if missing
        if not path.isfile(self.default_file):
            makedirs(path.dirname(self.default_file), exist_ok=True)
            open(self.default_file, 'x').close()
            
            # Create config from schema
            self.reset_to_defaults()
        else:
            try:
                self.config = self.load_config(self.default_file)
                jsonschema.validate(self.config, self.schema)
            
            # config.json does not meet schema requirements
            except jsonschema.ValidationError as err:
                print("!!! config.json is invalid and cannot be loaded !!!\n"
                      "Please reset config.json to defaults or fix manually.")
                
                while True:
                    user_in = input("Would you like to reset to default? [y/n]: ")
                    if user_in.lower() == "y":
                        self.reset_to_defaults()
                        break
                    elif user_in.lower() == "n":
                        print(f"Please see: {self.default_file}, and fix "
                              "the following error.")
                        raise err
                    else:
                        print("Invalid Input.")
        
    
    def __str__(self) -> str:
        """
        Produces a display similar to how the config.json file looks

        Returns
        -------
        str

        """
        return json.dumps(self.config, indent=4)
    
    
    def __repr__(self):
        return self.config
    
    
    def dump(self):
        """
        Prints out Information about config.json, including location and all data
        contained inside the file

        """
        print(f"config.json at: {self.default_file} \ncontents:")
        print(str(self))
    
    
    def get(self, key):
        """
        Returns data of at specified key.
        Key must be laid out as a dot (.) seperated path, i.e.
            'GUI.main_frame_size'
        
        Parameters
        ----------
        key : str
            Key of value.

        Raises
        ------
        KeyError
            Key passed was invalid, other not in correct for or key not found.

        Returns
        -------
        out : any
            Value at specified key.

        """
        out = None
        # Get number of nests to look though
        keys = key.split(".")
        if len(keys) == 1:
            out = self.config.get(key)
        elif len(keys) == 2:
            out = self.config.get(keys[0]).get(keys[1])
        else:
            raise KeyError(f"Key length too long, {key}. Please ensure you use a dot (.) seperated key")
        
        # Return value if found
        if out != None:
            return out
        else:
            raise KeyError(f"Key: {key}, not found. Please ensure you use a dot (.) seperated key")
    
    
    def update(self, key, value):
        """
        Updates value at key location

        Parameters
        ----------
        key : str
            Lookup Key of value.
        value : any
            Value to be changed to.

        Raises
        ------
        jsonschema.exceptions.ValidationError
            Value updated is not allowed under conditions set by schema.
            Either due to incorrect typing or trying to add a new value.

        """
        keys = key.split(".")
        
        # Create copy to prevent unwanted changes to file
        config = deepcopy(self.config)
        
        #to anyone reading this, good luck
        run_str = ""
        for key in keys:
            run_str += f"['{key}']" # chain .get(keys) for dic item
        
        exec(f"config{run_str} = value") #add value to dic under key
        
        # Check update is allowed by schema
        jsonschema.validate(config, self.schema)
        
        # Update config file
        self.config = config
        self.save_config(self.default_file)
    
    
    def load_config(self, path: str):
        """
        Load a config JSON file

        Parameters
        ----------
        path: str
            path to the config file
        
        Return
        ------
        config: dict    
            Returns config file in form of a dictionary
        
        Raises
        ------
        FileNotFoundError: 
            if config is missing

        """
        with open(path) as fp:
            config = json.load(fp)
        return config


    def save_config(self, path: str) -> None:
        """
        Save current config to file at given path.

        Parameters
        ----------
        path: str 
            path of file

        """
        with open(path, "w") as fp:
            json.dump(self.config, fp, indent=4)
            
    
    def reset_to_defaults(self):
        """
        Resets the config.json file to default determined by the config_schema

        Needs adjusting if nesting of config increases
        """
        config = {}
        # Runs through all values in schema and fetch default value
        for key, val in self.schema["properties"].items():
            subdict = {}
            for k, v in val["properties"].items():
                subdict[k] = v["default"]
            # Reset value
            config[key] = subdict
        
        # Confirm reset worked
        jsonschema.validate(config, self.schema)
        
        # Save reset to file
        self.config = config
        self.save_config(self.default_file)
        
###############################################################################    
#handled functions
    
    @property
    def theme(self):
        """
        Fetches theme data from .themes

        Returns
        -------
        callable
            Returns the class in qplt.configuration.themes coresponding to the
            set config value

        """
        config_theme = self.get("user_preference.theme")        
        return eval(config_theme)
