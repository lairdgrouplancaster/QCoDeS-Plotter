from .config import config

from jsonschema import ValidationError

import sys

class sysHandle:
    def __init__(self, command, *args):
        
        self.valid_args = [f"-{str(method_name)}" for method_name in dir(sysHandle)
                      if callable(getattr(sysHandle, method_name))
                      and method_name[0] != "_"]

        if command in self.valid_args:
            func = getattr(self, command[1:])
        else:
            key = f"Command: ({command}), not found. Valid options: {self.valid_args}"
            raise KeyError(key)
        
        self.config = config()
        func(*args)

    def dump(self):
        self.config.dump()
        
    def reset(self):
        self.config.reset_to_defaults()
        print("Config reset:")
        self.dump()
        
    def find(self, key):
        print(self.config.get(key))
        
    def set_value(self, key, value):
        try:
            self.config.update(key, value)
        except ValidationError as error:
            err_key = "Could not set value. Please ensure a dot (.) seperated key, or use -find to ensure key is valid. "
            err_key += str(error)
            raise ValidationError(err_key)
        print(f"set '{key}' to '{value}'")
        
    def info(self):
        print(f"Valid Commands:\n\t{self.valid_args}")
        
        
def scripts():
    args=sys.argv
    sysHandle(*args[1:])