import json
import jsonschema

from copy import deepcopy

from os import makedirs
from os import path

from importlib.resources import files

from .themes import *

class config:
    
    config_file_name = "config.json"
    schema_file_name = "config_schema.json"
    
    default_file = path.expanduser(
        path.join("~", ".qplot", config_file_name)
        )
    default__schema_file = str(files("qplot.configuration") / schema_file_name)
    
    def __init__(self):
        self.schema = self.load_config(self.default__schema_file)
        
        if not path.isfile(self.default_file):
            makedirs(path.dirname(self.default_file), exist_ok=True)
            open(self.default_file, 'x').close()
            
            self.reset_to_defaults()
        else:
            self.config = self.load_config(self.default_file)
        
    
    def __str__(self):
        return json.dumps(self.config, indent=4)
    
    
    def __repr__(self):
        return self.config
    
    
    def dump(self):
        print(f"config.json at: {self.default_file} \ncontents:")
        print(str(self))
    
    
    def get(self, key):
        out = None
        keys = key.split(".")
        if len(keys) == 1:
            out = self.config.get(key)
        elif len(keys) == 2:
            out = self.config.get(keys[0]).get(keys[1])
        else:
            raise KeyError(f"Key length too long, {key}. Please ensure you use a dot (.) seperated key")
        
        if out != None:
            return out
        else:
            raise KeyError(f"Key: {key}, not found. Please ensure you use a dot (.) seperated key")
    
    
    def update(self, key, value):
        keys = key.split(".")
        
        config = deepcopy(self.config)
        
        #to anyone reading this, good luck
        run_str = ""
        for key in keys:
            run_str += f"['{key}']" # chain .get(keys) for dic item
        
        exec(f"config{run_str} = value") #add value to dic under key
        
        jsonschema.validate(config, self.schema)
        
        self.config = config
        self.save_config(self.default_file)
    
    
    def load_config(self, path: str):
        """Load a config JSON file

        Args:
            path: path to the config file
        Return:
            a dot accessible dictionary config object
        Raises:
            FileNotFoundError: if config is missing

        """
        with open(path) as fp:
            config = json.load(fp)
        return config


    def save_config(self, path: str) -> None:
        """
        Save current config to file at given path.

        Args:
            path: path of new file

        """
        with open(path, "w") as fp:
            json.dump(self.config, fp, indent=4)
            
    
    def reset_to_defaults(self):
        config = {}
        for key, val in self.schema["properties"].items():
            subdict = {}
            for k, v in val["properties"].items():
                subdict[k] = v["default"]
            config[key] = subdict
        
        jsonschema.validate(config, self.schema)
        self.config = config
        
        self.save_config(self.default_file)
        
###############################################################################    
#handled functions
    
    @property
    def theme(self):
        config_theme = self.get("user_preference.theme")        
        return eval(config_theme)
