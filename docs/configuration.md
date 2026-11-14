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
`Options -> Preferences...`. On macOS this appears as
`qPlot -> Preferences...`.

Python code can use the public `config` object directly:

```python
from qplot import config

cfg = config()
cfg.update("user_preference.theme", "dark")
cfg.dump()
```

## Config Behaviour

On startup, QCoDeS-Plotter creates `~/.qplot/config.json` if it does not exist.
Existing config files are validated against `config_schema.json`.

Every section and setting declared by the current schema is required, and extra
sections or settings are rejected. QCoDeS-Plotter does not migrate older or
incomplete settings files to the current format.

If the config file is old, incomplete, invalid JSON, or otherwise fails schema
validation, it is copied to `config.invalid.json` in `~/.qplot` and replaced
with the exact current schema defaults. If that backup already exists,
QCoDeS-Plotter uses the next available numbered backup, such as
`config.invalid.1.json`.

If backup or replacement fails, startup continues with defaults in memory and a
warning that recovery could not be saved. A failed backup prevents replacement
of the original. Unreadable settings are also left untouched while defaults are
used for that session. Check the diagnostic log and access permissions on the
settings file and directory before retrying; starting successfully does not
mean recovered settings were persisted.

Config keys use dotted paths in code and in `qplot-cfg`, for example
`user_preference.default_refresh_rate`.

## Key Reference

| Key | Type | Default | Validation | Purpose |
| --- | --- | --- | --- | --- |
| `GUI.plot_frame_fraction` | number | `0.47` | `0 < value < 1` | Fraction of a plot window used for the plot frame. |
| `GUI.main_frame_size` | integer array | `[900, 700]` | exactly 2 items, each `1 <= value <= 16777215` | Initial main-window width and height. |
| `GUI.preview_size` | integer | `200` | `50 <= value <= 1000` | Preview thumbnail size in pixels. |
| `GUI.run_table_column_widths` | integer array | `[]` | empty or exactly 12 items, each `32 <= value <= 16777215` | Saved run-table column widths in display order; an empty array uses responsive widths. |
| `GUI.run_table_visible_columns` | string array | `["run_id", "measurements", "setpoints", "started", "status", "duration", "size"]` | unique run-table column IDs | Columns shown in the run table, in the table's fixed display order. |
| `file.default_load_path` | string | `""` | any string | Default folder for selecting database files. |
| `file.last_testdata_csv_directory` | string | `""` | any string | Automatically remembered folder for test-data CSV selection and export. |
| `file.recent_file_paths` | string array | `[]` | at most 10 unique strings | Recent database files shown by the application, newest first. |
| `user_preference.csv_filename_format` | string | `"current"` | `current` or `qcodes` | Default CSV filename convention. |
| `user_preference.show_source_annotation` | boolean | `true` | `true` or `false` | Show source annotations in plot windows. |
| `user_preference.cursor_significant_figures` | integer | `6` | `1 <= value <= 17` | Significant figures in cursor readouts. |
| `user_preference.complex_1d` | string | `"magnitude"` | `magnitude`, `phase`, `real`, `imag`, `imag_vs_real` | Display projection for complex 1D measurements; phase is in degrees. |
| `user_preference.complex_nd` | string | `"magnitude"` | `magnitude`, `phase`, `real`, `imag` | Display projection for complex heatmaps and 3D slices; phase is in degrees. |
| `user_preference.font_size` | integer | `10` | `6 <= value <= 36` | Application font size in points. |
| `user_preference.plot_tile_rows` | integer | `2` | `1 <= value <= 10` | Rows in the plot tiling grid. |
| `user_preference.plot_tile_columns` | integer | `3` | `1 <= value <= 10` | Columns in the plot tiling grid. |
| `user_preference.colorbar_width` | integer | `15` | `1 <= value <= 500` | Heatmap colour bar width in pixels. |
| `user_preference.axis_tick_width` | number | `2` | `0.5 <= value <= 10` | Plot axis tick width. |
| `user_preference.axis_major_tick_count` | integer | `3` | `2 <= value <= 10` | Target major tick count for plot axes. |
| `user_preference.confirm_incompatible_axes` | boolean | `true` | `true` or `false` | Confirm drops with incompatible axis assignments. |
| `user_preference.theme` | string | `"light"` | `light`, `dark`, or `pyqt` | Active application theme. |
| `user_preference.bar_colour` | string | `"viridis"` | any string | Default 2D colour map name. |
| `user_preference.trace_colour` | string | `"#1f77b4"` | `#RRGGBB` hex colour | Initial line colour for new traces. |
| `user_preference.trace_line_width` | number | `2` | `0.5 <= value <= 15` | Initial line width for new traces, in pixels. |
| `user_preference.trace_line_style` | string | `"Solid"` | `Solid`, `Dash`, `Dot`, or `Dash Dot` | Initial line style for new traces. |
| `user_preference.bar_colour_excluded` | string array | `[]` | any strings | Hide colour maps with these exact names. |
| `user_preference.bar_colour_excluded_prefixes` | string array | `[]` | any strings | Hide colour maps with these prefixes. |
| `user_preference.confirm_close` | boolean | `false` | `true` or `false` | Ask before closing the main window. |
| `user_preference.confirm_close_all` | boolean | `true` | `true` or `false` | Ask before closing all plot windows. |
| `user_preference.auto_plot` | boolean | `false` | `true` or `false` | Automatically open newly detected runs. |
| `user_preference.copy_plot_image_resolution` | string | `"screen"` | `screen`, `dpi_300`, or `svg` | Format/resolution used by plot-window Copy Plot Image. |
| `user_preference.default_refresh_rate` | number | `1` | `0 <= value <= 10000` | Shared interval for the run table and all live plots, in seconds; zero disables automatic refresh everywhere. |
| `runtime_settings.max_threads` | integer | `4` | `1 <= value <= 999` | Maximum worker threads for background loading. |
| `runtime_settings.max_full_heatmap_points` | integer | `2000000` | `1 <= value <= 2000000000` | Maximum estimated points loaded at full resolution before 2D plot windows use SQL spatial aggregation. |
| `runtime_settings.max_heatmap_grid_cells` | integer | `250000` | `1 <= value <= 2000000000` | Total cell budget for an aggregated heatmap, shared proportionally across its axes. Preferences allows up to 2,000,000; actual aggregated allocations above that internal limit are rejected. |
| `runtime_settings.max_heatmap_grid_side` | integer | `800` | `1 <= value <= 2000000000` | Deprecated. Accepted for settings-file compatibility but ignored by the application; the total cell budget now determines axis limits. |
| `runtime_settings.del_grace_period` | number | `10` | `0 <= value <= 300` | Seconds to retain an unused plot dataset connection for quick reopening. |
| `runtime_settings.cloud_sync_timeout` | number | `120` | `1 <= value <= 3600` | Seconds to wait for cloud storage to hydrate a database before failing. |
| `updates.check_on_launch` | boolean | `true` | `true` or `false` | Check PyPI in the background after launch, at most once every 24 hours. |
| `updates.include_prereleases` | boolean | `false` | `true` or `false` | Include beta, release-candidate, and development releases in update checks. |
| `updates.last_check` | number | `0` | `0 <= value <= 253402300799` | Unix timestamp of the last attempted update check; maintained automatically. |

`Preferences -> Updates` controls launch checks and pre-release notifications.
Launch checks are disabled by default, time out after five seconds, and remain
quiet when offline or when no newer matching release is found. The timestamp
also records failed attempts so repeated launches do not repeatedly contact PyPI.
`Help -> Check for Updates…` always allows a manual check, even within the daily
interval. Checks offer release notes and upgrade instructions; installation is
performed separately after closing qPlot.

The 12 stored run-table widths follow the table's display order: ID,
Experiment, Sample, Measurements, Setpoints, Name, Started, Completed, Status,
Duration, Size, and GUID.

## Adding Config Keys

When adding or changing a config key:

1. Update `src/qplot/configuration/config_schema.json`.
2. Update the relevant code that reads or writes the key.
3. Add or update tests in `tests/test_config.py` and any affected window tests.
4. Update this document.

Keep config nesting to the existing two-level structure unless the config
loader is updated. The current `config.get()` and `config.update()` helpers are
designed around keys such as `section.name`.
