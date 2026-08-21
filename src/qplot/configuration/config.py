import json
import math
import os
import stat
import tempfile
from copy import deepcopy
from importlib.resources import files
from os import makedirs, path
from shutil import copyfileobj

import jsonschema

from qplot.diagnostics import log_exception

from .themes import dark, light, pyqt

THEME_CLASSES = {
    "light": light,
    "dark": dark,
    "pyqt": pyqt,
}


def _strict_integer(_checker, instance):
    return isinstance(instance, int) and not isinstance(instance, bool)


def _finite_number(_checker, instance):
    if isinstance(instance, bool) or not isinstance(instance, (int, float)):
        return False
    try:
        return math.isfinite(instance)
    except (OverflowError, TypeError, ValueError):
        return False


_STRICT_TYPE_CHECKER = (
    jsonschema.Draft202012Validator.TYPE_CHECKER
    .redefine("integer", _strict_integer)
    .redefine("number", _finite_number)
    )
StrictConfigValidator = jsonschema.validators.extend(
    jsonschema.Draft202012Validator,
    type_checker=_STRICT_TYPE_CHECKER,
    )


def _reject_json_constant(value):
    raise json.JSONDecodeError(
        f"Non-standard JSON numeric constant {value!r}",
        value,
        0,
        )

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
        self.startup_warning = None
        
        # Make config file if missing
        if not path.isfile(self.default_file):
            self.config = self.build_default_config()
            try:
                self.save_config(self.default_file)
            except OSError as error:
                self._record_startup_persistence_failure(
                    "Could not create default configuration at "
                    f"{self.default_file}",
                    error,
                    )
        else:
            try:
                loaded_config = self.load_config(self.default_file)
                if not isinstance(loaded_config, dict):
                    raise jsonschema.ValidationError(
                        "config.json root must be a JSON object"
                    )

                migrated = self._migrate_config(loaded_config)
                self.validate(loaded_config)
                self.config = loaded_config
                if migrated:
                    try:
                        self.save_config(self.default_file)
                    except OSError as error:
                        self._record_startup_persistence_failure(
                            "Could not persist migrated configuration at "
                            f"{self.default_file}",
                            error,
                            )

            # config.json does not meet schema requirements
            except (json.JSONDecodeError, jsonschema.ValidationError) as error:
                self._recover_invalid_config(error)
        
    
    def __str__(self) -> str:
        """
        Produces a display similar to how the config.json file looks

        Returns
        -------
        str

        """
        return json.dumps(self.config, indent=4)
    
    
    def __repr__(self):
        return str(self)
    
    
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
            section = self.config.get(keys[0])
            if isinstance(section, dict):
                out = section.get(keys[1])
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
        self.update_many({key: value})


    def update_many(self, values):
        """Validate and persist several values as one configuration update."""

        updated_config = deepcopy(self.config)
        for key, value in values.items():
            keys = key.split(".")
            target = updated_config
            for part in keys[:-1]:
                try:
                    target = target[part]
                except KeyError as err:
                    raise KeyError(
                        f"Key: {key}, not found. Please ensure you use a dot (.) seperated key"
                    ) from err

                if not isinstance(target, dict):
                    raise KeyError(
                        f"Key: {key}, cannot be updated because {part} is not a section"
                    )

            if keys[-1] not in target:
                raise KeyError(
                    f"Key: {key}, not found. Please ensure you use a dot (.) seperated key"
                )
            target[keys[-1]] = value

        self.validate(updated_config)
        previous_config = self.config
        self.config = updated_config
        try:
            self.save_config(self.default_file)
        except Exception:
            self.config = previous_config
            raise
    
    
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
            config = json.load(fp, parse_constant=_reject_json_constant)
        return config


    def save_config(self, path: str) -> None:
        """
        Save current config to file at given path.

        Parameters
        ----------
        path: str 
            path of file

        """
        directory = os.path.dirname(os.path.abspath(path))
        makedirs(directory, exist_ok=True)
        file_mode = None
        try:
            file_mode = stat.S_IMODE(os.stat(path).st_mode)
        except FileNotFoundError:
            pass

        file_descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{os.path.basename(path)}.",
            suffix=".tmp",
            dir=directory,
            text=True,
            )
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as fp:
                json.dump(self.config, fp, indent=4, allow_nan=False)
                fp.write("\n")
                fp.flush()
                os.fsync(fp.fileno())
            if file_mode is not None:
                os.chmod(temporary_path, file_mode)
            os.replace(temporary_path, path)
            self._sync_config_directory(directory)
        except Exception:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
            raise


    @staticmethod
    def _sync_config_directory(directory: str) -> None:
        """Persist the rename on platforms that support directory fsync."""

        if os.name == "nt":
            return
        try:
            descriptor = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)
            
    
    def reset_to_defaults(self):
        """
        Resets the config.json file to default determined by the config_schema

        Needs adjusting if nesting of config increases
        """
        config = self.build_default_config()
        
        # Save reset to file. Keep the last persisted configuration
        # authoritative if any part of the atomic write fails.
        had_previous_config = hasattr(self, "config")
        previous_config = getattr(self, "config", {})
        self.config = config
        try:
            self.save_config(self.default_file)
        except Exception:
            if had_previous_config:
                self.config = previous_config
            else:
                del self.config
            raise


    def build_default_config(self):
        """Construct and validate a fresh default configuration in memory."""

        config = {}
        for key, val in self.schema["properties"].items():
            config[key] = {
                setting: deepcopy(setting_schema["default"])
                for setting, setting_schema in val["properties"].items()
                }
        self.validate(config)
        return config


    def _recover_invalid_config(self, original_error):
        """Use defaults after an invalid config without risking the original."""

        log_exception(
            f"Invalid configuration at {self.default_file}",
            original_error,
            __name__,
            )
        self.config = self.build_default_config()
        try:
            self.invalid_config_backup_file = self.backup_invalid_config()
        except OSError as error:
            self._record_startup_persistence_failure(
                "Could not back up invalid configuration at "
                f"{self.default_file}",
                error,
                )
            return

        try:
            self.save_config(self.default_file)
        except OSError as error:
            self._record_startup_persistence_failure(
                "Could not replace invalid configuration at "
                f"{self.default_file}",
                error,
                )


    def _record_startup_persistence_failure(self, context, error):
        """Log a failed startup write and retain a non-blocking UI warning."""

        log_exception(context, error, __name__)
        self.startup_warning = (
            "Configuration recovery could not be saved; using defaults for "
            "this session."
            )


    def validate(self, candidate):
        """Validate a config with strict Python integer and finite-number types."""

        StrictConfigValidator(self.schema).validate(candidate)


    def _migrate_config(self, candidate):
        """Apply non-destructive migrations for newly introduced settings."""

        preferences = candidate.get("user_preference")
        if not isinstance(preferences, dict):
            return False

        preference_schema = self.schema["properties"]["user_preference"]["properties"]
        migrated = False
        # ``axis_tick_density`` was superseded by an explicit target because
        # PyQtGraph clamps density values and cannot produce sparse axes.
        if preferences.pop("axis_tick_density", None) is not None:
            migrated = True

        for key in ("colorbar_width", "axis_tick_width", "axis_major_tick_count"):
            if key not in preferences:
                preferences[key] = deepcopy(preference_schema[key]["default"])
                migrated = True
        return migrated


    def schema_for(self, key):
        """Return the JSON-schema node for a supported dotted config key."""

        keys = key.split(".")
        if len(keys) != 2:
            raise KeyError(
                f"Key: {key}, not found. Please ensure you use a dot (.) seperated key"
                )
        try:
            return self.schema["properties"][keys[0]]["properties"][keys[1]]
        except KeyError as err:
            raise KeyError(
                f"Key: {key}, not found. Please ensure you use a dot (.) seperated key"
                ) from err


    def backup_invalid_config(self):
        """
        Copies an invalid config file aside before resetting to defaults.

        """
        directory = path.dirname(self.default_file)
        makedirs(directory, exist_ok=True)
        source_stat = os.stat(self.default_file)

        while True:
            backup_file = self.next_invalid_config_backup_file()
            try:
                descriptor = os.open(
                    backup_file,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    stat.S_IMODE(source_stat.st_mode),
                    )
            except FileExistsError:
                continue
            break

        try:
            with (
                open(self.default_file, "rb") as source,
                os.fdopen(descriptor, "wb") as destination,
                ):
                copyfileobj(source, destination)
                destination.flush()
                os.fsync(destination.fileno())
            os.chmod(backup_file, stat.S_IMODE(source_stat.st_mode))
            os.utime(
                backup_file,
                ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns),
                )
        except OSError:
            try:
                os.unlink(backup_file)
            except FileNotFoundError:
                pass
            raise
        return backup_file


    def next_invalid_config_backup_file(self):
        """
        Returns a non-existing path for an invalid config backup.

        """
        directory = path.dirname(self.default_file)
        stem, suffix = path.splitext(path.basename(self.default_file))
        candidate = path.join(directory, f"{stem}.invalid{suffix}")
        if not path.exists(candidate):
            return candidate

        index = 1
        while True:
            candidate = path.join(directory, f"{stem}.invalid.{index}{suffix}")
            if not path.exists(candidate):
                return candidate
            index += 1

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
        return THEME_CLASSES[config_theme]
