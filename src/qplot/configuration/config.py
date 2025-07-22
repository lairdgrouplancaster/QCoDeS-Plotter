import json
import jsonschema

from copy import deepcopy

from importlib.resources import files

class config:
    
    config_file_name = "config.json"
    schema_file_name = "config_schema.json"
    
    default_file = str(files("qplot.configuration") / config_file_name)
    default__schema_file = str(files("qplot.configuration") / schema_file_name)
    
    def __init__(self):
        self.config = self.load_config(self.default_file)
        self.schema = self.load_config(self.default__schema_file)
        jsonschema.validate(self.config, self.schema)
        
    
    def __str__(self):
        return json.dumps(self.config, indent=4)
    
    def __repr__(self):
        return self.config
    
    
    def get(self, key):
        keys = key.split(".")
        if len(keys) == 1:
            return self.config.get(key)
        elif len(keys) == 2:
            return self.config.get(keys[0]).get(keys[1])
        else:
            raise KeyError(f"key length too long, {key}")
    
    
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


if __name__=="__main__":
    conf = config()
    
    conf.reset_to_defaults()