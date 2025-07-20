import json

from importlib.resources import files

class config:
    
    config_file_name = "config.json"
    
    default_file = str(files("qplot.configuration") / config_file_name)
    
    
    def __init__(self):
        self.load_config(self.default_file)

    
    def __str__(self):
        out = ""
        for k, v in self.config.items():
            out += f"{k}: {v}\n"
        return out
    
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
        if len(keys) == 1:
            self.config[key] = value
        elif len(keys) == 2:
            self.config[keys[0]][keys[1]] = value
        else:
            raise KeyError(f"key length too long, {key}")
        
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
            self.config = json.load(fp)


    def save_config(self, path: str) -> None:
        """
        Save current config to file at given path.

        Args:
            path: path of new file

        """
        with open(path, "w") as fp:
            json.dump(self.config, fp, indent=4)

    
