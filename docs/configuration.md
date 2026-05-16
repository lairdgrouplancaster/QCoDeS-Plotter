# Configuration

QCoDeS-Plotter stores user settings in:

```text
~/.qplot/config.json
```

The schema and defaults live in
`src/qplot/configuration/config_schema.json`. Treat that file as the source of
truth for valid keys, value types, defaults, and validation limits.

## Common Commands

Show available config commands:

```console
qplot-cfg -info
```

Show the installed qPlot version:

```console
qplot-cfg -version
```

Print the current config:

```console
qplot-cfg -dump
```

Find a value:

```console
qplot-cfg -find user_preference.theme
```

Update a value:

```console
qplot-cfg -set_value user_preference.theme dark
```

Use quotes around terminal values that contain spaces:

```console
qplot-cfg -set_value file.default_load_path "C:\Users\<user>\Desktop"
```

Reset all settings to schema defaults:

```console
qplot-cfg -reset
```

Common settings can also be edited in the application with
`Options -> Preferences...`.

Python code can use the public `config` object directly:

```python
from qplot import config

cfg = config()
cfg.update("user_preference.theme", "dark")
cfg.dump()
```

## Config Behavior

On startup, QCoDeS-Plotter creates `~/.qplot/config.json` if it does not exist.
Existing config files are validated against `config_schema.json`.

When new keys are added to the schema, they are added to existing config files
with their default values.

If the config file is invalid JSON or fails schema validation, it is copied to
`config.invalid.json` in `~/.qplot` and replaced with defaults. If that backup
already exists, QCoDeS-Plotter uses the next available numbered backup, such as
`config.invalid.1.json`.

Config keys use dotted paths in code and in `qplot-cfg`, for example
`user_preference.default_refresh_rate`.

## Key Reference

| Key | Type | Default | Validation | Purpose |
| --- | --- | --- | --- | --- |
| `GUI.plot_frame_fraction` | number | `0.47` | `0 < value < 1` | Fraction of a plot window used for the plot frame. |
| `GUI.main_frame_size` | integer array | `[600, 700]` | exactly 2 items | Initial main-window width and height. |
| `GUI.preview_size` | integer | `200` | `50 <= value <= 1000` | Preview thumbnail size in pixels. |
| `file.default_load_path` | string | `""` | any string | Default folder for selecting database files. |
| `file.last_file_path` | string | `""` | any string | Last database file opened by the application. |
| `file.recent_file_paths` | string array | `[]` | any strings | Recent database files shown by the application. |
| `user_preference.theme` | string | `"light"` | `light`, `dark`, or `pyqt` | Active application theme. |
| `user_preference.bar_colour` | string | `"viridis"` | any string | Default 2D colour map name. |
| `user_preference.bar_colour_include_cet` | boolean | `true` | `true` or `false` | Show colour maps from `colorcet`. |
| `user_preference.bar_colour_include_matplotlib` | boolean | `true` | `true` or `false` | Show Matplotlib colour maps. |
| `user_preference.bar_colour_include_local` | boolean | `true` | `true` or `false` | Show locally defined colour maps. |
| `user_preference.bar_colour_include_custom` | boolean | `true` | `true` or `false` | Show custom colour maps. |
| `user_preference.bar_colour_include_cet_linear` | boolean | `true` | `true` or `false` | Show linear `colorcet` maps. |
| `user_preference.bar_colour_include_cet_divergent` | boolean | `true` | `true` or `false` | Show divergent `colorcet` maps. |
| `user_preference.bar_colour_include_cet_cyclic` | boolean | `true` | `true` or `false` | Show cyclic `colorcet` maps. |
| `user_preference.bar_colour_include_cet_rainbow` | boolean | `true` | `true` or `false` | Show rainbow `colorcet` maps. |
| `user_preference.bar_colour_include_cet_isoluminant` | boolean | `true` | `true` or `false` | Show isoluminant `colorcet` maps. |
| `user_preference.bar_colour_include_cet_other` | boolean | `true` | `true` or `false` | Show uncategorized `colorcet` maps. |
| `user_preference.bar_colour_include_matplotlib_perceptual` | boolean | `true` | `true` or `false` | Show perceptual Matplotlib maps. |
| `user_preference.bar_colour_include_matplotlib_sequential` | boolean | `true` | `true` or `false` | Show sequential Matplotlib maps. |
| `user_preference.bar_colour_include_matplotlib_divergent` | boolean | `true` | `true` or `false` | Show divergent Matplotlib maps. |
| `user_preference.bar_colour_include_matplotlib_cyclic` | boolean | `true` | `true` or `false` | Show cyclic Matplotlib maps. |
| `user_preference.bar_colour_include_matplotlib_qualitative` | boolean | `true` | `true` or `false` | Show qualitative Matplotlib maps. |
| `user_preference.bar_colour_include_matplotlib_other` | boolean | `true` | `true` or `false` | Show uncategorized Matplotlib maps. |
| `user_preference.bar_colour_excluded` | string array | `[]` | any strings | Hide colour maps with these exact names. |
| `user_preference.bar_colour_excluded_prefixes` | string array | `[]` | any strings | Hide colour maps with these prefixes. |
| `user_preference.confirm_close` | boolean | `true` | `true` or `false` | Ask before closing the main window. |
| `user_preference.confirm_close_all` | boolean | `true` | `true` or `false` | Ask before closing all plot windows. |
| `user_preference.default_refresh_rate` | number | `1` | `value >= 0` | Default plot refresh interval. |
| `runtime_settings.max_threads` | integer | `4` | `value >= 1` | Maximum worker threads for background loading. |
| `runtime_settings.del_grace_period` | number | `10` | `0 <= value <= 300` | Grace period before deleting temporary files. |
| `runtime_settings.cloud_sync_timeout` | number | `120` | `1 <= value <= 3600` | Seconds to wait for cloud storage to hydrate a database before failing. |

## Adding Config Keys

When adding or changing a config key:

1. Update `src/qplot/configuration/config_schema.json`.
2. Update the relevant code that reads or writes the key.
3. Add or update tests in `tests/test_config.py` and any affected window tests.
4. Update this document.

Keep config nesting to the existing two-level structure unless the config
loader is updated. The current `config.get()` and `config.update()` helpers are
designed around keys such as `section.name`.
